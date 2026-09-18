from __future__ import annotations

import json
from dataclasses import dataclass, fields
from pathlib import Path


@dataclass(frozen=True)
class CameraConfig:
    camera_id: str
    serial_number: str
    pipe_size: int = 4 * 1024 * 1024
    initial_packet_length: int = 4096
    expected_pixels: int = 2048
    byte_order: str = ">u2"
    wait_timeout_ms: int = 3000
    reverse_pixels: bool = False
    steering_role: str | None = None
    millimeters_per_pixel: float | None = None
    zero_pixel: float | None = None

    @classmethod
    def from_mapping(cls, values: dict) -> "CameraConfig":
        allowed = {field.name for field in fields(cls)}
        unknown = set(values) - allowed
        if unknown:
            raise ValueError(f"Unknown camera keys: {sorted(unknown)}")
        item = cls(**values)
        if not item.camera_id.strip():
            raise ValueError("camera_id must not be empty")
        if not item.serial_number.strip():
            raise ValueError(f"serial_number is required for {item.camera_id}")
        if item.expected_pixels < 1:
            raise ValueError(f"{item.camera_id}: expected_pixels must be positive")
        if item.initial_packet_length != item.expected_pixels * 2:
            raise ValueError(f"{item.camera_id}: initial_packet_length must equal expected_pixels * 2 for one-line transfers")
        if item.pipe_size < item.initial_packet_length:
            raise ValueError(f"{item.camera_id}: pipe_size is too small")
        if item.wait_timeout_ms < 1:
            raise ValueError(f"{item.camera_id}: wait_timeout_ms must be positive")
        if item.byte_order not in (">u2", "<u2"):
            raise ValueError(f"{item.camera_id}: unsupported byte_order")
        if item.steering_role not in (None, "top", "bottom"):
            raise ValueError(f"{item.camera_id}: steering_role must be top or bottom")
        if any(value is not None for value in (item.steering_role, item.millimeters_per_pixel, item.zero_pixel)):
            if item.steering_role is None:
                raise ValueError(f"{item.camera_id}: steering_role is required when steering calibration is configured")
            if item.millimeters_per_pixel is None:
                raise ValueError(f"{item.camera_id}: millimeters_per_pixel is required")
            if item.zero_pixel is None:
                raise ValueError(f"{item.camera_id}: zero_pixel is required")
            if item.millimeters_per_pixel <= 0:
                raise ValueError(f"{item.camera_id}: millimeters_per_pixel must be positive")
            if not 0.0 <= item.zero_pixel <= item.expected_pixels - 1:
                raise ValueError(f"{item.camera_id}: zero_pixel must be within the sensor")
        return item


@dataclass(frozen=True)
class SteeringConfig:
    sensor_separation_mm: float = 230.0
    maximum_pair_age_us: int = 2000

    @classmethod
    def from_mapping(cls, values: dict | None) -> "SteeringConfig":
        values = values or {}
        allowed = {field.name for field in fields(cls)}
        unknown = set(values) - allowed
        if unknown:
            raise ValueError(f"Unknown steering keys: {sorted(unknown)}")
        item = cls(**values)
        if item.sensor_separation_mm <= 0:
            raise ValueError("sensor_separation_mm must be positive")
        if item.maximum_pair_age_us < 0:
            raise ValueError("maximum_pair_age_us cannot be negative")
        return item


