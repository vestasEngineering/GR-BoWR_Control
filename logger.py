# logger.py
import logging
import os
from datetime import datetime

class Logger():
    
    def __init__(self):
        self.log = self.setup_log()
        

    def __name__(self):
        return str(Logger.__name__)

    def define_log_file(self):
        return os.path.join("logs", ("gr-towr_" + str(datetime.now().strftime('%Y-%m-%d %H:%M:%S')) + ".log").replace(" ", "_"))


    def configure_logger(self):
        logging.basicConfig(filename = self.lf, 
                            level    = logging.INFO, 
                            format='[%(asctime)s] %(levelname)s %(filename)s:%(lineno)d %(message)s',
                            datefmt  = '%Y-%m-%d %H:%M:%S')

    def get_log_name(self):
        return logging.getLogger(__name__)


    def setup_log(self):

        self.lf = self.define_log_file()
        self.configure_logger()
        return self.get_log_name()
