from __future__ import annotations

import hashlib
import json
from dataclasses import asdict, dataclass, field
from typing import Mapping, Optional

import numpy as np

ADC_MAX = 65535.0


@dataclass(frozen=True)
class TriggerConfig:
    threshold: int = 60000
    minimum_width: int = 2
    maximum_width: int = 512
    minimum_integrated_drop: float = 10000.0
    maximum_gap: int = 3
    edge_window: int = 5
    edge_minimum_active: int = 3
    quantile_low: float = 0.10
    quantile_high: float = 0.90
    core_fraction: float = 0.80
    core_minimum_width: int = 2
    maximum_center_disagreement: float = 12.0
    minimum_active_pixels: int = 12
    minimum_active_coverage: float = 0.8368
    maximum_internal_gap: int = 8
    maximum_internal_gap_fraction: float = 0.25
    minimum_sustained_span: int = 12

    def __post_init__(self) -> None:
        if not 0 <= self.threshold <= 65535: raise ValueError("threshold must be between 0 and 65535")
        if self.minimum_width < 1: raise ValueError("minimum_width must be at least 1")
        if self.maximum_width < self.minimum_width: raise ValueError("maximum_width must be at least minimum_width")
        if self.minimum_integrated_drop < 0: raise ValueError("minimum_integrated_drop cannot be negative")
        if self.maximum_gap < 0: raise ValueError("maximum_gap cannot be negative")
        if self.edge_window < 1 or self.edge_window % 2 == 0: raise ValueError("edge_window must be a positive odd integer")
        if not 1 <= self.edge_minimum_active <= self.edge_window: raise ValueError("edge_minimum_active must be within edge_window")
        if not 0.0 <= self.quantile_low < self.quantile_high <= 1.0: raise ValueError("quantiles must satisfy 0 <= low < high <= 1")
        if not 0.0 < self.core_fraction <= 1.0: raise ValueError("core_fraction must be in (0, 1]")
        if self.core_minimum_width < 1: raise ValueError("core_minimum_width must be at least 1")
        if self.maximum_center_disagreement < 0: raise ValueError("maximum_center_disagreement cannot be negative")
        if self.minimum_active_pixels < 1: raise ValueError("minimum_active_pixels must be positive")
        if not 0.0 <= self.minimum_active_coverage <= 1.0: raise ValueError("minimum_active_coverage must be between 0 and 1")
        if self.maximum_internal_gap < 0: raise ValueError("maximum_internal_gap cannot be negative")
        if not 0.0 <= self.maximum_internal_gap_fraction <= 1.0: raise ValueError("maximum_internal_gap_fraction must be between 0 and 1")
        if self.minimum_sustained_span < 1: raise ValueError("minimum_sustained_span must be positive")

    @classmethod
    def from_mapping(cls, values: Mapping[str, object]) -> "TriggerConfig":
        names = cls.__dataclass_fields__.keys()
        return cls(**{name: values[name] for name in names if name in values})

    @property
    def configuration_id(self) -> str:
        encoded = json.dumps(asdict(self), sort_keys=True, separators=(",", ":")).encode()
        return hashlib.sha256(encoded).hexdigest()[:16]


@dataclass(frozen=True)
class QualityConfig:
    edge_exclusion: int = 2
    high_rail_threshold: int = 65503
    low_plateau_threshold: int = 1024
    maximum_internal_gap: int = 8
    minimum_background_rail_fraction: float = 0.50
    minimum_interior_low_fraction: float = 0.10
    minimum_shape_score: float = 0.50
    minimum_active_coverage: float = 0.20
    maximum_internal_gap_fraction: float = 0.50

    def __post_init__(self) -> None:
        if self.edge_exclusion < 0: raise ValueError("edge_exclusion cannot be negative")
        if not 0 <= self.high_rail_threshold <= 65535: raise ValueError("high_rail_threshold must be between 0 and 65535")
        if not 0 <= self.low_plateau_threshold < self.high_rail_threshold: raise ValueError("low_plateau_threshold must be below high_rail_threshold")
        if self.maximum_internal_gap < 0: raise ValueError("maximum_internal_gap cannot be negative")
        for name in ("minimum_background_rail_fraction", "minimum_interior_low_fraction", "minimum_shape_score", "minimum_active_coverage", "maximum_internal_gap_fraction"):
            if not 0.0 <= getattr(self, name) <= 1.0: raise ValueError(f"{name} must be between 0 and 1")


