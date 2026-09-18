from __future__ import annotations

import argparse
import csv
import hashlib
import html
import json
import math
import os
import queue
import statistics
import threading
import time
import uuid
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlparse

import numpy as np

from .downward_detector import QualityConfig, TriggerConfig, detect_frame

CAMERA = {"bias": 282, "gain": 15, "st_high": 9000, "st_low": 100, "edge_delay": 88}
TRIGGER = {
    "threshold": 60000, "minimum_width": 2, "maximum_width": 512,
    "minimum_integrated_drop": 10000, "maximum_gap": 3,
    "edge_window": 5, "edge_minimum_active": 3,
    "quantile_low": 0.10, "quantile_high": 0.90,
    "core_fraction": 0.80, "core_minimum_width": 2,
    "maximum_center_disagreement": 12.0,
}
METHODS = {
    "sustained_edge": "sustained_edge_center",
    "boundary": "boundary_center",
    "drop_quantile": "quantile_center",
    "active_median": "active_median_center",
    "drop_weighted": "centroid",
    "deep_core": "deep_core_center",
    "selected": "selected_center",
}
FEATURES = [
    "width", "integrated_drop", "peak_drop", "minimum_raw", "active_coverage",
    "largest_internal_gap", "filled_gap_count", "filled_gap_pixels",
    "background_rail_fraction", "interior_low_fraction", "shape_score",
    "center_disagreement",
]
REASONS = {
    "false_trigger", "wrong_detector_region", "multiple_footprints",
    "footprint_outside_sensor", "left_edge_ambiguous", "right_edge_ambiguous",
    "both_edges_ambiguous", "corrupted_frame", "duplicate_or_residual", "other",
}
OUTLIER_DISPOSITIONS = {
    "unreviewed", "valid_difficult", "manual_annotation_corrected",
    "wrong_detector_region", "partial_footprint", "merged_responses",
    "corrupted_or_residual", "should_be_rejected", "unexplained",
}
FALSE_FAMILIES = {
    "unreviewed", "no_coherent_footprint", "fixed_sensor_artifact",
    "single_deep_glitch", "broad_baseline_disturbance", "fragmented_noise",
    "wrong_region_stronger_than_laser", "multiple_regions",
    "residual_or_duplicate_transfer", "malformed_frame", "unknown",
}


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def jsonable(value: Any) -> Any:
    if isinstance(value, np.ndarray): return value.tolist()
    if isinstance(value, np.generic): return value.item()
    if isinstance(value, Path): return str(value)
    if isinstance(value, dict): return {str(k): jsonable(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)): return [jsonable(v) for v in value]
    if isinstance(value, float) and not math.isfinite(value): return None
    return value


def atomic_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_suffix(path.suffix + ".tmp")
    with temp.open("w", encoding="utf-8") as stream:
        json.dump(jsonable(value), stream, indent=2, sort_keys=True, allow_nan=False)
        stream.flush(); os.fsync(stream.fileno())
    os.replace(temp, path)


def frame_hash(frame: np.ndarray) -> str:
    return hashlib.sha256(np.asarray(frame, dtype=np.uint16).tobytes()).hexdigest()


def detector_payload(result: Any) -> dict[str, Any]:
    region = result.selected_region
    if region is None: return {}
    names = set(FEATURES + list(METHODS.values()) + [
        "start", "end", "sustained_left_edge", "sustained_right_edge",
        "quantile_low_position", "quantile_high_position", "deep_core_start",
        "deep_core_end", "selected_center_method", "center_confidence",
        "measurement_quality", "interior_p90",
    ])
    payload = {name: getattr(region, name, None) for name in names}
    if payload.get("minimum_raw") is not None:
        payload["peak_drop"] = 65535.0 - float(payload["minimum_raw"])
    return jsonable(payload)


@dataclass(frozen=True)
class FrameRecord:
    study_frame_id: str
    sequence: int
    camera_frame_number: int
    monotonic_timestamp: float
    captured_utc: str
    event_id: str
    frame_file: str
    frame_sha256: str
    detection: dict[str, Any]


