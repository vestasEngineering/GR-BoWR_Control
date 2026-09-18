from __future__ import annotations

import argparse
import json
import queue
import threading
import time
import uuid
from collections import deque
from dataclasses import asdict, dataclass
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import numpy as np

from .downward_detector import QualityConfig, TriggerConfig, detect_frame
from .steering_output import (
    SensorCalibration,
    SteeringAnglePairer,
    measurement_from_detection,
)
from .trigger_recorder import TriggerSessionRecorder


def validate_settings(settings):
    values = {
        key: int(settings[key])
        for key in ("bias", "gain", "st_high", "st_low", "edge_delay")
    }
    for key, minimum, maximum in (
        ("bias", 0, 1023),
        ("gain", 0, 63),
        ("st_high", 1, 0xFFFFFFFF),
        ("st_low", 0, 0xFFFFFFFF),
        ("edge_delay", 0, 255),
    ):
        if not minimum <= values[key] <= maximum:
            raise ValueError(f"{key} must be {minimum}..{maximum}")
    return values


def validate_trigger(settings):
    config = TriggerConfig.from_mapping(settings)
    history_size = int(settings.get("history_size", 128))
    if not 1 <= history_size <= 256:
        raise ValueError("history_size must be 1..256")
    return {**asdict(config), "history_size": history_size}


def region_payload(region):
    if region is None:
        return None
    payload = {
        name: getattr(region, name)
        for name in region.__dataclass_fields__
    }
    payload["peak_drop"] = 65535 - int(region.minimum_raw)
    return payload


def detect_downward_region(frame_or_result, trigger=None, quality=None):
    """Serialize a result while preserving the legacy frame/trigger API."""
    if hasattr(frame_or_result, "selected_region"):
        if trigger is not None:
            raise TypeError("trigger must be omitted with a DetectionResult")
        result = frame_or_result
    else:
        if trigger is None:
            raise TypeError("trigger is required when passing a raw frame")
        config = (
            trigger
            if isinstance(trigger, TriggerConfig)
            else TriggerConfig.from_mapping(trigger)
        )
        result = detect_frame(np.asarray(frame_or_result), config, quality)
    # Legacy diagnostic compatibility only. Production steering consumes
    # DetectionResult.selected_region directly in SensorViewer.loop.
    region = result.selected_region
    if region is None and result.regions:
        region = max(result.regions, key=lambda item: item.integrated_drop)
    return region_payload(region)


@dataclass
class Event:
    number: int
    timestamp: float
    frame: np.ndarray
    detection: dict
    result: object

    def payload(self, now):
        return {
            "frame_number": self.number,
            "timestamp": self.timestamp,
            "age_ms": (now - self.timestamp) * 1000.0,
            "pixels": self.frame.tolist(),
            **self.detection,
        }


class SteeringCoordinator:
    """Single owner for pairing sensor results without blocking acquisition."""

    def __init__(self, pairer):
        self.pairer = pairer
        self.queue = queue.Queue(maxsize=8)
        self.running = threading.Event()
        self.thread = None
        self.lock = threading.Lock()
        self.latest = None
        self.error = None
        self.dropped = 0

    def start(self):
        if self.thread and self.thread.is_alive():
            return
        self.running.set()
        self.thread = threading.Thread(
            target=self._run,
            name="LISM-steering-pairer",
            daemon=True,
        )
        self.thread.start()

    def submit(self, measurement):
        try:
            self.queue.put_nowait(measurement)
        except queue.Full:
            self.dropped += 1

    def _run(self):
        try:
            while self.running.is_set():
                try:
                    measurement = self.queue.get(timeout=0.1)
                except queue.Empty:
                    continue
                result = self.pairer.update(measurement)
                if result is not None:
                    with self.lock:
                        self.latest = result
        except Exception as exc:
            with self.lock:
                self.error = str(exc)
            self.running.clear()

    def snapshot(self):
        with self.lock:
            result = self.latest
            error = self.error
        latest = None
        if result is not None:
            latest = {
                "sequence": result.sequence,
                "monotonic_us": result.monotonic_ns // 1000,
                "valid": result.valid,
                "angle_degrees": result.angle_degrees,
                "top_position_mm": result.top_position_mm,
                "bottom_position_mm": result.bottom_position_mm,
                "pair_age_us": result.pair_age_us,
                "reason": result.reason,
            }
        return {
            "state": (
                "faulted"
                if error
                else "running"
                if self.running.is_set()
                else "stopped"
            ),
            "error": error,
            "queue_depth": self.queue.qsize(),
            "dropped": self.dropped,
            "latest": latest,
        }

    def stop(self):
        self.running.clear()
        if self.thread:
            self.thread.join(1.0)
        if self.thread and self.thread.is_alive():
            raise RuntimeError("Steering coordinator did not stop")


