from __future__ import annotations
import numpy as np
from .models import Detection

def _regions(mask: np.ndarray):
    padded = np.pad(mask.astype(np.int8), (1, 1))
    changes = np.diff(padded)
    starts = np.flatnonzero(changes == 1)
    ends = np.flatnonzero(changes == -1) - 1
    return list(zip(starts.tolist(), ends.tolist()))

def detect(frame: np.ndarray, calibration, noise_sigma: float, minimum_signal: float, half_width: int, minimum_region_width: int = 1, maximum_region_width: int = 256, clip_margin: int = 16) -> Detection:
    raw = frame.astype(np.float64)
    polarity = calibration.polarity or "positive"
    signed = calibration.dark_mean - raw if polarity == "negative" else raw - calibration.dark_mean
    corrected = np.maximum(signed, 0.0)
    thresholds = np.maximum(calibration.dark_noise * noise_sigma, minimum_signal)
    candidates = [(s, e) for s, e in _regions(corrected > thresholds) if minimum_region_width <= e - s + 1 <= maximum_region_width]
    low_clipped = bool(np.min(raw) <= clip_margin)
    high_clipped = bool(np.max(raw) >= 65535 - clip_margin)
    if not candidates:
        amplitude = float(np.max(corrected)) if corrected.size else 0.0
        threshold = float(thresholds[int(np.argmax(corrected))]) if corrected.size else 0.0
        return Detection(False, None, None, amplitude, threshold, 0.0, low_clipped, high_clipped, None, None, 0, 0.0, polarity, corrected)
    def region_score(region):
        s, e = region
        integrated = float(np.sum(np.maximum(corrected[s:e + 1] - thresholds[s:e + 1], 0.0)))
        if calibration.reference_centroid is None:
            return integrated
        center = 0.5 * (s + e)
        distance_penalty = 1.0 + abs(center - calibration.reference_centroid) / max(1.0, calibration.reference_end - calibration.reference_start + 1)
        return integrated / distance_penalty
    start, end = max(candidates, key=region_score)
    region = corrected[start:end + 1]
    local_peak = int(np.argmax(region)); peak = start + local_peak
    amplitude = float(corrected[peak]); threshold = float(thresholds[peak])
    lo = max(start, peak - half_width); hi = min(end + 1, peak + half_width + 1)
    weights = np.maximum(corrected[lo:hi] - thresholds[lo:hi], 0.0)
    total = float(weights.sum())
    centroid = float(peak) if total <= 0 else float(np.dot(np.arange(lo, hi), weights) / total)
    integrated_signal = float(region.sum())
    snr = amplitude / max(float(calibration.dark_noise[peak]), 1.0)
    return Detection(True, peak, centroid, amplitude, threshold, snr, low_clipped, high_clipped, start, end, end - start + 1, integrated_signal, polarity, corrected)
