import unittest
from collections import deque
from types import SimpleNamespace
import numpy as np
from laser_viewer.promising_viewer import LiveViewer

class ViewerTests(unittest.TestCase):
    def test_negative_signal_snapshot(self):
        viewer = LiveViewer(SimpleNamespace(), {"bias":256,"gain":4,"st_high":5000,"st_low":100,"edge_delay":88}, rolling_frames=2)
        viewer.baseline = np.full(8, 1000.0)
        viewer.latest = np.full(8, 1000.0)
        viewer.latest[3] = 600
        viewer.frames = deque([viewer.latest.copy(), viewer.latest.copy()], maxlen=2)
        result = viewer.snapshot()
        self.assertTrue(result["ready"])
        self.assertEqual(result["polarity"], "negative")
        self.assertEqual(result["peak_pixel"], 3)

if __name__ == "__main__":
    unittest.main()
