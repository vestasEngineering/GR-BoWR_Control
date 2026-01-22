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
    thresholds_for_mcu, triggers_from_thresholds
)

class WebsocketServer():
    def __init__(self, logger:Logger, queues:Queues):
        self.logger = logger
        #self.images = queues.images
        self.commands = queues.commands
        #self.angles    = queues.angles
        #self.offsets   = queues.offsets
        self.responses = queues.responses
        self.mcu_reads = queues.mcu_reads
        self.mcu_writes = queues.mcu_writes
        self.connected = False
        self.shutdown_event = asyncio.Event()
        #self._active_connections = set[WebsocketServerProtocol] = set()
    
    async def run(self):
        # Load robot.list.json once
        try:
            self.robot_data = load_robot_list()
            self.logger.log.info("robot.list.json loaded.")
        except Exception as e:
            self.logger.log.exception(f"Failed to load robot.list.json: {e}")
            # Minimal fallback
            self.robot_data = {
                "version": 1, "units": "encoder_counts", "encoder_scale": 1.0,
                "robots": [{"id": "robotA", "name": "Default", "channels": 4}],
                "blade_types": [{"id": "bladeX", "name": "Default", "max_transitions": 4}],
                "transitions": {"robotA": {"bladeX": [0, 1150, 2300, 3450]}},
                "defaults": {"robot_id": "robotA", "blade_id": "bladeX"}
            }

        # Apply defaults to MCU
        try:
            rid, bid = get_defaults(self.robot_data)
            vals = get_transitions(self.robot_data, rid, bid)
            thresholds = thresholds_for_mcu(self.robot_data, vals)

            # Build triggers (likely returns dicts with keys including delay_ms)
            triggers = triggers_from_thresholds(thresholds, delay_ms=9)

            self.logger.log.info(f"WS: set_triggers (incremental) count={len(triggers)}")

            channel_count = self.get_channel_count_for_robot(self.robot_data, rid)

            # Incremental send to reduce MCU memory pressure
            await self.send_triggers_incrementally(
                triggers=triggers,
                clear_first=True,
                channel_count=channel_count,
                default_delay_s=None,   # if triggers miss delay, set something like 0.009 here
                wait_for_ack=False,     # set True if your reader pushes MCU acks into self.mcu_reads
                ack_timeout_s=1.5,
            )

            # Also enqueue an initial broadcast to HMI
            await self.responses.put({
                "type": "robot_list",
                "v": self.robot_data.get("version", 1),
                "robots": self.robot_data["robots"],
                "blade_types": self.robot_data["blade_types"],
                "transitions": self.robot_data["transitions"],
                "defaults": self.robot_data.get("defaults", {})
            })
            await self.responses.put({
                "type": "selection_applied",
                "robot_id": rid, "blade_id": bid, "thresholds": thresholds
            })
        except Exception as e:
            self.logger.log.exception(f"Failed to apply/default broadcast: {e}")


        # Start the websocket server
        async with serve(self.connection_handler, "0.0.0.0", 5000):
            await self.shutdown_event.wait()

    async def connection_handler(self, websocket):
        await asyncio.gather(
            self.consumer(websocket),
            #self.image_producer(websocket),
            self.response_producer(websocket),
        )
        self.shutdown_event.set()

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

        # --- Simple config commands ---
        t = cmd.get("type")

        if t == "get_robot_list":
            # Send current list to HMI
            await self.responses.put({
                "type": "robot_list",
                "v": self.robot_data.get("version", 1),
                "robots": self.robot_data["robots"],
                "blade_types": self.robot_data["blade_types"],
                "transitions": self.robot_data["transitions"],
                "defaults": self.robot_data.get("defaults", {}),
            })
            return

        elif t == "apply_selection":
            rid = cmd.get("robot_id")
            bid = cmd.get("blade_id")
            try:
                vals = get_transitions(self.robot_data, rid, bid)
                thresholds = thresholds_for_mcu(self.robot_data, vals)
                triggers = triggers_from_thresholds(thresholds, delay_ms=9)

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
                triggers = triggers_from_thresholds(thresholds, delay_ms=9)

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
                await self.responses.put({
                    "type": "robot_list",
                    "v": self.robot_data.get("version", 1),
                    "robots": self.robot_data["robots"],
                    "blade_types": self.robot_data["blade_types"],
                    "transitions": self.robot_data["transitions"],
                    "defaults": self.robot_data.get("defaults", {}),
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

        # Keep your existing behavior for other commands
        await self.commands.put(cmd)


    #async def image_producer(self, websocket):
        #while True:
            #image = await self.images.get()
            #angle = await self.angles.get()
            #offset = await self.offsets.get()
            
            #await websocket.send(json.dumps({'image': image}))


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
        """
        Incrementally send triggers to the MCU, minimizing parsing/memory pressure.
        Each message is: {"action":"set_triggers", "trigger": {...}, ["clear": True on first]}
        - Normalizes to MCU schema (delay in seconds).
        - Optionally validates against channel_count.
        - Optional ack wait after each send (expects an MCU response on self.mcu_reads).
        """
        triggers_list = list(triggers)  # in case caller passes a generator

        if not triggers_list and clear_first:
            # If no triggers, we can still clear MCU state to be explicit.
            msg = {"action": "set_triggers", "clear": True}
            await self.mcu_writes.put(msg)
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
                self.logger.log.warning(f"WS: skipping invalid trigger at index {idx}: {err}; trigger={t}")
                continue

            payload = {"action": "set_triggers", "trigger": t}
            if clear_first and idx == 0:
                payload["clear"] = True

            await self.mcu_writes.put(payload)
            self.logger.log.debug(f"WS: set_triggers -> {payload}")

            # Yield to the loop so other tasks can run promptly
            await asyncio.sleep(0)

            # Optional ack handling (expects your read loop to push MCU replies into self.mcu_reads)
            if wait_for_ack:
                try:
                    resp = await asyncio.wait_for(self.mcu_reads.get(), timeout=ack_timeout_s)
                    # If you need to verify the response:
                    # if not (isinstance(resp, dict) and resp.get("status") == "triggers_loaded"):
                    #     self.logger.log.warning(f"Unexpected MCU ack: {resp}")
                    self.logger.log.debug(f"MCU ack: {resp}")
                except asyncio.TimeoutError:
                    self.logger.log.warning("WS: No MCU ack for set_triggers within timeout; continuing...")
