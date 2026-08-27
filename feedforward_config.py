from __future__ import annotations

import asyncio
import math
import uuid
from dataclasses import dataclass
from typing import Any, Dict, Mapping

FACTORY_FEEDFORWARD: Dict[str, float] = {
    "base_speed_ms": 0.095,
    "kp_speed_up_track": 0.00025,
    "kp_slow_down_track": 0.0020,
    "min_track_speed_ms": 0.035,
    "max_track_speed_ms": 0.120,
    "tracking_deadband_mm": 3.0,
    "track_accel_mps2": 0.25,
    "track_decel_mps2": 1.00,
}

FEEDFORWARD_WIRE_KEYS = (
    "base_speed_ms",
    "kp_speed_up_track",
    "kp_slow_down_track",
    "min_track_speed_ms",
    "max_track_speed_ms",
    "tracking_deadband_mm",
    "track_accel_mps2",
    "track_decel_mps2",
)

# Conservative software limits. These are validation boundaries, not tuning
# recommendations. Bench validation remains required for every changed value.
LIMITS = {
    "base_speed_ms": (0.0, 0.25),
    "kp_speed_up_track": (0.0, 0.02),
    "kp_slow_down_track": (0.0, 0.02),
    "min_track_speed_ms": (0.0, 0.25),
    "max_track_speed_ms": (0.0, 0.25),
    "tracking_deadband_mm": (0.0, 50.0),
    "track_accel_mps2": (0.01, 5.0),
    "track_decel_mps2": (0.01, 10.0),
}


def validate_feedforward(values: Mapping[str, Any]) -> Dict[str, float]:
    if not isinstance(values, Mapping):
        raise ValueError("feedforward values must be an object")
    missing = [key for key in FACTORY_FEEDFORWARD if key not in values]
    extra = [key for key in values if key not in FACTORY_FEEDFORWARD]
    if missing:
        raise ValueError("missing feedforward values: " + ", ".join(missing))
    if extra:
        raise ValueError("unsupported feedforward values: " + ", ".join(extra))

    clean: Dict[str, float] = {}
    for key in FACTORY_FEEDFORWARD:
        raw = values[key]
        if isinstance(raw, bool) or not isinstance(raw, (int, float)):
            raise ValueError(f"{key} must be numeric")
        value = float(raw)
        if not math.isfinite(value):
            raise ValueError(f"{key} must be finite")
        low, high = LIMITS[key]
        if not low <= value <= high:
            raise ValueError(f"{key} must be between {low} and {high}")
        clean[key] = value

    if clean["min_track_speed_ms"] > clean["base_speed_ms"]:
        raise ValueError("min_track_speed_ms cannot exceed base_speed_ms")
    if clean["base_speed_ms"] > clean["max_track_speed_ms"]:
        raise ValueError("base_speed_ms cannot exceed max_track_speed_ms")
    return clean

def feedforward_to_wire(
    values: Mapping[str, float],
) -> list:
    clean = validate_feedforward(
        values
    )

    return [
        clean[key]
        for key in FEEDFORWARD_WIRE_KEYS
    ]

def feedforward_from_wire(
    raw_values: Any,
) -> Dict[str, float]:
    if not isinstance(
        raw_values,
        list,
    ):
        raise ValueError(
            "H7 feedforward acknowledgement "
            "must contain an ff array."
        )

    if (
        len(raw_values)
        != len(FEEDFORWARD_WIRE_KEYS)
    ):
        raise ValueError(
            "H7 feedforward acknowledgement "
            "contains an invalid ff value count."
        )

    values = {
        key: raw_values[index]
        for index, key in enumerate(
            FEEDFORWARD_WIRE_KEYS
        )
    }

    return validate_feedforward(
        values
    )

def feedforward_values_match(
    expected: Mapping[str, float],
    actual: Mapping[str, float],
) -> bool:
    if set(expected.keys()) != set(actual.keys()):
        return False

    for key in FACTORY_FEEDFORWARD:
        expected_value = float(
            expected[key]
        )

        actual_value = float(
            actual[key]
        )

        if not math.isclose(
            expected_value,
            actual_value,
            rel_tol=1.0e-6,
            abs_tol=1.0e-8,
        ):
            return False

    return True