@dataclass(frozen=True)
class Config:
    sdk_directory: str = "../../sdk"
    library_path: str = "../../sdk/libusblismpi2664.so"
    cameras: list[dict] | None = None
    steering: dict | SteeringConfig | None = None
    device_index: int = 0
    pipe_size: int = 4 * 1024 * 1024
    initial_packet_length: int = 4096
    expected_pixels: int = 2048
    byte_order: str = ">u2"
    wait_timeout_ms: int = 3000
    settle_frames: int = 16
    dark_frames: int = 256
    laser_frames: int = 128
    noise_sigma: float = 6.0
    minimum_signal: float = 100.0
    centroid_half_width: int = 8
    minimum_region_width: int = 2
    maximum_region_width: int = 256
    clip_margin: int = 16
    calibration_noise_sigma: float = 3.0
    calibration_minimum_signal: float = 20.0
    calibration_relative_threshold: float = 0.25
    publish_hz: float = 20.0
    host: str = "127.0.0.1"
    port: int = 8769
    calibration_directory: str = "../calibration"
    log_directory: str = "../logs"
    optimizer_directory: str = "../optimization"
    optimizer_settle_frames: int = 32
    optimizer_capture_frames: int = 64
    optimizer_finalists: int = 5
    optimizer_bias_values: list[int] | None = None
    optimizer_gain_values: list[int] | None = None
    optimizer_st_high_values: list[int] | None = None
    optimizer_st_low: int = 100
    optimizer_edge_delay: int = 88
    optimizer_rail_margin: int = 32
    optimizer_max_rail_fraction: float = 0.01
    optimizer_max_region_clip_fraction: float = 0.005
    optimizer_random_std_tolerance: float = 0.12
    optimizer_min_frame_correlation: float = 0.05
    optimizer_min_laser_snr: float = 6.0
    optimizer_refine_bias_step: int = 32
    optimizer_refine_bias_radius: int = 128
    optimizer_refine_st_high_values: list[int] | None = None
    configuration_settle_ms: int = 150
    configuration_readback_retries: int = 5
    configuration_readback_retry_ms: int = 100

    def __post_init__(self):
        object.__setattr__(self, "optimizer_bias_values", self.optimizer_bias_values or [256, 512, 768])
        object.__setattr__(self, "optimizer_gain_values", self.optimizer_gain_values or [0, 4])
        object.__setattr__(self, "optimizer_st_high_values", self.optimizer_st_high_values or [500, 1000, 5000, 9900])
        object.__setattr__(self, "optimizer_refine_st_high_values", self.optimizer_refine_st_high_values or [7500, 8500, 9250, 9900])
        parsed = tuple(value if isinstance(value, CameraConfig) else CameraConfig.from_mapping(value) for value in (self.cameras or []))
        if parsed:
            if len(parsed) != 2:
                raise ValueError("Exactly two camera definitions are required when cameras is configured")
            ids = [item.camera_id for item in parsed]
            serials = [item.serial_number for item in parsed]
            if len(ids) != len(set(ids)):
                raise ValueError(f"Duplicate camera_id values: {ids}")
            if len(serials) != len(set(serials)):
                raise ValueError(f"Duplicate camera serial numbers: {serials}")
            roles = [item.steering_role for item in parsed if item.steering_role is not None]
            if roles and sorted(roles) != ["bottom", "top"]:
                raise ValueError("Steering calibration requires exactly one top and one bottom camera")
        object.__setattr__(self, "cameras", parsed)
        object.__setattr__(self, "steering", self.steering if isinstance(self.steering, SteeringConfig) else SteeringConfig.from_mapping(self.steering))
        if self.configuration_readback_retries < 1:
            raise ValueError("configuration_readback_retries must be at least 1")
        if self.optimizer_refine_bias_step < 1:
            raise ValueError("optimizer_refine_bias_step must be positive")

    def steering_cameras(self) -> tuple[CameraConfig, CameraConfig]:
        by_role = {camera.steering_role: camera for camera in self.cameras if camera.steering_role is not None}
        if set(by_role) != {"top", "bottom"}:
            raise ValueError("Steering requires serial-bound calibration for one top and one bottom camera")
        return by_role["top"], by_role["bottom"]

    @classmethod
    def load(cls, path: str | Path) -> tuple["Config", Path]:
        config_path = Path(path).expanduser().resolve()
        data = json.loads(config_path.read_text(encoding="utf-8"))
        allowed = {field.name for field in fields(cls)}
        unknown = set(data) - allowed
        if unknown:
            raise ValueError(f"Unknown configuration keys: {sorted(unknown)}")
        return cls(**data), config_path.parent

    def resolve(self, base: Path, value: str) -> Path:
        path = Path(value).expanduser()
        return path.resolve() if path.is_absolute() else (base / path).resolve()
