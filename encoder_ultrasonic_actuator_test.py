import asyncio
from actuator_JSON import Actuator
from ultrasonic import Ultrasonic
from encoder import Encoder
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

async def actuator_sequence_controller(actuator: Actuator, encoder: Encoder, logger: Logger):
    """
    This function controls the sequence of actuators based on encoder position.
        - After the stage 1 transition (100cm), it sets actuator 0 to 0V and immediately sets actuator 1 to 5v
        - After the stage 2 transition (500cm), it sets actuator 1 to 0V and actuator 2 to 5v
    """
    # Reset the encoder to 0
    encoder.mcu_writes.put_nowait({"action": "reset_encoder"})

    try:
        #Set actuator 0 to 5.0v immediately 
        await actuator.set_actuator_voltage(0, 4.0)

        #Flags to ensure each stage is triggered only once.
        stage1_triggered = False
        stage2_triggered = False

        while True:

            #Continously poll the encoder for its current position.
            pos = await asyncio.wait_for(encoder.read_encoder(), timeout=2.0)
            logger.log.info(F"Current encoder position: {pos}")

            #When the encoder surpasses 100, trigger stage 1. 
            if not stage1_triggered and pos >=100:
                stage1_triggered = True
                logger.log.info("Encoder threshold 100 reached: Activating actuator 1 and deactivating actuator 0.")
                await actuator.set_actuator_voltage(1, 4.0)
                asyncio.create_task(actuator.delayed_actuator_voltage(0, 0.0, 9))

            #When the encoder surpasses 500, trigger stage 2.
            if not stage2_triggered and pos >= 500:
                stage2_triggered = True
                logger.log.info("Encoder threshold 500 reached: Activating actuator 2 and deactivating actuator 1.")
                await actuator.set_actuator_voltage(2, 4.0)
                asyncio.create_task(actuator.delayed_actuator_voltage(1, 0.0, 9))


            #Short pause before polling again
            await asyncio.sleep(0.1)

    except asyncio.CancelledError:
        logger.log.info("actuation cancelled")

    except asyncio.TimeoutError:
        logger.log.warning("Ultrasonic validation timed out-actuatore sequence aborted.")

    except Exception as e:
        logger.log.error(f"Error is actuator sequence controller: {e}")
