import asyncio
from simple_pid import PID
from logger import Logger
from queues import Queues

# this is designed to map angles and offsets to velocities

# input angles from say -5 deg to 5 deg.
# input offsets from -0.04% to 0.04%

# the output of the pid should be 0 if deg    is 0
# the output of the pid should be 0 if offset is 0

# the output should be a faster motor on the left if the angle is negative
# the output should be a faster motor on the right if the angle is positive 

# the output should be a faster motor on the left if the offset is negative
# the output should be a faster motor on the right if the offset is positive

# the output should be gotten from the offset PID until it is with a specific range



class Steering():
    def __init__(self, logger:Logger, queues:Queues):
        self.logger = logger
        self.mode = ''
        self.mcu_writes = queues.mcu_writes
        self.angles = 0
        self.offsets = 0
        self.steering = True
        self.p = 0.1
        self.i = 0.0
        self.d = 0.0
        self.set_point = 0.0
        self.process_speed = 0.00 # m/s
        
        self.pid = PID(Kp=self.p, Ki=self.i, Kd=self.d, setpoint=self.set_point)
        self.pid.sample_time = 0.1 # seconds
        self.pid.output_limits = (-0.008, 0.008)    # Output value will be between -0.01 and 0.01 m/s
        self.pid.setpoint = 0.0



    async def setup(self):
        while  True:
            current_angle = await self.angles.get()
            current_offset = await self.offsets.get()

    async def run(self):
        try:
            while True:
                current_angle = await self.angles.get()
                current_offset = await self.offsets.get()
                # if the robot is within the tolerance then just correct for angle
                #if min(self.offset_deadband) < current_offset <= max(self.offset_deadband):
                u = round(self.pid(current_offset), 8)

                ### left_speed =  max(round(self.process_speed + u/2, 8), self.process_speed)
                ### right_speed = max(round(self.process_speed - u/2, 8), self.process_speed)

                left_speed =  self.process_speed
                right_speed = self.process_speed

                #'angle: '+str(current_angle)+
                self.logger.log.info(
                                     ' offset: '+str(current_offset)+
                                     ' left: '+str(left_speed)+
                                     ' right: '+str(right_speed)+
                                     ' u: '+str(u))

                # m1 is the left side right now, m4 is the right side
                await self.mcu_writes.put({"speed0": -1.0 * float(left_speed)})
                await self.mcu_writes.put({"speed1": -1.0 * float(left_speed)})
                await self.mcu_writes.put({"speed2":        float(right_speed)})
                await self.mcu_writes.put({"speed3":        float(right_speed)})


                

        except Exception as e:
            self.logger.log.info(e.__class__.__name__)
        except asyncio.CancelledError:
            self.logger.log.info("steering cancelled")
            self.mcu_writes.put_nowait({"speed0": -1.0 *  float(0.0)})
            self.mcu_writes.put_nowait({"speed1": -1.0 *  float(0.0)})
            self.mcu_writes.put_nowait({"speed2":        float(0.0)})
            self.mcu_writes.put_nowait({"speed3":        float(0.0)})
