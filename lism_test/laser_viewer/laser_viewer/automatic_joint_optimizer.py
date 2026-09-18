from __future__ import annotations

import argparse
import itertools
import json
import math
import os
import shutil
import time
from dataclasses import asdict, dataclass
from pathlib import Path

import numpy as np

from .downward_detector import QualityConfig, TriggerConfig, detect_frame


@dataclass(frozen=True)
class CameraCandidate:
    bias: int
    gain: int
    st_high: int
    st_low: int
    edge_delay: int

    @property
    def candidate_id(self) -> str:
        return f"b{self.bias}_g{self.gain}_h{self.st_high}_l{self.st_low}_e{self.edge_delay}"

    def apply_args(self) -> dict:
        return {"adc_bias": self.bias, "adc_gain": self.gain, "st_high": self.st_high, "st_low": self.st_low, "edge_delay": self.edge_delay}


@dataclass(frozen=True)
class TriggerCandidate:
    threshold: int
    minimum_width: int
    maximum_width: int
    minimum_integrated_drop: float
    maximum_gap: int

    def config(self) -> TriggerConfig:
        return TriggerConfig(self.threshold, self.minimum_width, self.maximum_width, self.minimum_integrated_drop, self.maximum_gap)


@dataclass
class Counts:
    total: int = 0
    basic: int = 0
    good: int = 0
    multi: int = 0
    centroid_values: list[float] | None = None
    boundary_values: list[float] | None = None
    shape_values: list[float] | None = None

    def __post_init__(self):
        self.centroid_values = [] if self.centroid_values is None else self.centroid_values
        self.boundary_values = [] if self.boundary_values is None else self.boundary_values
        self.shape_values = [] if self.shape_values is None else self.shape_values

    def observe(self, result) -> None:
        self.total += 1
        if len(result.qualifying_regions) > 1:
            self.multi += 1
        region = result.selected_region
        if region is None:
            return
        self.basic += 1
        if region.measurement_quality:
            self.good += 1
            self.centroid_values.append(float(region.centroid))
            self.boundary_values.append(float(region.boundary_center))
            self.shape_values.append(float(region.shape_score))

    def summary(self) -> dict:
        basic_low, basic_high = wilson(self.basic, self.total)
        good_low, good_high = wilson(self.good, self.total)
        return {
            "total_frames": self.total,
            "basic_frames": self.basic,
            "good_frames": self.good,
            "basic_fraction": self.basic / self.total if self.total else 0.0,
            "basic_wilson_low": basic_low,
            "basic_wilson_high": basic_high,
            "good_yield": self.good / self.total if self.total else 0.0,
            "good_wilson_low": good_low,
            "good_wilson_high": good_high,
            "good_precision": self.good / self.basic if self.basic else 0.0,
            "multi_region_fraction": self.multi / self.total if self.total else 0.0,
            "centroid_std": float(np.std(self.centroid_values)) if self.centroid_values else None,
            "boundary_std": float(np.std(self.boundary_values)) if self.boundary_values else None,
            "median_shape": float(np.median(self.shape_values)) if self.shape_values else None,
        }


def wilson(successes: int, trials: int, z: float = 1.959963984540054) -> tuple[float, float]:
    if trials <= 0:
        return 0.0, 1.0
    p = successes / trials
    denominator = 1.0 + z * z / trials
    center = (p + z * z / (2.0 * trials)) / denominator
    margin = z * math.sqrt(p * (1.0 - p) / trials + z * z / (4.0 * trials * trials)) / denominator
    return max(0.0, center - margin), min(1.0, center + margin)


def json_safe(value):
    if isinstance(value, np.ndarray): return value.tolist()
    if isinstance(value, np.generic): return value.item()
    if isinstance(value, Path): return str(value)
    if isinstance(value, dict): return {str(k): json_safe(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)): return [json_safe(v) for v in value]
    return value


def atomic_json(path: Path, value) -> None:
    temporary = path.with_suffix(path.suffix + ".tmp")
    with temporary.open("w", encoding="utf-8") as handle:
        json.dump(json_safe(value), handle, indent=2, allow_nan=False)
        handle.flush()
        os.fsync(handle.fileno())
    os.replace(temporary, path)


def parse_ints(text: str) -> list[int]:
    return [int(value.strip()) for value in text.split(",") if value.strip()]


def parse_floats(text: str) -> list[float]:
    return [float(value.strip()) for value in text.split(",") if value.strip()]


