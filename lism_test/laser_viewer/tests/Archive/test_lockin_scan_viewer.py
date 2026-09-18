import unittest
import numpy as np

from laser_viewer.lockin_scan_viewer import analyze_lockin, lockin_profile


class LockinTests(unittest.TestCase):
    def test_recovers_modulated_pixel_band(self):
        sample_rate = 1000.0
        timestamps = np.arange(512) / sample_rate
        rng = np.random.default_rng(7)
        off = 30000.0 + rng.normal(0, 20, size=(512, 2048))
        current = 30000.0 + rng.normal(0, 20, size=(512, 2048))
        wave = 500.0 * np.sin(2.0 * np.pi * 49.6318 * timestamps)
        current[:, 700:760] += wave[:, None]
        result = analyze_lockin(off, timestamps, current, timestamps, 49.6318)
        self.assertIsNotNone(result["band"])
        self.assertLessEqual(result["band"]["start"], 710)
        self.assertGreaterEqual(result["band"]["end"], 750)
        self.assertGreater(result["band"]["concentration"], 0.8)

    def test_rejects_wrong_frequency(self):
        sample_rate = 1000.0
        timestamps = np.arange(512) / sample_rate
        frames = np.full((512, 32), 30000.0)
        wave = 500.0 * np.sin(2.0 * np.pi * 49.6318 * timestamps)
        frames[:, 10] += wave
        correct = lockin_profile(frames, timestamps, 49.6318)["amplitude"][10]
        wrong = lockin_profile(frames, timestamps, 80.0)["amplitude"][10]
        self.assertGreater(correct, 10.0 * wrong)


if __name__ == "__main__":
    unittest.main()
