from __future__ import annotations

import asyncio
import math
import time
from datetime import datetime, timezone
from enum import Enum
from typing import Any, Awaitable, Callable, Dict, Optional
from uuid import uuid4


class EncoderRecoveryState(str, Enum):
    STARTUP_RECOVERY = "startup_recovery"
    VALID = "valid"
    POWER_LOST = "power_lost"
    WAITING_FOR_RESTORE = "waiting_for_restore"
    RESTORING = "restoring"
    RESTORE_FAILED = "restore_failed"
    OPERATOR_ACTION_REQUIRED = "operator_action_required"


class EncoderPersistenceCoordinator:
    """Owns accepted encoder session, persistence, recovery, and job origin."""

    def __init__(
        self,
        *,
        db,
        mcu_writes: asyncio.Queue,
        encoder_acks: asyncio.Queue,
        logger,
        publish: Optional[Callable[[Dict[str, Any]], Awaitable[None]]] = None,
        checkpoint_period_s: float = 2.0,
        checkpoint_distance_mm: float = 25.0,
        restore_tolerance_mm: float = 5.0,
        transaction_timeout_s: float = 8.0,
    ):
        self.db = db
        self.mcu_writes = mcu_writes
        self.encoder_acks = encoder_acks
        self.logger = logger
        self.publish = publish
        self.checkpoint_period_s = max(0.5, float(checkpoint_period_s))
        self.checkpoint_distance_mm = max(0.0, float(checkpoint_distance_mm))
        self.restore_tolerance_mm = max(0.0, float(restore_tolerance_mm))
        self.transaction_timeout_s = max(1.0, float(transaction_timeout_s))
        self.state = EncoderRecoveryState.STARTUP_RECOVERY
        self.accepted_session_id: Optional[int] = None
        self.motor_power_present = False
        self._latest_h7_session: Optional[int] = None
        self._last_valid: Optional[Dict[str, Any]] = None
        self._pending: Optional[Dict[str, Any]] = None
        self._last_saved_mm: Optional[float] = None
        self._last_saved_monotonic = 0.0
        self._lock = asyncio.Lock()
        self._transaction_lock = asyncio.Lock()
        self._session_ready = asyncio.Event()
        self._restore_task: Optional[asyncio.Task] = None

    @staticmethod
    def _utc_now() -> str:
        return datetime.now(timezone.utc).isoformat()

    async def _emit(self, message: str, **extra: Any) -> None:
        payload = {
            "type": "encoder_recovery",
            "state": self.state.value,
            "valid": self.state == EncoderRecoveryState.VALID,
            "position_frozen": self.state != EncoderRecoveryState.VALID,
            "motor_power_present": self.motor_power_present,
            "encoder_session_id": self._latest_h7_session,
            "last_valid_rear_distance_mm": (
                None if self._last_valid is None
                else self._last_valid["rear_distance_mm"]
            ),
            "message": message,
            "physical_movement_warning": (
                "Movement while motor controllers are unpowered cannot be detected."
            ),
        }
        payload.update(extra)
        if self.publish is not None:
            await self.publish(payload)

    async def start_serial_runtime(self) -> None:
        async with self._lock:
            self.state = EncoderRecoveryState.STARTUP_RECOVERY
            self.accepted_session_id = None
            self._latest_h7_session = None
            self.motor_power_present = False
            self._pending = None
            self._session_ready.clear()
        await self._emit(
            "Encoder telemetry is quarantined until hardware state is established."
        )

    def event_context_snapshot(
        self,
        active_job: Optional[Dict[str, Any]] = None,
    ) -> Dict[str, Any]:
        """Synchronous snapshot used when inserting a significant job event."""
        if self._last_valid is not None:
            sample_monotonic = self._last_valid.get("received_monotonic")
            age_ms = None
            if sample_monotonic is not None:
                age_ms = max(0, int((time.monotonic() - sample_monotonic) * 1000.0))
            currently_valid = (
                self.state == EncoderRecoveryState.VALID
                and self.accepted_session_id
                == self._last_valid.get("encoder_session_id")
            )
            return {
                "available": True,
                "valid": currently_valid,
                "position_frozen": not currently_valid,
                "rear_distance_mm": self._last_valid["rear_distance_mm"],
                "radius_m": self._last_valid["rear_distance_mm"] / 1000.0,
                "counts": list(self._last_valid.get("counts", [])),
                "encoder_session_id": self._last_valid.get("encoder_session_id"),
                "sampled_at": self._last_valid.get("sampled_at"),
                "age_ms": age_ms,
                "source": (
                    "live_valid_telemetry" if currently_valid
                    else "last_valid_telemetry"
                ),
                "position_interpretation": (
                    "current_valid_position" if currently_valid
                    else "last_known_valid_position"
                ),
            }
        if active_job is not None and active_job.get("encoder_distance_mm") is not None:
            distance = float(active_job["encoder_distance_mm"])
            return {
                "available": True,
                "valid": False,
                "position_frozen": True,
                "rear_distance_mm": distance,
                "radius_m": distance / 1000.0,
                "counts": list(active_job.get("encoder_counts") or []),
                "encoder_session_id": active_job.get("encoder_session_id"),
                "checkpoint_sequence": int(
                    active_job.get("encoder_checkpoint_sequence") or 0
                ),
                "sampled_at": active_job.get("encoder_checkpoint_at"),
                "age_ms": None,
                "source": "durable_job_checkpoint",
                "position_interpretation": "last_durable_checkpoint",
            }
        return {
            "available": False,
            "valid": False,
            "position_frozen": True,
            "rear_distance_mm": None,
            "radius_m": None,
            "counts": [],
            "encoder_session_id": None,
            "sampled_at": None,
            "age_ms": None,
            "source": "unavailable",
            "position_interpretation": "position_unknown",
        }

    async def handle_session_event(self, msg: Dict[str, Any]) -> None:
        sid = int(msg.get("encoder_session_id", 0))
        reported_state = str(msg.get("state", "")).lower()
        self._latest_h7_session = sid
        self.motor_power_present = msg.get("motor_power_present") is True
        if self.motor_power_present and reported_state in {
            "waiting_for_restore", "startup_recovery", "valid"
        }:
            self._session_ready.set()
        if reported_state == "power_lost":
            async with self._lock:
                self.state = EncoderRecoveryState.POWER_LOST
                self.accepted_session_id = None
                self._pending = None
            job = await asyncio.to_thread(self.db.get_active_job)
            if job is not None:
                await asyncio.to_thread(
                    self.db.mark_job_encoder_recovery_required,
                    job["job_uuid"], session_id=sid, state="power_lost",
                    reason="motor_controller_power_lost",
                )
            await self._emit("Motor-controller power lost. Encoder position is frozen.")
        elif reported_state == "waiting_for_restore":
            async with self._lock:
                self.state = EncoderRecoveryState.WAITING_FOR_RESTORE
                self.accepted_session_id = None
                self._pending = None
            job = await asyncio.to_thread(self.db.get_active_job)
            if job is not None:
                await asyncio.to_thread(
                    self.db.mark_job_encoder_recovery_required,
                    job["job_uuid"], session_id=sid, state="waiting_for_restore",
                    reason="motor_controller_power_restored",
                )
            await self._emit(
                "Motor-controller power restored. Waiting to restore position."
            )
            if self._restore_task is None or self._restore_task.done():
                self._restore_task = asyncio.create_task(self.restore_active_job())

    async def observe(
        self,
        message: Dict[str, Any],
        *,
        force: bool = False,
    ) -> Optional[Dict[str, Any]]:
        try:
            distance = float(message["rear_distance_mm"])
            counts = list(message.get("counts") or [])
            sid = int(message.get("encoder_session_id", -1))
            sample_valid = message.get("valid") is True
            if not math.isfinite(distance) or len(counts) != 4:
                return None
            if (
                self.state != EncoderRecoveryState.VALID
                or not sample_valid
                or sid != self.accepted_session_id
            ):
                return None
            snapshot = {
                "type": "encoder",
                "valid": True,
                "encoder_session_id": sid,
                "rear_distance_mm": distance,
                "radius_m": distance / 1000.0,
                "counts": [int(value) for value in counts],
                "sampled_at": self._utc_now(),
                "received_monotonic": time.monotonic(),
            }
            self._last_valid = snapshot
            async with self._lock:
                self._pending = snapshot
                elapsed = time.monotonic() - self._last_saved_monotonic
                changed = (
                    self._last_saved_mm is None
                    or abs(distance - self._last_saved_mm)
                    >= self.checkpoint_distance_mm
                )
                if force or elapsed >= self.checkpoint_period_s or changed:
                    await self._flush_locked()
            # Strip private timing metadata before forwarding to Flutter.
            return {k: v for k, v in snapshot.items() if k != "received_monotonic"}
        except Exception:
            self.logger.log.exception("Failed to process encoder telemetry checkpoint.")
            return None

    async def flush(self) -> None:
        async with self._lock:
            await self._flush_locked()

    async def _flush_locked(self) -> None:
        if self._pending is None or self.state != EncoderRecoveryState.VALID:
            return
        active_job = await asyncio.to_thread(self.db.get_active_job)
        if active_job is None:
            self._pending = None
            return
        snapshot = self._pending
        updated = await asyncio.to_thread(
            self.db.update_job_encoder_checkpoint,
            active_job["job_uuid"],
            rear_distance_mm=snapshot["rear_distance_mm"],
            counts=snapshot["counts"],
            session_id=snapshot["encoder_session_id"],
            source="h7_encoder_telemetry",
        )
        if updated is not None:
            self._last_saved_mm = snapshot["rear_distance_mm"]
            self._last_saved_monotonic = time.monotonic()
            self._pending = None

    async def startup_recovery(self, *, ready_timeout_s: float = 20.0):
        active_job = await asyncio.to_thread(self.db.get_active_job)
        await asyncio.wait_for(self._session_ready.wait(), timeout=ready_timeout_s)
        if active_job is not None and active_job.get("encoder_distance_mm") is not None:
            return await self.restore_active_job()
        self.state = EncoderRecoveryState.OPERATOR_ACTION_REQUIRED
        await self._emit(
            "No active-job checkpoint exists. A new job or explicit encoder set/reset "
            "must establish a valid coordinate origin."
        )
        return None

    async def restore_active_job(self):
        async with self._transaction_lock:
            job = await asyncio.to_thread(self.db.get_active_job)
            if job is None or job.get("encoder_distance_mm") is None:
                return None
            if not self.motor_power_present or self._latest_h7_session is None:
                return None
            expected = float(job["encoder_distance_mm"])
            return await self._perform_transaction(
                job=job,
                position_mm=expected,
                source="job_recovery",
                success_event_type="encoder_restoration_succeeded",
            )

    async def create_job_at_zero(self, *, create_job) -> Dict[str, Any]:
        """Create one READY job and establish its verified, job-scoped zero origin."""
        async with self._transaction_lock:
            if not self.motor_power_present or self._latest_h7_session is None:
                raise RuntimeError(
                    "Motor-controller power is unavailable. The job cannot be initialized."
                )
            self.state = EncoderRecoveryState.RESTORING
            self.accepted_session_id = None
            self._pending = None
            before_context = self.event_context_snapshot(None)
            await self._emit(
                "Creating a new job and resetting its encoder origin to zero. "
                "Motion remains stopped.",
                source="new_job_initialization",
            )
            job = None
            try:
                # RESTORING was set before this insert, so prior-job telemetry
                # cannot be checkpointed against the new job.
                job = await asyncio.to_thread(create_job)
                result = await self._perform_transaction(
                    job=job,
                    position_mm=0.0,
                    source="new_job_initialization",
                    success_event_type="new_job_encoder_origin_established",
                    reset=True,
                    before_context=before_context,
                )
                refreshed = await asyncio.to_thread(
                    self.db.get_job_by_uuid, job["job_uuid"]
                )
                if refreshed is None:
                    raise RuntimeError("Initialized job could not be reloaded.")
                return {"job": refreshed, "encoder": result}
            except Exception as error:
                self._pending = None
                self.accepted_session_id = None
                self.state = EncoderRecoveryState.OPERATOR_ACTION_REQUIRED
                if job is not None:
                    try:
                        await asyncio.to_thread(
                            self.db.update_job_state,
                            job["job_uuid"],
                            "FAILED",
                            result="INITIALIZATION_FAILED",
                            failure_reason="ENCODER_RESET_FAILED",
                        )
                    except Exception:
                        self.logger.log.exception(
                            "Failed to mark partially created job as failed."
                        )
                await self._emit(
                    "The new job encoder origin could not be initialized. "
                    "Operator action is required.",
                    source="new_job_initialization",
                    error=str(error),
                )
                raise

    async def operator_set(self, position_mm: float):
        value = float(position_mm)
        if not math.isfinite(value):
            raise ValueError("Encoder position must be finite.")
        async with self._transaction_lock:
            self._require_powered_session()
            job = await asyncio.to_thread(self.db.get_active_job)
            return await self._perform_transaction(
                job=job,
                position_mm=value,
                source="operator_set",
                success_event_type="operator_set_encoder",
            )

    async def operator_reset(self):
        async with self._transaction_lock:
            self._require_powered_session()
            job = await asyncio.to_thread(self.db.get_active_job)
            return await self._perform_transaction(
                job=job,
                position_mm=0.0,
                source="operator_reset",
                success_event_type="operator_reset_encoder",
                reset=True,
            )

    def _require_powered_session(self) -> None:
        if not self.motor_power_present or self._latest_h7_session is None:
            raise RuntimeError("Motor-controller power is unavailable.")

    async def _perform_transaction(
        self,
        *,
        job,
        position_mm: float,
        source: str,
        success_event_type: str,
        reset: bool = False,
        before_context: Optional[Dict[str, Any]] = None,
    ) -> Dict[str, Any]:
        sid = int(self._latest_h7_session)
        transaction_id = f"enc-{uuid4()}"
        if before_context is None:
            before_context = self.event_context_snapshot(job)
        self.state = EncoderRecoveryState.RESTORING
        self.accepted_session_id = None
        self._pending = None
        await self._emit(
            "Encoder position transaction in progress. Motion remains stopped.",
            transaction_id=transaction_id,
            source=source,
        )
        self._drain_acks()
        command = {
            "action": "reset_encoder" if reset else "set_encoder",
            "transaction_id": transaction_id,
            "source": source,
            "expected_session_id": sid,
        }
        if not reset:
            command["position_mm"] = float(position_mm)
        await self.mcu_writes.put(command)
        try:
            ack = await self._wait_for_ack(
                transaction_id,
                sid,
                float(position_mm),
                self.transaction_timeout_s,
            )
            confirmed = float(ack["rear_distance_mm"])
            counts = [int(value) for value in ack.get("counts", [])]
            if len(counts) != 4:
                raise RuntimeError("Encoder acknowledgement omitted four counts.")
            sampled_at = self._utc_now()
            self.accepted_session_id = sid
            self.state = EncoderRecoveryState.VALID
            self._last_valid = {
                "type": "encoder",
                "valid": True,
                "encoder_session_id": sid,
                "rear_distance_mm": confirmed,
                "radius_m": confirmed / 1000.0,
                "counts": counts,
                "sampled_at": sampled_at,
                "received_monotonic": time.monotonic(),
            }
            self._last_saved_mm = confirmed
            self._last_saved_monotonic = time.monotonic()
            if job is not None:
                await asyncio.to_thread(
                    self.db.mark_job_encoder_transaction_succeeded,
                    job["job_uuid"],
                    requested_distance_mm=float(position_mm),
                    confirmed_distance_mm=confirmed,
                    session_id=sid,
                    counts=counts,
                    source=source,
                    transaction_id=transaction_id,
                    event_type=success_event_type,
                    before_context=before_context,
                )
            await self._emit(
                "Encoder position established. Motion remains stopped.",
                transaction_id=transaction_id,
                source=source,
                rear_distance_mm=confirmed,
                radius_m=confirmed / 1000.0,
            )
            return {
                "type": "encoder_transaction",
                "ok": True,
                "transaction_id": transaction_id,
                "source": source,
                "encoder_session_id": sid,
                "rear_distance_mm": confirmed,
                "radius_m": confirmed / 1000.0,
                "counts": counts,
            }
        except Exception as error:
            self.state = EncoderRecoveryState.OPERATOR_ACTION_REQUIRED
            if job is not None:
                await asyncio.to_thread(
                    self.db.mark_job_encoder_restore_failed,
                    job["job_uuid"],
                    reason=str(error),
                    requested_distance_mm=float(position_mm),
                    session_id=sid,
                    event_type=f"{source}_failed",
                    encoder_context=before_context,
                )
            await self._emit(
                "Encoder position transaction failed. Operator action is required.",
                transaction_id=transaction_id,
                source=source,
                error=str(error),
            )
            raise

    def _drain_acks(self) -> None:
        while True:
            try:
                self.encoder_acks.get_nowait()
            except asyncio.QueueEmpty:
                return

    async def _wait_for_ack(
        self,
        transaction_id: str,
        session_id: int,
        expected_mm: float,
        timeout_s: float,
    ) -> Dict[str, Any]:
        deadline = asyncio.get_running_loop().time() + timeout_s
        while True:
            remaining = deadline - asyncio.get_running_loop().time()
            if remaining <= 0.0:
                raise asyncio.TimeoutError("Timed out waiting for encoder acknowledgement.")
            msg = await asyncio.wait_for(self.encoder_acks.get(), timeout=remaining)
            if msg.get("transaction_id") != transaction_id:
                continue
            if int(msg.get("encoder_session_id", -1)) != session_id:
                raise RuntimeError("Encoder session mismatch.")
            if msg.get("ok") is not True or msg.get("valid") is not True:
                raise RuntimeError(
                    f"{msg.get('error', 'encoder_operation_failed')}; "
                    f"encoder_read_mask={msg.get('encoder_read_mask')}; "
                    f"restore_attempts={msg.get('restore_attempts')}; "
                    f"maximum_error_mm={msg.get('maximum_error_mm')}"
                )
            actual = float(msg["rear_distance_mm"])
            if abs(actual - expected_mm) > self.restore_tolerance_mm:
                raise RuntimeError(
                    f"Encoder verification failed: expected {expected_mm:.3f} mm, "
                    f"got {actual:.3f} mm."
                )
            return msg
