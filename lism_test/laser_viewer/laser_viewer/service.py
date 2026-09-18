from __future__ import annotations
import csv
import queue
import threading
import time
from dataclasses import dataclass
from pathlib import Path
import numpy as np
from .calibration import CalibrationStore, add_laser, build_dark
from .camera import LismCamera
from .processing import detect

@dataclass
class Command:
    name: str
    response: queue.Queue

class AcquisitionService:
    """One thread exclusively owns the SDK device and services calibration commands."""
    def __init__(self, config, config_base: Path):
        self.config = config
        self.config_base = config_base
        self.store = CalibrationStore(config.resolve(config_base, config.calibration_directory))
        self.commands: queue.Queue[Command] = queue.Queue()
        self.lock = threading.Lock()
        self.latest = {"state": "stopped", "sequence": 0, "profile": []}
        self.stop_event = threading.Event()
        self.thread = None
        self.csv_file = None
        self.csv_writer = None

    def start(self):
        if self.thread and self.thread.is_alive():
            return
        self.stop_event.clear()
        self.thread = threading.Thread(target=self._run, name="lism-acquisition", daemon=True)
        self.thread.start()

    def stop(self):
        self.stop_event.set()
        if self.thread:
            self.thread.join(timeout=max(5.0, self.config.wait_timeout_ms / 1000 + 2))
        self._close_log()

    def command(self, name: str, timeout: float = 120.0):
        response = queue.Queue(maxsize=1)
        self.commands.put(Command(name, response))
        ok, payload = response.get(timeout=timeout)
        if not ok:
            raise RuntimeError(payload)
        return payload

    def snapshot(self):
        with self.lock:
            return dict(self.latest)

    def _set(self, **values):
        with self.lock:
            self.latest.update(values)

    def _open_log(self):
        if self.csv_file:
            return
        directory = self.config.resolve(self.config_base, self.config.log_directory)
        directory.mkdir(parents=True, exist_ok=True)
        path = directory / time.strftime("laser_%Y%m%d_%H%M%S.csv")
        self.csv_file = path.open("w", newline="", encoding="utf-8")
        self.csv_writer = csv.writer(self.csv_file)
        self.csv_writer.writerow(["unix_time", "sequence", "detected", "peak_pixel", "centroid", "amplitude", "snr"])
        self._set(logging=True, log_path=str(path))

    def _close_log(self):
        if self.csv_file:
            self.csv_file.close()
            self.csv_file = None
            self.csv_writer = None
        self._set(logging=False)

    def _run(self):
        sequence = 0
        try:
            with LismCamera(self.config, self.config_base) as camera:
                signature = camera.signature()
                calibration = self.store.load()
                if calibration:
                    self.store.validate(calibration, signature)
                camera.start()
                for _ in range(self.config.settle_frames):
                    camera.read_frame()
                self._set(state="running", error=None, signature=signature.as_dict(), calibrated=calibration is not None, laser_calibrated=bool(calibration and calibration.has_laser), logging=False)
                next_publish = 0.0
                while not self.stop_event.is_set():
                    while True:
                        try:
                            cmd = self.commands.get_nowait()
                        except queue.Empty:
                            break
                        try:
                            if cmd.name == "dark":
                                self._set(state="calibrating_dark")
                                frames = camera.capture_frames(self.config.dark_frames, self.config.settle_frames)
                                calibration = build_dark(frames, signature)
                                self.store.save(calibration)
                                result = {"created_utc": calibration.created_utc}
                            elif cmd.name == "laser":
                                if calibration is None:
                                    raise RuntimeError("Dark calibration is required first")
                                self.store.validate(calibration, signature)
                                self._set(state="calibrating_laser")
                                frames = camera.capture_frames(self.config.laser_frames, self.config.settle_frames)
                                calibration = add_laser(calibration, frames, self.config.noise_sigma, self.config.minimum_signal, self.config.calibration_noise_sigma, self.config.calibration_minimum_signal, self.config.calibration_relative_threshold)
                                self.store.save(calibration)
                                result = {"laser_created_utc": calibration.laser_created_utc}
                            elif cmd.name == "log_start":
                                self._open_log(); result = {"logging": True}
                            elif cmd.name == "log_stop":
                                self._close_log(); result = {"logging": False}
                            else:
                                raise ValueError(f"Unknown command {cmd.name}")
                            self._set(state="running", calibrated=calibration is not None, laser_calibrated=bool(calibration and calibration.has_laser))
                            cmd.response.put((True, result))
                        except Exception as exc:
                            self._set(state="running", error=str(exc))
                            cmd.response.put((False, str(exc)))
                    frame = camera.read_frame()
                    sequence += 1
                    if calibration is not None:
                        result = detect(frame, calibration, self.config.noise_sigma, self.config.minimum_signal, self.config.centroid_half_width, self.config.minimum_region_width, self.config.maximum_region_width, self.config.clip_margin)
                    else:
                        result = None
                    if self.csv_writer and result:
                        self.csv_writer.writerow([time.time(), sequence, result.detected, result.peak_pixel, result.centroid, result.amplitude, result.snr])
                        if sequence % 50 == 0:
                            self.csv_file.flush()
                    now = time.monotonic()
                    if now >= next_publish:
                        stride = max(1, frame.size // 512)
                        profile = (result.corrected if result else frame)[::stride].round(2).tolist()
                        payload = {
                            "state": "running", "sequence": sequence, "timestamp": time.time(),
                            "profile": profile, "profile_stride": stride,
                            "raw_min": float(frame.min()), "raw_max": float(frame.max()),
                            "detected": bool(result and result.detected),
                            "peak_pixel": result.peak_pixel if result else None,
                            "centroid": result.centroid if result else None,
                            "amplitude": result.amplitude if result else None,
                            "threshold": result.threshold if result else None,
                            "snr": result.snr if result else None,
                            "low_clipped": bool(result and result.low_clipped),
                            "high_clipped": bool(result and result.high_clipped),
                            "region_start": result.region_start if result else None,
                            "region_end": result.region_end if result else None,
                            "region_width": result.region_width if result else 0,
                            "integrated_signal": result.integrated_signal if result else 0.0,
                            "polarity": result.polarity if result else None,
                        }
                        self._set(**payload)
                        next_publish = now + 1.0 / max(self.config.publish_hz, 1.0)
        except Exception as exc:
            self._set(state="fault", error=str(exc))
            while True:
                try:
                    cmd = self.commands.get_nowait()
                    cmd.response.put((False, str(exc)))
                except queue.Empty:
                    break
        finally:
            self._close_log()
