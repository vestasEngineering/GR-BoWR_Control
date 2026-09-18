from __future__ import annotations

import argparse
import json
import threading
import time
from collections import deque
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import numpy as np

from .camera import LismCamera
from .config import Config


def lockin_profile(frames: np.ndarray, timestamps: np.ndarray, frequency_hz: float) -> dict:
    frames = np.asarray(frames, dtype=np.float64)
    timestamps = np.asarray(timestamps, dtype=np.float64)
    if frames.ndim != 2 or frames.shape[0] < 8:
        raise ValueError("At least eight 2D frames are required")
    if timestamps.shape != (frames.shape[0],):
        raise ValueError("Timestamp count must match frame count")

    relative = timestamps - timestamps[0]
    centered = frames - np.mean(frames, axis=0, keepdims=True)
    window = np.hanning(frames.shape[0])
    phase = 2.0 * np.pi * frequency_hz * relative
    reference = window * np.exp(-1j * phase)
    normalization = 2.0 / max(float(window.sum()), 1.0)
    complex_response = normalization * np.sum(centered * reference[:, None], axis=0)
    amplitude = np.abs(complex_response)
    phase_degrees = np.degrees(np.angle(complex_response))

    frame_energy = np.sum(centered, axis=1)
    global_response = normalization * np.sum(frame_energy * reference)
    global_phase = float(np.degrees(np.angle(global_response)))
    phase_error = np.angle(np.exp(1j * np.radians(phase_degrees - global_phase)))
    coherent_amplitude = amplitude * np.maximum(np.cos(phase_error), 0.0)

    return {
        "amplitude": amplitude,
        "phase_degrees": phase_degrees,
        "coherent_amplitude": coherent_amplitude,
        "global_amplitude": float(abs(global_response)),
        "global_phase_degrees": global_phase,
    }


def select_band(signal: np.ndarray, threshold: float, minimum_width: int = 2) -> dict | None:
    signal = np.asarray(signal, dtype=np.float64)
    active = signal > threshold
    changes = np.diff(np.pad(active.astype(np.int8), (1, 1)))
    starts = np.flatnonzero(changes == 1)
    ends = np.flatnonzero(changes == -1) - 1
    regions = [(int(start), int(end)) for start, end in zip(starts, ends) if end - start + 1 >= minimum_width]
    if not regions:
        return None
    start, end = max(regions, key=lambda region: float(signal[region[0]:region[1] + 1].sum()))
    weights = signal[start:end + 1]
    total = float(weights.sum())
    return {
        "start": start,
        "end": end,
        "width": end - start + 1,
        "centroid": float(np.dot(np.arange(start, end + 1), weights) / total) if total > 0 else None,
        "peak_pixel": start + int(np.argmax(weights)),
        "peak_amplitude": float(weights.max()),
        "integrated_amplitude": total,
        "concentration": float(total / max(float(signal.sum()), 1.0)),
    }


def analyze_lockin(
    off_frames: np.ndarray,
    off_timestamps: np.ndarray,
    recent_frames: np.ndarray,
    recent_timestamps: np.ndarray,
    frequency_hz: float,
    threshold_sigma: float = 6.0,
) -> dict:
    off = lockin_profile(off_frames, off_timestamps, frequency_hz)
    current = lockin_profile(recent_frames, recent_timestamps, frequency_hz)
    off_amplitude = off["coherent_amplitude"]
    current_amplitude = current["coherent_amplitude"]
    excess = np.maximum(current_amplitude - off_amplitude, 0.0)
    median = float(np.median(off_amplitude))
    mad = float(1.4826 * np.median(np.abs(off_amplitude - median)))
    threshold = median + threshold_sigma * max(mad, 1.0)
    band = select_band(excess, max(threshold - median, 1.0))
    return {
        "off_amplitude": off_amplitude,
        "current_amplitude": current_amplitude,
        "excess_amplitude": excess,
        "phase_degrees": current["phase_degrees"],
        "off_global_amplitude": off["global_amplitude"],
        "current_global_amplitude": current["global_amplitude"],
        "current_global_phase_degrees": current["global_phase_degrees"],
        "off_amplitude_median": median,
        "off_amplitude_mad": mad,
        "excess_threshold": max(threshold - median, 1.0),
        "band": band,
    }


