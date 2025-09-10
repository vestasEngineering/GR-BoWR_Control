import asyncio
import serial
from logger import Logger
from queues import Queues

class Actuator:
    def __init__(self, logger: Logger, queues: Queues, port="/dev/ttyACM0", baudrate=115200):
        """Initialize Actuator wrapper with I2C bus, logger, and queues."""
        self.logger = logger
        self.queues = queues.feedbackSignals
        try:
            self.ser = serial.Serial(port, baudrate, timeout=2)
            self.logger.log.info(f"Opened serial on {port} at {baudrate} baud.")
        except Exception as e:
            self.logger.log.error("Failed to open serial port: " + str(e))
            self.ser = None

    async def send_command(self, command: str) -> str:
        """
        Send a plain-text command to Portenta.
        Expected commands are in the format:
            "set <actuator> <voltage>"
            "state"
            "test"
        """

        if not self.ser:
            self.logger.log.error("Serial connection not available.")
            return ""
        full_command = command + "\n"
        self.logger.log.info("Sending command: " + full_command.strip())

        #Write the command to the serial port
        await asyncio.to_thread(self.ser.write, full_command.encode("ascii"))

        #Read a line from the serial port
        response_bytes = await asyncio.to_thread(self.ser.readline)
        try:
            response_str = response_bytes.decode("ascii").strip()
        except Exception as e:
            self.logger.log.error("Decoding error: " + str(e))
            response_try = ""
        self.logger.log.info("Recieved response: " + response_str)
        return response_str

    async def set_actuator_voltage(self, actuator_index: int, voltage: float) -> str:
        """Send a command to set the specified actuator voltage and return the response"""
        command = f"set {actuator_index} {voltage}\n"
        return await self.send_command(command)
    
    async def get_actuator_state(self) -> str:
        """Request the current actuator state by sending the 'state' command."""
        command = "state"
        return await self.send_command(command)
    
    async def run_self_test(self) -> str:
        """Run the self-test routine on the Portenta"""
        command = "test"
        return await self.send_command(command)
    
    async def test_actuators(self):
        """A test routine that sets each actuator to a voltage, waits, then resets it to 0."""
        try:
            #Use the number of actuators you want to support in range(n), where n = # of actuators
            for actuator_index in range(3):
                self.logger.log.info(f"Testing actuator {actuator_index}")

                #Set actuator to 5V
                response = await self.set_actuator_voltage(actuator_index, 5.0)
                #response = await self.set_actuator_voltage(actuator_index, 5.0)
                self.logger.log.info(f"Response after sending actuator {actuator_index} to 2.5V: {response}")
                await asyncio.sleep(2) #Allow time for the actuator to respond.

                #Set actuator to 0v
                response = await self.set_actuator_voltage(actuator_index, 0.0)
                self.logger.log.info(f"Response after resetting actuator {actuator_index} to 0.0V: {response}")
                await asyncio.sleep(2) #Allow time for the actuator to respond.

            self.logger.log.info("All actuators tested successfully")
        except Exception as e:
            self.logger.log.error(f"Error during actuator testing: {e}")