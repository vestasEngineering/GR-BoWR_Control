"""Pi -> Portenta steering line: one JSON object per angle, newline terminated."""
from __future__ import annotations

from typing import Optional


def encode_steering(sequence: int, valid: bool, angle_degrees: Optional[float],
                    offset_mm: Optional[float] = None) -> dict:
    # valid 0 tells the Portenta the line is lost; angle is then a placeholder 0.0.
    ok = bool(valid) and angle_degrees is not None
    return {
        "action": "steer",
        "seq": sequence,
        "angle": round(angle_degrees, 3) if ok else 0.0,
        "offset_mm": round(offset_mm, 3) if ok and offset_mm is not None else 0.0,
        "valid": int(ok),
    }
