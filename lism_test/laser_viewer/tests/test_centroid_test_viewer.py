import json
import tempfile
import unittest
from pathlib import Path

import numpy as np

from laser_viewer.centroid_test_viewer import build_payload, load_frame
from laser_viewer.downward_detector import TriggerConfig


class CentroidTestViewerTests(unittest.TestCase):
    def setUp(self):
        self.trigger = TriggerConfig.from_mapping({
            "threshold": 60000,
            "minimum_width": 2,
            "maximum_width": 20,
            "minimum_integrated_drop": 1000,
            "maximum_gap": 0,
        })

    def test_build_payload_reports_geometric_center(self):
        frame = np.full(64, 65535, dtype=np.uint16)
        frame[20:26] = np.array([59000, 50000, 30000, 10000, 1000, 100], dtype=np.uint16)
        payload = build_payload(frame, "test", self.trigger)
        self.assertEqual(payload["comparison"]["start"], 20)
        self.assertEqual(payload["comparison"]["end"], 25)
        self.assertEqual(payload["comparison"]["geometric_center"], 22.5)
        self.assertGreater(payload["comparison"]["drop_weighted_centroid"], 22.5)

    def test_loads_npy_and_json_frames(self):
        frame = np.arange(16, dtype=np.uint16)
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            npy = root / "frame.npy"
            js = root / "frame.json"
            np.save(npy, frame, allow_pickle=False)
            js.write_text(json.dumps({"pixels": frame.tolist()}), encoding="utf-8")
            np.testing.assert_array_equal(load_frame(npy), frame)
            np.testing.assert_array_equal(load_frame(js), frame)

    def test_rejects_frame_without_detected_region(self):
        frame = np.full(64, 65535, dtype=np.uint16)
        with self.assertRaisesRegex(ValueError, "no qualifying"):
            build_payload(frame, "quiet", self.trigger)


if __name__ == "__main__":
    unittest.main()
