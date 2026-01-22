import glob
import fnmatch
import serial
import json
import asyncio
from logger import Logger
from queues import Queues
import traceback
from robot_list import (
    load_robot_list, get_defaults, get_transitions,
    thresholds_for_mcu, triggers_from_thresholds
)


class SerialServer():
    def __init__(self, logger: Logger, queues: Queues):
        self.logger = logger
        self.mcu_reads = queues.mcu_reads
        self.mcu_writes = queues.mcu_writes
        self.distance = queues.distance
        self.feedback_reads = queues.feedbackSignals
        self.encoder_distance = queues.encoder_distance

        # self.mcu_writes.put_nowait({"msgtyp": "get", "device":"?", "motorSpeed":0})
        self.mcu_writes.put_nowait({"start_serial":      1})
        self.mcu_writes.put_nowait({
            "speed0": 0,
            "speed1": 0,
            "speed2": 0,
            "speed3": 0
        })

        # Initialize actuators
        self.mcu_writes.put_nowait({"action": "set_triggers", "clear": True})
        #self.mcu_writes.put_nowait({'action': 'read_feedback', 'channel': 0})
        #self.mcu_writes.put_nowait({'action': 'read_feedback', 'channel': 1})
        #self.mcu_writes.put_nowait({'action': 'read_feedback', 'channel': 2})

        self.mcu_writes.put_nowait({'action': 'set_light', 'state': 'GREEN'})
        self.mcu_writes.put_nowait({'action': 'reset_encoder'})    
        
        try:
            data = load_robot_list()
            rid, bid = get_defaults(data)
            vals = get_transitions(data, rid, bid)
            thresholds = thresholds_for_mcu(data, vals)

            triggers = self.build_triggers_with_pattern(
                thresholds,
                first_delay_ms=0,   # first has delay 0
                other_delay_ms=9    # rest have delay 9
            )

            self.logger.log.info(f"SerialServer: will set_triggers from saved list -> {triggers}")

            async def schedule_triggers():
                await self._send_triggers_single_object(
                    triggers,
                    inter_delay=0.05,
                    clear_first=True  # use firmware 'clear' to reset your buffer
                )

            asyncio.get_event_loop().create_task(schedule_triggers())

        except Exception as e:
            self.logger.log.error(f"Error sending triggers: {e}") 

        self.mcu = None

    def detect_serial(self, preferred_list=['*']):
        '''try to auto-detect serial ports on win32'''
        glist = glob.glob('/dev/ttyUSB*') + glob.glob('/dev/ttyACM*')
        ret = []

        # try preferred ones first
        for d in glist:
            for preferred in preferred_list:
                if fnmatch.fnmatch(d, preferred):
                    ret.append(d)
        if len(ret) > 0:
            return ret
        # now the rest
        for d in glist:
            ret.append(d)
        return ret

    def connect_serial(self, available_ports):
        try:
            connected_device = serial.Serial(
                available_ports[0], 115200, timeout=10.0)

            if connected_device.isOpen():
                self.logger.log.info("serial connected to "+str(available_ports[0]))
                return connected_device
            else:
                raise Exception("No serial devices")
        except Exception as e:
            # send alert to the tablet
            self.logger.log.info(e)

    def valididate_serial(self, device):
        try:
            msg = {"msgtyp": "get", "device": "?"}
            device.write((json.dumps(msg)+'\n').encode('ascii'))
            new_msg = json.loads(device.read_until(
                expected=b"\n").decode('ascii'))
            self.logger.log.info(new_msg)
            if new_msg["device"] == "h7":
                self.logger.log.info("h7 connected")
                return 1
            else:
                self.logger.log.error("NOT CONNECTED")
                return 0
        except Exception as e:
            self.logger.log.error(e)
            self.logger.log.error(
                "no valid device / comm issue / no api endpoint")
            
    def clear_serial(self):
        self.mcu.reset_input_buffer()
        self.mcu.reset_output_buffer()


    async def run(self):
        self.mcu = self.connect_serial(self.detect_serial())
        if self.mcu:
            self.clear_serial()
            self.send_task = asyncio.create_task(self.send())
            self.receive_task = asyncio.create_task(self.receive())
            await asyncio.gather(self.send_task, self.receive_task)
        else:
            self.logger.log.info("Unable to connect to serial device. Exiting...")
            quit()


    async def send(self):
        try:
            while True:
                msg = await self.mcu_writes.get()
                self.logger.log.info(f"Sending: {msg}")
                self.mcu.write(('<' + json.dumps(msg) + '>').encode('ascii'))
                await asyncio.sleep(0.01)
        except asyncio.CancelledError:
            self.logger.log.info("Send task cancelled.")


    async def hb(self):
        while True:
            await self.mcu_writes.put({"hb": 1})
            await asyncio.sleep(0.75)


    #async def parse_dict(self, msg_dict):
    #    if 'distance' in msg_dict:
    #        await self.distance.put(msg_dict['distance'])


    async def send_shutdown(self):
            if self.mcu:
                self.logger.log.info("Sending shutdown command to H7...")
                try:
                    self.mcu.write(('<' + json.dumps({"action": "shutdown"}) + '>').encode('ascii'))
                    await asyncio.sleep(0.5)  # Give H7 time to respond
                except Exception as e:
                    self.logger.log.error(f"Error sending shutdown: {e}")


    def close_serial(self):
        if self.mcu:
            try:
                self.mcu.reset_input_buffer()
                self.mcu.reset_output_buffer()
                self.mcu.close()
                self.logger.log.info("Serial connection closed.")
            except Exception as e:
                self.logger.log.error(f"Error closing serial: {e}")


    async def shutdown(self):
        self.mcu_writes.put_nowait({'action': 'set_light', 'state': 'YELLOW'})
        self.logger.log.info("SerialServer shutting down...")

        try:
            await self.mcu_writes.put({"action": "shutdown"})
            await asyncio.sleep(0.5)

            if self.send_task:
                self.send_task.cancel()
            if self.receive_task:
                self.receive_task.cancel()

            if self.mcu and self.mcu.is_open:
                self.mcu.reset_input_buffer()
                self.mcu.reset_output_buffer()
                self.mcu.close()
                self.logger.log.info("Serial port closed.")
        except Exception as e:
            self.logger.log.error(f"Error during SerialServer shutdown: {e}")


    async def receive(self):
        try:
            while True:
                if not self.mcu or not self.mcu.is_open:
                    self.logger.log.info("Serial port is closed. Exiting receive loop.")
                    break  # Exit the loop cleanly

                try:
                    line = await asyncio.to_thread(self.mcu.readline)
                    line = line.decode('ascii').strip()

                    if not line.startswith("{"):
                        self.logger.log.warning(f"Ignoring non-JSON line: {line}")
                        continue

                    msg_dict = json.loads(line)

                    # Handle known message types
                    if 'distance' in msg_dict:
                        await self.distance.put(msg_dict['distance'])
                    elif 'feedback' in msg_dict:
                        await self.feedback_reads.put(msg_dict['feedback'])
                    elif 'encoder_distance' in msg_dict:
                        await self.encoder_distance.put(msg_dict['encoder_distance'])
                    elif msg_dict.get('status', '').lower() == "encoder reset":
                        await self.encoder_distance.put(msg_dict)
                    elif msg_dict.get('status', '').lower() == "light_updated":
                        self.logger.log.info(f"Andon light updated to: {msg_dict.get('state')}")
                    elif msg_dict.get("trigger_reached"):
                        self.logger.log.info(f"Trigger reached on channel {msg_dict['channel']} at value {msg_dict['value']}")
                    elif msg_dict.get("trigger_deactivated"):
                        self.logger.log.info(f"Trigger deactivated on channel {msg_dict['channel']}")
                    elif msg_dict.get("status", "").lower() == "shutdown_complete":
                        self.logger.log.info("Shutdown confirmed by H7.")

                except json.JSONDecodeError as e:
                    self.logger.log.error(f"JSON decode error: {e} - Raw data: {line}")
                except Exception as e:
                    self.logger.log.error(f"Error parsing serial data: {e} - Raw data: {line}")

                await asyncio.sleep(0)
        except asyncio.CancelledError:
            self.logger.log.info("Receive task cancelled.")



    def build_triggers_with_pattern(thresholds, first_delay_ms=0, other_delay_ms=9):
        """
        Create the trigger dicts with:
        activate = i
        deactivate = (i - 1) % n
        delay = first_delay_ms for i==0 else other_delay_ms
        """
        n = len(thresholds)
        triggers = []
        for i, th in enumerate(thresholds):
            triggers.append({
                "threshold": int(th),
                        "activate": i,
                "deactivate": (i - 1) % n,
                "delay": first_delay_ms if i == 0 else other_delay_ms,
            })

    
    async def _send_triggers_single_object(self, triggers, *, inter_delay=0.05, clear_first=True):
        try:
            if clear_first:
                await self.mcu_writes.put({"action": "set_triggers", "clear": True})

            for trig in triggers:
                # Use the single 'trigger' object format supported by your firmware
                await self.mcu_writes.put({"action": "set_triggers", "trigger": trig})
                await asyncio.sleep(inter_delay)
        except Exception as e:
            self.logger.log.error(f"Error sending triggers: {e}")