class StudyStore:
    def __init__(self, directory: Path, manifest: dict[str, Any] | None = None):
        self.directory = Path(directory)
        self.frames_directory = self.directory / "raw_frames"
        self.annotations_directory = self.directory / "annotations"
        self.records_file = self.directory / "frames.jsonl"
        self.audit_file = self.directory / "annotation_audit.jsonl"
        self.lock = threading.RLock()
        self.directory.mkdir(parents=True, exist_ok=True)
        self.frames_directory.mkdir(exist_ok=True)
        self.annotations_directory.mkdir(exist_ok=True)
        if manifest is not None:
            if (self.directory / "study_manifest.json").exists():
                raise ValueError("Study directory already exists")
            atomic_json(self.directory / "study_manifest.json", {
                **manifest, "schema_version": 2, "created_utc": utc_now(),
                "analysis_version": "4.0",
            })
        self.records = self._load_records()

    @property
    def manifest(self) -> dict[str, Any]:
        return json.loads((self.directory / "study_manifest.json").read_text(encoding="utf-8"))

    def _load_records(self) -> list[FrameRecord]:
        if not self.records_file.exists(): return []
        return [FrameRecord(**json.loads(line)) for line in self.records_file.read_text(encoding="utf-8").splitlines() if line]

    def append_frame(self, frame, camera_frame_number, timestamp, detection, event_id) -> FrameRecord:
        values = np.asarray(frame, dtype=np.uint16)
        if values.ndim != 1 or values.size == 0: raise ValueError("Frame must be a non-empty 1-D array")
        with self.lock:
            sequence = len(self.records) + 1
            frame_id = f"frame-{sequence:06d}-{uuid.uuid4().hex[:8]}"
            relative = f"raw_frames/{frame_id}.npy"
            destination = self.directory / relative
            temporary = destination.with_suffix(".npy.tmp")
            with temporary.open("wb") as stream:
                np.save(stream, values, allow_pickle=False); stream.flush(); os.fsync(stream.fileno())
            os.replace(temporary, destination)
            record = FrameRecord(frame_id, sequence, int(camera_frame_number), float(timestamp), utc_now(), str(event_id), relative, frame_hash(values), detection)
            with self.records_file.open("a", encoding="utf-8") as stream:
                stream.write(json.dumps(jsonable(asdict(record)), sort_keys=True) + "\n"); stream.flush(); os.fsync(stream.fileno())
            self.records.append(record)
            return record

    def load_frame(self, index: int) -> tuple[FrameRecord, np.ndarray]:
        if not 0 <= index < len(self.records): raise IndexError("Frame index is out of range")
        record = self.records[index]
        values = np.load(self.directory / record.frame_file, allow_pickle=False)
        if frame_hash(values) != record.frame_sha256: raise IOError("Raw-frame integrity check failed")
        return record, values

    def annotation(self, frame_id: str) -> dict[str, Any] | None:
        path = self.annotations_directory / f"{frame_id}.json"
        return json.loads(path.read_text(encoding="utf-8")) if path.exists() else None

    def save_annotation(self, frame_id: str, body: dict[str, Any]) -> dict[str, Any]:
        if frame_id not in {record.study_frame_id for record in self.records}: raise ValueError("Unknown study frame")
        old = self.annotation(frame_id) or {}
        status = str(body.get("status", old.get("status", "")))
        if status not in {"accepted", "uncertain", "rejected"}: raise ValueError("Invalid annotation status")
        left, right = body.get("manual_left_edge", old.get("manual_left_edge")), body.get("manual_right_edge", old.get("manual_right_edge"))
        reason = body.get("rejection_reason", old.get("rejection_reason"))
        if status == "rejected":
            left = right = None
            if reason not in REASONS: raise ValueError("A valid rejection reason is required")
        else:
            if left is None or right is None: raise ValueError("Both manual edges are required")
            left, right = sorted((float(left), float(right)))
            if not 0 <= left < right <= 2047: raise ValueError("Manual edges must satisfy 0 <= left < right <= 2047")
            reason = None
        outlier_disposition = str(body.get("outlier_disposition", old.get("outlier_disposition", "unreviewed")))
        false_family = str(body.get("false_trigger_family", old.get("false_trigger_family", "unreviewed")))
        if outlier_disposition not in OUTLIER_DISPOSITIONS: raise ValueError("Invalid outlier disposition")
        if false_family not in FALSE_FAMILIES: raise ValueError("Invalid false-trigger family")
        annotation = {
            "study_frame_id": frame_id, "status": status,
            "manual_left_edge": left, "manual_right_edge": right,
            "manual_midpoint": None if left is None else (left + right) / 2.0,
            "manual_width": None if left is None else right - left,
            "confidence": str(body.get("confidence", old.get("confidence", "high"))),
            "rejection_reason": reason,
            "detector_region_assessment": str(body.get("detector_region_assessment", old.get("detector_region_assessment", "not_assessed"))),
            "note": str(body.get("note", old.get("note", "")))[:4000],
            "blinded_at_initial_save": bool(old.get("blinded_at_initial_save", body.get("blinded_at_initial_save", True))),
            "outlier_disposition": outlier_disposition,
            "false_trigger_family": false_family,
            "review_note": str(body.get("review_note", old.get("review_note", "")))[:4000],
            "reviewed_utc": utc_now() if outlier_disposition != "unreviewed" or false_family != "unreviewed" else old.get("reviewed_utc"),
            "annotation_utc": utc_now(), "revision": int(old.get("revision", 0)) + 1,
        }
        atomic_json(self.annotations_directory / f"{frame_id}.json", annotation)
        with self.audit_file.open("a", encoding="utf-8") as stream:
            stream.write(json.dumps({"old": old or None, "new": annotation}, sort_keys=True) + "\n")
        return annotation

    def progress(self) -> dict[str, int]:
        values = [self.annotation(record.study_frame_id) for record in self.records]
        return {
            "captured": len(values), "annotated": sum(v is not None for v in values),
            "accepted": sum(bool(v and v["status"] == "accepted") for v in values),
            "uncertain": sum(bool(v and v["status"] == "uncertain") for v in values),
            "rejected": sum(bool(v and v["status"] == "rejected") for v in values),
            "outliers_reviewed": sum(bool(v and v.get("outlier_disposition", "unreviewed") != "unreviewed") for v in values),
            "false_triggers_reviewed": sum(bool(v and v.get("false_trigger_family", "unreviewed") != "unreviewed") for v in values),
        }


