from __future__ import annotations
import json
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from importlib.resources import files

class Handler(BaseHTTPRequestHandler):
    service = None
    def _send(self, code, body, content_type="application/json"):
        data = body if isinstance(body, bytes) else body.encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers(); self.wfile.write(data)
    def do_GET(self):
        if self.path == "/api/status":
            self._send(200, json.dumps(self.service.snapshot(), allow_nan=False))
        elif self.path == "/":
            self._send(200, files("laser_viewer").joinpath("web/index.html").read_bytes(), "text/html; charset=utf-8")
        else:
            self._send(404, json.dumps({"error": "not found"}))
    def do_POST(self):
        mapping = {"/api/calibrate/dark":"dark", "/api/calibrate/laser":"laser", "/api/log/start":"log_start", "/api/log/stop":"log_stop"}
        command = mapping.get(self.path)
        if not command:
            self._send(404, json.dumps({"error":"not found"})); return
        try:
            result = self.service.command(command)
            self._send(200, json.dumps(result))
        except Exception as exc:
            self._send(409, json.dumps({"error": str(exc)}))
    def log_message(self, fmt, *args):
        return

def serve(service, host: str, port: int):
    Handler.service = service
    server = ThreadingHTTPServer((host, port), Handler)
    try:
        server.serve_forever()
    finally:
        server.server_close()
