import asyncio
import json
import websockets.exceptions
from websockets.server import serve
from typing import Dict, Any, Optional, Tuple, Iterable, List
from logger import Logger
from queues import Queues
from typing import Iterable
from robot_list import (
    load_robot_list, save_robot_list,
    get_defaults, get_transitions,
    thresholds_for_mcu, triggers_from_thresholds, mps_to_qpps, qpps_to_mps
)
from health import HealthModel

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
    def __init__(self, logger:Logger, queues:Queues):
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
    
    async def run(self):
        # Load robot.list.json once
        try:
            self.robot_data = load_robot_list()
            self.logger.log.info("robot.list.json loaded.")
        except Exception as e:
            self.logger.log.exception(f"Failed to load robot.list.json: {e}")
            # Minimal fallback
            self.robot_data = {
                "version": 1,
                "units": "encoder_counts",
                "encoder_scale": 1.0,
                "robots": [{"id": "robotA", "name": "Default", "channels": 4}],
                "blade_types": [{"id": "bladeX", "name": "Default", "max_transitions": 4}],
                "transitions": {"robotA": {"bladeX": [0, 1150, 2300, 3450]}},
                "defaults": {"robot_id": "robotA", "blade_id": "bladeX"},
                "motor_tuning": {
                    "max_speed_qpps": 2500,
                    "accel_qpps_s": 4250,
                    "decel_qpps_s": 8500
                }
            }

        # Apply defaults to MCU
        try:
            rid, bid = get_defaults(self.robot_data)
            vals = get_transitions(self.robot_data, rid, bid)
            thresholds = thresholds_for_mcu(self.robot_data, vals)
            triggers = triggers_from_thresholds(
                thresholds,
                delay_s=9.0,
                first_delay_s=0.0
            )
            self.logger.log.info(f"WS: set_triggers (incremental) count={len(triggers)}")
            channel_count = self.get_channel_count_for_robot(self.robot_data, rid)

            # Incremental send to reduce MCU memory pressure
            await self.send_triggers_incrementally(
                triggers=triggers,
                clear_first=True,
                channel_count=channel_count,
                default_delay_s=None,
                wait_for_ack=True,
                ack_timeout_s=1.5,
            )
            mt = self.robot_data.get("motor_tuning", {
                "max_speed_qpps": 2500,
                "accel_qpps_s": 4250,
                "decel_qpps_s": 8500
            })

            # Backward compatibility for older robot.list.json files
            mt.setdefault("max_speed_qpps", 2500)
            mt.setdefault("accel_qpps_s", 4250)
            mt.setdefault("decel_qpps_s", 8500)

            # Convert accel/decel back to physical units for the ultrasonic PID slew limiter
            accel_mps2 = qpps_to_mps(
                int(mt["accel_qpps_s"]),
                WHEEL_DIAMETER_M,
                ENCODER_CPR
            )

            decel_mps2 = qpps_to_mps(
                int(mt["decel_qpps_s"]),
                WHEEL_DIAMETER_M,
                ENCODER_CPR
            )

            await self.mcu_writes.put({
                "action": "set_motor_tuning",

                # Hardware/RoboClaw-style limits
                "max_speed": int(mt["max_speed_qpps"]),
                "accel": int(mt["accel_qpps_s"]),
                "decel": int(mt["decel_qpps_s"]),

                # Physical-unit limits used by Ultrasonic PID slew limiter
                "accel_mps2": accel_mps2,
                "decel_mps2": decel_mps2,
            })

            await self.apply_motor_direction_to_mcu()

            self.logger.log.info(
                f"WS: applied motor tuning "
                f"max_speed_qpps={mt['max_speed_qpps']} "
                f"accel_qpps_s={mt['accel_qpps_s']} "
                f"decel_qpps_s={mt['decel_qpps_s']} "
                f"accel_mps2={accel_mps2:.4f} "
                f"decel_mps2={decel_mps2:.4f}"
            )

            await self.responses.put({
                "type": "selection_applied",
                "robot_id": rid, "blade_id": bid, "thresholds": thresholds
            })
        except Exception as e:
            self.logger.log.exception(f"Failed to apply/default broadcast: {e}")

        # Start the websocket server
        async with serve(self.connection_handler, "0.0.0.0", 5000):
            asyncio.create_task(self.health_pump())
            await self.shutdown_event.wait()

    async def connection_handler(self, websocket):
        # send a snapshot right away if we have one
        if self.latest_health is not None:
            try:
                await websocket.send(json.dumps({'response': self.latest_health}))
            except Exception:
                pass

        await asyncio.gather(
            self.consumer(websocket),
            self.response_producer(websocket),
        )
        self.shutdown_event.set()
        
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

                if isinstance(msg, dict) and msg.get("type") == "encoder":
                    await self.responses.put(msg)
                    continue

                if isinstance(msg, dict) and msg.get("type") in ("encoder_reset", "encoder_set"):
                    await self.responses.put(msg)
                    continue

                # 2) Quick synthesis from boot_health (immediate snapshot for HMI)
                if isinstance(msg, dict) and msg.get("type") == "boot_health":
                    bh = msg
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
                    
                if isinstance(msg, dict) and msg.get("type") == "fw_version":
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
# receive the messages / commands from the tablet
    async def consumer(self, websocket):
        try:
            async for message in websocket:
                self.connected = True
                await self.consumer_handler(message)
        except websockets.exceptions.ConnectionClosedError:
            self.connected = False


    async def consumer_handler(self, packet):
        self.logger.log.info(packet)
        cmd = json.loads(packet)

        t = cmd.get("type")
        action = cmd.get("action")

        # ----------------------------------------------------------
        # HMI WebSocket heartbeat
        # ----------------------------------------------------------
        if t == "ping":
            await self.responses.put({
                "type": "pong",
                "ts": cmd.get("ts"),
            })
            return
        
        if t == "reset_encoder":
            await self.mcu_writes.put({
                "action": "reset_encoder"
            })
            return

        if t == "set_encoder":
            try:
                radius_m = float(cmd.get("radius_m", 0.0))
            except Exception:
                await self.responses.put({
                    "type": "error",
                    "error": "bad_encoder_value",
                    "details": f"radius_m={cmd.get('radius_m')}"
                })
                return

            if radius_m < 0:
                await self.responses.put({
                    "type": "error",
                    "error": "bad_encoder_value",
                    "details": "radius_m must be >= 0"
                })
                return

            await self.mcu_writes.put({
                "action": "set_encoder",
                "radius_m": radius_m
            })
            return

        # ----------------------------------------------------------
        # Direct MCU passthrough commands from HMI
        # ----------------------------------------------------------

        if action == "jog":
            direction = cmd.get("dir")
            if direction not in ("forward", "backward"):
                await self.responses.put({
                    "type": "error",
                    "id": "jog",
                    "error": "invalid_direction",
                    "details": f"dir={direction}",
                })
                return

            try:
                speed = float(cmd.get("speed", 0.02))
            except Exception:
                speed = 0.02

            try:
                lease_ms = int(cmd.get("lease_ms", 250))
            except Exception:
                lease_ms = 250

            try:
                seq = int(cmd.get("seq", 0))
            except Exception:
                seq = 0

            speed = max(0.0, min(speed, 0.02))
            lease_ms = max(1, min(lease_ms, 500))

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
                seq = int(cmd.get("seq", 0))
            except Exception:
                seq = 0

            await self.mcu_writes.put({
                "action": "jog_stop",
                "seq": seq,
            })
            return


        if t == "get_robot_list":
            # Send current list to HMI
            mt = self.robot_data.get("motor_tuning", {
                "max_speed_qpps": 2500,
                "accel_qpps_s": 4250,
                "decel_qpps_s": 8500
            })

            await self.responses.put({
                "type": "robot_list",
                "robots": self.robot_data["robots"],
                "blade_types": self.robot_data["blade_types"],
                "transitions": self.robot_data["transitions"],
                "defaults": self.robot_data.get("defaults", {}),
                "motor_tuning": {
                    "max_speed_mps": qpps_to_mps(
                        mt["max_speed_qpps"], WHEEL_DIAMETER_M, ENCODER_CPR
                    ),
                    "accel_mps2": qpps_to_mps(
                        mt["accel_qpps_s"], WHEEL_DIAMETER_M, ENCODER_CPR
                    ),
                    "decel_mps2": qpps_to_mps(
                        mt["decel_qpps_s"], WHEEL_DIAMETER_M, ENCODER_CPR
                    ),
                },
                "motor_direction": self.get_motor_direction(),
            })
            return

        elif t == "set_motor_direction":
            try:
                directions = cmd.get("directions")
                if not isinstance(directions, dict):
                    raise ValueError("directions must be an object")

                clean = {}
                for key in ("motor_1", "motor_2", "motor_3", "motor_4"):
                    val = directions.get(key)
                    if val not in (-1, 1):
                        val = int(val)
                    clean[key] = -1 if val < 0 else 1

                self.robot_data["motor_direction"] = clean
                save_robot_list(self.robot_data)

                await self.apply_motor_direction_to_mcu()

                await self.responses.put({
                    "type": "ack",
                    "ok": True,
                    "info": "motor_direction_saved",
                    "motor_direction": clean,
                })

            except Exception as e:
                await self.responses.put({
                    "type": "error",
                    "error": "motor_direction_failed",
                    "details": str(e),
                })

            return
        
        elif t == "get_health":
            await self.responses.put(
                self.latest_health or {
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

        elif t == "get_firmware":
            await self.mcu_writes.put({
                "action": "get_firmware"
            })

            await self.responses.put({
                "type": "ack",
                "ok": True,
                "info": "firmware_refresh_requested"
            })
            return

        elif t == "apply_selection":
            rid = cmd.get("robot_id")
            bid = cmd.get("blade_id")
            try:
                vals = get_transitions(self.robot_data, rid, bid)
                thresholds = thresholds_for_mcu(self.robot_data, vals)
                triggers = triggers_from_thresholds(
                    thresholds,
                    delay_s=9.0,
                    first_delay_s=0.0
                )

                channel_count = self.get_channel_count_for_robot(self.robot_data, rid)
                await self.send_triggers_incrementally(
                    triggers=triggers,
                    clear_first=True,
                    channel_count=channel_count,
                    default_delay_s=None,
                    wait_for_ack=False,  # set True if you want to pace by MCU acks
                    ack_timeout_s=1.5,
                )

                await self.responses.put({
                    "type": "selection_applied",
                    "robot_id": rid,
                    "blade_id": bid,
                    "thresholds": thresholds,
                })

                self.robot_data["defaults"] = {"robot_id": rid, "blade_id": bid}
                save_robot_list(self.robot_data)

            except Exception as e:
                await self.responses.put({
                    "type": "error",
                    "error": "apply_failed",
                    "details": str(e),
                })
            return


        elif t == "save_transitions":
            rid = cmd.get("robot_id")
            bid = cmd.get("blade_id")
            vals = cmd.get("values")
            try:
                self.robot_data["transitions"].setdefault(rid, {})[bid] = vals
                save_robot_list(self.robot_data)

                thresholds = thresholds_for_mcu(self.robot_data, [float(v) for v in vals])
                triggers = triggers_from_thresholds(
                    thresholds,
                    delay_s=9.0,
                    first_delay_s=0.0
                )


                channel_count = self.get_channel_count_for_robot(self.robot_data, rid)
                await self.send_triggers_incrementally(
                    triggers=triggers,
                    clear_first=True,   # new transitions should replace prior set
                    channel_count=channel_count,
                    default_delay_s=None,
                    wait_for_ack=False,
                    ack_timeout_s=1.5,
                )

                await self.responses.put({"type": "ack", "ok": True, "info": "saved"})
                mt = self.robot_data.get("motor_tuning", {
                    "max_speed_qpps": 2500,
                    "accel_qpps_s": 4250,
                    "decel_qpps_s": 8500
                })

                await self.responses.put({
                    "type": "robot_list",
                    "robots": self.robot_data["robots"],
                    "blade_types": self.robot_data["blade_types"],
                    "transitions": self.robot_data["transitions"],
                    "defaults": self.robot_data.get("defaults", {}),
                    "motor_tuning": {
                        "max_speed_mps": qpps_to_mps(
                            mt["max_speed_qpps"], WHEEL_DIAMETER_M, ENCODER_CPR
                        ),
                        "accel_mps2": qpps_to_mps(
                            mt["accel_qpps_s"], WHEEL_DIAMETER_M, ENCODER_CPR
                        ),
                        "decel_mps2": qpps_to_mps(
                            mt["decel_qpps_s"], WHEEL_DIAMETER_M, ENCODER_CPR
                        ),
                    },
                })
                await self.responses.put({
                    "type": "selection_applied",
                    "robot_id": rid,
                    "blade_id": bid,
                    "thresholds": thresholds,
                })

            except Exception as e:
                await self.responses.put({
                    "type": "error",
                    "error": "save_failed",
                    "details": str(e),
                })
            return

        elif t == "get_modules":
            await self.responses.put({"type": "modules", "items": MODULES})
            return
        
        elif t == "set_motor_tuning":
            try:
                # HMI sends physical units
                max_speed_mps = float(cmd.get("max_speed"))
                accel_mps2 = float(cmd.get("accel"))
                decel_mps2 = float(cmd.get("decel"))

                if max_speed_mps < 0:
                    raise ValueError("max_speed must be >= 0")
                if accel_mps2 < 0:
                    raise ValueError("accel must be >= 0")
                if decel_mps2 < 0:
                    raise ValueError("decel must be >= 0")

                # Convert to MCU/RoboClaw units
                max_speed_qpps = mps_to_qpps(
                    max_speed_mps,
                    WHEEL_DIAMETER_M,
                    ENCODER_CPR
                )

                accel_qpps = mps_to_qpps(
                    accel_mps2,
                    WHEEL_DIAMETER_M,
                    ENCODER_CPR
                )

                decel_qpps = mps_to_qpps(
                    decel_mps2,
                    WHEEL_DIAMETER_M,
                    ENCODER_CPR
                )

                # Persist hardware units
                self.robot_data["motor_tuning"] = {
                    "max_speed_qpps": max_speed_qpps,
                    "accel_qpps_s": accel_qpps,
                    "decel_qpps_s": decel_qpps
                }

                save_robot_list(self.robot_data)

                # Apply immediately to MCU
                await self.mcu_writes.put({
                    "action": "set_motor_tuning",
                    "max_speed": max_speed_qpps,
                    "accel": accel_qpps,
                    "decel": decel_qpps,

                    # Also send physical units for PID slew limiting
                    "accel_mps2": accel_mps2,
                    "decel_mps2": decel_mps2
                })

                await self.responses.put({
                    "type": "ack",
                    "ok": True,
                    "info": "motor_tuning_saved"
                })

            except Exception as e:
                await self.responses.put({
                    "type": "error",
                    "error": "motor_tuning_failed",
                    "details": str(e)
                })

            return

        elif t == "test_module":
            mid = cmd.get("id")
            if not mid or mid not in MODULE_BY_ID:
                await self.responses.put({"type":"error","error":"bad_request","details":"unknown module id","id":mid})
                return

            mod = MODULE_BY_ID[mid]
            cat = mod["category"]

            # Ack immediately so HMI shows "Running"
            await self.responses.put({"type":"ack","ok":True,"info":"test_started","id":mid})

            if cat == "motor":
                await self.mcu_writes.put({
                    "action":"test_motor","id":mid,
                    "index": int(mod.get("index",0)),
                    "speed": 0.02, "duration_ms": 600
                })
            elif cat == "actuator":
                await self.mcu_writes.put({
                    "action":"test_actuator","id":mid,
                    "channel": int(mod.get("channel",0)),
                    "voltage": 3.0, "tolerance": 0.8, "settle_ms": 100
                })
            elif cat == "sensor":
                sensor_kind = mod.get("sensor","ultrasonic")
                payload = {"action":"test_sensor","id":mid,"sensor": sensor_kind}
                if sensor_kind == "digital":
                    payload["pin"] = int(mod.get("pin",1))
                    payload["expect"] = bool(mod.get("expect",True))
                    payload["sample_ms"] = 300
                await self.mcu_writes.put(payload)
            elif cat == "andon":
                await self.mcu_writes.put({"action":"test_light","id": mid})
            elif cat == "servo":
                await self.mcu_writes.put({"action": "test_servo", "id": mid})
            else:
                await self.responses.put({"type":"error","error":"unsupported_module","details":f"category={cat}","id":mid})
            return

        await self.commands.put(cmd)


    async def response_producer(self, websocket):
        while True:
            response = await self.responses.get()
            await websocket.send(json.dumps({'response':response}))

            #response = await self.mcu_reads.get()
            #await websocket.send(json.dumps({'mcu_reads':response}))


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
                try:
                    await self.wait_for_trigger_ack(expected_count=0, timeout_s=ack_timeout_s)
                except asyncio.TimeoutError:
                    self.logger.log.warning(
                        "WS: No trigger ACK after clear within timeout; continuing..."
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
                try:
                    await self.wait_for_trigger_ack(
                        expected_count=expected_count,
                        timeout_s=ack_timeout_s,
                    )
                except asyncio.TimeoutError:
                    self.logger.log.warning(
                        f"WS: No matching trigger ACK for count={expected_count} "
                        f"within {ack_timeout_s}s; continuing..."
                    )