import unittest
import numpy as np
from laser_viewer.downward_detector import TriggerConfig, detect_frame

class DetectorTests(unittest.TestCase):
    def config(self, **changes):
        values=dict(minimum_integrated_drop=1000, minimum_active_pixels=12,
                    minimum_active_coverage=.35, minimum_sustained_span=12)
        values.update(changes); return TriggerConfig(**values)
    def test_broad_dynamic_footprint_is_valid_anywhere(self):
        for start in (5, 700, 1800):
            frame=np.full(2048,65535,dtype=np.uint16); frame[start:start+80]=30000
            result=detect_frame(frame,self.config())
            self.assertIsNotNone(result.selected_region)
            self.assertAlmostEqual(result.selected_region.selected_center,start+39.5,delta=2)
    def test_deep_narrow_spike_is_not_steering_valid(self):
        frame=np.full(2048,65535,dtype=np.uint16); frame[900:904]=0
        result=detect_frame(frame,self.config())
        self.assertTrue(result.detected_candidate); self.assertIsNone(result.selected_region)
        self.assertEqual(result.regions[0].rejection_reason,"insufficient_active_pixels")
    def test_valid_region_beats_deeper_spike(self):
        frame=np.full(2048,65535,dtype=np.uint16); frame[100:104]=0; frame[1200:1260]=40000
        result=detect_frame(frame,self.config())
        self.assertEqual(result.selected_region.start,1200)
    def test_fragmented_sparse_region_rejected(self):
        frame=np.full(256,65535,dtype=np.uint16); frame[50:100:4]=1000
        result=detect_frame(frame,self.config(maximum_gap=3,minimum_active_pixels=8))
        self.assertIsNone(result.selected_region)

if __name__=='__main__': unittest.main()
