import asyncio
import json
import math
from uuid import uuid4
from typing import Any, Dict, Iterable, List, Optional, Tuple
import websockets.exceptions
from websockets.server import serve
from service_runtime import ServiceRuntime
from configuration_manager import ConfigurationManager
from actuator_extension_depth import ActuatorExtensionManager
from encoder_persistence import (EncoderPersistenceCoordinator, EncoderRecoveryState)
from diagnostic_plan_runtime import DiagnosticPlanRuntime
from feedforward_config import FeedforwardManager
from motor_direction_config import (FACTORY_MOTOR_DIRECTIONS, MotorDirectionManager,)
from health import HealthModel
from logger import Logger
from queues import Queues


WHEEL_DIAMETER_M = 0.048
ENCODER_CPR = 4096

MODULES = [
    {"id": "motor_1", "name": "Motor 1 (Axis 0)", "category": "motor", "index": 0},
    {"id": "motor_2", "name": "Motor 2 (Axis 1)", "category": "motor", "index": 1},
    {"id": "motor_3", "name": "Motor 3 (Axis 2)", "category": "motor", "index": 2},
    {"id": "motor_4", "name": "Motor 4 (Axis 3)", "category": "motor", "index": 3},

    {"id": "actuator_1", "name": "Actuator A (Ch 0)", "category": "actuator", "channel": 0},
    {"id": "actuator_2", "name": "Actuator B (Ch 1)", "category": "actuator", "channel": 1},
    {"id": "actuator_3", "name": "Actuator C (Ch 2)", "category": "actuator", "channel": 2},
    {"id": "actuator_4", "name": "Actuator D (Ch 3)", "category": "actuator", "channel": 3},

    {"id": "ultrasonic", "name": "Ultrasonic Sensor", "category": "sensor", "sensor": "ultrasonic"},
    {"id": "battery", "name": "Battery", "category": "sensor", "sensor": "battery"},

    {"id": "andon_ring", "name": "Andon Ring", "category": "andon"},
    {"id": "clamp_sensor", "name": "Clamp Sensor", "category": "sensor", "sensor": "clamp"},
]
MODULE_BY_ID = {m["id"]: m for m in MODULES}

