import glob
import fnmatch
import serial
import json
import asyncio
from typing import List, Optional
from logger import Logger
from queues import Queues
from robot_list import (
    load_robot_list, get_defaults, get_transitions,
    thresholds_for_mcu, triggers_from_thresholds
)


class SerialServer:
    def __init__(self, logger: Logger, queues: Queues, device: str = "/dev/ttyAMA0"):
        self.logger = logger
        self.device = device
        # Queues (unchanged)
        self.mcu_reads = queues.mcu_reads
        self.mcu_writes = queues.mcu_writes
        self.distance = queues.distance
        self.feedback_reads = queues.feedbackSignals
        self.encoder_distance = queues.encoder_distance
        self.trigger_acks = queues.trigger_acks
        self.encoder_acks = queues.encoder_acks
        self.mcu_ready = queues.mcu_ready
        self.h7_runtime_ready = queues.h7_runtime_ready
        self.ultrasonic_dbg = queues.ultrasonic_dbg
        self.ultrasonic_log_path = self._build_ultrasonic_log_path()
        self.feedforward_acks = queues.feedforward_acks
        self.motor_direction_acks = queues.motor_direction_acks
        self.process_start_acks = queues.process_start_acks
        self.drive_direction_acks = queues.drive_direction_acks
        self.drive_direction_test_acks = queues.drive_direction_test_acks
        self.drive_direction_test_results = queues.drive_direction_test_results

        # State
        self.last_andon_code: Optional[int] = None
        self.mcu: Optional[serial.Serial] = None
        self.send_task: Optional[asyncio.Task] = None
        self.receive_task: Optional[asyncio.Task] = None
        self.heartbeat_task: Optional[asyncio.Task] = None
        self._stopping = False
        self._last_tx = 0.0
        self._last_actuator_status = None

        # RX rolling buffer for brace-balanced extraction (B)
        self._rx_buf: str = ""

        # Runtime startup requests are queued per serial session only after
        # the H7 publishes runtime_ready.

    def log_ultrasonic(self, msg):
        import os, csv

        log_file = self.ultrasonic_log_path

        os.makedirs(os.path.dirname(log_file), exist_ok=True)

        file_exists = os.path.isfile(log_file)

        with open(log_file, mode='a', newline='') as f:

            if not file_exists:
                f.write("# Ultrasonic debug log\n")
                f.write(f"# File: {log_file}\n")
                f.write("\n")

            writer = csv.writer(f)

            if not file_exists:
                writer.writerow([
                    "t_ms",
                    "meas_mm",
                    "filt_mm",
                    "sp_mm",
                    "err_mm",
                    "target_ms",
                    "process_ms",
                    "state",
                    "bad",
                    "qpps0",
                    "qpps1",
                    "qpps2",
                    "qpps3"
                ])

            qpps = msg.get("qpps", [0, 0, 0, 0])

            writer.writerow([
                msg.get("t_ms"),
                msg.get("meas_mm"),
                msg.get("filt_mm"),
                msg.get("sp_mm"),
                msg.get("err_mm"),
                msg.get("target_ms"),
                msg.get("process_ms"),
                msg.get("state"),
                msg.get("bad"),
                qpps[0],
                qpps[1],
                qpps[2],
                qpps[3],
            ])

    # --------------------------
    # Port management
    # --------------------------            
    def detect_serial(self, preferred_list: Optional[List[str]] = None) -> List[str]:
        if preferred_list is None:
            preferred_list = []
        return [self.device]
    
    def connect_serial(self, available_ports: List[str]) -> Optional[serial.Serial]:
        """Open the first available port. Returns serial.Serial or None."""
        if not available_ports:
            self.logger.log.info("No serial ports available.")
            return None
        try:
            dev = available_ports[0]
            # Keep 115200 to match MCU. timeout keeps read() bounded.
            connected_device = serial.Serial(dev, 115200, timeout=0.2)
            if connected_device.is_open:
                self.logger.log.info(f"serial connected to {dev}")
                self.logger.log.info("MCU connection established")
                return connected_device
            else:
                self.logger.log.error("Serial port failed to open (unknown reason).")
                return None
        except Exception as e:
            self.logger.log.info(f"connect_serial error: {e}")
            return None

    def clear_serial(self):
        """Clear only TX buffer if port is open; preserve RX so startup messages aren't lost."""
        if self.mcu and getattr(self.mcu, "is_open", False):
            try:
                self.mcu.reset_output_buffer()
            except Exception as e:
                self.logger.log.error(f"Error clearing serial buffers: {e}")


    def close_serial(self):
        """Close port if open and null the handle."""
        if self.mcu:
            try:
                if getattr(self.mcu, "is_open", False):
                    try:
                        self.mcu.reset_input_buffer()
                        self.mcu.reset_output_buffer()
                    except Exception:
                        pass
                    self.mcu.close()
                    self.logger.log.info("Serial connection closed.")
                else:
                    self.logger.log.debug("close_serial: port already closed.")
            except Exception as e:
                self.logger.log.error(f"Error closing serial: {e}")
            finally:
                self.mcu = None

    # --------------------------
    # Lifecycle
    # --------------------------
    async def run(self):
        """
        Auto-reconnect loop:

            connect
            publish restored boundary
            run send/receive/heartbeat
            publish unexpected loss boundary
            clean up
            retry
        """
        backoff = 1.0

        while not self._stopping:
            # 1) Detect and connect.
            ports = self.detect_serial()
            self.mcu = self.connect_serial(ports)

            if not self.mcu:
                self.logger.log.info(
                    f"No device. Retry in {backoff:.1f}s..."
                )

                await asyncio.sleep(backoff)
                backoff = min(
                    backoff * 1.5,
                    10.0,
                )
                continue

            # A serial port has been opened successfully.
            backoff = 1.0
            self._rx_buf = ""
            self.mcu_ready.clear()
            self.h7_runtime_ready.clear()

            self.encoder_runtime_events = asyncio.Queue()

            # Publish exactly one connection-restored boundary for this
            # serial session. WebsocketServer.health_pump() will persist
            # it against the active job and forward it to the HMI.
            await self.mcu_reads.put({
                "type": "cm5_transport_event",
                "event": "mcu_serial_connection_restored",
                "device": self.device,
            })

            self.logger.log.info(
                "Published MCU serial connection-restored event: "
                f"device={self.device}"
            )

            # 2) Spawn communication tasks.
            self.send_task = asyncio.create_task(
                self.send(),
                name="mcu-serial-send",
            )

            self.receive_task = asyncio.create_task(
                self.receive(),
                name="mcu-serial-receive",
            )

            runtime_startup_task = asyncio.create_task(
                self.queue_runtime_startup_requests(),
                name="mcu-runtime-startup-requests",
            )

            await asyncio.sleep(1.0)

            if self._stopping:
                # Shutdown might have been requested during the startup delay.
                pending = {
                    task
                    for task in (
                        self.send_task,
                        self.receive_task,
                        runtime_startup_task,
                    )
                    if task is not None
                    and not task.done()
                }

                for task in pending:
                    task.cancel()

                if pending:
                    await asyncio.gather(
                        *pending,
                        return_exceptions=True,
                    )

                self.mcu_ready.clear()
                self.h7_runtime_ready.clear()
                self.close_serial()
                break

            self.heartbeat_task = asyncio.create_task(
                self.heartbeat(period_s=1.5),
                name="mcu-serial-heartbeat",
            )

            # Only long-running communication tasks define the lifetime of
            # the serial session. runtime_startup_task is intentionally not
            # included because it is a one-shot task that exits normally after
            # queueing startup requests. Treating its normal completion as a
            # transport failure caused the serial port to reconnect immediately.
            session_tasks = {
                self.send_task,
                self.receive_task,
                self.heartbeat_task,
            }

            # 3) Wait for a long-running communication task to exit.
            done, pending = await asyncio.wait(
                session_tasks,
                return_when=asyncio.FIRST_COMPLETED,
            )

            # Capture the reason before cancelling the remaining tasks.
            loss_details = {
                "type": "cm5_transport_event",
                "event": "mcu_serial_connection_lost",
                "device": self.device,
            }

            completed_task_names = []

            for task in done:
                completed_task_names.append(
                    task.get_name()
                )

                if task.cancelled():
                    continue

                try:
                    error = task.exception()
                except asyncio.CancelledError:
                    error = None

                if error is not None:
                    loss_details["reason"] = str(error)
                    loss_details["failed_task"] = (
                        task.get_name()
                    )
                    break

            if completed_task_names:
                loss_details["completed_tasks"] = (
                    completed_task_names
                )

            # 4) Cancel tasks that belong to the failed connection. The
            # one-shot runtime startup task is cleaned up here but does not
            # define the lifetime of the serial session.
            if not runtime_startup_task.done():
                pending.add(runtime_startup_task)

            for task in pending:
                task.cancel()

            if pending:
                await asyncio.gather(
                    *pending,
                    return_exceptions=True,
                )

            # Retrieve results from completed tasks to avoid an unhandled
            # task-exception warning if their implementation changes and
            # allows an exception to propagate.
            if done:
                await asyncio.gather(
                    *done,
                    return_exceptions=True,
                )

            # Publish one loss boundary for this serial session. Do this
            # before closing the handle and only for an unexpected exit.
            if not self._stopping:
                await self.mcu_reads.put(
                    loss_details
                )

                self.logger.log.warning(
                    "Published MCU serial connection-lost event: "
                    f"device={self.device} "
                    f"completed_tasks={completed_task_names} "
                    f"reason={loss_details.get('reason')}"
                )

            self.mcu_ready.clear()
            self.h7_runtime_ready.clear()
            self.close_serial()

            # Clear references to tasks from the completed session.
            self.send_task = None
            self.receive_task = None
            self.heartbeat_task = None

            # Brief pause before reconnecting.
            if not self._stopping:
                await asyncio.sleep(1.0)

        self.logger.log.info(
            "SerialServer.run exiting (stopping=True)."
        )

    # --------------------------
    # Tasks
    # --------------------------
    async def heartbeat(
        self,
        period_s: float = 1.0,
    ):
        try:
            await self.h7_runtime_ready.wait()

            while not self._stopping:
                if (
                    not self.mcu
                    or not getattr(
                        self.mcu,
                        "is_open",
                        False,
                    )
                ):
                    break

                if not self.h7_runtime_ready.is_set():
                    break

                if self.mcu_writes.empty():
                    await self.mcu_writes.put({
                        "hb": 1,
                    })

                await asyncio.sleep(
                    period_s
                )

        except asyncio.CancelledError:
            raise

    def _encode_line(self, msg: dict) -> bytes:
        return (json.dumps(msg, separators=(',', ':')) + '\n').encode('utf-8')

    async def send(self):
        """Send queued commands only to the current ready H7 serial session.

        Expired jog leases are discarded. Transient motion commands are never
        requeued after a serial write failure, preventing replay after reconnect.
        Private CM5 metadata keys beginning with an underscore are removed before
        UART serialization.
        """
        try:
            while True:
                if not self.mcu or not getattr(self.mcu, "is_open", False):
                    self.logger.log.info("Serial port is closed. Exiting send loop.")
                    break

                if not self.h7_runtime_ready.is_set():
                    try:
                        await asyncio.wait_for(
                            self.h7_runtime_ready.wait(),
                            timeout=1.0,
                        )
                    except asyncio.TimeoutError:
                        continue

                if not self.mcu or not getattr(self.mcu, "is_open", False):
                    break

                msg = await self.mcu_writes.get()

                if not self.h7_runtime_ready.is_set():
                    if not msg.get("_transient_motion", False):
                        await self.mcu_writes.put(msg)
                    continue

                expires_at = msg.get("_expires_monotonic")
                if expires_at is not None:
                    now = asyncio.get_running_loop().time()
                    if now >= float(expires_at):
                        self.logger.log.debug(
                            "Dropping expired transient motion command: "
                            f"action={msg.get('action')} seq={msg.get('seq')}"
                        )
                        continue

                wire_msg = {
                    key: value
                    for key, value in msg.items()
                    if not str(key).startswith("_")
                }

                self.logger.log.info(f"PI TX: {wire_msg}")

                try:
                    raw = self._encode_line(
                        wire_msg
                    )

                    self.logger.log.info(
                        "PI TX: "
                        f"bytes={len(raw)} "
                        f"action={wire_msg.get('action')} "
                        f"run_id={wire_msg.get('run_id')} "
                        f"transaction_id="
                        f"{wire_msg.get('transaction_id')} "
                        f"payload={wire_msg}"
                    )

                    if len(raw) > 240:
                        chunk_size = 32
                        inter_chunk_delay_s = 0.006
                    else:
                        chunk_size = 64
                        inter_chunk_delay_s = 0.003

                    await self._write_serial_frame(
                        raw,
                        chunk_size=chunk_size,
                        inter_chunk_delay_s=(
                            inter_chunk_delay_s
                        ),
                    )

                    self._last_tx = (
                        asyncio.get_running_loop().time()
                    )

                except (serial.SerialException, serial.SerialTimeoutException) as error:
                    self.logger.log.error(
                        "Serial write error: "
                        f"{error}. Will reconnect."
                    )

                    if not msg.get("_transient_motion", False):
                        try:
                            self.mcu_writes.put_nowait(msg)
                        except Exception:
                            pass
                    else:
                        self.logger.log.warning(
                            "Discarded transient motion command after serial failure: "
                            f"action={msg.get('action')} seq={msg.get('seq')}"
                        )
                    break

                except Exception as error:
                    self.logger.log.error(f"Unexpected send error: {error}")

                    if not msg.get("_transient_motion", False):
                        try:
                            self.mcu_writes.put_nowait(msg)
                        except Exception:
                            pass
                    else:
                        self.logger.log.warning(
                            "Discarded transient motion command after send failure: "
                            f"action={msg.get('action')} seq={msg.get('seq')}"
                        )
                    break

                await asyncio.sleep(0.03)

        except asyncio.CancelledError:
            self.logger.log.info("Send task cancelled.")
            raise


    # --------------------------
    # Robust RX: brace-balanced extraction (B)
    # --------------------------
    def _extract_json_objects(self):
        """Yield complete JSON objects from self._rx_buf via brace matching.
        Leaves incomplete tails in the buffer for next read.
        """
        outs = []
        buf = self._rx_buf
        start = buf.find('{')
        while start != -1:
            depth = 0
            end = -1
            for i, ch in enumerate(buf[start:], start):
                if ch == '{':
                    depth += 1
                elif ch == '}':
                    depth -= 1
                    if depth == 0:
                        end = i
                        break
            if end != -1:
                outs.append(buf[start:end + 1])
                buf = buf[end + 1:]
                start = buf.find('{')
            else:
                # Incomplete JSON; keep from 'start' onward and break
                buf = buf[start:]
                break
        self._rx_buf = buf
        return outs
    
    def _extract_framed_messages(self):
        """
        Extract complete <...> frames from self._rx_buf.
        Leaves incomplete tail in the buffer.
        """
        outs = []
        buf = self._rx_buf

        while True:
            start = buf.find('<')
            if start == -1:
                # No frame start at all; drop garbage
                buf = ""
                break

            end = buf.find('>', start + 1)
            if end == -1:
                # Incomplete frame; keep from start onward
                buf = buf[start:]
                break

            outs.append(buf[start + 1:end])  # content inside < >
            buf = buf[end + 1:]

        self._rx_buf = buf
        return outs

    def _build_ultrasonic_log_path(self):
        import os

        base_log = getattr(self.logger, "lf", None)

        if not base_log:
            return os.path.join("logs", "ultrasonic_fallback.csv")

        base_no_ext = os.path.splitext(base_log)[0]
        return base_no_ext + "_ultrasonic.csv"

    async def receive(self):
        """Read newline-delimited JSON messages from MCU until disconnect."""
        try:
            self.logger.log.info("Receive loop started.")
            while True:
                if not self.mcu or not getattr(self.mcu, "is_open", False):
                    self.logger.log.info("Serial port is closed. Exiting receive loop.")
                    break

                raw = b""
                try:
                    raw = await asyncio.to_thread(self.mcu.readline)

                    if not raw:
                        await asyncio.sleep(0.01)
                        continue

                    #self.logger.log.info(f"RX raw bytes: {raw!r}")

                    text = raw.decode("utf-8", errors="replace").strip()
                    if not text:
                        continue

                    #self.logger.log.info(f"RX text: {text}")

                    try:
                        msg_dict = json.loads(text)

                        # Any valid JSON response proves that this H7
                        # serial session is operational.
                        self.mcu_ready.set()

                        if msg_dict.get("type") not in (
                            "encoder",
                            "ultrasonic_dbg",
                            "actuator_status",
                            "hmi_connection_status",
                        ):
                            self.logger.log.debug(
                                "MCU RX: "
                                f"{json.dumps(msg_dict, separators=(',', ':'))}"
                            )

                    except json.JSONDecodeError as e:
                        self.logger.log.error(f"JSON decode error: {e} - Raw line: {text!r}")
                        continue

                    # ---- Dispatch ----
                    if 'distance' in msg_dict:
                        await self.distance.put(msg_dict['distance'])

                    elif 'feedback' in msg_dict:
                        await self.feedback_reads.put(msg_dict['feedback'])

                    elif msg_dict.get("type") == "fw_version":
                        await self.mcu_reads.put(msg_dict)

                    elif 'encoder_distance' in msg_dict:
                        await self.encoder_distance.put(msg_dict['encoder_distance'])

                    elif msg_dict.get('status', '').lower() in ("encoder reset", "all encoders reset"):
                        await self.encoder_distance.put(msg_dict)

                    elif msg_dict.get('status', '').lower() == "light_updated":
                        self.logger.log.info(f"Andon light updated to: {msg_dict.get('state')}")

                    elif msg_dict.get("type") == "serial_rx_error":
                        self.logger.log.error(
                            "H7 serial receive error: "
                            f"error={msg_dict.get('error')} "
                            f"code={msg_dict.get('code')} "
                            f"input_length="
                            f"{msg_dict.get('input_length')} "
                            f"buffer_capacity="
                            f"{msg_dict.get('buffer_capacity')} "
                            f"json_capacity="
                            f"{msg_dict.get('json_capacity')} "
                            f"ts_ms={msg_dict.get('ts_ms')}"
                        )

                        await self.mcu_reads.put(
                            msg_dict
                        )

                    elif msg_dict.get("status", "").lower() in (
                        "triggers_loaded",
                        "triggers_reconciled",
                        "trigger_load_failed",
                    ):
                        await self.trigger_acks.put(msg_dict)

                        self.logger.log.info(
                            "Trigger acknowledgement received: "
                            f"status={msg_dict.get('status')} "
                            f"ok={msg_dict.get('ok')} "
                            f"count={msg_dict.get('count')} "
                            f"position_mm={msg_dict.get('position_mm')}"
                        )

                    elif msg_dict.get("trigger_reached"):
                        self.logger.log.info(
                            f"Trigger reached on channel {msg_dict.get('channel')} "
                            f"at value {msg_dict.get('value')}"
                        )

                    elif msg_dict.get("trigger_deactivated"):
                        self.logger.log.info(
                            f"Trigger deactivated on channel {msg_dict.get('channel')}"
                        )

                    elif msg_dict.get("status", "").lower() == "shutdown_complete":
                        self.logger.log.info("Shutdown confirmed by H7.")


                    elif 'andon_diag' in msg_dict:
                        diag = msg_dict['andon_diag']
                        code = diag.get('code')
                        state = diag.get('state')
                        override = diag.get('override')
                        reasons = diag.get('reasons', {})
                        faults = diag.get('faults', [])
                        fmods = diag.get('fault_modules', [])

                        if code != self.last_andon_code:
                            self.last_andon_code = code

                        await self.mcu_reads.put({'type': 'andon_diag', **diag})
                        self.logger.log.info(
                            f"Andon diag → state={state} code={code} override={override} "
                            f"reasons={reasons} faults={faults} fault_modules={fmods} "
                            f"keys={list(diag.keys())}"
                        )

                    elif (
                        msg_dict.get("type") == "status"
                        and msg_dict.get("module") == "runtime"
                        and msg_dict.get("status") == "runtime_ready"
                    ):
                        self.h7_runtime_ready.set()
                        await self.mcu_reads.put(msg_dict)

                        self.logger.log.info(
                            "H7 runtime ready. The firmware setup sequence "
                            "has completed and normal command processing "
                            "is available."
                        )

                    elif msg_dict.get("type") == "boot_health":
                        await self.mcu_reads.put(msg_dict)

                        self.logger.log.info(
                            f"Boot health ok={msg_dict.get('ok')} "
                            f"checks="
                            f"{list((msg_dict.get('checks') or {}).keys())}"
                        )

                    elif msg_dict.get("type") == "test_result":
                        await self.mcu_reads.put(msg_dict)

                    elif msg_dict.get("type") == "ultrasonic_dbg":
                        await self.mcu_reads.put(msg_dict)
                        await self.ultrasonic_dbg.put(msg_dict)      
                        self.log_ultrasonic(msg_dict)


                    elif msg_dict.get("type") == "encoder":
                        await self.mcu_reads.put(msg_dict)

                    elif msg_dict.get("type") == "encoder_session":
                        await self.mcu_reads.put(msg_dict)

                    elif msg_dict.get("type") in ("encoder_reset", "encoder_set"):
                        await self.encoder_acks.put(msg_dict)
                        await self.mcu_reads.put(msg_dict)

                    elif (
                        msg_dict.get("type")
                        == "process_start_ack"
                    ):
                        await self.process_start_acks.put(
                            msg_dict
                        )

                        await self.mcu_reads.put(
                            msg_dict
                        )

                        self.logger.log.info(
                            "Process start acknowledgement: "
                            f"ok={msg_dict.get('ok')} "
                            f"accepted="
                            f"{msg_dict.get('accepted')} "
                            f"state={msg_dict.get('state')} "
                            f"transaction_id="
                            f"{msg_dict.get('transaction_id')} "
                            f"error={msg_dict.get('error')}"
                        )

                    elif msg_dict.get("type") == "process_status":
                        self.logger.log.info(
                            "Process status: "
                            f"state={msg_dict.get('state')} "
                            f"active={msg_dict.get('active')} "
                            f"reason={msg_dict.get('reason')} "
                            f"ts_ms={msg_dict.get('ts_ms')}"
                        )

                        await self.mcu_reads.put(msg_dict)

                    elif msg_dict.get("type") == "feedforward_configuration_ack":
                        await self.feedforward_acks.put(msg_dict)
                        self.logger.log.info(
                            "Feedforward acknowledgement: "
                            f"ok={msg_dict.get('ok')} "
                            f"transaction_id="
                            f"{msg_dict.get('transaction_id')} "
                            f"error={msg_dict.get('error')} "
                            f"ff={msg_dict.get('ff')}"
                        )

                    elif msg_dict.get("type") == "drive_direction_configuration_ack":
                        await self.drive_direction_acks.put(msg_dict)
                        self.logger.log.info(
                            "Drive direction configuration acknowledgement: "
                            f"ok={msg_dict.get('ok')} "
                            f"transaction_id={msg_dict.get('transaction_id')} "
                            f"error={msg_dict.get('error')} "
                            f"motor={msg_dict.get('motor_directions')} "
                            f"encoder={msg_dict.get('encoder_directions')}"
                        )

                    elif msg_dict.get("type") == "drive_direction_test_ack":
                        await self.drive_direction_test_acks.put(msg_dict)

                    elif msg_dict.get("type") == "drive_direction_test_result":
                        await self.drive_direction_test_results.put(msg_dict)

                    elif (
                        msg_dict.get("type")
                        == "motor_direction_ack"
                    ):
                        await self.motor_direction_acks.put(
                            msg_dict
                        )

                        self.logger.log.info(
                            "Motor direction acknowledgement: "
                            f"ok={msg_dict.get('ok')} "
                            f"transaction_id="
                            f"{msg_dict.get('transaction_id')} "
                            f"error={msg_dict.get('error')} "
                            f"directions="
                            f"{msg_dict.get('directions')}"
                        )

                    # ------------------------------------------------------
                    # SSv Glue Card / actuator telemetry
                    # ------------------------------------------------------
                    elif msg_dict.get("type") == "actuator_status":

                        status_key = (
                            msg_dict.get("commanded_mask"),
                            msg_dict.get("feedback_mask"),
                            msg_dict.get("jam_mask"),
                            msg_dict.get("pcb_fault"),
                        )

                        if status_key != self._last_actuator_status:
                            self._last_actuator_status = status_key

                            self.logger.log.info(
                                "Actuator state changed: "
                                f"commanded_mask={msg_dict.get('commanded_mask')} "
                                f"feedback_mask={msg_dict.get('feedback_mask')} "
                                f"jam_mask={msg_dict.get('jam_mask')} "
                                f"pcb_fault={msg_dict.get('pcb_fault')}"
                            )

                        await self.mcu_reads.put(msg_dict)

                    # ------------------------------------------------------
                    # Temporary firmware diagnostics
                    # ------------------------------------------------------
                    elif msg_dict.get("type") in (
                        "firmware_features",
                        "main_loop_heartbeat",
                        "loop_checkpoint",
                    ):
                        self.logger.log.info(
                            f"MCU diagnostic: {msg_dict}"
                        )

                        await self.mcu_reads.put(msg_dict)

                    else:
                        await self.mcu_reads.put(msg_dict)

                except serial.SerialException as e:
                    self.logger.log.error(f"Serial exception: {e} - Raw data preview: {raw!r}")
                    break
                except Exception as e:
                    self.logger.log.error(f"Error parsing serial data: {e} - Raw data preview: {raw!r}")

                await asyncio.sleep(0)

        except asyncio.CancelledError:
            self.logger.log.info("Receive task cancelled.")


    # --------------------------
    # Shutdown helpers
    # --------------------------
    async def send_shutdown(self):
        if self.mcu and getattr(self.mcu, "is_open", False):
            self.logger.log.info("Sending shutdown command to H7...")
            try:
                raw = self._encode_line({"action": "shutdown"})
                self.mcu.write(raw)
                self.mcu.flush()
                await asyncio.sleep(0.5)
            except Exception as e:
                self.logger.log.error(f"Error sending shutdown: {e}")

    async def shutdown(self):
        """Stop auto-reconnect loop and close port cleanly."""
        self._stopping = True
        self.logger.log.info("SerialServer shutting down...")
        try:
            await self.mcu_writes.put({"action": "shutdown"})
            await asyncio.sleep(0.5)
        except Exception:
            pass

        for t in (self.send_task, self.receive_task, self.heartbeat_task):
            if t and not t.done():
                t.cancel()
        # Best-effort gather
        try:
            await asyncio.gather(
                *(t for t in (self.send_task, self.receive_task, self.heartbeat_task) if t),
                return_exceptions=True
            )
        except Exception:
            pass

        self.mcu_ready.clear()
        self.h7_runtime_ready.clear()
        self.close_serial()

    async def queue_runtime_startup_requests(
        self,
    ) -> None:
        """Queue one-time requests after the H7 can process commands."""
        await self.h7_runtime_ready.wait()

        await self.mcu_writes.put({
            "speed0": 0,
            "speed1": 0,
            "speed2": 0,
            "speed3": 0,
        })

        await self.mcu_writes.put({
            "action": "ping",
        })

        await self.mcu_writes.put({
            "action": "get_firmware_features",
        })

    async def _write_serial_frame(
        self,
        raw: bytes,
        *,
        chunk_size: int = 32,
        inter_chunk_delay_s: float = 0.006,
    ) -> None:
        """
        Write one newline-delimited JSON frame in bounded chunks.

        This prevents a long one-shot Linux UART write from overrunning the
        Portenta H7's lower-level UART receive buffering before the firmware
        main loop can drain it.

        The logical frame is not retried or duplicated. The newline remains
        exclusively at the end of the original encoded frame.
        """
        mcu = self.mcu

        if (
            mcu is None
            or not getattr(
                mcu,
                "is_open",
                False,
            )
        ):
            raise serial.SerialException(
                "Serial port is not open."
            )

        if chunk_size <= 0:
            raise ValueError(
                "chunk_size must be greater than zero."
            )

        if inter_chunk_delay_s < 0:
            raise ValueError(
                "inter_chunk_delay_s cannot be negative."
            )

        frame_length = len(raw)

        for offset in range(
            0,
            frame_length,
            chunk_size,
        ):
            chunk = raw[
                offset:
                offset + chunk_size
            ]

            written = mcu.write(chunk)

            if written != len(chunk):
                raise serial.SerialTimeoutException(
                    "Incomplete serial frame write: "
                    f"frame_length={frame_length} "
                    f"offset={offset} "
                    f"expected={len(chunk)} "
                    f"written={written}"
                )

            if (
                offset + len(chunk)
                < frame_length
            ):
                await asyncio.sleep(
                    inter_chunk_delay_s
                )

        mcu.flush()