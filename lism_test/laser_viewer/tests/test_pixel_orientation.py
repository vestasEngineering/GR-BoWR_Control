import unittest
from types import SimpleNamespace
from unittest.mock import Mock

import numpy as np

from laser_viewer.camera import LismCamera


class PixelOrientationTests(unittest.TestCase):
    @staticmethod
    def make_camera(reverse_pixels: bool) -> LismCamera:
        sdk = SimpleNamespace(RET_OK=0, LISM_ADDR=0xFE)
        api = SimpleNamespace(geterrorstring=lambda rc: "SUCCESS")
        session = SimpleNamespace(
            sdk=sdk,
            api=api,
            config=SimpleNamespace(
                configuration_readback_retries=1,
                configuration_readback_retry_ms=0,
            ),
        )
        config = SimpleNamespace(
            camera_id="test_sensor",
            serial_number="test_serial",
            pipe_size=4096,
            initial_packet_length=8,
            expected_pixels=4,
            byte_order=">u2",
            wait_timeout_ms=10,
            reverse_pixels=reverse_pixels,
        )
        camera = LismCamera.from_shared_device(session, config)
        camera.streaming = True
        camera.packet_length = 8
        camera.dev = Mock()
        camera.dev.waitforpipecount.return_value = (0, 8)
        camera.dev.getpipe.return_value = (
            0,
            np.array([10, 20, 30, 40], dtype=">u2").tobytes(),
        )
        return camera

    def test_native_order_is_preserved(self):
        frame = self.make_camera(False).read_frame()
        np.testing.assert_array_equal(frame, [10, 20, 30, 40])

    def test_reversed_order_is_applied(self):
        frame = self.make_camera(True).read_frame()
        np.testing.assert_array_equal(frame, [40, 30, 20, 10])


if __name__ == "__main__":
    unittest.main()
