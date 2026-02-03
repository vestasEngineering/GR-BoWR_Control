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
    def __init__(self, logger: Logger, queues: Queues):
        self.logger = logger

        # Queues (unchanged)
        self.mcu_reads = queues.mcu_reads
        self.mcu_writes = queues.mcu_writes
        self.distance = queues.distance
        self.feedback_reads = queues.feedbackSignals
        self.encoder_distance = queues.encoder_distance

        # State
        self.last_andon_code: Optional[int] = None
        self.mcu: Optional[serial.Serial] = None
        self.send_task: Optional[asyncio.Task] = None
        self.receive_task: Optional[asyncio.Task] = None
        self.heartbeat_task: Optional[asyncio.Task] = None
        self._stopping = False

        # RX rolling buffer for brace-balanced extraction (B)
        self._rx_buf: str = ""

        # Initial startup messages (queued; will be sent after first connect)
        self.mcu_writes.put_nowait({"start_serial": 1})
        self.mcu_writes.put_nowait({"speed0": 0, "speed1": 0, "speed2": 0, "speed3": 0})
        self.mcu_writes.put_nowait({"action": "reset_encoder"})
        #self.mcu_writes.put_nowait(
        #                            {"action":"test_actuator_map",
        #                            "id":"map_ch0",
        #                            "channel":1,
        #                            "start_V":0.0,
        #                            "end_V":5.0,
        #                            "step_V":0.5,
        #                            "settle_ms":800,
        #                            "avg_samples":5,
        #                            "avg_delay_ms":3})



    # --------------------------
    # Port management
    # --------------------------
    def detect_serial(self, preferred_list: List[str] = ['*']) -> List[str]:
        """Auto-detect serial ports on Linux (/dev/ttyUSB* /dev/ttyACM*).
        Returns ports matching preferred_list first, then everything else.
        """
        glist = glob.glob('/dev/ttyUSB*') + glob.glob('/dev/ttyACM*')
        ret: List[str] = []
        for d in glist:
            for preferred in preferred_list:
                if fnmatch.fnmatch(d, preferred):
                    ret.append(d)
        if ret:
            return ret
        return glist

    def connect_serial(self, available_ports: List[str]) -> Optional[serial.Serial]:
        """Open the first available port. Returns serial.Serial or None."""
        if not available_ports:
            self.logger.log.info("No serial ports available.")
            return None
        try:
            dev = available_ports[0]
            # Keep 115200 to match MCU. timeout keeps read() bounded.
            connected_device = serial.Serial(dev, 115200, timeout=10.0)
            if connected_device.is_open:
                self.logger.log.info(f"serial connected to {dev}")
                return connected_device
            else:
                self.logger.log.error("Serial port failed to open (unknown reason).")
                return None
        except Exception as e:
            self.logger.log.info(f"connect_serial error: {e}")
            return None

    def clear_serial(self):
        """Clear buffers if port is open."""
        if self.mcu and getattr(self.mcu, "is_open", False):
            try:
                self.mcu.reset_input_buffer()
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
        """Auto-reconnect loop: connect → run send/receive/heartbeat → cleanup → retry."""
        backoff = 1.0
        while not self._stopping:
            # 1) Detect & connect
            ports = self.detect_serial()
            self.mcu = self.connect_serial(ports)

            if not self.mcu:
                self.logger.log.info(f"No device. Retry in {backoff:.1f}s...")
                await asyncio.sleep(backoff)
                backoff = min(backoff * 1.5, 10.0)  # capped backoff
                continue

            # Connected
            backoff = 1.0
            self.clear_serial()
            self._rx_buf = ""  # reset RX buffer

            # 2) Spawn tasks
            self.send_task = asyncio.create_task(self.send())
            self.receive_task = asyncio.create_task(self.receive())
            # (A) heartbeat task keeps MCU comms alive
            self.heartbeat_task = asyncio.create_task(self.heartbeat(period_s=0.5))

            # 3) Wait until either task exits (disconnect/error/shutdown)
            done, pending = await asyncio.wait(
                {self.send_task, self.receive_task, self.heartbeat_task},
                return_when=asyncio.FIRST_COMPLETED
            )

            # 4) Cancel the other tasks and cleanup
            for t in pending:
                t.cancel()
            try:
                await asyncio.gather(*pending, return_exceptions=True)
            except Exception:
                pass

            self.close_serial()

            # brief pause before trying again
            if not self._stopping:
                await asyncio.sleep(1.0)

        self.logger.log.info("SerialServer.run exiting (stopping=True).")

    # --------------------------
    # Tasks
    # --------------------------
    async def heartbeat(self, period_s: float = 2.0):
        """
        Simple fixed heartbeat at a low rate (e.g., every 2s).
        Keeps MCU comms watchdog alive without chatty traffic.
        """
        try:
            while not self._stopping:
                if not self.mcu or not getattr(self.mcu, "is_open", False):
                    break
                try:
                    # Write directly, not via queue, so it doesn’t interfere with commands
                    self.mcu.write(b'<{"hb":1}>')
                except Exception as e:
                    self.logger.log.debug(f"HB write failed (will retry): {e}")
                await asyncio.sleep(period_s)
        except asyncio.CancelledError:
            return


    async def send(self):
        """Drain outbound queue while port is open."""
        try:
            while True:
                if not self.mcu or not getattr(self.mcu, "is_open", False):
                    self.logger.log.info("Serial port is closed. Exiting send loop.")
                    break

                msg = await self.mcu_writes.get()
                self.logger.log.info(f"Sending: {msg}")

                try:
                    # Pi -> MCU is framed <JSON> (you already did this; keep it)
                    payload = '<' + json.dumps(msg) + '>'
                    self.mcu.write(payload.encode('ascii', errors='ignore'))
                except (serial.SerialException, serial.SerialTimeoutException) as e:
                    # Re-queue the message so it isn't lost, then exit loop to trigger reconnect
                    self.logger.log.error(f"Serial write error: {e}. Will reconnect.")
                    try:
                        self.mcu_writes.put_nowait(msg)
                    except Exception:
                        pass
                    break
                except Exception as e:
                    self.logger.log.error(f"Unexpected send error: {e}")
                    # Re-queue once; then exit
                    try:
                        self.mcu_writes.put_nowait(msg)
                    except Exception:
                        pass
                    break

                await asyncio.sleep(0.01)
        except asyncio.CancelledError:
            self.logger.log.info("Send task cancelled.")

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

    async def receive(self):
        """Read bytes, extract JSON with brace matching, dispatch, until disconnect."""
        try:
            while True:
                if not self.mcu or not getattr(self.mcu, "is_open", False):
                    self.logger.log.info("Serial port is closed. Exiting receive loop.")
                    break

                chunk = b""
                try:
                    # Read a small chunk in a thread (pyserial is blocking)
                    chunk = await asyncio.to_thread(self.mcu.read, 256)
                    if not chunk:
                        await asyncio.sleep(0.01)
                        continue

                    text = chunk.decode('ascii', errors='ignore')
                    if not text:
                        await asyncio.sleep(0.01)
                        continue

                    # Accumulate and extract any complete JSON objects (B)
                    self._rx_buf += text
                    for json_text in self._extract_json_objects():
                        try:
                            msg_dict = json.loads(json_text)
                        except json.JSONDecodeError as e:
                            self.logger.log.error(f"JSON decode error: {e} - Raw: {json_text[:160]}")
                            continue

                        # ---- Dispatch (C tweak applied) ----
                        if 'distance' in msg_dict:
                            await self.distance.put(msg_dict['distance'])

                        elif 'feedback' in msg_dict:
                            await self.feedback_reads.put(msg_dict['feedback'])

                        elif 'encoder_distance' in msg_dict:
                            await self.encoder_distance.put(msg_dict['encoder_distance'])

                        # (C) Accept both "encoder reset" and "All encoders reset"
                        elif msg_dict.get('status', '').lower() in ("encoder reset", "all encoders reset"):
                            await self.encoder_distance.put(msg_dict)

                        elif msg_dict.get('status', '').lower() == "light_updated":
                            self.logger.log.info(f"Andon light updated to: {msg_dict.get('state')}")

                        elif msg_dict.get("trigger_reached"):
                            self.logger.log.info(
                                f"Trigger reached on channel {msg_dict.get('channel')} at value {msg_dict.get('value')}"
                            )

                        elif msg_dict.get("trigger_deactivated"):
                            self.logger.log.info(f"Trigger deactivated on channel {msg_dict.get('channel')}")

                        elif msg_dict.get("status", "").lower() == "shutdown_complete":
                            self.logger.log.info("Shutdown confirmed by H7.")

                        elif 'andon_diag' in msg_dict:
                            diag = msg_dict['andon_diag']
                            code = diag.get('code')
                            state = diag.get('state')
                            override = diag.get('override')
                            reasons = diag.get('reasons', {})
                            faults  = diag.get('faults', [])
                            fmods   = diag.get('fault_modules', [])


                            if code != self.last_andon_code:
                                self.last_andon_code = code

                            await self.mcu_reads.put({'type': 'andon_diag', **diag})
                            self.logger.log.info(
                                f"Andon diag → state={state} code={code} override={override} "
                                f"reasons={reasons} faults={faults} fault_modules={fmods} keys={list(diag.keys())}"
                            )

                  
                        elif msg_dict.get("type") == "boot_health":
                            # Forward to central queue for health aggregation
                            await self.mcu_reads.put(msg_dict)
                            self.logger.log.info(
                                f"Boot health ok={msg_dict.get('ok')} checks={list((msg_dict.get('checks') or {}).keys())}"
                            )

                        elif msg_dict.get("type") == "test_result":
                            # Pass directly to WebSocketServer health_pump via mcu_reads queue
                            await self.mcu_reads.put(msg_dict)
                    
                        else:
                            # Forward any other top-level dict to health/WS pipeline.
                            await self.mcu_reads.put(msg_dict)


                except serial.SerialException as e:
                    preview = repr(chunk[:80]) if isinstance(chunk, (bytes, bytearray)) else repr(chunk)
                    self.logger.log.error(f"Serial exception: {e} - Raw data preview: {preview}")
                    # Exit loop to trigger reconnect in run()
                    break
                except Exception as e:
                    preview = repr(chunk[:80]) if isinstance(chunk, (bytes, bytearray)) else repr(chunk)
                    self.logger.log.error(f"Error parsing serial data: {e} - Raw data preview: {preview}")

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
                payload = '<' + json.dumps({"action": "shutdown"}) + '>'
                self.mcu.write(payload.encode('ascii', errors='ignore'))
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

        self.close_serial()