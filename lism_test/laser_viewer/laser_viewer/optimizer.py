from __future__ import annotations

import csv
import itertools
import json
import math
import os
from dataclasses import asdict, dataclass
from pathlib import Path

import numpy as np


UNIFORM_U16_STD = math.sqrt(((65536.0 ** 2) - 1.0) / 12.0)


@dataclass(frozen=True)
class Candidate:
    bias: int
    gain: int
    st_high: int
    st_low: int
    edge_delay: int

    def as_apply_args(self) -> dict:
        return {
            "adc_bias": self.bias,
            "adc_gain": self.gain,
            "st_high": self.st_high,
            "st_low": self.st_low,
            "edge_delay": self.edge_delay,
        }


def safe_correlation(a: np.ndarray, b: np.ndarray) -> float:
    av = a.ravel().astype(np.float64)
    bv = b.ravel().astype(np.float64)
    if av.size != bv.size or av.size < 2 or np.std(av) == 0 or np.std(bv) == 0:
        return 0.0
    result = float(np.corrcoef(av, bv)[0, 1])
    return result if np.isfinite(result) else 0.0


def _clip_statistics(frames: np.ndarray, rail_margin: int, start: int | None = None, end: int | None = None) -> dict:
    low = frames <= rail_margin
    high = frames >= 65535 - rail_margin
    result = {
        "low_clip_sample_count": int(low.sum()),
        "high_clip_sample_count": int(high.sum()),
        "low_clip_fraction": float(low.mean()),
        "high_clip_fraction": float(high.mean()),
        "pixels_ever_low_clipped": int(low.any(axis=0).sum()),
        "pixels_always_low_clipped": int(low.all(axis=0).sum()),
        "pixels_ever_high_clipped": int(high.any(axis=0).sum()),
        "pixels_always_high_clipped": int(high.all(axis=0).sum()),
    }
    if start is not None and end is not None:
        region_low = low[:, start:end + 1]
        region_high = high[:, start:end + 1]
        result.update({
            "region_low_clip_sample_count": int(region_low.sum()),
            "region_high_clip_sample_count": int(region_high.sum()),
            "region_low_clip_fraction": float(region_low.mean()),
            "region_high_clip_fraction": float(region_high.mean()),
            "region_pixels_ever_low_clipped": int(region_low.any(axis=0).sum()),
            "region_pixels_always_low_clipped": int(region_low.all(axis=0).sum()),
            "region_pixels_ever_high_clipped": int(region_high.any(axis=0).sum()),
            "region_pixels_always_high_clipped": int(region_high.all(axis=0).sum()),
        })
    return result


