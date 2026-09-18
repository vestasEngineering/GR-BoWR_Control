import math, struct, unittest, zlib
from laser_viewer.steering_output import *
class SteeringTests(unittest.TestCase):
 def setUp(self):
  self.p=SteeringAnglePairer(SensorCalibration('top',.1,1024),SensorCalibration('bottom',.1,1024),230,2000)
 def test_angle(self):
  self.assertIsNone(self.p.update(SensorMeasurement('top',1,1_000_000,1000,True,'high')))
  r=self.p.update(SensorMeasurement('bottom',1,1_500_000,1100,True,'high'))
  self.assertTrue(r.valid); self.assertAlmostEqual(r.angle_degrees,math.degrees(math.atan2(10,230)))
 def test_stale_pair_invalid(self):
  self.p.update(SensorMeasurement('top',1,1_000_000,1000,True,'high'))
  r=self.p.update(SensorMeasurement('bottom',1,5_000_000,1100,True,'high'))
  self.assertFalse(r.valid); self.assertEqual(r.reason,'pair_too_old')
 def test_no_reuse(self):
  self.p.update(SensorMeasurement('top',1,1_000_000,1000,True,'high'))
  self.assertIsNotNone(self.p.update(SensorMeasurement('bottom',1,1_100_000,1000,True,'high')))
  self.assertIsNone(self.p.update(SensorMeasurement('bottom',2,1_200_000,1001,True,'high')))
 def test_binary_crc(self):
  self.p.update(SensorMeasurement('top',1,1_000_000,1000,True,'high'))
  packet=self.p.update(SensorMeasurement('bottom',1,1_100_000,1000,True,'high')).to_binary()
  self.assertEqual(len(packet),38); self.assertEqual(struct.unpack('<I',packet[-4:])[0],zlib.crc32(packet[:-4])&0xffffffff)
if __name__=='__main__':unittest.main()
