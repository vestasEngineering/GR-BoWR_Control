# health.py
from __future__ import annotations
from typing import Optional, Dict, Any, List
import time

HealthStr = str  # "Healthy" | "Warning" | "Faulted" | "CommsLost" | "Paused" | "Unknown"

class HealthModel:
    """
    Aggregates MCU 'boot_health' and 'andon_diag' messages into a compact
    health snapshot for downstream consumers (HMI, logs, etc.).
    """

    def __init__(self) -> None:
        self.last_snapshot: Optional[Dict[str, Any]] = None
        self._last_andon: Optional[Dict[str, Any]] = None
        self._last_boot: Optional[Dict[str, Any]] = None
        self._last_fw: Optional[Dict[str, Any]] = None   # latest firmware object from boot_health

    # ---------------------------
    # Public API
    # ---------------------------
    def update_from_mcu(self, msg: Dict[str, Any]) -> Optional[Dict[str, Any]]:
        """
        Feed with MCU messages already parsed as dict.
        Returns a new snapshot only when it *changes*; else None.
        """
        if msg.get("type") == "boot_health":
            self._last_boot = msg
            fw = msg.get("firmware")
            if isinstance(fw, dict):
                self._last_fw = fw

        elif msg.get("type") == "fw_version":
            self._last_fw = msg

        elif msg.get("type") == "andon_diag":
            # MCU sends {"andon_diag": {...}} and serial_server forwards with {'type': 'andon_diag', **diag}
            self._last_andon = msg
        else:
            return None

        snapshot = self._compute_snapshot()
        if self._is_changed(snapshot):
            self.last_snapshot = snapshot
            return snapshot
        return None

    # ---------------------------
    # Internals
    # ---------------------------
    def _compute_snapshot(self) -> Dict[str, Any]:
        ts = time.time()

        # 1) Primary: ANDON drives runtime state
        if self._last_andon:
            return self._snapshot_from_andon(self._last_andon, ts)

        # 2) Fallback: during boot, rely on boot_health if present
        if self._last_boot:
            return self._snapshot_from_boot(self._last_boot, ts)

        # 3) Nothing seen yet
        return {
            "type": "health",
            "ts": ts,
            "state": "Unknown",
            "sources": [],
            "andon": None,
            "boot": None,
            "firmware": None
        }

    def _snapshot_from_andon(self, andon: Dict[str, Any], ts: float) -> Dict[str, Any]:
        """
        'andon' is like: {'type':'andon_diag', 'code': int, 'state': str, 'ms':..., 'override': bool, 'reasons': {...}}
        It may also include 'faults': [str], 'fault_modules': [str] in v2 schema.
        """
        reasons = andon.get("reasons", {}) or {}
        code    = andon.get("code")
        state_s = andon.get("state")

        faults        = (andon.get("faults") or [])
        fault_modules = (andon.get("fault_modules") or [])

        # Precedence (mirror MCU resolver):
        # E-Stop/Fault > CommsLost > BatteryLow/Blocked > PausedOrJog > Running/Green > default Warning
        if reasons.get("isEStop", False) or reasons.get("hasFault", False):
            hs: HealthStr = "Faulted"
        elif reasons.get("isCommsLost", False):
            hs = "CommsLost"
        elif reasons.get("isBatteryLow", False) or reasons.get("isBlockedOrStarved", False):
            hs = "Warning"
        elif reasons.get("isPausedOrJog", False):
            hs = "Paused"
        else:
            if state_s == "GREEN" or reasons.get("isRunning", False):
                hs = "Healthy"
            elif state_s in ("BLINK_RED", "RED"):
                hs = "Faulted"
            elif state_s in ("BLINK_YELLOW", "YELLOW"):
                hs = "Warning"
            elif state_s in ("BLINK_BLUE", "BLUE"):
                hs = "Paused"
            else:
                hs = "Warning"

        # Sources: prefer module-specific list; fall back to booleans if absent
        if fault_modules:
            src: List[str] = list(fault_modules)  # e.g., ["battery","ultrasonic","motor_1"]
        else:
            src = []
            if reasons.get("isEStop"):              src.append("EStop")
            if reasons.get("hasFault"):             src.append("ModuleFault")
            if reasons.get("isCommsLost"):          src.append("CommsLost")
            if reasons.get("isBatteryLow"):         src.append("BatteryLow")
            if reasons.get("isBlockedOrStarved"):   src.append("BlockedOrStarved")
            if reasons.get("isPausedOrJog"):        src.append("PausedOrJog")
            if reasons.get("isRunning"):            src.append("Running")

        return {
            "type": "health",
            "ts": ts,
            "state": hs,
            "sources": src,
            "faults": faults,                   # raw fault keys for logs/QA
            "fault_modules": fault_modules,     # module ids for HMI
            "andon": {
                "code": code,
                "state": state_s,
                "override": andon.get("override", False),
                "ms": andon.get("ms"),
            },
            "boot": self._boot_summary(self._last_boot) if self._last_boot else None,
            "firmware": self._firmware_summary(self._last_fw),
        }

    def _snapshot_from_boot(self, boot: Dict[str, Any], ts: float) -> Dict[str, Any]:
        """
        'boot' is like: {'type':'boot_health', 'ok': bool, 'checks': { ... } }
        """
        ok = bool(boot.get("ok", False))
        hs: HealthStr = "Healthy" if ok else "Warning"

        # If specific critical subsystems failed at boot, classify as Faulted
        checks = (boot.get("checks") or {})
        critical_fails = []
        for key in ("can", "motors", "actuator"):
            if not ((checks.get(key) or {}).get("ok", True)):
                critical_fails.append(key)
        if critical_fails:
            hs = "Faulted"

        src: List[str] = []
        if not ok:
            src.append("BootDegraded")
        src.extend([f"BootFail:{c}" for c in critical_fails])

        return {
            "type": "health",
            "ts": ts,
            "state": hs,
            "sources": src,
            "andon": None,
            "boot": self._boot_summary(boot),
            "firmware": self._firmware_summary(self._last_fw),

        }

    @staticmethod
    def _boot_summary(boot: Optional[Dict[str, Any]]) -> Optional[Dict[str, Any]]:
        if not boot:
            return None
        checks = boot.get("checks") or {}
        return {
            "ok": bool(boot.get("ok", False)),
            "can_ok": (checks.get("can") or {}).get("ok"),
            "motors_ok": (checks.get("motors") or {}).get("ok"),
            "actuator_ok": (checks.get("actuator") or {}).get("ok"),
            "ultrasonic_ok": (checks.get("ultrasonic") or {}).get("ok"),
            "ultrasonic_servo_ok": (checks.get("ultrasonic_servo") or {}).get("ok"),
            "battery_ok": (checks.get("battery") or {}).get("ok"),
        }
    
    
    @staticmethod
    def _firmware_summary(fw: Optional[Dict[str, Any]]) -> Optional[Dict[str, Any]]:
        if not fw:
            return None
        feats = (fw.get("features") or {})
        return {
            "model": fw.get("model"),
            "fleet_id": fw.get("fleet_id"),
            "semver": fw.get("semver"),
            "build": fw.get("build"),
            "board": fw.get("board"),
            "platform": fw.get("platform"),
            "channel": fw.get("channel"),
            "git": fw.get("git"),
            "features": {
                "andon_light": bool(feats.get("andon_light")),
                "ultrasonic": bool(feats.get("ultrasonic")),
                "ultrasonic_servo": bool(feats.get("ultrasonic_servo")),
                "actuator": bool(feats.get("actuator")),
                "battery_oled": bool(feats.get("battery_oled")),
                "motors": bool(feats.get("motors")),
            },
        }

    def _is_changed(self, snapshot: Dict[str, Any]) -> bool:
        """Only push to HMI when semantically changed."""
        if self.last_snapshot is None:
            return True
        prev = self.last_snapshot

        # health state & sources
        if prev.get("state") != snapshot.get("state"):
            return True
        if prev.get("sources") != snapshot.get("sources"):
            return True

        # andon code/state
        prev_and = (prev.get("andon") or {})
        curr_and = (snapshot.get("andon") or {})
        if prev_and.get("code") != curr_and.get("code"):
            return True
        if prev_and.get("state") != curr_and.get("state"):
            return True

        # boot summary diffs
        prev_boot = (prev.get("boot") or {})
        curr_boot = (snapshot.get("boot") or {})
        for k in ("ok", "can_ok", "motors_ok", "actuator_ok", "battery_ok"):
            if prev_boot.get(k) != curr_boot.get(k):
                return True

        # specific to module faults
        if (prev.get("fault_modules") or []) != (snapshot.get("fault_modules") or []):
            return True
        
        # firmware changed (e.g., reflash) -> push
        if (prev.get("firmware") or {}) != (snapshot.get("firmware") or {}):
            return True

        return False