from __future__ import annotations

import argparse
import csv
import json
import math
import os
import time
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

import numpy as np

from .camera import LismCamera
from .config import Config


@dataclass(frozen=True)
class FrequencyPeak:
    frequency_hz: float
    power: float
    relative_power: float


def robust_noise(frames: np.ndarray) -> np.ndarray:
    median = np.median(frames, axis=0)
    return np.maximum(
        1.4826 * np.median(np.abs(frames - median), axis=0),
        1.0,
    )


def frame_signals(frames: np.ndarray, baseline: np.ndarray, region_count: int = 16) -> dict[str, np.ndarray]:
    baseline_median = np.median(baseline, axis=0)
    baseline_noise = robust_noise(baseline)
    absolute_delta = np.abs(frames - baseline_median)
    threshold = np.maximum(3.0 * baseline_noise, 20.0)

    signals: dict[str, np.ndarray] = {
        "frame_mean": np.mean(frames, axis=1),
        "frame_std": np.std(frames, axis=1),
        "frame_max": np.max(frames, axis=1),
        "frame_min": np.min(frames, axis=1),
        "absolute_delta_sum": np.sum(absolute_delta, axis=1),
        "absolute_delta_p99": np.percentile(absolute_delta, 99, axis=1),
        "threshold_hit_count": np.sum(absolute_delta > threshold, axis=1).astype(np.float64),
    }

    pixel_regions = np.array_split(np.arange(frames.shape[1]), region_count)
    for index, pixels in enumerate(pixel_regions):
        region_delta = absolute_delta[:, pixels]
        signals[f"region_{index:02d}_delta_sum"] = np.sum(region_delta, axis=1)
        signals[f"region_{index:02d}_delta_max"] = np.max(region_delta, axis=1)

    return signals


def resample_signal(timestamps: np.ndarray, values: np.ndarray) -> tuple[np.ndarray, np.ndarray, float]:
    timestamps = np.asarray(timestamps, dtype=np.float64)
    values = np.asarray(values, dtype=np.float64)
    if timestamps.size < 8 or timestamps.size != values.size:
        raise ValueError("At least eight timestamped samples are required")
    relative = timestamps - timestamps[0]
    intervals = np.diff(relative)
    median_interval = float(np.median(intervals))
    if median_interval <= 0:
        raise ValueError("Timestamps must increase")
    sample_rate = 1.0 / median_interval
    uniform_time = np.arange(relative[0], relative[-1], median_interval)
    uniform_values = np.interp(uniform_time, relative, values)
    return uniform_time, uniform_values, sample_rate


def spectral_peaks(
    timestamps: np.ndarray,
    values: np.ndarray,
    minimum_frequency: float,
    maximum_frequency: float | None,
    peak_count: int = 8,
) -> dict:
    uniform_time, uniform_values, sample_rate = resample_signal(timestamps, values)
    centered = uniform_values - np.mean(uniform_values)
    if np.std(centered) == 0:
        return {
            "sample_rate_hz": sample_rate,
            "nyquist_hz": sample_rate / 2.0,
            "frequency_resolution_hz": sample_rate / len(centered),
            "peaks": [],
            "autocorrelation_frequency_hz": None,
        }

    windowed = centered * np.hanning(len(centered))
    spectrum = np.fft.rfft(windowed)
    frequencies = np.fft.rfftfreq(len(windowed), d=1.0 / sample_rate)
    power = np.abs(spectrum) ** 2
    upper = min(maximum_frequency or sample_rate / 2.0, sample_rate / 2.0)
    mask = (frequencies >= minimum_frequency) & (frequencies <= upper)
    indexes = np.flatnonzero(mask)
    if indexes.size == 0:
        selected: list[int] = []
    else:
        local_candidates = []
        for index in indexes:
            left = power[index - 1] if index > 0 else -1.0
            right = power[index + 1] if index + 1 < power.size else -1.0
            if power[index] >= left and power[index] >= right:
                local_candidates.append(int(index))
        selected = sorted(local_candidates, key=lambda index: float(power[index]), reverse=True)[:peak_count]

    max_power = max((float(power[index]) for index in selected), default=1.0)
    peaks = [
        FrequencyPeak(
            frequency_hz=float(frequencies[index]),
            power=float(power[index]),
            relative_power=float(power[index] / max_power),
        ).__dict__
        for index in selected
    ]

    autocorrelation = np.correlate(centered, centered, mode="full")[len(centered) - 1 :]
    autocorrelation[0] = 0.0
    minimum_lag = max(1, int(sample_rate / upper))
    maximum_lag = min(len(autocorrelation) - 1, int(sample_rate / minimum_frequency))
    autocorrelation_frequency = None
    if maximum_lag > minimum_lag:
        segment = autocorrelation[minimum_lag : maximum_lag + 1]
        lag = minimum_lag + int(np.argmax(segment))
        if lag > 0:
            autocorrelation_frequency = float(sample_rate / lag)

    return {
        "sample_rate_hz": sample_rate,
        "nyquist_hz": sample_rate / 2.0,
        "frequency_resolution_hz": sample_rate / len(centered),
        "peaks": peaks,
        "autocorrelation_frequency_hz": autocorrelation_frequency,
        "spectrum_frequencies_hz": frequencies[mask].tolist(),
        "spectrum_power": power[mask].tolist(),
    }


