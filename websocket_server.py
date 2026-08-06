import asyncio
import json
import math
from typing import Any, Dict, Iterable, List, Optional, Tuple

import websockets.exceptions
from websockets.server import serve

from configuration_manager import ConfigurationManager
from encoder_persistence import (EncoderPersistenceCoordinator, EncoderRecoveryState)
from health import HealthModel
from logger import Logger
from queues import Queues


WHEEL_DIAMETER_M = 0.048
ENCODER_CPR = 4096

MODULES = [
    # Motors (axes 0..3)
    {"id": "motor_1", "name": "Motor 1 (Axis 0)", "category": "motor", "index": 0},
    {"id": "motor_2", "name": "Motor 2 (Axis 1)", "category": "motor", "index": 1},
    {"id": "motor_3", "name": "Motor 3 (Axis 2)", "category": "motor", "index": 2},
    {"id": "motor_4", "name": "Motor 4 (Axis 3)", "category": "motor", "index": 3},

    # Actuators (channels 0..3)
    {"id": "actuator_1", "name": "Actuator A (Ch 0)", "category": "actuator", "channel": 0},
    {"id": "actuator_2", "name": "Actuator B (Ch 1)", "category": "actuator", "channel": 1},
    {"id": "actuator_3", "name": "Actuator C (Ch 2)", "category": "actuator", "channel": 2},
    {"id": "actuator_4", "name": "Actuator D (Ch 3)", "category": "actuator", "channel": 3},

    # Sensors
    {"id": "ultrasonic", "name": "Ultrasonic Sensor", "category": "sensor", "sensor": "ultrasonic"},
    {"id": "battery", "name": "Battery", "category": "sensor", "sensor": "battery"},
    {"id": "jog_forward_switch",  "name": "Jog Forward Switch (D1)",  "category": "sensor", "sensor": "digital", "pin": 1,  "expect": True},
    {"id": "jog_backward_switch", "name": "Jog Backward Switch (D10)","category": "sensor", "sensor": "digital", "pin": 10, "expect": True},


    #Ultrasonic Servo
    {"id": "ultrasonic_servo", "name": "Ultrasonic Servo", "category": "servo"},

    # Andon
    {"id": "andon_ring", "name": "Andon Ring", "category": "andon"},
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
        self.shutdown_event = asyncio.Event()
        self.health = HealthModel()
        self.latest_health: Optional[dict] = None
        #self._active_connections = set[WebsocketServerProtocol] = set()
        self.trigger_acks = queues.trigger_acks
        self.encoder_acks = queues.encoder_acks
        self.mcu_ready = queues.mcu_ready
        self.job_manager = job_manager

        if job_manager is None:
            raise ValueError("job_manager is required")

        self.config_manager = ConfigurationManager(
            db=job_manager.db,
            mcu_writes=self.mcu_writes,
            trigger_sender=self.send_triggers_incrementally,
            logger=self.logger,
        )

        self.latest_process_status: Optional[dict] = None
        self.latest_actuator_status: Optional[dict] = None

        self.h7_runtime_ready = queues.h7_runtime_ready

        self.encoder_startup_ready = False
        self.encoder_startup_error: Optional[str] = None

        self.configuration_loaded_to_mcu = False
        self.configuration_apply_error: Optional[str] = None

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

            # Apply configuration first. This loads the volatile H7
            # triggerBuffer independently of encoder restoration.
            self._initial_runtime_configuration_attempted = True

            configuration_ready = (
                await self.apply_persisted_configuration()
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
                    "message": (
                        str(error)
                        or self.encoder_startup_error
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
                    "encoder_valid": True,
                })

                self.logger.log.info(
                    "Robot runtime initialization completed."
                )

                return

            self.startup_complete.clear()

            errors = []

            if not configuration_ready:
                errors.append(
                    self.configuration_apply_error
                    or "Configuration was not loaded."
                )

            if not self.encoder_startup_ready:
                errors.append(
                    self.encoder_startup_error
                    or "Encoder is not valid."
                )

            self.startup_error = " ".join(errors)

            await self.responses.put({
                "type": "startup_status",
                "state": "fault",
                "ready": False,
                "stage": "incomplete",
                "configuration_loaded":
                    self.configuration_loaded_to_mcu,
                "encoder_valid":
                    self.encoder_startup_ready,
                "message": self.startup_error,
            })

            self.logger.log.error(
                "Robot runtime initialization incomplete: "
                f"configuration_loaded="
                f"{self.configuration_loaded_to_mcu}, "
                f"encoder_valid="
                f"{self.encoder_startup_ready}, "
                f"error={self.startup_error}"
            )

        except asyncio.CancelledError:
            self.logger.log.info(
                "Robot runtime initialization cancelled."
            )
            raise

    async def configuration_reapply_worker(self) -> None:
        """Reapply the persisted trigger table after each H7 serial session."""
        while True:
            await self._configuration_reapply_event.wait()
            self._configuration_reapply_event.clear()

            try:
                await asyncio.wait_for(
                    self.h7_runtime_ready.wait(),
                    timeout=30.0,
                )

                if self.configuration_loaded_to_mcu:
                    continue

                loaded = await self.apply_persisted_configuration()

                if not loaded:
                    self.logger.log.error(
                        "Transition configuration reapply failed "
                        "after MCU reconnection."
                    )

            except asyncio.TimeoutError:
                self.configuration_loaded_to_mcu = False
                self.configuration_apply_error = (
                    "H7 runtime readiness timed out after "
                    "serial connection restoration."
                )
                self.logger.log.error(
                    self.configuration_apply_error
                )

            except asyncio.CancelledError:
                raise

            except Exception as error:
                self.configuration_loaded_to_mcu = False
                self.configuration_apply_error = str(error)
                self.logger.log.exception(
                    "Unexpected configuration reapply failure."
                )

    async def connection_handler(self, websocket):
        """
        Handles one HMI WebSocket connection.

        The WebSocket server remains running after the tablet disconnects so
        that the HMI can reconnect automatically.
        """
        self.connected = True

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
            self.connected = False

            self.logger.log.info(
                "HMI WebSocket client disconnected."
            )

            # Do not call self.shutdown_event.set() here.
            #
            # The server must remain running so the tablet can reconnect.

        
    async def health_pump(self):
        """
        Consumes MCU messages (andon_diag, boot_health, test_result), computes a consolidated
        health snapshot, and pushes it to self.responses whenever it changes.
        """
        while True:
            msg = await self.mcu_reads.get()
            try:
                # 1) Pass-through module test results
                if isinstance(msg, dict) and msg.get("type") == "test_result":
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
                        self.configuration_loaded_to_mcu = False

                        self.configuration_apply_error = (
                            "Waiting for H7 runtime readiness "
                            "after serial connection restoration."
                        )

                        self.startup_complete.clear()

                        # The first serial connection belongs to initial startup.
                        # initialize_robot_runtime() owns that configuration apply.
                        #
                        # After the initial attempt, every new serial session must
                        # reload the volatile H7 trigger table.
                        if self._initial_runtime_configuration_attempted:
                            self._configuration_reapply_event.set()


                        self.logger.log.warning(
                            "MCU serial session restored. Waiting for H7 "
                            "runtime readiness before reapplying transitions."
                        )

                    elif event_type == "mcu_serial_connection_lost":
                        self.configuration_loaded_to_mcu = False
                        self.configuration_apply_error = (
                            "MCU serial connection was lost."
                        )
                        self.startup_complete.clear()

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
                    self.latest_actuator_status = (
                        dict(msg)
                    )

                    self.logger.log.info(
                        "Forwarding actuator status "
                        "to HMI: "
                        f"commanded_mask="
                        f"{msg.get('commanded_mask')} "
                        f"feedback_mask="
                        f"{msg.get('feedback_mask')} "
                        f"jam_mask="
                        f"{msg.get('jam_mask')} "
                        f"pcb_fault="
                        f"{msg.get('pcb_fault')} "
                        f"ts_ms="
                        f"{msg.get('ts_ms')}"
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
        self.logger.log.info(
            "HMI request: "
            f"type={cmd.get('type')} "
            f"action={cmd.get('action')} "
            f"req_id={cmd.get('req_id')} "
            f"actor_initials={cmd.get('actor_initials')}"
        )

        t = cmd.get("type")
        action = cmd.get("action")

        # ==========================================================
        # Request-specific robot configuration APIs
        # ==========================================================

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

        if action == "jog":
            direction = cmd.get("dir")

            if direction not in (
                "forward",
                "backward",
            ):
                await self.responses.put({
                    "type": "error",
                    "id": "jog",
                    "error": "invalid_direction",
                    "details": f"dir={direction}",
                })
                return

            try:
                speed = float(
                    cmd.get(
                        "speed",
                        0.02,
                    )
                )
            except (TypeError, ValueError):
                speed = 0.02

            try:
                lease_ms = int(
                    cmd.get(
                        "lease_ms",
                        250,
                    )
                )
            except (TypeError, ValueError):
                lease_ms = 250

            try:
                seq = int(
                    cmd.get(
                        "seq",
                        0,
                    )
                )
            except (TypeError, ValueError):
                seq = 0

            speed = max(
                0.0,
                min(
                    speed,
                    0.02,
                ),
            )

            lease_ms = max(
                1,
                min(
                    lease_ms,
                    500,
                ),
            )

            await self.mcu_writes.put({
                "action": "jog",
                "dir": direction,
                "speed": speed,
                "lease_ms": lease_ms,
                "seq": seq,
            })
            return

        if action == "jog_stop":
            try:
                seq = int(
                    cmd.get(
                        "seq",
                        0,
                    )
                )
            except (TypeError, ValueError):
                seq = 0

            await self.mcu_writes.put({
                "action": "jog_stop",
                "seq": seq,
            })
            return

        # ==========================================================
        # Process commands
        # ==========================================================

        if t == "start_process":
            if not self.configuration_loaded_to_mcu:
                await self.send_error(
                    websocket,
                    error="configuration_not_loaded",
                    message=(
                        "The Glue Card transition configuration has not "
                        "been loaded into the robot controller."
                    ),
                    request_id=cmd.get("req_id"),
                    details=self.configuration_apply_error,
                )
                return

            if (
                self.encoder_persistence.state
                != EncoderRecoveryState.VALID
            ):
                await self.send_error(
                    websocket,
                    error="encoder_not_valid",
                    message=(
                        "Encoder recovery must complete "
                        "before the process can start."
                    ),
                    request_id=cmd.get("req_id"),
                )
                return

            await self.mcu_writes.put({
                "action": "start_process",
            })
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

        if t == "test_module":
            module_id = cmd.get("id")

            if (
                not module_id
                or module_id not in MODULE_BY_ID
            ):
                await self.responses.put({
                    "type": "error",
                    "error": "bad_request",
                    "details": "unknown module id",
                    "id": module_id,
                })
                return

            module = MODULE_BY_ID[module_id]
            category = module["category"]

            # Acknowledge immediately so the HMI can show
            # that the test is running.
            await self.responses.put({
                "type": "ack",
                "ok": True,
                "info": "test_started",
                "id": module_id,
            })

            if category == "motor":
                await self.mcu_writes.put({
                    "action": "test_motor",
                    "id": module_id,
                    "index": int(
                        module.get(
                            "index",
                            0,
                        )
                    ),
                    "speed": 0.02,
                    "duration_ms": 600,
                })

            elif category == "actuator":
                await self.mcu_writes.put({
                    "action": "test_actuator",
                    "id": module_id,
                    "channel": int(
                        module.get(
                            "channel",
                            0,
                        )
                    ),
                    "voltage": 3.0,
                    "tolerance": 0.8,
                    "settle_ms": 100,
                })

            elif category == "sensor":
                sensor_kind = module.get(
                    "sensor",
                    "ultrasonic",
                )

                payload = {
                    "action": "test_sensor",
                    "id": module_id,
                    "sensor": sensor_kind,
                }

                if sensor_kind == "digital":
                    payload["pin"] = int(
                        module.get(
                            "pin",
                            1,
                        )
                    )

                    payload["expect"] = bool(
                        module.get(
                            "expect",
                            True,
                        )
                    )

                    payload["sample_ms"] = 300

                await self.mcu_writes.put(payload)

            elif category == "andon":
                await self.mcu_writes.put({
                    "action": "test_light",
                    "id": module_id,
                })

            elif category == "servo":
                await self.mcu_writes.put({
                    "action": "test_servo",
                    "id": module_id,
                })

            else:
                await self.responses.put({
                    "type": "error",
                    "error": "unsupported_module",
                    "details": (
                        f"category={category}"
                    ),
                    "id": module_id,
                })

            return

        # ==========================================================
        # Unhandled commands
        # ==========================================================

        # Preserve the existing behavior for commands handled by
        # another application component.
        await self.commands.put(cmd)


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
        md = self.robot_data.get("motor_direction", {
            "motor_1": 1,
            "motor_2": -1,
            "motor_3": -1,
            "motor_4": 1,
        })

        clean = {}
        for key, default in {
            "motor_1": 1,
            "motor_2": -1,
            "motor_3": -1,
            "motor_4": 1,
        }.items():
            val = md.get(key, default)
            clean[key] = -1 if int(val) < 0 else 1

        return clean


    async def apply_motor_direction_to_mcu(self):
        md = self.get_motor_direction()

        await self.mcu_writes.put({
            "action": "set_motor_direction",
            "directions": [
                md["motor_1"],
                md["motor_2"],
                md["motor_3"],
                md["motor_4"],
            ],
        })

        self.logger.log.info(f"WS: applied motor direction {md}")

    @staticmethod
    def normalize_trigger_for_mcu(trig: Dict[str, Any], *, default_delay_s: Optional[float] = None) -> Dict[str, Any]:
        """
        Normalize one trigger into the MCU's expected schema:
          - threshold: int
          - activate: int
          - deactivate: int
          - delay: float (seconds)
        Supports input with 'delay_ms' or 'delay'. If both absent, uses default_delay_s (if provided).
        """
        out: Dict[str, Any] = {}

        # Pass-through numeric fields (raise early if missing)
        required_int_fields = ["threshold", "activate", "deactivate"]
        for f in required_int_fields:
            if f not in trig:
                raise ValueError(f"Trigger missing required field '{f}'")
            out[f] = int(trig[f])

        # Delay handling
        if "delay_ms" in trig:
            out["delay"] = float(trig["delay_ms"]) / 1000.0
        elif "delay" in trig:
            # Assume caller is already providing seconds
            out["delay"] = float(trig["delay"])
        elif default_delay_s is not None:
            out["delay"] = float(default_delay_s)
        else:
            raise ValueError("Trigger missing 'delay'/'delay_ms' and no default provided")

        return out

    @staticmethod
    def validate_trigger_for_mcu(trig: Dict[str, Any], channel_count: Optional[int] = None) -> Tuple[bool, Optional[str]]:
        """
        Validate ranges and presence for a normalized MCU trigger (expects 'delay' in seconds).
        Returns (ok, error_message).
        """
        # Basic checks
        if trig.get("threshold") is None or trig.get("activate") is None or trig.get("deactivate") is None or trig.get("delay") is None:
            return False, "Missing one or more required fields"

        # Non-negative values
        if trig["threshold"] < 0:
            return False, "threshold must be >= 0"
        if trig["delay"] < 0:
            return False, "delay must be >= 0 seconds"

        # Channel bounds if known
        if channel_count is not None:
            if not (0 <= trig["activate"] < channel_count):
                return False, f"activate={trig['activate']} out of range [0, {channel_count-1}]"
            if not (0 <= trig["deactivate"] < channel_count):
                return False, f"deactivate={trig['deactivate']} out of range [0, {channel_count-1}]"

        return True, None

    async def drain_trigger_acks(self):
        while True:
            try:
                self.trigger_acks.get_nowait()
            except asyncio.QueueEmpty:
                break

    async def wait_for_trigger_ack(self, expected_count: int, timeout_s: float = 1.5):
        """
        Wait specifically for {"status":"triggers_loaded","count": expected_count}
        from the MCU.
        """
        deadline = asyncio.get_running_loop().time() + timeout_s

        while True:
            remaining = deadline - asyncio.get_running_loop().time()
            if remaining <= 0:
                raise asyncio.TimeoutError(
                    f"Timed out waiting for trigger ACK count={expected_count}"
                )

            msg = await asyncio.wait_for(self.trigger_acks.get(), timeout=remaining)

            status = str(msg.get("status", "")).lower()
            count = msg.get("count")

            if status == "triggers_loaded" and count == expected_count:
                self.logger.log.debug(f"Matched trigger ACK count={count}")
                return msg

            # Ignore stale / mismatched trigger ACKs
            self.logger.log.debug(
                f"Ignoring unexpected trigger ACK: {msg}, expected count={expected_count}"
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
        triggers_list = list(triggers)

        # Clear stale acks before starting a new programming sequence
        await self.drain_trigger_acks()

        # If we’re replacing the table, clear once as a standalone message.
        if clear_first:
            await self.mcu_writes.put({"action": "set_triggers", "clear": True})
            self.logger.log.debug("WS: set_triggers -> {'clear': True}")

            if wait_for_ack:
                await self.wait_for_trigger_ack(
                    expected_count=0,
                    timeout_s=ack_timeout_s,
                )

        if not triggers_list:
            if clear_first:
                self.logger.log.info("WS: set_triggers -> [clear only] (no triggers)")
            return

        for idx, raw in enumerate(triggers_list):
            try:
                t = self.normalize_trigger_for_mcu(raw, default_delay_s=default_delay_s)
            except Exception as e:
                self.logger.log.warning(f"WS: skipping invalid trigger at index {idx}: {e}")
                continue

            ok, err = self.validate_trigger_for_mcu(t, channel_count=channel_count)
            if not ok:
                self.logger.log.warning(
                    f"WS: skipping invalid trigger at index {idx}: {err}; trigger={t}"
                )
                continue

            payload = {"action": "set_triggers", "trigger": t}
            await self.mcu_writes.put(payload)
            self.logger.log.debug(f"WS: set_triggers -> {payload}")

            await asyncio.sleep(0)

            if wait_for_ack:
                expected_count = idx + 1

                await self.wait_for_trigger_ack(
                    expected_count=expected_count,
                    timeout_s=ack_timeout_s,
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