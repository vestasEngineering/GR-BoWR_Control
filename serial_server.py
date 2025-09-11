import glob
import fnmatch
import serial
import json
import asyncio
from logger import Logger
from queues import Queues
import traceback


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
        self.mcu_writes.put_nowait({'action': 'set_voltage', 'channel': 0, 'voltage': 0})
        self.mcu_writes.put_nowait({'action': 'set_voltage', 'channel': 1, 'voltage': 0})
        self.mcu_writes.put_nowait({'action': 'set_voltage', 'channel': 2, 'voltage': 0})
        self.mcu_writes.put_nowait({'action': 'set_voltage', 'channel': 3, 'voltage': 0})
        
        #self.mcu_writes.put_nowait({'action': 'read_feedback', 'channel': 0})
        #self.mcu_writes.put_nowait({'action': 'read_feedback', 'channel': 1})
        #self.mcu_writes.put_nowait({'action': 'read_feedback', 'channel': 2})

        self.mcu_writes.put_nowait({'action': 'reset_encoder'})
        self.mcu_writes.put_nowait({"action": "set_triggers", "triggers": [ {"threshold": 0, "activate": 0, "deactivate": 2, "delay": 0}]})
        self.mcu_writes.put_nowait({"action": "set_triggers", "triggers": [ {"threshold": 300, "activate": 1, "deactivate": 0, "delay": 9}]})
        self.mcu_writes.put_nowait({"action": "set_triggers", "triggers": [ {"threshold": 600, "activate": 2, "deactivate": 1, "delay": 9}]})

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
