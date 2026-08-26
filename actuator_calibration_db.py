from __future__ import annotations
from datetime import datetime, timezone
from typing import Any, Dict, Optional

class ActuatorCalibrationDatabaseMixin:
    def create_actuator_calibration_tables(self) -> None:
        with self._lock:
            self.conn.executescript("""
            CREATE TABLE IF NOT EXISTS actuator_calibrations (
                channel INTEGER PRIMARY KEY CHECK(channel BETWEEN 0 AND 3),
                extended_feedback_v REAL NOT NULL,
                retracted_feedback_v REAL NOT NULL,
                extended_tolerance_v REAL NOT NULL,
                retracted_tolerance_v REAL NOT NULL,
                extension_time_ms INTEGER NOT NULL,
                retraction_time_ms INTEGER NOT NULL,
                calibrated_at TEXT NOT NULL,
                calibrated_by TEXT,
                result_json TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS actuator_calibration_history (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                channel INTEGER NOT NULL,
                calibrated_at TEXT NOT NULL,
                calibrated_by TEXT,
                result_json TEXT NOT NULL
            );
            CREATE INDEX IF NOT EXISTS idx_actuator_calibration_history_channel
            ON actuator_calibration_history(channel, calibrated_at DESC);
            """)
            self.conn.commit()

    def save_actuator_calibration(self, channel: int, result: Dict[str, Any], actor: Optional[str]) -> Dict[str, Any]:
        if channel not in range(4): raise ValueError("Invalid actuator channel.")
        m = result.get("measurements") or {}
        if result.get("pass") is not True:
            raise ValueError("Only a successful full-span calibration may be saved.")
        try:
            ext = float(m["extended_feedback_v"])
            ret = float(m["retracted_feedback_v"])
        except (KeyError, TypeError, ValueError) as error:
            raise ValueError("Calibration endpoint measurements are missing or invalid.") from error
        span = abs(ret - ext)
        if span < 0.5:
            raise ValueError("Only a successful full-span calibration may be saved.")
        # Tolerance is channel-specific and bounded. It includes measured noise margin
        # without allowing a broad band to conceal a partial stroke.
        ext_tol = max(0.10, min(0.30, float(m.get("extended_sample_max_v", ext))-float(m.get("extended_sample_min_v", ext))+0.08))
        ret_tol = max(0.10, min(0.30, float(m.get("retracted_sample_max_v", ret))-float(m.get("retracted_sample_min_v", ret))+0.08))
        now = datetime.now(timezone.utc).isoformat(); raw = self._json_dumps(result)
        with self._lock:
            try:
                self.conn.execute("BEGIN IMMEDIATE")
                self.conn.execute("INSERT INTO actuator_calibration_history(channel,calibrated_at,calibrated_by,result_json) VALUES(?,?,?,?)",(channel,now,actor,raw))
                self.conn.execute("""INSERT INTO actuator_calibrations(channel,extended_feedback_v,retracted_feedback_v,extended_tolerance_v,retracted_tolerance_v,extension_time_ms,retraction_time_ms,calibrated_at,calibrated_by,result_json)
                VALUES(?,?,?,?,?,?,?,?,?,?) ON CONFLICT(channel) DO UPDATE SET extended_feedback_v=excluded.extended_feedback_v,retracted_feedback_v=excluded.retracted_feedback_v,extended_tolerance_v=excluded.extended_tolerance_v,retracted_tolerance_v=excluded.retracted_tolerance_v,extension_time_ms=excluded.extension_time_ms,retraction_time_ms=excluded.retraction_time_ms,calibrated_at=excluded.calibrated_at,calibrated_by=excluded.calibrated_by,result_json=excluded.result_json""",
                (channel,ext,ret,ext_tol,ret_tol,int(m["extension_time_ms"]),int(m["retraction_time_ms"]),now,actor,raw))
                self.conn.commit()
            except Exception:
                self.conn.rollback(); raise
        return self.get_actuator_calibration(channel)

    def get_actuator_calibration(self, channel: int) -> Optional[Dict[str, Any]]:
        with self._lock:
            row=self.conn.execute("SELECT * FROM actuator_calibrations WHERE channel=?",(channel,)).fetchone()
        return None if row is None else dict(row)
