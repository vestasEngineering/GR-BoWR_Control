from __future__ import annotations

import sys
import time
from pathlib import Path

import numpy as np

from .models import DeviceSignature


class CameraError(RuntimeError):
    pass


class LismCamera:
    """Camera adapter with existing and dual-camera construction support.

    Existing callers:
        LismCamera(config, config_base)

    Dual-camera viewer:
        LismCamera.from_shared_device(session, camera_config)
    """

    ACTIVE_CONFIGURATION_GETTERS = {
        "st_high": "getsthigh",
        "st_low": "getstlow",
        "edge_delay": "getedgedelay",
        "adc_gain": "getadcgain",
        "adc_bias": "getadcbias",
        "adc_offset": "getadcoffset",
    }

    def __init__(self, config, config_base: Path):
        self.project_config = config
        self.config = config
        self.config_base = Path(config_base)
        self.session = None
        self.cam = None
        self.dev = None
        self.sdk = None
        self.packet_length = 0
        self.serial = "unknown"
        self.camera_id = f"device_index_{getattr(config, 'device_index', 0)}"
        self.streaming = False
        self._owns_sdk = True

    @classmethod
    def from_shared_device(cls, session, camera_config):
        instance = cls.__new__(cls)
        instance.project_config = session.config
        instance.config = camera_config
        instance.config_base = None
        instance.session = session
        instance.cam = None
        instance.dev = None
        instance.sdk = session.sdk
        instance.packet_length = 0
        instance.serial = camera_config.serial_number
        instance.camera_id = camera_config.camera_id
        instance.streaming = False
        instance._owns_sdk = False
        return instance

    def _runtime_setting(self, name: str):
        return getattr(self.project_config, name)

    def _error_text(self, rc: int) -> str:
        try:
            api = self.session.api if self.session is not None else self.cam
            if api is not None:
                return api.geterrorstring(rc) or ""
        except Exception:
            pass
        return ""

    def _check(self, rc: int, operation: str) -> None:
        if rc != self.sdk.RET_OK:
            text = self._error_text(rc)
            suffix = f": {text}" if text else ""
            raise CameraError(
                f"{self.camera_id}: {operation} failed with SDK result {rc}{suffix}"
            )

    def open(self) -> None:
        if self.dev is not None:
            return

        if self.session is not None:
            self.dev = self.session.open_device(
                self.serial,
                self.config.pipe_size,
                self.config.initial_packet_length,
            )
        else:
            sdk_dir = self.project_config.resolve(
                self.config_base,
                self.project_config.sdk_directory,
            )
            if str(sdk_dir) not in sys.path:
                sys.path.insert(0, str(sdk_dir))
            try:
                import usblismpi26 as sdk
            except Exception as exc:
                raise CameraError(
                    f"Could not import SDK from {sdk_dir}: {exc}"
                ) from exc
            self.sdk = sdk
            library = self.project_config.resolve(
                self.config_base,
                self.project_config.library_path,
            )
            self.cam = sdk.USBLISMPI26(str(library))
            count = self.cam.enumdevices()
            if count < 0:
                self.close()
                raise CameraError(f"Device enumeration failed with result {count}")

            cameras = tuple(getattr(self.project_config, "cameras", ()) or ())
            if cameras:
                camera_config = cameras[0]
                self.config = camera_config
                self.camera_id = camera_config.camera_id
                self.serial = camera_config.serial_number
                rc, self.dev = self.cam.opendevicebyserial_ex(
                    self.serial,
                    self.config.pipe_size,
                    self.config.initial_packet_length,
                )
                operation = "opendevicebyserial_ex"
            else:
                index = self.project_config.device_index
                if count <= index:
                    self.close()
                    raise CameraError(
                        f"Device index {index} unavailable; enumerated {count}"
                    )
                self.serial = self.cam.getserialnumber(index) or "unknown"
                rc, self.dev = self.cam.opendevicebyindex_ex(
                    index,
                    self.project_config.pipe_size,
                    self.project_config.initial_packet_length,
                )
                operation = "opendevicebyindex_ex"
            self._check(rc, operation)

        try:
            rc, self.packet_length = self.dev.getpacketlength()
            self._check(rc, "getpacketlength")
            pixel_count = self._get_with_retry("getpixelcount")
            rc, cfg1 = self.dev.getcfg1()
            self._check(rc, "getcfg1")
            image_count = ((cfg1 >> 2) & 0x1F) + 1
            expected_packet_length = pixel_count * 2 * image_count
            if pixel_count != self.config.expected_pixels:
                raise CameraError(
                    f"{self.camera_id}: pixel count {pixel_count} does not match "
                    f"configured {self.config.expected_pixels}"
                )
            if image_count != 1:
                raise CameraError(
                    f"{self.camera_id}: CFG1 groups {image_count} lines; "
                    "the viewer requires one line per transfer"
                )
            if self.packet_length != expected_packet_length:
                raise CameraError(
                    f"{self.camera_id}: packet length {self.packet_length} does not "
                    f"match expected {expected_packet_length}"
                )
        except Exception:
            try:
                self.close()
            except Exception:
                pass
            raise

    def start(self) -> None:
        if self.dev is None:
            raise CameraError(f"{self.camera_id}: camera is not open")
        if not self.streaming:
            self._check(
                self.dev.setstate(self.sdk.LISM_ADDR, 1),
                "setstate(start)",
            )
            self.streaming = True

    def stop(self) -> None:
        if self.dev is not None and self.streaming:
            self._check(
                self.dev.setstate(self.sdk.LISM_ADDR, 0),
                "setstate(stop)",
            )
            self.streaming = False

    def read_frame(self) -> np.ndarray:
        if not self.streaming:
            raise CameraError(
                f"{self.camera_id}: cannot read while acquisition is stopped"
            )
        rc, available = self.dev.waitforpipecount(
            self.packet_length,
            self.config.wait_timeout_ms,
        )
        self._check(rc, "waitforpipecount")
        if available < self.packet_length:
            raise CameraError(
                f"{self.camera_id}: wait returned only {available} available bytes"
            )
        rc, data = self.dev.getpipe(self.packet_length)
        self._check(rc, "getpipe")
        if len(data) != self.packet_length:
            raise CameraError(
                f"{self.camera_id}: short packet, expected {self.packet_length}, "
                f"got {len(data)}"
            )
        frame = np.frombuffer(data, dtype=self.config.byte_order).astype(
            np.uint16,
            copy=True,
        )
        if frame.size != self.config.expected_pixels:
            raise CameraError(
                f"{self.camera_id}: decoded {frame.size} pixels, expected "
                f"{self.config.expected_pixels}"
            )
        if bool(getattr(self.config, "reverse_pixels", False)):
            frame = frame[::-1].copy()
        return frame

    def capture_frames(self, count: int, settle: int = 0) -> np.ndarray:
        if count <= 0:
            raise ValueError("count must be positive")
        for _ in range(settle):
            self.read_frame()
        return np.stack([self.read_frame() for _ in range(count)])

    def _get(self, name: str) -> int:
        rc, value = getattr(self.dev, name)(self.sdk.LISM_ADDR)
        self._check(rc, name)
        return value

    def _get_with_retry(self, name: str) -> int:
        last_error = None
        retries = self._runtime_setting("configuration_readback_retries")
        retry_ms = self._runtime_setting("configuration_readback_retry_ms")
        for attempt in range(retries):
            try:
                return self._get(name)
            except CameraError as exc:
                last_error = exc
                if attempt + 1 < retries:
                    time.sleep(retry_ms / 1000.0)
        raise CameraError(
            f"{name} failed after {retries} attempts: {last_error}"
        )

    def signature(self) -> DeviceSignature:
        return DeviceSignature(
            serial=self.serial,
            pixel_count=self._get_with_retry("getpixelcount"),
            packet_length=self.packet_length,
            st_high=self._get_with_retry("getsthigh"),
            st_low=self._get_with_retry("getstlow"),
            edge_delay=self._get_with_retry("getedgedelay"),
            adc_gain=self._get_with_retry("getadcgain"),
            adc_bias=self._get_with_retry("getadcbias"),
            adc_offset=self._get_with_retry("getadcoffset"),
        )

    def configuration(self) -> dict:
        return self.signature().as_dict()

    def active_configuration(self) -> dict:
        return {
            field: self._get_with_retry(getter)
            for field, getter in self.ACTIVE_CONFIGURATION_GETTERS.items()
            if field != "adc_offset"
        }

    def apply_temporary_configuration(
        self,
        *,
        st_high: int,
        st_low: int,
        edge_delay: int,
        adc_gain: int,
        adc_bias: int,
    ) -> dict:
        if self.streaming:
            self.stop()
        for name, value in (
            ("setsthigh", st_high),
            ("setstlow", st_low),
            ("setedgedelay", edge_delay),
            ("setadcgain", adc_gain),
            ("setadcbias", adc_bias),
        ):
            self._check(
                getattr(self.dev, name)(self.sdk.LISM_ADDR, value),
                name,
            )
        self._check(
            self.dev.updateparam(self.sdk.LISM_ADDR),
            "updateparam",
        )
        time.sleep(
            self._runtime_setting("configuration_settle_ms") / 1000.0
        )
        observed = self.active_configuration()
        expected = {
            "st_high": st_high,
            "st_low": st_low,
            "edge_delay": edge_delay,
            "adc_gain": adc_gain,
            "adc_bias": adc_bias,
        }
        mismatches = {
            key: (value, observed[key])
            for key, value in expected.items()
            if observed[key] != value
        }
        if mismatches:
            raise CameraError(
                f"{self.camera_id}: configuration readback mismatch: {mismatches}"
            )
        return observed

    def restore_configuration(self, original: dict) -> dict:
        return self.apply_temporary_configuration(
            st_high=original["st_high"],
            st_low=original["st_low"],
            edge_delay=original["edge_delay"],
            adc_gain=original["adc_gain"],
            adc_bias=original["adc_bias"],
        )

    def close(self) -> None:
        stop_error = None
        if self.dev is not None:
            try:
                self.stop()
            except Exception as exc:
                stop_error = exc
            try:
                if self.session is not None:
                    self.session.close_device(self.serial)
                else:
                    self.dev.close()
            finally:
                self.dev = None
                self.streaming = False
        if self._owns_sdk and self.cam is not None:
            try:
                self.cam.shutdown()
            finally:
                self.cam = None
        if stop_error:
            raise stop_error

    def __enter__(self):
        self.open()
        return self

    def __exit__(self, exc_type, exc_value, traceback):
        try:
            self.close()
        except Exception:
            if exc_value is None:
                raise
        return False