class CaptureWorker:
    def __init__(self, camera, store: StudyStore, target: int):
        self.camera, self.store, self.target = camera, store, target
        self.running = threading.Event(); self.queue = queue.Queue(maxsize=256)
        self.capture_thread = self.writer_thread = None
        self.frames_seen = self.qualified = self.dropped = 0; self.error = None

    def start(self):
        self.running.set()
        self.writer_thread = threading.Thread(target=self._writer, daemon=True, name="centroid-study-writer")
        self.capture_thread = threading.Thread(target=self._capture, daemon=True, name="centroid-study-capture")
        self.writer_thread.start(); self.capture_thread.start()

    def _capture(self):
        try:
            config, quality = TriggerConfig.from_mapping(TRIGGER), QualityConfig()
            while self.running.is_set() and self.qualified < self.target:
                frame = np.asarray(self.camera.read_frame(), dtype=np.uint16).copy()
                stamp = time.monotonic(); self.frames_seen += 1
                result = detect_frame(frame, config, quality)
                if result.selected_region is None: continue
                item = (frame, self.frames_seen, stamp, detector_payload(result), f"event-{int(stamp / 0.02015):012d}")
                try: self.queue.put_nowait(item); self.qualified += 1
                except queue.Full: self.dropped += 1
        except Exception as exc: self.error = str(exc)
        finally: self.running.clear(); self.queue.put(None)

    def _writer(self):
        while True:
            item = self.queue.get()
            if item is None: return
            try: self.store.append_frame(*item)
            except Exception as exc: self.error = str(exc); self.running.clear()

    def stop(self):
        self.running.clear()
        if self.capture_thread: self.capture_thread.join(3)
        if self.writer_thread: self.writer_thread.join(5)

    def state(self):
        return {"running": self.running.is_set(), "frames_seen": self.frames_seen, "qualified": self.qualified,
                "persisted": len(self.store.records), "target": self.target, "queue_depth": self.queue.qsize(),
                "dropped": self.dropped, "error": self.error}


