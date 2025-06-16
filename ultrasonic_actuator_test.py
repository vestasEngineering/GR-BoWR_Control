import asyncio
from actuator_JSON import Actuator
from ultrasonic import Ultrasonic
from logger import Logger
from queues import Queues

async def ultrasonic_controller(ultrasonic: Ultrasonic):
    """
    Runs the ultrasonic sensor loop. It continuously waits on distance measurements from its queue,
    uses them to update the PID control for the drive motor speeds, and publishes speed commands.
    The very first time a reading in the valid range is seen, it signals the first_valid_event.
    """

    try:
        while True:
            #Wait for a new distance measurments
            while not ultrasonic.distance_queue.empty():
                distance = await ultrasonic.distance_queue.get()

            if distance == 40:
                ultrasonic.process_speed = 0.0
                ultrasonic.current_speed = 0.0
                print("Invald measurement detected (40). Setting motor speed to 0.")

            else:
                #Process the measurment (bad readings are replaced by the setpoint)
                ultrasonic.current_distance = ultrasonic.ignore_bad_measurements(distance)

                #If within the deadband, no correction is needed; otherwise run PID
                lower, upper = ultrasonic.correction_deadband
                if lower < ultrasonic.current_distance <= upper:
                    u = 0
                else:
                    u = ultrasonic.pid(ultrasonic.current_distance)

                ultrasonic.process_speed = round(max(ultrasonic.current_speed + u, 0), 4)
                ultrasonic.current_speed = ultrasonic.process_speed

            #Publish drive motor speed commands
            await ultrasonic.mcu_writes.put({"speed0": -1.0 * float(ultrasonic.process_speed)})
            await ultrasonic.mcu_writes.put({"speed1": -1.0 * float(ultrasonic.process_speed)})
            await ultrasonic.mcu_writes.put({"speed2": float(ultrasonic.process_speed)})
            await ultrasonic.mcu_writes.put({"speed3": float(ultrasonic.process_speed)})

            await asyncio.sleep(0.1)

    except asyncio.CancelledError:
        ultrasonic.logger.log.info("Ultrasonic controller cancelled")

        #Send STOP command to the MCU
        ultrasonic.mcu_writes.put_nowait({"action": "STOP"})

        #On cancellation, send zero speed commands to all drive motors.
        ultrasonic.mcu_writes.put_nowait({"speed0": -1.0 *  float(0.0)})
        ultrasonic.mcu_writes.put_nowait({"speed1": -1.0 *  float(0.0)})
        ultrasonic.mcu_writes.put_nowait({"speed2":        float(0.0)})
        ultrasonic.mcu_writes.put_nowait({"speed3":        float(0.0)})

    except Exception as e:
        ultrasonic.logger.log.error(f"Error in ultrasonic controller: {e}")

async def actuator_sequence_controller(actuator: Actuator, first_valid_event: asyncio.Event, logger: Logger):
    """
    Waits for the first valid ultrasonic measurment and then starts a timer:
        - After 25s, it sets actuator 0 to 0V and immediately sets actuator 1 to 5v
        - After another 25s, it sets actuator 1 to 0V and actuator 2 to 5v
    """
    try:
        #Set actuator 0 to 5.0v immediately 
        await actuator.set_actuator_voltage(0, 5.0)

        #Wait for the ultrasonic sensor to capture its first valid measurment
        await asyncio.wait_for(first_valid_event.wait(), timeout=120)
        logger.log.info("First valid measurement detected. Starting actuator sequence timer")

        #Wait 25 seconds
        await asyncio.sleep(10)
        logger.log.info("25s elapsed: Switching actuator 1 on and actuator 0 off.")
        await actuator.set_actuator_voltage(1, 5.0)
        await asyncio.sleep(10)
        await actuator.set_actuator_voltage(0, 0.0)

        #Wait 25 seconds
        await asyncio.sleep(10)
        logger.log.info("50s elapsed: Switching actuator 1 off and actuator 2 on.")
        await actuator.set_actuator_voltage(2, 5.0)
        await asyncio.sleep(10)
        await actuator.set_actuator_voltage(1, 0.0)

    except asyncio.CancelledError:
        logger.log.info("actuation cancelled")
        actuator.set_actuator_voltage(0, 0.0)
        actuator.set_actuator_voltage(1, 0.0)
        actuator.set_actuator_voltage(2, 0.0)
    except asyncio.TimeoutError:
        logger.log.warning("Ultrasonic validation timed out-actuatore sequence aborted.")
    except asyncio.CancelledError:
        logger.log.info("Actuator sequence controller cancelled")
    except Exception as e:
        logger.log.error(f"Error is actuator sequence controller: {e}")