class SensorViewer:
    def __init__(self, camera, candidate, trigger, root, steering=None):
        self.camera = camera
        self.camera_id = camera.camera_id
        self.requested = validate_settings(candidate)
        self.trigger = validate_trigger(trigger)
        self.trigger_config = TriggerConfig.from_mapping(self.trigger)
        self.quality = QualityConfig()
        self.root = Path(root)
        self.steering = steering
        self.original = None
        self.observed = None
        self.latest = None
        self.history = deque(maxlen=self.trigger["history_size"])
        self.index = -1
        self.follow = True
        self.paused = 0
        self.frames = 0
        self.triggers = 0
        self.good = 0
        self.error = None
        self.state = "stopped"
        self.recorder = None
        self.session_id = None
        self.run = threading.Event()
        self.acquire = threading.Event()
        self.lock = threading.Lock()
        self.camera_lock = threading.Lock()
        self.worker = None

    def start(self):
        self.original = self.camera.configuration()
        self.apply(self.requested)
        self.session_id = f"{self.camera_id}-{uuid.uuid4().hex}"
        self.run.set()
        self.acquire.set()
        self.state = "running"
        self.worker = threading.Thread(
            target=self.loop,
            name=f"LISM-capture-{self.camera_id}",
            daemon=True,
        )
        self.worker.start()

    def apply(self, settings):
        settings = validate_settings(settings)
        with self.camera_lock:
            self.camera.stop()
            self.observed = self.camera.apply_temporary_configuration(
                st_high=settings["st_high"],
                st_low=settings["st_low"],
                edge_delay=settings["edge_delay"],
                adc_gain=settings["gain"],
                adc_bias=settings["bias"],
            )
            self.camera.start()
        self.requested = settings
        return self.observed

    def loop(self):
        while self.run.is_set():
            if not self.acquire.wait(0.1):
                continue
            try:
                with self.camera_lock:
                    frame = self.camera.read_frame()
                monotonic_ns = time.monotonic_ns()
                timestamp = monotonic_ns * 1e-9
                result = detect_frame(
                    frame,
                    self.trigger_config,
                    self.quality,
                )
                region = result.selected_region
                with self.lock:
                    self.frames += 1
                    frame_number = self.frames
                    if region is not None:
                        event = Event(
                            frame_number,
                            timestamp,
                            frame.copy(),
                            region_payload(region),
                            result,
                        )
                        self.latest = event
                        self.triggers += 1
                        if region.measurement_quality:
                            self.good += 1
                        if self.follow:
                            self.history.append(event)
                            self.index = len(self.history) - 1
                        else:
                            self.paused += 1
                    recorder = self.recorder

                if self.steering is not None:
                    self.steering.submit(
                        measurement_from_detection(
                            self.camera_id,
                            frame_number,
                            monotonic_ns,
                            result,
                        )
                    )

                if recorder:
                    recorder.submit(
                        frame_number,
                        timestamp,
                        frame,
                        result,
                    )
            except Exception as exc:
                with self.lock:
                    self.error = str(exc)
                    self.state = "faulted"
                self.run.clear()
                self.acquire.clear()

    def browse(self, direction):
        with self.lock:
            if not self.history:
                raise ValueError("No triggered frames")
            self.follow = False
            self.index = max(
                0,
                min(len(self.history) - 1, self.index + direction),
            )
            return {"ok": True}

    def follow_latest(self, enabled):
        with self.lock:
            if (
                enabled
                and not self.follow
                and self.latest
                and (
                    not self.history
                    or self.history[-1].number != self.latest.number
                )
            ):
                self.history.append(self.latest)
                self.index = len(self.history) - 1
            self.follow = bool(enabled)
            if enabled:
                self.paused = 0
        return {"ok": True}

    def record_start(self, label):
        with self.lock:
            if self.recorder:
                raise ValueError("Recording already active")
            manifest = {
                "camera_id": self.camera_id,
                "camera_serial": self.camera.serial,
                "session_id": self.session_id,
                "camera_configuration": self.observed,
                "trigger_configuration": self.trigger,
                "quality_configuration": asdict(self.quality),
            }
            self.recorder = TriggerSessionRecorder(
                self.root,
                label,
                manifest,
            )
            return {
                "ok": True,
                "directory": str(self.recorder.directory),
            }

    def record_stop(self):
        with self.lock:
            recorder = self.recorder
            self.recorder = None
        if not recorder:
            raise ValueError("No recording active")
        return {"ok": True, "directory": str(recorder.stop())}

    def snapshot(self):
        now = time.monotonic()
        with self.lock:
            event = (
                self.history[self.index]
                if self.history and self.index >= 0
                else None
            )
            return {
                "camera_id": self.camera_id,
                "serial_number": self.camera.serial,
                "reverse_pixels": self.camera.config.reverse_pixels,
                "state": self.state,
                "error": self.error,
                "frame_count": self.frames,
                "trigger_count": self.triggers,
                "good_trigger_count": self.good,
                "history_count": len(self.history),
                "history_index": self.index,
                "follow_latest": self.follow,
                "paused_trigger_count": self.paused,
                "selected_trigger": (
                    None if event is None else event.payload(now)
                ),
                "recording": (
                    None
                    if not self.recorder
                    else {
                        "directory": str(self.recorder.directory),
                        "queue_depth": self.recorder.q.qsize(),
                        "dropped": self.recorder.dropped,
                    }
                ),
            }

    def stop(self):
        with self.lock:
            recorder = self.recorder
            self.recorder = None
        if recorder:
            recorder.stop()
        self.run.clear()
        self.acquire.clear()
        if self.worker:
            self.worker.join(
                self.camera.config.wait_timeout_ms / 1000.0 + 1.0
            )
        if self.worker and self.worker.is_alive():
            raise RuntimeError(f"{self.camera_id} worker did not stop")
        with self.camera_lock:
            try:
                self.camera.stop()
            finally:
                if self.original:
                    self.camera.restore_configuration(self.original)
        self.state = "stopped"


