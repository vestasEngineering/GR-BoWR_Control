import asyncio
from simple_pid import PID
from logger import Logger
from queues import Queues
import time
#from my_redis import MyRedis
# this is designed to map height to a delta speed

# there should be no change in speed if the height is in the right spot.

# the speed should increase if the height is above the spec height, error +

# the speed should decrease if the height is below the spec height, error -

# process_speed == current_speed + delta_speed | delta_speed = pid(height)


class Ultrasonic():
    def __init__(self, logger:Logger, queues:Queues):
        self.logger = logger
        self.mcu_writes = queues.mcu_writes
        self.distance_queue = queues.distance
        self.num_bad_measurements = 0
        self.p = 3.5
        self.i = 0.3
        self.d = 0.08
        self.set_point = 0.0
        self.process_speed = 0.02
        self.current_speed = 0.0
        self.current_distance = 0.0
        self.delta_speed = 0.0
        self.tolerance = 2 # 2 mm

        # ultrasonic pid
        self.pid = PID(Kp=self.p, Ki=self.i, Kd=self.d, setpoint=self.set_point)
        self.pid.sample_time = 0.05 # seconds
        self.pid.setpoint = 125 # mm
        self.lower_limit = self.pid.setpoint - self.tolerance
        self.upper_limit = self.pid.setpoint + self.tolerance
        self.correction_deadband = (self.lower_limit, self.upper_limit)
        self.pid.output_limits = (-0.005, 0.005) # the speed should not increase or decrease more than 0.005 m/s per second


    def update_height(self, height):
        self.pid.setpoint = height

    def ignore_bad_measurements(self, distance):
        # since the sensor can only read between 40 and 300 we need to account for that.
        if 45 < distance < 250:
            return distance
        else:
            self.num_bad_measurements += 1
            print(self.num_bad_measurements)
            return self.pid.setpoint
        


    async def run(self):
        try:
            #Ensure motor speed is set to 0.0 before starting.
            self.process_speed = 0.0
            self.current_speed = 0.0
            
            while True:

                if self.num_bad_measurements < 50: 
                    print(f"Bad Measurments = {self.num_bad_measurements}")
                    distance = await self.distance_queue.get()
                    print(f"Distance = {distance}")
                    self.current_distance = self.ignore_bad_measurements(distance)

                    # if the robot is within the tolerance then don't adjust the speed
                    if min(self.correction_deadband) < self.current_distance <= max(self.correction_deadband):
                        u = 0
                    else:
                        u = self.pid(self.current_distance)

                    self.process_speed = round(max(self.current_speed + u, 0), 4) # dont allow the machine to go backwards...
                    self.current_speed = self.process_speed

                else:
                    self.process_speed = 0.0
                    raise ValueError('too many bad measurements')
                #self.logger.log.info('num bad measurements: '+str(self.num_bad_measurements))


                # self.logger.log.info(str(time.time())+','+str(self.current_distance)+','+str(self.process_speed))
                # m1 is the left side right now, m4 is the right side
                await self.mcu_writes.put({"speed0": -1.0 * float(self.process_speed)})
                await self.mcu_writes.put({"speed1": -1.0 * float(self.process_speed)})
                await self.mcu_writes.put({"speed2":        float(self.process_speed)})
                await self.mcu_writes.put({"speed3":        float(self.process_speed)})

                await asyncio.sleep(0.05)

        except asyncio.CancelledError:
            self.logger.log.info("ultrasonic cancelled")
            self.mcu_writes.put_nowait({"speed0": -1.0 *  float(0.0)})
            self.mcu_writes.put_nowait({"speed1": -1.0 *  float(0.0)})
            self.mcu_writes.put_nowait({"speed2":        float(0.0)})
            self.mcu_writes.put_nowait({"speed3":        float(0.0)})

        except Exception as e:
            self.logger.log.error(f"Error to ultrasonic run: {e}")