def analyze_recording(
    timestamps: np.ndarray,
    frames: np.ndarray,
    baseline: np.ndarray,
    minimum_frequency: float,
    maximum_frequency: float | None,
) -> dict:
    intervals = np.diff(timestamps)
    signals = frame_signals(frames, baseline)
    analyses = {
        name: spectral_peaks(timestamps, values, minimum_frequency, maximum_frequency)
        for name, values in signals.items()
    }

    ranked = []
    for name, analysis in analyses.items():
        if not analysis["peaks"]:
            continue
        peak = analysis["peaks"][0]
        values = signals[name]
        normalized_strength = float(np.std(values) / max(abs(np.mean(values)), 1.0))
        ranked.append({
            "signal": name,
            "frequency_hz": peak["frequency_hz"],
            "power": peak["power"],
            "normalized_signal_spread": normalized_strength,
            "autocorrelation_frequency_hz": analysis["autocorrelation_frequency_hz"],
        })
    ranked.sort(key=lambda item: item["power"], reverse=True)

    return {
        "frame_count": int(frames.shape[0]),
        "duration_seconds": float(timestamps[-1] - timestamps[0]),
        "measured_frame_rate_hz": float(1.0 / np.median(intervals)),
        "mean_frame_interval_ms": float(np.mean(intervals) * 1000.0),
        "median_frame_interval_ms": float(np.median(intervals) * 1000.0),
        "frame_interval_jitter_std_ms": float(np.std(intervals) * 1000.0),
        "frame_interval_p95_ms": float(np.percentile(intervals, 95) * 1000.0),
        "nyquist_hz": float(0.5 / np.median(intervals)),
        "top_signal_results": ranked[:12],
        "signal_analyses": analyses,
        "signals": {name: values.tolist() for name, values in signals.items()},
    }


def capture_for_duration(camera: LismCamera, duration_seconds: float) -> tuple[np.ndarray, np.ndarray]:
    timestamps = []
    frames = []
    deadline = time.monotonic() + duration_seconds
    while time.monotonic() < deadline:
        frame = camera.read_frame()
        timestamps.append(time.monotonic_ns() * 1e-9)
        frames.append(frame)
    if len(frames) < 8:
        raise RuntimeError(f"Only {len(frames)} frames captured; at least eight are required")
    return np.asarray(timestamps, dtype=np.float64), np.stack(frames)


def write_csv(path: Path, timestamps: np.ndarray, signals: dict[str, list[float]]) -> None:
    names = list(signals)
    relative = timestamps - timestamps[0]
    with path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.writer(handle)
        writer.writerow(["frame", "time_seconds", *names])
        for index in range(len(timestamps)):
            writer.writerow([index, float(relative[index]), *[signals[name][index] for name in names]])


def atomic_json(path: Path, value) -> None:
    temporary = path.with_suffix(path.suffix + ".tmp")
    with temporary.open("w", encoding="utf-8") as handle:
        json.dump(value, handle, indent=2, allow_nan=False)
        handle.flush()
        os.fsync(handle.fileno())
    os.replace(temporary, path)