def all_camera_candidates(args) -> list[CameraCandidate]:
    return [CameraCandidate(*values) for values in itertools.product(parse_ints(args.biases), parse_ints(args.gains), parse_ints(args.st_highs), parse_ints(args.st_lows), parse_ints(args.edge_delays))]


def normalized_vector(candidate: CameraCandidate, candidates: list[CameraCandidate]) -> np.ndarray:
    fields = ("bias", "gain", "st_high", "st_low", "edge_delay")
    vector = []
    for field in fields:
        values = [getattr(item, field) for item in candidates]
        span = max(values) - min(values)
        vector.append(0.0 if span == 0 else (getattr(candidate, field) - min(values)) / span)
    return np.asarray(vector)


def space_filling_candidates(candidates: list[CameraCandidate], maximum: int, reference: CameraCandidate) -> list[CameraCandidate]:
    if len(candidates) <= maximum:
        return candidates
    chosen = [min(candidates, key=lambda item: np.linalg.norm(normalized_vector(item, candidates) - normalized_vector(reference, candidates)))]
    remaining = [item for item in candidates if item not in chosen]
    while remaining and len(chosen) < maximum:
        item = max(remaining, key=lambda candidate: min(np.linalg.norm(normalized_vector(candidate, candidates) - normalized_vector(existing, candidates)) for existing in chosen))
        chosen.append(item)
        remaining.remove(item)
    return chosen


def trigger_candidates(args) -> list[TriggerCandidate]:
    return [TriggerCandidate(int(t), int(lo), int(hi), float(drop), int(gap)) for t, lo, hi, drop, gap in itertools.product(parse_ints(args.thresholds), parse_ints(args.minimum_widths), parse_ints(args.maximum_widths), parse_floats(args.integrated_drops), parse_ints(args.maximum_gaps)) if lo <= hi]


def evenly_spaced_indices(length: int, count: int, start: int, end: int) -> np.ndarray:
    start = max(0, min(length, start)); end = max(start, min(length, end))
    available = end - start
    if available <= count:
        return np.arange(start, end)
    return np.linspace(start, end - 1, count, dtype=np.int64)


def evaluate(frames: np.ndarray, config: TriggerConfig, quality: QualityConfig, indices: np.ndarray | None = None) -> dict:
    counts = Counts()
    selected = frames if indices is None else frames[indices]
    for frame in selected:
        counts.observe(detect_frame(frame, config, quality))
    return counts.summary()


def select_trigger(off_frames: np.ndarray, on_frames: np.ndarray, candidates: list[TriggerCandidate], quality: QualityConfig, training_count: int, validation_fraction: float, max_false_fraction: float) -> dict:
    split_off = max(1, int(len(off_frames) * (1.0 - validation_fraction)))
    split_on = max(1, int(len(on_frames) * (1.0 - validation_fraction)))
    off_train = evenly_spaced_indices(len(off_frames), training_count, 0, split_off)
    on_train = evenly_spaced_indices(len(on_frames), training_count, 0, split_on)
    ranked = []
    for candidate in candidates:
        config = candidate.config()
        off = evaluate(off_frames, config, quality, off_train)
        on = evaluate(on_frames, config, quality, on_train)
        accepted = off["basic_wilson_high"] <= max_false_fraction
        separation = on["basic_wilson_low"] - off["basic_wilson_high"]
        ranked.append({"trigger": asdict(candidate), "accepted": accepted, "training_off": off, "training_on": on, "separation": separation})
    ranked.sort(key=lambda row: (row["accepted"], row["training_on"]["good_wilson_low"], row["separation"], row["training_on"]["good_precision"]), reverse=True)
    finalists = ranked[:min(5, len(ranked))]
    off_validation = np.arange(split_off, len(off_frames))
    on_validation = np.arange(split_on, len(on_frames))
    for row in finalists:
        config = TriggerCandidate(**row["trigger"]).config()
        row["validation_off"] = evaluate(off_frames, config, quality, off_validation)
        row["validation_on"] = evaluate(on_frames, config, quality, on_validation)
        row["validation_accepted"] = row["validation_off"]["basic_wilson_high"] <= max_false_fraction
        row["validation_separation"] = row["validation_on"]["basic_wilson_low"] - row["validation_off"]["basic_wilson_high"]
    finalists.sort(key=lambda row: (row.get("validation_accepted", False), row["validation_on"]["good_wilson_low"], row["validation_separation"], row["validation_on"]["good_precision"]), reverse=True)
    return {"best": finalists[0], "training_candidate_count": len(ranked), "validated_finalists": finalists}