class WebsocketServer():
    def __init__(self, logger:Logger, queues:Queues, job_manager):
        self.logger = logger
        self.commands = queues.commands
        self.responses = queues.responses
        self.mcu_reads = queues.mcu_reads
        self.mcu_writes = queues.mcu_writes
        self.connected = False
        self._active_hmi_connections = set()
        self.shutdown_event = asyncio.Event()
        self.health = HealthModel()
        self.latest_health: Optional[dict] = None
        #self._active_connections = set[WebsocketServerProtocol] = set()
        self.trigger_acks = queues.trigger_acks
        self.encoder_acks = queues.encoder_acks
        self.process_start_acks = queues.process_start_acks
        self.actuator_extension_acks = queues.actuator_extension_acks
        self._process_start_lock = asyncio.Lock()
        self.feedforward_acks = queues.feedforward_acks
        self.mcu_ready = queues.mcu_ready
        self.job_manager = job_manager
        self.job_manager = job_manager

        self.drive_direction_acks = (
            queues.drive_direction_acks
        )

        self.drive_direction_test_acks = (
            queues.drive_direction_test_acks
        )

        self.drive_direction_test_results = (
            queues.drive_direction_test_results
        )

        self._drive_direction_test_lock = (
            asyncio.Lock()
        )

        if job_manager is None:
            raise ValueError("job_manager is required")

        self.service_runtime = ServiceRuntime(
            db=job_manager.db,
            mcu_writes=self.mcu_writes,
            modules=MODULES,
            logger=self.logger,
        )

        self.diagnostic_plan_runtime = DiagnosticPlanRuntime(
            service_runtime=self.service_runtime,
            logger=self.logger,
        )

        self._feedforward_apply_lock = (
            asyncio.Lock()
        )

        self.motor_direction_acks = (
            queues.motor_direction_acks
        )

        self.latest_process_status: Optional[dict] = None
        self.latest_actuator_status: Optional[dict] = None

        self._latest_actuator_status_received_monotonic: Optional[
            float
        ] = None

        self._actuator_status_freshness_limit_s = 2.5

        self.feedforward_manager = FeedforwardManager(
            db=job_manager.db,
            mcu_writes=self.mcu_writes,
            ack_queue=self.feedforward_acks,
            process_active=self._process_is_confirmed_active,
            logger=self.logger,
            timeout_s=5.0,
        )

        self.actuator_extension_manager = ActuatorExtensionManager(
            db=job_manager.db,
            mcu_writes=self.mcu_writes,
            ack_queue=self.actuator_extension_acks,
            process_active=self._process_is_confirmed_active,
            logger=self.logger,
            timeout_s=5.0,
        )

        self.config_manager = ConfigurationManager(
            db=job_manager.db,
            mcu_writes=self.mcu_writes,
            trigger_sender=self.send_triggers_incrementally,
            actuator_extension_manager=(
                self.actuator_extension_manager
            ),
            logger=self.logger,
        )

        self.motor_direction_manager = (
            MotorDirectionManager(
                db=job_manager.db,
                mcu_writes=self.mcu_writes,
                ack_queue=(
                    self.drive_direction_acks
                ),
                process_active=(
                    self
                    ._process_is_confirmed_active
                ),
                logger=self.logger,
                timeout_s=8.0,
            )
        )

        self.h7_runtime_ready = queues.h7_runtime_ready

        self.encoder_startup_ready = False
        self.encoder_startup_error: Optional[str] = None

        self.configuration_loaded_to_mcu = False
        self.configuration_apply_error: Optional[str] = None

        self._feedforward_runtime_loaded = False
        self._feedforward_apply_error: Optional[str] = None

        self._motor_direction_runtime_loaded = False
        self._motor_direction_apply_error: Optional[str] = None

        self.startup_complete = asyncio.Event()
        self.startup_error: Optional[str] = None
        self._startup_task: Optional[asyncio.Task] = None
        self._initial_runtime_configuration_attempted = False

        self._configuration_apply_lock = asyncio.Lock()
        self._configuration_reapply_event = asyncio.Event()
        self._configuration_reapply_task: Optional[asyncio.Task] = None

        self._last_actuator_fault_signature = (0, False)

        self.encoder_persistence = EncoderPersistenceCoordinator(
            db=job_manager.db,
            mcu_writes=self.mcu_writes,
            encoder_acks=self.encoder_acks,
            logger=self.logger,
            publish=self.responses.put,
            checkpoint_period_s=2.0,
            checkpoint_distance_mm=25.0,
            restore_tolerance_mm=5.0,
            transaction_timeout_s=8.0,
        )

        self.job_manager.set_encoder_context_provider(
            self.encoder_persistence.event_context_snapshot
        )


    async def run(self):
        """
        Start the WebSocket listener immediately.

        MCU encoder recovery and persisted configuration application run in a
        separate startup task so an MCU timeout cannot prevent the HMI from
        connecting to port 5000.

        Robot actions that depend on initialization must continue to check their
        own readiness conditions before being accepted.
        """
        await self.encoder_persistence.start_serial_runtime()

        health_task = asyncio.create_task(
            self.health_pump(),
            name="websocket-health-pump",
        )

        self._startup_task = asyncio.create_task(
            self.initialize_robot_runtime(),
            name="websocket-robot-runtime-initialization",
        )

        self._configuration_reapply_task = asyncio.create_task(
            self.configuration_reapply_worker(),
            name="websocket-configuration-reapply",
        )

        try:
            self.logger.log.info(
                "Starting HMI WebSocket server on 0.0.0.0:5000."
            )

            async with serve(
                self.connection_handler,
                "0.0.0.0",
                5000,
                ping_interval=20,
                ping_timeout=20,
                close_timeout=5,
            ):
                self.logger.log.info(
                    "HMI WebSocket server listening on 0.0.0.0:5000."
                )

                await self.shutdown_event.wait()

        except OSError:
            self.logger.log.exception(
                "Failed to bind HMI WebSocket server to 0.0.0.0:5000."
            )
            raise

        finally:
            tasks = [health_task]

            if self._startup_task is not None:
                tasks.append(self._startup_task)

            if self._configuration_reapply_task is not None:
                tasks.append(self._configuration_reapply_task)

            for task in tasks:
                task.cancel()

            await asyncio.gather(
                *tasks,
                return_exceptions=True,
            )

            await self.diagnostic_plan_runtime.shutdown()

    async def apply_persisted_configuration(
        self,
    ) -> bool:
        async with self._configuration_apply_lock:
            if self.configuration_loaded_to_mcu:
                return True

            try:
                catalog = self.config_manager.catalog()

                selection = catalog.get(
                    "selection",
                    {},
                )

                robot_id = str(
                    selection.get(
                        "robot_id",
                        "",
                    )
                    or ""
                ).strip()

                blade_id = str(
                    selection.get(
                        "blade_id",
                        "",
                    )
                    or ""
                ).strip()

                if not robot_id or not blade_id:
                    raise RuntimeError(
                        "No persisted robot and blade "
                        "selection is available."
                    )

                self.logger.log.info(
                    "Applying persisted robot configuration: "
                    f"robot_id={robot_id}, "
                    f"blade_id={blade_id}"
                )

                result = await self.config_manager.apply(
                    robot_id,
                    blade_id,
                )

                values = result.get(
                    "effective_values",
                    [],
                )

                if not isinstance(values, list) or not values:
                    raise RuntimeError(
                        "The applied profile contains "
                        "no transition values."
                    )

                self.configuration_loaded_to_mcu = True
                self.configuration_apply_error = None

                self.logger.log.info(
                    "Persisted configuration applied to H7: "
                    f"robot_id={robot_id}, "
                    f"blade_id={blade_id}, "
                    f"transition_count={len(values)}"
                )

                await self.responses.put({
                    "type":
                        "configuration_runtime_status",
                    "loaded": True,
                    "robot_id": robot_id,
                    "blade_id": blade_id,
                    "transition_count": len(values),
                })

                return True

            except asyncio.CancelledError:
                raise

            except Exception as error:
                self.configuration_loaded_to_mcu = False
                self.configuration_apply_error = str(error)

                self.logger.log.exception(
                    "Failed to apply persisted "
                    "configuration to H7."
                )

                await self.responses.put({
                    "type":
                        "configuration_runtime_status",
                    "loaded": False,
                    "error": str(error),
                })

                return False

    async def apply_persisted_feedforward(
        self,
    ) -> bool:
        async with self._feedforward_apply_lock:
            if self._feedforward_runtime_loaded:
                return True

            try:
                persisted_values = (
                    self.job_manager
                    .db
                    .get_feedforward_configuration()
                )

                self.logger.log.info(
                    "Applying persisted feedforward "
                    "configuration to H7: "
                    f"values={persisted_values}"
                )

                result = (
                    await self.feedforward_manager
                    .apply_persisted()
                )

                self._feedforward_runtime_loaded = True
                self._feedforward_apply_error = None

                self.logger.log.info(
                    "Persisted feedforward "
                    "configuration confirmed by H7: "
                    f"transaction_id="
                    f"{result.transaction_id} "
                    f"values={result.values}"
                )

                await self.responses.put({
                    "type":
                        "feedforward_runtime_status",
                    "loaded": True,
                    "transaction_id":
                        result.transaction_id,
                    "values":
                        result.values,
                })

                return True

            except asyncio.CancelledError:
                raise

            except Exception as error:
                self._feedforward_runtime_loaded = False
                self._feedforward_apply_error = str(
                    error
                )

                self.logger.log.exception(
                    "Failed to apply persisted "
                    "feedforward configuration to H7."
                )

                await self.responses.put({
                    "type":
                        "feedforward_runtime_status",
                    "loaded": False,
                    "error": str(error),
                })

                return False

    async def initialize_robot_runtime(self):
        """
        Wait for H7 boot completion, then initialize configuration and
        encoder recovery as separate startup requirements.

        A failure in one stage must not prevent the other stage from
        being attempted. Process start remains blocked until both are
        valid.
        """
        try:
            self.logger.log.info(
                "Waiting for H7 runtime readiness."
            )

            try:
                await asyncio.wait_for(
                    self.h7_runtime_ready.wait(),
                    timeout=30.0,
                )

            except asyncio.TimeoutError:
                self.startup_error = (
                    "H7 runtime readiness timed out."
                )

                self.logger.log.error(
                    self.startup_error
                )

                await self.responses.put({
                    "type": "startup_status",
                    "state": "fault",
                    "ready": False,
                    "stage": "h7_runtime",
                    "error": "h7_runtime_timeout",
                    "message": self.startup_error,
                })

                return

            self.logger.log.info(
                "H7 runtime readiness achieved. Beginning runtime initialization."
            )

            # Apply all volatile H7 configuration before
            # allowing process start.
            #
            # Transition and feedforward configuration use
            # independent acknowledgement paths. Both must
            # be confirmed.
            self._initial_runtime_configuration_attempted = True

            transition_ready = (
                await self
                .apply_persisted_configuration()
            )

            feedforward_ready = (
                await self
                .apply_persisted_feedforward()
            )

            motor_direction_ready = (
                await self
                .apply_persisted_motor_directions()
            )

            configuration_ready = (
                transition_ready
                and feedforward_ready
                and motor_direction_ready
            )

            self.logger.log.info(
                "Initial H7 configuration results: "
                f"transition_ready={transition_ready} "
                f"feedforward_ready={feedforward_ready} "
                f"motor_direction_ready={motor_direction_ready} "
                f"configuration_ready={configuration_ready}"
            )

            # Encoder recovery is a separate readiness requirement.
            self.logger.log.info(
                "Beginning MCU encoder startup recovery."
            )

            try:
                await self.encoder_persistence.startup_recovery(
                    ready_timeout_s=20.0,
                )

                self.encoder_startup_ready = (
                    self.encoder_persistence.state
                    == EncoderRecoveryState.VALID
                )

                if not self.encoder_startup_ready:
                    raise RuntimeError(
                        "Encoder recovery completed without "
                        "reaching VALID state: "
                        f"{self.encoder_persistence.state}"
                    )

                self.encoder_startup_error = None

                self.logger.log.info(
                    "MCU encoder startup recovery completed: "
                    f"state={self.encoder_persistence.state}"
                )

            except asyncio.TimeoutError as error:
                self.encoder_startup_ready = False
                self.encoder_startup_error = (
                    "Encoder startup recovery timed out."
                )

                self.logger.log.error(
                    self.encoder_startup_error
                )

                await self.responses.put({
                    "type": "startup_status",
                    "state": "fault",
                    "ready": False,
                    "stage": "encoder_recovery",
                    "error": "encoder_recovery_timeout",
                    "message":
                        self.encoder_startup_error,
                    "configuration_loaded": (
                        self.configuration_loaded_to_mcu
                        and self._feedforward_runtime_loaded
                        and self._motor_direction_runtime_loaded
                    ),
                    "transition_configuration_loaded":
                        self.configuration_loaded_to_mcu,
                    "feedforward_configuration_loaded":
                        self._feedforward_runtime_loaded,
                    "motor_direction_loaded":
                        self._motor_direction_runtime_loaded,
                    "encoder_valid": False,
                    "encoder_state": (
                        self.encoder_persistence
                        .state.value
                    ),
                })

            except asyncio.CancelledError:
                raise

            except Exception as error:
                self.encoder_startup_ready = False
                self.encoder_startup_error = str(error)

                self.logger.log.exception(
                    "Encoder startup recovery failed."
                )

                await self.responses.put({
                    "type": "startup_status",
                    "state": "fault",
                    "ready": False,
                    "stage": "encoder_recovery",
                    "error": "encoder_recovery_failed",
                    "message": str(error),
                })

            runtime_ready = (
                configuration_ready
                and self.encoder_startup_ready
            )

            if runtime_ready:
                self.startup_error = None
                self.startup_complete.set()

                await self.responses.put({
                    "type": "startup_status",
                    "state": "ready",
                    "ready": True,
                    "stage": "complete",
                    "configuration_loaded": True,
                    "transition_configuration_loaded": True,
                    "feedforward_configuration_loaded": True,
                    "motor_direction_loaded": True,
                    "encoder_valid": True,
                    "encoder_state":
                        self.encoder_persistence.state.value,
                })

                self.logger.log.info(
                    "Robot runtime initialization completed."
                )

                return

            self.startup_complete.clear()

            errors = []

            if not transition_ready:
                errors.append(
                    self.configuration_apply_error
                    or (
                        "Transition configuration "
                        "was not loaded."
                    )
                )

            if not feedforward_ready:
                errors.append(
                    self._feedforward_apply_error
                    or (
                        "Feedforward configuration "
                        "was not loaded."
                    )
                )

            if not motor_direction_ready:
                errors.append(
                    self._motor_direction_apply_error
                    or (
                        "Motor direction configuration "
                        "was not loaded."
                    )
                )

            if not self.encoder_startup_ready:
                errors.append(
                    self.encoder_startup_error
                    or "Encoder is not valid."
                )

            self.startup_error = " ".join(
                error
                for error in errors
                if error
            )

            await self.responses.put({
                "type": "startup_status",
                "state": "fault",
                "ready": False,
                "stage": "incomplete",
                "configuration_loaded": (
                    self.configuration_loaded_to_mcu
                    and self._feedforward_runtime_loaded
                    and self._motor_direction_runtime_loaded
                ),
                "transition_configuration_loaded":
                    self.configuration_loaded_to_mcu,
                "feedforward_configuration_loaded":
                    self._feedforward_runtime_loaded,
                "motor_direction_loaded":
                    self._motor_direction_runtime_loaded,
                "encoder_valid":
                    self.encoder_startup_ready,
                "encoder_state":
                    self.encoder_persistence.state.value,
                "message":
                    self.startup_error,
                "configuration_errors": {
                    "transition":
                        self.configuration_apply_error,
                    "feedforward":
                        self._feedforward_apply_error,
                    "motor_direction":
                        self._motor_direction_apply_error,
                    "encoder":
                        self.encoder_startup_error,
                },
            })

            self.logger.log.error(
                "Robot runtime initialization incomplete: "
                f"transition_configuration_loaded="
                f"{self.configuration_loaded_to_mcu}, "
                f"feedforward_configuration_loaded="
                f"{self._feedforward_runtime_loaded}, "
                f"motor_direction_loaded="
                f"{self._motor_direction_runtime_loaded}, "
                f"encoder_valid="
                f"{self.encoder_startup_ready}, "
                f"encoder_state="
                f"{self.encoder_persistence.state.value}, "
                f"error={self.startup_error}"
            )

        except asyncio.CancelledError:
            self.logger.log.info(
                "Robot runtime initialization cancelled."
            )
            raise

    async def configuration_reapply_worker(
        self,
    ) -> None:
        while True:
            await self._configuration_reapply_event.wait()

            self._configuration_reapply_event.clear()

            try:
                self.logger.log.info(
                    "Runtime configuration "
                    "reapplication requested."
                )

                await asyncio.wait_for(
                    self.h7_runtime_ready.wait(),
                    timeout=30.0,
                )

                self.logger.log.info(
                    "H7 runtime is ready. "
                    "Reapplying persisted transition "
                    "and feedforward configuration."
                )

                transition_ready = (
                    await self
                    .apply_persisted_configuration()
                )

                feedforward_ready = (
                    await self
                    .apply_persisted_feedforward()
                )

                motor_direction_ready = (
                    await self
                    .apply_persisted_motor_directions()
                )

                configuration_ready = (
                    transition_ready
                    and feedforward_ready
                    and motor_direction_ready
                )

                if not transition_ready:
                    self.logger.log.error(
                        "Transition configuration "
                        "reapplication failed: "
                        f"{self.configuration_apply_error}"
                    )

                if not feedforward_ready:
                    self.logger.log.error(
                        "Feedforward configuration "
                        "reapplication failed: "
                        f"{self._feedforward_apply_error}"
                    )

                if not motor_direction_ready:
                    self.logger.log.error(
                        "Motor direction configuration "
                        "reapplication failed: "
                        f"{self._motor_direction_apply_error}"
                    )

                if not configuration_ready:
                    self.startup_complete.clear()

                    errors = []

                    if not transition_ready:
                        errors.append(
                            self.configuration_apply_error
                            or (
                                "Transition configuration "
                                "reapplication failed."
                            )
                        )

                    if not feedforward_ready:
                        errors.append(
                            self._feedforward_apply_error
                            or (
                                "Feedforward configuration "
                                "reapplication failed."
                            )
                        )

                    if not motor_direction_ready:
                        errors.append(
                            self._motor_direction_apply_error
                            or (
                                "Motor direction configuration "
                                "reapplication failed."
                            )
                        )

                    self.startup_error = " ".join(
                        error
                        for error in errors
                        if error
                    )

                    await self.responses.put({
                        "type":
                            "configuration_runtime_status",
                        "loaded": False,
                        "transition_configuration_loaded":
                            transition_ready,
                        "feedforward_configuration_loaded":
                            feedforward_ready,
                        "motor_direction_loaded":
                            motor_direction_ready,
                        "error":
                            self.startup_error,
                        "configuration_errors": {
                            "transition":
                                self.configuration_apply_error,
                            "feedforward":
                                self._feedforward_apply_error,
                            "motor_direction":
                                self._motor_direction_apply_error,
                        },
                    })

                    continue

                self.startup_error = None

                if self.encoder_startup_ready:
                    self.startup_complete.set()

                await self.responses.put({
                    "type":
                        "configuration_runtime_status",
                    "loaded": True,
                    "transition_configuration_loaded":
                        True,
                    "feedforward_configuration_loaded":
                        True,
                    "motor_direction_loaded":
                        True,
                })

                self.logger.log.info(
                    "Persisted transition, feedforward, and "
                    "motor direction configuration were "
                    "confirmed for the restored H7 serial "
                    "session. Motion remains stopped until "
                    "explicitly started."
                )

            except asyncio.TimeoutError:
                error_message = (
                    "H7 runtime readiness timed out "
                    "during configuration "
                    "reapplication."
                )

                self.invalidate_h7_runtime_configuration(
                    error_message
                )

                self.logger.log.error(
                    error_message
                )

            except asyncio.CancelledError:
                raise

            except Exception as error:
                error_message = str(
                    error
                )

                self.invalidate_h7_runtime_configuration(
                    error_message
                )

                self.logger.log.exception(
                    "Unexpected runtime "
                    "configuration reapplication "
                    "failure."
                )

    async def connection_handler(self, websocket):
        """
        Handles one HMI WebSocket connection.

        The WebSocket server remains running after the tablet disconnects so
        that the HMI can reconnect automatically.
        """
        self._active_hmi_connections.add(websocket)
        await self.publish_hmi_connection_state()

        self.logger.log.info(
            "HMI WebSocket client connected."
        )

        await self.send_response(
            websocket,
            {
                "type": "startup_status",
                "state": (
                    "ready"
                    if self.startup_complete.is_set()
                    else (
                        "fault"
                        if self.startup_error is not None
                        else "initializing"
                    )
                ),
                "ready": self.startup_complete.is_set(),
                "error": self.startup_error,
            },
        )

        try:
            # Send the most recent health snapshot immediately.
            if self.latest_health is not None:
                await websocket.send(
                    json.dumps({
                        "response": self.latest_health,
                    })
                )

            # Send the most recent process state immediately.
            if self.latest_process_status is not None:
                await websocket.send(
                    json.dumps({
                        "response": self.latest_process_status,
                    })
                )
        
            # Send the most recent actuator state immediately.
            if self.latest_actuator_status is not None:
                await websocket.send(
                    json.dumps({
                        "response": self.latest_actuator_status,
                    })
                )

            await self.mcu_writes.put({"action": "get_actuator_status"})

            consumer_task = asyncio.create_task(
                self.consumer(websocket)
            )

            producer_task = asyncio.create_task(
                self.response_producer(websocket)
            )

            # End this connection handler as soon as either the consumer
            # or producer finishes.
            done, pending = await asyncio.wait(
                {
                    consumer_task,
                    producer_task,
                },
                return_when=asyncio.FIRST_COMPLETED,
            )

            # Cancel the remaining task. This is especially important when
            # response_producer is waiting on self.responses.get().
            for task in pending:
                task.cancel()

            # Retrieve completed-task exceptions so asyncio does not report
            # "Task exception was never retrieved".
            for task in done:
                try:
                    task.result()
                except asyncio.CancelledError:
                    pass
                except websockets.exceptions.ConnectionClosed:
                    pass
                except Exception:
                    self.logger.log.exception(
                        "WebSocket connection task failed."
                    )

            # Wait for cancelled tasks to finish cleaning up.
            if pending:
                await asyncio.gather(
                    *pending,
                    return_exceptions=True,
                )

        except websockets.exceptions.ConnectionClosed:
            self.logger.log.info(
                "HMI WebSocket connection closed."
            )

        except Exception:
            self.logger.log.exception(
                "Unexpected WebSocket connection-handler error."
            )

        finally:
            self._active_hmi_connections.discard(websocket)
            await self.publish_hmi_connection_state()

            self.logger.log.info(
                "HMI WebSocket client disconnected."
            )

            # Do not call self.shutdown_event.set() here.
            #
            # The server must remain running so the tablet can reconnect.

    async def publish_hmi_connection_state(self) -> None:
        """Publish the current HMI WebSocket availability to the H7."""
        connected = bool(self._active_hmi_connections)
        self.connected = connected

        await self.mcu_writes.put({
            "action": "set_hmi_connected",
            "connected": connected,
        })

        self.logger.log.info(
            "Published HMI connection state to H7: "
            f"connected={connected}"
        )

    async def health_pump(self):
        """
        Consumes MCU messages (andon_diag, boot_health, test_result), computes a consolidated
        health snapshot, and pushes it to self.responses whenever it changes.
        """
        while True:
            msg = await self.mcu_reads.get()
            try:
                if (
                    isinstance(msg, dict)
                    and msg.get("type") == "diagnostic_progress"
                ):
                    self.logger.log.info(
                        "Forwarding diagnostic progress to HMI: "
                        f"run_id={msg.get('run_id')} "
                        f"transaction_id={msg.get('transaction_id')} "
                        f"module_id={msg.get('id')} "
                        f"category={msg.get('category')} "
                        f"phase={msg.get('phase')} "
                        f"channel={msg.get('channel')} "
                        f"confirmation_timeout_ms="
                        f"{msg.get('confirmation_timeout_ms')}"
                    )

                    await self.responses.put(msg)
                    continue
                # 1) Pass-through module test results
                if (
                    isinstance(msg, dict)
                    and msg.get("type") == "test_result"
                ):
                    self.logger.log.info(
                        "Diagnostic terminal result received: "
                        f"run_id={msg.get('run_id')} "
                        f"transaction_id={msg.get('transaction_id')} "
                        f"module_id="
                        f"{msg.get('id') or msg.get('module_id')} "
                        f"category={msg.get('category')} "
                        f"pass={msg.get('pass')} "
                        f"reason={msg.get('reason')}"
                    )

                    self.service_runtime.accept_test_result(
                        msg
                    )

                    await self.responses.put(msg)
                    continue

                if isinstance(msg, dict) and msg.get("type") == "encoder_session":
                    await self.encoder_persistence.handle_session_event(msg)

                    state = str(msg.get("state", "")).lower()

                    if state == "power_lost":
                        self.job_manager.log_event(
                            "motor_controller_power_lost",
                            msg,
                        )
                    elif state == "waiting_for_restore":
                        self.job_manager.log_event(
                            "motor_controller_power_restored",
                            msg,
                        )

                    await self.responses.put(msg)
                    continue

                if isinstance(msg, dict) and msg.get("type") == "encoder":
                    accepted = await self.encoder_persistence.observe(msg)
                    if accepted is not None:
                        await self.responses.put(accepted)
                    # Invalid samples are intentionally not forwarded. The recovery
                    # message preserves the last valid display value in Flutter.
                    continue

                if (
                    isinstance(msg, dict)
                    and msg.get("type")
                    == "process_status"
                ):
                    self.latest_process_status = dict(msg)

                    if msg.get("active") is False:
                        # Save the newest encoder telemetry at a process
                        # stop or fault boundary.
                        await self.encoder_persistence.flush()

                    self.logger.log.info(
                        "Forwarding process status "
                        "to HMI: "
                        f"state={msg.get('state')} "
                        f"active={msg.get('active')} "
                        f"reason={msg.get('reason')}"
                    )

                    if self.job_manager is not None:
                        try:
                            active_job = (
                                self.job_manager
                                .get_active_job()
                            )

                            if active_job is not None:
                                self.job_manager.log_event(
                                    "process_status",
                                    msg,
                                )

                                is_active = (
                                    msg.get("active") is True
                                )

                                process_state = str(
                                    msg.get(
                                        "state",
                                        "",
                                    )
                                ).strip().lower()

                                process_is_running = (
                                    is_active
                                    or process_state
                                    in {
                                        "running",
                                        "active",
                                        "started",
                                    }
                                )

                                if (
                                    process_is_running
                                    and active_job.get("state")
                                    != "RUNNING"
                                ):
                                    updated_job = (
                                        self.job_manager
                                        .mark_running()
                                    )

                                    if updated_job is not None:
                                        self.logger.log.info(
                                            "Job marked RUNNING "
                                            "from MCU process status: "
                                            f"job_uuid="
                                            f"{updated_job['job_uuid']}"
                                        )

                        except Exception:
                            self.logger.log.exception(
                                "Failed to persist "
                                "process status."
                            )

                    await self.responses.put(msg)
                    continue

                if (
                    isinstance(msg, dict)
                    and msg.get("type") == "cm5_transport_event"
                ):
                    event_type = str(
                        msg.get(
                            "event",
                            "transport_event",
                        )
                    )

                    if (
                        event_type
                        == "mcu_serial_connection_restored"
                    ):
                        restoration_reason = (
                            "Waiting for H7 runtime readiness "
                            "after serial connection restoration."
                        )

                        self.invalidate_h7_runtime_configuration(
                            restoration_reason
                        )

                        if (
                            self
                            ._initial_runtime_configuration_attempted
                        ):
                            self.logger.log.info(
                                "Scheduling transition, feedforward, "
                                "and motor direction reapplication "
                                "for the restored H7 serial session."
                            )

                            self._configuration_reapply_event.set()

                        else:
                            self.logger.log.info(
                                "Initial runtime initialization owns "
                                "configuration application for this "
                                "serial session."
                            )

                        self.logger.log.warning(
                            "MCU serial session restored. "
                            "Transition, feedforward, and motor "
                            "direction runtime configuration are "
                            "now unconfirmed. Motion remains stopped "
                            "until configuration is reapplied and a "
                            "new operator start is accepted."
                        )


                    elif (
                        event_type
                        == "mcu_serial_connection_lost"
                    ):
                        self.service_runtime.transport_lost(
                            msg
                        )

                        self._latest_actuator_status_received_monotonic = (
                            None
                        )

                        self.invalidate_h7_runtime_configuration(
                            "MCU serial connection was lost."
                        )

                        self.logger.log.warning(
                            "MCU serial session lost. All H7 "
                            "runtime configuration confirmations "
                            "were invalidated. Process start is "
                            "blocked until the H7 runtime is ready "
                            "and all configuration domains are "
                            "confirmed again."
                        )

                    if self.job_manager is not None:
                        try:
                            self.job_manager.log_event(
                                event_type,
                                msg,
                            )
                        except Exception:
                            self.logger.log.exception(
                                "Failed to persist MCU serial "
                                f"transport event: {event_type}"
                            )

                    await self.responses.put(msg)
                    continue

                # SSv Glue Card status and temporary firmware diagnostics
                if (
                    isinstance(msg, dict)
                    and msg.get("type")
                    == "actuator_status"
                ):
                    self.latest_actuator_status = dict(
                        msg
                    )

                    self._latest_actuator_status_received_monotonic = (
                        asyncio.get_running_loop().time()
                    )

                    if self.job_manager:
                        try:
                            jam_mask = int(
                                msg.get(
                                    "jam_mask",
                                    0,
                                )
                            )

                            pcb_fault = (
                                msg.get("pcb_fault")
                                is True
                            )

                            signature = (
                                jam_mask,
                                pcb_fault,
                            )

                            if (
                                signature
                                != self._last_actuator_fault_signature
                            ):
                                previous = (
                                    self._last_actuator_fault_signature
                                )

                                # Update the edge state even if no job is active. This prevents
                                # repeated logging if a job becomes active while the same fault
                                # remains asserted.
                                self._last_actuator_fault_signature = signature

                                if (
                                    self.job_manager is not None
                                    and (
                                        jam_mask != 0
                                        or pcb_fault
                                    )
                                ):
                                    self.job_manager.log_event(
                                        "actuator_fault_activated",
                                        {
                                            **msg,
                                            "previous_jam_mask": (
                                                previous[0]
                                            ),
                                            "previous_pcb_fault": (
                                                previous[1]
                                            ),
                                        },
                                    )

                                elif (
                                    self.job_manager is not None
                                    and (
                                        previous[0] != 0
                                        or previous[1]
                                    )
                                ):
                                    self.job_manager.log_event(
                                        "actuator_fault_cleared",
                                        {
                                            **msg,
                                            "previous_jam_mask": (
                                                previous[0]
                                            ),
                                            "previous_pcb_fault": (
                                                previous[1]
                                            ),
                                        },
                                    )

                        except (
                            TypeError,
                            ValueError,
                        ):
                            self.logger.log.warning(
                                "Received invalid actuator fault status: "
                                f"jam_mask={msg.get('jam_mask')!r} "
                                f"pcb_fault={msg.get('pcb_fault')!r}"
                            )

                        except Exception:
                            self.logger.log.exception(
                                "Failed to persist actuator fault transition."
                            )

                    await self.responses.put(msg)
                    continue

                if (
                    isinstance(msg, dict)
                    and msg.get("type")
                    in (
                        "firmware_features",
                        "main_loop_heartbeat",
                        "loop_checkpoint",
                    )
                ):
                    await self.responses.put(msg)
                    continue

                if (
                    isinstance(msg, dict)
                    and msg.get("type") in (
                        "encoder_reset",
                        "encoder_set",
                    )
                ):
                    await self.responses.put(msg)
                    continue

                if (
                    isinstance(msg, dict)
                    and msg.get("type") == "status"
                    and msg.get("module") == "runtime"
                    and msg.get("status") == "runtime_ready"
                ):

                    self.logger.log.info(
                        "H7 runtime-ready message received. "
                        "MCU command processing is available."
                    )

                    await self.responses.put(msg)

                    continue

                # 2) Quick synthesis from boot_health (immediate snapshot for HMI)
                if (
                    isinstance(msg, dict)
                    and msg.get("type") == "boot_health"
                ):
                    bh = msg

                    self.logger.log.info(
                        "H7 boot-health message received. "
                        "Waiting for runtime_ready before "
                        "sending MCU commands."
                    )
                    state = "ok" if bh.get("ok") else "fault"
                    synth = {
                        "type": "health",
                        "state": state,
                        "sources": ["boot_health"],
                        "ts": bh.get("ts_ms"),
                        "boot_checks": bh.get("checks", {}),
                        "firmware": bh.get("firmware")
                    }
                    self.latest_health = synth
                    await self.responses.put(synth)
                    self.logger.log.info(f"[HEALTH/SYNTH] from boot_health state={state}")
                    # Don't 'continue'—also let HealthModel see it below

                if (
                    isinstance(msg, dict)
                    and msg.get("type") in (
                        "encoder",
                        "actuator_status",
                        "main_loop_heartbeat",
                        "loop_checkpoint",
                        "firmware_features",
                    )
                ):
                    await self.responses.put(msg)
                    continue
                    
                if isinstance(msg, dict) and msg.get("type") == "fw_version":

                    if self.job_manager:
                        self.job_manager.update_identity(msg)

                    new_snap = self.health.update_from_mcu(msg)

                    if new_snap:
                        self.latest_health = new_snap
                        await self.responses.put(new_snap)

                    continue

                # 3) Optional: map andon_diag to a minimal health (keeps UI lively)
                if isinstance(msg, dict) and msg.get("type") == "andon_diag":
                    ad = msg
                    # You can tighten this mapping based on your AndonManager policy
                    # Example simple mapping:
                    code_state = (ad.get("state") or "").lower()
                    # normalize a few to health states
                    mapping = {
                        "green": "ok",
                        "yellow": "warning",
                        "blue": "degraded",
                        "red": "fault",
                        "blink_red": "fault",
                        "blink_yellow": "warning",
                        "blink_green": "ok",
                        "blink_blue": "degraded",
                        "off": "unknown",
                    }
                    state = mapping.get(code_state, "unknown")
                    synth = {
                        "type": "health",
                        "state": state,
                        "sources": ["andon_diag"],
                        "ts": ad.get("ms"),
                        "andon": ad,  # include full original for raw dump
                    }
                    self.latest_health = synth
                    await self.responses.put(synth)
                    self.logger.log.info(f"[HEALTH/SYNTH] from andon_diag mapped_state={state}")
                    # Don't continue; HealthModel may want it too

                # 4) Let the canonical model run—if it emits, prefer it
                new_snap = self.health.update_from_mcu(msg)
                if new_snap:
                    # IMPORTANT: make sure new_snap includes {"type": "health", ...}
                    if new_snap.get("type") != "health":
                        new_snap["type"] = "health"
                    self.latest_health = new_snap
                    await self.responses.put(new_snap)
                    self.logger.log.info(
                        f"[HEALTH] state={new_snap.get('state')} sources={new_snap.get('sources')}"
                    )

            except Exception as e:
                self.logger.log.exception(f"health_pump error: {e}")


