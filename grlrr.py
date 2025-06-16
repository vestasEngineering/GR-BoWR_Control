from logger import Logger
from log_server import LogServer
from queues import Queues
from websocket_server import WebsocketServer
from serial_server import SerialServer
from ultrasonic import Ultrasonic
from encoder import Encoder
#from motor_test import MotorTest
from actuator_JSON import Actuator
from encoder_ultrasonic_actuator_test import ultrasonic_controller, actuator_sequence_controller
#from ultrasonic_actuator_test import ultrasonic_controller, actuator_sequence_controller
import asyncio 

class Grlrr():
    def __init__(self):
        # varibale and object creation and organization
        self.logger = Logger()
        self.qs = Queues()
        self.log_server = LogServer(logger=self.logger)
        self.wss = WebsocketServer(logger=self.logger, queues=self.qs)
        self.ss = SerialServer(logger=self.logger, queues=self.qs)
        self.ultrasonic = Ultrasonic(logger=self.logger, queues=self.qs)
        #self.motor_test = MotorTest(logger=self.logger, queues=self.qs)
        self.actuator = Actuator(logger=self.logger, queues=self.qs)
        self.encoder = Encoder(logger=self.logger, queues=self.qs)


        self.logger.log.info("grlrr init")
        self.cmd = 'initialize_robot'

        #Task tracking
        self.integration_tasks = []


    def setup(self):
        self.qs.commands.put_nowait({'initialize_robot':1})
        self.event_loop = asyncio.get_running_loop()

        self.logger.log.info('grlrr setup')
        self.event_loop.create_task(self.wss.run())
        self.event_loop.create_task(self.ss.run())
        #self.ultrasonic_task = self.event_loop.create_task(self.ultrasonic.run())


    def get_command(self):
        try:
            return self.qs.commands.get_nowait()
        except asyncio.QueueEmpty:
            pass
            #self.logger.log.debug('command queue empty')
        except Exception as e:
            self.logger.log.info('other exception')
            print(e.__class__.__name__)


    def change_state(self, cmd: dict):
        if cmd:
            cmd, param = list(cmd.keys())[0], list(cmd.values())[0]
        match cmd:
            case 'e_stop':
                quit()
         
            case 'initialize_robot':
                print('some init')
          
            case 'set_speed':
                print('set speed')
                self.ultrasonic.process_speed = param
         
            case 'start_process':
                print('started process')
                #self.ultrasonic_task = self.event_loop.create_task(self.ultrasonic.run())
                #self.motor_test_task = self.event_loop.create_task(self.motor_test.test_motors())
                #self.actuator_test_task = self.event_loop.create_task(self.actuator.test_actuators())
                
                #Reset the speed to 0.0 before starting
                self.ultrasonic.process_speed = 0.0
                self.ultrasonic.current_speed = 0.0

                self.integration_tasks.append(
                    self.event_loop.create_task(ultrasonic_controller(self.ultrasonic))
                )
                self.integration_tasks.append(
                    self.event_loop.create_task(actuator_sequence_controller(self.actuator, self.encoder, self.logger))
                    #self.event_loop.create_task(actuator_sequence_controller(self.actuator, self.first_valid_event, self.logger))
                )

            case 'stop_process':
                print('stopped process')              
                #self.ultrasonic_task.cancel()
                #self.motor_test_task.cancel()
                #self.actuator_test_task.cancel()
                
                for task in self.integration_tasks:
                    task.cancel()
                self.integration_tasks.clear()


            case None:
                return
                print('none case ...........')
         
            case _:
                return
                print('default')


    def update_state(self):
        new_cmd = self.get_command()
        if new_cmd != self.cmd:
            self.cmd = new_cmd
            self.change_state(new_cmd)


    async def loop(self):
        start_time = self.event_loop.time()
        while True:
            
            self.update_state()
            #print('main loop')
            ##start = self.event_loop.time()
            #self.qs.show_queue_size()
            self.logger.log.debug(self.event_loop.time()-start_time)

            self.logger.log.debug('main')
            await asyncio.sleep(0)
            #print('duration: ', str(self.event_loop.time()-start))


    async def main(self):
        self.setup()
        await self.loop()