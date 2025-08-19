import asyncio
from simple_pid import PID
from logger import Logger
from queues import Queues
import time
#from my_redis import MyRedis

"""
Runs the ultrasonic sensor loop. It continuously waits on distance measurements from its queue,
uses them to update the PID control for the drive motor speeds, and publishes speed commands.
The very first time a reading in the valid range is seen, it signals the first_valid_event.
"""

class Ultrasonic:
    def __init__(self, logger: Logger, queues: Queues):
        self.logger = logger
        self.mcu_writes = queues.mcu_writes
        self.distance_queue = queues.distance

        self.num_bad_measurements = 0
        self.set_point = 130.0  # mm
        self.tolerance = 2      # mm

        self.current_distance = 0.0
        self.current_speed = 0.0
        self.process_speed = 0.0

        # PID controller setup
        self.pid = PID(Kp=3.5, Ki=0.3, Kd=0.08, setpoint=self.set_point)
        self.pid.sample_time = 0.05
        self.pid.output_limits = (-0.005, 0.005) #Acceleration limits

        self.lower_limit = self.set_point - self.tolerance
        self.upper_limit = self.set_point + self.tolerance

    def update_height(self, height):
        self.pid.setpoint = height
        self.lower_limit = height - self.tolerance
        self.upper_limit = height + self.tolerance

    def ignore_bad_measurements(self, distance):
        # since the sensor can only read between 40 and 300 we need to account for that.
        if 45 < distance < 250:
            return distance
        else:
            #self.num_bad_measurements += 1
            #print(self.num_bad_measurements)
            return None

    async def run(self):
        self.logger.log.info("Ultrasonic.run() started")

        try:
            self.process_speed = 0.0
            self.current_speed = 0.0

            while True:
                self.logger.log.debug("Waiting for distance data...")
                distance = await self.distance_queue.get()
                self.current_distance = self.ignore_bad_measurements(distance)

                if self.current_distance is None:
                    continue  # Skip bad measurement

                if self.current_distance < 70:
                    self.process_speed = 0.0
                    self.current_speed = 0.0
                else:
                    if self.lower_limit < self.current_distance <= self.upper_limit:
                        u = 0
                    else:
                        u = self.pid(self.current_distance)

                    self.process_speed = round(max(self.current_speed + u, 0), 4)
                    self.current_speed = self.process_speed

                #else:
                 #   self.process_speed = 0.0
                  #  raise ValueError("Too many bad measurements")

                # Send all motor speeds in one message
                await self.mcu_writes.put({
                    "speed0": -self.process_speed,
                    "speed1": -self.process_speed,
                    "speed2":  self.process_speed,
                    "speed3":  self.process_speed
                })

                await asyncio.sleep(0.05)

        except asyncio.CancelledError:
            self.logger.log.info("Ultrasonic task cancelled")
            self._stop_motors()

        except Exception as e:
            self.logger.log.error(f"Error in ultrasonic run: {e}")
            self._stop_motors()

    def _stop_motors(self):
        stop_msg = {
            "speed0": 0.0,
            "speed1": 0.0,
            "speed2": 0.0,
            "speed3": 0.0
        }
        self.mcu_writes.put_nowait(stop_msg)