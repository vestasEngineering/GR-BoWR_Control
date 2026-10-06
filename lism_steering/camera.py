from __future__ import annotations

import time

import numpy as np


class CameraError(RuntimeError):
    pass


class LismCamera:
    """One USB LISM sensor opened through a shared SDK session."""

    ACTIVE_CONFIGURATION_GETTERS = {
        "st_high": "getsthigh",
        "st_low": "getstlow",
        "edge_delay": "getedgedelay",
        "adc_gain": "getadcgain",
        "adc_bias": "getadcbias",
    }

    def __init__(self, session, camera_config, settings):
        self.session = session
        self.config = camera_config
        self.settings = settings
        self.camera_id = camera_config.camera_id
        self.serial = camera_config.serial_number
        self.sdk = session.sdk
        self.dev = None
        self.packet_length = 0
        self.streaming = False

    def _check(self, rc: int, operation: str) -> None:
        if rc != self.sdk.RET_OK:
            try:
                text = self.session.api.geterrorstring(rc) or ""
            except Exception:
                text = ""
            suffix = f": {text}" if text else ""
            raise CameraError(
                f"{self.camera_id}: {operation} failed with SDK result {rc}{suffix}"
            )

    def open(self) -> None:
        if self.dev is not None:
            return
        self.dev = self.session.open_device(
            self.serial, self.config.pipe_size, self.config.initial_packet_length
        )
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
                    "one line per transfer is required"
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
            self._check(self.dev.setstate(self.sdk.LISM_ADDR, 1), "setstate(start)")
            self.streaming = True

    def stop(self) -> None:
        if self.dev is not None and self.streaming:
            self._check(self.dev.setstate(self.sdk.LISM_ADDR, 0), "setstate(stop)")
            self.streaming = False

    def read_frame(self) -> np.ndarray:
        if not self.streaming:
            raise CameraError(f"{self.camera_id}: cannot read while acquisition is stopped")
        rc, available = self.dev.waitforpipecount(self.packet_length, self.config.wait_timeout_ms)
        self._check(rc, "waitforpipecount")
        if available < self.packet_length:
            raise CameraError(f"{self.camera_id}: wait returned only {available} available bytes")
        rc, data = self.dev.getpipe(self.packet_length)
        self._check(rc, "getpipe")
        if len(data) != self.packet_length:
            raise CameraError(
                f"{self.camera_id}: short packet, expected {self.packet_length}, got {len(data)}"
            )
        frame = np.frombuffer(data, dtype=self.config.byte_order).astype(np.uint16, copy=True)
        if frame.size != self.config.expected_pixels:
            raise CameraError(
                f"{self.camera_id}: decoded {frame.size} pixels, expected {self.config.expected_pixels}"
            )
        if self.config.reverse_pixels:
            frame = frame[::-1].copy()
        return frame

    def _get(self, name: str) -> int:
        rc, value = getattr(self.dev, name)(self.sdk.LISM_ADDR)
        self._check(rc, name)
        return value

    def _get_with_retry(self, name: str) -> int:
        last_error = None
        retries = self.settings.configuration_readback_retries
        for attempt in range(retries):
            try:
                return self._get(name)
            except CameraError as exc:
                last_error = exc
                if attempt + 1 < retries:
                    time.sleep(self.settings.configuration_readback_retry_ms / 1000.0)
        raise CameraError(f"{name} failed after {retries} attempts: {last_error}")

    def active_configuration(self) -> dict:
        return {
            field: self._get_with_retry(getter)
            for field, getter in self.ACTIVE_CONFIGURATION_GETTERS.items()
        }

    def apply_temporary_configuration(self, *, st_high, st_low, edge_delay, adc_gain, adc_bias) -> dict:
        if self.streaming:
            self.stop()
        for name, value in (
            ("setsthigh", st_high),
            ("setstlow", st_low),
            ("setedgedelay", edge_delay),
            ("setadcgain", adc_gain),
            ("setadcbias", adc_bias),
        ):
            self._check(getattr(self.dev, name)(self.sdk.LISM_ADDR, value), name)
        self._check(self.dev.updateparam(self.sdk.LISM_ADDR), "updateparam")
        time.sleep(self.settings.configuration_settle_ms / 1000.0)
        observed = self.active_configuration()
        expected = {
            "st_high": st_high,
            "st_low": st_low,
            "edge_delay": edge_delay,
            "adc_gain": adc_gain,
            "adc_bias": adc_bias,
        }
        mismatches = {
            key: (value, observed[key]) for key, value in expected.items() if observed[key] != value
        }
        if mismatches:
            raise CameraError(f"{self.camera_id}: configuration readback mismatch: {mismatches}")
        return observed

    def close(self) -> None:
        stop_error = None
        if self.dev is not None:
            try:
                self.stop()
            except Exception as exc:
                stop_error = exc
            try:
                self.session.close_device(self.serial)
            finally:
                self.dev = None
                self.streaming = False
        if stop_error:
            raise stop_error
