from __future__ import annotations

import argparse
import csv
import json
import math
from dataclasses import asdict, dataclass
from pathlib import Path

import numpy as np

ADC_MAX = 65535.0


@dataclass(frozen=True)
class TestConfig:
    threshold: int = 60000
    minimum_width: int = 2
    maximum_width: int = 512
    minimum_integrated_drop: float = 10000.0
    maximum_gap: int = 1
    edge_trim: int = 4
    winsor_percent: float = 5.0
    minimum_frames_per_position: int = 10


@dataclass(frozen=True)
class Region:
    start: int
    end: int
    width: int
    integrated_drop: float


def _runs(mask: np.ndarray) -> list[tuple[int, int]]:
    changes = np.diff(np.pad(mask.astype(np.int8), (1, 1)))
    starts = np.flatnonzero(changes == 1)
    ends = np.flatnonzero(changes == -1) - 1
    return [(int(start), int(end)) for start, end in zip(starts, ends)]


def _close_small_gaps(mask: np.ndarray, maximum_gap: int) -> np.ndarray:
    if maximum_gap <= 0:
        return mask
    closed = mask.copy()
    for start, end in _runs(~closed):
        if start > 0 and end < len(closed) - 1 and end - start + 1 <= maximum_gap:
            closed[start:end + 1] = True
    return closed


def select_region(frame: np.ndarray, config: TestConfig) -> Region | None:
    active = np.asarray(frame) < config.threshold
    grouped = _close_small_gaps(active, config.maximum_gap)
    candidates = []
    for start, end in _runs(grouped):
        width = end - start + 1
        if not config.minimum_width <= width <= config.maximum_width:
            continue
        integrated = float(np.sum(ADC_MAX - np.asarray(frame[start:end + 1], dtype=np.float64)))
        if integrated >= config.minimum_integrated_drop:
            candidates.append(Region(start, end, width, integrated))
    return max(candidates, key=lambda item: item.integrated_drop) if candidates else None


def _weighted_position(indices: np.ndarray, weights: np.ndarray) -> float | None:
    total = float(np.sum(weights))
    return float(np.dot(indices, weights) / total) if total > 0 else None


