"""Acquisition process: two LISM cameras -> steering angle -> JSON lines on the pipe.

Run as ``python -m lism_steering.worker --config laser_steering_config.json``.
stdout carries only protocol lines (the vendor library's own prints are
redirected to stderr). The process exits when stdin closes, so it can never
outlive the supervisor and keep the USB devices claimed.
"""
from __future__ import annotations

import argparse
import json
import logging
import os
import signal
import sys
import threading
import time
from logging.handlers import RotatingFileHandler

from .camera import LismCamera
from .config import Settings
from .detector import detect_frame
from .sdk_session import LismSdkSession
from .steering import (
    SendPolicy,
    SensorCalibration,
    SteeringEstimator,
    measurement_from_detection,
)

LOG = logging.getLogger("lism_steering.worker")
CSV_HEADER = (
    "unix_time,monotonic_ms,sent,valid,angle_deg,top_mm,bottom_mm,"
    "top_age_ms,bottom_age_ms,reason"
)
STATUS_PERIOD_S = 0.5
TICK_S = 0.005


class _CsvHandler(RotatingFileHandler):
    def _open(self):
        stream = super()._open()
        if os.fstat(stream.fileno()).st_size == 0:
            stream.write(CSV_HEADER + "\n")
        return stream


def _fmt(value, digits):
    return "" if value is None else f"{value:.{digits}f}"


