import asyncio
from logger import Logger
from queues import Queues

# This script is created to test each motor individually to ensure proper function.

class MotorTest():
    def __init__(self, logger: Logger, queues: Queues):
        self.logger = logger
        self.mcu_writes = queues.mcu_writes
        self.test_speed = 1.0 # Set a reasonable test speed

    async def test_motors(self):
        try:
            for motor_index in range(4):
                self.logger.log.info(f"Testing motor {motor_index}")

                #Set speed for the current motor
                speed_command = {f"speed{motor_index}": self.test_speed}
                await self.mcu_writes.put(speed_command)
                
                #Wait to observe motor behavior
                await asyncio.sleep(8) #Two seconds

                #Stop the motor
                stop_command = {f"speed{motor_index}": 0.0}
                await self.mcu_writes.put(stop_command)
                self.logger.log.info("Motor {motor_index} test completed.")

            self.logger.log.info("All motors tested successfully")

        except Exception as e:
            self.logger.log.error(f"Error during motor testing: {e.__class__.__name__}")
        except asyncio.CancelledError:
            self.logger.log.info("Motor testing cancelled.")