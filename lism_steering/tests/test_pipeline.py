"""End-to-end: fake vendor SDK -> worker process -> supervisor -> fake serial sink."""
import asyncio
import json
import logging
import os
import subprocess
import sys
import tempfile
import textwrap
import unittest
from unittest import mock
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from laser_steering import LaserSteeringService  # noqa: E402

FAKE_SDK = textwrap.dedent('''
    import os, time
    import numpy as np
    RET_OK = 0
    LISM_ADDR = 0
    CENTERS = {k: int(v) for k, v in (p.split(":") for p in os.environ["FAKE_LISM_CENTERS"].split(","))}
    FAIL_AFTER = int(os.environ.get("FAKE_LISM_FAIL_AFTER", "0"))

    class Device:
        def __init__(self, serial):
            self.serial, self.reads, self.values = serial, 0, {}
        def getpacketlength(self): return 0, 4096
        def getpixelcount(self, a): return 0, 2048
        def getcfg1(self): return 0, 0
        def seteptimeout(self, t): return 0
        def setstate(self, a, v): return 0
        def updateparam(self, a): return 0
        def waitforpipecount(self, n, timeout):
            time.sleep(0.02)
            self.reads += 1
            if FAIL_AFTER and self.reads > FAIL_AFTER:
                return 5, 0
            return 0, n
        def getpipe(self, n):
            frame = np.full(2048, 65535, dtype=">u2")
            c = CENTERS[self.serial]
            frame[c - 20:c + 20] = 100
            return 0, frame.tobytes()
        def close(self): return 0
        def __getattr__(self, name):
            if name.startswith("set"):
                return lambda a, v: self.values.__setitem__(name[3:], v) or 0
            if name.startswith("get"):
                return lambda a: (0, self.values.get({"adcgain": "adcgain"}.get(name[3:], name[3:]), 0))
            raise AttributeError(name)

    class USBLISMPI26:
        def __init__(self, path): pass
        def libversion(self): return 0x0200
        def enumdevices(self): return 2
        def getserialnumber(self, i): return ["160155", "160154"][i]
        def opendevicebyserial_ex(self, serial, pipe, packet): return 0, Device(serial)
        def geterrorstring(self, rc): return "fake error"
        def shutdown(self): return 0
''')

# top is reversed (pixel = 2047 - raw): raw 1047 -> pixel 1000; bottom raw 1100 -> pixel 1100
CENTERS = "160155:1047,160154:1100"
EXPECTED_DEG = 0.3488
STALLED_WORKER = (
    "import json, time; print(json.dumps({'k': 'status', 'state': 'running'}), flush=True); time.sleep(600)"
)


def write_config(directory: Path) -> Path:
    sdk = directory / "sdk"
    sdk.mkdir()
    (sdk / "usblismpi26.py").write_text(FAKE_SDK)
    data = json.loads((ROOT / "laser_steering_config.json").read_text())
    data.update(
        sdk_directory=str(sdk), library_path=str(sdk / "fake.so"),
        configuration_settle_ms=1, settle_frames=2,
    )
    data["logging"]["directory"] = str(directory / "logs")
    data["supervisor"].update(restart_backoff_s=[0.2], startup_grace_s=10)
    path = directory / "config.json"
    path.write_text(json.dumps(data))
    return path


def env(**extra):
    return {**os.environ, "FAKE_LISM_CENTERS": CENTERS, **extra}


