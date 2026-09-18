from __future__ import annotations

import argparse
import hashlib
import itertools
import json
import os
import shutil
import time
from dataclasses import asdict, dataclass
from pathlib import Path

import numpy as np


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
        return {
            "adc_bias": self.bias,
            "adc_gain": self.gain,
            "st_high": self.st_high,
            "st_low": self.st_low,
            "edge_delay": self.edge_delay,
        }


def json_safe(value):
    if isinstance(value, np.ndarray):
        return value.tolist()
    if isinstance(value, np.generic):
        return value.item()
    if isinstance(value, Path):
        return str(value)
    if isinstance(value, dict):
        return {str(key): json_safe(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [json_safe(item) for item in value]
    return value


def atomic_json(path: Path, value) -> None:
    temporary = path.with_suffix(path.suffix + ".tmp")
    with temporary.open("w", encoding="utf-8") as handle:
        json.dump(json_safe(value), handle, indent=2, allow_nan=False)
        handle.flush()
        os.fsync(handle.fileno())
    os.replace(temporary, path)


def sha256_file(path: Path, chunk_size: int = 4 * 1024 * 1024) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        while True:
            chunk = handle.read(chunk_size)
            if not chunk:
                break
            digest.update(chunk)
    return digest.hexdigest()


def parse_ints(text: str) -> list[int]:
    return [int(value.strip()) for value in text.split(",") if value.strip()]


def all_candidates(args) -> list[CameraCandidate]:
    return [
        CameraCandidate(*values)
        for values in itertools.product(
            parse_ints(args.biases),
            parse_ints(args.gains),
            parse_ints(args.st_highs),
            parse_ints(args.st_lows),
            parse_ints(args.edge_delays),
        )
    ]


def normalized(candidate: CameraCandidate, candidates: list[CameraCandidate]) -> np.ndarray:
    output = []
    for field in ("bias", "gain", "st_high", "st_low", "edge_delay"):
        values = [getattr(item, field) for item in candidates]
        span = max(values) - min(values)
        output.append(0.0 if span == 0 else (getattr(candidate, field) - min(values)) / span)
    return np.asarray(output, dtype=np.float64)


def space_filling(candidates: list[CameraCandidate], count: int, reference: CameraCandidate) -> list[CameraCandidate]:
    if len(candidates) <= count:
        return candidates
    vectors = {candidate: normalized(candidate, candidates) for candidate in candidates}
    selected = [min(candidates, key=lambda candidate: np.linalg.norm(vectors[candidate] - normalized(reference, candidates)))]
    remaining = [candidate for candidate in candidates if candidate not in selected]
    while remaining and len(selected) < count:
        chosen = max(
            remaining,
            key=lambda candidate: min(np.linalg.norm(vectors[candidate] - vectors[existing]) for existing in selected),
        )
        selected.append(chosen)
        remaining.remove(chosen)
    return selected


def load_plan(path: Path) -> list[CameraCandidate]:
    data = json.loads(path.read_text(encoding="utf-8"))
    rows = data.get("candidates", data) if isinstance(data, dict) else data
    return [CameraCandidate(**row) for row in rows]


def capture(camera, frame_count: int, settle_frames: int, directory: Path, state: str) -> dict:
    frame_path = directory / f"{state}_frames.npy"
    timestamp_path = directory / f"{state}_timestamps.npy"
    frames = np.lib.format.open_memmap(frame_path, mode="w+", dtype=np.uint16, shape=(frame_count, 2048))
    timestamps = np.lib.format.open_memmap(timestamp_path, mode="w+", dtype=np.float64, shape=(frame_count,))
    for _ in range(settle_frames):
        camera.read_frame()
    started_monotonic = time.monotonic()
    started_utc = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    minimum = 65535
    maximum = 0
    high_rail_samples = 0
    for index in range(frame_count):
        frame = np.asarray(camera.read_frame(), dtype=np.uint16)
        if frame.shape != (2048,):
            raise RuntimeError(f"Expected a 2048-pixel frame, received {frame.shape}")
        frames[index] = frame
        timestamps[index] = time.monotonic()
        minimum = min(minimum, int(frame.min()))
        maximum = max(maximum, int(frame.max()))
        high_rail_samples += int(np.count_nonzero(frame >= 65503))
        if (index + 1) % 1000 == 0 or index + 1 == frame_count:
            elapsed = time.monotonic() - started_monotonic
            rate = (index + 1) / elapsed if elapsed else 0.0
            print(f"\r{state.upper()}: {index + 1:,}/{frame_count:,} frames, {rate:,.1f} fps", end="", flush=True)
    print()
    frames.flush()
    timestamps.flush()
    del frames, timestamps
    elapsed = time.monotonic() - started_monotonic
    summary = {
        "state": state,
        "frame_count": frame_count,
        "pixel_count": 2048,
        "dtype": "uint16",
        "capture_started_utc": started_utc,
        "elapsed_seconds": elapsed,
        "observed_fps": frame_count / elapsed if elapsed else None,
        "minimum_raw": minimum,
        "maximum_raw": maximum,
        "high_rail_fraction": high_rail_samples / (frame_count * 2048),
        "frames_file": frame_path.name,
        "timestamps_file": timestamp_path.name,
        "frames_sha256": sha256_file(frame_path),
        "timestamps_sha256": sha256_file(timestamp_path),
    }
    atomic_json(directory / f"{state}_summary.json", summary)
    return summary


def main(argv=None):
    from .camera import LismCamera
    from .config import Config

    parser = argparse.ArgumentParser(description="LISM capture-only camera sweep for later desktop analysis")
    parser.add_argument("--config", default="config/default.json")
    parser.add_argument("--output", required=True)
    parser.add_argument("--plan")
    parser.add_argument("--biases", default="246,254,262,270,278,286")
    parser.add_argument("--gains", default="15")
    parser.add_argument("--st-highs", default="7500,8000,8500,9000")
    parser.add_argument("--st-lows", default="50,100,150")
    parser.add_argument("--edge-delays", default="80,84,88,92,96")
    parser.add_argument("--max-candidates", type=int, default=36)
    parser.add_argument("--reference-bias", type=int, default=286)
    parser.add_argument("--reference-gain", type=int, default=15)
    parser.add_argument("--reference-st-high", type=int, default=8000)
    parser.add_argument("--reference-st-low", type=int, default=100)
    parser.add_argument("--reference-edge-delay", type=int, default=88)
    parser.add_argument("--off-seconds", type=float, default=5.0)
    parser.add_argument("--on-seconds", type=float, default=8.0)
    parser.add_argument("--estimated-fps", type=float, default=1000.0)
    parser.add_argument("--settle-frames", type=int, default=32)
    parser.add_argument("--free-space-reserve-gb", type=float, default=2.0)
    args = parser.parse_args(argv)

    output = Path(args.output).expanduser().resolve()
    output.mkdir(parents=True, exist_ok=False)
    candidates_root = output / "candidates"
    candidates_root.mkdir()
    if args.plan:
        candidates = load_plan(Path(args.plan).expanduser().resolve())
        full_count = len(candidates)
    else:
        complete = all_candidates(args)
        reference = CameraCandidate(args.reference_bias, args.reference_gain, args.reference_st_high, args.reference_st_low, args.reference_edge_delay)
        candidates = space_filling(complete, args.max_candidates, reference)
        full_count = len(complete)
    if not candidates:
        raise ValueError("No camera candidates were selected")
    off_frames = max(1, round(args.off_seconds * args.estimated_fps))
    on_frames = max(1, round(args.on_seconds * args.estimated_fps))
    estimated_bytes = len(candidates) * (off_frames + on_frames) * (2048 * 2 + 8)
    free_bytes = shutil.disk_usage(output).free
    reserve = int(args.free_space_reserve_gb * 1024**3)
    if estimated_bytes + reserve > free_bytes:
        raise RuntimeError(f"Insufficient disk space: need about {estimated_bytes / 1024**3:.2f} GiB plus reserve; free {free_bytes / 1024**3:.2f} GiB")

    config, base = Config.load(args.config)
    manifest = {
        "schema_version": 1,
        "status": "INITIALIZING",
        "created_utc": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "arguments": vars(args),
        "full_grid_count": full_count,
        "selected_candidate_count": len(candidates),
        "off_frames_per_candidate": off_frames,
        "on_frames_per_candidate": on_frames,
        "estimated_bytes": estimated_bytes,
        "candidates": [asdict(candidate) for candidate in candidates],
    }
    atomic_json(output / "run_manifest.json", manifest)
    atomic_json(output / "candidate_plan.json", {"schema_version": 1, "candidates": [asdict(candidate) for candidate in candidates]})
    status_rows = {candidate.candidate_id: {"candidate": asdict(candidate), "status": "PLANNED"} for candidate in candidates}
    atomic_json(output / "capture_status.json", list(status_rows.values()))

    print(f"Selected {len(candidates)} camera candidates from {full_count} possible combinations.")
    print(f"Estimated raw capture storage: {estimated_bytes / 1024**3:.2f} GiB")
    print("Mechanically secure the fixture and keep scan and laser OFF.")
    input("Press Enter to begin all OFF captures: ")

    with LismCamera(config, base) as camera:
        original = camera.configuration()
        atomic_json(output / "startup_configuration.json", original)
        primary_error = None
        try:
            manifest["status"] = "CAPTURING_OFF"
            atomic_json(output / "run_manifest.json", manifest)
            for index, candidate in enumerate(candidates, 1):
                print(f"\nOFF [{index}/{len(candidates)}] {candidate.candidate_id}")
                directory = candidates_root / candidate.candidate_id
                directory.mkdir()
                status_rows[candidate.candidate_id]["status"] = "OFF_IN_PROGRESS"
                atomic_json(output / "capture_status.json", list(status_rows.values()))
                observed = camera.apply_temporary_configuration(**candidate.apply_args())
                atomic_json(directory / "camera_configuration.json", {"requested": asdict(candidate), "observed": observed})
                camera.start()
                off_summary = capture(camera, off_frames, args.settle_frames, directory, "off")
                camera.stop()
                status_rows[candidate.candidate_id].update({"status": "OFF_COMPLETE", "off_summary": off_summary})
                atomic_json(output / "capture_status.json", list(status_rows.values()))

            print("\nAll OFF captures are complete.")
            print("Enable normal LAP scanning and laser emission at a fixed CENTER position.")
            input("Wait for stabilization, then press Enter. Leave the system unchanged until completion: ")

            manifest["status"] = "CAPTURING_ON"
            atomic_json(output / "run_manifest.json", manifest)
            for index, candidate in enumerate(candidates, 1):
                print(f"\nON [{index}/{len(candidates)}] {candidate.candidate_id}")
                directory = candidates_root / candidate.candidate_id
                status_rows[candidate.candidate_id]["status"] = "ON_IN_PROGRESS"
                atomic_json(output / "capture_status.json", list(status_rows.values()))
                observed = camera.apply_temporary_configuration(**candidate.apply_args())
                stored = json.loads((directory / "camera_configuration.json").read_text(encoding="utf-8"))
                stored["on_observed"] = observed
                atomic_json(directory / "camera_configuration.json", stored)
                camera.start()
                on_summary = capture(camera, on_frames, args.settle_frames, directory, "on")
                camera.stop()
                status_rows[candidate.candidate_id].update({"status": "COMPLETE", "on_summary": on_summary})
                atomic_json(output / "capture_status.json", list(status_rows.values()))

            manifest["status"] = "CAPTURE_COMPLETE"
            manifest["completed_utc"] = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
            manifest["completed_candidates"] = sum(row["status"] == "COMPLETE" for row in status_rows.values())
            atomic_json(output / "run_manifest.json", manifest)
        except BaseException as exc:
            primary_error = exc
            manifest["status"] = "FAILED_OR_CANCELLED"
            manifest["error"] = str(exc)
            atomic_json(output / "run_manifest.json", manifest)
            raise
        finally:
            try:
                camera.stop()
            except Exception:
                pass
            try:
                restored = camera.restore_configuration(original)
                atomic_json(output / "restored_configuration.json", restored)
            except Exception as exc:
                atomic_json(output / "restore_error.json", {"error": str(exc)})
                if primary_error is None:
                    raise

    print(f"Capture complete: {output}")
    print("Transfer the complete directory to the desktop before analysis.")


if __name__ == "__main__":
    main()
