import cv2
import apriltag
import asyncio
import threading
import inspect
from collections import deque, defaultdict
import math
from queues import Queues

import os, sys
from contextlib import contextmanager


def is_squareish(corners, angle_tol_deg=35, side_ratio_tol=0.5):
    v = []
    for i in range(4):
        x1, y1 = corners[i]
        x2, y2 = corners[(i + 1) % 4]
        v.append((x2 - x1, y2 - y1))

    lens = [math.hypot(dx, dy) for (dx, dy) in v]
    min_len, max_len = min(lens), max(lens)
    if min_len < 1e-6:
        return False
    if (min_len / max_len) < side_ratio_tol:
        return False

    def angle_deg(a, b):
        (ax, ay), (bx, by) = a, b
        dot = ax * bx + ay * by
        na = math.hypot(ax, ay)
        nb = math.hypot(bx, by)
        if na * nb == 0:
            return 0.0
        cosang = max(-1.0, min(1.0, dot / (na * nb)))
        return math.degrees(math.acos(cosang))

    for i in range(4):
        ang = angle_deg(v[i], v[(i + 1) % 4])
        if abs(ang - 90) > angle_tol_deg:
            return False

    return True

class FrameGrabber:
    def __init__(self, rtsp_url):
        self.cap = cv2.VideoCapture(rtsp_url)
        self.latest_frame = None
        self.running = True
        self.lock = threading.Lock()
        threading.Thread(target=self._update, daemon=True).start()

    def _update(self):
        while self.running:
            ret, frame = self.cap.read()
            if ret:
                with self.lock:
                    self.latest_frame = frame

    def get_latest_frame(self):
        with self.lock:
            return self.latest_frame

    def stop(self):
        self.running = False
        self.cap.release()

class AprilTagDetector:
    def __init__(self, queues: Queues, rtsp_url: str):
        self.responses = queues.responses
        self.rtsp_url = rtsp_url

        # Build DetectorOptions with only kwargs supported by the installed package
        base_kwargs = dict(
            families="tag16h5",
            nthreads=2,
            quad_decimate=1.0,     # accuracy first (fewer false positives)
            refine_edges=True,
            decode_sharpening=0.25,
            # quad_sigma will be added only if supported (see below)
        )
        # Detect supported parameters
        try:
            sig = inspect.signature(apriltag.DetectorOptions)
            supported = set(sig.parameters.keys())
            # Add quad_sigma only if supported
            if "quad_sigma" in supported:
                base_kwargs["quad_sigma"] = 0.8
            # Filter to supported keys
            opt_kwargs = {k: v for k, v in base_kwargs.items() if k in supported}
            options = apriltag.DetectorOptions(**opt_kwargs)
        except Exception:
            # Fallback: minimal options
            options = apriltag.DetectorOptions(families="tag16h5")

        self.detector = apriltag.Detector(options)
        self.running = True
        self.frame_grabber = FrameGrabber(rtsp_url)

        # Temporal memory
        self.history = defaultdict(lambda: deque(maxlen=3))

        # Tunables
        self.min_decision_margin = 40.0
        self.require_hamming_zero = True
        self.min_box_side = 20  
        self.center_tolerance_px = 20
        self.min_frames_confirm = 2
        self.allowed_ids = {0, 1}

        # Preprocessing toggles
        self.use_pre_blur = True      # emulate quad_sigma if needed
        self.pre_blur_ksize = (3, 3)  # small blur
        self.pre_blur_sigma = 0       # OpenCV auto sigma (or use 0.8 if you prefer)

    def _passes_quality_filters(self, d, frame_shape):
        hamming = int(getattr(d, "hamming", 1))
        margin = float(getattr(d, "decision_margin", 0.0))

        if self.require_hamming_zero and hamming != 0:
            return False
        if margin < self.min_decision_margin:
            return False

        x_coords = [int(pt[0]) for pt in d.corners]
        y_coords = [int(pt[1]) for pt in d.corners]
        x1, y1, x2, y2 = min(x_coords), min(y_coords), max(x_coords), max(y_coords)
        box_w = x2 - x1
        box_h = y2 - y1
        if min(box_w, box_h) < self.min_box_side:
            return False

        if not is_squareish(d.corners, angle_tol_deg=35, side_ratio_tol=0.5):
            return False

        return True

    def _temporal_vote(self, tag_id, cx, cy):
        q = self.history[tag_id]
        close = sum(
            1 for (px, py) in q
            if abs(px - cx) <= self.center_tolerance_px and abs(py - cy) <= self.center_tolerance_px
        )
        q.append((cx, cy))
        return (close + 1) >= self.min_frames_confirm

    async def run(self):
        print("📡 RTSP stream opened. Starting AprilTag detection...")

        try:
            while self.running:
                frame = self.frame_grabber.get_latest_frame()
                if frame is None:
                    await asyncio.sleep(0.05)
                    continue

                gray = cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY)

                if self.use_pre_blur:
                    gray = cv2.GaussianBlur(gray, self.pre_blur_ksize, self.pre_blur_sigma)
                
                with suppress_stderr_fd():
                    detections = self.detector.detect(gray)

                frame_detections = []

                for d in detections:
                    if d.tag_id not in self.allowed_ids:
                        continue
                    if not self._passes_quality_filters(d, frame.shape):
                        continue

                    cx, cy = int(d.center[0]), int(d.center[1])
                    if not self._temporal_vote(d.tag_id, cx, cy):
                        continue

                    x_coords = [int(pt[0]) for pt in d.corners]
                    y_coords = [int(pt[1]) for pt in d.corners]
                    x1, y1 = min(x_coords), min(y_coords)
                    x2, y2 = max(x_coords), max(y_coords)

                    detection_data = {
                        "type": "apriltag",
                        "tag_id": int(d.tag_id),
                        "center": {"x": cx, "y": cy},
                        "box": {"x1": x1, "y1": y1, "x2": x2, "y2": y2},
                        "decision_margin": float(getattr(d, "decision_margin", 0.0)),
                        "hamming": int(getattr(d, "hamming", -1)),
                    }
                    frame_detections.append(detection_data)

                if frame_detections:
                    await self.responses.put(frame_detections)

                await asyncio.sleep(0.05)

        except asyncio.CancelledError:
            print("🛑 AprilTag detection cancelled.")
        finally:
            self.frame_grabber.stop()
            print("📴 RTSP stream closed.")
            
@contextmanager
def suppress_stderr_fd():
    """
    Temporarily redirect the process-level file descriptor 2 (stderr) to /dev/null.
    Works for C libraries that write directly to stderr.
    """
    devnull_fd = os.open(os.devnull, os.O_WRONLY)
    saved_stderr_fd = os.dup(2)
    try:
        os.dup2(devnull_fd, 2)  # redirect fd 2 to /dev/null
        yield
    finally:
        os.dup2(saved_stderr_fd, 2)  # restore fd 2
        os.close(saved_stderr_fd)
        os.close(devnull_fd)
