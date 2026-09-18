#!/usr/bin/env python3

from __future__ import annotations

import json
import sys
from pathlib import Path

import numpy as np


EXPECTED_RANGES = [
    (1000, 1120),
    (1140, 1260),
    (1320, 1400),
]


def correlation(a: np.ndarray, b: np.ndarray) -> float:
    a = np.asarray(a, dtype=np.float64).ravel()
    b = np.asarray(b, dtype=np.float64).ravel()

    if a.size != b.size or a.size < 2:
        return 0.0

    if np.std(a) == 0 or np.std(b) == 0:
        return 0.0

    value = np.corrcoef(a, b)[0, 1]
    return float(value) if np.isfinite(value) else 0.0


def robust_noise(frames: np.ndarray) -> np.ndarray:
    median = np.median(frames, axis=0)
    return np.maximum(
        1.4826 * np.median(np.abs(frames - median), axis=0),
        1.0,
    )


def strongest_region(
    signal: np.ndarray,
    threshold: np.ndarray,
    minimum_width: int = 2,
    maximum_width: int = 256,
) -> dict | None:
    mask = signal > threshold
    padded = np.pad(mask.astype(np.int8), (1, 1))
    changes = np.diff(padded)

    starts = np.flatnonzero(changes == 1)
    ends = np.flatnonzero(changes == -1) - 1

    regions = [
        (int(start), int(end))
        for start, end in zip(starts, ends)
        if minimum_width <= end - start + 1 <= maximum_width
    ]

    if not regions:
        return None

    start, end = max(
        regions,
        key=lambda region: float(
            signal[region[0] : region[1] + 1].sum()
        ),
    )

    region_signal = signal[start : end + 1]
    peak = start + int(np.argmax(region_signal))
    total = float(region_signal.sum())

    centroid = (
        float(
            np.dot(
                np.arange(start, end + 1),
                region_signal,
            )
            / total
        )
        if total > 0
        else None
    )

    return {
        "start": start,
        "end": end,
        "width": end - start + 1,
        "peak": peak,
        "peak_signal": float(signal[peak]),
        "integrated_signal": total,
        "centroid": centroid,
    }


def range_summary(
    positive: np.ndarray,
    negative: np.ndarray,
    noise: np.ndarray,
    start: int,
    end: int,
) -> dict:
    stop = min(end + 1, positive.size)
    start = max(0, start)

    positive_slice = positive[start:stop]
    negative_slice = negative[start:stop]
    noise_slice = noise[start:stop]

    positive_peak_index = start + int(np.argmax(positive_slice))
    negative_peak_index = start + int(np.argmax(negative_slice))

    return {
        "start": start,
        "end": stop - 1,
        "positive_peak_pixel": positive_peak_index,
        "positive_peak_signal": float(positive[positive_peak_index]),
        "positive_peak_snr": float(
            positive[positive_peak_index]
            / max(noise[positive_peak_index], 1.0)
        ),
        "negative_peak_pixel": negative_peak_index,
        "negative_peak_signal": float(negative[negative_peak_index]),
        "negative_peak_snr": float(
            negative[negative_peak_index]
            / max(noise[negative_peak_index], 1.0)
        ),
        "positive_integrated_signal": float(positive_slice.sum()),
        "negative_integrated_signal": float(negative_slice.sum()),
    }


