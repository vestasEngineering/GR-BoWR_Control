# queues.py
import asyncio

class Queues():
    def __init__(self):
        self.distance = asyncio.Queue(10)
        self.encoder_distance = asyncio.Queue(10)
        self.feedbackSignals = asyncio.Queue(10)
        self.responses = asyncio.Queue(10)
        self.commands = asyncio.Queue(10)
        self.mcu_writes = asyncio.Queue(50)
        self.mcu_reads = asyncio.Queue(10)
        self.queues = [self.responses, self.commands, self.mcu_reads, self.mcu_writes, self.feedbackSignals] #self.encoder_reads]        
        self.trigger_acks = asyncio.Queue()
        self.encoder_acks = asyncio.Queue()
        self.mcu_ready = asyncio.Event()
        self.ultrasonic_dbg = asyncio.Queue(50)
        self.h7_runtime_ready = asyncio.Event()
        self.feedforward_acks = asyncio.Queue()
        self.motor_direction_acks = asyncio.Queue()

    def show_queue_size(self):
        for q in self.queues:
            print(q.qsize())
        print()
