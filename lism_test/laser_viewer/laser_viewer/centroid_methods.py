from __future__ import annotations

from dataclasses import asdict, dataclass
from typing import Iterable

import numpy as np

ADC_MAX = 65535


@dataclass(frozen=True)
class CentroidComparison:
    """Centroid measurements calculated over one detected footprint.

    geometric_center is the production measurement. The other values are
    diagnostics that reveal footprint asymmetry, threshold sensitivity, and
    differences between geometry and optical-response weighting.
    """

    start: int
    end: int
    width: int
    geometric_center: float
    active_pixel_mean: float
    drop_weighted_centroid: float
    half_drop_position: float
    peak_drop_position: int
    geometric_minus_weighted: float
    geometric_minus_half_drop: float

    def to_dict(self) -> dict:
        return asdict(self)


def _validate_frame(frame: Iterable[int]) -> np.ndarray:
    values = np.asarray(frame)
    if values.ndim != 1:
        raise ValueError("frame must be a one-dimensional pixel array")
    if values.size == 0:
        raise ValueError("frame must not be empty")
    if not np.issubdtype(values.dtype, np.number):
        raise ValueError("frame must contain numeric pixel values")
    numeric = values.astype(np.float64, copy=False)
    if not np.all(np.isfinite(numeric)):
        raise ValueError("frame contains non-finite values")
    if np.any(numeric < 0) or np.any(numeric > ADC_MAX):
        raise ValueError(f"frame values must be between 0 and {ADC_MAX}")
    return numeric


def _validate_bounds(start: int, end: int, pixel_count: int) -> tuple[int, int]:
    start = int(start)
    end = int(end)
    if start < 0 or end < 0:
        raise ValueError("region bounds must be non-negative")
    if start > end:
        raise ValueError("region start must not exceed region end")
    if end >= pixel_count:
        raise ValueError("region end is outside the frame")
    return start, end


def _half_drop_position(indices: np.ndarray, drops: np.ndarray) -> float:
    """Return the linearly interpolated position containing half the drop area."""
    total = float(drops.sum())
    if total <= 0.0:
        return float((indices[0] + indices[-1]) / 2.0)

    target = total / 2.0
    cumulative = np.cumsum(drops)
    crossing = int(np.searchsorted(cumulative, target, side="left"))
    if crossing == 0:
        return float(indices[0])

    before = float(cumulative[crossing - 1])
    current = float(drops[crossing])
    if current <= 0.0:
        return float(indices[crossing])
    fraction = min(1.0, max(0.0, (target - before) / current))
    return float(indices[crossing - 1] + fraction)


def compare_centroids(frame: Iterable[int], start: int, end: int) -> CentroidComparison:
    """Calculate geometric and signal-derived centers for a detected region.

    Region bounds are inclusive. No threshold is reapplied here because the
    detector already owns footprint selection. This prevents the comparison
    code from silently changing the detector's selected footprint.
    """
    values = _validate_frame(frame)
    start, end = _validate_bounds(start, end, values.size)
    indices = np.arange(start, end + 1, dtype=np.float64)
    region = values[start : end + 1]
    drops = np.clip(ADC_MAX - region, 0.0, None)

    geometric = float((start + end) / 2.0)
    active_mean = float(indices.mean())
    total_drop = float(drops.sum())
    weighted = geometric if total_drop <= 0.0 else float(np.dot(indices, drops) / total_drop)
    half_drop = _half_drop_position(indices, drops)
    peak = int(start + int(np.argmax(drops)))

    return CentroidComparison(
        start=start,
        end=end,
        width=end - start + 1,
        geometric_center=geometric,
        active_pixel_mean=active_mean,
        drop_weighted_centroid=weighted,
        half_drop_position=half_drop,
        peak_drop_position=peak,
        geometric_minus_weighted=geometric - weighted,
        geometric_minus_half_drop=geometric - half_drop,
    )


def comparison_from_detection(frame: Iterable[int], detection: dict) -> dict:
    """Add a center-method comparison to a detector result dictionary."""
    if detection is None:
        raise ValueError("detection is required")
    if "start" not in detection or "end" not in detection:
        raise ValueError("detection must contain start and end")
    return compare_centroids(frame, detection["start"], detection["end"]).to_dict()