#==============================================================
# message receiver
#==============================================================

    async def send_response(
        self,
        websocket,
        payload: Dict[str, Any],
        *,
        request_id: Any = None,
    ) -> None:
        """
        Send a response directly to the HMI that made the request.

        Request responses should not use the shared responses queue.
        A shared queue could deliver a response to the wrong HMI if
        multiple HMI clients are connected.
        """
        response = dict(payload)

        if request_id is not None:
            response["req_id"] = request_id

        await websocket.send(
            json.dumps(
                {
                    "response": response,
                },
                separators=(",", ":"),
                default=str,
            )
        )

    async def send_error(
        self,
        websocket,
        *,
        error: str,
        message: str,
        request_id: Any = None,
        details: Any = None,
    ) -> None:
        """
        Send a structured error directly to the requesting HMI.
        """
        payload = {
            "type": "error",
            "ok": False,
            "error": error,
            "message": message,
        }

        if details is not None:
            payload["details"] = details

        await self.send_response(
            websocket,
            payload,
            request_id=request_id,
        )

    async def handle_service_request(self, websocket, cmd):
        request_id = cmd.get("req_id")
        request_type = cmd.get("type")
        service_types = {
            "get_service_summary",
            "get_service_modules",
            "get_diagnostic_catalog",
            "get_service_events",
            "start_diagnostic",
            "calibrate_actuator",
            "confirm_actuator_extension",
            "get_diagnostic_status",
            "abort_diagnostic",
            "confirm_clamp_state",
            "confirm_andon_color",
            "start_diagnostic_plan",
            "get_diagnostic_plan_status",
            "abort_diagnostic_plan",
        }
        if request_type not in service_types:
            return False

        try:
            if request_type == "get_service_summary":
                payload = self.service_runtime.summary(self)

            elif request_type == "get_service_modules":
                payload = {
                    "type": "service_modules",
                    "ok": True,
                    "items": self.service_runtime.module_items(self),
                }

            elif request_type == "get_diagnostic_catalog":
                payload = {
                    "type": "diagnostic_catalog",
                    "ok": True,
                    "items": self.service_runtime.diagnostic_catalog(self),
                }

            elif request_type == "get_service_events":
                result = self.job_manager.db.list_service_events(
                    limit=int(cmd.get("limit", 50)),
                    offset=int(cmd.get("offset", 0)),
                    search=str(cmd.get("search", "")),
                    level=str(cmd.get("level", "all")),
                    source=str(cmd.get("source", "all")),
                )
                payload = {"type": "service_events", "ok": True, **result}

            elif request_type == "start_diagnostic":
                if self.diagnostic_plan_runtime.active:
                    raise RuntimeError(
                        "Individual diagnostics are unavailable while the full system check is running."
                    )
                run = await self.service_runtime.start(
                    diagnostic_id=str(cmd.get("diagnostic_id", "")),
                    module_id=str(cmd.get("module_id", "")),
                    actor=None,
                    server=self,
                )
                payload = {"type": "diagnostic_started", "ok": True, "run": run}

            elif request_type == "calibrate_actuator":
                actor = self.validate_actor_initials(cmd)
                run = await self.service_runtime.start_actuator_calibration(
                    module_id=str(cmd.get("module_id", "")),
                    actor=actor,
                    server=self,
                )
                payload = {"type": "actuator_calibration_started", "ok": True, "run": run}

            elif request_type == "confirm_actuator_extension":
                payload = await self.service_runtime.confirm_actuator_extension(
                    run_id=str(cmd.get("run_id", "")),
                    module_id=str(cmd.get("module_id", "")),
                    confirmed=cmd.get("confirmed") is True,
                )

            elif request_type == "confirm_clamp_state":
                payload = await self.service_runtime.confirm_clamp_state(
                    run_id=str(cmd.get("run_id", "")),
                    module_id=str(cmd.get("module_id", "")),
                    expected_clamped=cmd.get("expected_clamped") is True,
                )
            elif request_type == "confirm_andon_color":
                payload = await self.service_runtime.confirm_andon_color(
                    run_id=str(cmd.get("run_id", "")),
                    module_id=str(cmd.get("module_id", "")),
                    color=str(cmd.get("color", "")),
                    confirmed=cmd.get("confirmed") is True,
                )

            elif request_type == "start_diagnostic_plan":
                plan = await self.diagnostic_plan_runtime.start(
                    policy=str(cmd.get("policy", "continue_independent_tests")),
                    actor=None,
                    server=self,
                )
                payload = {
                    "type": "diagnostic_plan_started",
                    "ok": True,
                    "plan": plan,
                }

            elif request_type == "get_diagnostic_plan_status":
                plan = self.diagnostic_plan_runtime.snapshot(
                    str(cmd.get("plan_id", ""))
                )
                payload = {
                    "type": "diagnostic_plan_status",
                    "ok": True,
                    "plan": plan,
                }

            elif request_type == "abort_diagnostic_plan":
                plan = await self.diagnostic_plan_runtime.abort(
                    str(cmd.get("plan_id", ""))
                )
                payload = {
                    "type": "diagnostic_plan_aborted",
                    "ok": True,
                    "plan": plan,
                }

            elif request_type == "get_diagnostic_status":
                run = self.service_runtime.status(str(cmd.get("run_id", "")))
                payload = {"type": "diagnostic_status", "ok": True, "run": run}

            elif request_type == "abort_diagnostic":
                run = await self.service_runtime.abort(
                    str(cmd.get("run_id", ""))
                )

                payload = {
                    "type": "diagnostic_aborted",
                    "ok": True,
                    "run": run,
                }

            else:
                raise ValueError(
                    "Unsupported service request."
                )

            await self.send_response(
                websocket,
                payload,
                request_id=request_id,
            )

        except (
            TypeError,
            ValueError,
            RuntimeError,
        ) as error:
            await self.send_error(
                websocket,
                error="service_request_rejected",
                message=str(error),
                request_id=request_id,
            )

        except Exception as error:
            self.logger.log.exception(
                "Service request failed."
            )

            await self.send_error(
                websocket,
                error="service_request_failed",
                message=(
                    "The service request could "
                    "not be completed."
                ),
                details=str(error),
                request_id=request_id,
            )

        return True

    async def _wait_for_correlated_queue_message(
        self,
        queue: asyncio.Queue,
        *,
        transaction_id: str,
        expected_type: str,
        timeout_s: float,
    ) -> Dict[str, Any]:
        """
        Wait for a queue message belonging to one transaction.

        Stale messages from earlier operations are discarded. The timeout
        applies to the complete wait, not separately to every stale message.

        This helper assumes operations using the queue are serialized so only
        one active caller owns the expected transaction.
        """
        deadline = (
            asyncio.get_running_loop().time()
            + timeout_s
        )

        while True:
            remaining_s = (
                deadline
                - asyncio.get_running_loop().time()
            )

            if remaining_s <= 0:
                raise asyncio.TimeoutError(
                    "Timed out waiting for "
                    f"{expected_type} for transaction "
                    f"{transaction_id}."
                )

            message = await asyncio.wait_for(
                queue.get(),
                timeout=remaining_s,
            )

            if not isinstance(
                message,
                dict,
            ):
                self.logger.log.warning(
                    "Discarding non-dictionary queue "
                    "message while waiting for "
                    f"{expected_type}: "
                    f"value={message!r}"
                )

                continue

            message_type = str(
                message.get(
                    "type",
                    "",
                )
            )

            received_transaction_id = str(
                message.get(
                    "transaction_id",
                    "",
                )
            )

            if (
                message_type
                != expected_type
            ):
                self.logger.log.warning(
                    "Discarding unexpected message from "
                    "motor-test acknowledgement queue: "
                    f"expected_type={expected_type} "
                    f"received_type={message_type} "
                    f"expected_transaction_id="
                    f"{transaction_id} "
                    f"received_transaction_id="
                    f"{received_transaction_id}"
                )

                continue

            if (
                received_transaction_id
                != transaction_id
            ):
                self.logger.log.warning(
                    "Discarding stale motor-test message: "
                    f"type={message_type} "
                    f"expected_transaction_id="
                    f"{transaction_id} "
                    f"received_transaction_id="
                    f"{received_transaction_id} "
                    f"axis={message.get('axis')}"
                )

                continue

            return message

    async def handle_drive_direction_request(
        self,
        websocket,
        cmd,
    ):
        """
        Handle motor and encoder installation-direction requests.

        Reading configuration and running a bounded motor test do not require
        actor initials.

        Saving or restoring configuration requires validated actor initials
        because those operations change persisted robot configuration.
        """
        request_type = cmd.get(
            "type"
        )

        supported_types = {
            "get_drive_direction_configuration",
            "save_drive_direction_configuration",
            "restore_drive_direction_defaults",
            "run_drive_direction_test",
        }

        if request_type not in supported_types:
            return False

        request_id = cmd.get(
            "req_id"
        )

        try:
            # ----------------------------------------------------------
            # Read current configuration
            # ----------------------------------------------------------
            if (
                request_type
                == "get_drive_direction_configuration"
            ):
                await self.send_response(
                    websocket,
                    {
                        "type":
                            "drive_direction_configuration",
                        "ok":
                            True,
                        **self.motor_direction_manager.get(),
                    },
                    request_id=request_id,
                )

                return True

            # ----------------------------------------------------------
            # Run one bounded motor installation test
            # ----------------------------------------------------------
            if (
                request_type
                == "run_drive_direction_test"
            ):
                axis = int(
                    cmd.get(
                        "axis",
                        -1,
                    )
                )

                if axis not in range(4):
                    raise ValueError(
                        "axis must be between "
                        "0 and 3."
                    )

                if self._process_is_confirmed_active():
                    raise RuntimeError(
                        "Stop the process before "
                        "testing a motor."
                    )

                async with self._drive_direction_test_lock:
                    transaction_id = (
                        f"direction-test-{uuid4()}"
                    )

                    await self.mcu_writes.put({
                        "action":
                            "start_drive_direction_test",
                        "transaction_id":
                            transaction_id,
                        "axis":
                            axis,
                        "lease_ms":
                            2000,

                        # This is a transient motion command.
                        #
                        # It must never be replayed after a
                        # serial failure or reconnection.
                        "_transient_motion":
                            True,

                        "_expires_monotonic": (
                            asyncio
                            .get_running_loop()
                            .time()
                            + 0.6
                        ),
                    })

                    ack = (
                        await self
                        ._wait_for_correlated_queue_message(
                            self.drive_direction_test_acks,
                            transaction_id=transaction_id,
                            expected_type=(
                                "drive_direction_test_ack"
                            ),
                            timeout_s=2.0,
                        )
                    )

                    if ack.get("ok") is not True:
                        raise RuntimeError(
                            str(
                                ack.get("error")
                                or (
                                    "The motor direction "
                                    "test was rejected."
                                )
                            )
                        )

                    result = (
                        await self
                        ._wait_for_correlated_queue_message(
                            self.drive_direction_test_results,
                            transaction_id=transaction_id,
                            expected_type=(
                                "drive_direction_test_result"
                            ),
                            timeout_s=5.0,
                        )
                    )

                    if (
                        result.get("axis")
                        is not None
                        and int(result["axis"])
                        != axis
                    ):
                        raise RuntimeError(
                            "The motor-test result was "
                            "returned for the wrong axis."
                        )

                    await self.send_response(
                        websocket,
                        {
                            "type":
                                "drive_direction_test_result",
                            **result,
                        },
                        request_id=request_id,
                    )

                return True

            # ----------------------------------------------------------
            # Persist combined motor and encoder direction settings
            # ----------------------------------------------------------
            if (
                request_type
                == "save_drive_direction_configuration"
            ):
                await self.save_drive_direction_configuration_request(
                    websocket,
                    cmd,
                )
                return True

            # ----------------------------------------------------------
            # Restore combined factory direction settings
            # ----------------------------------------------------------
            if (
                request_type
                == "restore_drive_direction_defaults"
            ):
                await self.restore_drive_direction_defaults_request(
                    websocket,
                    cmd,
                )
                return True

            raise ValueError(
                "Unsupported drive direction request."
            )

        except asyncio.TimeoutError as error:
            self.logger.log.error(
                "Drive direction request timed out: "
                f"type={request_type} "
                f"req_id={request_id} "
                f"error={error}"
            )

            await self.send_error(
                websocket,
                error=(
                    "drive_direction_request_timeout"
                ),
                message=(
                    "The robot controller did not "
                    "complete the motor installation "
                    "request before the timeout."
                ),
                request_id=request_id,
                details=str(error),
            )

        except (
            TypeError,
            ValueError,
            RuntimeError,
        ) as error:
            self.logger.log.warning(
                "Drive direction request rejected: "
                f"type={request_type} "
                f"req_id={request_id} "
                f"error={error}"
            )

            await self.send_error(
                websocket,
                error=(
                    "drive_direction_request_rejected"
                ),
                message=str(error),
                request_id=request_id,
            )

        except Exception as error:
            self.logger.log.exception(
                "Drive direction request failed."
            )

            await self.send_error(
                websocket,
                error=(
                    "drive_direction_request_failed"
                ),
                message=(
                    "The motor installation "
                    "configuration request could "
                    "not be completed."
                ),
                request_id=request_id,
                details=str(error),
            )

        return True

    async def handle_actuator_extension_request(
        self,
        websocket,
        cmd: Dict[str, Any],
    ) -> bool:
        from actuator_extension_depth import (
            validate_extension_voltage,
        )

        request_type = cmd.get("type")

        supported_types = {
            "get_actuator_extension_depth",
            "save_actuator_extension_depth",
            "restore_actuator_extension_depth",
            "preview_actuator_voltage",
            "stop_actuator_preview",
        }

        if request_type not in supported_types:
            return False

        request_id = cmd.get("req_id")

        try:
            if (
                request_type
                == "get_actuator_extension_depth"
            ):
                robot_id = str(
                    cmd.get(
                        "robot_id",
                        "",
                    )
                ).strip()

                blade_id = str(
                    cmd.get(
                        "blade_id",
                        "",
                    )
                ).strip()

                if not robot_id:
                    raise ValueError(
                        "robot_id is required."
                    )

                if not blade_id:
                    raise ValueError(
                        "blade_id is required."
                    )

                profile = (
                    self.job_manager.db
                    .get_transition_profile(
                        robot_id,
                        blade_id,
                    )
                )

                await self.send_response(
                    websocket,
                    {
                        "type":
                            "actuator_extension_depth",
                        "ok": True,
                        "robot_id":
                            robot_id,
                        "blade_id":
                            blade_id,
                        "default_voltage":
                            profile[
                                "default_actuator_voltage"
                            ],
                        "override_voltage":
                            profile[
                                "override_actuator_voltage"
                            ],
                        "effective_voltage":
                            profile[
                                "effective_actuator_voltage"
                            ],
                        "has_override":
                            profile[
                                "actuator_voltage_has_override"
                            ],
                        "override_revision":
                            profile[
                                "actuator_voltage_override_revision"
                            ],
                        "override_updated_by":
                            profile[
                                "actuator_voltage_override_updated_by"
                            ],
                        "override_updated_at":
                            profile[
                                "actuator_voltage_override_updated_at"
                            ],
                    },
                    request_id=request_id,
                )

                return True

            if request_type in {
                "save_actuator_extension_depth",
                "restore_actuator_extension_depth",
            }:
                self._require_safe_manual_actuator_state(
                    require_fresh_status=False
                )

                actor = self.validate_actor_initials(
                    cmd
                )

                robot_id = str(
                    cmd.get(
                        "robot_id",
                        "",
                    )
                ).strip()

                blade_id = str(
                    cmd.get(
                        "blade_id",
                        "",
                    )
                ).strip()

                if not robot_id:
                    raise ValueError(
                        "robot_id is required."
                    )

                if not blade_id:
                    raise ValueError(
                        "blade_id is required."
                    )

                if (
                    request_type
                    == "save_actuator_extension_depth"
                ):
                    voltage = validate_extension_voltage(
                        cmd.get("voltage")
                    )

                    persisted_profile = (
                        self.job_manager.db
                        .set_actuator_extension_override(
                            robot_type_id=robot_id,
                            blade_type_id=blade_id,
                            voltage=voltage,
                            actor=actor,
                        )
                    )

                    response_type = (
                        "actuator_extension_depth_saved"
                    )

                else:
                    persisted_profile = (
                        self.job_manager.db
                        .clear_actuator_extension_override(
                            robot_type_id=robot_id,
                            blade_type_id=blade_id,
                            actor=actor,
                        )
                    )

                    response_type = (
                        "actuator_extension_depth_restored"
                    )

                try:
                    applied_profile = (
                        await self.config_manager.apply(
                            robot_id,
                            blade_id,
                        )
                    )

                except asyncio.CancelledError:
                    raise

                except Exception as apply_error:
                    self.configuration_loaded_to_mcu = (
                        False
                    )

                    self.configuration_apply_error = (
                        "The actuator extension setting "
                        "was persisted, but the complete "
                        "runtime profile was not confirmed "
                        "by H7: "
                        f"{apply_error}"
                    )

                    self.startup_complete.clear()

                    self.startup_error = (
                        self.configuration_apply_error
                    )

                    self.logger.log.exception(
                        "Actuator extension setting was "
                        "persisted, but complete runtime "
                        "configuration application failed."
                    )

                    await self.send_error(
                        websocket,
                        error=(
                            "actuator_extension_runtime_apply_failed"
                        ),
                        message=(
                            "The actuator extension setting "
                            "was saved, but the robot "
                            "controller did not confirm the "
                            "complete runtime configuration. "
                            "Process start remains blocked "
                            "until the profile is reapplied."
                        ),
                        request_id=request_id,
                        details={
                            "persistence_succeeded":
                                True,
                            "runtime_apply_succeeded":
                                False,
                            "robot_id":
                                robot_id,
                            "blade_id":
                                blade_id,
                            "effective_voltage":
                                persisted_profile[
                                    "effective_actuator_voltage"
                                ],
                            "error":
                                str(apply_error),
                        },
                    )

                    return True

                self.configuration_loaded_to_mcu = (
                    True
                )

                self.configuration_apply_error = (
                    None
                )

                if (
                    self._feedforward_runtime_loaded
                    and self._motor_direction_runtime_loaded
                    and self.encoder_startup_ready
                ):
                    self.startup_error = None
                    self.startup_complete.set()

                await self.send_response(
                    websocket,
                    {
                        "type":
                            response_type,
                        "ok": True,
                        "profile":
                            applied_profile,
                        "actuator_extension_transaction_id":
                            applied_profile.get(
                                "actuator_extension_transaction_id"
                            ),
                        "applied_voltage":
                            applied_profile.get(
                                "applied_actuator_voltage"
                            ),
                    },
                    request_id=request_id,
                )

                return True

            if (
                request_type
                == "preview_actuator_voltage"
            ):
                self._require_safe_manual_actuator_state(
                    require_fresh_status=True
                )

                session_id = str(
                    cmd.get(
                        "preview_session_id",
                        "",
                    )
                ).strip()

                if (
                    not session_id
                    or len(session_id) > 48
                ):
                    raise ValueError(
                        "A valid preview_session_id "
                        "is required."
                    )

                channel = int(
                    cmd.get(
                        "channel",
                        0,
                    )
                )

                if channel != 0:
                    raise ValueError(
                        "Actuator preview is restricted "
                        "to channel 0."
                    )

                voltage = validate_extension_voltage(
                    cmd.get("voltage")
                )

                lease_ms = int(
                    cmd.get(
                        "lease_ms",
                        250,
                    )
                )

                if not 100 <= lease_ms <= 1000:
                    raise ValueError(
                        "lease_ms must be between "
                        "100 and 1000."
                    )

                sequence = int(
                    cmd.get(
                        "seq",
                        0,
                    )
                )

                if sequence < 0:
                    raise ValueError(
                        "seq cannot be negative."
                    )

                loop = asyncio.get_running_loop()

                await self.mcu_writes.put({
                    "action":
                        "preview_actuator_voltage",
                    "preview_session_id":
                        session_id,
                    "channel":
                        0,
                    "voltage":
                        voltage,
                    "lease_ms":
                        lease_ms,
                    "seq":
                        sequence,
                    "_transient_motion":
                        True,
                    "_expires_monotonic": (
                        loop.time()
                        + lease_ms / 1000.0
                    ),
                })

                await self.send_response(
                    websocket,
                    {
                        "type":
                            "actuator_voltage_preview_accepted",
                        "ok": True,
                        "preview_session_id":
                            session_id,
                        "channel":
                            0,
                        "voltage":
                            voltage,
                        "lease_ms":
                            lease_ms,
                        "seq":
                            sequence,
                    },
                    request_id=request_id,
                )

                return True

            if (
                request_type
                == "stop_actuator_preview"
            ):
                session_id = str(
                    cmd.get(
                        "preview_session_id",
                        "",
                    )
                ).strip()

                if (
                    not session_id
                    or len(session_id) > 48
                ):
                    raise ValueError(
                        "A valid preview_session_id "
                        "is required."
                    )

                sequence = int(
                    cmd.get(
                        "seq",
                        0,
                    )
                )

                if sequence < 0:
                    raise ValueError(
                        "seq cannot be negative."
                    )

                command_queued = False

                if self.h7_runtime_ready.is_set():
                    await self.mcu_writes.put({
                        "action":
                            "stop_actuator_preview",
                        "preview_session_id":
                            session_id,
                        "channel":
                            0,
                        "seq":
                            sequence,
                        "_transient_motion":
                            True,
                        "_expires_monotonic": (
                            asyncio
                            .get_running_loop()
                            .time()
                            + 0.5
                        ),
                    })

                    command_queued = True

                await self.send_response(
                    websocket,
                    {
                        "type":
                            "actuator_voltage_preview_stopped",
                        "ok": True,
                        "preview_session_id":
                            session_id,
                        "channel":
                            0,
                        "seq":
                            sequence,
                        "command_queued":
                            command_queued,
                    },
                    request_id=request_id,
                )

                return True

            raise ValueError(
                "Unsupported actuator extension request."
            )

        except asyncio.CancelledError:
            raise

        except asyncio.TimeoutError as error:
            await self.send_error(
                websocket,
                error=(
                    "actuator_extension_request_timeout"
                ),
                message=(
                    "The robot controller did not "
                    "confirm the actuator extension "
                    "request before the timeout."
                ),
                request_id=request_id,
                details=str(error),
            )

        except (
            TypeError,
            ValueError,
            RuntimeError,
        ) as error:
            await self.send_error(
                websocket,
                error=(
                    "actuator_extension_request_rejected"
                ),
                message=str(error),
                request_id=request_id,
            )

        except Exception as error:
            self.logger.log.exception(
                "Actuator extension request failed."
            )

            await self.send_error(
                websocket,
                error=(
                    "actuator_extension_request_failed"
                ),
                message=(
                    "The actuator extension request "
                    "could not be completed."
                ),
                request_id=request_id,
                details=str(error),
            )

        return True

    async def handle_feedforward_request(
        self,
        websocket,
        cmd: Dict[str, Any],
    ) -> bool:
        request_type = cmd.get("type")

        supported_types = {
            "get_feedforward_configuration",
            "set_feedforward_configuration",
            "restore_feedforward_defaults",
        }

        if request_type not in supported_types:
            return False

        request_id = cmd.get("req_id")

        try:
            if request_type == "get_feedforward_configuration":
                result = self.feedforward_manager.get()

                await self.send_response(
                    websocket,
                    {
                        "type": "feedforward_configuration",
                        "ok": True,
                        **result,
                    },
                    request_id=request_id,
                )

                return True

            actor = self.validate_actor_initials(
                cmd
            )

            if request_type == "set_feedforward_configuration":
                raw_values = cmd.get(
                    "values"
                )

                if not isinstance(
                    raw_values,
                    dict,
                ):
                    raise ValueError(
                        "values must be a JSON object."
                    )

                result = (
                    await self.feedforward_manager.save(
                        raw_values,
                        actor=actor,
                    )
                )

                response_type = (
                    "feedforward_configuration_saved"
                )

            else:
                result = (
                    await self.feedforward_manager
                    .restore_defaults(
                        actor=actor,
                    )
                )

                response_type = (
                    "feedforward_defaults_restored"
                )

            self._feedforward_runtime_loaded = True
            self._feedforward_apply_error = None

            await self.send_response(
                websocket,
                {
                    "type": response_type,
                    "ok": True,
                    "values": result.values,
                    "transaction_id":
                        result.transaction_id,
                    "restored_defaults":
                        result.restored_defaults,
                },
                request_id=request_id,
            )

        except asyncio.TimeoutError as error:
            self._feedforward_runtime_loaded = False

            self._feedforward_apply_error = (
                "The H7 did not confirm the "
                "feedforward configuration: "
                f"{error}"
            )

            self.startup_complete.clear()

            self.logger.log.error(
                "Feedforward configuration result "
                "is unknown because the H7 "
                "acknowledgement timed out: "
                f"{error}"
            )

            await self.send_error(
                websocket,
                error=(
                    "feedforward_configuration_timeout"
                ),
                message=(
                    "The robot controller did not "
                    "confirm the feedforward "
                    "configuration. Process start is "
                    "blocked until the stored "
                    "configuration is reapplied."
                ),
                request_id=request_id,
                details=str(error),
            )

        except (
            TypeError,
            ValueError,
            RuntimeError,
        ) as error:
            self.logger.log.warning(
                "Feedforward configuration request "
                f"was rejected: {error}"
            )

            await self.send_error(
                websocket,
                error=(
                    "feedforward_configuration_rejected"
                ),
                message=str(error),
                request_id=request_id,
            )

        except Exception as error:
            self._feedforward_runtime_loaded = False

            self._feedforward_apply_error = str(
                error
            )

            self.startup_complete.clear()

            self.logger.log.exception(
                "Feedforward configuration request "
                "failed."
            )

            await self.send_error(
                websocket,
                error=(
                    "feedforward_configuration_failed"
                ),
                message=(
                    "The feedforward configuration "
                    "could not be completed."
                ),
                request_id=request_id,
                details=str(error),
            )

        return True