def analyze_dark(frames, rail_margin, max_rail_fraction, random_std_tolerance, min_frame_correlation):
    if frames.ndim != 2 or frames.shape[0] < 3:
        raise ValueError("Expected at least three frames in a 2D array")
    median = np.median(frames, axis=0)
    mad = 1.4826 * np.median(np.abs(frames - median), axis=0)
    frame_corr = float(np.median([safe_correlation(frames[i - 1], frames[i]) for i in range(1, frames.shape[0])]))
    pixel_corr = float(np.median([safe_correlation(frame[:-1], frame[1:]) for frame in frames]))
    std = float(np.std(frames))
    clips = _clip_statistics(frames, rail_margin)
    uniform_similarity = abs(std - UNIFORM_U16_STD) / UNIFORM_U16_STD
    robust_noise = float(np.median(mad))
    random_like = (
        uniform_similarity <= random_std_tolerance
        and (abs(frame_corr) < min_frame_correlation or abs(pixel_corr) < min_frame_correlation)
        and robust_noise >= 5000.0
    )
    low_clipped = clips["low_clip_fraction"] > max_rail_fraction
    high_clipped = clips["high_clip_fraction"] > max_rail_fraction
    if random_like:
        classification = "RANDOM_LIKE"
    elif clips["low_clip_fraction"] >= 0.95:
        classification = "RAIL_PINNED_LOW"
    elif clips["high_clip_fraction"] >= 0.95:
        classification = "RAIL_PINNED_HIGH"
    elif low_clipped:
        classification = "PROMISING_CLIPPED_LOW" if abs(frame_corr) >= min_frame_correlation else "CLIPPED_LOW_UNSTABLE"
    elif high_clipped:
        classification = "PROMISING_CLIPPED_HIGH" if abs(frame_corr) >= min_frame_correlation else "CLIPPED_HIGH_UNSTABLE"
    elif abs(frame_corr) < min_frame_correlation:
        classification = "PROVISIONAL_LOW_FRAME_CORRELATION"
    else:
        classification = "QUALIFIED"
    eligible_for_laser = classification in {
        "QUALIFIED", "PROVISIONAL_LOW_FRAME_CORRELATION",
        "PROMISING_CLIPPED_LOW", "PROMISING_CLIPPED_HIGH",
    }
    final_eligible = classification == "QUALIFIED"
    score = robust_noise + 20000.0 * (clips["low_clip_fraction"] + clips["high_clip_fraction"])
    score += 5000.0 * max(0.0, min_frame_correlation - abs(frame_corr))
    if random_like:
        score += 20000.0
    return {
        "mean": float(np.mean(frames)), "std": std,
        "robust_noise_median": robust_noise,
        "frame_correlation": frame_corr, "pixel_correlation": pixel_corr,
        "uniform_std_similarity": uniform_similarity,
        "classification": classification,
        "qualified": final_eligible,
        "eligible_for_laser": eligible_for_laser,
        "dark_score": score,
        "rejection_reason": "" if final_eligible else classification,
        **clips,
    }


