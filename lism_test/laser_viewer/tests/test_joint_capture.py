import tempfile
import unittest
from pathlib import Path
from laser_viewer.joint_capture import CameraCandidate, sha256_file, space_filling

class CaptureTests(unittest.TestCase):
    def test_space_filling_is_deterministic_and_bounded(self):
        candidates=[CameraCandidate(b,15,h,l,e) for b in (246,270,286) for h in (7500,9000) for l in (50,100) for e in (80,88)]
        reference=CameraCandidate(286,15,7500,100,88)
        self.assertEqual(space_filling(candidates,8,reference),space_filling(candidates,8,reference))
        self.assertEqual(len(space_filling(candidates,8,reference)),8)
    def test_checksum_changes_with_content(self):
        with tempfile.TemporaryDirectory() as directory:
            path=Path(directory)/'data';path.write_bytes(b'a');first=sha256_file(path);path.write_bytes(b'b');self.assertNotEqual(first,sha256_file(path))
