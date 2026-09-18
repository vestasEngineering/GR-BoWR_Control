from __future__ import annotations

import argparse
import json
import threading
from collections import deque
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import numpy as np

from .camera import LismCamera
from .config import Config


def robust_noise(frames: np.ndarray) -> np.ndarray:
    median = np.median(frames, axis=0)
    return np.maximum(
        1.4826 * np.median(np.abs(frames - median), axis=0),
        1.0,
    )


def contiguous_band(signal: np.ndarray, threshold: np.ndarray, minimum_width: int = 2) -> dict | None:
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
        "integrated_signal": total,
        "peak_pixel": start + int(np.argmax(weights)),
        "peak_signal": float(weights.max()),
        "concentration": float(total / max(float(signal.sum()), 1.0)),
    }


def scanning_metrics(baseline: np.ndarray, recent: np.ndarray, noise_sigma: float, minimum_signal: float) -> dict:
    baseline_median = np.median(baseline, axis=0)
    baseline_noise = robust_noise(baseline)
    recent_median = np.median(recent, axis=0)
    recent_noise = robust_noise(recent)

    positive_mean = np.maximum(recent_median - baseline_median, 0.0)
    negative_mean = np.maximum(baseline_median - recent_median, 0.0)
    excess_amplitude = np.maximum(recent_noise - baseline_noise, 0.0)

    baseline_p99 = np.percentile(baseline, 99, axis=0)
    recent_p99 = np.percentile(recent, 99, axis=0)
    percentile_response = np.maximum(recent_p99 - baseline_p99, 0.0)

    upper_threshold = baseline_median + np.maximum(noise_sigma * baseline_noise, minimum_signal)
    lower_threshold = baseline_median - np.maximum(noise_sigma * baseline_noise, minimum_signal)
    hit_rate = np.mean((recent > upper_threshold) | (recent < lower_threshold), axis=0)

    amplitude_threshold = np.maximum(0.5 * baseline_noise, minimum_signal)
    percentile_threshold = np.maximum(noise_sigma * baseline_noise, minimum_signal)
    band = contiguous_band(excess_amplitude, amplitude_threshold)

    return {
        "baseline_median": baseline_median,
        "recent_median": recent_median,
        "baseline_noise": baseline_noise,
        "recent_noise": recent_noise,
        "positive_mean": positive_mean,
        "negative_mean": negative_mean,
        "excess_amplitude": excess_amplitude,
        "percentile_response": percentile_response,
        "hit_rate": hit_rate,
        "amplitude_threshold": amplitude_threshold,
        "percentile_threshold": percentile_threshold,
        "band": band,
        "median_baseline_noise": float(np.median(baseline_noise)),
        "median_recent_noise": float(np.median(recent_noise)),
        "maximum_hit_rate": float(hit_rate.max()),
        "maximum_percentile_response": float(percentile_response.max()),
    }