# receive the messages / commands from the tablet
    async def consumer(self, websocket):
        """
        Receives commands from the HMI.
        """
        try:
            async for message in websocket:
                await self.consumer_handler(websocket, message)

        except websockets.exceptions.ConnectionClosedOK:
            self.logger.log.info(
                "HMI WebSocket consumer closed normally."
            )

        except websockets.exceptions.ConnectionClosedError as error:
            self.logger.log.warning(
                f"HMI WebSocket consumer lost connection: {error}"
            )

        except websockets.exceptions.ConnectionClosed:
            self.logger.log.info(
                "HMI WebSocket consumer connection closed."
            )

        except Exception:
            self.logger.log.exception(
                "Unexpected WebSocket consumer error."
            )


    async def consumer_handler(self, websocket, packet):
        """
        Handle one command received from an HMI WebSocket connection.

        Request-specific responses that include a req_id are sent directly
        to the requesting WebSocket. Asynchronous MCU events and broadcasts
        continue to use the shared responses queue.
        """
        try:
            cmd = json.loads(packet)
        except (TypeError, json.JSONDecodeError) as error:
            self.logger.log.warning(
                "Received invalid JSON from HMI: %s",
                error,
            )

            await self.send_error(
                websocket,
                error="invalid_json",
                message="The request is not valid JSON.",
                details=str(error),
            )
            return

        if not isinstance(cmd, dict):
            self.logger.log.warning(
                "Received invalid HMI request type: %s",
                type(cmd).__name__,
            )

            await self.send_error(
                websocket,
                error="invalid_request",
                message="The request must be a JSON object.",
            )
            return

        # cmd has now been parsed and confirmed to be a dictionary.
        if cmd.get("type") != "ping":
            self.logger.log.info(
                "HMI request: "
                f"type={cmd.get('type')} "
                f"action={cmd.get('action')} "
                f"req_id={cmd.get('req_id')} "
                f"actor_initials={cmd.get('actor_initials')}"
            )
        else:
            self.logger.log.debug("HMI ping")

        t = cmd.get("type")
        action = cmd.get("action")

        if await self.handle_service_request(
            websocket,
            cmd,
        ):
            return

        if await self.handle_feedforward_request(
            websocket,
            cmd,
        ):
            return

        if await self.handle_actuator_extension_request(
            websocket,
            cmd,
        ):
            return

        if await self.handle_drive_direction_request(
            websocket,
            cmd,
        ):
            return

        # ==========================================================
        # Request-specific robot configuration APIs
        # ==========================================================

        if t == "get_process_start_readiness":
            await self.send_response(
                websocket,
                {
                    "type": "process_start_readiness",
                    **self.process_start_readiness(),
                },
                request_id=cmd.get("req_id"),
            )
            return

        if t == "get_robot_configuration":
            request_id = cmd.get("req_id")

            try:
                catalog = self.config_manager.catalog()

                await self.send_response(
                    websocket,
                    {
                        "type": "robot_configuration",
                        "ok": True,
                        **catalog,
                    },
                    request_id=request_id,
                )

            except Exception as error:
                self.logger.log.exception(
                    "Failed to load robot configuration."
                )

                await self.send_error(
                    websocket,
                    error="configuration_read_failed",
                    message="Configuration could not be loaded.",
                    request_id=request_id,
                    details=str(error),
                )

            return

        if t == "delete_blade_type":
            request_id = cmd.get("req_id")

            try:
                actor = (
                    self.validate_actor_initials(
                        cmd
                    )
                )

                self.config_manager.delete_blade_type(
                    str(cmd["blade_id"]),
                    actor,
                )

                await self.send_response(
                    websocket,
                    {
                        "type":
                            "blade_type_deleted",
                        "ok": True,
                        "blade_id":
                            str(
                                cmd["blade_id"]
                            ),
                    },
                    request_id=request_id,
                )

            except Exception as error:
                self.logger.log.exception(
                    "Failed to delete blade type."
                )

                await self.send_error(
                    websocket,
                    error=(
                        "delete_blade_type_failed"
                    ),
                    message=str(error),
                    request_id=request_id,
                )

            return

        if t == "apply_transition_profile":
            request_id = cmd.get("req_id")

            try:
                robot_id = str(
                    cmd.get("robot_id", "")
                ).strip()

                blade_id = str(
                    cmd.get("blade_id", "")
                ).strip()

                result = await self.config_manager.apply(
                    robot_id,
                    blade_id,
                )

                self.configuration_loaded_to_mcu = True
                self.configuration_apply_error = None

                await self.send_response(
                    websocket,
                    {
                        "type": "transition_profile_applied",
                        "ok": True,
                        "profile": result,
                    },
                    request_id=request_id,
                )

            except Exception as error:
                self.configuration_loaded_to_mcu = False
                self.configuration_apply_error = str(error)
                self.logger.log.exception(
                    "Failed to apply transition profile."
                )

                await self.send_error(
                    websocket,
                    error="apply_transition_profile_failed",
                    message=str(error),
                    request_id=request_id,
                )

            return

        if t == "save_transition_override":
            request_id = cmd.get("req_id")

            try:
                robot_id = str(
                    cmd.get("robot_id", "")
                ).strip()

                blade_id = str(
                    cmd.get("blade_id", "")
                ).strip()

                values = list(
                    cmd.get("values") or []
                )

                # Replace this with the authenticated username once
                # CM5-side authentication exists.
                actor = (
                    self.validate_actor_initials(
                        cmd
                    )
                )

                result = (
                    await self.config_manager
                    .save_override_and_apply(
                        robot_id,
                        blade_id,
                        values,
                        actor,
                    )
                )

                self.configuration_loaded_to_mcu = True
                self.configuration_apply_error = None

                await self.send_response(
                    websocket,
                    {
                        "type": "transition_override_saved",
                        "ok": True,
                        "profile": result,
                    },
                    request_id=request_id,
                )

            except Exception as error:
                self.configuration_loaded_to_mcu = False
                self.configuration_apply_error = str(error)
                self.logger.log.exception(
                    "Failed to save transition override."
                )

                await self.send_error(
                    websocket,
                    error="save_transition_override_failed",
                    message=str(error),
                    request_id=request_id,
                )

            return

        if t == "create_blade_type":
            request_id = cmd.get("req_id")

            try:
                actor = (
                    self.validate_actor_initials(
                        cmd
                    )
                )

                blade = (
                    self.config_manager
                    .create_blade_type(
                        blade_id=str(
                            cmd["blade_id"]
                        ),
                        name=str(
                            cmd["name"]
                        ),
                        max_transitions=int(
                            cmd[
                                "max_transitions"
                            ]
                        ),
                        copy_from_blade_id=str(
                            cmd.get(
                                "copy_from_blade_id",
                                "",
                            )
                        ),
                        actor=actor,
                    )
                )

                await self.send_response(
                    websocket,
                    {
                        "type":
                            "blade_type_created",
                        "ok": True,
                        "blade": blade,
                    },
                    request_id=request_id,
                )

            except Exception as error:
                self.logger.log.exception(
                    "Failed to create blade type."
                )

                await self.send_error(
                    websocket,
                    error=(
                        "create_blade_type_failed"
                    ),
                    message=str(error),
                    request_id=request_id,
                )

            return

        if t == "get_motor_direction":
            request_id = cmd.get(
                "req_id"
            )

            try:
                result = (
                    self.motor_direction_manager
                    .get()
                )

                await self.send_response(
                    websocket,
                    {
                        "type":
                            "motor_direction_configuration",
                        "ok": True,
                        **result,
                    },
                    request_id=request_id,
                )

            except Exception as error:
                self.logger.log.exception(
                    "Failed to retrieve motor "
                    "direction configuration."
                )

                await self.send_error(
                    websocket,
                    error=(
                        "get_motor_direction_failed"
                    ),
                    message=str(error),
                    request_id=request_id,
                )

            return

        if t == "set_motor_direction":
            request_id = cmd.get(
                "req_id"
            )

            try:
                actor = (
                    self.validate_actor_initials(
                        cmd
                    )
                )

                directions = cmd.get(
                    "directions"
                )

                if not isinstance(
                    directions,
                    dict,
                ):
                    raise ValueError(
                        "directions must be a "
                        "JSON object."
                    )

                result = (
                    await self
                    .motor_direction_manager
                    .save(
                        directions,
                        actor=actor,
                    )
                )

                self._motor_direction_runtime_loaded = (
                    True
                )

                self._motor_direction_apply_error = (
                    None
                )

                await self.send_response(
                    websocket,
                    {
                        "type":
                            "motor_direction_saved",
                        "ok": True,
                        "directions":
                            result.directions,
                        "transaction_id":
                            result.transaction_id,
                        "restored_defaults":
                            False,
                        "is_factory_default": (
                            result.directions
                            == FACTORY_MOTOR_DIRECTIONS
                        ),
                    },
                    request_id=request_id,
                )

            except asyncio.TimeoutError as error:
                self._motor_direction_runtime_loaded = (
                    False
                )

                self._motor_direction_apply_error = (
                    str(error)
                )

                self.startup_complete.clear()

                await self.send_error(
                    websocket,
                    error=(
                        "motor_direction_timeout"
                    ),
                    message=(
                        "The robot controller did "
                        "not confirm the motor "
                        "directions."
                    ),
                    request_id=request_id,
                    details=str(error),
                )

            except (
                TypeError,
                ValueError,
                RuntimeError,
            ) as error:
                await self.send_error(
                    websocket,
                    error=(
                        "motor_direction_rejected"
                    ),
                    message=str(error),
                    request_id=request_id,
                )

            except Exception as error:
                self.logger.log.exception(
                    "Motor direction save failed."
                )

                await self.send_error(
                    websocket,
                    error=(
                        "motor_direction_save_failed"
                    ),
                    message=str(error),
                    request_id=request_id,
                )

            return

        if t == "restore_motor_direction_defaults":
            request_id = cmd.get(
                "req_id"
            )

            try:
                actor = (
                    self.validate_actor_initials(
                        cmd
                    )
                )

                result = (
                    await self
                    .motor_direction_manager
                    .restore_defaults(
                        actor=actor,
                    )
                )

                self._motor_direction_runtime_loaded = (
                    True
                )

                self._motor_direction_apply_error = (
                    None
                )

                await self.send_response(
                    websocket,
                    {
                        "type":
                            "motor_direction_defaults_restored",
                        "ok": True,
                        "directions":
                            result.directions,
                        "transaction_id":
                            result.transaction_id,
                        "restored_defaults":
                            True,
                        "is_factory_default":
                            True,
                    },
                    request_id=request_id,
                )

            except asyncio.TimeoutError as error:
                self._motor_direction_runtime_loaded = (
                    False
                )

                self._motor_direction_apply_error = (
                    str(error)
                )

                self.startup_complete.clear()

                await self.send_error(
                    websocket,
                    error=(
                        "motor_direction_restore_timeout"
                    ),
                    message=(
                        "The robot controller did not "
                        "confirm the factory motor "
                        "directions."
                    ),
                    request_id=request_id,
                    details=str(error),
                )

            except (
                TypeError,
                ValueError,
                RuntimeError,
            ) as error:
                await self.send_error(
                    websocket,
                    error=(
                        "motor_direction_restore_rejected"
                    ),
                    message=str(error),
                    request_id=request_id,
                )

            except Exception as error:
                self.logger.log.exception(
                    "Motor direction factory "
                    "restore failed."
                )

                await self.send_error(
                    websocket,
                    error=(
                        "motor_direction_restore_failed"
                    ),
                    message=str(error),
                    request_id=request_id,
                )

            return

        if t == "update_blade_type":
            request_id = cmd.get("req_id")

            try:
                actor = (
                    self.validate_actor_initials(
                        cmd
                    )
                )

                blade = (
                    self.config_manager
                    .update_blade_type(
                        blade_id=str(
                            cmd["blade_id"]
                        ),
                        name=str(
                            cmd["name"]
                        ),
                        max_transitions=int(
                            cmd[
                                "max_transitions"
                            ]
                        ),
                        actor=actor,
                    )
                )

                await self.send_response(
                    websocket,
                    {
                        "type":
                            "blade_type_updated",
                        "ok": True,
                        "blade": blade,
                    },
                    request_id=request_id,
                )

            except Exception as error:
                self.logger.log.exception(
                    "Failed to update blade type."
                )

                await self.send_error(
                    websocket,
                    error=(
                        "update_blade_type_failed"
                    ),
                    message=str(error),
                    request_id=request_id,
                )

            return

        if t == "archive_blade_type":
            request_id = cmd.get("req_id")

            try:
                actor = (
                    self.validate_actor_initials(
                        cmd
                    )
                )

                self.config_manager.archive_blade_type(
                    str(cmd["blade_id"]),
                    actor,
                )

                await self.send_response(
                    websocket,
                    {
                        "type":
                            "blade_type_archived",
                        "ok": True,
                    },
                    request_id=request_id,
                )

            except Exception as error:
                self.logger.log.exception(
                    "Failed to archive blade type."
                )

                await self.send_error(
                    websocket,
                    error=(
                        "archive_blade_type_failed"
                    ),
                    message=str(error),
                    request_id=request_id,
                )

            return

        if t == "restore_blade_type":
            request_id = cmd.get("req_id")

            try:
                actor = (
                    self.validate_actor_initials(
                        cmd
                    )
                )

                self.config_manager.restore_blade_type(
                    str(cmd["blade_id"]),
                    actor,
                )

                await self.send_response(
                    websocket,
                    {
                        "type":
                            "blade_type_restored",
                        "ok": True,
                    },
                    request_id=request_id,
                )

            except Exception as error:
                self.logger.log.exception(
                    "Failed to restore blade type."
                )

                await self.send_error(
                    websocket,
                    error=(
                        "restore_blade_type_failed"
                    ),
                    message=str(error),
                    request_id=request_id,
                )

            return

        if t == "get_job_metrics":
            request_id = cmd.get(
                "req_id"
            )

            try:
                if self.job_manager is None:
                    raise RuntimeError(
                        "Job manager is unavailable."
                    )

                metrics = (
                    self.job_manager
                    .db
                    .get_job_history_metrics()
                )

                await self.send_response(
                    websocket,
                    {
                        "type": "job_metrics",
                        "ok": True,
                        **metrics,
                    },
                    request_id=request_id,
                )

                self.logger.log.info(
                    "Job metrics sent to HMI: "
                    f"req_id={request_id}"
                )

            except Exception as error:
                self.logger.log.exception(
                    "Failed to retrieve job metrics."
                )

                await self.send_error(
                    websocket,
                    error=(
                        "get_job_metrics_failed"
                    ),
                    message=(
                        "Job metrics could not "
                        "be retrieved."
                    ),
                    request_id=request_id,
                    details=str(error),
                )

            return

        if t == "get_job_details":
            request_id = cmd.get(
                "req_id"
            )

            try:
                if self.job_manager is None:
                    raise RuntimeError(
                        "Job manager is unavailable."
                    )

                job_uuid = str(
                    cmd.get(
                        "job_uuid",
                        "",
                    )
                ).strip()

                if not job_uuid:
                    raise ValueError(
                        "job_uuid is required."
                    )

                job = (
                    self.job_manager
                    .db
                    .get_job_by_uuid(
                        job_uuid
                    )
                )

                if job is None:
                    raise ValueError(
                        "Job not found."
                    )

                events = (
                    self.job_manager
                    .db
                    .get_job_events(
                        job_uuid
                    )
                )

                await self.send_response(
                    websocket,
                    {
                        "type": "job_details",
                        "ok": True,
                        "job": job,
                        "events": events,
                    },
                    request_id=request_id,
                )

                self.logger.log.info(
                    "Job details sent to HMI: "
                    f"job_uuid={job_uuid} "
                    f"events={len(events)} "
                    f"req_id={request_id}"
                )

            except ValueError as error:
                self.logger.log.warning(
                    "Job details request rejected: "
                    f"{error}"
                )

                await self.send_error(
                    websocket,
                    error=(
                        "get_job_details_rejected"
                    ),
                    message=str(error),
                    request_id=request_id,
                )

            except Exception as error:
                self.logger.log.exception(
                    "Failed to retrieve job details."
                )

                await self.send_error(
                    websocket,
                    error=(
                        "get_job_details_failed"
                    ),
                    message=(
                        "Job details could not "
                        "be retrieved."
                    ),
                    request_id=request_id,
                    details=str(error),
                )

            return

        if t == "revert_transition_override":
            request_id = cmd.get("req_id")

            try:
                robot_id = str(
                    cmd.get("robot_id", "")
                ).strip()

                blade_id = str(
                    cmd.get("blade_id", "")
                ).strip()

                actor = (
                    self.validate_actor_initials(
                        cmd
                    )
                )

                result = (
                    await self.config_manager
                    .revert_and_apply(
                        robot_id,
                        blade_id,
                        actor,
                    )
                )

                self.configuration_loaded_to_mcu = True
                self.configuration_apply_error = None

                await self.send_response(
                    websocket,
                    {
                        "type": "transition_override_reverted",
                        "ok": True,
                        "profile": result,
                    },
                    request_id=request_id,
                )

            except Exception as error:
                self.configuration_loaded_to_mcu = False
                self.configuration_apply_error = str(error)
                self.logger.log.exception(
                    "Failed to revert transition override."
                )

                await self.send_error(
                    websocket,
                    error="revert_transition_override_failed",
                    message=str(error),
                    request_id=request_id,
                )

            return

        # ==========================================================
        # HMI WebSocket heartbeat
        # ==========================================================

        if t == "ping":
            await websocket.send(
                json.dumps({
                    "response": {
                        "type": "pong",
                        "ts": cmd.get("ts"),
                    }
                })
            )
            return

        # ==========================================================
        # Encoder commands
        # ==========================================================

        if t == "reset_encoder":
            request_id = cmd.get("req_id")
            try:
                result = await self.encoder_persistence.operator_reset()
                await self.send_response(
                    websocket,
                    {**result, "type": "encoder_reset"},
                    request_id=request_id,
                )
            except Exception as error:
                await self.send_error(
                    websocket,
                    error="encoder_reset_failed",
                    message=str(error),
                    request_id=request_id,
                )
            return

        if t == "set_encoder":
            request_id = cmd.get("req_id")
            try:
                radius_m = float(cmd["radius_m"])
                if not math.isfinite(radius_m):
                    raise ValueError("radius_m must be finite.")
                result = await self.encoder_persistence.operator_set(
                    radius_m * 1000.0
                )
                await self.send_response(
                    websocket,
                    {**result, "type": "encoder_set"},
                    request_id=request_id,
                )
            except Exception as error:
                await self.send_error(
                    websocket,
                    error="encoder_set_failed",
                    message=str(error),
                    request_id=request_id,
                )
            return

        # ==========================================================
        # Direct MCU passthrough commands from HMI
        # ==========================================================

        if action == "set_voltage":
            request_id = cmd.get("req_id")

            try:
                channel = int(
                    cmd.get("channel")
                )

                voltage = float(
                    cmd.get("voltage")
                )

                if channel not in range(4):
                    raise ValueError(
                        "Actuator channel must be "
                        "between 0 and 3."
                    )

                if (
                    not math.isfinite(voltage)
                    or voltage < 0.0
                    or voltage > 5.0
                ):
                    raise ValueError(
                        "Actuator voltage must be "
                        "between 0.0 and 5.0 V."
                    )

                self._require_safe_manual_actuator_state(
                    require_fresh_status=True
                )

                await self.mcu_writes.put({
                    "action":
                        "set_voltage",
                    "channel":
                        channel,
                    "voltage":
                        voltage,
                    "_transient_motion":
                        True,
                    "_expires_monotonic": (
                        asyncio
                        .get_running_loop()
                        .time()
                        + 0.5
                    ),
                })

                if request_id is not None:
                    await self.send_response(
                        websocket,
                        {
                            "type":
                                "actuator_voltage_accepted",
                            "ok": True,
                            "channel":
                                channel,
                            "voltage":
                                voltage,
                        },
                        request_id=request_id,
                    )

            except (
                TypeError,
                ValueError,
                RuntimeError,
            ) as error:
                if request_id is not None:
                    await self.send_error(
                        websocket,
                        error=(
                            "manual_actuator_command_rejected"
                        ),
                        message=str(error),
                        request_id=request_id,
                    )
                else:
                    self.logger.log.warning(
                        "Manual actuator command rejected: "
                        f"{error}"
                    )

            except Exception as error:
                self.logger.log.exception(
                    "Manual actuator command failed."
                )

                if request_id is not None:
                    await self.send_error(
                        websocket,
                        error=(
                            "manual_actuator_command_failed"
                        ),
                        message=(
                            "The manual actuator command "
                            "could not be completed."
                        ),
                        request_id=request_id,
                        details=str(error),
                    )

            return

        if action == "jog":
            direction = cmd.get("dir")
            request_id = cmd.get("req_id")
            session_id = str(cmd.get("jog_session_id", "")).strip()

            if not session_id or len(session_id) > 48:
                await self.send_error(
                    websocket,
                    error="invalid_jog_session",
                    message="A valid jog_session_id is required.",
                    request_id=request_id,
                )
                return

            if direction not in ("forward", "backward"):
                await self.send_error(
                    websocket,
                    error="invalid_direction",
                    message=f"Unsupported jog direction: {direction}",
                    request_id=request_id,
                )
                return

            if not self.h7_runtime_ready.is_set():
                await self.send_error(
                    websocket,
                    error="h7_runtime_not_ready",
                    message="Jogging is unavailable until the H7 runtime is ready.",
                    request_id=request_id,
                )
                return

            try:
                speed = float(cmd.get("speed", 0.02))
            except (TypeError, ValueError):
                speed = 0.02

            try:
                lease_ms = int(cmd.get("lease_ms", 250))
            except (TypeError, ValueError):
                lease_ms = 250

            try:
                seq = int(cmd.get("seq", 0))
            except (TypeError, ValueError):
                seq = 0

            speed = max(0.0, min(speed, 0.02))
            lease_ms = max(1, min(lease_ms, 500))

            loop = asyncio.get_running_loop()
            await self.mcu_writes.put({
                "action": "jog",
                "jog_session_id": session_id,
                "dir": direction,
                "speed": speed,
                "lease_ms": lease_ms,
                "seq": seq,
                # Private CM5 metadata. SerialServer removes keys beginning with `_`
                # before JSON encoding and drops this command if it has expired.
                "_expires_monotonic": loop.time() + (lease_ms / 1000.0),
                "_transient_motion": True,
            })
            return

        if action == "jog_stop":
            request_id = cmd.get("req_id")
            session_id = str(cmd.get("jog_session_id", "")).strip()

            if not session_id or len(session_id) > 48:
                await self.send_error(
                    websocket,
                    error="invalid_jog_session",
                    message="A valid jog_session_id is required.",
                    request_id=request_id,
                )
                return

            try:
                seq = int(cmd.get("seq", 0))
            except (TypeError, ValueError):
                seq = 0

            if self.h7_runtime_ready.is_set():
                await self.mcu_writes.put({
                    "action": "jog_stop",
                    "jog_session_id": session_id,
                    "seq": seq,
                    "_transient_motion": True,
                })
            return

        # ==========================================================
        # Process commands
        # ==========================================================

        if t == "start_process":
            request_id = cmd.get("req_id")

            if request_id is None:
                self.logger.log.warning(
                    "Process start request did not "
                    "include req_id. The request will "
                    "still be processed, but the HMI "
                    "cannot reliably correlate its result."
                )

            readiness = (
                self.process_start_readiness()
            )

            if not readiness["ready"]:
                reason = str(
                    readiness["blocking_reason"]
                    or "process_start_not_ready"
                )

                self.logger.log.warning(
                    "Process start rejected: "
                    f"reason={reason} "
                    f"req_id={request_id} "
                    f"readiness={readiness}"
                )

                await self.send_error(
                    websocket,
                    error=reason,
                    message=(
                        "The process cannot start because "
                        f"{reason}."
                    ),
                    request_id=request_id,
                    details=readiness,
                )
                return

            try:
                acknowledgement = (
                    await self.request_process_start()
                )

                await self.send_response(
                    websocket,
                    {
                        "type":
                            "process_start_result",
                        "ok": True,
                        "accepted": True,
                        "state":
                            acknowledgement.get(
                                "state",
                                "running",
                            ),
                        "transaction_id":
                            acknowledgement.get(
                                "transaction_id"
                            ),
                        "reason":
                            acknowledgement.get(
                                "reason"
                            ),
                    },
                    request_id=request_id,
                )

            except asyncio.TimeoutError as error:
                await self.send_error(
                    websocket,
                    error=(
                        "process_start_ack_timeout"
                    ),
                    message=(
                        "The robot controller did not "
                        "confirm the process start. The "
                        "command was not retried. Verify "
                        "the current process state before "
                        "trying again."
                    ),
                    request_id=request_id,
                    details=str(error),
                )

            except RuntimeError as error:
                error_code = str(
                    error
                    or "process_start_rejected"
                )

                await self.send_error(
                    websocket,
                    error=error_code,
                    message=error_code,
                    request_id=request_id,
                    details=(
                        self.process_start_readiness()
                    ),
                )

            except Exception as error:
                self.logger.log.exception(
                    "Process start transaction failed."
                )

                await self.send_error(
                    websocket,
                    error="process_start_failed",
                    message=(
                        "The process start request "
                        "could not be completed."
                    ),
                    request_id=request_id,
                    details=str(error),
                )

            return


        if t == "stop_process":
            await self.mcu_writes.put({
                "action": "stop_process",
            })
            return

        if t == "get_process_status":
            await self.mcu_writes.put({
                "action": "get_process_status",
            })
            return

        # ==========================================================
        # Job commands
        # ==========================================================

        if t == "start_job":
            request_id = cmd.get("req_id")

            try:
                if self.job_manager is None:
                    raise RuntimeError(
                        "Job manager is unavailable."
                    )

                operator_initials = str(
                    cmd.get(
                        "operator_initials",
                        "",
                    )
                ).strip()

                blade_serial = str(
                    cmd.get(
                        "blade_serial",
                        "",
                    )
                ).strip()

                blade_type_id = str(
                    cmd.get(
                        "blade_type_id",
                        "",
                    )
                ).strip()

                blade_type_name = str(
                    cmd.get(
                        "blade_type_name",
                        blade_type_id,
                    )
                ).strip()

                catalog = self.config_manager.catalog()

                selection = catalog.get(
                    "selection",
                    {},
                )

                default_robot_id = selection.get(
                    "robot_id"
                )

                if not default_robot_id:
                    raise ValueError(
                        "No robot type is selected."
                    )

                if not blade_type_id:
                    raise ValueError(
                        "blade_type_id is required."
                    )

                configuration_snapshot = (
                    self.config_manager.job_snapshot(
                        default_robot_id,
                        blade_type_id,
                    )
                )

                initialization = (
                    await self.encoder_persistence.create_job_at_zero(
                        create_job=lambda: self.job_manager.start_job(
                            operator_initials=operator_initials,
                            blade_serial=blade_serial,
                            blade_type_id=blade_type_id,
                            blade_type_name=blade_type_name,
                            configuration_snapshot=configuration_snapshot,
                        )
                    )
                )

                job = initialization["job"]
                encoder_initialization = initialization["encoder"]

                self.logger.log.info(
                    "Job created with encoder zero origin: "
                    f"job_uuid={job['job_uuid']} "
                    f"operator={job['operator_initials']} "
                    f"blade_serial={job['blade_serial']} "
                    f"blade_type={job['blade_type_id']} "
                    f"origin_mm="
                    f"{encoder_initialization['rear_distance_mm']} "
                    f"encoder_session_id="
                    f"{encoder_initialization['encoder_session_id']} "
                    f"transaction_id="
                    f"{encoder_initialization['transaction_id']}"
                )

                await self.send_response(
                    websocket,
                    {
                        "type": "job_started",
                        "ok": True,
                        "job": job,
                        "encoder_initialization": {
                            "ok": True,
                            "origin_mm": (
                                encoder_initialization[
                                    "rear_distance_mm"
                                ]
                            ),
                            "radius_m": (
                                encoder_initialization[
                                    "radius_m"
                                ]
                            ),
                            "counts": (
                                encoder_initialization[
                                    "counts"
                                ]
                            ),
                            "encoder_session_id": (
                                encoder_initialization[
                                    "encoder_session_id"
                                ]
                            ),
                            "transaction_id": (
                                encoder_initialization[
                                    "transaction_id"
                                ]
                            ),
                        },
                    },
                    request_id=request_id,
                )

            except ValueError as error:
                self.logger.log.warning(
                    f"Start job rejected: {error}"
                )

                await self.send_error(
                    websocket,
                    error="start_job_rejected",
                    message=str(error),
                    request_id=request_id,
                )

            except Exception as error:
                self.logger.log.exception(
                    "Failed to create job with encoder zero origin."
                )

                # create_job_at_zero() marks a partially created job as
                # terminal when encoder initialization fails. Refresh the
                # in-memory cache so it does not retain that terminal job.
                try:
                    self.job_manager.current_job = (
                        self.job_manager.db.get_active_job()
                    )
                except Exception:
                    self.logger.log.exception(
                        "Failed to refresh the active-job cache after "
                        "encoder initialization failure."
                    )

                await self.send_error(
                    websocket,
                    error="start_job_initialization_failed",
                    message=(
                        "The job could not be started because its encoder origin "
                        "could not be initialized."
                    ),
                    request_id=request_id,
                    details=str(error),
                )

            return

        if t in (
            "get_job",
            "get_active_job",
        ):
            request_id = cmd.get("req_id")

            try:
                if self.job_manager is None:
                    raise RuntimeError(
                        "Job manager is unavailable."
                    )

                job = self.job_manager.get_active_job()

                await self.send_response(
                    websocket,
                    {
                        "type": "active_job",
                        "ok": True,
                        "job": job,
                    },
                    request_id=request_id,
                )

            except Exception as error:
                self.logger.log.exception(
                    "Failed to retrieve active job."
                )

                await self.send_error(
                    websocket,
                    error="get_active_job_failed",
                    message="The active job could not be retrieved.",
                    request_id=request_id,
                    details=str(error),
                )

            return

        if t == "end_job":
            request_id = cmd.get("req_id")

            try:
                if self.job_manager is None:
                    raise RuntimeError(
                        "Job manager is unavailable."
                    )

                job_uuid = str(
                    cmd.get("job_uuid", "")
                ).strip()

                result = str(
                    cmd.get("result", "")
                ).strip().upper()

                raw_failure_reason = cmd.get(
                    "failure_reason"
                )

                if not job_uuid:
                    raise ValueError(
                        "job_uuid is required."
                    )

                if result not in {
                    "PASS",
                    "FAIL",
                    "CANCELLED",
                    "CANCELED",
                    "ABORTED",
                }:
                    raise ValueError(
                        "result must be PASS, FAIL, or CANCELLED."
                    )

                failure_reason: Optional[str] = None

                if raw_failure_reason is not None:
                    if not isinstance(
                        raw_failure_reason,
                        str,
                    ):
                        raise ValueError(
                            "failure_reason must be text."
                        )

                    failure_reason = raw_failure_reason.strip()

                    if len(failure_reason) > 500:
                        raise ValueError(
                            "failure_reason cannot exceed 500 characters."
                        )

                    if not failure_reason:
                        failure_reason = None

                if result != "FAIL":
                    failure_reason = None

                await self.encoder_persistence.flush()

                completed_job = self.job_manager.end_job(
                    job_uuid=job_uuid,
                    result=result,
                    failure_reason=failure_reason,
                )

                self.logger.log.info(
                    "Job closed: "
                    f"job_uuid={job_uuid} "
                    f"result={completed_job.get('result')} "
                    f"failure_reason_present="
                    f"{bool(completed_job.get('failure_reason'))}"
                )

                await self.send_response(
                    websocket,
                    {
                        "type": "job_completed",
                        "ok": True,
                        "job": completed_job,
                    },
                    request_id=request_id,
                )

            except ValueError as error:
                self.logger.log.warning(
                    f"End job rejected: {error}"
                )

                await self.send_error(
                    websocket,
                    error="end_job_rejected",
                    message=str(error),
                    request_id=request_id,
                )

            except Exception as error:
                self.logger.log.exception(
                    "Failed to close job."
                )

                await self.send_error(
                    websocket,
                    error="end_job_failed",
                    message="The job could not be closed.",
                    request_id=request_id,
                    details=str(error),
                )

            return


        if t == "get_job_history":
            request_id = cmd.get(
                "req_id"
            )

            try:
                if self.job_manager is None:
                    raise RuntimeError(
                        "Job manager is unavailable."
                    )

                limit = int(
                    cmd.get(
                        "limit",
                        50,
                    )
                )

                offset = int(
                    cmd.get(
                        "offset",
                        0,
                    )
                )

                search = str(
                    cmd.get(
                        "search",
                        "",
                    )
                    or ""
                ).strip()

                result_filter = str(
                    cmd.get(
                        "result",
                        "all",
                    )
                    or "all"
                ).strip().lower()

                from_utc = cmd.get(
                    "from_utc"
                )

                to_utc = cmd.get(
                    "to_utc"
                )

                result = (
                    self.job_manager
                    .db
                    .list_job_history(
                        limit=limit,
                        offset=offset,
                        search=search,
                        result_filter=(
                            result_filter
                        ),
                        from_utc=from_utc,
                        to_utc=to_utc,
                    )
                )

                self.logger.log.info(
                    "Job history query: "
                    f"search={search!r} "
                    f"result={result_filter!r} "
                    f"from_utc={from_utc!r} "
                    f"to_utc={to_utc!r} "
                    f"offset={offset} "
                    f"returned="
                    f"{len(result['jobs'])} "
                    f"total={result['total']}"
                )

                await self.send_response(
                    websocket,
                    {
                        "type": "job_history",
                        "ok": True,
                        **result,
                    },
                    request_id=request_id,
                )

            except (
                TypeError,
                ValueError,
            ) as error:
                self.logger.log.warning(
                    "Job history request "
                    f"rejected: {error}"
                )

                await self.send_error(
                    websocket,
                    error=(
                        "get_job_history_rejected"
                    ),
                    message=str(error),
                    request_id=request_id,
                )

            except Exception as error:
                self.logger.log.exception(
                    "Failed to retrieve "
                    "job history."
                )

                await self.send_error(
                    websocket,
                    error=(
                        "get_job_history_failed"
                    ),
                    message=(
                        "Job history could not "
                        "be retrieved."
                    ),
                    request_id=request_id,
                    details=str(error),
                )

            return

        if t == "get_applied_transition_profile":
            request_id = cmd.get("req_id")

            try:
                applied = self.config_manager.applied_snapshot()

                if applied is None:
                    raise ValueError(
                        "No transition profile has been applied."
                    )

                await self.send_response(
                    websocket,
                    {
                        "type": "applied_transition_profile",
                        **applied,
                    },
                    request_id=request_id,
                )

            except Exception as error:
                await self.send_error(
                    websocket,
                    error="applied_profile_unavailable",
                    message=str(error),
                    request_id=request_id,
                )

            return

        # ==========================================================
        # Actuator status
        # ==========================================================

        if t == "get_actuator_status":
            if self.latest_actuator_status is not None:
                await websocket.send(
                    json.dumps({
                        "response": (
                            self.latest_actuator_status
                        ),
                    })
                )

            await self.mcu_writes.put({
                "action": "get_actuator_status",
            })
            return

        # ==========================================================
        # Health and firmware
        # ==========================================================

        if t == "get_health":
            await self.responses.put(
                self.latest_health
                or {
                    "type": "health",
                    "state": "unknown",
                    "sources": [],
                    "ts": None,
                    "boot": None,
                    "andon": None,
                    "firmware": None,
                }
            )
            return

        if t == "get_firmware":
            await self.mcu_writes.put({
                "action": "get_firmware",
            })

            await self.responses.put({
                "type": "ack",
                "ok": True,
                "info": "firmware_refresh_requested",
            })
            return

        # ==========================================================
        # Module information and testing
        # ==========================================================

        if t == "get_modules":
            await self.responses.put({
                "type": "modules",
                "items": MODULES,
            })
            return

        # ==========================================================
        # Unhandled commands
        # ==========================================================

        # Preserve the existing behavior for commands handled by
        # another application component.
        if cmd.get("req_id") is not None:
            await self.send_error(
                websocket,
                error="unsupported_request",
                message=f"Unsupported request type: {t or action or 'unknown'}",
                request_id=cmd.get("req_id"),
            )
            return

        await self.commands.put(cmd)

    async def _commit_drive_direction_configuration(
        self,
        *,
        motor_directions,
        encoder_directions,
        actor,
        restored_defaults,
    ):
        """Apply, persist, and reset the logical encoder origin for the new signs.

        Direction application invalidates the H7 encoder session. The reset is
        therefore performed against the new session before process readiness can
        be restored. A failure leaves motion inhibited and startup incomplete.
        """
        active_job = self.job_manager.get_active_job() if self.job_manager else None
        if active_job is not None:
            raise RuntimeError(
                "End or cancel the active job before changing drive directions."
            )

        if restored_defaults:
            result = await self.motor_direction_manager.restore_defaults(actor)
        else:
            result = await self.motor_direction_manager.save(
                motor_directions,
                encoder_directions,
                actor,
            )

        self._motor_direction_runtime_loaded = True
        self._motor_direction_apply_error = None
        self.encoder_startup_ready = False
        self.startup_complete.clear()

        try:
            encoder_result = await self.encoder_persistence.operator_reset()
        except Exception as error:
            self.encoder_startup_ready = False
            self.encoder_startup_error = str(error)
            self.startup_error = (
                "Drive directions were saved, but encoder restoration failed. "
                "Motion remains inhibited."
            )
            self.logger.log.exception(
                "Drive directions applied but encoder reset failed: "
                "transaction_id=%s encoder_session_id=%s",
                result.transaction_id,
                result.encoder_session_id,
            )
            raise RuntimeError(self.startup_error) from error

        confirmed_session = encoder_result.get("encoder_session_id")
        if confirmed_session != result.encoder_session_id:
            self.encoder_startup_ready = False
            self.encoder_startup_error = "encoder_session_mismatch_after_direction_change"
            self.startup_error = (
                "Encoder restoration completed for an unexpected session. "
                "Motion remains inhibited."
            )
            raise RuntimeError(self.startup_error)

        self.encoder_startup_ready = (
            self.encoder_persistence.state == EncoderRecoveryState.VALID
        )
        if not self.encoder_startup_ready:
            self.encoder_startup_error = "encoder_not_valid_after_direction_change"
            self.startup_error = (
                "Encoder restoration did not reach VALID state. Motion remains inhibited."
            )
            raise RuntimeError(self.startup_error)

        self.encoder_startup_error = None
        self.startup_error = None
        if (
            self.configuration_loaded_to_mcu
            and self._feedforward_runtime_loaded
            and self._motor_direction_runtime_loaded
        ):
            self.startup_complete.set()

        return result, encoder_result


    async def save_drive_direction_configuration_request(self, websocket, cmd):
        """Complete request branch for save_drive_direction_configuration."""
        actor = self.validate_actor_initials(cmd)
        result, encoder_result = await self._commit_drive_direction_configuration(
            motor_directions=cmd.get("motor_directions"),
            encoder_directions=cmd.get("encoder_directions"),
            actor=actor,
            restored_defaults=False,
        )
        await self.send_response(
            websocket,
            {
                "type": "drive_direction_configuration_saved",
                "ok": True,
                "transaction_id": result.transaction_id,
                "motor_directions": result.motor_directions,
                "encoder_directions": result.encoder_directions,
                "encoder_session_id": result.encoder_session_id,
                "encoder_restore_required": False,
                "encoder_reset": encoder_result,
                "verification_required": True,
                "is_factory_default": False,
            },
            request_id=cmd.get("req_id"),
        )


    async def restore_drive_direction_defaults_request(self, websocket, cmd):
        """Complete request branch for restore_drive_direction_defaults."""
        actor = self.validate_actor_initials(cmd)
        result, encoder_result = await self._commit_drive_direction_configuration(
            motor_directions=None,
            encoder_directions=None,
            actor=actor,
            restored_defaults=True,
        )
        await self.send_response(
            websocket,
            {
                "type": "drive_direction_defaults_restored",
                "ok": True,
                "transaction_id": result.transaction_id,
                "motor_directions": result.motor_directions,
                "encoder_directions": result.encoder_directions,
                "encoder_session_id": result.encoder_session_id,
                "encoder_restore_required": False,
                "encoder_reset": encoder_result,
                "verification_required": True,
                "is_factory_default": True,
            },
            request_id=cmd.get("req_id"),
        )


    async def response_producer(self, websocket):
        """
        Sends queued robot responses to the connected HMI.
        """
        try:
            while True:
                response = await self.responses.get()

                try:
                    await websocket.send(
                        json.dumps({
                            "response": response,
                        })
                    )

                finally:
                    # asyncio.Queue does not require task_done unless another
                    # part of the application calls queue.join(). If your
                    # custom Queues implementation uses join(), uncomment:
                    #
                    # self.responses.task_done()
                    pass

        except asyncio.CancelledError:
            # Expected when the consumer finishes or the connection closes.
            raise

        except websockets.exceptions.ConnectionClosedOK:
            self.logger.log.info(
                "HMI WebSocket response producer closed normally."
            )

        except websockets.exceptions.ConnectionClosedError as error:
            self.logger.log.warning(
                f"HMI WebSocket response producer lost connection: {error}"
            )

        except websockets.exceptions.ConnectionClosed:
            self.logger.log.info(
                "HMI WebSocket response producer connection closed."
            )

        except Exception:
            self.logger.log.exception(
                "Unexpected WebSocket response-producer error."
            )


    # -------------------------
    # Helpers
    # -------------------------

    @staticmethod
    def get_channel_count_for_robot(robot_data: Dict[str, Any], robot_id: str) -> Optional[int]:
        """
        Returns the channel count for the given robot_id, or None if not found/unspecified.
        """
        robots = robot_data.get("robots", [])
        for r in robots:
            if r.get("id") == robot_id:
                return r.get("channels")
        return None

    def get_motor_direction(self) -> Dict[str, int]:
        catalog = self.config_manager.catalog()

        raw = catalog.get(
            "motor_direction",
            {},
        )

        return {
            "motor_1": int(raw.get("motor_1", 1)),
            "motor_2": int(raw.get("motor_2", -1)),
            "motor_3": int(raw.get("motor_3", -1)),
            "motor_4": int(raw.get("motor_4", 1)),
        }


    def process_start_readiness(self) -> Dict[str, Any]:
        encoder_state = self.encoder_persistence.state

        h7_ready = self.h7_runtime_ready.is_set()
        transition_ready = self.configuration_loaded_to_mcu
        feedforward_ready = self._feedforward_runtime_loaded
        motor_direction_ready = self._motor_direction_runtime_loaded
        encoder_ready = (
            encoder_state == EncoderRecoveryState.VALID
        )

        diagnostic_active = (
            self.service_runtime._active_run_id
            is not None
        )

        diagnostic_plan_active = (
            self.diagnostic_plan_runtime.active
        )

        process_status = (
            self.latest_process_status
            if isinstance(
                self.latest_process_status,
                dict,
            )
            else {}
        )

        process_state = str(
            process_status.get(
                "state",
                "unknown",
            )
        ).strip().lower()

        process_active = (
            process_status.get("active") is True
            or process_state in {
                "running",
                "active",
                "started",
                "starting",
                "stopping",
            }
        )

        blocking_reason = None

        if not h7_ready:
            blocking_reason = "h7_runtime_not_ready"

        elif not transition_ready:
            blocking_reason = (
                "transition_configuration_not_loaded"
            )

        elif not feedforward_ready:
            blocking_reason = (
                "feedforward_configuration_not_loaded"
            )

        elif not motor_direction_ready:
            blocking_reason = (
                "motor_direction_not_loaded"
            )

        elif not encoder_ready:
            blocking_reason = "encoder_not_valid"

        elif diagnostic_active:
            blocking_reason = "diagnostic_active"

        elif diagnostic_plan_active:
            blocking_reason = (
                "diagnostic_plan_active"
            )

        elif process_active:
            blocking_reason = (
                "process_already_active"
            )

        return {
            "ready": blocking_reason is None,
            "blocking_reason": blocking_reason,
            "h7_runtime_ready": h7_ready,
            "transition_configuration_loaded":
                transition_ready,
            "feedforward_configuration_loaded":
                feedforward_ready,
            "motor_direction_loaded":
                motor_direction_ready,
            "encoder_valid": encoder_ready,
            "encoder_state": encoder_state.value,
            "encoder_session_id": (
                self.encoder_persistence
                .accepted_session_id
            ),
            "motor_power_present": (
                self.encoder_persistence
                .motor_power_present
            ),
            "diagnostic_active": diagnostic_active,
            "diagnostic_plan_active":
                diagnostic_plan_active,
            "process_active": process_active,
            "process_state": process_state,
            "startup_complete":
                self.startup_complete.is_set(),
            "startup_error": self.startup_error,
            "configuration_errors": {
                "transition":
                    self.configuration_apply_error,
                "feedforward":
                    self._feedforward_apply_error,
                "motor_direction":
                    self._motor_direction_apply_error,
            },
        }

    def invalidate_h7_runtime_configuration(
        self,
        reason: str,
    ) -> None:
        self.configuration_loaded_to_mcu = False
        self._feedforward_runtime_loaded = False
        self._motor_direction_runtime_loaded = False

        self.configuration_apply_error = reason
        self._feedforward_apply_error = reason
        self._motor_direction_apply_error = reason

        self.startup_complete.clear()
        self.startup_error = reason

    def drain_process_start_acks(self) -> None:
        while True:
            try:
                self.process_start_acks.get_nowait()
            except asyncio.QueueEmpty:
                return

    async def wait_for_process_start_ack(
        self,
        transaction_id: str,
        timeout_s: float = 5.0,
    ) -> Dict[str, Any]:
        loop = asyncio.get_running_loop()
        deadline = loop.time() + timeout_s

        while True:
            remaining = deadline - loop.time()

            if remaining <= 0.0:
                raise asyncio.TimeoutError(
                    "Timed out waiting for H7 "
                    "process-start acknowledgement."
                )

            message = await asyncio.wait_for(
                self.process_start_acks.get(),
                timeout=remaining,
            )

            if not isinstance(message, dict):
                self.logger.log.warning(
                    "Ignoring non-dictionary process "
                    "start acknowledgement: "
                    f"{message!r}"
                )
                continue

            received_transaction_id = str(
                message.get(
                    "transaction_id",
                    "",
                )
            )

            if (
                received_transaction_id
                != transaction_id
            ):
                self.logger.log.warning(
                    "Ignoring unmatched process start "
                    "acknowledgement: "
                    f"expected_transaction_id="
                    f"{transaction_id} "
                    f"received_transaction_id="
                    f"{received_transaction_id}"
                )
                continue

            return message

    async def request_process_start(
        self,
    ) -> Dict[str, Any]:
        async with self._process_start_lock:
            readiness = (
                self.process_start_readiness()
            )

            if not readiness["ready"]:
                reason = str(
                    readiness["blocking_reason"]
                    or "process_start_not_ready"
                )

                self.logger.log.warning(
                    "Process start rejected before "
                    "UART transmission: "
                    f"reason={reason} "
                    f"readiness={readiness}"
                )

                raise RuntimeError(reason)

            transaction_id = (
                f"process-start-{uuid4()}"
            )

            self.drain_process_start_acks()

            await self.mcu_writes.put({
                "action": "start_process",
                "transaction_id":
                    transaction_id,
            })

            self.logger.log.info(
                "Process start queued for H7: "
                f"transaction_id={transaction_id} "
                f"readiness={readiness}"
            )

            try:
                acknowledgement = (
                    await self
                    .wait_for_process_start_ack(
                        transaction_id,
                        timeout_s=5.0,
                    )
                )

            except asyncio.TimeoutError:
                self.logger.log.error(
                    "Process start result unknown: "
                    "H7 acknowledgement timed out. "
                    "The command will not be retried "
                    "automatically. "
                    f"transaction_id={transaction_id}"
                )

                raise

            if (
                acknowledgement.get("ok")
                is not True
                or acknowledgement.get(
                    "accepted"
                )
                is not True
            ):
                error = str(
                    acknowledgement.get(
                        "error",
                        "process_start_rejected",
                    )
                )

                self.logger.log.warning(
                    "H7 rejected process start: "
                    f"transaction_id="
                    f"{transaction_id} "
                    f"error={error} "
                    f"acknowledgement="
                    f"{acknowledgement}"
                )

                raise RuntimeError(error)

            self.logger.log.info(
                "H7 accepted process start: "
                f"transaction_id={transaction_id} "
                f"state="
                f"{acknowledgement.get('state')}"
            )

            return acknowledgement

    @staticmethod
    def normalize_trigger_for_mcu(
        trig: Dict[str, Any],
        *,
        default_delay_s: Optional[
            float
        ] = None,
    ) -> Dict[str, Any]:
        """
        Normalize one trigger into the schema expected
        by the H7.

        Required fields:

        - threshold: int
        - activate: int
        - deactivate: int
        - delay: float seconds

        Optional fields:

        - hold_active: bool

        A one-transition profile uses hold_active=True
        so its only actuator channel is not immediately
        deactivated after activation.
        """
        if not isinstance(
            trig,
            dict,
        ):
            raise ValueError(
                "Trigger must be a dictionary."
            )

        out: Dict[str, Any] = {}

        required_int_fields = [
            "threshold",
            "activate",
            "deactivate",
        ]

        for field in required_int_fields:
            if field not in trig:
                raise ValueError(
                    "Trigger missing required field "
                    f"'{field}'."
                )

            raw_value = trig[
                field
            ]

            if isinstance(
                raw_value,
                bool,
            ):
                raise ValueError(
                    f"Trigger field '{field}' "
                    "must be an integer."
                )

            try:
                value = int(
                    raw_value
                )
            except (
                TypeError,
                ValueError,
                OverflowError,
            ) as error:
                raise ValueError(
                    f"Trigger field '{field}' "
                    "must be an integer."
                ) from error

            out[
                field
            ] = value

        if "delay_ms" in trig:
            raw_delay = trig[
                "delay_ms"
            ]

            if isinstance(
                raw_delay,
                bool,
            ):
                raise ValueError(
                    "Trigger field 'delay_ms' "
                    "must be numeric."
                )

            try:
                out[
                    "delay"
                ] = (
                    float(
                        raw_delay
                    )
                    / 1000.0
                )
            except (
                TypeError,
                ValueError,
                OverflowError,
            ) as error:
                raise ValueError(
                    "Trigger field 'delay_ms' "
                    "must be numeric."
                ) from error

        elif "delay" in trig:
            raw_delay = trig[
                "delay"
            ]

            if isinstance(
                raw_delay,
                bool,
            ):
                raise ValueError(
                    "Trigger field 'delay' "
                    "must be numeric."
                )

            try:
                out[
                    "delay"
                ] = float(
                    raw_delay
                )
            except (
                TypeError,
                ValueError,
                OverflowError,
            ) as error:
                raise ValueError(
                    "Trigger field 'delay' "
                    "must be numeric."
                ) from error

        elif default_delay_s is not None:
            if isinstance(
                default_delay_s,
                bool,
            ):
                raise ValueError(
                    "Default trigger delay "
                    "must be numeric."
                )

            out[
                "delay"
            ] = float(
                default_delay_s
            )

        else:
            raise ValueError(
                "Trigger missing 'delay' or "
                "'delay_ms' and no default "
                "was provided."
            )

        raw_hold_active = trig.get(
            "hold_active",
            False,
        )

        if not isinstance(
            raw_hold_active,
            bool,
        ):
            raise ValueError(
                "Trigger field 'hold_active' "
                "must be true or false."
            )

        out[
            "hold_active"
        ] = raw_hold_active

        return out

    @staticmethod
    def validate_trigger_for_mcu(
        trig: Dict[str, Any],
        channel_count: Optional[
            int
        ] = None,
    ) -> Tuple[
        bool,
        Optional[str],
    ]:
        """
        Validate a normalized H7 trigger.

        The trigger is expected to contain delay in
        seconds and an explicit hold_active Boolean.
        """
        if not isinstance(
            trig,
            dict,
        ):
            return (
                False,
                "Trigger must be a dictionary",
            )

        required_fields = [
            "threshold",
            "activate",
            "deactivate",
            "delay",
            "hold_active",
        ]

        for field in required_fields:
            if field not in trig:
                return (
                    False,
                    "Missing required trigger "
                    f"field: {field}",
                )

        threshold = trig[
            "threshold"
        ]

        activate = trig[
            "activate"
        ]

        deactivate = trig[
            "deactivate"
        ]

        delay = trig[
            "delay"
        ]

        hold_active = trig[
            "hold_active"
        ]

        if (
            isinstance(
                threshold,
                bool,
            )
            or not isinstance(
                threshold,
                int,
            )
        ):
            return (
                False,
                "threshold must be an integer",
            )

        if (
            isinstance(
                activate,
                bool,
            )
            or not isinstance(
                activate,
                int,
            )
        ):
            return (
                False,
                "activate must be an integer",
            )

        if (
            isinstance(
                deactivate,
                bool,
            )
            or not isinstance(
                deactivate,
                int,
            )
        ):
            return (
                False,
                "deactivate must be an integer",
            )

        if (
            isinstance(
                delay,
                bool,
            )
            or not isinstance(
                delay,
                (
                    int,
                    float,
                ),
            )
        ):
            return (
                False,
                "delay must be numeric",
            )

        if not isinstance(
            hold_active,
            bool,
        ):
            return (
                False,
                "hold_active must be true or false",
            )

        if threshold < 0:
            return (
                False,
                "threshold must be >= 0",
            )

        if delay < 0:
            return (
                False,
                "delay must be >= 0 seconds",
            )

        if channel_count is not None:
            if channel_count < 1:
                return (
                    False,
                    "channel_count must be at least 1",
                )

            if not (
                0
                <= activate
                < channel_count
            ):
                return (
                    False,
                    f"activate={activate} out of "
                    f"range [0, {channel_count - 1}]",
                )

            if not (
                0
                <= deactivate
                < channel_count
            ):
                return (
                    False,
                    f"deactivate={deactivate} out of "
                    f"range [0, {channel_count - 1}]",
                )

        return (
            True,
            None,
        )

    async def drain_trigger_acks(self) -> None:
        while True:
            try:
                self.trigger_acks.get_nowait()
            except asyncio.QueueEmpty:
                break

    def _require_safe_manual_actuator_state(
        self,
        *,
        require_fresh_status: bool,
    ) -> Dict[str, Any]:
        """
        Validate CM5-side preconditions for manual actuator output.

        The H7 remains responsible for authoritative interlock,
        E-stop, communication timeout, lease, and hardware-fault
        enforcement.
        """
        if not self.h7_runtime_ready.is_set():
            raise RuntimeError(
                "The H7 runtime is not ready."
            )

        process = (
            self.latest_process_status
            if isinstance(
                self.latest_process_status,
                dict,
            )
            else {}
        )

        process_state = str(
            process.get(
                "state",
                "unknown",
            )
        ).strip().lower()

        process_confirmed_stopped = (
            process.get("active") is False
            and process_state in {
                "stopped",
                "inactive",
                "idle",
            }
        )

        if not process_confirmed_stopped:
            raise RuntimeError(
                "The robot process must be confirmed stopped."
            )

        if (
            self.service_runtime._active_run_id
            is not None
        ):
            raise RuntimeError(
                "Manual actuator control is blocked "
                "while a diagnostic is running."
            )

        if self.diagnostic_plan_runtime.active:
            raise RuntimeError(
                "Manual actuator control is blocked "
                "while the Full System Check is running."
            )

        actuator_status = (
            self.latest_actuator_status
            if isinstance(
                self.latest_actuator_status,
                dict,
            )
            else None
        )

        if not require_fresh_status:
            return (
                dict(actuator_status)
                if actuator_status is not None
                else {}
            )

        received_at = (
            self._latest_actuator_status_received_monotonic
        )

        if (
            actuator_status is None
            or received_at is None
        ):
            raise RuntimeError(
                "Fresh actuator status is unavailable."
            )

        age_s = (
            asyncio.get_running_loop().time()
            - received_at
        )

        if (
            age_s < 0.0
            or age_s
            > self._actuator_status_freshness_limit_s
        ):
            raise RuntimeError(
                "Actuator status is stale."
            )

        try:
            jam_mask = int(
                actuator_status.get(
                    "jam_mask",
                    0,
                )
            )
        except (
            TypeError,
            ValueError,
        ) as error:
            raise RuntimeError(
                "Actuator fault status is invalid."
            ) from error

        if jam_mask != 0:
            raise RuntimeError(
                "Manual actuator control is blocked "
                "because an actuator jam is active."
            )

        if (
            actuator_status.get("pcb_fault")
            is True
        ):
            raise RuntimeError(
                "Manual actuator control is blocked "
                "because the actuator PCB reports a fault."
            )

        return dict(
            actuator_status
        )

    def _process_is_confirmed_active(self) -> bool:
        status = self.latest_process_status

        if not isinstance(status, dict):
            return False

        if status.get("active") is True:
            return True

        state = str(
            status.get(
                "state",
                "",
            )
        ).strip().lower()

        return state in {
            "running",
            "active",
            "started",
            "starting",
            "stopping",
        }


    async def wait_for_trigger_ack(
        self,
        expected_count: int,
        timeout_s: float = 1.5,
    ) -> dict:
        deadline = asyncio.get_running_loop().time() + timeout_s

        while True:
            remaining = deadline - asyncio.get_running_loop().time()
            if remaining <= 0:
                raise asyncio.TimeoutError(
                    "Timed out waiting for trigger load acknowledgement: "
                    f"count={expected_count}"
                )

            msg = await asyncio.wait_for(
                self.trigger_acks.get(),
                timeout=remaining,
            )

            status = str(msg.get("status", "")).lower()
            count = msg.get("count")

            if status == "trigger_load_failed" or msg.get("ok") is False:
                raise RuntimeError(
                    "H7 rejected the trigger table: "
                    f"status={status} error={msg.get('error')} count={count}"
                )

            if status == "triggers_loaded" and count == expected_count:
                self.logger.log.debug(
                    f"Matched trigger load acknowledgement count={count}"
                )
                return msg

            self.logger.log.debug(
                "Ignoring unexpected trigger acknowledgement: "
                f"{msg}, expected_count={expected_count}"
            )

    async def wait_for_trigger_commit_ack(
        self,
        expected_count: int,
        timeout_s: float = 5.0,
    ) -> dict:
        loop = asyncio.get_running_loop()
        deadline = loop.time() + timeout_s

        while True:
            remaining = deadline - loop.time()

            if remaining <= 0:
                self.logger.log.error(
                    "Timed out waiting for H7 trigger reconciliation: "
                    f"expected_count={expected_count}, "
                    f"h7_runtime_ready={self.h7_runtime_ready.is_set()}, "
                    f"mcu_ready={self.mcu_ready.is_set()}, "
                    f"configuration_loaded="
                    f"{self.configuration_loaded_to_mcu}"
                )

                raise asyncio.TimeoutError(
                    "Timed out waiting for H7 trigger reconciliation: "
                    f"count={expected_count}"
                )

            try:
                msg = await asyncio.wait_for(
                    self.trigger_acks.get(),
                    timeout=remaining,
                )
            except asyncio.TimeoutError:
                self.logger.log.error(
                    "No trigger commit acknowledgement was received "
                    "before the deadline: "
                    f"expected_count={expected_count}, "
                    f"timeout_s={timeout_s}, "
                    f"h7_runtime_ready="
                    f"{self.h7_runtime_ready.is_set()}, "
                    f"mcu_ready={self.mcu_ready.is_set()}"
                )
                raise

            if not isinstance(msg, dict):
                self.logger.log.warning(
                    "Ignoring non-dictionary trigger acknowledgement: "
                    f"value={msg!r}, "
                    f"expected_count={expected_count}"
                )
                continue

            status = str(
                msg.get("status", "")
            ).strip().lower()

            count = msg.get("count")

            self.logger.log.debug(
                "Trigger commit acknowledgement candidate: "
                f"status={status}, "
                f"ok={msg.get('ok')}, "
                f"count={count}, "
                f"expected_count={expected_count}, "
                f"error={msg.get('error')}"
            )

            if (
                status == "trigger_load_failed"
                or msg.get("ok") is False
            ):
                raise RuntimeError(
                    "H7 trigger reconciliation failed: "
                    f"status={status}, "
                    f"error={msg.get('error')}, "
                    f"count={count}, "
                    f"expected_count={expected_count}"
                )

            if (
                status == "triggers_reconciled"
                and count == expected_count
                and msg.get("ok") is True
            ):
                self.logger.log.info(
                    "H7 trigger table committed and reconciled: "
                    f"count={count}, "
                    f"position_mm={msg.get('position_mm')}, "
                    f"reached_count={msg.get('reached_count')}, "
                    f"pending_count={msg.get('pending_count')}, "
                    f"commanded_mask={msg.get('commanded_mask')}, "
                    f"feedback_mask={msg.get('feedback_mask')}, "
                    f"pcb_fault={msg.get('pcb_fault')}"
                )
                return msg

            self.logger.log.warning(
                "Ignoring unmatched trigger commit acknowledgement: "
                f"message={msg!r}, "
                f"expected_count={expected_count}"
            )

    async def send_triggers_incrementally(
        self,
        triggers: Iterable[Dict[str, Any]],
        *,
        clear_first: bool = True,
        channel_count: Optional[int] = None,
        default_delay_s: Optional[float] = None,
        wait_for_ack: bool = False,
        ack_timeout_s: float = 1.5,
    ) -> None:
        raw_triggers = list(triggers)
        normalized_triggers = []

        # Validate the complete table before changing H7 state. A malformed
        # trigger rejects the entire operation instead of loading a partial table.
        for index, raw in enumerate(raw_triggers):
            try:
                trigger = self.normalize_trigger_for_mcu(
                    raw,
                    default_delay_s=default_delay_s,
                )
            except Exception as error:
                raise ValueError(
                    f"Invalid trigger at index {index}: {error}"
                ) from error

            valid, validation_error = self.validate_trigger_for_mcu(
                trigger,
                channel_count=channel_count,
            )
            if not valid:
                raise ValueError(
                    f"Invalid trigger at index {index}: {validation_error}"
                )

            normalized_triggers.append(trigger)

            self.logger.log.info(
                "Normalized H7 trigger: "
                f"index={index} "
                f"threshold={trigger['threshold']} "
                f"activate={trigger['activate']} "
                f"deactivate={trigger['deactivate']} "
                f"delay={trigger['delay']} "
                f"hold_active="
                f"{trigger['hold_active']}"
            )

        await self.drain_trigger_acks()

        if clear_first:
            await self.mcu_writes.put({
                "action": "set_triggers",
                "clear": True,
                "begin": True,
            })
            self.logger.log.debug(
                "WS: began atomic trigger-table replacement."
            )

            if wait_for_ack:
                await self.wait_for_trigger_ack(
                    expected_count=0,
                    timeout_s=ack_timeout_s,
                )

        for index, trigger in enumerate(normalized_triggers):
            payload = {
                "action": "set_triggers",
                "trigger": trigger,
            }
            await self.mcu_writes.put(payload)
            self.logger.log.debug(
                f"WS: queued trigger {index + 1}/{len(normalized_triggers)}"
            )

            if wait_for_ack:
                await self.wait_for_trigger_ack(
                    expected_count=index + 1,
                    timeout_s=ack_timeout_s,
                )

        # Commit is always acknowledged, even when callers do not request an
        # acknowledgement for every individual trigger. Apply to Robot must not
        # report success until reconciliation is confirmed by the H7.
        await self.mcu_writes.put({
            "action": "set_triggers",
            "commit": True,
        })

        await self.wait_for_trigger_commit_ack(
            expected_count=len(normalized_triggers),
            timeout_s=max(ack_timeout_s, 3.0),
        )


    @staticmethod
    def validate_actor_initials(
        cmd: Dict[str, Any],
    ) -> str:
        import re

        initials = str(
            cmd.get(
                "actor_initials",
                "",
            )
        ).strip().upper()

        if not re.fullmatch(
            r"[A-Z]{2,6}",
            initials,
        ):
            raise ValueError(
                "actor_initials must contain "
                "2 to 6 letters."
            )

        return initials

    async def flush_encoder_checkpoint(
        self,
    ) -> None:
        try:
            await self.encoder_persistence.flush()

        except Exception:
            self.logger.log.exception(
                "Failed to flush encoder checkpoint."
            )

    async def apply_persisted_motor_directions(
        self,
    ) -> bool:
        """
        Apply the persisted motor-output and encoder-normalization
        installation directions to the current H7 runtime.

        Both direction domains are treated as one atomic installation
        configuration. Startup readiness is not confirmed unless the H7
        acknowledges the matching transaction and exact values.

        This method does not start or resume motion.
        """
        if self._motor_direction_runtime_loaded:
            return True

        try:
            persisted = (
                self.motor_direction_manager.get()
            )

            motor_directions = (
                persisted[
                    "motor_directions"
                ]
            )

            encoder_directions = (
                persisted[
                    "encoder_directions"
                ]
            )

            self.logger.log.info(
                "Applying persisted drive direction "
                "configuration to H7: "
                f"motor_directions="
                f"{motor_directions} "
                f"encoder_directions="
                f"{encoder_directions}"
            )

            result = (
                await self
                .motor_direction_manager
                .apply_persisted()
            )

            self._motor_direction_runtime_loaded = (
                True
            )

            self._motor_direction_apply_error = (
                None
            )

            self.logger.log.info(
                "Persisted drive direction "
                "configuration confirmed by H7: "
                f"transaction_id="
                f"{result.transaction_id} "
                f"motor_directions="
                f"{result.motor_directions} "
                f"encoder_directions="
                f"{result.encoder_directions} "
                f"encoder_restore_required="
                f"{result.encoder_restore_required}"
            )

            await self.responses.put({
                "type":
                    "motor_direction_runtime_status",

                "loaded":
                    True,

                "transaction_id":
                    result.transaction_id,

                "motor_directions":
                    result.motor_directions,

                "encoder_directions":
                    result.encoder_directions,

                "encoder_restore_required":
                    result.encoder_restore_required,
            })

            return True

        except asyncio.CancelledError:
            raise

        except asyncio.TimeoutError as error:
            self._motor_direction_runtime_loaded = (
                False
            )

            self._motor_direction_apply_error = (
                "The H7 did not confirm the "
                "persisted motor and encoder "
                "direction configuration before "
                "the timeout."
            )

            self.startup_complete.clear()

            self.logger.log.exception(
                "Timed out while applying persisted "
                "motor and encoder directions to H7."
            )

            await self.responses.put({
                "type":
                    "motor_direction_runtime_status",

                "loaded":
                    False,

                "error":
                    "drive_direction_acknowledgement_timeout",

                "message":
                    self._motor_direction_apply_error,

                "details":
                    str(error),
            })

            return False

        except (
            KeyError,
            TypeError,
            ValueError,
            RuntimeError,
        ) as error:
            self._motor_direction_runtime_loaded = (
                False
            )

            self._motor_direction_apply_error = (
                str(error)
            )

            self.startup_complete.clear()

            self.logger.log.exception(
                "Persisted motor and encoder direction "
                "configuration is invalid or was "
                "rejected by H7."
            )

            await self.responses.put({
                "type":
                    "motor_direction_runtime_status",

                "loaded":
                    False,

                "error":
                    "drive_direction_configuration_rejected",

                "message":
                    str(error),
            })

            return False

        except Exception as error:
            self._motor_direction_runtime_loaded = (
                False
            )

            self._motor_direction_apply_error = (
                str(error)
            )

            self.startup_complete.clear()

            self.logger.log.exception(
                "Failed to apply persisted motor "
                "and encoder directions."
            )

            await self.responses.put({
                "type":
                    "motor_direction_runtime_status",

                "loaded":
                    False,

                "error":
                    "drive_direction_configuration_failed",

                "message":
                    (
                        "The persisted motor and encoder "
                        "directions could not be applied "
                        "to the robot controller."
                    ),

                "details":
                    str(error),
            })

            return False