def descriptive(values: list[float]) -> dict[str, Any]:
    if not values: return {"n": 0}
    array = np.asarray(values, dtype=float)
    return {"n": len(values), "median": float(np.median(array)), "mean": float(np.mean(array)),
            "q25": float(np.percentile(array, 25)), "q75": float(np.percentile(array, 75)),
            "p05": float(np.percentile(array, 5)), "p95": float(np.percentile(array, 95)),
            "minimum": float(np.min(array)), "maximum": float(np.max(array))}


def method_statistics(errors: list[float], eligible: int) -> dict[str, Any]:
    if not errors: return {"eligible": eligible, "valid": 0, "invalid": eligible, "valid_fraction": 0 if eligible else None}
    absolute = [abs(v) for v in errors]; median = statistics.median(errors)
    mad = statistics.median(abs(v - median) for v in errors)
    return {"eligible": eligible, "valid": len(errors), "invalid": eligible - len(errors),
            "valid_fraction": len(errors) / eligible if eligible else None,
            "mean_signed_error": statistics.fmean(errors), "median_signed_error": median,
            "mean_absolute_error": statistics.fmean(absolute), "median_absolute_error": statistics.median(absolute),
            "rmse": math.sqrt(statistics.fmean(v * v for v in errors)), "error_mad": mad,
            "absolute_error_p95": float(np.percentile(absolute, 95)), "maximum_absolute_error": max(absolute),
            "within_1": sum(v <= 1 for v in absolute) / len(absolute),
            "robust_outliers": sum(abs(v - median) > max(5 * mad, 5.0) for v in errors)}


def candidate_thresholds(accepted: list[float], rejected: list[float], feature: str) -> list[dict[str, Any]]:
    if not accepted or not rejected: return []
    values = sorted(set(accepted + rejected)); candidates = []
    if len(values) > 80:
        candidates = [float(np.percentile(values, q)) for q in range(5, 100, 5)]
    else:
        candidates = [(a + b) / 2 for a, b in zip(values, values[1:])]
    results = []
    for threshold in candidates:
        for direction in ("minimum", "maximum"):
            keep = (lambda v: v >= threshold) if direction == "minimum" else (lambda v: v <= threshold)
            true_retained = sum(keep(v) for v in accepted) / len(accepted)
            false_rejected = sum(not keep(v) for v in rejected) / len(rejected)
            if true_retained >= 0.90:
                results.append({"feature": feature, "direction": direction, "threshold": threshold,
                                "accepted_retention": true_retained, "false_rejection": false_rejected,
                                "score": true_retained + false_rejected})
    return sorted(results, key=lambda row: (row["score"], row["false_rejection"]), reverse=True)[:3]


