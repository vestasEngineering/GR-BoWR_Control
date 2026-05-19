import json, os, tempfile
from typing import Dict, Any, List, Tuple
from pathlib import Path
import math

PATH = Path(__file__).with_name("robot.list.json")

def load_robot_list() -> Dict[str, Any]:
    if not PATH.exists():
        raise FileNotFoundError(f"Config not found: {PATH}")
    with PATH.open("r", encoding="utf-8") as f:
        data = json.load(f)
    validate(data)
    return data

def save_robot_list(data: Dict[str, Any]) -> None:
    validate(data)
    # Atomic write using a temp file in the same directory
    tmp_fd, tmp_path = tempfile.mkstemp(
        dir=str(PATH.parent),
        prefix="robot.list.",
        suffix=".json"
    )
    try:
        with open(tmp_fd, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
        Path(tmp_path).replace(PATH)  # atomic replace
    finally:
        try:
            Path(tmp_path).unlink(missing_ok=True)
        except Exception:
            pass

def validate(data: Dict[str, Any]) -> None:
    assert isinstance(data.get("robots"), list) and data["robots"], "robots required"
    assert isinstance(data.get("blade_types"), list) and data["blade_types"], "blade_types required"
    assert isinstance(data.get("transitions"), dict), "transitions required"
    defaults = data.get("defaults", {})
    assert defaults.get("robot_id") and defaults.get("blade_id"), "defaults.robot_id & blade_id required"

    robot_ids = {r["id"] for r in data["robots"]}
    blade_ids = {b["id"] for b in data["blade_types"]}
    for rid, blades in data["transitions"].items():
        assert rid in robot_ids, f"Unknown robot_id: {rid}"
        assert isinstance(blades, dict)
        for bid, vals in blades.items():
            assert bid in blade_ids, f"Unknown blade_id: {bid}"
            assert isinstance(vals, list) and 1 <= len(vals) <= 8, f"Transitions count 1..8 for {rid}/{bid}"
            for v in vals:
                if not isinstance(v, (int, float)):
                    raise AssertionError(f"Non-numeric transition {v} in {rid}/{bid}")

    rid, bid = defaults["robot_id"], defaults["blade_id"]
    assert rid in data["transitions"] and bid in data["transitions"][rid], \
           f"Default transitions missing for {rid}/{bid}"

def get_defaults(data: Dict[str, Any]) -> Tuple[str, str]:
    d = data.get("defaults", {})
    return d.get("robot_id"), d.get("blade_id")

def get_transitions(data: Dict[str, Any], rid: str, bid: str) -> List[float]:
    vals = data["transitions"].get(rid, {}).get(bid)
    if vals is None:
        raise KeyError(f"No transitions for {rid}/{bid}")
    return [float(v) for v in vals]


def thresholds_for_mcu(data: Dict[str, Any], vals: List[float]) -> List[int]:
    units = (data.get("units") or "").lower()
    scale = float(data.get("encoder_scale", 1000.0))
    if units == "m":
        return [int(round(v * scale)) for v in vals]
    return [int(round(v)) for v in vals]


def triggers_from_thresholds(
    thresholds: List[int],
    *,
    delay_s: float = 9.0,
    first_delay_s: float = 0.0
) -> List[Dict[str, int]]:
    """
    Build MCU triggers using seconds for the delay.
    First trigger uses first_delay_s (default 0.0s), all others use delay_s (default 9.0s).
    """
    n = len(thresholds)
    res: List[Dict[str, int]] = []
    for i, th in enumerate(thresholds):
        res.append({
            "threshold": int(th),
            "activate": i,
            "deactivate": (i - 1) % n,
            "delay": float(first_delay_s if i == 0 else delay_s),  # seconds
        })
    return res

    
def mps_to_qpps(mps: float, wheel_diameter_m: float, encoder_cpr: int) -> int:
    wheel_circ = math.pi * wheel_diameter_m
    revs_per_sec = mps / wheel_circ
    return int(round(revs_per_sec * encoder_cpr))


def qpps_to_mps(qpps: int, wheel_diameter_m: float, encoder_cpr: int) -> float:
    wheel_circ = math.pi * wheel_diameter_m
    revs_per_sec = qpps / encoder_cpr
    return revs_per_sec * wheel_circ
