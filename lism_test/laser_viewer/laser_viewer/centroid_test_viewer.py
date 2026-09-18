from __future__ import annotations

import argparse
import json
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import numpy as np

from .centroid_methods import compare_centroids
from .downward_detector import TriggerConfig, detect_frame


HTML = r'''<!doctype html><meta charset="utf-8"><title>LISM Centroid Method Comparison</title>
<style>body{margin:0;padding:18px;font-family:system-ui;background:#10141b;color:#e8eef7}.card{background:#19212d;border-radius:12px;padding:12px;margin-top:12px}canvas{width:100%;height:340px;background:#080b10;border-radius:8px}.note{color:#aebdce}</style>
<h1>LISM detected-footprint center comparison</h1><p class="note">Diagnostic view of the strongest basic threshold-qualified region. Steering validity is reported separately.</p><div id="summary" class="card"></div><canvas id="plot" width="1400" height="340"></canvas>
<script>function line(x,v,c,n){if(v==null)return;const X=v/(n-1)*x.canvas.width;x.strokeStyle=c;x.lineWidth=3;x.beginPath();x.moveTo(X,0);x.lineTo(X,x.canvas.height);x.stroke()}async function init(){const d=await(await fetch('/api/frame')).json(),c=document.getElementById('plot'),x=c.getContext('2d'),p=d.pixels;x.strokeStyle='#43d5ff';x.beginPath();p.forEach((v,i)=>{const X=i/(p.length-1)*c.width,Y=c.height-v/65535*c.height;i?x.lineTo(X,Y):x.moveTo(X,Y)});x.stroke();line(x,d.comparison.geometric_center,'#64d98b',p.length);line(x,d.comparison.drop_weighted_centroid,'#ffbf69',p.length);line(x,d.comparison.half_drop_position,'#c49bff',p.length);line(x,d.comparison.peak_drop_position,'#ff6b6b',p.length);summary.textContent=`region=${d.comparison.start}..${d.comparison.end} geometric=${d.comparison.geometric_center.toFixed(3)} px steering_valid=${d.steering_valid} rejection=${d.region_rejection_reason}`;}init().catch(e=>summary.textContent=e);</script>'''


def load_frame(path: Path) -> np.ndarray:
    if path.suffix.lower() == ".npy":
        frame = np.load(path, allow_pickle=False)
    elif path.suffix.lower() == ".json":
        payload = json.loads(path.read_text(encoding="utf-8"))
        frame = payload["pixels"] if isinstance(payload, dict) else payload
    elif path.suffix.lower() == ".csv":
        frame = np.loadtxt(path, delimiter=",")
    else:
        raise ValueError("frame must be .npy, .json, or .csv")
    frame = np.asarray(frame)
    if frame.ndim == 2 and frame.shape[0] == 1:
        frame = frame[0]
    if frame.ndim != 1:
        raise ValueError("frame file must contain exactly one one-dimensional frame")
    return frame.astype(np.uint16)


def build_payload(frame: np.ndarray, source: str, trigger: TriggerConfig) -> dict:
    result = detect_frame(frame, trigger)
    region = result.selected_region
    if region is None and result.regions:
        region = max(result.regions, key=lambda item: item.integrated_drop)
    if region is None:
        raise ValueError("the supplied frame has no qualifying detected region")
    comparison = compare_centroids(frame, region.start, region.end)
    return {
        "source": source,
        "pixels": frame.tolist(),
        "steering_valid": bool(result.selected_region is region and region.measurement_quality),
        "region_measurement_quality": bool(region.measurement_quality),
        "region_rejection_reason": region.rejection_reason,
        "comparison": comparison.to_dict(),
    }


def serve_payload(payload: dict, host: str, port: int) -> None:
    encoded_payload = json.dumps(payload, allow_nan=False).encode("utf-8")
    encoded_html = HTML.encode("utf-8")
    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            if self.path == "/api/frame": body, content_type = encoded_payload, "application/json"
            elif self.path in ("/", "/index.html"): body, content_type = encoded_html, "text/html; charset=utf-8"
            else: self.send_error(404); return
            self.send_response(200); self.send_header("Content-Type", content_type); self.send_header("Cache-Control", "no-store"); self.send_header("Content-Length", str(len(body))); self.end_headers(); self.wfile.write(body)
        def log_message(self, *_): return
    server = ThreadingHTTPServer((host, port), Handler)
    try: print(f"Open http://{host}:{port}"); server.serve_forever(0.2)
    except KeyboardInterrupt: pass
    finally: server.server_close()


def main(argv=None) -> None:
    parser = argparse.ArgumentParser(description="Compare centroid methods on one saved LISM frame")
    parser.add_argument("frame", type=Path); parser.add_argument("--threshold", type=int, default=60000); parser.add_argument("--minimum-width", type=int, default=2); parser.add_argument("--maximum-width", type=int, default=512); parser.add_argument("--minimum-integrated-drop", type=float, default=10000); parser.add_argument("--maximum-gap", type=int, default=0); parser.add_argument("--host", default="127.0.0.1"); parser.add_argument("--port", type=int, default=8770)
    args = parser.parse_args(argv); trigger = TriggerConfig.from_mapping(vars(args)); frame = load_frame(args.frame); payload = build_payload(frame, str(args.frame), trigger); serve_payload(payload, args.host, args.port)


if __name__ == "__main__": main()
