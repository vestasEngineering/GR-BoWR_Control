from __future__ import annotations

import asyncio
from copy import deepcopy
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional
from uuid import uuid4


TERMINAL_RUN_STATES = {
    "passed",
    "failed",
    "aborted",
    "timed_out",
}

TERMINAL_PLAN_STATES = {
    "completed",
    "completed_with_issues",
    "aborted",
    "inconclusive",
}


class DiagnosticPlanRuntime:
    """Owns one in-memory, serialized full-system diagnostic plan.

    Individual diagnostic runs remain authoritative in ServiceRuntime and the
    existing diagnostic_runs table. This coordinator only sequences those runs
    and exposes a stable plan view to the HMI.
    """

    def __init__(self, *, service_runtime, logger):
        self.service_runtime = service_runtime
        self.logger = logger
        self._lock = asyncio.Lock()
        self._plan: Optional[Dict[str, Any]] = None
        self._task: Optional[asyncio.Task] = None
        self._abort_requested = False

    @staticmethod
    def _now() -> str:
        return datetime.now(timezone.utc).isoformat()

    @property
    def active(self) -> bool:
        plan = self._plan
        return bool(plan and plan.get("state") not in TERMINAL_PLAN_STATES)

    @property
    def active_plan_id(self) -> Optional[str]:
        return str(self._plan["plan_id"]) if self.active and self._plan else None

    async def start(
        self,
        *,
        policy: str,
        actor: Optional[str],
        server,
    ) -> Dict[str, Any]:
        normalized_policy = str(policy or "").strip().lower()
        if normalized_policy != "continue_independent_tests":
            raise ValueError("Unsupported diagnostic plan policy.")

        async with self._lock:
            if self.active:
                raise RuntimeError("A full system diagnostic plan is already running.")
            if self.service_runtime._active_run_id is not None:
                raise RuntimeError("An individual diagnostic is already running.")

            catalog = self.service_runtime.diagnostic_catalog(server)
            eligible = [item for item in catalog if item.get("allowed") is True]
            if not eligible:
                raise RuntimeError("No diagnostics are currently available for the plan.")

            plan_id = str(uuid4())
            items: List[Dict[str, Any]] = []
            for index, definition in enumerate(eligible):
                items.append({
                    "item_id": f"{plan_id}:{index + 1}",
                    "diagnostic_id": str(definition["id"]),
                    "module_id": str(definition["module_id"]),
                    "name": str(definition["name"]),
                    "state": "queued",
                    "result": None,
                    "skip_reason": None,
                    "run_id": None,
                    "started_at": None,
                    "completed_at": None,
                })

            self._plan = {
                "plan_id": plan_id,
                "state": "running",
                "policy": normalized_policy,
                "started_at": self._now(),
                "completed_at": None,
                "active_item_id": None,
                "message": "Full system diagnostic plan started.",
                "requested_by": actor,
                "items": items,
            }
            self._abort_requested = False
            self._task = asyncio.create_task(
                self._run(plan_id=plan_id, server=server, actor=actor),
                name=f"diagnostic-plan-{plan_id}",
            )
            return self.snapshot(plan_id)

    def snapshot(self, plan_id: str) -> Dict[str, Any]:
        plan = self._plan
        if plan is None or str(plan.get("plan_id")) != str(plan_id):
            raise ValueError("Diagnostic plan not found.")
        return deepcopy(plan)

    async def abort(self, plan_id: str) -> Dict[str, Any]:
        async with self._lock:
            plan = self._require_plan(plan_id)
            if plan["state"] in TERMINAL_PLAN_STATES:
                return deepcopy(plan)

            self._abort_requested = True
            active_run_id = self.service_runtime._active_run_id
            if active_run_id:
                try:
                    await self.service_runtime.abort(active_run_id)
                except Exception:
                    self.logger.log.exception(
                        "Failed to abort the active diagnostic while aborting plan %s.",
                        plan_id,
                    )

            plan["state"] = "aborted"
            plan["message"] = "Full system diagnostic plan aborted by the operator."
            plan["completed_at"] = self._now()
            plan["active_item_id"] = None

            for item in plan["items"]:
                if item["state"] == "running":
                    item["state"] = "aborted"
                    item["completed_at"] = self._now()
                elif item["state"] == "queued":
                    item["skip_reason"] = "plan_aborted"
                    item["completed_at"] = self._now()

            return deepcopy(plan)

    async def _run(self, *, plan_id: str, server, actor: Optional[str]) -> None:
        try:
            plan = self._require_plan(plan_id)

            for item in plan["items"]:
                if self._abort_requested:
                    break

                plan["active_item_id"] = item["item_id"]
                item["state"] = "running"
                item["started_at"] = self._now()

                try:
                    run = await self.service_runtime.start(
                        diagnostic_id=item["diagnostic_id"],
                        module_id=item["module_id"],
                        actor=actor,
                        server=server,
                    )
                    item["run_id"] = run["run_id"]
                    completed = await self._wait_for_run(run["run_id"])
                    item["state"] = completed["state"]
                    item["result"] = completed.get("result")
                    item["completed_at"] = completed.get("completed_at") or self._now()

                except asyncio.CancelledError:
                    raise

                except Exception as error:
                    self.logger.log.exception(
                        "Diagnostic plan item failed to execute: plan_id=%s module_id=%s",
                        plan_id,
                        item["module_id"],
                    )
                    item["state"] = "failed"
                    item["result"] = {
                        "type": "test_result",
                        "id": item["module_id"],
                        "pass": False,
                        "reason": "plan_item_execution_failed",
                        "measurements": {"error": str(error)},
                    }
                    item["completed_at"] = self._now()

            if self._abort_requested:
                return

            failed = any(
                item["state"] in {"failed", "timed_out", "aborted"}
                or item.get("skip_reason") is not None
                for item in plan["items"]
            )
            plan["state"] = "completed_with_issues" if failed else "completed"
            plan["message"] = (
                "Full system check completed with one or more issues."
                if failed
                else "Full system check completed successfully."
            )
            plan["completed_at"] = self._now()
            plan["active_item_id"] = None

        except asyncio.CancelledError:
            plan = self._plan
            if plan and plan.get("plan_id") == plan_id and plan["state"] not in TERMINAL_PLAN_STATES:
                plan["state"] = "inconclusive"
                plan["message"] = "Diagnostic plan coordinator stopped before completion."
                plan["completed_at"] = self._now()
                plan["active_item_id"] = None
            raise

        except Exception:
            self.logger.log.exception("Diagnostic plan coordinator failed: plan_id=%s", plan_id)
            plan = self._plan
            if plan and plan.get("plan_id") == plan_id:
                plan["state"] = "inconclusive"
                plan["message"] = "Diagnostic plan coordinator failed."
                plan["completed_at"] = self._now()
                plan["active_item_id"] = None

    async def _wait_for_run(self, run_id: str) -> Dict[str, Any]:
        while True:
            run = self.service_runtime.status(run_id)
            if str(run.get("state", "")).lower() in TERMINAL_RUN_STATES:
                return run
            if self._abort_requested:
                raise RuntimeError("Diagnostic plan was aborted.")
            await asyncio.sleep(0.2)

    def _require_plan(self, plan_id: str) -> Dict[str, Any]:
        plan = self._plan
        if plan is None or str(plan.get("plan_id")) != str(plan_id):
            raise ValueError("Diagnostic plan not found.")
        return plan

    async def shutdown(self) -> None:
        task = self._task
        self._task = None
        if task is not None and not task.done():
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