class Manager:
    def __init__(self, viewers, steering=None):
        self.viewers = viewers
        self.steering = steering

    def snapshot(self):
        return {
            "order": list(self.viewers),
            "cameras": {
                key: viewer.snapshot()
                for key, viewer in self.viewers.items()
            },
            "steering": (
                self.steering.snapshot() if self.steering else None
            ),
        }

    def start(self):
        started = []
        steering_started = False
        try:
            if self.steering:
                self.steering.start()
                steering_started = True
            for viewer in self.viewers.values():
                viewer.start()
                started.append(viewer)
        except Exception:
            for viewer in reversed(started):
                try:
                    viewer.stop()
                except Exception:
                    pass
            if steering_started:
                try:
                    self.steering.stop()
                except Exception:
                    pass
            raise

    def stop(self):
        errors = []
        for viewer in self.viewers.values():
            try:
                viewer.stop()
            except Exception as exc:
                errors.append(str(exc))
        if self.steering:
            try:
                self.steering.stop()
            except Exception as exc:
                errors.append(str(exc))
        if errors:
            raise RuntimeError("; ".join(errors))


HTML = """<!doctype html><meta charset=utf-8><title>Dual LISM Selected Triggers</title><style>body{background:#10141b;color:#e8eef7;font-family:system-ui;padding:15px}.sensor,.steering{background:#19212d;padding:12px;border-radius:10px;margin:12px 0}canvas{width:100%;height:330px;background:#080b10}button,input{padding:7px;margin:3px}.meta{white-space:pre-wrap}</style><h1>Dual LISM Selected Trigger Viewer</h1><section class=steering><h2>Steering angle</h2><div class=meta id=steering>Waiting for a fresh pair</div></section><div id=app></div><script>let built=false;function line(x,v,c,n){if(v==null)return;let X=v/(n-1)*x.canvas.width;x.strokeStyle=c;x.beginPath();x.moveTo(X,0);x.lineTo(X,x.canvas.height);x.stroke()}function draw(id,e){let c=document.getElementById('c_'+id),x=c.getContext('2d');x.clearRect(0,0,c.width,c.height);if(!e)return;let a=e.pixels;x.strokeStyle='#43d5ff';x.beginPath();a.forEach((v,i)=>{let X=i/(a.length-1)*c.width,Y=c.height-v/65535*c.height;i?x.lineTo(X,Y):x.moveTo(X,Y)});x.stroke();line(x,e.selected_center,'#64d98b',a.length);line(x,e.centroid,'#ff6b6b',a.length);line(x,e.quantile_center,'#f6c85f',a.length);line(x,e.deep_core_center,'#bc8cff',a.length);line(x,e.boundary_center,'#4da3ff',a.length)}async function post(id,p,b={}){await fetch('/api/'+id+p,{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(b)})}function build(o){app.innerHTML=o.map((id,i)=>`<section class=sensor><h2>${i?'Bottom':'Top'} sensor: ${id}</h2><button onclick="post('${id}','/browse',{direction:-1})">Previous</button><button onclick="post('${id}','/browse',{direction:1})">Next</button><button onclick="post('${id}','/follow',{enabled:false})">Pause</button><button onclick="post('${id}','/follow',{enabled:true})">Latest / Follow Live</button><input id=l_${id} value=center><button onclick="post('${id}','/record/start',{label:document.getElementById('l_${id}').value})">Record</button><button onclick="post('${id}','/record/stop')">Stop recording</button><div class=meta id=m_${id}></div><canvas id=c_${id} width=1400 height=330></canvas></section>`).join('');built=true}async function tick(){let d=await(await fetch('/api/state',{cache:'no-store'})).json();if(!built)build(d.order);let s=d.steering&&d.steering.latest;document.getElementById('steering').textContent=s?`valid=${s.valid} angle=${s.angle_degrees==null?'n/a':s.angle_degrees.toFixed(4)+' deg'} top=${s.top_position_mm==null?'n/a':s.top_position_mm.toFixed(3)+' mm'} bottom=${s.bottom_position_mm==null?'n/a':s.bottom_position_mm.toFixed(3)+' mm'} pair_age=${s.pair_age_us} us reason=${s.reason}`:'Waiting for a fresh pair';d.order.forEach(id=>{let v=d.cameras[id],e=v.selected_trigger;draw(id,e);document.getElementById('m_'+id).textContent=`serial=${v.serial_number} reversed=${v.reverse_pixels} state=${v.state} frames=${v.frame_count} triggers=${v.trigger_count} good=${v.good_trigger_count} history=${v.history_count} ${v.follow_latest?'FOLLOWING':'PAUSED'}\nselected=${e?e.selected_center:'n/a'} method=${e?e.selected_center_method:'n/a'} confidence=${e?e.center_confidence:'n/a'} error=${v.error||'none'}`});setTimeout(tick,100)}tick()</script>"""


