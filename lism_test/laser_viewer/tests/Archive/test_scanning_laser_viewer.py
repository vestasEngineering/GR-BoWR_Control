import unittest
import numpy as np
from laser_viewer.scanning_laser_viewer import scanning_metrics

class ScanningMetricsTests(unittest.TestCase):
    def test_detects_broad_variance_band_without_mean_shift(self):
        rng = np.random.default_rng(4)
        baseline = rng.normal(30000, 10, size=(256, 2048))
        recent = rng.normal(30000, 10, size=(128, 2048))
        recent[:, 700:900] += rng.normal(0, 250, size=(128, 200))
        result = scanning_metrics(baseline, recent, noise_sigma=3.0, minimum_signal=20.0)
        self.assertIsNotNone(result["band"])
        self.assertLessEqual(result["band"]["start"], 750)
        self.assertGreaterEqual(result["band"]["end"], 850)
        self.assertGreater(result["median_recent_noise"], result["median_baseline_noise"])

    def test_quiet_capture_has_no_broad_band(self):
        rng = np.random.default_rng(5)
        baseline = rng.normal(30000, 10, size=(256, 2048))
        recent = rng.normal(30000, 10, size=(128, 2048))
        result = scanning_metrics(baseline, recent, noise_sigma=3.0, minimum_signal=50.0)
        self.assertIsNone(result["band"])

if __name__ == "__main__":
    unittest.main()
