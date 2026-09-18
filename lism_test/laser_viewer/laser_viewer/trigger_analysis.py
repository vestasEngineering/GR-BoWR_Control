from __future__ import annotations

from dataclasses import asdict
import itertools
import time
import numpy as np

from .downward_detector import ADC_MAX, QualityConfig, TriggerConfig


def _runs(mask: np.ndarray) -> tuple[np.ndarray, np.ndarray]:
    changes = np.diff(np.pad(mask.astype(np.int8), (1, 1)))
    return np.flatnonzero(changes == 1), np.flatnonzero(changes == -1) - 1


def _close_small_gaps(mask: np.ndarray, maximum_gap: int) -> np.ndarray:
    if maximum_gap == 0:
        return mask
    closed = mask.copy()
    starts, ends = _runs(~closed)
    lengths = ends - starts + 1
    for start, end in zip(starts[(lengths <= maximum_gap)], ends[(lengths <= maximum_gap)]):
        if start > 0 and end < len(closed) - 1:
            closed[start:end + 1] = True
    return closed


def _extract_regions(frame: np.ndarray, threshold: int, maximum_gap: int, quality: QualityConfig) -> list[tuple]:
    active = frame < threshold
    grouped = _close_small_gaps(active, maximum_gap)
    starts, ends = _runs(grouped)
    output = []
    for start_value, end_value in zip(starts, ends):
        start, end = int(start_value), int(end_value)
        width = end - start + 1
        pixels = frame[start:end + 1].astype(np.float64, copy=False)
        drops = ADC_MAX - pixels
        integrated = float(drops.sum())
        source_active = active[start:end + 1]
        gap_starts, gap_ends = _runs(~source_active)
        largest_gap = int(np.max(gap_ends - gap_starts + 1)) if gap_starts.size else 0
        edge = min(quality.edge_exclusion, max(0, (width - 1) // 2))
        interior = pixels[edge:width - edge] if width > 2 * edge else pixels
        outside_high = int(np.count_nonzero(frame[:start] >= quality.high_rail_threshold))
        outside_high += int(np.count_nonzero(frame[end + 1:] >= quality.high_rail_threshold))
        outside_count = len(frame) - width
        background = outside_high / outside_count if outside_count else 1.0
        interior_low = float(np.mean(interior <= quality.low_plateau_threshold))
        coverage = float(np.mean(source_active))
        gap_score = 1.0 if largest_gap == 0 else max(0.0, 1.0 - largest_gap / (quality.maximum_internal_gap + 1))
        score = 0.30 * background + 0.30 * interior_low + 0.20 * coverage + 0.20 * gap_score
        good = background >= quality.minimum_background_rail_fraction and interior_low >= quality.minimum_interior_low_fraction and largest_gap <= quality.maximum_internal_gap and score >= quality.minimum_shape_score
        centroid = float(np.dot(np.arange(start, end + 1), drops) / integrated) if integrated else (start + end) / 2.0
        output.append((width, integrated, centroid, (start + end) / 2.0, float(score), bool(good)))
    return output


class _Accumulator:
    __slots__ = ("config", "basic", "good", "shape", "centroid", "boundary")
    def __init__(self, config: TriggerConfig):
        self.config = config
        self.basic = 0
        self.good = 0
        self.shape = []
        self.centroid = []
        self.boundary = []

    def observe(self, regions: list[tuple]) -> None:
        candidates = [region for region in regions if self.config.minimum_width <= region[0] <= self.config.maximum_width and region[1] >= self.config.minimum_integrated_drop]
        if not candidates:
            return
        selected = max(candidates, key=lambda region: region[1])
        self.basic += 1
        if selected[5]:
            self.good += 1
            self.centroid.append(selected[2])
            self.boundary.append(selected[3])
            self.shape.append(selected[4])

    def summary(self, total: int) -> dict:
        return {
            "configuration": asdict(self.config),
            "configuration_id": self.config.configuration_id,
            "total_frames": total,
            "basic_trigger_frames": self.basic,
            "good_frames": self.good,
            "basic_trigger_fraction": self.basic / total if total else 0.0,
            "good_frame_yield": self.good / total if total else 0.0,
            "good_trigger_precision": self.good / self.basic if self.basic else 0.0,
            "median_shape_score": float(np.median(self.shape)) if self.shape else None,
            "centroid_std": float(np.std(self.centroid)) if self.centroid else None,
            "boundary_center_std": float(np.std(self.boundary)) if self.boundary else None,
        }


def analyze_dataset_streaming(frames, base: dict, grid: dict, quality=None, progress=None, dataset_name="DATA") -> dict[str, dict]:
    quality = quality or QualityConfig()
    pairs = list(itertools.product(grid["threshold"], grid["maximum_gap"]))
    total_frames = len(frames)
    summaries = {}
    started = time.monotonic()
    for pair_index, (threshold, maximum_gap) in enumerate(pairs, 1):
        accumulators = []
        for minimum_width, integrated_drop in itertools.product(grid["minimum_width"], grid["minimum_integrated_drop"]):
            values = dict(base, threshold=int(threshold), maximum_gap=int(maximum_gap), minimum_width=int(minimum_width), minimum_integrated_drop=float(integrated_drop))
            accumulators.append(_Accumulator(TriggerConfig.from_mapping(values)))
        for frame_index, frame in enumerate(frames, 1):
            regions = _extract_regions(np.asarray(frame), int(threshold), int(maximum_gap), quality)
            for accumulator in accumulators:
                accumulator.observe(regions)
            if progress and (frame_index == total_frames or frame_index % 5000 == 0):
                progress(dataset_name, pair_index, len(pairs), frame_index, total_frames, started)
        for accumulator in accumulators:
            summary = accumulator.summary(total_frames)
            summaries[summary["configuration_id"]] = summary
    return summaries


def compare_off_on_fast(off_frames, on_frames, base, grid, quality=None, max_false_fraction=0.001, progress=None, result_callback=None):
    # Preserve the prior positional API where the fifth argument was max_false_fraction.
    if isinstance(quality, (int, float, np.integer, np.floating)):
        max_false_fraction = float(quality)
        quality = None
    off = analyze_dataset_streaming(off_frames, base, grid, quality, progress, "OFF")
    on = analyze_dataset_streaming(on_frames, base, grid, quality, progress, "ON")
    rows = []
    for configuration_id, on_summary in on.items():
        off_summary = off[configuration_id]
        row = {"accepted": off_summary["basic_trigger_fraction"] <= max_false_fraction, "false_trigger_fraction": off_summary["basic_trigger_fraction"], "off": off_summary, "on": on_summary}
        rows.append(row)
        if result_callback:
            result_callback(row)
    return sorted(rows, key=lambda row: (row["accepted"], row["on"]["good_frame_yield"], row["on"]["good_trigger_precision"], row["on"]["median_shape_score"] or 0.0), reverse=True)


def compare_off_on(off_frames, on_frames, base, grid, quality=None, max_false_fraction=0.001):
    return compare_off_on_fast(off_frames, on_frames, base, grid, quality, max_false_fraction)
