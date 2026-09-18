from __future__ import annotations

import argparse
import csv
import json
from dataclasses import asdict
from pathlib import Path

import numpy as np

from .downward_detector import TriggerConfig, detect_frame


METHODS = (
    "centroid",
    "boundary_center",
    "sustained_edge_center",
    "quantile_center",
    "deep_core_center",
    "active_median_center",
    "selected_center",
)


def summarize(values: list[float]) -> dict:
    if not values:
        return {"count": 0}
    array = np.asarray(values, dtype=np.float64)
    median = float(np.median(array))
    return {
        "count": int(array.size),
        "mean": float(array.mean()),
        "median": median,
        "standard_deviation": float(array.std()),
        "median_absolute_deviation": float(np.median(np.abs(array - median))),
        "minimum": float(array.min()),
        "maximum": float(array.max()),
        "p05": float(np.percentile(array, 5)),
        "p95": float(np.percentile(array, 95)),
        "peak_to_peak": float(np.ptp(array)),
    }


def evaluate(frames: np.ndarray, config: TriggerConfig) -> tuple[list[dict], dict]:
    if frames.ndim == 1:
        frames = frames[np.newaxis, :]
    if frames.ndim != 2:
        raise ValueError("input must have shape [frame, pixel] or [pixel]")
    rows = []
    method_values = {name: [] for name in METHODS}
    for frame_number, frame in enumerate(frames, start=1):
        result = detect_frame(frame, config)
        region = result.selected_region
        row = {"frame_number": frame_number, "detected": region is not None}
        if region is not None:
            row.update(asdict(region))
            for name in METHODS:
                value = getattr(region, name)
                if value is not None:
                    method_values[name].append(float(value))
        rows.append(row)
    summary = {
        "frame_count": int(frames.shape[0]),
        "detected_count": sum(bool(row["detected"]) for row in rows),
        "configuration": asdict(config),
        "configuration_id": config.configuration_id,
        "methods": {name: summarize(values) for name, values in method_values.items()},
    }
    return rows, summary


def main(argv=None):
    parser = argparse.ArgumentParser(description="Evaluate LISM centroid methods against saved NumPy frames")
    parser.add_argument("frames", help=".npy file containing [frame, pixel] or [pixel]")
    parser.add_argument("--output", default="centroid_evaluation")
    parser.add_argument("--threshold", type=int, default=60000)
    parser.add_argument("--minimum-width", type=int, default=2)
    parser.add_argument("--maximum-width", type=int, default=512)
    parser.add_argument("--minimum-integrated-drop", type=float, default=10000)
    parser.add_argument("--maximum-gap", type=int, default=3)
    args = parser.parse_args(argv)
    config = TriggerConfig(
        threshold=args.threshold,
        minimum_width=args.minimum_width,
        maximum_width=args.maximum_width,
        minimum_integrated_drop=args.minimum_integrated_drop,
        maximum_gap=args.maximum_gap,
    )
    frames = np.load(args.frames, allow_pickle=False)
    rows, summary = evaluate(frames, config)
    output = Path(args.output)
    output.mkdir(parents=True, exist_ok=True)
    (output / "summary.json").write_text(json.dumps(summary, indent=2, allow_nan=False), encoding="utf-8")
    fieldnames = sorted({key for row in rows for key in row})
    with (output / "frame_results.csv").open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(rows)
    print(json.dumps(summary, indent=2, allow_nan=False))


if __name__ == "__main__":
    main()
