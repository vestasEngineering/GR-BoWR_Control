import asyncio
import json
from logger import Logger
from queues import Queues

# This script is designed to listen for encoder readings from the queue of our four drive motors and average them.
# We want to allow the capability to actuate our shovel actuators based off of the encoder information.
#When the averaged encoder value hits specific threshold values (A,B,C,...) it will signal an actuator.

class Encoder:
    def __init__(self, logger: Logger, queues: Queues):
        """Initialize the Encoder wrapper with a logger and relevant queues."""
        self.logger = logger
        self.mcu_writes = queues.mcu_writes
        self.encoder_queue = queues.encoder_distance
        self.current_encoder_value = 0

    async def read_encoder(self):
        """Return the most recent encoder reading"""
        try:
            self.current_encoder_value = await asyncio.wait_for(self.encoder_queue.get(), timeout=2.0)
            if self.current_encoder_value is None:
                self.current_encoder_value = 0

            if isinstance(self.current_encoder_value, dict):
                return self.current_encoder_value.get("encoder_distance", 0)
            
            return self.current_encoder_value
        
        except asyncio.TimeoutError:
            self.logger.log.error("Timeout waiting for encoder reading.")
            self.current_encoder_value = 0
        
        return self.current_encoder_value

    
    async def reset_encoder(self):
        """Send a command to reset the encoder and await for confirmation."""
        command = {"action": "reset_encoder"}
        await self.mcu_writes.put_nowait(command)
        self.logger.log.info("Sent encoder reset command.")

        reset_confirmed = False
        try:
            while not reset_confirmed:
                msg = await asyncio.wait_for(self.encoder_queue.get(), timeout=2.0)
                if isinstance(msg, dict) and msg.get("status") == "Encoder reset":
                    reset_confirmed = True
        except asyncio.TimeoutError:
            self.logger.log.error("Timeout waiting for encoder reset confirmation.")
            return False
        
        self.logger.log.info("Encoder reset confirmed.")
        return True