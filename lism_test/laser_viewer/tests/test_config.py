import unittest
from laser_viewer.config import Config
class T(unittest.TestCase):
 def test_serials(self):
  c=Config(cameras=[{'camera_id':'a','serial_number':'160154'},{'camera_id':'b','serial_number':'160155'}]);self.assertEqual(len(c.cameras),2)
 def test_duplicate(self):
  with self.assertRaises(ValueError):Config(cameras=[{'camera_id':'a','serial_number':'1'},{'camera_id':'b','serial_number':'1'}])
if __name__=='__main__':unittest.main()
