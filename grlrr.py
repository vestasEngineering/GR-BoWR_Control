from logger import Logger
from log_server import LogServer
from queues import Queues
from websocket_server import WebsocketServer
from serial_server import SerialServer
from april_tag_detector import AprilTagDetector
from job_manager import JobManager
from event_logger import EventLogger
from database_service import DatabaseService
import asyncio
import signal


class Grlrr():
    def __init__(self):
        self.logger = Logger()

        self.db = DatabaseService()

        self.job_manager = JobManager(
            db=self.db
        )

        restored_job = (
            self.job_manager.get_active_job()
        )

        if restored_job is not None:
            self.logger.log.warning(
                "Restored active job from database: "
                f"job_uuid="
                f"{restored_job['job_uuid']} "
                f"state="
                f"{restored_job['state']}"
            )

        self.event_logger = EventLogger(
            db=self.db
        )

        self.qs = Queues()

        self.log_server = LogServer(
            logger=self.logger
        )

        self.wss = WebsocketServer(
            logger=self.logger,
            queues=self.qs,
            job_manager=self.job_manager,
        )

        self.ss = SerialServer(
            logger=self.logger,
            queues=self.qs,
        )

        self.detector = AprilTagDetector(
            queues=self.qs,
            rtsp_url=(
                "rtsp://vestas:vestasvestas@"
                "192.168.8.164:554/stream1"
            ),
        )

        self.logger.log.info("grlrr init")
        self.cmd = 'initialize_robot'
        self.integration_tasks = []

        self.event_logger.log(
            "INFO",
            "application_start",
            "GRLRR application started"
        )
        


    async def teardown(self):
        self.logger.log.info(
            "Shutdown requested."
        )

        self.event_logger.log(
            "INFO",
            "application_shutdown",
            "GRLRR application is shutting down",
        )

        if hasattr(self, "loop_task"):
            self.loop_task.cancel()

        try:
            await self.ss.shutdown()
        except Exception:
            self.logger.log.exception(
                "Serial shutdown failed."
            )

        try:
            await self.wss.flush_encoder_checkpoint()

        except Exception:
            self.logger.log.exception(
                "Final encoder checkpoint flush failed."
            )

        try:
            self.db.close()
        except Exception:
            self.logger.log.exception(
                "Database close failed."
            )

        self.wss.shutdown_event.set()

        self.logger.log.info(
            "Teardown complete."
        )

    '''
    async def cli_listener(self):
    #For cmd line interface to send commands
        while True:
            command = await asyncio.to_thread(input, "Enter command: ")
            command = command.strip().lower()

            if command == "start":
                await self.qs.commands.put({'start_process': 1})
            elif command == "stop":
                await self.qs.commands.put({'stop_process': 1})
            elif command == "reset":
                await self.qs.commands.put({'reset_motors': 1})
            elif command == "exit":
                self.logger.log.info("Exit command received.")
                self.wss.shutdown_event.set()
                await self.teardown()
                break
            else:
                print(f"Unknown command: {command}")
    '''

    def setup(self):
        self.install_signal_handlers()

        self.qs.commands.put_nowait(
            {
                "initialize_robot": 1,
            }
        )

        self.event_loop = asyncio.get_running_loop()

        self.logger.log.info("grlrr setup")

        self.event_loop.create_task(
            self.wss.run()
        )

        self.event_loop.create_task(
            self.ss.run()
        )

        self.event_loop.create_task(
            self.detector.run()
        )
        
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
        try:
            while True:
                cmd = await self.qs.commands.get()
                self.logger.log.debug(f"Main loop received command: {cmd}")
                self.change_state(cmd)
        except asyncio.CancelledError:
            self.logger.log.info("Main loop cancelled.")


    async def main(self):
            self.setup()
            self.loop_task = asyncio.create_task(self.loop())
            await asyncio.gather(self.loop_task)
            #cli_task = asyncio.create_task(self.cli_listener())
            #await asyncio.gather(self.loop_task, self.cli_listener())

    async def monitor_andon_diag(self):
        while True:
            try:
                msg = await self.qs.mcu_reads.get()
                if isinstance(msg, dict) and msg.get('type') == 'andon_diag':
                    # Minimal log; you could also forward to WS/UI here
                    self.logger.log.info(
                        f"[ANDON] {msg.get('state')} (code={msg.get('code')}) "
                        f"override={msg.get('override')} reasons={msg.get('reasons')}"
                    )
                    # Example forward (if your WebsocketServer exposes such API):
                    # await self.wss.broadcast_json({"andon": msg})
            except Exception as e:
                self.logger.log.error(f"monitor_andon_diag error: {e}")

    def install_signal_handlers(self) -> None:
        loop = asyncio.get_running_loop()

        for sig in (
            signal.SIGINT,
            signal.SIGTERM,
        ):
            loop.add_signal_handler(
                sig,
                lambda: asyncio.create_task(
                    self.teardown()
                ),
            )