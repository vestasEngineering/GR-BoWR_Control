from __future__ import annotations

import argparse
import concurrent.futures
import hashlib
import itertools
import json
import math
import os
import time
from dataclasses import asdict, dataclass
from pathlib import Path

import numpy as np

from .downward_detector import QualityConfig, TriggerConfig, detect_frame


@dataclass(frozen=True)
class TriggerCandidate:
    threshold: int
    minimum_width: int
    maximum_width: int
    minimum_integrated_drop: float
    maximum_gap: int

    def configuration(self) -> TriggerConfig:
        return TriggerConfig(self.threshold, self.minimum_width, self.maximum_width, self.minimum_integrated_drop, self.maximum_gap)


@dataclass(frozen=True)
class QualityCandidate:
    low_plateau_threshold: int
    minimum_interior_low_fraction: float
    minimum_background_rail_fraction: float
    maximum_internal_gap: int
    minimum_shape_score: float

    def configuration(self) -> QualityConfig:
        return QualityConfig(
            low_plateau_threshold=self.low_plateau_threshold,
            minimum_interior_low_fraction=self.minimum_interior_low_fraction,
            minimum_background_rail_fraction=self.minimum_background_rail_fraction,
            maximum_internal_gap=self.maximum_internal_gap,
            minimum_shape_score=self.minimum_shape_score,
        )


def parse_ints(text: str) -> list[int]:
    return [int(value.strip()) for value in text.split(",") if value.strip()]


def parse_floats(text: str) -> list[float]:
    return [float(value.strip()) for value in text.split(",") if value.strip()]


def wilson(successes: int, trials: int, z: float = 1.959963984540054) -> tuple[float, float]:
    if trials <= 0:
        return 0.0, 1.0
    p = successes / trials
    denominator = 1 + z * z / trials
    center = (p + z * z / (2 * trials)) / denominator
    margin = z * math.sqrt(p * (1 - p) / trials + z * z / (4 * trials * trials)) / denominator
    return max(0.0, center - margin), min(1.0, center + margin)


def sha256_file(path: Path, chunk_size: int = 4 * 1024 * 1024) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        while True:
            chunk = handle.read(chunk_size)
            if not chunk:
                break
            digest.update(chunk)
    return digest.hexdigest()


def atomic_json(path: Path, value) -> None:
    temporary = path.with_suffix(path.suffix + ".tmp")
    with temporary.open("w", encoding="utf-8") as handle:
        json.dump(value, handle, indent=2, allow_nan=False)
        handle.flush()
        os.fsync(handle.fileno())
    os.replace(temporary, path)


def frame_indices(length: int, start_fraction: float, end_fraction: float, maximum: int | None = None) -> np.ndarray:
    start = int(length * start_fraction)
    end = int(length * end_fraction)
    if end <= start:
        return np.empty((0,), dtype=np.int64)
    count = end - start
    if maximum is None or count <= maximum:
        return np.arange(start, end, dtype=np.int64)
    return np.linspace(start, end - 1, maximum, dtype=np.int64)


def metrics(frames, trigger: TriggerConfig, quality: QualityConfig, indices: np.ndarray) -> dict:
    basic = good = multi = 0
    centroid = []
    boundary = []
    shape = []
    widths = []
    drops = []
    for index in indices:
        result = detect_frame(frames[int(index)], trigger, quality)
        if len(result.qualifying_regions) > 1:
            multi += 1
        region = result.selected_region
        if region is None:
            continue
        basic += 1
        widths.append(float(region.width))
        drops.append(float(region.integrated_drop))
        if region.measurement_quality:
            good += 1
            centroid.append(float(region.centroid))
            boundary.append(float(region.boundary_center))
            shape.append(float(region.shape_score))
    total = len(indices)
    basic_low, basic_high = wilson(basic, total)
    good_low, good_high = wilson(good, total)
    return {
        "total_frames": total,
        "basic_frames": basic,
        "good_frames": good,
        "basic_fraction": basic / total if total else 0.0,
        "basic_wilson_low": basic_low,
        "basic_wilson_high": basic_high,
        "good_yield": good / total if total else 0.0,
        "good_wilson_low": good_low,
        "good_wilson_high": good_high,
        "good_precision": good / basic if basic else 0.0,
        "multi_region_fraction": multi / total if total else 0.0,
        "centroid_std": float(np.std(centroid)) if centroid else None,
        "boundary_std": float(np.std(boundary)) if boundary else None,
        "median_shape": float(np.median(shape)) if shape else None,
        "median_width": float(np.median(widths)) if widths else None,
        "median_integrated_drop": float(np.median(drops)) if drops else None,
    }