def analyze_study(store: StudyStore) -> dict[str, Any]:
    frame_rows, accepted_records, rejected_records = [], [], []
    rejection_counts, outlier_review_counts, false_family_counts = {}, {}, {}
    for record in store.records:
        annotation = store.annotation(record.study_frame_id)
        if not annotation: continue
        if annotation["status"] == "rejected":
            rejected_records.append((record, annotation))
            reason = annotation.get("rejection_reason", "other"); rejection_counts[reason] = rejection_counts.get(reason, 0) + 1
            family = annotation.get("false_trigger_family", "unreviewed"); false_family_counts[family] = false_family_counts.get(family, 0) + 1
            continue
        if annotation["status"] != "accepted": continue
        accepted_records.append((record, annotation))
        disposition = annotation.get("outlier_disposition", "unreviewed")
        outlier_review_counts[disposition] = outlier_review_counts.get(disposition, 0) + 1
        row = {"study_frame_id": record.study_frame_id, "sequence": record.sequence, "event_id": record.event_id,
               "manual_left_edge": annotation["manual_left_edge"], "manual_right_edge": annotation["manual_right_edge"],
               "manual_midpoint": annotation["manual_midpoint"], "manual_width": annotation["manual_width"],
               "outlier_disposition": disposition}
        row.update({f"feature_{feature}": record.detection.get(feature) for feature in FEATURES})
        for method, field in METHODS.items():
            center = record.detection.get(field); row[method] = center
            row[f"{method}_error"] = None if center is None else float(center) - annotation["manual_midpoint"]
        start, end = record.detection.get("start"), record.detection.get("end")
        row["detector_left_error"] = None if start is None else float(start) - annotation["manual_left_edge"]
        row["detector_right_error"] = None if end is None else float(end) - annotation["manual_right_edge"]
        if start is not None and end is not None:
            intersection = max(0.0, min(float(end), annotation["manual_right_edge"]) - max(float(start), annotation["manual_left_edge"]))
            union = max(float(end), annotation["manual_right_edge"]) - min(float(start), annotation["manual_left_edge"])
            row["window_iou"] = intersection / union if union else 1.0
        frame_rows.append(row)

    method_results = {method: method_statistics([row[f"{method}_error"] for row in frame_rows if row[f"{method}_error"] is not None], len(frame_rows)) for method in METHODS}
    ranking = sorted([method for method, result in method_results.items() if result.get("median_absolute_error") is not None], key=lambda method: (method_results[method]["median_absolute_error"], method_results[method]["absolute_error_p95"], -method_results[method]["valid_fraction"]))

    leader = ranking[0] if ranking else "sustained_edge"
    leader_errors = [row[f"{leader}_error"] for row in frame_rows if row[f"{leader}_error"] is not None]
    leader_median = statistics.median(leader_errors) if leader_errors else 0.0
    leader_mad = statistics.median(abs(v - leader_median) for v in leader_errors) if leader_errors else 0.0
    outlier_limit = max(5.0, 5 * leader_mad)
    for row in frame_rows:
        error = row.get(f"{leader}_error")
        row["outlier_score"] = None if error is None else abs(error - leader_median)
        row["is_robust_outlier"] = bool(error is not None and abs(error - leader_median) > outlier_limit)
        row["squared_error_contribution"] = None if error is None else error * error
    outliers = sorted([row for row in frame_rows if row["is_robust_outlier"]], key=lambda row: row["outlier_score"], reverse=True)
    largest_errors = sorted(frame_rows, key=lambda row: abs(row.get(f"{leader}_error") or 0), reverse=True)[:15]

    feature_analysis, recommendations = {}, []
    for feature in FEATURES:
        accepted_values = [float(record.detection[feature]) for record, _ in accepted_records if record.detection.get(feature) is not None]
        rejected_values = [float(record.detection[feature]) for record, _ in rejected_records if record.detection.get(feature) is not None]
        candidates = candidate_thresholds(accepted_values, rejected_values, feature)
        feature_analysis[feature] = {"accepted": descriptive(accepted_values), "rejected": descriptive(rejected_values), "candidate_thresholds": candidates}
        recommendations.extend(candidates)
    recommendations = sorted(recommendations, key=lambda row: (row["score"], row["false_rejection"]), reverse=True)[:12]

    return {"generated_utc": utc_now(), "analysis_version": "4.0", "progress": store.progress(),
            "rejections": rejection_counts, "false_trigger_families": false_family_counts,
            "outlier_review_dispositions": outlier_review_counts, "methods": method_results,
            "ranking": ranking, "leader": leader, "outlier_rule": {"center": leader_median, "mad": leader_mad, "threshold": outlier_limit},
            "outliers": outliers, "largest_errors": largest_errors, "feature_analysis": feature_analysis,
            "detector_candidates": recommendations, "frame_rows": frame_rows,
            "limitations": [
                "Manual edges are operator references, not calibrated physical ground truth.",
                "Detector candidates are evaluated only on frames qualified by the baseline detector.",
                "The retained dataset cannot measure false negatives among frames the baseline detector never saved.",
                "Candidate thresholds are development hypotheses and require a new locked validation capture.",
                "Hardware motion safety and physical-unit accuracy are outside this study.",
            ]}


