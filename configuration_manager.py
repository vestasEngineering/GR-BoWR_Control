from __future__ import annotations

from typing import Any, Dict, List


class ConfigurationManager:
    def __init__(self, db, mcu_writes, trigger_sender, actuator_extension_manager, logger):
        self.db = db
        self.mcu_writes = mcu_writes
        self.trigger_sender = trigger_sender
        self.actuator_extension_manager = actuator_extension_manager
        self.logger = logger

    def catalog(self) -> Dict[str, Any]:
        return self.db.get_configuration_catalog()

    def applied_snapshot(self) -> Dict[str, Any] | None:
        return self.db.get_applied_transition_profile_snapshot()

    async def apply(
        self,
        robot_id: str,
        blade_id: str,
    ) -> Dict[str, Any]:
        """
        Apply one complete transition configuration to the H7.

        The applied configuration database snapshot is updated only after:

        1. The H7 confirms the actuator extension voltage.
        2. The H7 confirms trigger-table reconciliation.

        This method does not start or resume motion.
        """
        robot_id = str(robot_id).strip()
        blade_id = str(blade_id).strip()

        if not robot_id:
            raise ValueError(
                "robot_id is required."
            )

        if not blade_id:
            raise ValueError(
                "blade_id is required."
            )

        profile = self.db.get_transition_profile(
            robot_id,
            blade_id,
        )

        catalog = self.db.get_configuration_catalog()

        scale = float(
            catalog.get(
                "encoder_scale",
                1000.0,
            )
        )

        units = str(
            catalog.get(
                "units",
                "m",
            )
        ).strip().lower()

        values: List[float] = list(
            profile["effective_values"]
        )

        if not values:
            raise ValueError(
                "The transition profile contains no values."
            )

        if units == "m":
            thresholds = [
                int(round(value * scale))
                for value in values
            ]
        else:
            thresholds = [
                int(round(value))
                for value in values
            ]

        transition_count = len(thresholds)

        triggers = [
            {
                "threshold": threshold,
                "activate": index,
                "deactivate": (
                    index - 1
                ) % transition_count,
                "delay": (
                    0.0
                    if index == 0
                    else 9.0
                ),
                "hold_active":
                    transition_count == 1,
            }
            for index, threshold
            in enumerate(thresholds)
        ]

        actuator_result = (
            await self.actuator_extension_manager.apply(
                robot_type_id=robot_id,
                blade_type_id=blade_id,
                voltage=float(
                    profile[
                        "effective_actuator_voltage"
                    ]
                ),
            )
        )

        await self.trigger_sender(
            triggers=triggers,
            clear_first=True,
            channel_count=int(
                profile["channels"]
            ),
            default_delay_s=None,
            wait_for_ack=True,
            ack_timeout_s=5.0,
        )

        self.db.mark_profile_applied(
            profile
        )

        return {
            **profile,
            "thresholds":
                thresholds,
            "actuator_extension_transaction_id":
                actuator_result.transaction_id,
            "applied_actuator_voltage":
                actuator_result.voltage,
        }

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

    def job_snapshot(
        self,
        robot_id: str,
        blade_id: str,
    ) -> Dict[str, Any]:
        profile = self.db.get_transition_profile(
            robot_id,
            blade_id,
        )

        catalog = self.db.get_configuration_catalog()

        return {
            "configuration_schema_version": 3,
            "transition_profile_id":
                profile["profile_id"],
            "robot_type_id":
                robot_id,
            "blade_type_id":
                blade_id,
            "transition_values":
                list(
                    profile["effective_values"]
                ),
            "transition_source":
                profile["effective_source"],
            "used_operator_override":
                profile["has_override"],
            "default_revision":
                profile["default_revision"],
            "override_revision":
                profile["override_revision"],
            "actuator_extension_voltage":
                float(
                    profile[
                        "effective_actuator_voltage"
                    ]
                ),
            "actuator_extension_source": (
                "operator_override"
                if profile[
                    "actuator_voltage_has_override"
                ]
                else "factory_default"
            ),
            "actuator_extension_override_revision":
                int(
                    profile[
                        "actuator_voltage_override_revision"
                    ]
                ),
            "units":
                catalog["units"],
            "encoder_scale":
                catalog["encoder_scale"],
            "motor_direction":
                catalog["motor_direction"],
            "motor_tuning":
                catalog["motor_tuning"],
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
        
    def save_motor_direction(
        self,
        directions: Dict[str, int],
    ) -> None:
        self.db.save_configuration_state(
            motor_direction=directions,
        )