def validate_capture(directory: Path) -> dict:
    configuration_path = directory / "camera_configuration.json"
    required = [configuration_path, directory / "off_frames.npy", directory / "on_frames.npy", directory / "off_summary.json", directory / "on_summary.json"]
    missing = [path.name for path in required if not path.exists()]
    if missing:
        return {"valid": False, "error": f"Missing files: {missing}"}
    off_summary = json.loads((directory / "off_summary.json").read_text(encoding="utf-8"))
    on_summary = json.loads((directory / "on_summary.json").read_text(encoding="utf-8"))
    for state, summary in (("off", off_summary), ("on", on_summary)):
        frame_path = directory / summary["frames_file"]
        timestamp_path = directory / summary["timestamps_file"]
        if sha256_file(frame_path) != summary["frames_sha256"]:
            return {"valid": False, "error": f"{state} frame checksum mismatch"}
        if sha256_file(timestamp_path) != summary["timestamps_sha256"]:
            return {"valid": False, "error": f"{state} timestamp checksum mismatch"}
    return {
        "valid": True,
        "configuration": json.loads(configuration_path.read_text(encoding="utf-8")),
        "off_summary": off_summary,
        "on_summary": on_summary,
    }


def analyze_candidate(task: dict) -> dict:
    directory = Path(task["directory"])
    validation = validate_capture(directory)
    candidate_id = directory.name
    if not validation["valid"]:
        return {"candidate_id": candidate_id, "status": "INVALID_CAPTURE", **validation}
    off = np.load(directory / "off_frames.npy", mmap_mode="r", allow_pickle=False)
    on = np.load(directory / "on_frames.npy", mmap_mode="r", allow_pickle=False)
    trigger_candidates = [TriggerCandidate(**row) for row in task["trigger_candidates"]]
    quality_default = QualityConfig()
    train_off = frame_indices(len(off), 0.0, 0.50, task["training_frames"])
    train_on = frame_indices(len(on), 0.0, 0.50, task["training_frames"])
    selection_off = frame_indices(len(off), 0.50, 0.75, task["selection_frames"])
    selection_on = frame_indices(len(on), 0.50, 0.75, task["selection_frames"])
    validation_off = frame_indices(len(off), 0.75, 1.0)
    validation_on = frame_indices(len(on), 0.75, 1.0)

    trigger_rows = []
    for candidate in trigger_candidates:
        trigger = candidate.configuration()
        off_result = metrics(off, trigger, quality_default, train_off)
        on_result = metrics(on, trigger, quality_default, train_on)
        accepted = off_result["basic_wilson_high"] <= task["max_false_fraction"]
        separation = on_result["basic_wilson_low"] - off_result["basic_wilson_high"]
        trigger_rows.append({"trigger": asdict(candidate), "accepted": accepted, "off": off_result, "on": on_result, "separation": separation})
    trigger_rows.sort(key=lambda row: (row["accepted"], row["on"]["basic_wilson_low"], row["separation"], row["on"]["good_precision"]), reverse=True)
    trigger_finalists = trigger_rows[:task["trigger_finalists"]]

    quality_candidates = [QualityCandidate(**row) for row in task["quality_candidates"]]
    joint_rows = []
    for trigger_row in trigger_finalists:
        trigger = TriggerCandidate(**trigger_row["trigger"]).configuration()
        for quality_candidate in quality_candidates:
            quality = quality_candidate.configuration()
            off_result = metrics(off, trigger, quality, selection_off)
            on_result = metrics(on, trigger, quality, selection_on)
            accepted = off_result["basic_wilson_high"] <= task["max_false_fraction"]
            separation = on_result["basic_wilson_low"] - off_result["basic_wilson_high"]
            joint_rows.append({"trigger": trigger_row["trigger"], "quality": asdict(quality_candidate), "accepted": accepted, "off": off_result, "on": on_result, "separation": separation})
    joint_rows.sort(key=lambda row: (row["accepted"], row["on"]["good_wilson_low"], row["separation"], row["on"]["good_precision"], -(row["on"]["centroid_std"] if row["on"]["centroid_std"] is not None else 1e9)), reverse=True)
    validation_rows = []
    for row in joint_rows[:task["joint_finalists"]]:
        trigger = TriggerCandidate(**row["trigger"]).configuration()
        quality = QualityCandidate(**row["quality"]).configuration()
        off_result = metrics(off, trigger, quality, validation_off)
        on_result = metrics(on, trigger, quality, validation_on)
        accepted = off_result["basic_wilson_high"] <= task["max_false_fraction"]
        separation = on_result["basic_wilson_low"] - off_result["basic_wilson_high"]
        validation_rows.append({"trigger": row["trigger"], "quality": row["quality"], "validation_accepted": accepted, "validation_off": off_result, "validation_on": on_result, "validation_separation": separation})
    validation_rows.sort(key=lambda row: (row["validation_accepted"], row["validation_on"]["good_wilson_low"], row["validation_separation"], row["validation_on"]["good_precision"], -(row["validation_on"]["centroid_std"] if row["validation_on"]["centroid_std"] is not None else 1e9)), reverse=True)
    return {
        "candidate_id": candidate_id,
        "status": "ANALYZED",
        "camera_configuration": validation["configuration"],
        "capture_summaries": {"off": validation["off_summary"], "on": validation["on_summary"]},
        "best": validation_rows[0],
        "validated_finalists": validation_rows,
        "training_trigger_count": len(trigger_rows),
    }


