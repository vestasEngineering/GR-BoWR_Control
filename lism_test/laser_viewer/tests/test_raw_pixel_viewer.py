import unittest
import numpy as np
from laser_viewer.raw_pixel_viewer import detect_downward_region, validate_trigger
class TriggerTests(unittest.TestCase):
 def test_detects_weighted_downward_region(self):
  frame=np.full(2048,65535,dtype=np.uint16);frame[900:910]=np.array([59000,50000,30000,10000,100,10000,30000,50000,59000,59900]);trigger=validate_trigger({'threshold':60000,'minimum_width':2,'maximum_width':20,'minimum_integrated_drop':10000,'history_size':32});result=detect_downward_region(frame,trigger);self.assertIsNotNone(result);self.assertEqual(result['start'],900);self.assertEqual(result['end'],909);self.assertGreater(result['centroid'],903);self.assertLess(result['centroid'],906)
 def test_rejects_single_pixel_glitch(self):
  frame=np.full(2048,65535,dtype=np.uint16);frame[100]=0;trigger=validate_trigger({'threshold':60000,'minimum_width':2,'maximum_width':20,'minimum_integrated_drop':10000,'history_size':32});self.assertIsNone(detect_downward_region(frame,trigger))
 def test_prefers_region_with_larger_integrated_drop(self):
  frame=np.full(64,65535,dtype=np.uint16);frame[5:8]=59000;frame[30:35]=20000;trigger=validate_trigger({'threshold':60000,'minimum_width':2,'maximum_width':20,'minimum_integrated_drop':1000,'history_size':8});self.assertEqual(detect_downward_region(frame,trigger)['start'],30)
if __name__=='__main__':unittest.main()
