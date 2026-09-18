import unittest
from laser_viewer.config import Config

class SteeringConfigurationTests(unittest.TestCase):
    def camera(self, camera_id, serial, role, reverse=False, zero=1023.5):
        return {"camera_id": camera_id, "serial_number": serial, "reverse_pixels": reverse, "steering_role": role, "millimeters_per_pixel": 0.014, "zero_pixel": zero}
    def test_role_lookup_is_independent_of_order(self):
        config = Config(cameras=[self.camera("bottom", "2", "bottom", True, 1025.0), self.camera("top", "1", "top", False, 1019.0)])
        top, bottom = config.steering_cameras()
        self.assertEqual((top.camera_id, bottom.camera_id), ("top", "bottom"))
    def test_duplicate_role_is_rejected(self):
        with self.assertRaisesRegex(ValueError, "one top and one bottom"):
            Config(cameras=[self.camera("a", "1", "top"), self.camera("b", "2", "top")])
    def test_partial_calibration_is_rejected(self):
        values = self.camera("a", "1", "top"); del values["zero_pixel"]
        with self.assertRaisesRegex(ValueError, "zero_pixel"):
            Config(cameras=[values, self.camera("b", "2", "bottom")])
    def test_zero_pixel_must_be_on_sensor(self):
        with self.assertRaisesRegex(ValueError, "within the sensor"):
            Config(cameras=[self.camera("a", "1", "top", zero=3000), self.camera("b", "2", "bottom")])
    def test_230_mm_geometry(self):
        config = Config(cameras=[self.camera("a", "1", "top"), self.camera("b", "2", "bottom")], steering={"sensor_separation_mm": 230.0, "maximum_pair_age_us": 2000})
        self.assertEqual(config.steering.sensor_separation_mm, 230.0)
if __name__ == "__main__": unittest.main()