def write_csv(path: Path, rows: list[dict[str, Any]]) -> None:
    if not rows: return
    fields = sorted({key for row in rows for key in row})
    with path.open("w", newline="", encoding="utf-8") as stream:
        writer = csv.DictWriter(stream, fields); writer.writeheader(); writer.writerows(rows)


def generate_report(store: StudyStore) -> Path:
    results = analyze_study(store); output = store.directory / "report"; output.mkdir(exist_ok=True)
    atomic_json(output / "results.json", results)
    write_csv(output / "frame_results.csv", results["frame_rows"])
    write_csv(output / "accepted_outliers.csv", results["largest_errors"])
    write_csv(output / "detector_candidates.csv", results["detector_candidates"])

    method_rows = "".join("<tr>" + "".join(f"<td>{html.escape(str(value))}</td>" for value in [
        method, stats.get("valid"), f'{stats.get("valid_fraction", 0):.3f}',
        f'{stats.get("mean_signed_error", 0):.3f}', f'{stats.get("median_absolute_error", 0):.3f}',
        f'{stats.get("rmse", 0):.3f}', f'{stats.get("absolute_error_p95", 0):.3f}'
    ]) + "</tr>" for method in results["ranking"] for stats in [results["methods"][method]])
    candidate_rows = "".join("<tr>" + "".join(f"<td>{html.escape(str(value))}</td>" for value in [
        row["feature"], row["direction"], f'{row["threshold"]:.4g}', f'{row["accepted_retention"]:.1%}', f'{row["false_rejection"]:.1%}'
    ]) + "</tr>" for row in results["detector_candidates"])
    outlier_rows = "".join("<tr>" + "".join(f"<td>{html.escape(str(value))}</td>" for value in [
        row["sequence"], row["study_frame_id"], f'{row.get(results["leader"] + "_error", 0):.3f}',
        f'{row.get("window_iou", 0):.3f}', row.get("outlier_disposition", "unreviewed")
    ]) + "</tr>" for row in results["largest_errors"])
    feature_sections = "".join(f"<details><summary>{html.escape(feature)}</summary><pre>{html.escape(json.dumps(data, indent=2))}</pre></details>" for feature, data in results["feature_analysis"].items())
    document = f'''<!doctype html><meta charset="utf-8"><title>LISM Centroid Study Report</title><style>body{{font:15px/1.45 system-ui;max-width:1200px;margin:40px auto;padding:0 20px;color:#182633}}h1,h2{{color:#124c72}}.hero{{padding:24px;background:#edf7ff;border-radius:16px}}.callout{{padding:15px;background:#fff4d8;border-radius:10px}}table{{border-collapse:collapse;width:100%;margin:10px 0 24px}}th,td{{padding:8px;border-bottom:1px solid #cfdae2;text-align:right}}th:first-child,td:first-child{{text-align:left}}pre{{white-space:pre-wrap}}details{{border:1px solid #d5e0e7;border-radius:8px;padding:8px;margin:7px 0}}summary{{font-weight:700;cursor:pointer}}</style>
<div class="hero"><h1>LISM Centroid Study Report</h1><p>Generated {results['generated_utc']}</p><p><b>Centroid leader:</b> {html.escape(results['leader'])}</p><p><b>Accepted:</b> {results['progress']['accepted']} | <b>Rejected:</b> {results['progress']['rejected']} | <b>Robust outliers:</b> {len(results['outliers'])}</p></div>
<h2>Executive findings</h2><div class="callout"><p>The centroid ranking and detector investigation are reported separately. Detector-threshold candidates are hypotheses obtained from this labeled, baseline-qualified dataset. They must be tested on a new locked capture before becoming defaults.</p></div>
<h2>Centroid accuracy</h2><table><tr><th>Method</th><th>Valid</th><th>Yield</th><th>Bias</th><th>Median absolute</th><th>RMSE</th><th>P95 absolute</th></tr>{method_rows}</table>
<h2>Accepted-frame outlier review</h2><p>Robust rule: error deviation greater than {results['outlier_rule']['threshold']:.3f} pixels from the leader median. Outliers are never removed silently.</p><table><tr><th>Frame</th><th>ID</th><th>Leader error</th><th>Window IoU</th><th>Review disposition</th></tr>{outlier_rows}</table><pre>{html.escape(json.dumps(results['outlier_review_dispositions'], indent=2))}</pre>
<h2>False-trigger investigation</h2><h3>Original rejection reasons</h3><pre>{html.escape(json.dumps(results['rejections'], indent=2))}</pre><h3>Reviewed false-trigger families</h3><pre>{html.escape(json.dumps(results['false_trigger_families'], indent=2))}</pre>
<h2>Detector-development candidates</h2><table><tr><th>Feature</th><th>Rule</th><th>Threshold</th><th>Accepted retention</th><th>False rejection</th></tr>{candidate_rows}</table>
<h2>Feature evidence</h2>{feature_sections}
<h2>Limitations and required validation</h2><ul>{''.join('<li>'+html.escape(item)+'</li>' for item in results['limitations'])}</ul>
<h2>Reproducibility manifest</h2><pre>{html.escape(json.dumps(store.manifest, indent=2))}</pre>'''
    (output / "report.html").write_text(document, encoding="utf-8")
    return output / "report.html"