class ScanningLaserDiagnostic:
    def __init__(self, camera, candidate: dict, baseline_frames: int, history_frames: int, analysis_frames: int):
        self.camera = camera
        self.candidate = candidate
        self.baseline_frames = baseline_frames
        self.analysis_frames = analysis_frames
        self.history = deque(maxlen=history_frames)
        self.original = None
        self.baseline = None
        self.error = None
        self.running = threading.Event()
        self.lock = threading.Lock()
        self.worker = None

    def start(self) -> None:
        self.original = self.camera.configuration()
        self.camera.apply_temporary_configuration(
            st_high=self.candidate["st_high"],
            st_low=self.candidate["st_low"],
            edge_delay=self.candidate["edge_delay"],
            adc_gain=self.candidate["gain"],
            adc_bias=self.candidate["bias"],
        )
        input("Laser OFF for reference capture. Wait for stability, then press Enter: ")
        self.camera.start()
        self.baseline = self.camera.capture_frames(self.baseline_frames, settle=32)
        self.running.set()
        self.worker = threading.Thread(target=self._capture_loop, name="scanning-laser-diagnostic", daemon=True)
        self.worker.start()

    def _capture_loop(self) -> None:
        while self.running.is_set():
            try:
                frame = self.camera.read_frame()
                with self.lock:
                    self.history.append(frame)
                    self.error = None
            except Exception as exc:
                with self.lock:
                    self.error = str(exc)
                self.running.clear()

    def reset_reference(self) -> None:
        with self.lock:
            frames = list(self.history)[-self.baseline_frames:]
        if len(frames) < min(self.baseline_frames, 16):
            raise RuntimeError("Not enough recent frames to reset the reference")
        self.baseline = np.stack(frames)

    def snapshot(self) -> dict:
        with self.lock:
            history = list(self.history)
            error = self.error
        if self.baseline is None or len(history) < min(self.analysis_frames, 8):
            return {"ready": False, "error": error, "frames": len(history), "configuration": self.candidate}

        recent = np.stack(history[-self.analysis_frames:])
        metrics = scanning_metrics(self.baseline, recent, noise_sigma=3.0, minimum_signal=20.0)

        heatmap_source = recent - metrics["baseline_median"]
        heatmap_rows = min(128, heatmap_source.shape[0])
        if heatmap_source.shape[0] > heatmap_rows:
            indexes = np.linspace(0, heatmap_source.shape[0] - 1, heatmap_rows).astype(int)
            heatmap_source = heatmap_source[indexes]
        heatmap_scale = float(max(np.percentile(np.abs(heatmap_source), 99), 1.0))
        heatmap = np.clip((heatmap_source / heatmap_scale + 1.0) * 127.5, 0, 255).astype(np.uint8)

        payload = {
            "ready": True,
            "error": error,
            "configuration": self.candidate,
            "frames": len(history),
            "analysis_frames": int(recent.shape[0]),
            "heatmap": heatmap.tolist(),
            "heatmap_scale": heatmap_scale,
            "positive_mean": metrics["positive_mean"].tolist(),
            "negative_mean": metrics["negative_mean"].tolist(),
            "excess_amplitude": metrics["excess_amplitude"].tolist(),
            "percentile_response": metrics["percentile_response"].tolist(),
            "hit_rate": metrics["hit_rate"].tolist(),
            "median_baseline_noise": metrics["median_baseline_noise"],
            "median_recent_noise": metrics["median_recent_noise"],
            "maximum_hit_rate": metrics["maximum_hit_rate"],
            "maximum_percentile_response": metrics["maximum_percentile_response"],
            "band": metrics["band"],
            "low_clip_fraction": float(np.mean(recent <= 32)),
            "high_clip_fraction": float(np.mean(recent >= 65503)),
        }
        return payload

    def stop(self) -> None:
        self.running.clear()
        if self.worker is not None:
            self.worker.join(timeout=2.0)
        try:
            self.camera.stop()
        finally:
            if self.original is not None:
                self.camera.restore_configuration(self.original)


