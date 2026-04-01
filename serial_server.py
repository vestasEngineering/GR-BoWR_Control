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

        # State
        self.last_andon_code: Optional[int] = None
        self.mcu: Optional[serial.Serial] = None
        self.send_task: Optional[asyncio.Task] = None
        self.receive_task: Optional[asyncio.Task] = None
        self.heartbeat_task: Optional[asyncio.Task] = None
        self._stopping = False
        self._last_tx = 0.0

        # RX rolling buffer for brace-balanced extraction (B)
        self._rx_buf: str = ""

        # Initial startup messages (queued; will be sent after first connect)
        self.mcu_writes.put_nowait({"action": "ping"})
        self.mcu_writes.put_nowait({"speed0": 0, "speed1": 0, "speed2": 0, "speed3": 0})
        self.mcu_writes.put_nowait({"action": "reset_encoder"})


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
            #self.clear_serial()
            self._rx_buf = ""  # reset RX buffer

            # 2) Spawn tasks
            self.send_task = asyncio.create_task(self.send())
            self.receive_task = asyncio.create_task(self.receive())
            await asyncio.sleep(1.0)

            # (A) heartbeat task keeps MCU comms alive
            self.heartbeat_task = asyncio.create_task(self.heartbeat(period_s=1.5))

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
    async def heartbeat(self, period_s: float = 1.0):
        try:
            while not self._stopping:
                if not self.mcu or not getattr(self.mcu, "is_open", False):
                    break

                if self.mcu_writes.empty():
                    await self.mcu_writes.put({"hb": 1})

                await asyncio.sleep(period_s)
        except asyncio.CancelledError:
            return

    def _encode_line(self, msg: dict) -> bytes:
        return (json.dumps(msg, separators=(',', ':')) + '\n').encode('utf-8')

    async def send(self):
        """Drain outbound queue while port is open."""
        try:
            while True:
                if not self.mcu or not getattr(self.mcu, "is_open", False):
                    self.logger.log.info("Serial port is closed. Exiting send loop.")
                    break

                msg = await self.mcu_writes.get()
                self.logger.log.info(f"PI TX: {msg}")

                try:
                    # Pi -> MCU is framed <JSON> (you already did this; keep it)
                    raw = self._encode_line(msg)
                    #self.logger.log.info(f"TX raw: {raw!r}")
                    self.mcu.write(raw)
                    self.mcu.flush()
                    self._last_tx = asyncio.get_running_loop().time()
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

                await asyncio.sleep(0.03)
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
                        self.logger.log.info(
                            f"MCU RX: {json.dumps(msg_dict, separators=(',', ':'))}"
                        )
                    except json.JSONDecodeError as e:
                        self.logger.log.error(f"JSON decode error: {e} - Raw line: {text!r}")
                        continue

                    # ---- Dispatch ----
                    if 'distance' in msg_dict:
                        await self.distance.put(msg_dict['distance'])

                    elif 'feedback' in msg_dict:
                        await self.feedback_reads.put(msg_dict['feedback'])

                    elif 'encoder_distance' in msg_dict:
                        await self.encoder_distance.put(msg_dict['encoder_distance'])

                    elif msg_dict.get('status', '').lower() in ("encoder reset", "all encoders reset"):
                        await self.encoder_distance.put(msg_dict)

                    elif msg_dict.get('status', '').lower() == "light_updated":
                        self.logger.log.info(f"Andon light updated to: {msg_dict.get('state')}")

                    elif msg_dict.get("status", "").lower() == "triggers_loaded":
                        # Dedicated ACK path for trigger programming
                        await self.trigger_acks.put(msg_dict)
                        self.logger.log.info(
                            f"Trigger ACK received: count={msg_dict.get('count')}"
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

                    elif msg_dict.get("type") == "boot_health":
                        await self.mcu_reads.put(msg_dict)
                        self.logger.log.info(
                            f"Boot health ok={msg_dict.get('ok')} "
                            f"checks={list((msg_dict.get('checks') or {}).keys())}"
                        )

                    elif msg_dict.get("type") == "test_result":
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

        self.close_serial()