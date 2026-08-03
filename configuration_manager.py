from __future__ import annotations

from typing import Any, Dict, List


class ConfigurationManager:
    def __init__(self, db, mcu_writes, trigger_sender, logger):
        self.db = db
        self.mcu_writes = mcu_writes
        self.trigger_sender = trigger_sender
        self.logger = logger

    def catalog(self) -> Dict[str, Any]:
        return self.db.get_configuration_catalog()

    async def apply(self, robot_id: str, blade_id: str) -> Dict[str, Any]:
        profile = self.db.get_transition_profile(robot_id, blade_id)
        catalog = self.db.get_configuration_catalog()
        scale = float(catalog.get("encoder_scale", 1000.0))
        units = str(catalog.get("units", "m")).lower()
        values: List[float] = profile["effective_values"]
        thresholds = [int(round(v * scale)) for v in values] if units == "m" else [int(round(v)) for v in values]
        n = len(thresholds)
        triggers = [
            {"threshold": threshold, "activate": i,
             "deactivate": (i - 1) % n, "delay": 0.0 if i == 0 else 9.0}
            for i, threshold in enumerate(thresholds)
        ]
        await self.trigger_sender(
            triggers=triggers, clear_first=True,
            channel_count=int(profile["channels"]), default_delay_s=None,
            wait_for_ack=True, ack_timeout_s=3.0,
        )
        self.db.mark_profile_applied(profile)
        return {**profile, "thresholds": thresholds}

    async def save_override_and_apply(self, robot_id: str, blade_id: str,
                                      values: List[float], actor: str) -> Dict[str, Any]:
        profile = self.db.set_transition_override(
            robot_type_id=robot_id, blade_type_id=blade_id, values=values, actor=actor)
        try:
            return await self.apply(robot_id, blade_id)
        except Exception:
            self.logger.log.exception("Override saved, but MCU application failed.")
            raise

    async def revert_and_apply(self, robot_id: str, blade_id: str, actor: str) -> Dict[str, Any]:
        self.db.clear_transition_override(
            robot_type_id=robot_id, blade_type_id=blade_id, actor=actor)
        return await self.apply(robot_id, blade_id)

    def job_snapshot(self, robot_id: str, blade_id: str) -> Dict[str, Any]:
        profile = self.db.get_transition_profile(robot_id, blade_id)
        catalog = self.db.get_configuration_catalog()
        return {
            "configuration_schema_version": 2,
            "transition_profile_id": profile["profile_id"],
            "robot_type_id": robot_id,
            "blade_type_id": blade_id,
            "transition_values": list(profile["effective_values"]),
            "transition_source": profile["effective_source"],
            "used_operator_override": profile["has_override"],
            "default_revision": profile["default_revision"],
            "override_revision": profile["override_revision"],
            "units": catalog["units"],
            "encoder_scale": catalog["encoder_scale"],
            "motor_direction": catalog["motor_direction"],
            "motor_tuning": catalog["motor_tuning"],
        }

    def create_blade_type(
        self,
        *,
        blade_id: str,
        name: str,
        max_transitions: int,
        copy_from_blade_id: str,
        actor: str,
    ) -> Dict[str, Any]:
        return self.db.create_blade_type(
            blade_id=blade_id,
            name=name,
            max_transitions=max_transitions,
            copy_from_blade_id=(
                copy_from_blade_id
            ),
            actor=actor,
        )


    def update_blade_type(
        self,
        *,
        blade_id: str,
        name: str,
        max_transitions: int,
        actor: str,
    ) -> Dict[str, Any]:
        return self.db.update_blade_type(
            blade_id=blade_id,
            name=name,
            max_transitions=max_transitions,
            actor=actor,
        )


    def archive_blade_type(
        self,
        blade_id: str,
        actor: str,
    ) -> None:
        self.db.archive_blade_type(
            blade_id,
            actor,
        )


    def restore_blade_type(
        self,
        blade_id: str,
        actor: str,
    ) -> None:
        self.db.restore_blade_type(
            blade_id,
            actor,
        )


    def delete_blade_type(
        self,
        blade_id: str,
        actor: str,
    ) -> None:
        self.db.delete_blade_type(
            blade_id,
            actor,
        )