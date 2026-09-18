import unittest

import numpy as np

from laser_viewer.calibration import Calibration
from laser_viewer.models import DeviceSignature
from laser_viewer.processing import detect


SIG = DeviceSignature("x", 32, 64, 1000, 100, 88, 0, 768, 277)


class ProcessingTests(unittest.TestCase):
    def calibration(self, polarity="positive", reference_centroid=None):
        return Calibration(
            np.full(32, 1000.0),
            np.full(32, 2.0),
            None,
            SIG,
            "now",
            polarity=polarity,
            reference_centroid=reference_centroid,
            reference_start=8 if reference_centroid is not None else None,
            reference_end=14 if reference_centroid is not None else None,
        )

    def test_no_detection(self):
        result = detect(np.full(32, 1005), self.calibration(), 6, 100, 3)
        self.assertFalse(result.detected)

    def test_positive_centroid(self):
        frame = np.full(32, 1000.0)
        frame[10:13] += [200, 400, 200]
        result = detect(frame, self.calibration(), 6, 50, 3)
        self.assertTrue(result.detected)
        self.assertEqual(result.peak_pixel, 11)
        self.assertAlmostEqual(result.centroid, 11.0)
        self.assertEqual(result.region_width, 3)

    def test_negative_going_laser_is_preserved(self):
        frame = np.full(32, 1000.0)
        frame[10:14] -= [200, 400, 400, 200]
        result = detect(frame, self.calibration("negative"), 6, 50, 4)
        self.assertTrue(result.detected)
        self.assertEqual(result.region_start, 10)
        self.assertEqual(result.region_end, 13)
        self.assertAlmostEqual(result.centroid, 11.5)

    def test_low_and_high_clip(self):
        frame = np.full(32, 1000.0)
        frame[4] = 0
        frame[20] = 65530
        result = detect(frame, self.calibration(), 6, 50, 2, clip_margin=16)
        self.assertTrue(result.low_clipped)
        self.assertTrue(result.high_clipped)
        self.assertTrue(result.saturated)

    def test_saturated_is_false_without_rail_clipping(self):
        frame = np.full(32, 1000.0)
        frame[10:13] += [200, 400, 200]
        result = detect(frame, self.calibration(), 6, 50, 3, clip_margin=16)
        self.assertFalse(result.saturated)

    def test_single_pixel_glitch_rejected(self):
        frame = np.full(32, 1000.0)
        frame[5] = 5000
        result = detect(
            frame,
            self.calibration(),
            6,
            50,
            2,
            minimum_region_width=2,
        )
        self.assertFalse(result.detected)


if __name__ == "__main__":
    unittest.main()