@dataclass
class RegionResult:
    start: int
    end: int
    width: int
    centroid: float
    boundary_center: float
    minimum_raw: int
    integrated_drop: float
    shape_score: float
    measurement_quality: bool
    background_rail_fraction: float
    interior_low_fraction: float
    interior_p90: float
    active_coverage: float
    active_pixel_count: int
    largest_internal_gap: int
    internal_gap_fraction: float
    sustained_span: Optional[float] = None
    rejection_reason: str = "none"
    sustained_left_edge: Optional[float] = None
    sustained_right_edge: Optional[float] = None
    sustained_edge_center: Optional[float] = None
    quantile_low_position: Optional[float] = None
    quantile_high_position: Optional[float] = None
    quantile_center: Optional[float] = None
    deep_core_start: Optional[int] = None
    deep_core_end: Optional[int] = None
    deep_core_center: Optional[float] = None
    active_median_center: Optional[float] = None
    selected_center: Optional[float] = None
    diagnostic_center: Optional[float] = None
    selected_center_method: str = "none"
    center_disagreement: Optional[float] = None
    center_confidence: str = "invalid"
    filled_gap_count: int = 0
    filled_gap_pixels: int = 0


@dataclass
class DetectionResult:
    regions: list[RegionResult] = field(default_factory=list)
    selected_region: Optional[RegionResult] = None
    detected_candidate: bool = False


def _runs(mask: np.ndarray) -> list[tuple[int, int]]:
    changes = np.diff(np.concatenate(([False], mask, [False])).astype(np.int8))
    return list(zip(np.flatnonzero(changes == 1).tolist(), (np.flatnonzero(changes == -1) - 1).tolist()))


def _fill_small_gaps(mask: np.ndarray, maximum_gap: int) -> np.ndarray:
    cleaned = mask.copy()
    if maximum_gap <= 0: return cleaned
    for start, end in _runs(~cleaned):
        if start > 0 and end < cleaned.size - 1 and end - start + 1 <= maximum_gap:
            cleaned[start:end + 1] = True
    return cleaned


def _gap_metrics(mask: np.ndarray) -> tuple[int, float, int, int]:
    active = np.flatnonzero(mask)
    if active.size < 2: return 0, 0.0, 0, 0
    interior = mask[active[0]:active[-1] + 1]
    gaps = _runs(~interior)
    largest = max((b - a + 1 for a, b in gaps), default=0)
    pixels = sum(b - a + 1 for a, b in gaps)
    return largest, largest / interior.size, len(gaps), pixels


def _sustained_edges(active: np.ndarray, start: int, config: TriggerConfig):
    if active.size < config.edge_window: return None, None
    occupancy = np.convolve(active.astype(np.int16), np.ones(config.edge_window, dtype=np.int16), mode="valid")
    valid = np.flatnonzero(occupancy >= config.edge_minimum_active)
    if valid.size == 0: return None, None
    return float(start + valid[0]), float(start + valid[-1] + config.edge_window - 1)


def _deep_core(drop: np.ndarray, start: int, config: TriggerConfig):
    if drop.size == 0: return None, None, None
    peak = float(np.percentile(drop, 95))
    if peak <= 0: return None, None, None
    candidates = [(a, b) for a, b in _runs(drop >= config.core_fraction * peak)
                  if b - a + 1 >= config.core_minimum_width]
    if not candidates: return None, None, None
    a, b = max(candidates, key=lambda item: float(drop[item[0]:item[1] + 1].sum()))
    a += start; b += start
    return a, b, (a + b) / 2.0


def _quantile(indices, weights, fraction):
    total = float(weights.sum())
    if total <= 0: return None
    cumulative = np.cumsum(weights)
    p = min(int(np.searchsorted(cumulative, fraction * total, side="left")), len(indices) - 1)
    if p == 0: return float(indices[0])
    w = float(weights[p])
    if w <= 0: return float(indices[p])
    ratio = float(np.clip((fraction * total - cumulative[p - 1]) / w, 0.0, 1.0))
    return float(indices[p - 1] + ratio)