HTML = r'''<!doctype html><html><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>LISM Scanning Laser Diagnostic</title><style>
body{font-family:system-ui;background:#10141b;color:#e8eef7;margin:0;padding:18px}.cards{display:grid;grid-template-columns:repeat(auto-fit,minmax(170px,1fr));gap:10px;margin:12px 0}.card{background:#19212d;padding:12px;border-radius:8px}canvas{width:100%;height:260px;background:#080b10;border-radius:8px;margin:7px 0}#heat{image-rendering:pixelated}.warn{color:#ffbe55}.bad{color:#ff6b6b}button{padding:8px 14px}</style></head><body>
<h1>LISM Scanning Laser Diagnostic</h1><p>The heatmap preserves time. Blue is below the OFF reference, red is above it, and dark is near the reference. Switch the fixed galvo scan ON without moving the rig.</p><button onclick="resetRef()">Use current frames as new OFF reference</button><div class="cards" id="cards"></div>
<h2>Pixel versus time heatmap</h2><canvas id="heat" width="1024" height="256"></canvas>
<h2>Excess temporal amplitude</h2><canvas id="amp" width="1200" height="260"></canvas>
<h2>99th-percentile response</h2><canvas id="p99" width="1200" height="260"></canvas>
<h2>Per-pixel hit rate</h2><canvas id="hit" width="1200" height="260"></canvas>
<h2>Mean response: positive (cyan), negative (yellow)</h2><canvas id="mean" width="1200" height="260"></canvas>
<script>
function plot(id,series,zero=true){let c=document.getElementById(id),x=c.getContext('2d');x.clearRect(0,0,c.width,c.height);let all=series.flatMap(s=>s.d),lo=zero?0:Math.min(...all),hi=Math.max(...all,1);series.forEach(s=>{x.strokeStyle=s.c;x.lineWidth=1.4;x.beginPath();s.d.forEach((v,i)=>{let X=i*(c.width-1)/(s.d.length-1),Y=c.height-1-(v-lo)*(c.height-2)/(hi-lo);i?x.lineTo(X,Y):x.moveTo(X,Y)});x.stroke()})}
function heat(rows){let c=document.getElementById('heat'),x=c.getContext('2d'),h=rows.length,w=rows[0].length,img=x.createImageData(w,h);for(let y=0;y<h;y++)for(let i=0;i<w;i++){let v=rows[y][i],p=(y*w+i)*4;img.data[p]=v>128?(v-128)*2:0;img.data[p+1]=Math.max(0,255-Math.abs(v-128)*2);img.data[p+2]=v<128?(128-v)*2:0;img.data[p+3]=255}let t=document.createElement('canvas');t.width=w;t.height=h;t.getContext('2d').putImageData(img,0,0);x.imageSmoothingEnabled=false;x.clearRect(0,0,c.width,c.height);x.drawImage(t,0,0,c.width,c.height)}
async function resetRef(){await fetch('/api/reset-reference',{method:'POST'})}
async function tick(){try{let s=await(await fetch('/api/status')).json();if(s.ready){heat(s.heatmap);plot('amp',[{d:s.excess_amplitude,c:'#64d98b'}]);plot('p99',[{d:s.percentile_response,c:'#d976ff'}]);plot('hit',[{d:s.hit_rate,c:'#ff8b3d'}]);plot('mean',[{d:s.positive_mean,c:'#43d5ff'},{d:s.negative_mean,c:'#f6c85f'}]);let b=s.band||{};cards.innerHTML=`<div class=card>Frames<br><b>${s.analysis_frames}/${s.frames}</b></div><div class=card>Noise OFF/current<br><b>${s.median_baseline_noise.toFixed(1)} / ${s.median_recent_noise.toFixed(1)}</b></div><div class=card>Band<br><b>${b.start??'-'} to ${b.end??'-'} (${b.width??0})</b></div><div class=card>Band centroid<br><b>${b.centroid?.toFixed(2)??'-'}</b></div><div class=card>Concentration<br><b>${b.concentration?(100*b.concentration).toFixed(1)+'%':'-'}</b></div><div class=card>Max hit rate<br><b>${(100*s.maximum_hit_rate).toFixed(1)}%</b></div><div class=card>Clipping<br><b class=${s.low_clip_fraction>.01||s.high_clip_fraction>.01?'bad':''}>low ${(100*s.low_clip_fraction).toFixed(2)}%, high ${(100*s.high_clip_fraction).toFixed(2)}%</b></div><div class=card>Heat scale<br><b>+/- ${s.heatmap_scale.toFixed(0)}</b></div>`}}catch(e){}setTimeout(tick,250)}tick()
</script></body></html>'''


def serve(diagnostic: ScanningLaserDiagnostic, host: str, port: int) -> None:
    class Handler(BaseHTTPRequestHandler):
        def _send_json(self, value, status=200):
            payload = json.dumps(value, allow_nan=False).encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)

        def do_GET(self):
            if self.path == "/api/status":
                self._send_json(diagnostic.snapshot())
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
                diagnostic.reset_reference()
                self._send_json({"ok": True})
            except Exception as exc:
                self._send_json({"ok": False, "error": str(exc)}, 409)

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
    parser = argparse.ArgumentParser(description="Time-resolved diagnostic for a scanning or pulsed laser")
    parser.add_argument("--config", default="config/default.json")
    parser.add_argument("--bias", type=int, default=256)
    parser.add_argument("--gain", type=int, default=4)
    parser.add_argument("--st-high", type=int, default=9900)
    parser.add_argument("--st-low", type=int, default=100)
    parser.add_argument("--edge-delay", type=int, default=88)
    parser.add_argument("--baseline-frames", type=int, default=256)
    parser.add_argument("--history-frames", type=int, default=256)
    parser.add_argument("--analysis-frames", type=int, default=128)
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8767)
    args = parser.parse_args(argv)

    config, base = Config.load(args.config)
    candidate = {
        "bias": args.bias,
        "gain": args.gain,
        "st_high": args.st_high,
        "st_low": args.st_low,
        "edge_delay": args.edge_delay,
    }
    with LismCamera(config, base) as camera:
        diagnostic = ScanningLaserDiagnostic(
            camera,
            candidate,
            baseline_frames=args.baseline_frames,
            history_frames=args.history_frames,
            analysis_frames=args.analysis_frames,
        )
        diagnostic.start()
        try:
            serve(diagnostic, args.host, args.port)
        finally:
            diagnostic.stop()


if __name__ == "__main__":
    main()
