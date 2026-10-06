from __future__ import annotations

import sys
import threading
from pathlib import Path


class SdkSessionError(RuntimeError):
    pass


class LismSdkSession:
    """Owns the vendor library handle shared by both cameras."""

    def __init__(self, sdk_directory: Path, library_path: Path):
        self.sdk_directory = Path(sdk_directory)
        self.library_path = Path(library_path)
        self.sdk = None
        self.api = None
        self.serials: tuple[str, ...] = ()
        self.handles = {}
        self.lock = threading.RLock()
        self.closed = False

    def open(self):
        with self.lock:
            if self.api:
                return
            if str(self.sdk_directory) not in sys.path:
                sys.path.insert(0, str(self.sdk_directory))
            import usblismpi26 as sdk

            self.sdk = sdk
            self.api = sdk.USBLISMPI26(str(self.library_path))
            if self.api.libversion() < 0x0200:
                raise SdkSessionError("Vendor library Version 2.0 or later is required")
            count = self.api.enumdevices()
            if count < 0:
                raise SdkSessionError(f"Enumeration failed: {count}")
            self.serials = tuple(self.api.getserialnumber(i) or "" for i in range(count))

    def open_device(self, serial, pipe_size, packet_length):
        with self.lock:
            self.open()
            if serial in self.handles:
                raise SdkSessionError(f"{serial} is already open")
            if serial not in self.serials:
                raise SdkSessionError(f"{serial} not found; enumerated {self.serials}")
            rc, device = self.api.opendevicebyserial_ex(serial, pipe_size, packet_length)
            if rc != self.sdk.RET_OK or device is None:
                raise SdkSessionError(f"Open {serial} failed: {rc} {self.api.geterrorstring(rc)}")
            self.handles[serial] = device
            return device

    def close_device(self, serial):
        with self.lock:
            device = self.handles.pop(serial, None)
        if device is not None:
            rc = device.close()
            if rc != self.sdk.RET_OK:
                raise SdkSessionError(f"Close {serial} failed: {rc}")

    def close(self):
        with self.lock:
            if self.closed:
                return
            items = list(self.handles.items())
            self.handles.clear()
            api = self.api
            self.closed = True
        errors = []
        for serial, device in items:
            try:
                device.close()
            except Exception as exc:
                errors.append(f"{serial}: {exc}")
        if api:
            try:
                api.shutdown()
            except Exception as exc:
                errors.append(f"shutdown: {exc}")
        if errors:
            raise SdkSessionError("; ".join(errors))
