import tempfile
import unittest
from pathlib import Path
import numpy as np
from laser_viewer.calibration import CalibrationStore, add_laser, build_dark
from laser_viewer.models import DeviceSignature

SIG=DeviceSignature("serial",16,32,1000,100,88,0,768,277)
class CalibrationTests(unittest.TestCase):
    def dark(self): return build_dark(np.tile(np.arange(16)+1000.0,(8,1)),SIG)
    def test_negative_polarity_auto_detection_and_roundtrip(self):
        dark=self.dark(); frames=np.tile(dark.dark_mean,(8,1)); frames[:,6:10]-=np.array([200,400,400,200])
        laser=add_laser(dark,frames,6,50)
        self.assertEqual(laser.polarity,"negative"); self.assertEqual((laser.reference_start,laser.reference_end),(4,11)); self.assertAlmostEqual(laser.reference_centroid,7.5)
        with tempfile.TemporaryDirectory() as d:
            store=CalibrationStore(Path(d)); store.save(laser); loaded=store.load()
            self.assertEqual(loaded.polarity,"negative"); self.assertAlmostEqual(loaded.reference_centroid,7.5)
    def test_positive_polarity_auto_detection(self):
        dark=self.dark(); frames=np.tile(dark.dark_mean,(8,1)); frames[:,4:7]+=np.array([200,500,200])
        self.assertEqual(add_laser(dark,frames,6,50).polarity,"positive")
    def test_signature_mismatch_rejected(self):
        wrong=DeviceSignature("other",16,32,1000,100,88,0,768,277)
        with self.assertRaises(ValueError): CalibrationStore.validate(self.dark(),wrong)
if __name__ == "__main__": unittest.main()

class PermissiveCalibrationThresholdTests(unittest.TestCase):
    def test_calibration_threshold_can_be_lower_than_runtime_threshold(self):
        dark=build_dark(np.tile(np.arange(16)+1000.0,(8,1)),SIG)
        frames=np.tile(dark.dark_mean,(8,1)); frames[:,6:10]-=np.array([30,60,60,30])
        laser=add_laser(dark,frames,noise_sigma=6,minimum_signal=100,calibration_noise_sigma=3,calibration_minimum_signal=20)
        self.assertEqual(laser.polarity,"negative")
        self.assertTrue(laser.has_laser)

    def test_missing_laser_still_rejected_with_diagnostics(self):
        dark=build_dark(np.tile(np.arange(16)+1000.0,(8,1)),SIG)
        frames=np.tile(dark.dark_mean,(8,1))
        with self.assertRaisesRegex(ValueError, "peak_delta"):
            add_laser(dark,frames,calibration_noise_sigma=3,calibration_minimum_signal=20)
