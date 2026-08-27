from __future__ import annotations

import asyncio
import uuid
from dataclasses import dataclass
from typing import Any, Dict, Mapping


FACTORY_MOTOR_DIRECTIONS: Dict[str, int] = {
    "motor_1": 1,
    "motor_2": -1,
    "motor_3": -1,
    "motor_4": 1,
}

MOTOR_DIRECTION_KEYS = (
    "motor_1",
    "motor_2",
    "motor_3",
    "motor_4",
)


def validate_motor_directions(
    directions: Mapping[str, Any],
) -> Dict[str, int]:
    if not isinstance(
        directions,
        Mapping,
    ):
        raise ValueError(
            "Motor directions must be "
            "a JSON object."
        )

    missing = [
        key
        for key in MOTOR_DIRECTION_KEYS
        if key not in directions
    ]

    extra = [
        key
        for key in directions
        if key not in MOTOR_DIRECTION_KEYS
    ]

    if missing:
        raise ValueError(
            "Missing motor directions: "
            + ", ".join(missing)
        )

    if extra:
        raise ValueError(
            "Unsupported motor directions: "
            + ", ".join(extra)
        )

    clean: Dict[str, int] = {}

    for key in MOTOR_DIRECTION_KEYS:
        raw_value = directions[key]

        if (
            isinstance(raw_value, bool)
            or not isinstance(
                raw_value,
                (int, float),
            )
        ):
            raise ValueError(
                f"{key} must be 1 or -1."
            )

        value = int(
            raw_value
        )

        if value not in {
            -1,
            1,
        }:
            raise ValueError(
                f"{key} must be 1 or -1."
            )

        clean[key] = value

    return clean


def motor_directions_to_wire(
    directions: Mapping[str, Any],
) -> list:
    clean = validate_motor_directions(
        directions
    ) 

    return [
        clean[key]
        for key in MOTOR_DIRECTION_KEYS
    ]


def motor_directions_from_wire(
    raw_directions: Any,
) -> Dict[str, int]:
    if not isinstance(
        raw_directions,
        list,
    ):
        raise ValueError(
            "H7 motor direction "
            "acknowledgement must contain "
            "a directions array."
        )

    if (
        len(raw_directions)
        != len(MOTOR_DIRECTION_KEYS)
    ):
        raise ValueError(
            "H7 motor direction "
            "acknowledgement contains an "
            "invalid direction count."
        )

    directions = {
        key: raw_directions[index]
        for index, key in enumerate(
            MOTOR_DIRECTION_KEYS
        )
    }

    return validate_motor_directions(
        directions
    )


@dataclass(
    frozen=True,
)
class MotorDirectionApplyResult:
    directions: Dict[str, int]
    transaction_id: str
    restored_defaults: bool