class CentroidStudyService:
    def __init__(self, camera, root: Path):
        self.camera, self.root = camera, Path(root)
        self.store: StudyStore | None = None; self.capture: CaptureWorker | None = None
        self.original_configuration = None

    def start_capture(self, body):
        if self.capture and self.capture.running.is_set(): raise ValueError("Capture is already active")
        clean = lambda value: "".join(c if c.isalnum() or c in "-_" else "_" for c in str(value)).strip("_") or "study"
        name, session = clean(body.get("study_name", "study")), clean(body.get("session_label", "session"))
        directory = self.root / f"{name}_{session}_{datetime.now().strftime('%Y%m%d_%H%M%S')}"
        self.original_configuration = self.camera.configuration(); self.camera.stop()
        try:
            observed = self.camera.apply_temporary_configuration(st_high=9000, st_low=100, edge_delay=88, adc_gain=15, adc_bias=282)
            if not isinstance(observed, dict): observed = self.camera.configuration()
            self.camera.start()
        except Exception:
            self.camera.restore_configuration(self.original_configuration); raise
        self.store = StudyStore(directory, {"study_name": body.get("study_name", name), "session_label": body.get("session_label", session),
             "operator": body.get("operator", ""), "camera_requested": CAMERA, "camera_observed": observed,
             "trigger_configuration": TRIGGER, "annotation_definition": "Operator-selected coherent-footprint edges; midpoint calculated by software."})
        self.capture = CaptureWorker(self.camera, self.store, max(1, int(body.get("target", 200)))); self.capture.start()
        return {"ok": True, "directory": str(directory)}

    def open_study(self, path):
        directory = Path(path).expanduser().resolve()
        if not (directory / "study_manifest.json").exists(): raise ValueError("Study manifest was not found")
        self.store = StudyStore(directory); return {"ok": True, "directory": str(directory)}

    def stop_capture(self):
        if self.capture: self.capture.stop()
        return {"ok": True}

    def state(self):
        return {"ready": self.store is not None, "progress": self.store.progress() if self.store else {k: 0 for k in ["captured", "annotated", "accepted", "uncertain", "rejected", "outliers_reviewed", "false_triggers_reviewed"]},
                "capture": self.capture.state() if self.capture else None, "study_directory": str(self.store.directory) if self.store else None}

    def shutdown(self):
        if self.capture: self.capture.stop()
        if self.original_configuration is not None:
            try: self.camera.stop()
            finally: self.camera.restore_configuration(self.original_configuration)