def laser_metrics(dark, laser, rail_margin, minimum_region_width=2, maximum_region_width=256, minimum_snr=6.0, max_region_clip_fraction=0.005):
    dark_median = np.median(dark, axis=0)
    laser_median = np.median(laser, axis=0)
    noise = np.maximum(1.4826 * np.median(np.abs(dark - dark_median), axis=0), 1.0)
    delta = laser_median - dark_median
    positive = np.maximum(delta, 0.0)
    negative = np.maximum(-delta, 0.0)
    top_count = max(4, dark.shape[1] // 100)
    positive_score = float(np.mean(np.partition(positive, -top_count)[-top_count:]))
    negative_score = float(np.mean(np.partition(negative, -top_count)[-top_count:]))
    polarity = "negative" if negative_score > positive_score else "positive"
    signal = negative if polarity == "negative" else positive
    threshold = np.maximum(noise * 6.0, 100.0)
    changes = np.diff(np.pad((signal > threshold).astype(np.int8), (1, 1)))
    regions = [(int(start), int(end)) for start, end in zip(
        np.flatnonzero(changes == 1),
        np.flatnonzero(changes == -1) - 1,
    )]
    valid_regions = [
        (start, end) for start, end in regions
        if minimum_region_width <= end - start + 1 <= maximum_region_width
    ]
    if valid_regions:
        start, end = max(
            valid_regions,
            key=lambda region: float(signal[region[0]:region[1] + 1].sum()),
        )
        region_signal = signal[start:end + 1]
        local_peak = int(np.argmax(region_signal))
        peak = start + local_peak
        peak_signal = float(signal[peak])
        peak_snr = peak_signal / float(noise[peak])
        total = float(region_signal.sum())
        centroid = float(np.dot(np.arange(start, end + 1), region_signal) / total) if total > 0 else None
        width = end - start + 1
    else:
        start = end = centroid = None
        width = 0
        total = 0.0
        peak = None
        peak_signal = 0.0
        peak_snr = 0.0
    clips = _clip_statistics(laser, rail_margin, start, end)
    region_clip_fraction = max(
        clips.get("region_low_clip_fraction", 1.0),
        clips.get("region_high_clip_fraction", 1.0),
    )
    if not regions:
        classification = "REJECTED_NO_REGION"
    elif not valid_regions:
        classification = "REJECTED_REGION_WIDTH"
    elif peak_snr < minimum_snr:
        classification = "REJECTED_LOW_SNR"
    elif region_clip_fraction > max_region_clip_fraction:
        classification = "PROVISIONAL_REGION_CLIPPING"
    else:
        classification = "RECOMMENDED"
    score = peak_snr + math.log10(max(total, 1.0))
    if classification.startswith("REJECTED"):
        score -= 50.0
    elif classification == "PROVISIONAL_REGION_CLIPPING":
        score -= 10.0
    return {
        "laser_classification": classification,
        "laser_final_eligible": classification == "RECOMMENDED",
        "polarity": polarity,
        "peak_pixel": peak,
        "peak_signal": peak_signal,
        "peak_snr": peak_snr,
        "region_start": start,
        "region_end": end,
        "region_width": width,
        "centroid": centroid,
        "integrated_signal": total,
        "laser_score": score,
        **clips,
    }


class ConfigOptimizer:
    def __init__(self, camera, config, output: Path):
        self.camera = camera
        self.config = config
        self.output = output
        self.captures = output / "captures"

    def _atomic_json(self, path: Path, value) -> None:
        temp = path.with_suffix(path.suffix + ".tmp")
        with temp.open("w", encoding="utf-8") as handle:
            json.dump(value, handle, indent=2, allow_nan=False)
            handle.flush(); os.fsync(handle.fileno())
        os.replace(temp, path)

    def _restore(self, original: dict, primary_error: BaseException | None) -> None:
        try:
            restored = self.camera.restore_configuration(original)
            self._atomic_json(self.output / "restored_configuration.json", restored)
        except Exception as exc:
            message = f"Original configuration restoration failed: {exc}"
            self._atomic_json(self.output / "restore_error.json", {"error": message})
            print(f"WARNING: {message}", flush=True)
            if primary_error is None:
                raise RuntimeError(message) from exc

    def _coarse_candidates(self):
        for bias, gain, high in itertools.product(self.config.optimizer_bias_values, self.config.optimizer_gain_values, self.config.optimizer_st_high_values):
            yield Candidate(bias, gain, high, self.config.optimizer_st_low, self.config.optimizer_edge_delay)

    def _capture_dark_candidates(self, candidates, prefix: str) -> list[dict]:
        rows = []
        original = self.camera.configuration()
        primary_error = None
        try:
            for index, candidate in enumerate(candidates):
                print(f"[{index + 1}] {prefix} candidate {asdict(candidate)}", flush=True)
                try:
                    observed = self.camera.apply_temporary_configuration(**candidate.as_apply_args())
                    self.camera.start()
                    frames = self.camera.capture_frames(self.config.optimizer_capture_frames, self.config.optimizer_settle_frames)
                    self.camera.stop()
                    metrics = analyze_dark(frames, self.config.optimizer_rail_margin, self.config.optimizer_max_rail_fraction, self.config.optimizer_random_std_tolerance, self.config.optimizer_min_frame_correlation)
                    capture_name = f"{prefix}_{index:03d}.npy"
                    np.save(self.captures / capture_name, frames)
                    metrics["dark_classification"] = metrics.pop("classification")
                    metrics["dark_qualified"] = metrics.pop("qualified")
                    row = {"index": index, **asdict(candidate), **metrics, "capture": capture_name, "observed": observed}
                except Exception as exc:
                    try: self.camera.stop()
                    except Exception: pass
                    row = {"index": index, **asdict(candidate), "classification": "ACQUISITION_ERROR", "qualified": False, "eligible_for_laser": False, "dark_score": 1e30, "rejection_reason": str(exc), "capture": None}
                rows.append(row)
                self._atomic_json(self.output / f"{prefix}_candidates.json", rows)
        except BaseException as exc:
            primary_error = exc
            raise
        finally:
            try: self.camera.stop()
            except Exception: pass
            self._restore(original, primary_error)
        return rows

    def dark_search(self) -> dict:
        self.output.mkdir(parents=True, exist_ok=False)
        self.captures.mkdir()
        rows = self._capture_dark_candidates(list(self._coarse_candidates()), "dark")
        ranked = sorted(rows, key=lambda row: float(row["dark_score"]))
        eligible = [row for row in ranked if row.get("eligible_for_laser")]
        finalists = eligible[:self.config.optimizer_finalists]
        provisional = [row for row in ranked if row.get("dark_classification", "").startswith("PROMISING_CLIPPED")]
        self._atomic_json(self.output / "dark_ranked.json", ranked)
        self._atomic_json(self.output / "finalists.json", finalists)
        self._atomic_json(self.output / "provisional_candidates.json", provisional)
        self._write_csv(self.output / "dark_candidates.csv", rows)
        summary = {"run_directory": str(self.output), "candidate_count": len(rows), "laser_eligible_count": len(eligible), "finalist_count": len(finalists), "provisional_clipped_count": len(provisional), "no_valid_configuration": not eligible}
        self._atomic_json(self.output / "manifest.json", summary)
        return summary

    def refine_search(self) -> dict:
        ranked = json.loads((self.output / "dark_ranked.json").read_text(encoding="utf-8"))
        leads = [row for row in ranked if row.get("dark_classification", "").startswith("PROMISING_CLIPPED")]
        if not leads:
            raise RuntimeError("No promising clipped candidate is available for refinement")
        lead = min(leads, key=lambda row: float(row["dark_score"]))
        radius = self.config.optimizer_refine_bias_radius
        step = self.config.optimizer_refine_bias_step
        biases = list(range(max(0, lead["bias"] - radius), min(1023, lead["bias"] + radius) + 1, step))
        gains = sorted({0, int(lead["gain"])})
        candidates = [Candidate(bias, gain, high, lead["st_low"], lead["edge_delay"]) for bias, gain, high in itertools.product(biases, gains, self.config.optimizer_refine_st_high_values)]
        rows = self._capture_dark_candidates(candidates, "refine_dark")
        ranked_refined = sorted(rows, key=lambda row: float(row["dark_score"]))
        eligible = [row for row in ranked_refined if row.get("eligible_for_laser") and row.get("dark_classification") in {"QUALIFIED", "PROVISIONAL_LOW_FRAME_CORRELATION"}]
        finalists = eligible[:self.config.optimizer_finalists]
        self._atomic_json(self.output / "refine_dark_ranked.json", ranked_refined)
        self._atomic_json(self.output / "refine_finalists.json", finalists)
        self._write_csv(self.output / "refine_dark_candidates.csv", rows)
        return {"lead": lead, "candidate_count": len(rows), "finalist_count": len(finalists), "finalists_file": "refine_finalists.json"}

    def laser_search(self, label: str | None = None) -> dict:
        finalist_file = self.output / "refine_finalists.json"
        if not finalist_file.exists():
            finalist_file = self.output / "finalists.json"
        finalists = json.loads(finalist_file.read_text(encoding="utf-8"))
        if not finalists:
            raise RuntimeError("No laser-eligible finalists are available")

        safe_label = "".join(character for character in (label or "paired") if character.isalnum() or character in "-_") or "paired"
        from datetime import datetime
        session = self.output / "laser_sessions" / f"{datetime.now().strftime('%Y%m%d_%H%M%S_%f')}_{safe_label}"
        session_captures = session / "captures"
        session_captures.mkdir(parents=True, exist_ok=False)
        original = self.camera.configuration()
        rows = []
        primary_error = None
        try:
            for order, finalist in enumerate(finalists):
                candidate = Candidate(finalist["bias"], finalist["gain"], finalist["st_high"], finalist["st_low"], finalist["edge_delay"])
                print(f"[{order + 1}/{len(finalists)}] paired candidate {asdict(candidate)}", flush=True)
                self.camera.apply_temporary_configuration(**candidate.as_apply_args())

                input("  Set laser OFF, then press Enter: ")
                self.camera.start()
                off = self.camera.capture_frames(self.config.optimizer_capture_frames, self.config.optimizer_settle_frames)
                self.camera.stop()

                input("  Set laser ON, then press Enter: ")
                self.camera.start()
                on = self.camera.capture_frames(self.config.optimizer_capture_frames, self.config.optimizer_settle_frames)
                self.camera.stop()

                stem = f"candidate_{order:03d}_{candidate.bias}_{candidate.gain}_{candidate.st_high}"
                off_name = f"{stem}_off.npy"
                on_name = f"{stem}_on.npy"
                np.save(session_captures / off_name, off)
                np.save(session_captures / on_name, on)

                stored_dark = np.load(self.captures / finalist["capture"], allow_pickle=False)
                kwargs = (
                    self.config.optimizer_rail_margin,
                    self.config.minimum_region_width,
                    self.config.maximum_region_width,
                    self.config.optimizer_min_laser_snr,
                    self.config.optimizer_max_region_clip_fraction,
                )
                off_control = laser_metrics(stored_dark, off, *kwargs)
                response = laser_metrics(off, on, *kwargs)
                false_positive = off_control["laser_classification"] in {"RECOMMENDED", "PROVISIONAL_REGION_CLIPPING"}
                final_eligible = response["laser_final_eligible"] and not false_positive
                final_classification = "REJECTED_OFF_CONTROL_FALSE_POSITIVE" if false_positive else response["laser_classification"]
                row = {
                    **finalist,
                    "dark_classification": finalist.get("dark_classification", finalist.get("classification")),
                    "off_control": off_control,
                    **response,
                    "laser_classification": final_classification,
                    "laser_final_eligible": final_eligible,
                    "off_capture": off_name,
                    "on_capture": on_name,
                }
                rows.append(row)
                self._atomic_json(session / "laser_candidates.json", rows)
        except BaseException as exc:
            primary_error = exc
            raise
        finally:
            try:
                self.camera.stop()
            except Exception:
                pass
            self._restore(original, primary_error)

        ranked = sorted(rows, key=lambda row: float(row["laser_score"]), reverse=True)
        recommendation = next((row for row in ranked if row.get("laser_final_eligible")), None)
        self._atomic_json(session / "laser_ranked.json", ranked)
        self._atomic_json(session / "recommended_configuration.json", recommendation)
        self._write_csv(session / "laser_candidates.csv", rows)
        manifest = {
            "session_directory": str(session),
            "label": safe_label,
            "tested": len(rows),
            "recommended": recommendation is not None,
        }
        self._atomic_json(session / "session_manifest.json", manifest)
        self._atomic_json(self.output / "latest_laser_session.json", manifest)
        return {**manifest, "recommendation": recommendation}

    def verify(self) -> dict:
        raise RuntimeError("The paired laser scan already includes OFF/ON control. A separate repeated-pair verification command will be added only after a consistent paired recommendation exists.")

    def apply(self) -> dict:
        raise RuntimeError("Automatic apply is disabled until a paired recommendation is repeated consistently. Apply no settings permanently.")

    @staticmethod
    def _write_csv(path: Path, rows: list[dict]) -> None:
        scalar_rows = [{key: value for key, value in row.items() if not isinstance(value, (dict, list))} for row in rows]
        keys = sorted({key for row in scalar_rows for key in row})
        with path.open("w", newline="", encoding="utf-8") as handle:
            writer = csv.DictWriter(handle, fieldnames=keys)
            writer.writeheader(); writer.writerows(scalar_rows)
