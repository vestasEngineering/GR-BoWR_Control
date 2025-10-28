import cv2
import apriltag
import asyncio
import threading
from queues import Queues

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
        self.detector = apriltag.Detector(apriltag.DetectorOptions(families="tag16h5"))
        self.running = True
        self.frame_grabber = FrameGrabber(rtsp_url)

    async def run(self):
        print("📡 RTSP stream opened. Starting AprilTag detection...")

        try:
            while self.running:
                frame = self.frame_grabber.get_latest_frame()
                if frame is None:
                    await asyncio.sleep(0.1)
                    continue

                gray = cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY)
                detections = self.detector.detect(gray)

                frame_detections = []

                for d in detections:
                    if d.tag_id in [0, 1]:
                        # Extract corner coordinates
                        x_coords = [int(pt[0]) for pt in d.corners]
                        y_coords = [int(pt[1]) for pt in d.corners]

                        # Compute bounding box from corners
                        x1, y1 = min(x_coords), min(y_coords)
                        x2, y2 = max(x_coords), max(y_coords)

                        detection_data = {
                            "type": "apriltag",
                            "tag_id": d.tag_id,
                            "center": {"x": int(d.center[0]), "y": int(d.center[1])},
                            "box": {
                                "x1": x1,
                                "y1": y1,
                                "x2": x2,
                                "y2": y2
                            }
                        }

                        frame_detections.append(detection_data)


                if frame_detections:
                    await self.responses.put(frame_detections)

                await asyncio.sleep(0.2)

        except asyncio.CancelledError:
            print("🛑 AprilTag detection cancelled.")
        finally:
            self.frame_grabber.stop()
            print("📴 RTSP stream closed.")