class LockinViewer:
    def __init__(self, camera, candidate: dict, frequency_hz: float, off_frames: int, history_frames: int):
        self.camera = camera
        self.candidate = candidate
        self.frequency_hz = frequency_hz
        self.off_frame_count = off_frames
        self.history = deque(maxlen=history_frames)
        self.off_frames = None
        self.off_timestamps = None
        self.original = None
        self.error = None
        self.running = threading.Event()
        self.lock = threading.Lock()
        self.worker = None

    def _capture_count(self, count: int) -> tuple[np.ndarray, np.ndarray]:
        frames = []
        timestamps = []
        for _ in range(count):
            frame = self.camera.read_frame()
            timestamps.append(time.monotonic_ns() * 1e-9)
            frames.append(frame)
        return np.stack(frames), np.asarray(timestamps)

    def start(self) -> None:
        self.original = self.camera.configuration()
        self.camera.apply_temporary_configuration(
            st_high=self.candidate["st_high"],
            st_low=self.candidate["st_low"],
            edge_delay=self.candidate["edge_delay"],
            adc_gain=self.candidate["gain"],
            adc_bias=self.candidate["bias"],
        )
        input("Disable the LAP scan for the lock-in reference, wait for stability, then press Enter: ")
        self.camera.start()
        for _ in range(32):
            self.camera.read_frame()
        self.off_frames, self.off_timestamps = self._capture_count(self.off_frame_count)
        self.running.set()
        self.worker = threading.Thread(target=self._capture_loop, name="lockin-scan-viewer", daemon=True)
        self.worker.start()

    def _capture_loop(self) -> None:
        while self.running.is_set():
            try:
                frame = self.camera.read_frame()
                timestamp = time.monotonic_ns() * 1e-9
                with self.lock:
                    self.history.append((timestamp, frame))
                    self.error = None
            except Exception as exc:
                with self.lock:
                    self.error = str(exc)
                self.running.clear()

    def reset_reference(self) -> None:
        with self.lock:
            recent = list(self.history)[-self.off_frame_count:]
        if len(recent) < self.off_frame_count:
            raise RuntimeError(f"Need {self.off_frame_count} recent frames before resetting reference")
        self.off_timestamps = np.asarray([item[0] for item in recent])
        self.off_frames = np.stack([item[1] for item in recent])

    def snapshot(self) -> dict:
        with self.lock:
            recent = list(self.history)
            error = self.error
        if self.off_frames is None or len(recent) < 16:
            return {"ready": False, "frames": len(recent), "error": error, "configuration": self.candidate}
        timestamps = np.asarray([item[0] for item in recent])
        frames = np.stack([item[1] for item in recent])
        analysis = analyze_lockin(
            self.off_frames,
            self.off_timestamps,
            frames,
            timestamps,
            self.frequency_hz,
        )

        baseline_median = np.median(self.off_frames, axis=0)
        heat_source = frames - baseline_median
        display_rows = min(96, heat_source.shape[0])
        if heat_source.shape[0] > display_rows:
            indexes = np.linspace(0, heat_source.shape[0] - 1, display_rows).astype(int)
            heat_source = heat_source[indexes]
        heat_scale = float(max(np.percentile(np.abs(heat_source), 99), 1.0))
        heatmap = np.clip((heat_source / heat_scale + 1.0) * 127.5, 0, 255).astype(np.uint8)

        return {
            "ready": True,
            "error": error,
            "configuration": self.candidate,
            "frequency_hz": self.frequency_hz,
            "period_ms": 1000.0 / self.frequency_hz,
            "frames": len(recent),
            "duration_seconds": float(timestamps[-1] - timestamps[0]),
            "measured_frame_rate_hz": float(1.0 / np.median(np.diff(timestamps))),
            "off_amplitude": analysis["off_amplitude"].tolist(),
            "current_amplitude": analysis["current_amplitude"].tolist(),
            "excess_amplitude": analysis["excess_amplitude"].tolist(),
            "phase_degrees": analysis["phase_degrees"].tolist(),
            "off_global_amplitude": analysis["off_global_amplitude"],
            "current_global_amplitude": analysis["current_global_amplitude"],
            "current_global_phase_degrees": analysis["current_global_phase_degrees"],
            "excess_threshold": analysis["excess_threshold"],
            "band": analysis["band"],
            "heatmap": heatmap.tolist(),
            "heat_scale": heat_scale,
            "low_clip_fraction": float(np.mean(frames <= 32)),
            "high_clip_fraction": float(np.mean(frames >= 65503)),
        }

    def stop(self) -> None:
        self.running.clear()
        if self.worker is not None:
            self.worker.join(timeout=2.0)
        try:
            self.camera.stop()
        finally:
            if self.original is not None:
                self.camera.restore_configuration(self.original)


