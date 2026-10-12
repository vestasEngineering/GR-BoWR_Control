from __future__ import annotations

import json
from dataclasses import dataclass, fields
from pathlib import Path

from .detector import QualityConfig, TriggerConfig


def _reject_unknown(section: str, values: dict, allowed) -> None:
    unknown = set(values) - set(allowed)
    if unknown:
        raise ValueError(f"Unknown keys in '{section}': {sorted(unknown)}")


def _build(cls, section: str, values: dict | None):
    values = dict(values or {})
    _reject_unknown(section, values, (f.name for f in fields(cls)))
    return cls(**values)


@dataclass(frozen=True)
class CameraConfig:
    camera_id: str
    serial_number: str
    steering_role: str
    millimeters_per_pixel: float
    zero_pixel: float
    reverse_pixels: bool = False
    pipe_size: int = 4 * 1024 * 1024
    initial_packet_length: int = 4096
    expected_pixels: int = 2048
    byte_order: str = ">u2"
    wait_timeout_ms: int = 3000
    endpoint_timeout_ms: int = 1000

    def __post_init__(self):
        if not self.camera_id.strip():
            raise ValueError("camera_id must not be empty")
        if not self.serial_number.strip():
            raise ValueError(f"{self.camera_id}: serial_number is required")
        if self.steering_role not in ("top", "bottom"):
            raise ValueError(f"{self.camera_id}: steering_role must be top or bottom")
        if self.millimeters_per_pixel <= 0:
            raise ValueError(f"{self.camera_id}: millimeters_per_pixel must be positive")
        if self.expected_pixels < 1:
            raise ValueError(f"{self.camera_id}: expected_pixels must be positive")
        if not 0.0 <= self.zero_pixel <= self.expected_pixels - 1:
            raise ValueError(f"{self.camera_id}: zero_pixel must be within the sensor")
        if self.initial_packet_length != self.expected_pixels * 2:
            raise ValueError(f"{self.camera_id}: initial_packet_length must equal expected_pixels * 2")
        if self.pipe_size < self.initial_packet_length:
            raise ValueError(f"{self.camera_id}: pipe_size is too small")
        if self.wait_timeout_ms < 1:
            raise ValueError(f"{self.camera_id}: wait_timeout_ms must be positive")
        if self.endpoint_timeout_ms < 1:
            raise ValueError(f"{self.camera_id}: endpoint_timeout_ms must be positive")
        if self.byte_order not in (">u2", "<u2"):
            raise ValueError(f"{self.camera_id}: unsupported byte_order")


@dataclass(frozen=True)
class SensorSettings:
    bias: int
    gain: int
    st_high: int
    st_low: int
    edge_delay: int

    def __post_init__(self):
        for name, low, high in (
            ("bias", 0, 1023),
            ("gain", 0, 63),
            ("st_high", 1, 0xFFFFFFFF),
            ("st_low", 0, 0xFFFFFFFF),
            ("edge_delay", 0, 255),
        ):
            if not low <= getattr(self, name) <= high:
                raise ValueError(f"sensor_settings.{name} must be {low}..{high}")


@dataclass(frozen=True)
class SteeringConfig:
    sensor_separation_mm: float = 230.0
    measurement_timeout_ms: int = 1000

    def __post_init__(self):
        if self.sensor_separation_mm <= 0:
            raise ValueError("steering.sensor_separation_mm must be positive")
        if self.measurement_timeout_ms < 1:
            raise ValueError("steering.measurement_timeout_ms must be positive")


@dataclass(frozen=True)
class OutputConfig:
    deadband_deg: float = 0.02
    min_interval_ms: int = 20
    keepalive_ms: int = 100
    worker_silent_timeout_ms: int = 500

    def __post_init__(self):
        if self.deadband_deg < 0:
            raise ValueError("output.deadband_deg cannot be negative")
        if self.min_interval_ms < 0:
            raise ValueError("output.min_interval_ms cannot be negative")
        if self.keepalive_ms < max(1, self.min_interval_ms):
            raise ValueError("output.keepalive_ms must be at least min_interval_ms")
        if self.worker_silent_timeout_ms <= self.keepalive_ms:
            raise ValueError("output.worker_silent_timeout_ms must exceed keepalive_ms")