def main(argv=None) -> None:
    parser = argparse.ArgumentParser(description="Measure the observed optical crossing frequency of a scanning laser")
    parser.add_argument("--config", default="config/default.json")
    parser.add_argument("--bias", type=int, default=256)
    parser.add_argument("--gain", type=int, default=4)
    parser.add_argument("--st-high", type=int, default=9900)
    parser.add_argument("--st-low", type=int, default=100)
    parser.add_argument("--edge-delay", type=int, default=88)
    parser.add_argument("--off-duration", type=float, default=10.0)
    parser.add_argument("--scan-duration", type=float, default=30.0)
    parser.add_argument("--minimum-frequency", type=float, default=0.5)
    parser.add_argument("--maximum-frequency", type=float)
    parser.add_argument("--label", default="lap_scan")
    parser.add_argument("--output-directory", default="scan_rate_sessions")
    args = parser.parse_args(argv)

    if args.off_duration <= 0 or args.scan_duration <= 0:
        parser.error("capture durations must be positive")
    safe_label = "".join(character for character in args.label if character.isalnum() or character in "-_") or "scan"
    output = Path(args.output_directory).expanduser().resolve() / f"{datetime.now().strftime('%Y%m%d_%H%M%S_%f')}_{safe_label}"
    output.mkdir(parents=True, exist_ok=False)

    config, base = Config.load(args.config)
    candidate = {
        "bias": args.bias,
        "gain": args.gain,
        "st_high": args.st_high,
        "st_low": args.st_low,
        "edge_delay": args.edge_delay,
    }

    with LismCamera(config, base) as camera:
        original = camera.configuration()
        atomic_json(output / "original_configuration.json", original)
        primary_error = None
        try:
            camera.apply_temporary_configuration(
                st_high=args.st_high,
                st_low=args.st_low,
                edge_delay=args.edge_delay,
                adc_gain=args.gain,
                adc_bias=args.bias,
            )
            input("Disable the LAP scan and laser emission, wait for stability, then press Enter: ")
            camera.start()
            for _ in range(32):
                camera.read_frame()
            off_timestamps, off_frames = capture_for_duration(camera, args.off_duration)

            input("Enable the normal LAP scan, wait for stable operation, then press Enter: ")
            scan_timestamps, scan_frames = capture_for_duration(camera, args.scan_duration)
            camera.stop()

            np.save(output / "off_frames.npy", off_frames)
            np.save(output / "scan_frames.npy", scan_frames)
            np.save(output / "off_timestamps.npy", off_timestamps)
            np.save(output / "scan_timestamps.npy", scan_timestamps)

            off_report = analyze_recording(
                off_timestamps,
                off_frames,
                off_frames,
                args.minimum_frequency,
                args.maximum_frequency,
            )
            scan_report = analyze_recording(
                scan_timestamps,
                scan_frames,
                off_frames,
                args.minimum_frequency,
                args.maximum_frequency,
            )

            scan_signals = scan_report.pop("signals")
            off_signals = off_report.pop("signals")
            write_csv(output / "off_temporal_signals.csv", off_timestamps, off_signals)
            write_csv(output / "scan_temporal_signals.csv", scan_timestamps, scan_signals)

            summary = {
                "session_directory": str(output),
                "configuration": candidate,
                "off": off_report,
                "scan": scan_report,
                "interpretation": {
                    "reported_frequency_is": "observed optical or system modulation frequency, not proven mechanical galvo frequency",
                    "bidirectional_warning": "the beam may cross the sensor twice per mechanical galvo cycle",
                    "alias_warning": "frequencies above the reported Nyquist limit can alias to lower observed frequencies",
                },
            }
            atomic_json(output / "report.json", summary)
        except BaseException as exc:
            primary_error = exc
            atomic_json(output / "error.json", {"error": str(exc)})
            raise
        finally:
            try:
                camera.stop()
            except Exception:
                pass
            try:
                restored = camera.restore_configuration(original)
                atomic_json(output / "restored_configuration.json", restored)
            except Exception as restore_error:
                atomic_json(output / "restore_error.json", {"error": str(restore_error)})
                if primary_error is None:
                    raise

    print(json.dumps({
        "session_directory": str(output),
        "measured_frame_rate_hz": summary["scan"]["measured_frame_rate_hz"],
        "nyquist_hz": summary["scan"]["nyquist_hz"],
        "top_signal_results": summary["scan"]["top_signal_results"][:8],
    }, indent=2, allow_nan=False))


if __name__ == "__main__":
    main()