HTML = r'''<!doctype html><html><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>LISM 49.63 Hz Lock-in Viewer</title><style>
body{font-family:system-ui;background:#10141b;color:#e8eef7;margin:0;padding:18px}.cards{display:grid;grid-template-columns:repeat(auto-fit,minmax(170px,1fr));gap:10px;margin:12px 0}.card{background:#19212d;padding:12px;border-radius:8px}canvas{width:100%;height:260px;background:#080b10;border-radius:8px;margin:7px 0}#heat{image-rendering:pixelated}.bad{color:#ff6b6b}button{padding:8px 14px}</style></head><body>
<h1>LISM 49.63 Hz Scan-Synchronous Viewer</h1><p>This view rejects unrelated frequencies and displays only modulation coherent with the measured LAP frequency. Capture the reference with the scan OFF, then enable the normal scan without moving the rig.</p><button onclick="resetRef()">Set current scan-OFF frames as reference</button><div id="cards" class="cards"></div>
<h2>49.63 Hz excess coherent amplitude</h2><canvas id="excess" width="1200" height="260"></canvas>
<h2>Current coherent amplitude (cyan) and scan-OFF amplitude (yellow)</h2><canvas id="amp" width="1200" height="260"></canvas>
<h2>49.63 Hz phase by pixel</h2><canvas id="phase" width="1200" height="260"></canvas>
<h2>Raw pixel-versus-time heatmap</h2><canvas id="heat" width="1024" height="240"></canvas>
<script>
function plot(id,series,lo,hi){let c=document.getElementById(id),x=c.getContext('2d');x.clearRect(0,0,c.width,c.height);let all=series.flatMap(s=>s.d);if(lo===null)lo=Math.min(...all);if(hi===null)hi=Math.max(...all,lo+1);series.forEach(s=>{x.strokeStyle=s.c;x.lineWidth=1.4;x.beginPath();s.d.forEach((v,i)=>{let X=i*(c.width-1)/(s.d.length-1),Y=c.height-1-(v-lo)*(c.height-2)/(hi-lo);i?x.lineTo(X,Y):x.moveTo(X,Y)});x.stroke()})}
function heat(rows){let c=document.getElementById('heat'),x=c.getContext('2d'),h=rows.length,w=rows[0].length,img=x.createImageData(w,h);for(let y=0;y<h;y++)for(let i=0;i<w;i++){let v=rows[y][i],p=(y*w+i)*4;img.data[p]=v>128?(v-128)*2:0;img.data[p+1]=Math.max(0,255-Math.abs(v-128)*2);img.data[p+2]=v<128?(128-v)*2:0;img.data[p+3]=255}let t=document.createElement('canvas');t.width=w;t.height=h;t.getContext('2d').putImageData(img,0,0);x.imageSmoothingEnabled=false;x.clearRect(0,0,c.width,c.height);x.drawImage(t,0,0,c.width,c.height)}
async function resetRef(){await fetch('/api/reset-reference',{method:'POST'})}
async function tick(){try{let s=await(await fetch('/api/status')).json();if(s.ready){plot('excess',[{d:s.excess_amplitude,c:'#64d98b'}],0,null);plot('amp',[{d:s.current_amplitude,c:'#43d5ff'},{d:s.off_amplitude,c:'#f6c85f'}],0,null);plot('phase',[{d:s.phase_degrees,c:'#d976ff'}],-180,180);heat(s.heatmap);let b=s.band||{};let ratio=s.off_global_amplitude? s.current_global_amplitude/s.off_global_amplitude:0;cards.innerHTML=`<div class=card>Target<br><b>${s.frequency_hz.toFixed(4)} Hz (${s.period_ms.toFixed(3)} ms)</b></div><div class=card>Camera rate<br><b>${s.measured_frame_rate_hz.toFixed(2)} fps</b></div><div class=card>Window<br><b>${s.frames} frames, ${s.duration_seconds.toFixed(3)} s</b></div><div class=card>Global amplitude ratio<br><b>${ratio.toFixed(2)}x</b></div><div class=card>Band<br><b>${b.start??'-'} to ${b.end??'-'} (${b.width??0})</b></div><div class=card>Centroid<br><b>${b.centroid?.toFixed(2)??'-'}</b></div><div class=card>Concentration<br><b>${b.concentration?(100*b.concentration).toFixed(1)+'%':'-'}</b></div><div class=card>Phase<br><b>${s.current_global_phase_degrees.toFixed(1)} deg</b></div><div class=card>Clipping<br><b class=${s.low_clip_fraction>.01||s.high_clip_fraction>.01?'bad':''}>low ${(100*s.low_clip_fraction).toFixed(2)}%, high ${(100*s.high_clip_fraction).toFixed(2)}%</b></div>`}}catch(e){}setTimeout(tick,250)}tick()
</script></body></html>'''


