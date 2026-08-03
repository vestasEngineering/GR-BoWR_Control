from __future__ import annotations

import math
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional


class EncoderCheckpointDatabaseMixin:
    """Durable, per-job encoder checkpoint operations."""

    @staticmethod
    def _encoder_utc_now() -> str:
        return datetime.now(timezone.utc).isoformat()

    def migrate_encoder_checkpoint_columns(self) -> None:
        with self._lock:
            self._add_column_if_missing("jobs", "encoder_distance_mm", "REAL")
            self._add_column_if_missing("jobs", "encoder_counts_json", "TEXT")
            self._add_column_if_missing("jobs", "encoder_checkpoint_at", "TEXT")
            self._add_column_if_missing("jobs", "encoder_checkpoint_source", "TEXT")
            self._add_column_if_missing(
                "jobs", "encoder_checkpoint_sequence", "INTEGER NOT NULL DEFAULT 0"
            )
            self._add_column_if_missing(
                "jobs", "encoder_restore_required", "INTEGER NOT NULL DEFAULT 0"
            )
            self._add_column_if_missing("jobs", "encoder_restored_at", "TEXT")
            self._add_column_if_missing("jobs", "encoder_session_id", "INTEGER")
            self._add_column_if_missing(
                "jobs", "encoder_restore_state", "TEXT NOT NULL DEFAULT 'none'"
            )
            self._add_column_if_missing("jobs", "encoder_restore_failure", "TEXT")
            self._add_column_if_missing("jobs", "encoder_restore_requested_at", "TEXT")
            self.conn.commit()

    @staticmethod
    def _validate_encoder_snapshot(
        rear_distance_mm: float,
        counts: List[int],
    ) -> tuple[float, List[int]]:
        distance = float(rear_distance_mm)
        if not math.isfinite(distance):
            raise ValueError("Encoder distance must be finite.")
        if not isinstance(counts, list) or len(counts) != 4:
            raise ValueError("Exactly four encoder counts are required.")
        return distance, [int(value) for value in counts]

    def update_job_encoder_checkpoint(
        self,
        job_uuid: str,
        *,
        rear_distance_mm: float,
        counts: List[int],
        session_id: int,
        source: str = "h7_encoder_telemetry",
    ) -> Optional[Dict[str, Any]]:
        """Update only the named active job. This does not create a job event."""
        distance, clean_counts = self._validate_encoder_snapshot(
            rear_distance_mm, counts
        )
        timestamp = self._encoder_utc_now()
        with self._lock:
            cursor = self.conn.execute(
                """
                UPDATE jobs
                SET encoder_distance_mm = ?,
                    encoder_counts_json = ?,
                    encoder_checkpoint_at = ?,
                    encoder_checkpoint_source = ?,
                    encoder_checkpoint_sequence = encoder_checkpoint_sequence + 1,
                    encoder_restore_required = 0,
                    encoder_session_id = ?,
                    encoder_restore_state = 'valid',
                    encoder_restore_failure = NULL
                WHERE job_uuid = ?
                  AND state IN ('READY','STARTING','RUNNING','PAUSED','COMPLETING')
                """,
                (
                    distance,
                    self._json_dumps(clean_counts),
                    timestamp,
                    str(source),
                    int(session_id),
                    str(job_uuid),
                ),
            )
            self.conn.commit()
        if cursor.rowcount == 0:
            return None
        return self.get_job_by_uuid(job_uuid)

    def mark_job_encoder_recovery_required(
        self,
        job_uuid: str,
        *,
        session_id: int,
        state: str,
        reason: str,
    ) -> None:
        now = self._encoder_utc_now()
        with self._lock:
            self.conn.execute(
                """
                UPDATE jobs
                SET encoder_restore_required = 1,
                    encoder_session_id = ?,
                    encoder_restore_state = ?,
                    encoder_restore_failure = ?,
                    encoder_restore_requested_at = ?
                WHERE job_uuid = ?
                  AND state IN ('READY','STARTING','RUNNING','PAUSED','COMPLETING')
                """,
                (int(session_id), str(state), str(reason), now, str(job_uuid)),
            )
            self.conn.commit()

    def mark_job_encoder_transaction_succeeded(
        self,
        job_uuid: str,
        *,
        requested_distance_mm: float,
        confirmed_distance_mm: float,
        session_id: int,
        counts: List[int],
        source: str,
        transaction_id: str,
        event_type: str,
        before_context: Optional[Dict[str, Any]] = None,
    ) -> Optional[Dict[str, Any]]:
        confirmed, clean_counts = self._validate_encoder_snapshot(
            confirmed_distance_mm, counts
        )
        requested = float(requested_distance_mm)
        if not math.isfinite(requested):
            raise ValueError("Requested encoder distance must be finite.")
        timestamp = self._encoder_utc_now()
        after_context = {
            "available": True,
            "valid": True,
            "position_frozen": False,
            "rear_distance_mm": confirmed,
            "radius_m": confirmed / 1000.0,
            "counts": clean_counts,
            "encoder_session_id": int(session_id),
            "sampled_at": timestamp,
            "source": str(source),
            "position_interpretation": "current_valid_position",
        }
        with self._lock:
            try:
                self.conn.execute("BEGIN IMMEDIATE")
                row = self.conn.execute(
                    "SELECT id FROM jobs WHERE job_uuid = ?", (str(job_uuid),)
                ).fetchone()
                if row is None:
                    self.conn.rollback()
                    return None
                self.conn.execute(
                    """
                    UPDATE jobs
                    SET encoder_distance_mm = ?,
                        encoder_counts_json = ?,
                        encoder_checkpoint_at = ?,
                        encoder_checkpoint_source = ?,
                        encoder_checkpoint_sequence = encoder_checkpoint_sequence + 1,
                        encoder_restore_required = 0,
                        encoder_restored_at = ?,
                        encoder_session_id = ?,
                        encoder_restore_state = 'valid',
                        encoder_restore_failure = NULL
                    WHERE job_uuid = ?
                    """,
                    (
                        confirmed,
                        self._json_dumps(clean_counts),
                        timestamp,
                        str(source),
                        timestamp,
                        int(session_id),
                        str(job_uuid),
                    ),
                )
                self.conn.execute(
                    """
                    INSERT INTO job_events (job_id,timestamp,event_type,data_json)
                    VALUES (?,?,?,?)
                    """,
                    (
                        row["id"],
                        timestamp,
                        str(event_type),
                        self._json_dumps(
                            {
                                "transaction_id": str(transaction_id),
                                "source": str(source),
                                "requested_distance_mm": requested,
                                "confirmed_distance_mm": confirmed,
                                "difference_mm": confirmed - requested,
                                "before_encoder_context": before_context,
                                "encoder_context": after_context,
                            }
                        ),
                    ),
                )
                self.conn.commit()
            except Exception:
                self.conn.rollback()
                raise
        return self.get_job_by_uuid(job_uuid)

    def mark_job_encoder_restore_failed(
        self,
        job_uuid: str,
        *,
        reason: str,
        requested_distance_mm: float,
        session_id: Optional[int] = None,
        event_type: str = "encoder_restore_failed",
        encoder_context: Optional[Dict[str, Any]] = None,
    ) -> None:
        now = self._encoder_utc_now()
        with self._lock:
            try:
                self.conn.execute("BEGIN IMMEDIATE")
                row = self.conn.execute(
                    "SELECT id FROM jobs WHERE job_uuid = ?", (str(job_uuid),)
                ).fetchone()
                if row is None:
                    self.conn.rollback()
                    return
                self.conn.execute(
                    """
                    UPDATE jobs
                    SET encoder_restore_required = 1,
                        encoder_restore_state = 'restore_failed',
                        encoder_restore_failure = ?
                    WHERE job_uuid = ?
                    """,
                    (str(reason), str(job_uuid)),
                )
                self.conn.execute(
                    "INSERT INTO job_events(job_id,timestamp,event_type,data_json) VALUES(?,?,?,?)",
                    (
                        row["id"],
                        now,
                        str(event_type),
                        self._json_dumps(
                            {
                                "reason": str(reason),
                                "requested_distance_mm": float(requested_distance_mm),
                                "encoder_session_id": session_id,
                                "encoder_context": encoder_context,
                            }
                        ),
                    ),
                )
                self.conn.commit()
            except Exception:
                self.conn.rollback()
                raise

    def clear_job_encoder_restore_requirement(self, job_uuid: str) -> None:
        with self._lock:
            self.conn.execute(
                """
                UPDATE jobs
                SET encoder_restore_required = 0,
                    encoder_restore_state = CASE
                        WHEN encoder_distance_mm IS NULL THEN 'none'
                        ELSE 'valid'
                    END,
                    encoder_restore_failure = NULL
                WHERE job_uuid = ?
                """,
                (str(job_uuid),),
            )
            self.conn.commit()