def analyze_pair(
    off: np.ndarray,
    on: np.ndarray,
    rail_margin: int = 32,
) -> dict:
    off = np.asarray(off, dtype=np.float64)
    on = np.asarray(on, dtype=np.float64)

    if off.shape != on.shape:
        raise ValueError(
            f"OFF shape {off.shape} differs from ON shape {on.shape}"
        )

    if off.ndim != 2:
        raise ValueError(f"Expected 2D captures, got shape {off.shape}")

    off_median = np.median(off, axis=0)
    on_median = np.median(on, axis=0)

    noise = robust_noise(off)
    signed_delta = on_median - off_median

    positive = np.maximum(signed_delta, 0.0)
    negative = np.maximum(-signed_delta, 0.0)

    threshold = np.maximum(noise * 3.0, 20.0)

    positive_region = strongest_region(
        positive,
        threshold,
    )
    negative_region = strongest_region(
        negative,
        threshold,
    )

    off_frame_correlations = [
        correlation(off[index - 1], off[index])
        for index in range(1, off.shape[0])
    ]

    on_frame_correlations = [
        correlation(on[index - 1], on[index])
        for index in range(1, on.shape[0])
    ]

    difference_frames = on - off
    difference_median = np.median(difference_frames, axis=0)

    return {
        "shape": list(off.shape),
        "off": {
            "minimum": float(off.min()),
            "maximum": float(off.max()),
            "mean": float(off.mean()),
            "standard_deviation": float(off.std()),
            "median_frame_correlation": float(
                np.median(off_frame_correlations)
            ),
            "low_clip_fraction": float(
                np.mean(off <= rail_margin)
            ),
            "high_clip_fraction": float(
                np.mean(off >= 65535 - rail_margin)
            ),
        },
        "on": {
            "minimum": float(on.min()),
            "maximum": float(on.max()),
            "mean": float(on.mean()),
            "standard_deviation": float(on.std()),
            "median_frame_correlation": float(
                np.median(on_frame_correlations)
            ),
            "low_clip_fraction": float(
                np.mean(on <= rail_margin)
            ),
            "high_clip_fraction": float(
                np.mean(on >= 65535 - rail_margin)
            ),
        },
        "off_on_profile_correlation": correlation(
            off_median,
            on_median,
        ),
        "temporal_noise": {
            "median": float(np.median(noise)),
            "p90": float(np.percentile(noise, 90)),
            "p95": float(np.percentile(noise, 95)),
            "p99": float(np.percentile(noise, 99)),
            "maximum": float(noise.max()),
        },
        "signed_difference": {
            "minimum": float(signed_delta.min()),
            "maximum": float(signed_delta.max()),
            "mean": float(signed_delta.mean()),
            "median": float(np.median(signed_delta)),
            "standard_deviation": float(signed_delta.std()),
        },
        "paired_frame_difference": {
            "minimum": float(difference_frames.min()),
            "maximum": float(difference_frames.max()),
            "mean": float(difference_frames.mean()),
            "median_profile_minimum": float(
                difference_median.min()
            ),
            "median_profile_maximum": float(
                difference_median.max()
            ),
        },
        "positive_region": positive_region,
        "negative_region": negative_region,
        "expected_ranges": [
            range_summary(
                positive,
                negative,
                noise,
                start,
                end,
            )
            for start, end in EXPECTED_RANGES
        ],
    }


def main() -> int:
    if len(sys.argv) != 2:
        print(
            "Usage: python /tmp/analyze_paired_session.py "
            "<laser-session-directory>",
            file=sys.stderr,
        )
        return 2

    session = Path(sys.argv[1]).expanduser().resolve()
    captures = session / "captures"

    if not captures.is_dir():
        raise FileNotFoundError(
            f"Capture directory not found: {captures}"
        )

    off_files = sorted(captures.glob("*_off.npy"))

    if not off_files:
        raise FileNotFoundError(
            f"No *_off.npy files found in {captures}"
        )

    report = {
        "session_directory": str(session),
        "candidate_count": len(off_files),
        "candidates": [],
    }

    for off_path in off_files:
        on_path = off_path.with_name(
            off_path.name.replace("_off.npy", "_on.npy")
        )

        if not on_path.exists():
            report["candidates"].append(
                {
                    "off_file": off_path.name,
                    "error": f"Matching ON file missing: {on_path.name}",
                }
            )
            continue

        off = np.load(off_path, allow_pickle=False)
        on = np.load(on_path, allow_pickle=False)

        report["candidates"].append(
            {
                "off_file": off_path.name,
                "on_file": on_path.name,
                "analysis": analyze_pair(off, on),
            }
        )

    output = session / "paired_diagnostic_report.json"
    output.write_text(
        json.dumps(report, indent=2, allow_nan=False),
        encoding="utf-8",
    )

    print(output)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
PY