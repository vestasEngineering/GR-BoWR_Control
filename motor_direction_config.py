from __future__ import annotations

import asyncio
from dataclasses import dataclass
from typing import Any, Callable, Optional, Sequence
from uuid import uuid4

FACTORY_MOTOR_DIRECTIONS = [1, -1, -1, 1]
FACTORY_ENCODER_DIRECTIONS = [1, -1, 1, -1]


def validate_directions(values: Sequence[int], field: str) -> list[int]:
    if not isinstance(values, (list, tuple)) or len(values) != 4:
        raise ValueError(f"{field} must contain exactly four values.")
    normalized: list[int] = []
    for index, raw in enumerate(values):
        if isinstance(raw, bool):
            raise ValueError(f"{field}[{index}] must be -1 or 1.")
        try:
            value = int(raw)
        except (TypeError, ValueError) as error:
            raise ValueError(f"{field}[{index}] must be -1 or 1.") from error
        if value not in (-1, 1):
            raise ValueError(f"{field}[{index}] must be -1 or 1.")
        normalized.append(value)
    return normalized


def calculate_axis_directions(
    *,
    tested_motor_direction: int,
    raw_encoder_delta: int,
    operator_observed_forward: bool,
) -> tuple[int, int]:
    """Return motor-output and encoder-normalization polarity for one axis.

    The test is a positive robot-frame command made with
    tested_motor_direction. If the observed physical movement was backward,
    the output polarity must be inverted. The encoder polarity is then chosen
    so the raw delta for physical forward becomes positive.
    """
    if tested_motor_direction not in (-1, 1):
        raise ValueError("tested_motor_direction must be -1 or 1.")
    if isinstance(raw_encoder_delta, bool) or int(raw_encoder_delta) == 0:
        raise ValueError("raw_encoder_delta must be non-zero.")

    raw_delta = int(raw_encoder_delta)
    motor_direction = (
        tested_motor_direction
        if operator_observed_forward
        else -tested_motor_direction
    )
    forward_raw_delta = raw_delta if operator_observed_forward else -raw_delta
    encoder_direction = 1 if forward_raw_delta > 0 else -1
    return motor_direction, encoder_direction


@dataclass(frozen=True)
class DriveDirectionResult:
    transaction_id: str
    motor_directions: list[int]
    encoder_directions: list[int]
    encoder_session_id: int
    encoder_restore_required: bool
    restored_defaults: bool = False


class MotorDirectionManager:
    """Serialize and correlate combined drive-direction transactions."""

    def __init__(
        self,
        db: Any,
        mcu_writes: asyncio.Queue,
        ack_queue: asyncio.Queue,
        process_active: Callable[[], bool],
        logger: Any,
        timeout_s: float = 8.0,
    ) -> None:
        self.db = db
        self.mcu_writes = mcu_writes
        self.ack_queue = ack_queue
        self.process_active = process_active
        self.logger = logger
        self.timeout_s = timeout_s
        self._lock = asyncio.Lock()

    def get(self) -> dict[str, Any]:
        stored = self.db.get_drive_direction_configuration()
        if stored is None:
            motor = list(FACTORY_MOTOR_DIRECTIONS)
            encoder = list(FACTORY_ENCODER_DIRECTIONS)
        else:
            motor = validate_directions(
                stored.get("motor_directions"), "motor_directions"
            )
            encoder = validate_directions(
                stored.get("encoder_directions"), "encoder_directions"
            )
        return {
            "motor_directions": motor,
            "encoder_directions": encoder,
            "is_factory_default": (
                motor == FACTORY_MOTOR_DIRECTIONS
                and encoder == FACTORY_ENCODER_DIRECTIONS
            ),
        }

    async def apply_persisted(self) -> DriveDirectionResult:
        current = self.get()
        return await self._apply(
            current["motor_directions"],
            current["encoder_directions"],
            actor=None,
            persist=False,
            restored_defaults=current["is_factory_default"],
        )

    async def save(
        self,
        motor_directions: Sequence[int],
        encoder_directions: Sequence[int],
        actor: str,
    ) -> DriveDirectionResult:
        return await self._apply(
            validate_directions(motor_directions, "motor_directions"),
            validate_directions(encoder_directions, "encoder_directions"),
            actor=actor,
            persist=True,
            restored_defaults=False,
        )

    async def restore_defaults(self, actor: str) -> DriveDirectionResult:
        return await self._apply(
            list(FACTORY_MOTOR_DIRECTIONS),
            list(FACTORY_ENCODER_DIRECTIONS),
            actor=actor,
            persist=True,
            restored_defaults=True,
        )

    async def _apply(
        self,
        motor: Sequence[int],
        encoder: Sequence[int],
        *,
        actor: Optional[str],
        persist: bool,
        restored_defaults: bool,
    ) -> DriveDirectionResult:
        async with self._lock:
            if self.process_active():
                raise RuntimeError(
                    "The process must be confirmed stopped before changing "
                    "drive directions."
                )

            requested_motor = validate_directions(motor, "motor_directions")
            requested_encoder = validate_directions(
                encoder, "encoder_directions"
            )
            transaction_id = f"drive-direction-{uuid4()}"

            await self.mcu_writes.put({
                "action": "set_drive_direction_configuration",
                "transaction_id": transaction_id,
                "motor_directions": requested_motor,
                "encoder_directions": requested_encoder,
            })

            acknowledgement = await self._wait_for_ack(transaction_id)
            applied_motor = validate_directions(
                acknowledgement.get("motor_directions"), "motor_directions"
            )
            applied_encoder = validate_directions(
                acknowledgement.get("encoder_directions"), "encoder_directions"
            )
            if applied_motor != requested_motor or applied_encoder != requested_encoder:
                raise RuntimeError(
                    "The H7 acknowledged different drive direction values."
                )

            session_id = acknowledgement.get("encoder_session_id")
            if isinstance(session_id, bool) or not isinstance(session_id, int):
                raise RuntimeError(
                    "The H7 acknowledgement did not include a valid new "
                    "encoder_session_id."
                )
            if acknowledgement.get("encoder_restore_required") is not True:
                raise RuntimeError(
                    "The H7 did not keep motion inhibited for encoder restoration."
                )

            if persist:
                self.db.save_drive_direction_configuration(
                    requested_motor,
                    requested_encoder,
                    actor,
                    transaction_id,
                )

            return DriveDirectionResult(
                transaction_id=transaction_id,
                motor_directions=applied_motor,
                encoder_directions=applied_encoder,
                encoder_session_id=session_id,
                encoder_restore_required=True,
                restored_defaults=restored_defaults,
            )

    async def _wait_for_ack(self, transaction_id: str) -> dict[str, Any]:
        deadline = asyncio.get_running_loop().time() + self.timeout_s
        while True:
            remaining = deadline - asyncio.get_running_loop().time()
            if remaining <= 0:
                raise asyncio.TimeoutError(
                    "H7 drive-direction acknowledgement timed out."
                )
            message = await asyncio.wait_for(
                self.ack_queue.get(), timeout=remaining
            )
            if not isinstance(message, dict):
                continue
            if message.get("type") != "drive_direction_configuration_ack":
                self.logger.log.warning(
                    "Discarding unexpected drive direction acknowledgement: %r",
                    message,
                )
                continue
            if message.get("transaction_id") != transaction_id:
                self.logger.log.warning(
                    "Discarding stale drive direction acknowledgement: %r",
                    message,
                )
                continue
            if message.get("ok") is not True:
                raise RuntimeError(
                    str(message.get("error") or "H7 rejected drive directions.")
                )
            return message