def serve(manager, host, port):
    class Handler(BaseHTTPRequestHandler):
        def send_json(self, value, code=200):
            body = json.dumps(value, allow_nan=False).encode()
            self.send_response(code)
            self.send_header("Content-Type", "application/json")
            self.send_header("Cache-Control", "no-store")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def body(self):
            return json.loads(
                self.rfile.read(
                    int(self.headers.get("Content-Length", "0"))
                )
                or b"{}"
            )

        def do_GET(self):
            if self.path == "/api/state":
                self.send_json(manager.snapshot())
            elif self.path == "/":
                body = HTML.encode()
                self.send_response(200)
                self.send_header("Content-Type", "text/html; charset=utf-8")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)
            else:
                self.send_error(404)

        def do_POST(self):
            try:
                _, api, camera_id, *rest = self.path.split("/")
                if api != "api":
                    raise KeyError("Invalid API path")
                viewer = manager.viewers[camera_id]
                path = "/" + "/".join(rest)
                body = self.body()
                routes = {
                    "/browse": lambda: viewer.browse(
                        int(body["direction"])
                    ),
                    "/follow": lambda: viewer.follow_latest(
                        bool(body["enabled"])
                    ),
                    "/record/start": lambda: viewer.record_start(
                        str(body["label"])
                    ),
                    "/record/stop": viewer.record_stop,
                }
                self.send_json(routes[path]())
            except Exception as exc:
                self.send_json({"error": str(exc)}, 400)

        def log_message(self, *_):
            pass

    server = ThreadingHTTPServer((host, port), Handler)
    print(f"Open http://{host}:{port}")
    try:
        server.serve_forever(0.2)
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()