def calculate_methods(frame: np.ndarray, region: Region, config: TestConfig) -> dict[str, float | None]:
    values = np.asarray(frame[region.start:region.end + 1], dtype=np.float64)
    indices = np.arange(region.start, region.end + 1, dtype=np.float64)
    rail_drop = np.maximum(ADC_MAX - values, 0.0)
    threshold_drop = np.maximum(config.threshold - values, 0.0)

    trim = min(config.edge_trim, max(0, (region.width - 1) // 2))
    trimmed_values = values[trim:region.width - trim] if region.width > 2 * trim else values
    trimmed_indices = indices[trim:region.width - trim] if region.width > 2 * trim else indices
    trimmed_drop = np.maximum(ADC_MAX - trimmed_values, 0.0)

    lower = float(np.percentile(rail_drop, config.winsor_percent))
    upper = float(np.percentile(rail_drop, 100.0 - config.winsor_percent))
    winsor_drop = np.clip(rail_drop, lower, upper)

    active_indices = indices[values < config.threshold]
    methods = {
        "rail_weighted": _weighted_position(indices, rail_drop),
        "threshold_weighted": _weighted_position(indices, threshold_drop),
        "boundary_center": (region.start + region.end) / 2.0,
        "active_median": float(np.median(active_indices)) if active_indices.size else None,
        "trimmed_rail_weighted": _weighted_position(trimmed_indices, trimmed_drop),
        "winsorized_rail_weighted": _weighted_position(indices, winsor_drop),
    }
    return methods


def robust_statistics(values: list[float]) -> dict:
    array = np.asarray(values, dtype=np.float64)
    median = float(np.median(array))
    mad = float(1.4826 * np.median(np.abs(array - median)))
    return {
        "count": int(array.size),
        "mean": float(np.mean(array)),
        "standard_deviation": float(np.std(array)),
        "median": median,
        "robust_sigma_mad": mad,
        "minimum": float(np.min(array)),
        "maximum": float(np.max(array)),
        "peak_to_peak": float(np.ptp(array)),
        "p05": float(np.percentile(array, 5)),
        "p95": float(np.percentile(array, 95)),
    }


def analyze_position(path: Path, label: str, config: TestConfig) -> tuple[dict, list[dict]]:
    frames = np.load(path, mmap_mode="r", allow_pickle=False)
    method_values: dict[str, list[float]] = {}
    rows = []
    triggered = 0
    for frame_index, frame in enumerate(frames):
        region = select_region(frame, config)
        if region is None:
            continue
        triggered += 1
        methods = calculate_methods(frame, region, config)
        row = {"position": label, "frame_index": frame_index, "region_start": region.start, "region_end": region.end, "region_width": region.width, "integrated_drop": region.integrated_drop}
        for name, value in methods.items():
            row[name] = value
            if value is not None:
                method_values.setdefault(name, []).append(value)
        rows.append(row)
    summary = {
        "label": label,
        "path": str(path),
        "total_frames": int(len(frames)),
        "triggered_frames": triggered,
        "triggered_fraction": triggered / len(frames) if len(frames) else 0.0,
        "methods": {name: robust_statistics(values) for name, values in method_values.items()},
    }
    return summary, rows


def method_ranking(position_summaries: list[dict], ordered_labels: list[str], return_pair: tuple[str, str] | None, minimum_frames: int) -> list[dict]:
    method_names = sorted(set.intersection(*(set(position["methods"]) for position in position_summaries)))
    by_label = {position["label"]: position for position in position_summaries}
    rankings = []
    for method in method_names:
        stats = [by_label[label]["methods"][method] for label in ordered_labels]
        counts = [item["count"] for item in stats]
        means = [item["mean"] for item in stats]
        robust_sigmas = [item["robust_sigma_mad"] for item in stats]
        standard_deviations = [item["standard_deviation"] for item in stats]
        deltas = np.diff(means)
        increasing = bool(np.all(deltas > 0))
        decreasing = bool(np.all(deltas < 0))
        monotonic = increasing or decreasing
        direction = 1.0 if increasing else -1.0 if decreasing else 0.0
        minimum_separation = float(np.min(np.abs(deltas))) if len(deltas) else 0.0
        worst_robust_sigma = float(max(robust_sigmas))
        worst_standard_deviation = float(max(standard_deviations))
        separation_to_noise = minimum_separation / max(worst_robust_sigma, 1e-9)
        return_error = None
        if return_pair:
            return_error = abs(by_label[return_pair[0]]["methods"][method]["mean"] - by_label[return_pair[1]]["methods"][method]["mean"])
        eligible = min(counts) >= minimum_frames and monotonic
        score = separation_to_noise
        if return_error is not None:
            score /= 1.0 + return_error
        if not eligible:
            score = -1.0
        rankings.append({
            "method": method,
            "eligible": eligible,
            "counts": counts,
            "position_means": dict(zip(ordered_labels, means)),
            "monotonic": monotonic,
            "direction": direction,
            "minimum_adjacent_separation_pixels": minimum_separation,
            "worst_robust_sigma_pixels": worst_robust_sigma,
            "worst_standard_deviation_pixels": worst_standard_deviation,
            "separation_to_noise": separation_to_noise,
            "return_error_pixels": return_error,
            "score": score,
        })
    return sorted(rankings, key=lambda item: (item["eligible"], item["score"], -item["worst_robust_sigma_pixels"]), reverse=True)


def parse_dataset(value: str) -> tuple[str, Path]:
    if "=" not in value:
        raise argparse.ArgumentTypeError("Dataset must use LABEL=PATH")
    label, path = value.split("=", 1)
    if not label.strip():
        raise argparse.ArgumentTypeError("Dataset label cannot be empty")
    return label.strip(), Path(path).expanduser().resolve()


def main(argv=None):
    parser = argparse.ArgumentParser(description="Compare LISM centroid calculation methods on fixed-position captures")
    parser.add_argument("--dataset", action="append", type=parse_dataset, required=True, help="Repeat as LABEL=PATH; order defines expected physical order")
    parser.add_argument("--output", required=True)
    parser.add_argument("--return-pair", help="Two labels such as CENTER,RETURN_CENTER")
    parser.add_argument("--threshold", type=int, default=60000)
    parser.add_argument("--minimum-width", type=int, default=2)
    parser.add_argument("--maximum-width", type=int, default=512)
    parser.add_argument("--minimum-integrated-drop", type=float, default=10000)
    parser.add_argument("--maximum-gap", type=int, default=1)
    parser.add_argument("--edge-trim", type=int, default=4)
    parser.add_argument("--winsor-percent", type=float, default=5.0)
    parser.add_argument("--minimum-frames-per-position", type=int, default=10)
    args = parser.parse_args(argv)

    config = TestConfig(args.threshold, args.minimum_width, args.maximum_width, args.minimum_integrated_drop, args.maximum_gap, args.edge_trim, args.winsor_percent, args.minimum_frames_per_position)
    output = Path(args.output).expanduser().resolve()
    output.mkdir(parents=True, exist_ok=False)
    summaries = []
    event_rows = []
    for label, path in args.dataset:
        if not path.exists():
            raise FileNotFoundError(path)
        print(f"Analyzing {label}: {path}", flush=True)
        summary, rows = analyze_position(path, label, config)
        summaries.append(summary)
        event_rows.extend(rows)
        print(f"  {summary['triggered_frames']:,}/{summary['total_frames']:,} frames triggered", flush=True)

    labels = [label for label, _ in args.dataset]
    return_pair = tuple(item.strip() for item in args.return_pair.split(",")) if args.return_pair else None
    if return_pair and (len(return_pair) != 2 or any(label not in labels for label in return_pair)):
        raise ValueError("return-pair must contain two existing dataset labels")
    ranking = method_ranking(summaries, labels, return_pair, config.minimum_frames_per_position)
    report = {"configuration": asdict(config), "ordered_positions": labels, "return_pair": return_pair, "position_summaries": summaries, "method_ranking": ranking, "recommended_method": next((item for item in ranking if item["eligible"]), None)}
    (output / "centroid_method_report.json").write_text(json.dumps(report, indent=2, allow_nan=False), encoding="utf-8")
    fields = list(event_rows[0]) if event_rows else ["position", "frame_index"]
    with (output / "centroid_events.csv").open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=fields)
        writer.writeheader()
        writer.writerows(event_rows)
    with (output / "centroid_method_summary.csv").open("w", newline="", encoding="utf-8") as handle:
        fields = ["method", "eligible", "monotonic", "direction", "minimum_adjacent_separation_pixels", "worst_robust_sigma_pixels", "worst_standard_deviation_pixels", "separation_to_noise", "return_error_pixels", "score"]
        writer = csv.DictWriter(handle, fieldnames=fields)
        writer.writeheader()
        for item in ranking:
            writer.writerow({name: item[name] for name in fields})
    print(json.dumps(report["recommended_method"], indent=2, allow_nan=False))
    print(f"Results: {output}")


if __name__ == "__main__":
    main()
