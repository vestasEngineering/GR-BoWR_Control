from __future__ import annotations

import math
import threading
from dataclasses import dataclass
from typing import Optional


@dataclass(frozen=True)
class SensorCalibration:
    camera_id: str
    millimeters_per_pixel: float
    zero_pixel: float

    def position_mm(self, pixel: float) -> float:
        return (pixel - self.zero_pixel) * self.millimeters_per_pixel


@dataclass(frozen=True)
class SensorMeasurement:
    camera_id: str
    sequence: int
    monotonic_ns: int
    position_pixels: Optional[float]
    valid: bool
    confidence: str


@dataclass(frozen=True)
class SteeringResult:
    valid: bool
    angle_degrees: Optional[float]
    top_position_mm: Optional[float]
    bottom_position_mm: Optional[float]
    top_age_ms: Optional[float]
    bottom_age_ms: Optional[float]
    reason: str


def measurement_from_detection(camera_id, sequence, monotonic_ns, detection) -> SensorMeasurement:
    region = detection.selected_region
    return SensorMeasurement(
        camera_id,
        sequence,
        monotonic_ns,
        None if region is None else region.selected_center,
        bool(region is not None and region.measurement_quality and region.selected_center is not None),
        "invalid" if region is None else region.center_confidence,
    )


class SteeringEstimator:
    """Angle from the latest valid top and bottom measurements.

    Invalid frames never erase a stored valid measurement; the angle becomes
    invalid only when a sensor has no valid measurement newer than the timeout.
    Thread-safe so each camera thread can ingest directly.
    """

    def __init__(self, top: SensorCalibration, bottom: SensorCalibration,
                 sensor_separation_mm: float, measurement_timeout_ms: float):
        if sensor_separation_mm <= 0:
            raise ValueError("sensor_separation_mm must be positive")
        self.top = top
        self.bottom = bottom
        self.separation = sensor_separation_mm
        self.timeout_ns = int(measurement_timeout_ms * 1_000_000)
        self._last = {top.camera_id: None, bottom.camera_id: None}
        self._lock = threading.Lock()

    def ingest(self, measurement: SensorMeasurement) -> None:
        if measurement.camera_id not in self._last:
            raise ValueError(f"unknown camera_id {measurement.camera_id}")
        if measurement.valid and measurement.position_pixels is not None:
            with self._lock:
                self._last[measurement.camera_id] = measurement

    def evaluate(self, now_ns: int) -> SteeringResult:
        with self._lock:
            top = self._last[self.top.camera_id]
            bottom = self._last[self.bottom.camera_id]
        top_age = None if top is None else (now_ns - top.monotonic_ns) / 1e6
        bottom_age = None if bottom is None else (now_ns - bottom.monotonic_ns) / 1e6
        if top is None:
            return self._invalid("top_missing", top_age, bottom_age)
        if bottom is None:
            return self._invalid("bottom_missing", top_age, bottom_age)
        if now_ns - top.monotonic_ns > self.timeout_ns:
            return self._invalid("top_timeout", top_age, bottom_age)
        if now_ns - bottom.monotonic_ns > self.timeout_ns:
            return self._invalid("bottom_timeout", top_age, bottom_age)
        top_mm = self.top.position_mm(top.position_pixels)
        bottom_mm = self.bottom.position_mm(bottom.position_pixels)
        angle = math.degrees(math.atan2(bottom_mm - top_mm, self.separation))
        return SteeringResult(True, angle, top_mm, bottom_mm, top_age, bottom_age, "none")

    @staticmethod
    def _invalid(reason, top_age, bottom_age) -> SteeringResult:
        return SteeringResult(False, None, None, None, top_age, bottom_age, reason)


class SendPolicy:
    """Decides when a result is worth transmitting.

    Sends on: first result, any validity or invalid-reason change (immediately),
    an angle change of at least deadband_deg since the last *sent* angle (rate
    limited by min_interval_ms), and a keepalive when nothing else was sent.
    """

    def __init__(self, deadband_deg: float, min_interval_ms: float, keepalive_ms: float):
        self.deadband_deg = deadband_deg
        self.min_interval_ns = int(min_interval_ms * 1_000_000)
        self.keepalive_ns = int(keepalive_ms * 1_000_000)
        self._sent: Optional[SteeringResult] = None
        self._sent_ns = 0

    def should_send(self, result: SteeringResult, now_ns: int) -> bool:
        if self._sent is None:
            return True
        elapsed = now_ns - self._sent_ns
        if elapsed >= self.keepalive_ns:
            return True
        if result.valid != self._sent.valid or result.reason != self._sent.reason:
            return True
        if result.valid and elapsed >= self.min_interval_ns:
            return abs(result.angle_degrees - self._sent.angle_degrees) >= self.deadband_deg
        return False

    def mark_sent(self, result: SteeringResult, now_ns: int) -> None:
        self._sent = result
        self._sent_ns = now_ns