class MotorDirectionManager:
    def __init__(
        self,
        *,
        db: Any,
        mcu_writes: asyncio.Queue,
        ack_queue: asyncio.Queue,
        process_active,
        logger: Any,
        timeout_s: float = 8.0,
    ):
        self.db = db
        self.mcu_writes = mcu_writes
        self.ack_queue = ack_queue
        self.process_active = process_active
        self.logger = logger
        self.timeout_s = timeout_s

        self._lock = asyncio.Lock()

    def get(self) -> Dict[str, Any]:
        directions = (
            self._read_persisted_directions()
        )

        return {
            "directions":
                directions,
            "factory_defaults":
                dict(
                    FACTORY_MOTOR_DIRECTIONS
                ),
            "is_factory_default": (
                directions
                == FACTORY_MOTOR_DIRECTIONS
            ),
        }

    async def apply_persisted(
        self,
    ) -> MotorDirectionApplyResult:
        directions = (
            self._read_persisted_directions()
        )

        return await self._apply(
            directions,
            actor="system",
            restored_defaults=False,
            persist=False,
            event_type=None,
        )

    async def save(
        self,
        directions: Mapping[str, Any],
        *,
        actor: str,
    ) -> MotorDirectionApplyResult:
        clean = validate_motor_directions(
            directions
        )

        return await self._apply(
            clean,
            actor=actor,
            restored_defaults=False,
            persist=True,
            event_type=(
                "motor_direction_saved"
            ),
        )

    async def restore_defaults(
        self,
        *,
        actor: str,
    ) -> MotorDirectionApplyResult:
        return await self._apply(
            dict(
                FACTORY_MOTOR_DIRECTIONS
            ),
            actor=actor,
            restored_defaults=True,
            persist=True,
            event_type=(
                "motor_direction_"
                "factory_defaults_restored"
            ),
        )

    def _read_persisted_directions(
        self,
    ) -> Dict[str, int]:
        catalog = (
            self.db
            .get_configuration_catalog()
        )

        raw_directions = catalog.get(
            "motor_direction"
        )

        if not raw_directions:
            return dict(
                FACTORY_MOTOR_DIRECTIONS
            )

        return validate_motor_directions(
            raw_directions
        )

    async def _apply(
        self,
        directions: Dict[str, int],
        *,
        actor: str,
        restored_defaults: bool,
        persist: bool,
        event_type: str | None,
    ) -> MotorDirectionApplyResult:
        async with self._lock:
            if self.process_active():
                raise RuntimeError(
                    "Motor directions cannot "
                    "change while the process "
                    "is active."
                )

            transaction_id = (
                uuid.uuid4().hex
            )

            self._drain_acks()

            await self.mcu_writes.put({
                "action":
                    "set_motor_direction",
                "transaction_id":
                    transaction_id,
                "directions":
                    motor_directions_to_wire(
                        directions
                    ),
            })

            acknowledgement = (
                await self._wait_for_ack(
                    transaction_id
                )
            )

            applied_directions = (
                motor_directions_from_wire(
                    acknowledgement.get(
                        "directions"
                    )
                )
            )

            if (
                applied_directions
                != directions
            ):
                raise RuntimeError(
                    "The H7 acknowledged "
                    "different motor directions "
                    "than were requested."
                )

            if persist:
                self.db.save_motor_directions(
                    directions=directions,
                    actor=actor,
                    event_type=event_type,
                    restored_defaults=(
                        restored_defaults
                    ),
                    transaction_id=(
                        transaction_id
                    ),
                )

                self.logger.log.info(
                    "Motor directions committed "
                    "to SQLite: "
                    f"transaction_id="
                    f"{transaction_id} "
                    f"directions={directions}"
                )
            return MotorDirectionApplyResult(
                directions=dict(
                    directions
                ),
                transaction_id=(
                    transaction_id
                ),
                restored_defaults=(
                    restored_defaults
                ),
            )

    def _drain_acks(
        self,
    ) -> None:
        while True:
            try:
                self.ack_queue.get_nowait()
            except asyncio.QueueEmpty:
                return

    async def _wait_for_ack(
        self,
        transaction_id: str,
    ) -> Dict[str, Any]:
        loop = (
            asyncio.get_running_loop()
        )

        deadline = (
            loop.time()
            + self.timeout_s
        )

        while True:
            remaining = (
                deadline
                - loop.time()
            )

            if remaining <= 0:
                raise asyncio.TimeoutError(
                    "Timed out waiting for "
                    "the H7 motor direction "
                    "acknowledgement."
                )

            message = (
                await asyncio.wait_for(
                    self.ack_queue.get(),
                    timeout=remaining,
                )
            )

            if not isinstance(
                message,
                dict,
            ):
                continue

            if (
                message.get(
                    "transaction_id"
                )
                != transaction_id
            ):
                self.logger.log.warning(
                    "Ignoring stale or unrelated "
                    "motor direction "
                    "acknowledgement: "
                    f"expected="
                    f"{transaction_id} "
                    f"received="
                    f"{message.get('transaction_id')}"
                )

                continue

            if (
                message.get("type")
                != "motor_direction_ack"
            ):
                continue

            if message.get("ok") is not True:
                raise RuntimeError(
                    str(
                        message.get(
                            "error"
                        )
                        or (
                            "The H7 rejected "
                            "the motor directions."
                        )
                    )
                )

            return message