def _build_region(frame, original, start, end, config, quality) -> RegionResult:
    pixels = frame[start:end + 1].astype(np.float64)
    indices = np.arange(start, end + 1, dtype=np.float64)
    drops = ADC_MAX - pixels
    total = float(drops.sum())
    source = original[start:end + 1]
    active_count = int(np.count_nonzero(source))
    coverage = active_count / len(source)
    largest_gap, gap_fraction, gap_count, gap_pixels = _gap_metrics(source)
    left, right = _sustained_edges(source, start, config)
    span = None if left is None or right is None else right - left + 1.0
    sustained = None if span is None else (left + right) / 2.0
    ql = _quantile(indices, drops, config.quantile_low)
    qh = _quantile(indices, drops, config.quantile_high)
    qc = None if ql is None or qh is None else (ql + qh) / 2.0
    disagreement = None if sustained is None or qc is None else abs(sustained - qc)
    core_start, core_end, core_center = _deep_core(drops, start, config)
    active_indices = np.flatnonzero(source) + start
    active_median = float(np.median(active_indices)) if active_indices.size else None
    reasons = []
    if active_count < config.minimum_active_pixels: reasons.append("insufficient_active_pixels")
    if coverage < config.minimum_active_coverage: reasons.append("insufficient_active_coverage")
    if largest_gap > config.maximum_internal_gap: reasons.append("excessive_internal_gap")
    if gap_fraction > config.maximum_internal_gap_fraction: reasons.append("excessive_internal_gap_fraction")
    if sustained is None: reasons.append("missing_sustained_edges")
    elif span < config.minimum_sustained_span: reasons.append("insufficient_sustained_span")
    if disagreement is not None and disagreement > config.maximum_center_disagreement: reasons.append("center_disagreement")
    valid = not reasons
    background_count = frame.size - len(pixels)
    rail = int(np.count_nonzero(frame[:start] >= quality.high_rail_threshold)) + int(np.count_nonzero(frame[end + 1:] >= quality.high_rail_threshold))
    bg_fraction = rail / background_count if background_count else 0.0
    diagnostic = sustained if sustained is not None else qc
    centroid = float(np.dot(indices, drops) / total) if total > 0 else (start + end) / 2.0
    return RegionResult(
        start=start, end=end, width=end-start+1, centroid=centroid,
        boundary_center=(start+end)/2.0, minimum_raw=int(pixels.min()),
        integrated_drop=total, shape_score=float(total * max(coverage, 1e-9) / max(1.0, 1.0 + (disagreement or 0.0))),
        measurement_quality=valid, background_rail_fraction=bg_fraction,
        interior_low_fraction=float(np.mean(pixels <= quality.low_plateau_threshold)),
        interior_p90=float(np.percentile(pixels, 90)), active_coverage=coverage,
        active_pixel_count=active_count, largest_internal_gap=largest_gap,
        internal_gap_fraction=gap_fraction, sustained_span=span,
        rejection_reason="none" if valid else reasons[0], sustained_left_edge=left,
        sustained_right_edge=right, sustained_edge_center=sustained,
        quantile_low_position=ql, quantile_high_position=qh, quantile_center=qc,
        deep_core_start=core_start, deep_core_end=core_end, deep_core_center=core_center,
        active_median_center=active_median, selected_center=sustained if valid else None,
        diagnostic_center=diagnostic, selected_center_method="sustained_edge" if valid else "none",
        center_disagreement=disagreement,
        center_confidence="high" if valid and disagreement is not None and disagreement <= config.maximum_center_disagreement/2 else "medium" if valid else "invalid",
        filled_gap_count=gap_count, filled_gap_pixels=gap_pixels,
    )


def detect_frame(frame: np.ndarray, config: TriggerConfig, quality: Optional[QualityConfig] = None) -> DetectionResult:
    quality = quality or QualityConfig()
    values = np.asarray(frame)
    if values.ndim != 1: raise ValueError("frame must be a one-dimensional pixel array")
    if values.size == 0: return DetectionResult()
    if not np.issubdtype(values.dtype, np.number): raise TypeError("frame must contain numeric pixel values")
    original = values < config.threshold
    cleaned = _fill_small_gaps(original, config.maximum_gap)
    regions = []
    for start, end in _runs(cleaned):
        width = end - start + 1
        if not config.minimum_width <= width <= config.maximum_width: continue
        integrated = float((ADC_MAX - values[start:end + 1].astype(np.float64)).sum())
        if integrated < config.minimum_integrated_drop: continue
        regions.append(_build_region(values, original, start, end, config, quality))
    valid = [r for r in regions if r.measurement_quality]
    selected = max(valid, key=lambda r: (r.active_pixel_count, r.active_coverage, -(r.center_disagreement or 0.0), r.integrated_drop), default=None)
    return DetectionResult(regions=regions, selected_region=selected, detected_candidate=bool(regions))
