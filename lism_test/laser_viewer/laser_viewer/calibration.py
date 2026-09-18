from __future__ import annotations
import json
import os
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
import numpy as np
from .models import DeviceSignature

VALID_POLARITIES = {"positive", "negative"}

@dataclass(frozen=True)
class Calibration:
    dark_mean: np.ndarray
    dark_noise: np.ndarray
    laser_reference: np.ndarray | None
    signature: DeviceSignature
    created_utc: str
    laser_created_utc: str | None = None
    polarity: str | None = None
    reference_start: int | None = None
    reference_end: int | None = None
    reference_centroid: float | None = None

    @property
    def has_laser(self) -> bool:
        return self.laser_reference is not None and self.polarity in VALID_POLARITIES

class CalibrationStore:
    def __init__(self, directory: Path):
        self.directory = directory
        self.path = directory / "active_calibration.npz"
        self.meta_path = directory / "active_calibration.json"

    def save(self, calibration: Calibration) -> None:
        self.directory.mkdir(parents=True, exist_ok=True)
        tmp_npz = self.path.with_suffix(".npz.tmp")
        tmp_json = self.meta_path.with_suffix(".json.tmp")
        with tmp_npz.open("wb") as f:
            np.savez_compressed(
                f,
                dark_mean=calibration.dark_mean,
                dark_noise=calibration.dark_noise,
                laser_reference=np.array([]) if calibration.laser_reference is None else calibration.laser_reference,
            )
            f.flush(); os.fsync(f.fileno())
        meta = {
            "schema_version": 2,
            "created_utc": calibration.created_utc,
            "laser_created_utc": calibration.laser_created_utc,
            "polarity": calibration.polarity,
            "reference_start": calibration.reference_start,
            "reference_end": calibration.reference_end,
            "reference_centroid": calibration.reference_centroid,
            "signature": calibration.signature.as_dict(),
        }
        with tmp_json.open("w", encoding="utf-8") as f:
            json.dump(meta, f, indent=2); f.flush(); os.fsync(f.fileno())
        os.replace(tmp_npz, self.path); os.replace(tmp_json, self.meta_path)

    def load(self) -> Calibration | None:
        if not self.path.exists() or not self.meta_path.exists():
            return None
        meta = json.loads(self.meta_path.read_text(encoding="utf-8"))
        schema = meta.get("schema_version")
        if schema not in (1, 2):
            raise ValueError("Unsupported calibration schema")
        with np.load(self.path, allow_pickle=False) as data:
            laser = data["laser_reference"].astype(np.float64)
            return Calibration(
                dark_mean=data["dark_mean"].astype(np.float64),
                dark_noise=data["dark_noise"].astype(np.float64),
                laser_reference=None if laser.size == 0 else laser,
                signature=DeviceSignature(**meta["signature"]),
                created_utc=meta["created_utc"],
                laser_created_utc=meta.get("laser_created_utc"),
                polarity=meta.get("polarity"),
                reference_start=meta.get("reference_start"),
                reference_end=meta.get("reference_end"),
                reference_centroid=meta.get("reference_centroid"),
            )

    @staticmethod
    def validate(calibration: Calibration, signature: DeviceSignature) -> None:
        if calibration.signature != signature:
            raise ValueError(f"Calibration does not match current device/configuration. saved={calibration.signature.as_dict()} current={signature.as_dict()}")
        if calibration.dark_mean.size != signature.pixel_count or calibration.dark_noise.size != signature.pixel_count:
            raise ValueError("Calibration pixel count is inconsistent")

def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()

def build_dark(frames: np.ndarray, signature: DeviceSignature) -> Calibration:
    if frames.ndim != 2 or frames.shape[1] != signature.pixel_count:
        raise ValueError("Dark frames have the wrong shape")
    if frames.shape[0] < 2:
        raise ValueError("At least two dark frames are required")
    return Calibration(np.median(frames, axis=0), np.maximum(np.std(frames, axis=0, ddof=1), 1.0), None, signature, utc_now())

