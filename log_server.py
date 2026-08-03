import multiprocessing as mp
from aiohttp import web
from logger import Logger

class LogServer():
    def __init__(self, logger: Logger):
        self.host = '0.0.0.0'
        self.port = 1095
        self.logger = logger
        self.process = mp.Process(target=self.main)
        self.process.start()
        self.logger.log.info(str('log server started on: '+self.host+':'+str(self.port)))

    async def handle(self, request):
        f_served = open(self.logger.lf,'rb')
        f_content = f_served.read().decode('utf-8')
        f_served.close()
        return web.Response(text=f_content)


    def main(self):
        app = web.Application()
        app.add_routes([web.get('/', self.handle)])
        web.run_app(app, host=self.host, port=self.port)