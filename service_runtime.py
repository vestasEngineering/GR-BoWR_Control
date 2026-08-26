from __future__ import annotations
import math
import asyncio
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional
from uuid import uuid4


DIAGNOSTIC_METADATA = {
    'motor_1': ('Motor 1 test', True, 7),
    'motor_2': ('Motor 2 test', True, 7),
    'motor_3': ('Motor 3 test', True, 7),
    'motor_4': ('Motor 4 test', True, 7),
    'actuator_1': ('Actuator A test', True, 4),
    'actuator_2': ('Actuator B test', True, 4),
    'actuator_3': ('Actuator C test', True, 4),
    'actuator_4': ('Actuator D test', True, 4),
    'ultrasonic': ('Ultrasonic sensor test', False, 3),
    'battery': ('Battery measurement', False, 2),
    'andon_ring': ('Andon ring test', False, 3),
}


class ServiceRuntime:
    """Builds service views and owns one serialized diagnostic run."""

    def __init__(self, *, db, mcu_writes: asyncio.Queue, modules: List[Dict[str, Any]], logger):
        self.db = db
        self.mcu_writes = mcu_writes
        self.modules = modules
        self.logger = logger
        self._lock = asyncio.Lock()
        self._active_run_id: Optional[str] = None
        self._active_module_id: Optional[str] = None
        self._active_transaction_id: Optional[str] = None
        self._diagnostic_timeout_task: Optional[asyncio.Task] = None
        self._active_operation = None
        self._active_actor = None

    @staticmethod
    def _now() -> str:
        return datetime.now(timezone.utc).isoformat()

    def summary(self, server) -> Dict[str, Any]:
        health = server.latest_health or {}
        job = server.job_manager.get_active_job()
        encoder = server.encoder_persistence
        findings: List[Dict[str, Any]] = []
        faults = list(health.get('fault_modules') or [])
        if not server.h7_runtime_ready.is_set():
            findings.append(self._finding('runtime_not_ready', 'Robot runtime is not ready',
                'The H7 has not confirmed normal command processing.', 'danger', True,
                'Inspect the serial transport and H7 startup state.'))
        if not server.configuration_loaded_to_mcu:
            findings.append(self._finding('configuration_not_loaded', 'Glue-card configuration is not loaded',
                server.configuration_apply_error or 'The volatile trigger table has not been confirmed.',
                'danger', True, 'Reapply the persisted transition profile while the process is stopped.'))
        if encoder.state.value != 'valid':
            findings.append(self._finding('encoder_not_valid', 'Encoder position is not valid',
                encoder.state.value.replace('_', ' '), 'danger', True,
                'Verify physical position and complete the guided encoder recovery procedure.'))
        if server.latest_actuator_status:
            jam = int(server.latest_actuator_status.get('jam_mask') or 0)
            pcb = server.latest_actuator_status.get('pcb_fault') is True
            if jam or pcb:
                findings.append(self._finding('actuator_fault', 'Glue-card actuator fault',
                    f'jam_mask={jam}, pcb_fault={pcb}', 'danger', True,
                    'Inspect the actuator PCB and affected glue-card channels.'))
        for module in faults:
            findings.append(self._finding(f'module_fault:{module}', f'{module} reported a fault',
                'The health resolver reports this module as faulted.', 'danger', True,
                'Open the module details and review recent events.', module_id=str(module)))
        firmware = health.get('firmware') if isinstance(health.get('firmware'), dict) else {}
        battery = self._battery_percent(server)
        return {
            'type': 'service_summary', 'ok': True, 'generated_at': self._now(),
            'robot_state': str(health.get('state') or 'unknown'),
            'process_state': str((server.latest_process_status or {}).get('state') or 'unknown'),
            'runtime_ready': server.h7_runtime_ready.is_set(),
            'configuration_loaded': server.configuration_loaded_to_mcu,
            'encoder_valid': encoder.state.value == 'valid',
            'encoder_position_frozen': encoder.state.value != 'valid',
            'connection_available': True, 'fault_modules': faults,
            'findings': findings, 'firmware': firmware,
            'battery_percent': battery,
            'last_telemetry_at': self._now(),
            'active_job': job,
        }

    def module_items(self, server) -> List[Dict[str, Any]]:
        health_faults = set(
            (server.latest_health or {}).get('fault_modules') or []
        )
        actuator = server.latest_actuator_status or {}
        items = []

        for module in self.modules:
            module_id = str(module['id'])
            latest = self.db.get_latest_diagnostic_for_module(module_id)

            state = 'faulted' if module_id in health_faults else 'healthy'
            summary = 'No active fault is reported.'
            measurements = []
            calibration_payload = None

            if module_id == 'battery':
                pct = self._battery_percent(server)
                if pct is not None:
                    measurements.append({
                        'name': 'Charge',
                        'value': round(pct, 1),
                        'unit': '%',
                        'within_range': pct > 15,
                    })

            if module.get('category') == 'actuator':
                channel = int(module.get('channel', 0))

                jam = bool(
                    int(actuator.get('jam_mask') or 0) & (1 << channel)
                )
                feedback = bool(
                    int(actuator.get('feedback_mask') or 0) & (1 << channel)
                )

                measurements.extend([
                    {
                        'name': 'Feedback',
                        'value': 'Active' if feedback else 'Inactive',
                    },
                    {
                        'name': 'Jammed',
                        'value': 'Yes' if jam else 'No',
                        'within_range': not jam,
                    },
                ])

                if jam or actuator.get('pcb_fault') is True:
                    state = 'faulted'
                    summary = (
                        'Actuator feedback reports a jam or PCB fault.'
                    )

                calibration = self.db.get_actuator_calibration(channel)
                if calibration is not None:
                    calibration_payload = {
                        'valid': True,
                        'channel': channel,
                        'extended_feedback_v': (
                            calibration['extended_feedback_v']
                        ),
                        'retracted_feedback_v': (
                            calibration['retracted_feedback_v']
                        ),
                        'extended_tolerance_v': (
                            calibration['extended_tolerance_v']
                        ),
                        'retracted_tolerance_v': (
                            calibration['retracted_tolerance_v']
                        ),
                        'extension_time_ms': (
                            calibration['extension_time_ms']
                        ),
                        'retraction_time_ms': (
                            calibration['retraction_time_ms']
                        ),
                        'calibrated_at': calibration['calibrated_at'],
                        'calibrated_by': calibration['calibrated_by'],
                    }

            name, may_move, _ = DIAGNOSTIC_METADATA.get(
                module_id,
                (str(module.get('name')), False, 0),
            )

            items.append({
                **module,
                'channel': module.get('channel'),
                'calibration': calibration_payload,
                'state': state,
                'telemetry_fresh': True,
                'test_available': module_id in DIAGNOSTIC_METADATA,
                'may_move_hardware': may_move,
                'summary': summary,
                'last_updated_at': self._now(),
                'measurements': measurements,
                'last_test': latest,
            })

        return items

    def diagnostic_catalog(self, server) -> List[Dict[str, Any]]:
        process_state = str((server.latest_process_status or {}).get('state') or 'unknown').lower()
        base_allowed = server.h7_runtime_ready.is_set() and process_state in {'stopped', 'inactive', 'idle'}
        items = []
        for module in self.modules:
            module_id = str(module['id'])
            if module_id not in DIAGNOSTIC_METADATA:
                continue
            name, may_move, seconds = DIAGNOSTIC_METADATA[module_id]
            allowed = base_allowed and self._active_run_id is None
            reason = None
            if not server.h7_runtime_ready.is_set():
                reason = 'The H7 runtime is not ready.'
            elif process_state not in {'stopped', 'inactive', 'idle'}:
                reason = 'The robot process must be confirmed stopped.'
            elif self._active_run_id is not None:
                reason = 'Another diagnostic is running.'
            items.append({
                'id': f'test:{module_id}', 'name': name, 'module_id': module_id,
                'description': f'Run the approved {name.lower()} and persist its result.',
                'may_move_hardware': may_move, 'estimated_seconds': seconds,
                'preconditions': ['H7 runtime ready', 'Robot process confirmed stopped',
                                  'No other diagnostic running', 'Physical E-stop accessible'],
                'allowed': allowed, 'blocking_reason': reason,
            })
        return items

    async def start(self, *, diagnostic_id: str, module_id: str,
                    actor: Optional[str], server) -> Dict[str, Any]:
        async with self._lock:
            catalog = {item['id']: item for item in self.diagnostic_catalog(server)}
            definition = catalog.get(diagnostic_id)
            if definition is None or definition['module_id'] != module_id:
                raise ValueError('Unknown diagnostic.')
            if not definition['allowed']:
                raise RuntimeError(definition['blocking_reason'] or 'Diagnostic is blocked.')

            run_id = str(uuid4())
            transaction_id = f'diag-{uuid4()}'
            self.db.create_diagnostic_run(
                run_uuid=run_id,
                diagnostic_id=diagnostic_id,
                module_id=module_id,
                requested_by=actor,
                transaction_id=transaction_id,
                session_id=None,
            )
            self.db.update_diagnostic_run(
                run_id,
                state='RUNNING',
                message='Diagnostic command sent to H7.',
            )

            self._active_run_id = run_id
            self._active_module_id = module_id
            self._active_transaction_id = transaction_id
            self._active_operation = 'diagnostic'
            self._active_actor = actor

            command = self._command_for(module_id, run_id, transaction_id)
            await self.mcu_writes.put(command)

            if self._diagnostic_timeout_task is not None:
                self._diagnostic_timeout_task.cancel()

            timeout_s = 40.0 if module_id.startswith('actuator_') else 15.0

            self._diagnostic_timeout_task = asyncio.create_task(
                self._timeout_active_run(run_id, timeout_s=timeout_s),
                name=f'diagnostic-timeout-{run_id}',
            )
            return self.db.get_diagnostic_run(run_id)

    async def start_actuator_calibration(self, *, module_id: str, actor: str, server) -> Dict[str, Any]:
        async with self._lock:
            if self._active_run_id is not None:
                raise RuntimeError('Another diagnostic or calibration is running.')
            process_state = str((server.latest_process_status or {}).get('state') or 'unknown').lower()
            if not server.h7_runtime_ready.is_set() or process_state not in {'stopped','inactive','idle'}:
                raise RuntimeError('H7 must be ready and the process confirmed stopped.')
            module = next((m for m in self.modules if m.get('id') == module_id and m.get('category') == 'actuator'), None)
            if module is None:
                raise ValueError('Unknown actuator module.')
            run_id, transaction_id = str(uuid4()), f'cal-{uuid4()}'
            self.db.create_diagnostic_run(run_uuid=run_id, diagnostic_id=f'calibrate:{module_id}',
                module_id=module_id, requested_by=actor, transaction_id=transaction_id, session_id=None)
            self.db.update_diagnostic_run(run_id, state='RUNNING', message='Install calibration command sent to H7.')
            self._active_run_id, self._active_module_id = run_id, module_id
            self._active_transaction_id, self._active_operation, self._active_actor = transaction_id, 'calibration', actor
            loop = asyncio.get_running_loop()
            await self.mcu_writes.put({'action':'calibrate_actuator','id':module_id,'channel':int(module['channel']),
                'run_id':run_id,'transaction_id':transaction_id,'extend_voltage':3.0,
                '_transient_motion':True,'_expires_monotonic':loop.time()+2.0})
            self._diagnostic_timeout_task = asyncio.create_task(self._timeout_active_run(run_id, 40.0), name=f'calibration-timeout-{run_id}')
            return self.db.get_diagnostic_run(run_id)

    async def confirm_actuator_extension(
        self,
        *,
        run_id: str,
        module_id: str,
        confirmed: bool,
    ) -> Dict[str, Any]:
        async with self._lock:
            if (
                run_id != self._active_run_id
                or module_id != self._active_module_id
            ):
                raise RuntimeError(
                    'Calibration confirmation does not match the active run.'
                )

            if self._active_operation != 'calibration':
                raise RuntimeError(
                    'The active operation is not an actuator calibration.'
                )

            await self.mcu_writes.put({
                'action': 'confirm_actuator_extension',
                'run_id': run_id,
                'transaction_id': self._active_transaction_id,
                'id': module_id,
                'confirmed': bool(confirmed),
                '_transient_motion': True,
            })

            return {
                'type': 'actuator_extension_confirmation',
                'ok': True,
                'run_id': run_id,
                'module_id': module_id,
                'confirmed': bool(confirmed),
            }

    async def _timeout_active_run(self, run_id: str, timeout_s: float) -> None:
        try:
            await asyncio.sleep(timeout_s)

            async with self._lock:
                if self._active_run_id != run_id:
                    return

                try:
                    await self.mcu_writes.put({
                        'action': 'abort_diagnostic',
                        'run_id': run_id,
                        '_transient_motion': True,
                    })
                except Exception:
                    self.logger.log.exception(
                        'Failed to queue timeout abort for diagnostic run %s.',
                        run_id,
                    )

                self.db.update_diagnostic_run(
                    run_id,
                    state='TIMED_OUT',
                    message='The H7 did not return a terminal diagnostic result.',
                    result={
                        'type': 'test_result',
                        'run_id': run_id,
                        'id': self._active_module_id,
                        'pass': False,
                        'reason': 'diagnostic_result_timeout',
                        'measurements': {},
                    },
                )

                self._clear_active(run_id)

        except asyncio.CancelledError:
            raise

        except Exception:
            self.logger.log.exception('Failed to time out diagnostic run.')

    def status(self, run_id: str) -> Dict[str, Any]:
        return self.db.get_diagnostic_run(run_id)

    async def abort(self, run_id: str) -> Dict[str, Any]:
        async with self._lock:
            run = self.db.get_diagnostic_run(run_id)
            if run['state'] in {'passed', 'failed', 'aborted', 'timed_out'}:
                return run
            if run_id != self._active_run_id:
                raise RuntimeError('Diagnostic run is not the active run.')
            await self.mcu_writes.put({
                'action': 'abort_diagnostic',
                'run_id': run_id,
                '_transient_motion': True,
            })
            # Record operator intent. The H7 terminal result may arrive afterward;
            # accept_test_result ignores it because this run is already terminal.
            updated = self.db.update_diagnostic_run(
                run_id,
                state='ABORTED',
                message='Diagnostic aborted by service operator.',
            )
            self._clear_active(run_id)
            return updated

    def accept_test_result(self, message: Dict[str, Any]) -> None:
        run_id = str(message.get('run_id') or '')
        module_id = str(message.get('id') or message.get('module_id') or '')
        transaction_id = str(message.get('transaction_id') or '')

        if not run_id:
            self.logger.log.warning(
                'Ignoring diagnostic result without run_id: message=%r',
                message,
            )
            return
        if run_id != self._active_run_id:
            self.logger.log.warning('Ignoring stale diagnostic result: run_id=%s', run_id)
            return
        if module_id != self._active_module_id:
            self.logger.log.warning(
                'Ignoring diagnostic result with wrong module: expected=%s actual=%s',
                self._active_module_id, module_id,
            )
            return
        if transaction_id != self._active_transaction_id:
            self.logger.log.warning(
                'Ignoring diagnostic result with wrong transaction: run_id=%s', run_id,
            )
            return

        try:
            persisted = self.db.get_diagnostic_run(run_id)
            if persisted['state'] not in {'queued', 'running'}:
                return
            passed = message.get('pass') is True
            if self._active_operation == 'calibration' and message.get('pass') is True:
                channel = int(next(m['channel'] for m in self.modules if m.get('id') == module_id))
                self.db.save_actuator_calibration(channel, message, self._active_actor)
            self.db.update_diagnostic_run(
                run_id,
                state='PASSED' if passed else 'FAILED',
                message=(
                    'Diagnostic passed.' if passed
                    else str(message.get('reason') or 'Diagnostic failed.')
                ),
                result=dict(message),
            )
            self._clear_active(run_id)
        except Exception:
            self.logger.log.exception('Failed to persist diagnostic result.')

    def _clear_active(self, run_id: str) -> None:
        if self._active_run_id != run_id:
            return

        self._active_run_id = None
        self._active_module_id = None
        self._active_transaction_id = None

        self._active_operation = None
        self._active_actor = None

        task = self._diagnostic_timeout_task
        self._diagnostic_timeout_task = None

        if task is not None and task is not asyncio.current_task():
            task.cancel()

    def transport_lost(self, message: Optional[Dict[str, Any]] = None) -> None:
        run_id = self._active_run_id
        if not run_id:
            return
        try:
            self.db.update_diagnostic_run(
                run_id,
                state='FAILED',
                message='MCU serial connection was lost during the diagnostic.',
                result={
                    'type': 'test_result',
                    'id': self._active_module_id,
                    'run_id': run_id,
                    'transaction_id': self._active_transaction_id,
                    'pass': False,
                    'reason': 'mcu_connection_lost',
                    'measurements': {'transport_event': dict(message or {})},
                },
            )
        finally:
            self._clear_active(run_id)

    def _command_for(
        self,
        module_id: str,
        run_id: str,
        transaction_id: str,
    ) -> Dict[str, Any]:
        module = next(
            (
                item
                for item in self.modules
                if item.get("id") == module_id
            ),
            None,
        )

        if module is None:
            raise ValueError(
                "Unknown diagnostic module."
            )

        category = module.get("category")

        if category == "motor":
            loop = asyncio.get_running_loop()

            return {
                "id": module_id,
                "run_id": run_id,
                "transaction_id": transaction_id,
                "action": "test_motor",
                "index": int(
                    module.get(
                        "index",
                        0,
                    )
                ),
                "_transient_motion": True,
                "_expires_monotonic": (
                    loop.time() + 2.0
                ),
            }

        if category == "actuator":
            channel = int(
                module.get(
                    "channel",
                    0,
                )
            )

            calibration = (
                self.db.get_actuator_calibration(
                    channel
                )
            )

            if calibration is None:
                raise RuntimeError(
                    "This actuator must be "
                    "install-calibrated before testing."
                )

            extended_target_v = round(
                float(
                    calibration[
                        "extended_feedback_v"
                    ]
                ),
                4,
            )

            retracted_target_v = round(
                float(
                    calibration[
                        "retracted_feedback_v"
                    ]
                ),
                4,
            )

            extended_tolerance_v = round(
                float(
                    calibration[
                        "extended_tolerance_v"
                    ]
                ),
                4,
            )

            retracted_tolerance_v = round(
                float(
                    calibration[
                        "retracted_tolerance_v"
                    ]
                ),
                4,
            )

            limits = [
                extended_target_v,
                retracted_target_v,
                extended_tolerance_v,
                retracted_tolerance_v,
            ]

            for value in limits:
                if not math.isfinite(value):
                    raise RuntimeError(
                        "The saved actuator calibration "
                        "contains a non-finite value."
                    )

            if extended_target_v < 0.0:
                raise RuntimeError(
                    "The saved extended actuator target "
                    "cannot be negative."
                )

            if retracted_target_v < 0.0:
                raise RuntimeError(
                    "The saved retracted actuator target "
                    "cannot be negative."
                )

            if extended_tolerance_v <= 0.0:
                raise RuntimeError(
                    "The saved extended actuator tolerance "
                    "must be greater than zero."
                )

            if retracted_tolerance_v <= 0.0:
                raise RuntimeError(
                    "The saved retracted actuator tolerance "
                    "must be greater than zero."
                )

            loop = asyncio.get_running_loop()

            return {
                "id": module_id,
                "run_id": run_id,
                "transaction_id": transaction_id,
                "action": "test_actuator",
                "channel": channel,
                "extend_voltage": 3.0,
                "limits": limits,
                "_transient_motion": True,
                "_expires_monotonic": (
                    loop.time() + 2.0
                ),
            }

        if category == "sensor":
            return {
                "id": module_id,
                "run_id": run_id,
                "transaction_id": transaction_id,
                "action": "test_sensor",
                "sensor": module.get(
                    "sensor",
                    "ultrasonic",
                ),
            }

        if category == "andon":
            return {
                "id": module_id,
                "run_id": run_id,
                "transaction_id": transaction_id,
                "action": "test_andon",
            }

        raise ValueError(
            "Unsupported diagnostic category: "
            f"{category}"
        )

    @staticmethod
    def _battery_percent(server) -> Optional[float]:
        return None

    @staticmethod
    def _finding(code, title, message, severity, blocking, recommended_action, module_id=None):
        return {'code': code, 'title': title, 'message': message,
                'severity': severity, 'blocking': blocking,
                'recommended_action': recommended_action, 'module_id': module_id,
                'observed_at': datetime.now(timezone.utc).isoformat()}
