from __future__ import annotations

import json
import math
import struct
import time
import zlib
from dataclasses import dataclass
from typing import Optional

MAGIC = 0xA55A
VERSION = 1
MESSAGE_TYPE_STEERING = 1
# Header through payload, followed by CRC32. Little-endian.
_BINARY_NO_CRC = struct.Struct("<HBBI Q f f f f H")
_BINARY = struct.Struct("<HBBI Q f f f f H I")
FLAG_VALID = 1 << 0
FLAG_TOP_VALID = 1 << 1
FLAG_BOTTOM_VALID = 1 << 2
FLAG_PAIR_FRESH = 1 << 3


@dataclass(frozen=True)
class SensorCalibration:
    camera_id: str
    millimeters_per_pixel: float
    zero_pixel: float

    def position_mm(self, pixel: float) -> float:
        if self.millimeters_per_pixel <= 0:
            raise ValueError("millimeters_per_pixel must be positive")
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
    sequence: int
    monotonic_ns: int
    valid: bool
    angle_degrees: Optional[float]
    top_position_mm: Optional[float]
    bottom_position_mm: Optional[float]
    pair_age_us: int
    reason: str

    def to_json_bytes(self) -> bytes:
        payload = {
            "type": "steering_angle", "version": VERSION, "sequence": self.sequence,
            "monotonic_us": self.monotonic_ns // 1000, "valid": self.valid,
            "angle_deg": self.angle_degrees, "top_mm": self.top_position_mm,
            "bottom_mm": self.bottom_position_mm, "pair_age_us": self.pair_age_us,
            "reason": self.reason,
        }
        return (json.dumps(payload, separators=(",", ":"), allow_nan=False) + "\n").encode("ascii")

    def to_binary(self) -> bytes:
        flags = FLAG_PAIR_FRESH
        if self.valid: flags |= FLAG_VALID | FLAG_TOP_VALID | FLAG_BOTTOM_VALID
        nan = float("nan")
        values = (MAGIC, VERSION, MESSAGE_TYPE_STEERING, self.sequence & 0xffffffff,
                  self.monotonic_ns // 1000,
                  self.angle_degrees if self.angle_degrees is not None else nan,
                  self.top_position_mm if self.top_position_mm is not None else nan,
                  self.bottom_position_mm if self.bottom_position_mm is not None else nan,
                  float(self.pair_age_us), flags)
        body = _BINARY_NO_CRC.pack(*values)
        return _BINARY.pack(*values, zlib.crc32(body) & 0xffffffff)


class SteeringAnglePairer:
    def __init__(
        self,
        top,
        bottom,
        sensor_separation_mm=230.0,
        maximum_pair_age_us=2000,
        maximum_measurement_age_ms=1000,
    ):

        if sensor_separation_mm <= 0:
            raise ValueError("sensor_separation_mm must be positive")

        self.top_cal = top
        self.bottom_cal = bottom
        self.separation = sensor_separation_mm

        self.maximum_measurement_age_ns = (
            int(maximum_measurement_age_ms) * 1_000_000
        )

        self.last_valid_top = None
        self.last_valid_bottom = None
        self.output_sequence = 0

    def update(self, measurement):

        if measurement.camera_id == self.top_cal.camera_id:
            if measurement.valid and measurement.position_pixels is not None:
                self.last_valid_top = measurement

        elif measurement.camera_id == self.bottom_cal.camera_id:
            if measurement.valid and measurement.position_pixels is not None:
                self.last_valid_bottom = measurement
        else:
            raise ValueError(
                f"unknown camera_id {measurement.camera_id}"
            )

        now_ns = measurement.monotonic_ns

        if self.last_valid_top is None:
            return self._invalid(now_ns, 0, "top_missing")

        if self.last_valid_bottom is None:
            return self._invalid(now_ns, 0, "bottom_missing")

        top_age = now_ns - self.last_valid_top.monotonic_ns
        bottom_age = now_ns - self.last_valid_bottom.monotonic_ns

        if top_age > self.maximum_measurement_age_ns:
            return self._invalid(now_ns, 0, "top_timeout")

        if bottom_age > self.maximum_measurement_age_ns:
            return self._invalid(now_ns, 0, "bottom_timeout")

        top_mm = self.top_cal.position_mm(
            self.last_valid_top.position_pixels
        )

        bottom_mm = self.bottom_cal.position_mm(
            self.last_valid_bottom.position_pixels
        )

        angle = math.degrees(
            math.atan2(
                bottom_mm - top_mm,
                self.separation,
            )
        )

        self.output_sequence += 1

        return SteeringResult(
            self.output_sequence,
            now_ns,
            True,
            angle,
            top_mm,
            bottom_mm,
            max(top_age, bottom_age) // 1000,
            "none",
        )

    def _invalid(self, stamp, age_ns, reason):
        return SteeringResult(
            self.output_sequence,
            stamp,
            False,
            None,
            None,
            None,
            age_ns // 1000,
            reason,
        )


def measurement_from_detection(
    camera_id: str,
    sequence: int,
    monotonic_ns: int,
    detection,
) -> SensorMeasurement:

    region = detection.selected_region

    return SensorMeasurement(
        camera_id,
        sequence,
        monotonic_ns,
        None if region is None else region.selected_center,
        bool(
            region is not None
            and region.measurement_quality
            and region.selected_center is not None
        ),
        "invalid" if region is None else region.center_confidence,
    )
