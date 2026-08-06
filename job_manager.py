from __future__ import annotations

from typing import Any, Dict, Optional
from uuid import uuid4

from database_service import DatabaseService


class JobManager:
    """Owns persistent job lifecycle and centralized event enrichment."""

    def __init__(self, db: DatabaseService):
        self.db = db
        self.robot_identity = self.db.get_robot_identity()
        self.current_job: Optional[Dict[str, Any]] = self.db.get_active_job()
        self.encoder_context_provider = None

    def set_encoder_context_provider(self, provider) -> None:
        self.encoder_context_provider = provider

    def update_identity(self, fw: Dict[str, Any]) -> None:
        self.robot_identity = {
            "model": fw.get("model"), "fleet_id": fw.get("fleet_id"),
            "semver": fw.get("semver"), "git": fw.get("git"),
        }
        self.db.update_robot_identity(fw)

    def get_active_job(self) -> Optional[Dict[str, Any]]:
        self.current_job = self.db.get_active_job()
        return self.current_job

    def start_job(
        self,
        *,
        operator_initials: str,
        blade_serial: str,
        blade_type_id: str,
        blade_type_name: str,
        configuration_snapshot: Optional[Dict[str, Any]] = None,
    ) -> Dict[str, Any]:
        operator_initials = operator_initials.strip().upper()
        blade_serial = blade_serial.strip().upper()
        blade_type_id = blade_type_id.strip()
        blade_type_name = blade_type_name.strip()
        self._validate_start_job(
            operator_initials=operator_initials,
            blade_serial=blade_serial,
            blade_type_id=blade_type_id,
            blade_type_name=blade_type_name,
        )
        if self.db.get_active_job() is not None:
            raise ValueError("Another job is already active.")
        job_uuid = str(uuid4())
        snapshot = dict(configuration_snapshot or {})
        snapshot.setdefault("blade_type_id", blade_type_id)
        snapshot.setdefault("blade_type_name", blade_type_name)
        snapshot.setdefault("robot_identity", dict(self.robot_identity))
        job = self.db.start_job(
            job_uuid=job_uuid,
            operator_initials=operator_initials,
            blade_serial=blade_serial,
            blade_type_id=blade_type_id,
            blade_type_name=blade_type_name,
            robot_model=self.robot_identity.get("model"),
            fleet_id=self.robot_identity.get("fleet_id"),
            firmware_version=self.robot_identity.get("semver"),
            firmware_git=self.robot_identity.get("git"),
            configuration_snapshot=snapshot,
        )
        self.current_job = job
        return job

    def mark_starting(self):
        return self._set_active_state("STARTING")

    def mark_running(self):
        return self._set_active_state("RUNNING")

    def _set_active_state(self, state: str):
        job = self.get_active_job()
        if job is None:
            return None
        updated = self.db.update_job_state(job["job_uuid"], state)
        self.current_job = updated
        return updated

    def end_job(
        self,
        *,
        job_uuid: str,
        result: str,
        failure_reason: Optional[str] = None,
    ) -> Dict[str, Any]:
        active_job = self.get_active_job()

        if active_job is None:
            raise ValueError("There is no active job.")

        if active_job["job_uuid"] != job_uuid:
            raise ValueError(
                "The requested job is not the active job."
            )

        completed_job = self.db.end_job(
            job_uuid,
            result,
            failure_reason=failure_reason,
        )

        if completed_job is None:
            raise ValueError("The job could not be found.")

        self.current_job = None
        return completed_job


    def log_event(
        self,
        event_type: str,
        data: Any,
        *,
        include_encoder_context: bool = True,
    ) -> None:
        job = self.get_active_job()
        if job is None:
            return
        event_data = dict(data) if isinstance(data, dict) else {"value": data}
        if include_encoder_context and "encoder_context" not in event_data:
            if self.encoder_context_provider is None:
                event_data["encoder_context"] = {
                    "available": False,
                    "valid": False,
                    "source": "provider_not_configured",
                    "position_interpretation": "position_unknown",
                }
            else:
                try:
                    event_data["encoder_context"] = self.encoder_context_provider(job)
                except Exception as error:
                    event_data["encoder_context"] = {
                        "available": False,
                        "valid": False,
                        "source": "provider_error",
                        "error": str(error),
                        "position_interpretation": "position_unknown",
                    }
        self.db.insert_job_event(job["job_uuid"], event_type, event_data)

    @staticmethod
    def _validate_start_job(
        *, operator_initials: str, blade_serial: str,
        blade_type_id: str, blade_type_name: str,
    ) -> None:
        if not 2 <= len(operator_initials) <= 6 or not operator_initials.isalpha():
            raise ValueError("Operator initials must contain 2 to 6 letters.")
        if len(blade_serial) < 3:
            raise ValueError("Blade serial must contain at least 3 characters.")
        if not blade_type_id:
            raise ValueError("Blade type ID is required.")
        if not blade_type_name:
            raise ValueError("Blade type name is required.")
