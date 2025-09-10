from logger import Logger
from log_server import LogServer
from queues import Queues
from websocket_server import WebsocketServer
from serial_server import SerialServer
import asyncio 

class Grlrr():
    def __init__(self):
        # varibale and object creation and organization
        self.logger = Logger()
        self.qs = Queues()
        self.log_server = LogServer(logger=self.logger)
        self.wss = WebsocketServer(logger=self.logger, queues=self.qs)
        self.ss = SerialServer(logger=self.logger, queues=self.qs)

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
                #self.ss.mcu_writes.put_nowait({'action': 'set_speed', 'speed': '' ,})
         
            case 'start_process':
                print('started process')
                self.ss.mcu_writes.put_nowait({'action': 'start_process',})

            case 'stop_process':
                print('stopped process')
                self.ss.mcu_writes.put_nowait({'action': 'stop_process',})

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