def capture(camera, frames: int, settle: int, destination: Path, label: str) -> np.ndarray:
    data = np.lib.format.open_memmap(destination, mode="w+", dtype=np.uint16, shape=(frames, 2048))
    for _ in range(settle): camera.read_frame()
    started = time.monotonic()
    for index in range(frames):
        frame = np.asarray(camera.read_frame(), dtype=np.uint16)
        if frame.size != 2048: raise RuntimeError(f"Expected 2048 pixels, received {frame.size}")
        data[index] = frame
        if (index + 1) % 1000 == 0 or index + 1 == frames:
            elapsed = time.monotonic() - started; rate = (index + 1) / elapsed if elapsed else 0.0
            print(f"\r{label}: {index + 1:,}/{frames:,}, {rate:,.0f} fps", end="", flush=True)
    print(); data.flush(); del data
    return np.load(destination, mmap_mode="r", allow_pickle=False)


def camera_rank(row: dict) -> tuple:
    best = row["trigger_search"]["best"]
    on = best["validation_on"]; off = best["validation_off"]
    return (best["validation_accepted"], on["good_wilson_low"], best["validation_separation"], on["good_precision"], -(on["centroid_std"] if on["centroid_std"] is not None else 1e9), -off["basic_wilson_high"])


def main(argv=None):
    from .camera import LismCamera
    from .config import Config

    p = argparse.ArgumentParser(description="Automatic joint LISM camera and trigger optimizer")
    p.add_argument("--config", default="config/default.json"); p.add_argument("--output", required=True); p.add_argument("--duration-minutes", type=float, default=30)
    p.add_argument("--biases", default="246,254,262,270,278,286"); p.add_argument("--gains", default="15"); p.add_argument("--st-highs", default="7500,8000,8500,9000")
    p.add_argument("--st-lows", default="50,100,150"); p.add_argument("--edge-delays", default="80,84,88,92,96"); p.add_argument("--max-camera-candidates", type=int, default=36)
    p.add_argument("--reference-bias", type=int, default=286); p.add_argument("--reference-gain", type=int, default=15); p.add_argument("--reference-st-high", type=int, default=8000); p.add_argument("--reference-st-low", type=int, default=100); p.add_argument("--reference-edge-delay", type=int, default=88)
    p.add_argument("--off-seconds", type=float, default=5); p.add_argument("--on-seconds", type=float, default=8); p.add_argument("--estimated-fps", type=float, default=1000); p.add_argument("--settle-frames", type=int, default=32)
    p.add_argument("--thresholds", default="50000,55000,60000,62500"); p.add_argument("--minimum-widths", default="2,4,8"); p.add_argument("--maximum-widths", default="256,512"); p.add_argument("--integrated-drops", default="10000,100000,500000"); p.add_argument("--maximum-gaps", default="0,1,2")
    p.add_argument("--trigger-training-frames", type=int, default=1500); p.add_argument("--validation-fraction", type=float, default=.40); p.add_argument("--max-false-fraction", type=float, default=.001); p.add_argument("--minimum-good-frames", type=int, default=30)
    p.add_argument("--keep-all-captures", action="store_true"); p.add_argument("--free-space-reserve-gb", type=float, default=2)
    args = p.parse_args(argv)
    if not 0.2 <= args.validation_fraction <= 0.8: raise ValueError("validation-fraction must be between 0.2 and 0.8")
    all_candidates = all_camera_candidates(args); reference = CameraCandidate(args.reference_bias,args.reference_gain,args.reference_st_high,args.reference_st_low,args.reference_edge_delay)
    camera_candidates = space_filling_candidates(all_candidates,args.max_camera_candidates,reference); triggers = trigger_candidates(args); quality = QualityConfig()
    output=Path(args.output).expanduser().resolve(); output.mkdir(parents=True,exist_ok=False); captures=output/'captures'; captures.mkdir(); config,base=Config.load(args.config)
    off_count=max(1,round(args.off_seconds*args.estimated_fps)); on_count=max(1,round(args.on_seconds*args.estimated_fps)); estimated_bytes=len(camera_candidates)*(off_count+on_count)*2048*2
    free=shutil.disk_usage(output).free; reserve=int(args.free_space_reserve_gb*1024**3)
    if estimated_bytes+reserve>free: raise RuntimeError(f"Insufficient storage. Need approximately {estimated_bytes/1024**3:.2f} GiB plus reserve; free {free/1024**3:.2f} GiB")
    manifest={"status":"INITIALIZING","arguments":vars(args),"full_camera_grid_size":len(all_candidates),"tested_camera_candidates":[asdict(c) for c in camera_candidates],"trigger_candidate_count":len(triggers),"estimated_capture_bytes":estimated_bytes,"quality_configuration":asdict(quality)};atomic_json(output/'run_manifest.json',manifest)
    print(f"Full camera grid: {len(all_candidates)} combinations; space-filling test set: {len(camera_candidates)}; trigger grid: {len(triggers)}")
    print("Mechanically secure the fixture. Keep scan and laser OFF."); input("Press Enter to begin OFF acquisition: ")
    rows={c.candidate_id:{"camera":asdict(c)} for c in camera_candidates}; started=time.monotonic()
    with LismCamera(config,base) as camera:
        original=camera.configuration(); primary=None
        try:
            manifest['status']='OFF';atomic_json(output/'run_manifest.json',manifest)
            for i,candidate in enumerate(camera_candidates,1):
                print(f"\nOFF [{i}/{len(camera_candidates)}] {candidate.candidate_id}");observed=camera.apply_temporary_configuration(**candidate.apply_args());camera.start();path=captures/f'{candidate.candidate_id}_off.npy';capture(camera,off_count,args.settle_frames,path,'OFF');camera.stop();rows[candidate.candidate_id]['observed']=observed;rows[candidate.candidate_id]['off_path']=str(path);atomic_json(output/'candidate_results.json',list(rows.values()))
            print("\nOFF complete. Enable normal scan and laser at fixed CENTER.");input("Wait for stabilization, then press Enter. Leave laser ON: ")
            manifest['status']='ON_AND_ANALYSIS';atomic_json(output/'run_manifest.json',manifest)
            for i,candidate in enumerate(camera_candidates,1):
                elapsed=time.monotonic()-started
                if elapsed>=args.duration_minutes*60: print('Time budget reached before all candidates.');break
                print(f"\nON [{i}/{len(camera_candidates)}] {candidate.candidate_id}");camera.apply_temporary_configuration(**candidate.apply_args());camera.start();on_path=captures/f'{candidate.candidate_id}_on.npy';on=capture(camera,on_count,args.settle_frames,on_path,'ON');camera.stop();off=np.load(rows[candidate.candidate_id]['off_path'],mmap_mode='r',allow_pickle=False);search=select_trigger(off,on,triggers,quality,args.trigger_training_frames,args.validation_fraction,args.max_false_fraction);rows[candidate.candidate_id]['on_path']=str(on_path);rows[candidate.candidate_id]['trigger_search']=search;atomic_json(output/'candidate_results.json',list(rows.values()))
            completed=[row for row in rows.values() if 'trigger_search' in row];ranked=sorted(completed,key=camera_rank,reverse=True);atomic_json(output/'camera_trigger_ranked.json',ranked)
            recommendation=next((row for row in ranked if row['trigger_search']['best']['validation_accepted'] and row['trigger_search']['best']['validation_on']['good_frames']>=args.minimum_good_frames),None);atomic_json(output/'recommended_camera_and_trigger.json',recommendation)
            manifest['status']='COMPLETE' if recommendation else 'COMPLETE_NO_STATISTICAL_RECOMMENDATION';manifest['elapsed_seconds']=time.monotonic()-started;manifest['completed_camera_candidates']=len(completed);atomic_json(output/'run_manifest.json',manifest)
            if not args.keep_all_captures:
                keep=set()
                for row in ranked[:5]:keep.update((Path(row['off_path']),Path(row['on_path'])))
                for path in captures.glob('*.npy'):
                    if path not in keep:path.unlink()
        except BaseException as exc:
            primary=exc;manifest['status']='FAILED_OR_CANCELLED';manifest['error']=str(exc);manifest['elapsed_seconds']=time.monotonic()-started;atomic_json(output/'run_manifest.json',manifest);raise
        finally:
            try:camera.stop()
            except Exception:pass
            try:atomic_json(output/'restored_configuration.json',camera.restore_configuration(original))
            except Exception as exc:
                atomic_json(output/'restore_error.json',{'error':str(exc)})
                if primary is None:raise
    print(f"Results: {output}")
    print(json.dumps(json_safe(recommendation),indent=2) if recommendation else 'No statistically supported recommendation.')

if __name__=='__main__':main()
