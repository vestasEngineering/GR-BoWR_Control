"""Supervisor for the LISM steering worker, owned by the GR-BoWR event loop.

The worker (lism_steering.worker) runs in its own process so USB reads and
numpy work cannot add jitter to the asyncio loop. This module:

* spawns, watches and restarts the worker,
* turns the worker's steering events into sequence-numbered messages and writes
  them through SerialServer (the single owner of the Portenta UART),
* keeps the Portenta informed (valid=false) whenever the worker is down,
  starting or silent,
* publishes a status summary to the HMI.

When a dedicated steering UART exists, replace ``SerialServer.send_steering``
as the sink here (or move the UART write into the worker); nothing else in the
pipeline depends on which port is used.
"""
from __future__ import annotations

import asyncio
import json
import sys
from pathlib import Path
from typing import Callable, Optional

from lism_steering.config import Settings
from lism_steering.protocol import encode_steering

PROJECT_ROOT = Path(__file__).resolve().parent
DEFAULT_CONFIG = PROJECT_ROOT / "laser_steering_config.json"
WATCHDOG_PERIOD_S = 0.05
HEALTHY_RUN_S = 30.0


class LaserSteeringService:
    def __init__(
        self,
        logger,
        queues,
        serial_server,
        hmi_connected: Callable[[], bool] = lambda: False,
        config_path: Path | str = DEFAULT_CONFIG,
    ):
        self.logger = logger
        self.responses = queues.responses
        self.serial_server = serial_server
        self.hmi_connected = hmi_connected
        self.config_path = Path(config_path)
        self.settings: Optional[Settings] = None

        self._stop_event = asyncio.Event()
        self._tx_event = asyncio.Event()
        self._pending: Optional[tuple[bool, Optional[float], str]] = None
        self._sequence = 0
        self._proc: Optional[asyncio.subprocess.Process] = None
        self._tasks: list[asyncio.Task] = []

        self.state = "stopped"
        self.error: Optional[str] = None
        self.restarts = 0
        self.tx_sent = 0
        self.tx_dropped = 0
        self.latest_steer: Optional[dict] = None
        self.worker_status: Optional[dict] = None
        self._last_worker_msg: Optional[float] = None
        self._last_fallback_offer = 0.0
        self._tx_failing = False

    # ------------------------------------------------------------------ status
    def status(self) -> dict:
        steer = self.latest_steer or {}
        return {
            "type": "laser_steering_status",
            "state": self.state,
            "error": self.error,
            "valid": bool(steer.get("valid")),
            "angle_deg": steer.get("angle"),
            "reason": steer.get("reason", self.state),
            "worker_restarts": self.restarts,
            "tx_sent": self.tx_sent,
            "tx_dropped": self.tx_dropped,
            "sensors": (self.worker_status or {}).get("sensors"),
        }

    def _publish_status(self) -> None:
        if not self.hmi_connected():
            return
        try:
            self.responses.put_nowait(self.status())
        except asyncio.QueueFull:
            pass

    def _set_state(self, state: str, error: Optional[str] = None) -> None:
        changed = state != self.state
        self.state = state
        if error is not None or state == "running":
            self.error = error
        if changed:
            self.logger.log.info(f"laser steering state: {state}" + (f" ({error})" if error else ""))
            self._publish_status()

    # ---------------------------------------------------------------- transmit
    def _offer(self, valid: bool, angle: Optional[float], reason: str) -> None:
        self._pending = (valid, angle, reason)
        self._tx_event.set()

    async def _tx_loop(self) -> None:
        while True:
            await self._tx_event.wait()
            self._tx_event.clear()
            pending, self._pending = self._pending, None
            if pending is None:
                continue
            self._sequence = (self._sequence + 1) & 0xFFFFFFFF
            message = encode_steering(self._sequence, pending[0], pending[1])
            try:
                sent = await self.serial_server.send_steering(message)
            except Exception as error:
                sent = False
                if not self._tx_failing:
                    self.logger.log.warning(f"steering write failed: {error}")
            if sent:
                self.tx_sent += 1
                if self._tx_failing:
                    self.logger.log.info("steering transmit resumed")
                self._tx_failing = False
            else:
                self.tx_dropped += 1
                self._tx_failing = True

    async def _watchdog_loop(self) -> None:
        loop = asyncio.get_running_loop()
        silent_s = self.settings.output.worker_silent_timeout_ms / 1000.0
        keepalive_s = self.settings.output.keepalive_ms / 1000.0
        next_status = loop.time() + self.settings.supervisor.status_publish_s
        while True:
            await asyncio.sleep(WATCHDOG_PERIOD_S)
            now = loop.time()
            if self._proc is None:
                reason = "worker_down"
            elif self._last_worker_msg is None:
                reason = "worker_starting"
            elif now - self._last_worker_msg > silent_s:
                reason = "worker_silent"
            else:
                reason = None
            if reason is not None and now - self._last_fallback_offer >= keepalive_s:
                self._last_fallback_offer = now
                self.latest_steer = {"valid": False, "angle": None, "reason": reason}
                self._offer(False, None, reason)
            if now >= next_status:
                next_status = now + self.settings.supervisor.status_publish_s
                self._publish_status()

    # ------------------------------------------------------------------ worker
    def _handle_worker_message(self, message: dict) -> None:
        kind = message.get("k")
        self._last_worker_msg = asyncio.get_running_loop().time()
        if kind == "steer":
            self.latest_steer = message
            self._offer(bool(message.get("valid")), message.get("angle"), str(message.get("reason", "none")))
        elif kind == "status":
            self.worker_status = message
            if message.get("state") == "running":
                self._set_state("running")
        elif kind == "fault":
            self._set_state("fault", str(message.get("error")))

    async def _read_stdout(self, proc) -> None:
        while True:
            line = await proc.stdout.readline()
            if not line:
                return
            try:
                message = json.loads(line)
            except ValueError:
                self.logger.log.warning(f"steering worker stdout: {line[:200]!r}")
                continue
            if isinstance(message, dict):
                self._handle_worker_message(message)

    async def _read_stderr(self, proc) -> None:
        while True:
            line = await proc.stderr.readline()
            if not line:
                return
            self.logger.log.info(f"[steering worker] {line.decode(errors='replace').rstrip()}")

    async def _stop_process(self, proc, graceful: bool) -> None:
        if proc.returncode is not None:
            return
        if graceful and proc.stdin is not None:
            try:
                proc.stdin.close()
            except Exception:
                pass
            try:
                await asyncio.wait_for(proc.wait(), 6.0)
                return
            except asyncio.TimeoutError:
                pass
        proc.terminate()
        try:
            await asyncio.wait_for(proc.wait(), 3.0)
        except asyncio.TimeoutError:
            self.logger.log.error("steering worker ignored SIGTERM; killing")
            proc.kill()
            await proc.wait()

    async def _startup_guard(self, proc) -> None:
        await asyncio.sleep(self.settings.supervisor.startup_grace_s)
        if self.state == "starting" and proc.returncode is None:
            self.logger.log.error("steering worker did not report running; restarting it")
            self.error = "startup timeout"
            await self._stop_process(proc, graceful=False)

    async def _run_worker_once(self) -> float:
        loop = asyncio.get_running_loop()
        started = loop.time()
        self.error = None
        self._set_state("starting")
        self._last_worker_msg = None
        self.worker_status = None
        proc = await asyncio.create_subprocess_exec(
            sys.executable, "-m", "lism_steering.worker", "--config", str(self.config_path),
            cwd=str(PROJECT_ROOT),
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        self._proc = proc
        helpers = [
            asyncio.create_task(self._read_stdout(proc)),
            asyncio.create_task(self._read_stderr(proc)),
            asyncio.create_task(self._startup_guard(proc)),
        ]
        try:
            await proc.wait()
        except asyncio.CancelledError:
            await self._stop_process(proc, graceful=False)
            raise
        finally:
            self._proc = None
            helpers[2].cancel()
            await asyncio.gather(*helpers[:2], return_exceptions=True)
        if not self._stop_event.is_set():
            self._set_state("fault", self.error or f"worker exited with code {proc.returncode}")
        return loop.time() - started

    async def _supervise(self) -> None:
        backoff = self.settings.supervisor.restart_backoff_s
        failures = 0
        while not self._stop_event.is_set():
            try:
                lifetime = await self._run_worker_once()
            except Exception as error:
                self.logger.log.exception("steering worker launch failed")
                self._set_state("fault", str(error))
                lifetime = 0.0
            if self._stop_event.is_set():
                return
            failures = 0 if lifetime >= HEALTHY_RUN_S else failures + 1
            delay = backoff[min(max(failures, 1) - 1, len(backoff) - 1)]
            self.restarts += 1
            self.logger.log.warning(f"steering worker stopped; restarting in {delay:.1f}s")
            try:
                await asyncio.wait_for(self._stop_event.wait(), delay)
            except asyncio.TimeoutError:
                pass

    # --------------------------------------------------------------- lifecycle
    async def run(self) -> None:
        try:
            self.settings = Settings.load(self.config_path)
        except Exception as error:
            self._set_state("config_error", str(error))
            self.logger.log.exception(f"laser steering config invalid: {self.config_path}")
            return
        if not self.settings.enabled:
            self._set_state("disabled")
            self.logger.log.info("laser steering disabled in configuration")
            return

        self._tasks = [
            asyncio.create_task(self._tx_loop(), name="steering-tx"),
            asyncio.create_task(self._watchdog_loop(), name="steering-watchdog"),
        ]
        try:
            await self._supervise()
        finally:
            for task in self._tasks:
                task.cancel()
            await asyncio.gather(*self._tasks, return_exceptions=True)
            self._tasks = []
            self._set_state("stopped")

    async def shutdown(self) -> None:
        self._stop_event.set()
        proc = self._proc
        if proc is not None:
            await self._stop_process(proc, graceful=True)