def serve(viewer: LockinViewer, host: str, port: int) -> None:
    class Handler(BaseHTTPRequestHandler):
        def send_json(self, value, status=200):
            payload = json.dumps(value, allow_nan=False).encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)

        def do_GET(self):
            if self.path == "/api/status":
                self.send_json(viewer.snapshot())
            elif self.path in ("/", "/index.html"):
                payload = HTML.encode("utf-8")
                self.send_response(200)
                self.send_header("Content-Type", "text/html; charset=utf-8")
                self.send_header("Content-Length", str(len(payload)))
                self.end_headers()
                self.wfile.write(payload)
            else:
                self.send_error(404)

        def do_POST(self):
            if self.path != "/api/reset-reference":
                self.send_error(404)
                return
            try:
                viewer.reset_reference()
                self.send_json({"ok": True})
            except Exception as exc:
                self.send_json({"ok": False, "error": str(exc)}, 409)

        def log_message(self, *_):
            return

    server = ThreadingHTTPServer((host, port), Handler)
    try:
        print(f"Open http://{host}:{port} or forward port {port} in VS Code")
        server.serve_forever(poll_interval=0.2)
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()


def main(argv=None) -> None:
    parser = argparse.ArgumentParser(description="Scan-synchronous visualization centered on the measured LAP modulation")
    parser.add_argument("--config", default="config/default.json")
    parser.add_argument("--frequency", type=float, default=49.6318)
    parser.add_argument("--bias", type=int, default=256)
    parser.add_argument("--gain", type=int, default=4)
    parser.add_argument("--st-high", type=int, default=9900)
    parser.add_argument("--st-low", type=int, default=100)
    parser.add_argument("--edge-delay", type=int, default=88)
    parser.add_argument("--off-frames", type=int, default=512)
    parser.add_argument("--history-frames", type=int, default=512)
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8768)
    args = parser.parse_args(argv)
    if args.frequency <= 0 or args.off_frames < 16 or args.history_frames < 16:
        parser.error("frequency must be positive and frame counts must be at least 16")

    config, base = Config.load(args.config)
    candidate = {
        "bias": args.bias,
        "gain": args.gain,
        "st_high": args.st_high,
        "st_low": args.st_low,
        "edge_delay": args.edge_delay,
    }
    with LismCamera(config, base) as camera:
        viewer = LockinViewer(camera, candidate, args.frequency, args.off_frames, args.history_frames)
        viewer.start()
        try:
            serve(viewer, args.host, args.port)
        finally:
            viewer.stop()


if __name__ == "__main__":
    main()
