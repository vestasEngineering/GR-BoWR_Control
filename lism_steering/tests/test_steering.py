import json
import math
import tempfile
import unittest
from pathlib import Path

from lism_steering.config import Settings
from lism_steering.protocol import encode_steering
from lism_steering.steering import (
    SendPolicy,
    SensorCalibration,
    SensorMeasurement,
    SteeringEstimator,
)

MS = 1_000_000
ROOT = Path(__file__).resolve().parents[2]


def measurement(camera, ns, pixel, valid=True):
    return SensorMeasurement(camera, 1, ns, pixel, valid, "high")


def make_estimator(timeout_ms=1000):
    return SteeringEstimator(
        SensorCalibration("top", 0.1, 1000), SensorCalibration("bottom", 0.1, 1000), 230.0, timeout_ms
    )


class EstimatorTests(unittest.TestCase):
    def test_angle_matches_geometry(self):
        est = make_estimator()
        est.ingest(measurement("top", 0, 1000))
        est.ingest(measurement("bottom", 0, 1100))
        result = est.evaluate(10 * MS)
        self.assertTrue(result.valid)
        self.assertAlmostEqual(result.angle_degrees, math.degrees(math.atan2(10.0, 230.0)))
        self.assertAlmostEqual(result.top_age_ms, 10.0)

    def test_missing_then_timeout_reasons(self):
        est = make_estimator(timeout_ms=100)
        self.assertEqual(est.evaluate(0).reason, "top_missing")
        est.ingest(measurement("top", 0, 1000))
        self.assertEqual(est.evaluate(0).reason, "bottom_missing")
        est.ingest(measurement("bottom", 0, 1000))
        self.assertTrue(est.evaluate(50 * MS).valid)
        est.ingest(measurement("bottom", 90 * MS, 1000))
        self.assertEqual(est.evaluate(150 * MS).reason, "top_timeout")
        est.ingest(measurement("top", 150 * MS, 1000))
        self.assertTrue(est.evaluate(160 * MS).valid)
        self.assertEqual(est.evaluate(300 * MS).reason, "top_timeout")

    def test_invalid_frame_keeps_last_valid(self):
        est = make_estimator()
        est.ingest(measurement("top", 0, 1000))
        est.ingest(measurement("bottom", 0, 1000))
        est.ingest(measurement("top", 5 * MS, None, valid=False))
        result = est.evaluate(10 * MS)
        self.assertTrue(result.valid)
        self.assertAlmostEqual(result.top_age_ms, 10.0)

    def test_unknown_camera_rejected(self):
        with self.assertRaises(ValueError):
            make_estimator().ingest(measurement("other", 0, 1))


def result(angle=None, reason="none"):
    from lism_steering.steering import SteeringResult
    return SteeringResult(angle is not None, angle, None, None, None, None, reason)


class SendPolicyTests(unittest.TestCase):
    def setUp(self):
        self.policy = SendPolicy(deadband_deg=0.1, min_interval_ms=20, keepalive_ms=100)

    def send(self, res, ms):
        decision = self.policy.should_send(res, ms * MS)
        if decision:
            self.policy.mark_sent(res, ms * MS)
        return decision

    def test_first_always_sends(self):
        self.assertTrue(self.send(result(1.0), 0))

    def test_inside_deadband_is_quiet_until_keepalive(self):
        self.send(result(1.0), 0)
        self.assertFalse(self.send(result(1.05), 30))
        self.assertFalse(self.send(result(1.05), 99))
        self.assertTrue(self.send(result(1.05), 100))

    def test_change_beyond_deadband_sends_but_rate_limited(self):
        self.send(result(1.0), 0)
        self.assertFalse(self.send(result(2.0), 10))
        self.assertTrue(self.send(result(2.0), 20))
        self.assertFalse(self.send(result(2.5), 30))

    def test_slow_drift_eventually_sends(self):
        self.send(result(1.0), 0)
        sent = [self.send(result(1.0 + 0.03 * i), 25 * i) for i in range(1, 6)]
        self.assertEqual(sent, [False, False, False, True, False])

    def test_validity_and_reason_changes_send_immediately(self):
        self.send(result(1.0), 0)
        self.assertTrue(self.send(result(None, "top_timeout"), 1))
        self.assertFalse(self.send(result(None, "top_timeout"), 2))
        self.assertTrue(self.send(result(None, "bottom_timeout"), 3))
        self.assertTrue(self.send(result(1.0), 4))


class ProtocolTests(unittest.TestCase):
    def test_wire_format_matches_portenta_spec(self):
        line = json.dumps(encode_steering(7, True, 1.23456), separators=(",", ":"))
        self.assertEqual(line, '{"action":"steer","seq":7,"angle":1.235,"offset_mm":0.0,"valid":1}')
        lost = json.dumps(encode_steering(8, False, None), separators=(",", ":"))
        self.assertEqual(lost, '{"action":"steer","seq":8,"angle":0.0,"offset_mm":0.0,"valid":0}')
        self.assertEqual(encode_steering(9, True, None)["valid"], 0)

    def test_messages_fit_two_chunks(self):
        for message in (encode_steering(4294967295, True, -12.3456789, -11.3456789),
                        encode_steering(4294967295, False, None)):
            self.assertLess(len(json.dumps(message, separators=(",", ":"))) + 1, 128)


class ConfigTests(unittest.TestCase):
    def test_shipped_config_loads(self):
        settings = Settings.load(ROOT / "laser_steering_config.json")
        self.assertEqual(settings.camera("top").serial_number, "160155")
        self.assertEqual(settings.sensor_settings.bias, 282)
        self.assertEqual(settings.detector.minimum_active_coverage, 0.35)
        self.assertTrue(settings.library_path.exists())

    def test_unknown_keys_rejected(self):
        data = json.loads((ROOT / "laser_steering_config.json").read_text())
        for section, key in (("detector", "typo"), ("output", "typo"), (None, "typo")):
            bad = json.loads(json.dumps(data))
            (bad if section is None else bad[section])[key] = 1
            with tempfile.TemporaryDirectory() as tmp:
                path = Path(tmp) / "c.json"
                path.write_text(json.dumps(bad))
                with self.assertRaises(ValueError, msg=f"{section}.{key}"):
                    Settings.load(path)

    def test_requires_one_top_one_bottom(self):
        data = json.loads((ROOT / "laser_steering_config.json").read_text())
        data["cameras"][1]["steering_role"] = "top"
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "c.json"
            path.write_text(json.dumps(data))
            with self.assertRaises(ValueError):
                Settings.load(path)


if __name__ == "__main__":
    unittest.main()