class Worker:
    def __init__(self, settings: Settings, out):
        self.settings = settings
        self.out = out
        self.stop_event = threading.Event()
        self.fault = None
        self.emit_lock = threading.Lock()
        self.publish_lock = threading.Lock()
        top, bottom = settings.camera("top"), settings.camera("bottom")
        self.estimator = SteeringEstimator(
            SensorCalibration(top.camera_id, top.millimeters_per_pixel, top.zero_pixel),
            SensorCalibration(bottom.camera_id, bottom.millimeters_per_pixel, bottom.zero_pixel),
            settings.steering.sensor_separation_mm,
            settings.steering.measurement_timeout_ms,
        )
        out_cfg = settings.output
        self.policy = SendPolicy(out_cfg.deadband_deg, out_cfg.min_interval_ms, out_cfg.keepalive_ms)
        self.sensors = {
            c.camera_id: {"frames": 0, "valid": 0, "position_px": None, "valid_ns": None}
            for c in settings.cameras
        }
        self.sent = 0
        self.last_result = None
        self.csv = self._open_csv()

    def _open_csv(self):
        cfg = self.settings.logging
        directory = self.settings.resolve(cfg.directory)
        directory.mkdir(parents=True, exist_ok=True)
        handler = _CsvHandler(
            directory / "laser_steering.csv",
            maxBytes=int(cfg.max_file_mb * 1024 * 1024),
            backupCount=cfg.backup_count,
            encoding="utf-8",
        )
        handler.setFormatter(logging.Formatter("%(message)s"))
        csv_log = logging.getLogger("lism_steering.csv")
        csv_log.setLevel(logging.INFO)
        csv_log.propagate = False
        csv_log.addHandler(handler)
        return csv_log

    def emit(self, message: dict) -> None:
        line = json.dumps(message, separators=(",", ":"), allow_nan=False) + "\n"
        with self.emit_lock:
            try:
                self.out.write(line)
                self.out.flush()
            except (BrokenPipeError, ValueError):
                self.stop_event.set()

    def fail(self, error: str) -> None:
        if self.fault is None:
            self.fault = error
            LOG.error("fault: %s", error)
        self.stop_event.set()

    def publish(self, now_ns: int) -> None:
        with self.publish_lock:
            result = self.estimator.evaluate(now_ns)
            self.last_result = result
            send = self.policy.should_send(result, now_ns)
            if send:
                self.policy.mark_sent(result, now_ns)
                self.sent += 1
                self.emit({
                    "k": "steer",
                    "valid": result.valid,
                    "angle": result.angle_degrees,
                    "reason": result.reason,
                })
            if send or self.settings.logging.log_all_updates:
                self.csv.info(",".join((
                    f"{time.time():.6f}", f"{now_ns / 1e6:.3f}", "1" if send else "0",
                    "1" if result.valid else "0", _fmt(result.angle_degrees, 4),
                    _fmt(result.top_position_mm, 4), _fmt(result.bottom_position_mm, 4),
                    _fmt(result.top_age_ms, 2), _fmt(result.bottom_age_ms, 2), result.reason,
                )))

    def capture(self, camera: LismCamera) -> None:
        stats = self.sensors[camera.camera_id]
        try:
            while not self.stop_event.is_set():
                frame = camera.read_frame()
                now_ns = time.monotonic_ns()
                stats["frames"] += 1
                measurement = measurement_from_detection(
                    camera.camera_id,
                    stats["frames"],
                    now_ns,
                    detect_frame(frame, self.settings.detector, self.settings.quality),
                )
                if measurement.valid:
                    stats["valid"] += 1
                    stats["position_px"] = measurement.position_pixels
                    stats["valid_ns"] = now_ns
                self.estimator.ingest(measurement)
                self.publish(now_ns)
        except Exception as exc:
            self.fail(f"{camera.camera_id}: {exc}")

    def status(self, state: str) -> dict:
        now_ns = time.monotonic_ns()
        result = self.last_result
        sensors = {}
        for camera_id, stats in self.sensors.items():
            valid_ns = stats["valid_ns"]
            sensors[camera_id] = {
                "frames": stats["frames"],
                "valid": stats["valid"],
                "position_px": stats["position_px"],
                "age_ms": None if valid_ns is None else round((now_ns - valid_ns) / 1e6, 1),
            }
        return {
            "k": "status",
            "state": state,
            "valid": bool(result and result.valid),
            "angle": None if result is None else result.angle_degrees,
            "reason": "starting" if result is None else result.reason,
            "sent": self.sent,
            "sensors": sensors,
        }

    def run(self) -> int:
        settings = self.settings
        session = LismSdkSession(settings.sdk_directory, settings.library_path)
        cameras = []
        originals = {}
        threads = []
        try:
            session.open()
            for camera_config in settings.cameras:
                camera = LismCamera(session, camera_config, settings)
                camera.open()
                cameras.append(camera)
            sensor = settings.sensor_settings
            for camera in cameras:
                originals[camera.camera_id] = camera.active_configuration()
                LOG.info("%s original configuration: %s", camera.camera_id, originals[camera.camera_id])
                observed = camera.apply_temporary_configuration(
                    st_high=sensor.st_high,
                    st_low=sensor.st_low,
                    edge_delay=sensor.edge_delay,
                    adc_gain=sensor.gain,
                    adc_bias=sensor.bias,
                )
                camera.start()
                LOG.info("%s configured: %s", camera.camera_id, observed)
                for _ in range(settings.settle_frames):
                    camera.read_frame()

            threads = [
                threading.Thread(target=self.capture, args=(camera,), name=f"capture-{camera.camera_id}", daemon=True)
                for camera in cameras
            ]
            for thread in threads:
                thread.start()
            LOG.info("running")
            self.emit(self.status("running"))

            next_status = time.monotonic() + STATUS_PERIOD_S
            while not self.stop_event.wait(TICK_S):
                self.publish(time.monotonic_ns())
                if time.monotonic() >= next_status:
                    next_status += STATUS_PERIOD_S
                    self.emit(self.status("running"))
        except Exception as exc:
            self.fail(f"startup: {exc}")
        finally:
            self.stop_event.set()
            join_s = max(c.wait_timeout_ms for c in settings.cameras) / 1000.0 + 1.0
            for thread in threads:
                thread.join(join_s)
            if any(thread.is_alive() for thread in threads):
                LOG.error("capture thread did not stop; skipping device cleanup")
            else:
                for camera in cameras:
                    try:
                        camera.stop()
                        if camera.camera_id in originals:
                            restored = camera.apply_temporary_configuration(**originals[camera.camera_id])
                            LOG.info("%s restored original configuration: %s", camera.camera_id, restored)
                    except Exception as exc:
                        LOG.error("%s restore failed: %s", camera.camera_id, exc)
                    try:
                        camera.close()
                    except Exception as exc:
                        LOG.error("%s close failed: %s", camera.camera_id, exc)
                try:
                    session.close()
                except Exception as exc:
                    LOG.error("sdk close failed: %s", exc)
            if self.fault:
                self.emit({"k": "fault", "error": self.fault})
        return 2 if self.fault else 0


def _watch_stdin(stop_event: threading.Event) -> None:
    try:
        while os.read(0, 4096):
            pass
    except OSError:
        pass
    finally:
        stop_event.set()


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="LISM steering acquisition worker")
    parser.add_argument("--config", required=True)
    args = parser.parse_args(argv)

    out = os.fdopen(os.dup(1), "w", buffering=1)
    os.dup2(2, 1)
    logging.basicConfig(
        stream=sys.stderr,
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )

    worker = Worker(Settings.load(args.config), out)
    for signum in (signal.SIGTERM, signal.SIGINT):
        signal.signal(signum, lambda *_: worker.stop_event.set())
    if not sys.stdin.isatty():
        threading.Thread(target=_watch_stdin, args=(worker.stop_event,), daemon=True).start()
    return worker.run()


if __name__ == "__main__":
    sys.exit(main())