@dataclass(frozen=True)
class FeedforwardApplyResult:
    values: Dict[str, float]
    transaction_id: str
    restored_defaults: bool


class FeedforwardManager:
    """Serializes, persists, applies, and audits feedforward configuration.

    Persistence is committed only after the H7 confirms the exact transaction.
    A timeout or rejection leaves the previous database values authoritative.
    """

    def __init__(self, *, db: Any, mcu_writes: asyncio.Queue, ack_queue: asyncio.Queue,
                 process_active, logger: Any, timeout_s: float = 5.0):
        self.db = db
        self.mcu_writes = mcu_writes
        self.ack_queue = ack_queue
        self.process_active = process_active
        self.logger = logger
        self.timeout_s = timeout_s
        self._lock = asyncio.Lock()

    def get(self) -> Dict[str, Any]:
        values = validate_feedforward(self.db.get_feedforward_configuration())
        return {
            "values": values,
            "factory_defaults": dict(FACTORY_FEEDFORWARD),
            "is_factory_default": values == FACTORY_FEEDFORWARD,
            "limits": {k: {"min": v[0], "max": v[1]} for k, v in LIMITS.items()},
        }

    async def apply_persisted(self) -> FeedforwardApplyResult:
        values = validate_feedforward(self.db.get_feedforward_configuration())
        return await self._apply(values, actor="system", restored_defaults=False,
                                 persist=False, event_type=None)

    async def save(self, values: Mapping[str, Any], *, actor: str) -> FeedforwardApplyResult:
        return await self._apply(validate_feedforward(values), actor=actor,
                                 restored_defaults=False, persist=True,
                                 event_type="feedforward_configuration_saved")

    async def restore_defaults(self, *, actor: str) -> FeedforwardApplyResult:
        return await self._apply(dict(FACTORY_FEEDFORWARD), actor=actor,
                                 restored_defaults=True, persist=True,
                                 event_type="feedforward_factory_defaults_restored")

    async def _apply(self, values: Dict[str, float], *, actor: str,
                     restored_defaults: bool, persist: bool,
                     event_type: str | None) -> FeedforwardApplyResult:
        async with self._lock:
            if self.process_active():
                raise RuntimeError("Feedforward configuration cannot change while the process is active.")

            transaction_id = uuid.uuid4().hex
            self._drain_acks()
            await self.mcu_writes.put({
                "action":
                    "set_feedforward_configuration",
                "transaction_id":
                    transaction_id,
                "ff":
                    feedforward_to_wire(
                        values
                    ),
            })
            ack = await self._wait_for_ack(
                transaction_id
            )

            applied = feedforward_from_wire(
                ack.get(
                    "ff"
                )
            )

            if not feedforward_values_match(
                values,
                applied,
            ):
                mismatch_details = {
                    key: {
                        "requested": values[key],
                        "acknowledged": applied[key],
                        "difference": (
                            applied[key]
                            - values[key]
                        ),
                    }
                    for key in FACTORY_FEEDFORWARD
                    if not math.isclose(
                        values[key],
                        applied[key],
                        rel_tol=1.0e-6,
                        abs_tol=1.0e-8,
                    )
                }

                raise RuntimeError(
                    "H7 acknowledgement values do not "
                    "match the requested configuration: "
                    f"{mismatch_details}"
                )

            if persist:
                self.db.save_feedforward_configuration(
                    values=values,
                    actor=actor,
                    event_type=event_type,
                    restored_defaults=restored_defaults,
                )

            return FeedforwardApplyResult(
                values=dict(values),
                transaction_id=transaction_id,
                restored_defaults=restored_defaults,
            )

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
                raise asyncio.TimeoutError("Timed out waiting for H7 feedforward acknowledgement.")
            msg = await asyncio.wait_for(self.ack_queue.get(), remaining)
            if not isinstance(msg, dict):
                continue
            if msg.get("transaction_id") != transaction_id:
                self.logger.log.warning("Ignoring stale or unrelated feedforward acknowledgement")
                continue
            if msg.get("type") != "feedforward_configuration_ack" or msg.get("ok") is not True:
                raise RuntimeError(str(msg.get("error") or "H7 rejected feedforward configuration"))
            return msg
