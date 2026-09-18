import unittest
import numpy as np
from laser_viewer.camera_square_wave_optimize import score
from laser_viewer.downward_detector import QualityConfig,TriggerConfig
class CameraScoreTests(unittest.TestCase):
 def test_clean_frames_outscore_background(self):
  off=np.full((10,64),65535,dtype=np.uint16);on=off.copy();on[:,20:40]=0;cfg=TriggerConfig(maximum_width=50,minimum_integrated_drop=1);self.assertEqual(score(off,cfg,QualityConfig())['good_frames'],0);self.assertEqual(score(on,cfg,QualityConfig())['good_frames'],10)