def serve(service: CentroidStudyService, host: str, port: int):
    ui = Path(__file__).with_name("centroid_study_ui.html").read_bytes()
    class Handler(BaseHTTPRequestHandler):
        def send_json(self, value, status=200):
            data = json.dumps(jsonable(value), allow_nan=False).encode(); self.send_response(status)
            self.send_header("Content-Type", "application/json"); self.send_header("Cache-Control", "no-store")
            self.send_header("Content-Length", str(len(data))); self.end_headers(); self.wfile.write(data)
        def read_json(self): return json.loads(self.rfile.read(int(self.headers.get("Content-Length", "0"))) or b"{}")
        def do_GET(self):
            try:
                parsed = urlparse(self.path); query = parse_qs(parsed.query)
                if parsed.path in {"/", "/index.html"}:
                    self.send_response(200); self.send_header("Content-Type", "text/html;charset=utf-8"); self.send_header("Content-Length", str(len(ui))); self.end_headers(); self.wfile.write(ui)
                elif parsed.path == "/api/state": self.send_json(service.state())
                elif parsed.path == "/api/frame":
                    record, pixels = service.store.load_frame(int(query["index"][0])); self.send_json({"record": asdict(record), "pixels": pixels, "annotation": service.store.annotation(record.study_frame_id)})
                elif parsed.path == "/api/analysis": self.send_json(analyze_study(service.store))
                elif parsed.path == "/api/queue":
                    analysis = analyze_study(service.store); mode = query.get("mode", ["unannotated"])[0]
                    if mode == "outliers": ids = [row["study_frame_id"] for row in analysis["largest_errors"]]
                    elif mode == "false_triggers": ids = [r.study_frame_id for r in service.store.records if (service.store.annotation(r.study_frame_id) or {}).get("status") == "rejected"]
                    else: ids = [r.study_frame_id for r in service.store.records if service.store.annotation(r.study_frame_id) is None]
                    indexes = [{"index": i, "study_frame_id": r.study_frame_id} for i, r in enumerate(service.store.records) if r.study_frame_id in ids]
                    self.send_json({"mode": mode, "items": indexes})
                elif parsed.path == "/report/report.html":
                    data = (service.store.directory / "report/report.html").read_bytes(); self.send_response(200); self.send_header("Content-Type", "text/html;charset=utf-8"); self.send_header("Content-Length", str(len(data))); self.end_headers(); self.wfile.write(data)
                else: self.send_error(404)
            except Exception as exc: self.send_json({"error": str(exc)}, 400)
        def do_POST(self):
            try:
                if self.path == "/api/start": self.send_json(service.start_capture(self.read_json()))
                elif self.path == "/api/open": self.send_json(service.open_study(self.read_json()["path"]))
                elif self.path == "/api/stop": self.send_json(service.stop_capture())
                elif self.path == "/api/annotation":
                    body = self.read_json(); self.send_json(service.store.save_annotation(body["study_frame_id"], body))
                elif self.path == "/api/report":
                    path = generate_report(service.store); self.send_json({"ok": True, "path": str(path), "url": "/report/report.html"})
                else: self.send_error(404)
            except Exception as exc: self.send_json({"error": str(exc)}, 400)
        def log_message(self, *_): pass
    server = ThreadingHTTPServer((host, port), Handler)
    try: print(f"Open http://{host}:{port}"); server.serve_forever(0.2)
    except KeyboardInterrupt: pass
    finally: server.server_close(); service.shutdown()


def main(argv=None):
    from .camera import LismCamera
    from .config import Config
    parser = argparse.ArgumentParser(); parser.add_argument("--config", default="config/default.json")
    parser.add_argument("--study-root", default="centroid_studies"); parser.add_argument("--host", default="127.0.0.1"); parser.add_argument("--port", type=int, default=8770)
    args = parser.parse_args(argv); config, base = Config.load(args.config)
    with LismCamera(config, base) as camera: serve(CentroidStudyService(camera, Path(args.study_root)), args.host, args.port)

if __name__ == "__main__": main()
