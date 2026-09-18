from __future__ import annotations

import argparse
import json
import threading
from collections import deque
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import numpy as np

from .camera import LismCamera
from .config import Config


class LiveViewer:
    def __init__(self, camera, candidate, baseline_frames=64, rolling_frames=8):
        self.camera = camera
        self.candidate = candidate
        self.baseline_frames = baseline_frames
        self.frames = deque(maxlen=rolling_frames)
        self.original = None
        self.baseline = None
        self.latest = None
        self.error = None
        self.running = threading.Event()
        self.lock = threading.Lock()
        self.worker = None

    def start(self):
        self.original = self.camera.configuration()
        self.camera.apply_temporary_configuration(
            st_high=self.candidate["st_high"],
            st_low=self.candidate["st_low"],
            edge_delay=self.candidate["edge_delay"],
            adc_gain=self.candidate["gain"],
            adc_bias=self.candidate["bias"],
        )
        input("Laser OFF for baseline. Wait for stability, then press Enter: ")
        self.camera.start()
        baseline_frames = self.camera.capture_frames(self.baseline_frames, settle=32)
        self.baseline = np.median(baseline_frames, axis=0)
        self.running.set()
        self.worker = threading.Thread(target=self._acquire, daemon=True)
        self.worker.start()

    def _acquire(self):
        while self.running.is_set():
            try:
                frame = self.camera.read_frame()
                with self.lock:
                    self.latest = frame
                    self.frames.append(frame)
                    self.error = None
            except Exception as exc:
                with self.lock:
                    self.error = str(exc)
                self.running.clear()

    def snapshot(self):
        with self.lock:
            latest = None if self.latest is None else self.latest.copy()
            rolling = None if not self.frames else np.mean(np.stack(tuple(self.frames)), axis=0)
            error = self.error
        if latest is None or rolling is None or self.baseline is None:
            return {"ready": False, "error": error, "configuration": self.candidate}
        signed = rolling - self.baseline
        positive = np.maximum(signed, 0.0)
        negative = np.maximum(-signed, 0.0)
        polarity = "negative" if float(negative.max()) > float(positive.max()) else "positive"
        signal = negative if polarity == "negative" else positive
        peak = int(np.argmax(signal))
        return {
            "ready": True,
            "error": error,
            "configuration": self.candidate,
            "raw": latest.tolist(),
            "rolling": rolling.tolist(),
            "baseline": self.baseline.tolist(),
            "signal": signal.tolist(),
            "polarity": polarity,
            "peak_pixel": peak,
            "peak_signal": float(signal[peak]),
            "raw_min": float(latest.min()),
            "raw_max": float(latest.max()),
            "low_clip": bool(np.any(latest <= 32)),
            "high_clip": bool(np.any(latest >= 65503)),
            "rolling_frame_count": len(self.frames),
        }

    def stop(self):
        self.running.clear()
        if self.worker is not None:
            self.worker.join(timeout=2.0)
        try:
            self.camera.stop()
        finally:
            if self.original is not None:
                self.camera.restore_configuration(self.original)


HTML = '''<!doctype html><html><head><meta charset="utf-8"><title>LISM Viewer</title><style>
body{font-family:system-ui;background:#10141b;color:#e8eef7;padding:18px}.cards{display:grid;grid-template-columns:repeat(auto-fit,minmax(170px,1fr));gap:10px}.card{background:#19212d;padding:12px;border-radius:8px}canvas{width:100%;height:280px;background:#080b10;margin:8px 0}.bad{color:#ff6b6b}
</style></head><body><h1>LISM Promising Configuration Viewer</h1><p>Baseline was captured with laser OFF. Switch the fixed laser ON without moving the rig.</p><div id="cards" class="cards"></div><h2>Raw instantaneous</h2><canvas id="raw" width="1200" height="280"></canvas><h2>Rolling mean (cyan) and OFF baseline (yellow)</h2><canvas id="mean" width="1200" height="280"></canvas><h2>Polarity-corrected difference</h2><canvas id="sig" width="1200" height="280"></canvas><script>
function plot(id,ss){let c=document.getElementById(id),x=c.getContext('2d');x.clearRect(0,0,c.width,c.height);let a=ss.flatMap(s=>s.d),lo=Math.min(...a),hi=Math.max(...a);if(hi<=lo)hi=lo+1;ss.forEach(s=>{x.strokeStyle=s.c;x.beginPath();s.d.forEach((v,i)=>{let X=i*(c.width-1)/(s.d.length-1),Y=c.height-1-(v-lo)*(c.height-2)/(hi-lo);i?x.lineTo(X,Y):x.moveTo(X,Y)});x.stroke()})}
async function tick(){try{let s=await(await fetch('/api/status')).json();if(s.ready){plot('raw',[{d:s.raw,c:'#43d5ff'}]);plot('mean',[{d:s.baseline,c:'#f6c85f'},{d:s.rolling,c:'#43d5ff'}]);plot('sig',[{d:s.signal,c:'#64d98b'}]);let q=s.configuration;cards.innerHTML=`<div class=card>Config<br><b>bias ${q.bias}, gain ${q.gain}, ST ${q.st_high}/${q.st_low}</b></div><div class=card>Polarity<br><b>${s.polarity}</b></div><div class=card>Peak<br><b>${s.peak_pixel} (${s.peak_signal.toFixed(1)})</b></div><div class=card>Raw range<br><b>${s.raw_min.toFixed(0)} to ${s.raw_max.toFixed(0)}</b></div><div class=card>Clipping<br><b class=${s.low_clip||s.high_clip?'bad':''}>low ${s.low_clip}, high ${s.high_clip}</b></div><div class=card>Rolling frames<br><b>${s.rolling_frame_count}</b></div>`}}catch(e){}setTimeout(tick,100)}tick()
</script></body></html>'''


def serve(viewer, host, port):
    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            if self.path == "/api/status":
                payload = json.dumps(viewer.snapshot(), allow_nan=False).encode()
                content_type = "application/json"
            elif self.path in ("/", "/index.html"):
                payload = HTML.encode()
                content_type = "text/html; charset=utf-8"
            else:
                self.send_error(404)
                return
            self.send_response(200)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)

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


def main(argv=None):
    parser = argparse.ArgumentParser(description="Visualize one temporary LISM configuration")
    parser.add_argument("--config", default="config/default.json")
    parser.add_argument("--bias", type=int, default=256)
    parser.add_argument("--gain", type=int, default=4)
    parser.add_argument("--st-high", type=int, default=5000)
    parser.add_argument("--st-low", type=int, default=100)
    parser.add_argument("--edge-delay", type=int, default=88)
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8766)
    parser.add_argument("--baseline-frames", type=int, default=64)
    parser.add_argument("--rolling-frames", type=int, default=8)
    args = parser.parse_args(argv)
    config, base = Config.load(args.config)
    candidate = {"bias": args.bias, "gain": args.gain, "st_high": args.st_high, "st_low": args.st_low, "edge_delay": args.edge_delay}
    with LismCamera(config, base) as camera:
        viewer = LiveViewer(camera, candidate, args.baseline_frames, args.rolling_frames)
        viewer.start()
        try:
            serve(viewer, args.host, args.port)
        finally:
            viewer.stop()


if __name__ == "__main__":
    main()