def main(argv=None):
    from .camera import LismCamera
    from .config import Config
    from .sdk_session import LismSdkSession

    parser = argparse.ArgumentParser()
    parser.add_argument("--config", default="config/default.json")
    parser.add_argument("--bias", type=int, default=282)
    parser.add_argument("--gain", type=int, default=15)
    parser.add_argument("--st-high", type=int, default=9000)
    parser.add_argument("--st-low", type=int, default=100)
    parser.add_argument("--edge-delay", type=int, default=88)
    parser.add_argument("--threshold", type=int, default=60000)
    parser.add_argument("--minimum-width", type=int, default=2)
    parser.add_argument("--maximum-width", type=int, default=512)
    parser.add_argument(
        "--minimum-integrated-drop",
        type=float,
        default=10000,
    )
    parser.add_argument("--maximum-gap", type=int, default=3)
    parser.add_argument("--edge-window", type=int, default=5)
    parser.add_argument("--edge-minimum-active", type=int, default=3)
    parser.add_argument("--quantile-low", type=float, default=0.1)
    parser.add_argument("--quantile-high", type=float, default=0.9)
    parser.add_argument("--core-fraction", type=float, default=0.8)
    parser.add_argument("--core-minimum-width", type=int, default=2)
    parser.add_argument(
        "--maximum-center-disagreement",
        type=float,
        default=12,
    )
    parser.add_argument("--minimum-active-pixels", type=int, default=12)
    parser.add_argument(
        "--minimum-active-coverage",
        type=float,
        default=0.35,
    )
    parser.add_argument("--maximum-internal-gap", type=int, default=8)
    parser.add_argument(
        "--maximum-internal-gap-fraction",
        type=float,
        default=0.25,
    )
    parser.add_argument("--minimum-sustained-span", type=int, default=12)
    parser.add_argument("--history-size", type=int, default=128)
    parser.add_argument("--recording-root", default="trigger_sessions")
    parser.add_argument("--host")
    parser.add_argument("--port", type=int)
    args = parser.parse_args(argv)

    config, base = Config.load(args.config)
    if len(config.cameras) != 2:
        raise ValueError("Exactly two configured cameras are required")

    top_camera, bottom_camera = config.steering_cameras()
    top_id = top_camera.camera_id
    bottom_id = bottom_camera.camera_id
    candidate = {
        "bias": args.bias,
        "gain": args.gain,
        "st_high": args.st_high,
        "st_low": args.st_low,
        "edge_delay": args.edge_delay,
    }
    trigger = {
        "threshold": args.threshold,
        "minimum_width": args.minimum_width,
        "maximum_width": args.maximum_width,
        "minimum_integrated_drop": args.minimum_integrated_drop,
        "maximum_gap": args.maximum_gap,
        "edge_window": args.edge_window,
        "edge_minimum_active": args.edge_minimum_active,
        "quantile_low": args.quantile_low,
        "quantile_high": args.quantile_high,
        "core_fraction": args.core_fraction,
        "core_minimum_width": args.core_minimum_width,
        "maximum_center_disagreement": args.maximum_center_disagreement,
        "minimum_active_pixels": args.minimum_active_pixels,
        "minimum_active_coverage": args.minimum_active_coverage,
        "maximum_internal_gap": args.maximum_internal_gap,
        "maximum_internal_gap_fraction": (
            args.maximum_internal_gap_fraction
        ),
        "minimum_sustained_span": args.minimum_sustained_span,
        "history_size": args.history_size,
    }

    pairer = SteeringAnglePairer(
        SensorCalibration(
            top_id,
            top_camera.millimeters_per_pixel,
            top_camera.zero_pixel,
        ),
        SensorCalibration(
            bottom_id,
            bottom_camera.millimeters_per_pixel,
            bottom_camera.zero_pixel,
        ),
        sensor_separation_mm=config.steering.sensor_separation_mm,
        maximum_pair_age_us=config.steering.maximum_pair_age_us,
    )
    steering = SteeringCoordinator(pairer)
    session = LismSdkSession(config, base)
    cameras = []
    manager = None

    try:
        session.open()
        for camera_config in config.cameras:
            camera = LismCamera.from_shared_device(
                session,
                camera_config,
            )
            camera.open()
            cameras.append(camera)

        manager = Manager(
            {
                camera.camera_id: SensorViewer(
                    camera,
                    candidate,
                    trigger,
                    args.recording_root,
                    steering,
                )
                for camera in cameras
            },
            steering,
        )
        manager.start()
        serve(
            manager,
            args.host or config.host,
            args.port or config.port,
        )
    finally:
        if manager:
            try:
                manager.stop()
            except Exception as exc:
                print("viewer stop:", exc)
        for camera in cameras:
            try:
                camera.close()
            except Exception as exc:
                print("camera close:", exc)
        session.close()


if __name__ == "__main__":
    main()
