import unittest
import numpy as np

from laser_viewer.scan_rate import frame_signals, spectral_peaks


class ScanRateTests(unittest.TestCase):
    def test_fft_finds_known_frequency(self):
        sample_rate = 200.0
        timestamps = np.arange(0.0, 10.0, 1.0 / sample_rate)
        values = np.sin(2.0 * np.pi * 17.5 * timestamps)
        result = spectral_peaks(timestamps, values, 1.0, 80.0)
        self.assertTrue(result["peaks"])
        self.assertAlmostEqual(result["peaks"][0]["frequency_hz"], 17.5, delta=0.2)

    def test_region_signal_detects_intermittent_crossing(self):
        baseline = np.full((64, 2048), 30000.0)
        frames = baseline.copy()
        frames[::4, 512:640] += 2000.0
        signals = frame_signals(frames, baseline)
        self.assertGreater(signals["region_04_delta_sum"].max(), 0.0)
        self.assertEqual(signals["region_00_delta_sum"].max(), 0.0)


if __name__ == "__main__":
    unittest.main()