def result_rank(row: dict) -> tuple:
    if row.get("status") != "ANALYZED":
        return (False, 0.0, 0.0, 0.0, -1e9)
    best = row["best"]
    on = best["validation_on"]
    return (
        best["validation_accepted"],
        on["good_wilson_low"],
        best["validation_separation"],
        on["good_precision"],
        -(on["centroid_std"] if on["centroid_std"] is not None else 1e9),
    )


def main(argv=None):
    parser = argparse.ArgumentParser(description="Multicore desktop analysis for LISM joint captures")
    parser.add_argument("--input", required=True)
    parser.add_argument("--output", required=True)
    parser.add_argument("--workers", type=int, default=max(1, min(8, (os.cpu_count() or 2) - 1)))
    parser.add_argument("--thresholds", default="50000,55000,60000,62500")
    parser.add_argument("--minimum-widths", default="2,4,8")
    parser.add_argument("--maximum-widths", default="256,512")
    parser.add_argument("--integrated-drops", default="10000,100000,500000")
    parser.add_argument("--maximum-gaps", default="0,1,2")
    parser.add_argument("--low-plateau-thresholds", default="5000,10000,20000")
    parser.add_argument("--minimum-interior-low-fractions", default="0.70,0.80,0.90")
    parser.add_argument("--minimum-background-rail-fractions", default="0.94,0.96,0.98")
    parser.add_argument("--quality-maximum-gaps", default="2,4,8")
    parser.add_argument("--minimum-shape-scores", default="0.70,0.75,0.80")
    parser.add_argument("--training-frames", type=int, default=1200)
    parser.add_argument("--selection-frames", type=int, default=1000)
    parser.add_argument("--trigger-finalists", type=int, default=8)
    parser.add_argument("--joint-finalists", type=int, default=5)
    parser.add_argument("--max-false-fraction", type=float, default=0.001)
    parser.add_argument("--minimum-validation-good-frames", type=int, default=10)
    args = parser.parse_args(argv)

    source = Path(args.input).expanduser().resolve()
    output = Path(args.output).expanduser().resolve()
    output.mkdir(parents=True, exist_ok=False)
    manifest = json.loads((source / "run_manifest.json").read_text(encoding="utf-8"))
    if manifest.get("status") != "CAPTURE_COMPLETE":
        raise RuntimeError(f"Capture is not complete: status={manifest.get('status')}")
    candidate_directories = sorted(path for path in (source / "candidates").iterdir() if path.is_dir())
    triggers = [
        asdict(TriggerCandidate(t, minimum, maximum, drop, gap))
        for t, minimum, maximum, drop, gap in itertools.product(
            parse_ints(args.thresholds), parse_ints(args.minimum_widths), parse_ints(args.maximum_widths), parse_floats(args.integrated_drops), parse_ints(args.maximum_gaps)
        ) if minimum <= maximum
    ]
    qualities = [
        asdict(QualityCandidate(plateau, interior, background, gap, shape))
        for plateau, interior, background, gap, shape in itertools.product(
            parse_ints(args.low_plateau_thresholds), parse_floats(args.minimum_interior_low_fractions), parse_floats(args.minimum_background_rail_fractions), parse_ints(args.quality_maximum_gaps), parse_floats(args.minimum_shape_scores)
        )
    ]
    task_base = {
        "trigger_candidates": triggers,
        "quality_candidates": qualities,
        "training_frames": args.training_frames,
        "selection_frames": args.selection_frames,
        "trigger_finalists": args.trigger_finalists,
        "joint_finalists": args.joint_finalists,
        "max_false_fraction": args.max_false_fraction,
    }
    analysis_manifest = {
        "schema_version": 1,
        "status": "RUNNING",
        "source": str(source),
        "capture_manifest": manifest,
        "arguments": vars(args),
        "candidate_count": len(candidate_directories),
        "trigger_candidate_count": len(triggers),
        "quality_candidate_count": len(qualities),
        "started_utc": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
    }
    atomic_json(output / "analysis_manifest.json", analysis_manifest)
    results = []
    checkpoint = output / "analysis_checkpoint.json"
    started = time.monotonic()
    tasks = [dict(task_base, directory=str(directory)) for directory in candidate_directories]
    with concurrent.futures.ProcessPoolExecutor(max_workers=args.workers) as executor:
        future_map = {executor.submit(analyze_candidate, task): Path(task["directory"]).name for task in tasks}
        for completed, future in enumerate(concurrent.futures.as_completed(future_map), 1):
            candidate_id = future_map[future]
            try:
                result = future.result()
            except Exception as exc:
                result = {"candidate_id": candidate_id, "status": "ANALYSIS_ERROR", "error": str(exc)}
            results.append(result)
            atomic_json(checkpoint, results)
            elapsed = time.monotonic() - started
            rate = completed / elapsed if elapsed else 0.0
            remaining = (len(tasks) - completed) / rate if rate else 0.0
            print(f"[{completed}/{len(tasks)}] {candidate_id}: {result['status']}, approximately {remaining / 60:.1f} minutes remaining", flush=True)

    ranked = sorted(results, key=result_rank, reverse=True)
    recommendation = next((row for row in ranked if row.get("status") == "ANALYZED" and row["best"]["validation_accepted"] and row["best"]["validation_on"]["good_frames"] >= args.minimum_validation_good_frames), None)
    atomic_json(output / "camera_trigger_quality_ranked.json", ranked)
    atomic_json(output / "recommended_configuration.json", recommendation)
    rejection_summary = {
        "total_candidates": len(results),
        "analyzed": sum(row.get("status") == "ANALYZED" for row in results),
        "invalid_capture": sum(row.get("status") == "INVALID_CAPTURE" for row in results),
        "analysis_errors": sum(row.get("status") == "ANALYSIS_ERROR" for row in results),
        "validation_accepted": sum(row.get("status") == "ANALYZED" and row["best"]["validation_accepted"] for row in results),
        "statistically_eligible": sum(row.get("status") == "ANALYZED" and row["best"]["validation_accepted"] and row["best"]["validation_on"]["good_frames"] >= args.minimum_validation_good_frames for row in results),
    }
    atomic_json(output / "rejection_summary.json", rejection_summary)
    analysis_manifest["status"] = "COMPLETE" if recommendation else "COMPLETE_NO_STATISTICAL_RECOMMENDATION"
    analysis_manifest["elapsed_seconds"] = time.monotonic() - started
    analysis_manifest["completed_utc"] = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    atomic_json(output / "analysis_manifest.json", analysis_manifest)

    refinement = None
    if recommendation:
        camera = recommendation["camera_configuration"]["requested"]
        refinement_candidates = []
        for bias in sorted(set(max(0, camera["bias"] + delta) for delta in (-8, -4, 0, 4, 8))):
            for high in sorted(set(max(1, camera["st_high"] + delta) for delta in (-500, -250, 0, 250, 500))):
                for low in sorted(set(max(0, camera["st_low"] + delta) for delta in (-25, 0, 25))):
                    if high <= low:
                        continue
                    for edge in sorted(set(max(0, camera["edge_delay"] + delta) for delta in (-4, -2, 0, 2, 4))):
                        refinement_candidates.append({"bias": bias, "gain": camera["gain"], "st_high": high, "st_low": low, "edge_delay": edge})
        refinement = {"schema_version": 1, "source_recommendation": camera, "candidates": refinement_candidates[:60]}
        atomic_json(output / "refinement_plan.json", refinement)

    print(f"Analysis complete: {output}")
    print(json.dumps(recommendation, indent=2) if recommendation else "No statistically supported recommendation.")


if __name__ == "__main__":
    main()
