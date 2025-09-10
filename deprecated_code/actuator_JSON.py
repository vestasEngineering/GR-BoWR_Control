import asyncio
import json
from logger import Logger
from queues import Queues

class Actuator:
    def __init__(self, logger: Logger, queues: Queues):
        """Initialize Actuator wrapper with I2C bus, logger, and queues."""
        self.logger = logger
        self.mcu_writes = queues.mcu_writes
        self.feedback_reads = queues.feedbackSignals

    async def set_actuator_voltage(self, actuator_index, voltage):
        """Send actuator voltage command via serial to the Portenta H7."""
        voltage = max(0.0, min(voltage, 5.0))
        command = {
            "action": "set_voltage",
            "channel": actuator_index,
            "voltage": voltage
        }
        await self.mcu_writes.put(command)
        self.logger.log.info(f"Sent actuator {actuator_index} command: {command}")

    async def read_actuator_feedback(self, actuator_index):
        """Request actuator feedback from Portenta via serial."""
        command = {
            "action": "read_feedback",
            "channel": actuator_index
        }
        await self.mcu_writes.put(command)

        feedback_voltage = None
        try:
            while feedback_voltage is None:
                msg = await asyncio.wait_for(self.feedback_reads.get(), timeout=2.0)
                if msg.get("channel") == actuator_index:
                    feedback_voltage = msg.get("feedback")
        except asyncio.TimeoutError:
            self.logger.log.error(f"Timeout waiting for actuator {actuator_index} feedback")
            return None  # Explicitly return None on timeout

        self.logger.log.info(f"Actuator {actuator_index} feedback: {feedback_voltage} V")
        return feedback_voltage

    async def delayed_actuator_voltage(self, actuator_index, voltage, delay):
        """Set actuator voltage after a delay."""
        await asyncio.sleep(delay)
        await self.set_actuator_voltage(actuator_index, voltage)
    
    async def test_actuators(self):
        """Test each actuator."""
        try:
            for actuator_index in range(4):
                self.logger.log.info(f"Testing actuator {actuator_index}")

                # Set voltage for actuator
                await self.set_actuator_voltage(actuator_index, 2.5)

                # Wait for and log the feedback
                feedback = await self.read_actuator_feedback(actuator_index)
                if feedback is None:
                    self.logger.log.error(f"Failed to receive feedback for actuator {actuator_index}")
                    continue  # Skip the rest of the loop for this actuator

                # Reset actuator output before moving to next test
                await self.set_actuator_voltage(actuator_index, 0.0)

                self.logger.log.info(f"Actuator {actuator_index} test completed. Feedback: {feedback}")

            self.logger.log.info("All actuators tested successfully.")

        except Exception as e:
            self.logger.log.error(f"Error during actuator testing: {e.__class__.__name__}")
        except asyncio.CancelledError:
            self.logger.log.info("Actuator testing cancelled")