import unittest
from laser_viewer.joint_analyze import frame_indices, wilson

class AnalyzeTests(unittest.TestCase):
    def test_time_blocks_do_not_overlap(self):
        train=set(frame_indices(1000,0,.5));selection=set(frame_indices(1000,.5,.75));validation=set(frame_indices(1000,.75,1));self.assertFalse(train&selection);self.assertFalse(train&validation);self.assertFalse(selection&validation)
    def test_zero_event_upper_bound_remains_nonzero(self):
        low,high=wilson(0,5000);self.assertEqual(low,0);self.assertGreater(high,0);self.assertLess(high,.001)