def _strongest_contiguous_region(mask: np.ndarray, strength: np.ndarray) -> tuple[int, int]:
    padded = np.pad(mask.astype(np.int8), (1, 1))
    changes = np.diff(padded)
    starts = np.flatnonzero(changes == 1)
    ends = np.flatnonzero(changes == -1) - 1
    if starts.size == 0:
        raise ValueError("Laser calibration did not produce a contiguous above-noise region")
    scores = np.array([strength[s:e + 1].sum() for s, e in zip(starts, ends)])
    index = int(np.argmax(scores))
    return int(starts[index]), int(ends[index])

def _smooth(values: np.ndarray, width: int = 5) -> np.ndarray:
    if width <= 1:
        return values.copy()
    kernel = np.ones(width, dtype=np.float64) / width
    return np.convolve(values, kernel, mode="same")

def add_laser(
    calibration: Calibration,
    frames: np.ndarray,
    noise_sigma: float = 6.0,
    minimum_signal: float = 100.0,
    calibration_noise_sigma: float = 3.0,
    calibration_minimum_signal: float = 20.0,
    calibration_relative_threshold: float = 0.25,
) -> Calibration:
    """Build laser reference with a more permissive calibration-only threshold.

    Runtime detection remains governed by noise_sigma and minimum_signal. The
    calibration threshold is intentionally lower because calibration averages
    many frames and only needs to establish polarity and a reference region.
    """
    if frames.ndim != 2 or frames.shape[1] != calibration.signature.pixel_count:
        raise ValueError("Laser frames have the wrong shape")
    if not 0.0 < calibration_relative_threshold < 1.0:
        raise ValueError("calibration_relative_threshold must be between 0 and 1")

    laser_median = np.median(frames, axis=0)
    signed_delta = laser_median - calibration.dark_mean
    positive = np.maximum(signed_delta, 0.0)
    negative = np.maximum(-signed_delta, 0.0)

    # Compare the strongest 1 percent of pixels. This is more reliable than one
    # extreme pixel while still supporting a narrow laser response.
    top_count = max(4, calibration.signature.pixel_count // 100)
    positive_score = float(np.mean(np.partition(positive, -top_count)[-top_count:]))
    negative_score = float(np.mean(np.partition(negative, -top_count)[-top_count:]))
    polarity = "negative" if negative_score > positive_score else "positive"
    signal = negative if polarity == "negative" else positive
    smoothed = _smooth(signal, 5)

    calibration_threshold = np.maximum(
        calibration.dark_noise * calibration_noise_sigma,
        calibration_minimum_signal,
    )
    mask = smoothed > calibration_threshold

    peak = float(np.max(smoothed))
    peak_index = int(np.argmax(smoothed))
    peak_noise = float(calibration.dark_noise[peak_index])
    peak_snr = peak / max(peak_noise, 1.0)

    # If per-pixel dark noise is inflated by frame-to-frame common-mode motion,
    # permit a shape-based threshold, but only when a meaningful absolute and
    # noise-relative response is present. This avoids silently accepting a
    # laser-off calibration.
    if not np.any(mask) and peak >= calibration_minimum_signal and peak_snr >= 2.0:
        mask = smoothed >= max(
            calibration_minimum_signal,
            peak * calibration_relative_threshold,
        )

    if not np.any(mask):
        raise ValueError(
            "Laser calibration found no usable response. "
            f"polarity_candidate={polarity}, peak_delta={peak:.1f}, "
            f"peak_noise={peak_noise:.1f}, peak_snr={peak_snr:.2f}, "
            f"positive_score={positive_score:.1f}, negative_score={negative_score:.1f}. "
            "Confirm the laser is present and stable, then inspect clipping and "
            "consider lowering calibration_minimum_signal or "
            "calibration_noise_sigma in config/default.json."
        )

    start, end = _strongest_contiguous_region(mask, smoothed)
    weights = signal[start:end + 1]
    total = float(weights.sum())
    if total <= 0.0:
        raise ValueError("Laser calibration region has no positive integrated signal")
    centroid = float(np.dot(np.arange(start, end + 1), weights) / total)
    return Calibration(
        dark_mean=calibration.dark_mean,
        dark_noise=calibration.dark_noise,
        laser_reference=signal,
        signature=calibration.signature,
        created_utc=calibration.created_utc,
        laser_created_utc=utc_now(),
        polarity=polarity,
        reference_start=start,
        reference_end=end,
        reference_centroid=centroid,
    )