class WorkerProcessTests(unittest.TestCase):
    def run_worker(self, config, seconds, **extra):
        with tempfile.TemporaryFile() as out, tempfile.TemporaryFile() as err:
            proc = subprocess.Popen(
                [sys.executable, "-m", "lism_steering.worker", "--config", str(config)],
                cwd=ROOT, env=env(**extra), stdin=subprocess.PIPE, stdout=out, stderr=err,
            )
            try:
                proc.wait(timeout=seconds)
            except subprocess.TimeoutExpired:
                proc.stdin.close()
                proc.wait(timeout=15)
            proc.stdin.close() if not proc.stdin.closed else None
            out.seek(0)
            err.seek(0)
            lines = [json.loads(line) for line in out.read().decode().splitlines()]
            return proc.returncode, lines, err.read().decode()

    def test_streams_angle_and_stops_on_stdin_close(self):
        with tempfile.TemporaryDirectory() as tmp:
            config = write_config(Path(tmp))
            code, lines, err = self.run_worker(config, 2.0)
            self.assertEqual(code, 0, err)
            steer = [m for m in lines if m["k"] == "steer"]
            valid = [m for m in steer if m["valid"]]
            self.assertTrue(valid, lines[:5])
            self.assertAlmostEqual(valid[-1]["angle"], EXPECTED_DEG, delta=0.01)
            # keepalive 100 ms, deadband quiet: far fewer messages than 100 frames/s
            self.assertLess(len(steer), 60)
            self.assertTrue(any(m["k"] == "status" for m in lines))
            rows = (Path(tmp) / "logs" / "laser_steering.csv").read_text().splitlines()
            self.assertTrue(rows[0].startswith("unix_time,"))
            self.assertGreaterEqual(len(rows), 2)

    def test_usb_fault_exits_nonzero_with_fault_message(self):
        with tempfile.TemporaryDirectory() as tmp:
            config = write_config(Path(tmp))
            code, lines, _ = self.run_worker(config, 10.0, FAKE_LISM_FAIL_AFTER="40")
            self.assertEqual(code, 2)
            self.assertEqual(lines[-1]["k"], "fault")


class FakeSerial:
    def __init__(self):
        self.sent = []

    async def send_steering(self, message):
        self.sent.append(message)
        return True


class Queues:
    def __init__(self):
        self.responses = asyncio.Queue(10)


class Log:
    log = logging.getLogger("test")


class SupervisorTests(unittest.IsolatedAsyncioTestCase):
    async def start(self, config, **extra):
        patcher = mock.patch.dict(os.environ, env(**extra))
        patcher.start()
        self.addCleanup(patcher.stop)
        self.serial, self.queues = FakeSerial(), Queues()
        self.service = LaserSteeringService(Log(), self.queues, self.serial, lambda: True, config)
        self.task = asyncio.create_task(self.service.run())

    async def test_forwards_sequenced_messages_and_stops_cleanly(self):
        with tempfile.TemporaryDirectory() as tmp:
            await self.start(write_config(Path(tmp)))
            await asyncio.sleep(3.0)
            self.assertEqual(self.service.state, "running")
            good = [m for m in self.serial.sent if m["valid"] == 1]
            self.assertTrue(good)
            self.assertAlmostEqual(good[-1]["angle"], EXPECTED_DEG, delta=0.01)
            seqs = [m["seq"] for m in self.serial.sent]
            self.assertEqual(seqs, sorted(set(seqs)))
            await self.service.shutdown()
            await asyncio.wait_for(self.task, 10)
            self.assertEqual(self.service.state, "stopped")
            status = self.queues.responses.get_nowait()
            self.assertEqual(status["type"], "laser_steering_status")

    async def test_worker_fault_reports_invalid_and_restarts(self):
        with tempfile.TemporaryDirectory() as tmp:
            await self.start(write_config(Path(tmp)), FAKE_LISM_FAIL_AFTER="60")
            await asyncio.sleep(6.0)
            self.assertGreaterEqual(self.service.restarts, 1)
            self.assertTrue([m for m in self.serial.sent if m["valid"] == 0])
            seqs = [m["seq"] for m in self.serial.sent]
            self.assertEqual(seqs, sorted(set(seqs)))
            await self.service.shutdown()
            await asyncio.wait_for(self.task, 10)


class StalledWorkerTests(unittest.IsolatedAsyncioTestCase):
    async def test_silent_worker_is_killed_and_relaunched(self):
        real_exec = asyncio.create_subprocess_exec
        launches = []

        async def launch(*args, **kwargs):
            launches.append(1)
            return await real_exec(
                sys.executable, "-c", STALLED_WORKER,
                stdin=kwargs["stdin"], stdout=kwargs["stdout"], stderr=kwargs["stderr"],
            )

        with tempfile.TemporaryDirectory() as tmp, mock.patch("asyncio.create_subprocess_exec", launch):
            serial, queues = FakeSerial(), Queues()
            service = LaserSteeringService(Log(), queues, serial, lambda: True, write_config(Path(tmp)))
            task = asyncio.create_task(service.run())
            await asyncio.sleep(8.0)
            await service.shutdown()
            await asyncio.wait_for(task, 15)
        self.assertGreaterEqual(len(launches), 2)
        self.assertGreaterEqual(service.restarts, 1)
        self.assertTrue([m for m in serial.sent if m["valid"] == 0])


if __name__ == "__main__":
    unittest.main()
