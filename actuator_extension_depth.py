from __future__ import annotations

import asyncio
import math
from dataclasses import dataclass
from typing import Any, Dict, Optional
from uuid import uuid4


FACTORY_ACTUATOR_EXTENSION_VOLTAGE = 4.4
MIN_ACTUATOR_EXTENSION_VOLTAGE = 0.0
MAX_ACTUATOR_EXTENSION_VOLTAGE = 5.0


def validate_extension_voltage(value: Any) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError("Actuator extension voltage must be numeric.")
    voltage = float(value)
    if not math.isfinite(voltage):
        raise ValueError("Actuator extension voltage must be finite.")
    if not MIN_ACTUATOR_EXTENSION_VOLTAGE <= voltage <= MAX_ACTUATOR_EXTENSION_VOLTAGE:
        raise ValueError("Actuator extension voltage must be between 0.0 and 5.0 V.")
    return voltage


@dataclass(frozen=True)
class ActuatorExtensionApplyResult:
    transaction_id: str
    voltage: float


class ActuatorExtensionManager:
    """Serializes persistent extension-voltage application to the H7."""

    def __init__(self, *, db, mcu_writes: asyncio.Queue, ack_queue: asyncio.Queue,
                 process_active, logger, timeout_s: float = 5.0):
        self.db = db
        self.mcu_writes = mcu_writes
        self.ack_queue = ack_queue
        self.process_active = process_active
        self.logger = logger
        self.timeout_s = timeout_s
        self._lock = asyncio.Lock()

    async def apply(self, *, robot_type_id: str, blade_type_id: str,
                    voltage: float) -> ActuatorExtensionApplyResult:
        voltage = validate_extension_voltage(voltage)
        if self.process_active():
            raise RuntimeError("Stop the process before changing actuator extension depth.")

        async with self._lock:
            transaction_id = f"actuator-extension-{uuid4()}"
            self._drain_acks()
            await self.mcu_writes.put({
                "action": "set_transition_actuator_voltage",
                "transaction_id": transaction_id,
                "voltage": voltage,
            })
            ack = await self._wait_for_ack(transaction_id)
            if ack.get("ok") is not True:
                raise RuntimeError(str(ack.get("error") or "actuator_extension_rejected"))
            applied = validate_extension_voltage(ack.get("voltage"))
            if abs(applied - voltage) > 0.001:
                raise RuntimeError("H7 confirmed a different actuator extension voltage.")
            return ActuatorExtensionApplyResult(transaction_id, applied)

    def _drain_acks(self) -> None:
        while True:
            try:
                self.ack_queue.get_nowait()
            except asyncio.QueueEmpty:
                return

    async def _wait_for_ack(self, transaction_id: str) -> Dict[str, Any]:
        loop = asyncio.get_running_loop()
        deadline = loop.time() + self.timeout_s
        while True:
            remaining = deadline - loop.time()
            if remaining <= 0:
                raise asyncio.TimeoutError(
                    f"Timed out waiting for actuator extension acknowledgement {transaction_id}."
                )
            message = await asyncio.wait_for(self.ack_queue.get(), remaining)
            if not isinstance(message, dict):
                continue
            if message.get("type") != "transition_actuator_voltage_ack":
                continue
            if str(message.get("transaction_id", "")) != transaction_id:
                self.logger.log.warning(
                    "Discarding stale actuator extension acknowledgement: expected=%s received=%s",
                    transaction_id, message.get("transaction_id"),
                )
                continue
            return message