@dataclass(frozen=True)
class LoggingConfig:
    directory: str = "logs/laser_steering"
    log_all_updates: bool = False
    max_file_mb: float = 20.0
    backup_count: int = 10

    def __post_init__(self):
        if self.max_file_mb <= 0:
            raise ValueError("logging.max_file_mb must be positive")
        if self.backup_count < 0:
            raise ValueError("logging.backup_count cannot be negative")


@dataclass(frozen=True)
class SupervisorConfig:
    restart_backoff_s: tuple[float, ...] = (1.0, 2.0, 5.0, 10.0)
    status_publish_s: float = 2.0
    startup_grace_s: float = 20.0
    stall_restart_s: float = 2.0

    def __post_init__(self):
        object.__setattr__(self, "restart_backoff_s", tuple(float(v) for v in self.restart_backoff_s))
        if not self.restart_backoff_s or min(self.restart_backoff_s) <= 0:
            raise ValueError("supervisor.restart_backoff_s must contain positive values")
        if self.status_publish_s <= 0:
            raise ValueError("supervisor.status_publish_s must be positive")
        if self.stall_restart_s <= 0:
            raise ValueError("supervisor.stall_restart_s must be positive")


@dataclass(frozen=True)
class Settings:
    base_directory: Path
    enabled: bool
    sdk_directory: Path
    library_path: Path
    cameras: tuple[CameraConfig, CameraConfig]
    sensor_settings: SensorSettings
    detector: TriggerConfig
    quality: QualityConfig
    steering: SteeringConfig
    output: OutputConfig
    logging: LoggingConfig
    supervisor: SupervisorConfig
    configuration_settle_ms: int = 150
    configuration_readback_retries: int = 5
    configuration_readback_retry_ms: int = 100
    settle_frames: int = 16

    def camera(self, role: str) -> CameraConfig:
        return next(c for c in self.cameras if c.steering_role == role)

    def resolve(self, value: str | Path) -> Path:
        path = Path(value).expanduser()
        return path if path.is_absolute() else (self.base_directory / path).resolve()

    @classmethod
    def load(cls, path: str | Path) -> "Settings":
        config_path = Path(path).expanduser().resolve()
        data = json.loads(config_path.read_text(encoding="utf-8"))
        base = config_path.parent

        top_level = {
            "enabled", "sdk_directory", "library_path", "cameras",
            "sensor_settings", "detector", "quality", "steering", "output",
            "logging", "supervisor", "configuration_settle_ms",
            "configuration_readback_retries", "configuration_readback_retry_ms",
            "settle_frames",
        }
        _reject_unknown("top level", data, top_level)

        cameras = tuple(_build(CameraConfig, "cameras", c) for c in data.get("cameras", []))
        if len(cameras) != 2:
            raise ValueError("Exactly two cameras are required")
        if sorted(c.steering_role for c in cameras) != ["bottom", "top"]:
            raise ValueError("Cameras must be exactly one top and one bottom")
        if len({c.camera_id for c in cameras}) != 2:
            raise ValueError("Duplicate camera_id values")
        if len({c.serial_number for c in cameras}) != 2:
            raise ValueError("Duplicate camera serial numbers")

        detector_values = dict(data.get("detector", {}))
        _reject_unknown("detector", detector_values, TriggerConfig.__dataclass_fields__)
        quality_values = dict(data.get("quality", {}))
        _reject_unknown("quality", quality_values, QualityConfig.__dataclass_fields__)

        settings = cls(
            base_directory=base,
            enabled=bool(data.get("enabled", True)),
            sdk_directory=(base / data.get("sdk_directory", "lism_steering/sdk")).resolve(),
            library_path=(base / data.get("library_path", "lism_steering/sdk/libusblismpi2664.so")).resolve(),
            cameras=cameras,
            sensor_settings=_build(SensorSettings, "sensor_settings", data.get("sensor_settings")),
            detector=TriggerConfig(**detector_values),
            quality=QualityConfig(**quality_values),
            steering=_build(SteeringConfig, "steering", data.get("steering")),
            output=_build(OutputConfig, "output", data.get("output")),
            logging=_build(LoggingConfig, "logging", data.get("logging")),
            supervisor=_build(SupervisorConfig, "supervisor", data.get("supervisor")),
            **{
                key: data[key]
                for key in (
                    "configuration_settle_ms", "configuration_readback_retries",
                    "configuration_readback_retry_ms", "settle_frames",
                )
                if key in data
            },
        )
        if settings.configuration_readback_retries < 1:
            raise ValueError("configuration_readback_retries must be at least 1")
        return settings
