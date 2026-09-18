import unittest
import numpy as np
from laser_viewer.downward_detector import TriggerConfig,detect_frame
from laser_viewer.trigger_analysis import compare_off_on_fast
class FastTests(unittest.TestCase):
 def test_streaming_matches_brute_force(self):
  off=np.full((20,64),65535,dtype=np.uint16);on=off.copy();on[:,20:40]=0;grid={'threshold':[50000,60000],'minimum_width':[2,8],'minimum_integrated_drop':[1000.,100000.],'maximum_gap':[0,1]};rows=compare_off_on_fast(off,on,{'maximum_width':50},grid,.01)
  for row in rows:
   cfg=TriggerConfig.from_mapping(row['on']['configuration']);regions=[detect_frame(f,cfg).selected_region for f in on];basic=[r for r in regions if r];good=[r for r in basic if r.measurement_quality];self.assertAlmostEqual(row['on']['basic_trigger_fraction'],len(basic)/len(on));self.assertAlmostEqual(row['on']['good_frame_yield'],len(good)/len(on))
