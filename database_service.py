# database_service.py
from __future__ import annotations
from configuration_db import ConfigurationDatabaseMixin
from encoder_checkpoint_db import (EncoderCheckpointDatabaseMixin,)
import json
import sqlite3
import threading
from datetime import (
    datetime,
    timedelta,
    timezone,
)
from pathlib import Path
from typing import Any, Dict, List, Optional
from service_db import ServiceDatabaseMixin

DB_FILE = Path(__file__).with_name("grlrr.db")

ACTIVE_JOB_STATES = (
    "READY",
    "STARTING",
    "RUNNING",
    "PAUSED",
    "COMPLETING",
)


def utc_now_iso() -> str:
    """
    Return a timezone-aware UTC timestamp.

    Example:
        2026-07-23T22:30:00.123456+00:00
    """
    return datetime.now(timezone.utc).isoformat()


class DatabaseService(
    ConfigurationDatabaseMixin,
    EncoderCheckpointDatabaseMixin,
    ServiceDatabaseMixin,
):
    """
    Thread-safe SQLite service.

    The CM5 database is the authoritative source for:
      - Robot identity
      - Jobs
      - Job events
      - System events

    This remains a singleton because the rest of the current application
    already expects DatabaseService() to return one shared service.
    """

    _instance = None
    _initialized = False

    def __new__(cls):
        if cls._instance is None:
            cls._instance = super().__new__(cls)

        return cls._instance

    def __init__(self):
        if DatabaseService._initialized:
            return

        self._lock = threading.RLock()

        self.conn = sqlite3.connect(
            str(DB_FILE),
            check_same_thread=False,
            timeout=5.0,
        )

        self.conn.row_factory = sqlite3.Row

        self._configure_database()
        self.create_tables()
        self.create_configuration_tables()
        self.create_service_tables()
        self.migrate_configuration_tables()
        self._migrate_existing_database()
        self.migrate_encoder_checkpoint_columns()
        self.seed_default_configuration_if_empty()
        print(f"SQLite database path: {DB_FILE.resolve()}")

        DatabaseService._initialized = True

    # ------------------------------------------------------------------
    # SQLite setup
    # ------------------------------------------------------------------

    def _configure_database(self) -> None:
        with self._lock:
            self.conn.execute("PRAGMA foreign_keys = ON")
            self.conn.execute("PRAGMA journal_mode = WAL")
            self.conn.execute("PRAGMA synchronous = NORMAL")
            self.conn.execute("PRAGMA busy_timeout = 5000")
            self.conn.commit()

    def create_tables(self) -> None:
        """
        Creates the latest schema for a new robot.

        Existing robots are upgraded by _migrate_existing_database().
        """
        with self._lock:
            self.conn.execute(
                """
                CREATE TABLE IF NOT EXISTS robot_identity (
                    id INTEGER PRIMARY KEY CHECK (id = 1),

                    robot_model TEXT,
                    fleet_id TEXT,

                    firmware_version TEXT,
                    firmware_git TEXT,

                    updated_at TEXT NOT NULL
                )
                """
            )

            self.conn.execute(
                """
                CREATE TABLE IF NOT EXISTS jobs (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,

                    job_uuid TEXT NOT NULL UNIQUE,

                    operator_initials TEXT NOT NULL,
                    blade_serial TEXT NOT NULL,
                    blade_type_id TEXT NOT NULL,
                    blade_type_name TEXT NOT NULL,

                    robot_model TEXT,
                    fleet_id TEXT,
                    firmware_version TEXT,
                    firmware_git TEXT,

                    state TEXT NOT NULL DEFAULT 'READY',

                    created_at TEXT NOT NULL,
                    started_at TEXT,
                    completed_at TEXT,

                    result TEXT,
                    failure_reason TEXT,

                    configuration_snapshot_json TEXT,

                    uploaded INTEGER NOT NULL DEFAULT 0,
                    uploaded_at TEXT
                )
                """
            )

            self.conn.execute(
                """
                CREATE TABLE IF NOT EXISTS job_events (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,

                    job_id INTEGER NOT NULL,

                    timestamp TEXT NOT NULL,

                    event_type TEXT NOT NULL,
                    data_json TEXT NOT NULL,

                    FOREIGN KEY (job_id)
                        REFERENCES jobs(id)
                        ON DELETE CASCADE
                )
                """
            )

            self.conn.execute(
                """
                CREATE TABLE IF NOT EXISTS system_events (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,

                    timestamp TEXT NOT NULL,
                    level TEXT NOT NULL,

                    event_type TEXT NOT NULL,
                    message TEXT NOT NULL,

                    data_json TEXT
                )
                """
            )

            self._create_indexes()
            self.conn.commit()

    def _create_indexes(self) -> None:
        self.conn.execute(
            """
            CREATE INDEX IF NOT EXISTS idx_jobs_state
            ON jobs(state)
            """
        )

        self.conn.execute(
            """
            CREATE INDEX IF NOT EXISTS idx_jobs_created_at
            ON jobs(created_at)
            """
        )

        self.conn.execute(
            """
            CREATE INDEX IF NOT EXISTS idx_jobs_uploaded
            ON jobs(uploaded)
            """
        )

        self.conn.execute(
            """
            CREATE INDEX IF NOT EXISTS idx_job_events_job_timestamp
            ON job_events(job_id, timestamp)
            """
        )

        self.conn.execute(
            """
            CREATE INDEX IF NOT EXISTS idx_system_events_timestamp
            ON system_events(timestamp)
            """
        )

    # ------------------------------------------------------------------
    # Migration support for your current grlrr.db
    # ------------------------------------------------------------------

    def _column_names(
        self,
        table_name: str,
    ) -> set:
        rows = self.conn.execute(
            f"PRAGMA table_info({table_name})"
        ).fetchall()

        return {
            row["name"]
            for row in rows
        }


    def _add_column_if_missing(
        self,
        table_name: str,
        column_name: str,
        column_definition: str,
    ) -> None:
        columns = self._column_names(
            table_name
        )

        if column_name in columns:
            return

        self.conn.execute(
            f"""
            ALTER TABLE {table_name}
            ADD COLUMN {column_name}
            {column_definition}
            """
        )

    def _migrate_existing_database(self) -> None:
        """
        Adds fields missing from the original schema.

        SQLite cannot add all constraints through ALTER TABLE, so older
        rows may temporarily contain NULL values. All new jobs created by
        this service will contain the complete Phase 1 job data.
        """
        with self._lock:
            # Existing robot_identity table migration.
            robot_identity_columns = self._column_names(
                "robot_identity"
            )

            if "id" not in robot_identity_columns:
                # Rebuild robot_identity because the old table had no key.
                self.conn.execute(
                    """
                    ALTER TABLE robot_identity
                    RENAME TO robot_identity_legacy
                    """
                )

                self.conn.execute(
                    """
                    CREATE TABLE robot_identity (
                        id INTEGER PRIMARY KEY CHECK (id = 1),

                        robot_model TEXT,
                        fleet_id TEXT,

                        firmware_version TEXT,
                        firmware_git TEXT,

                        updated_at TEXT NOT NULL
                    )
                    """
                )

                legacy_row = self.conn.execute(
                    """
                    SELECT
                        robot_model,
                        fleet_id,
                        firmware_version,
                        firmware_git
                    FROM robot_identity_legacy
                    LIMIT 1
                    """
                ).fetchone()

                if legacy_row is not None:
                    self.conn.execute(
                        """
                        INSERT INTO robot_identity (
                            id,
                            robot_model,
                            fleet_id,
                            firmware_version,
                            firmware_git,
                            updated_at
                        )
                        VALUES (1, ?, ?, ?, ?, ?)
                        """,
                        (
                            legacy_row["robot_model"],
                            legacy_row["fleet_id"],
                            legacy_row["firmware_version"],
                            legacy_row["firmware_git"],
                            utc_now_iso(),
                        ),
                    )

                self.conn.execute(
                    "DROP TABLE robot_identity_legacy"
                )

            # Existing jobs table migration.
            self._add_column_if_missing(
                "jobs",
                "operator_initials",
                "TEXT",
            )

            self._add_column_if_missing(
                "jobs",
                "blade_type_id",
                "TEXT",
            )

            self._add_column_if_missing(
                "jobs",
                "blade_type_name",
                "TEXT",
            )

            self._add_column_if_missing(
                "jobs",
                "firmware_git",
                "TEXT",
            )

            self._add_column_if_missing(
                "jobs",
                "state",
                "TEXT DEFAULT 'COMPLETED'",
            )

            self._add_column_if_missing(
                "jobs",
                "created_at",
                "TEXT",
            )

            self._add_column_if_missing(
                "jobs",
                "failure_reason",
                "TEXT",
            )

            self._add_column_if_missing(
                "jobs",
                "configuration_snapshot_json",
                "TEXT",
            )

            self._add_column_if_missing(
                "jobs",
                "uploaded_at",
                "TEXT",
            )

            # Existing job_events used "data"; new code uses data_json.
            self._add_column_if_missing(
                "job_events",
                "data_json",
                "TEXT",
            )

            # Existing system_events did not have structured data.
            self._add_column_if_missing(
                "system_events",
                "data_json",
                "TEXT",
            )

            # Fill migration defaults for existing rows.
            self.conn.execute(
                """
                UPDATE jobs
                SET created_at = COALESCE(
                    created_at,
                    started_at,
                    completed_at,
                    ?
                )
                WHERE created_at IS NULL
                """,
                (utc_now_iso(),),
            )

            self.conn.execute(
                """
                UPDATE jobs
                SET state =
                    CASE
                        WHEN completed_at IS NOT NULL THEN 'COMPLETED'
                        WHEN started_at IS NOT NULL THEN 'RUNNING'
                        ELSE 'CANCELLED'
                    END
                WHERE state IS NULL
                   OR TRIM(state) = ''
                """
            )

            job_event_columns = self._column_names("job_events")

            if "data" in job_event_columns:
                self.conn.execute(
                    """
                    UPDATE job_events
                    SET data_json = COALESCE(data_json, data, '{}')
                    WHERE data_json IS NULL
                    """
                )
            else:
                self.conn.execute(
                    """
                    UPDATE job_events
                    SET data_json = '{}'
                    WHERE data_json IS NULL
                    """
                )

            self._create_indexes()

            # Enforce one active job at the SQLite level.
            #
            # This protects against two simultaneous start_job requests.
            try:
                self.conn.execute(
                    """
                    CREATE UNIQUE INDEX IF NOT EXISTS idx_one_active_job
                    ON jobs ((1))
                    WHERE state IN (
                        'READY',
                        'STARTING',
                        'RUNNING',
                        'PAUSED',
                        'COMPLETING'
                    )
                    """
                )
            except sqlite3.IntegrityError:
                # An older database may already contain multiple incomplete
                # jobs. Leave the database usable and log/reconcile manually.
                pass

            self.conn.commit()

    # ------------------------------------------------------------------
    # JSON helpers
    # ------------------------------------------------------------------

    @staticmethod
    def _json_dumps(value: Any) -> str:
        return json.dumps(
            value,
            ensure_ascii=False,
            separators=(",", ":"),
            default=str,
        )

    @staticmethod
    def _json_loads(
        value: Optional[str],
        default: Any,
    ) -> Any:
        if value is None or value == "":
            return default

        try:
            return json.loads(value)
        except (TypeError, json.JSONDecodeError):
            return default

    # ------------------------------------------------------------------
    # Robot identity
    # ------------------------------------------------------------------

    def update_robot_identity(
        self,
        fw: Dict[str, Any],
    ) -> None:
        with self._lock:
            self.conn.execute(
                """
                INSERT INTO robot_identity (
                    id,
                    robot_model,
                    fleet_id,
                    firmware_version,
                    firmware_git,
                    updated_at
                )
                VALUES (1, ?, ?, ?, ?, ?)

                ON CONFLICT(id) DO UPDATE SET
                    robot_model = excluded.robot_model,
                    fleet_id = excluded.fleet_id,
                    firmware_version = excluded.firmware_version,
                    firmware_git = excluded.firmware_git,
                    updated_at = excluded.updated_at
                """,
                (
                    fw.get("model"),
                    fw.get("fleet_id"),
                    fw.get("semver"),
                    fw.get("git"),
                    utc_now_iso(),
                ),
            )

            self.conn.commit()

    def get_robot_identity(self) -> Dict[str, Any]:
        with self._lock:
            row = self.conn.execute(
                """
                SELECT
                    robot_model,
                    fleet_id,
                    firmware_version,
                    firmware_git,
                    updated_at
                FROM robot_identity
                WHERE id = 1
                """
            ).fetchone()

        if row is None:
            return {
                "model": None,
                "fleet_id": None,
                "semver": None,
                "git": None,
                "updated_at": None,
            }

        return {
            "model": row["robot_model"],
            "fleet_id": row["fleet_id"],
            "semver": row["firmware_version"],
            "git": row["firmware_git"],
            "updated_at": row["updated_at"],
        }

    # ------------------------------------------------------------------
    # System events
    # ------------------------------------------------------------------

    def insert_system_event(
        self,
        level: str,
        event_type: str,
        message: str,
        data: Optional[Dict[str, Any]] = None,
    ) -> None:
        with self._lock:
            self.conn.execute(
                """
                INSERT INTO system_events (
                    timestamp,
                    level,
                    event_type,
                    message,
                    data_json
                )
                VALUES (?, ?, ?, ?, ?)
                """,
                (
                    utc_now_iso(),
                    level.upper(),
                    event_type,
                    message,
                    self._json_dumps(data)
                    if data is not None
                    else None,
                ),
            )

            self.conn.commit()

    # ------------------------------------------------------------------
    # Job conversion
    # ------------------------------------------------------------------

    def _job_row_to_dict(
        self,
        row: sqlite3.Row,
    ) -> Dict[str, Any]:
        return {
            "job_uuid": row["job_uuid"],
            "operator_initials":
                row["operator_initials"] or "",
            "blade_serial":
                row["blade_serial"] or "",
            "blade_type_id":
                row["blade_type_id"] or "",
            "blade_type_name":
                row["blade_type_name"] or "",
            "robot_model": row["robot_model"],
            "fleet_id": row["fleet_id"],
            "firmware_version":
                row["firmware_version"],
            "firmware_git": row["firmware_git"],
            "state": row["state"],
            "created_at": row["created_at"],
            "started_at": row["started_at"],
            "completed_at": row["completed_at"],
            "result": row["result"],
            "failure_reason":
                row["failure_reason"],
            "configuration_snapshot":
                self._json_loads(
                    row["configuration_snapshot_json"],
                    {},
                ),
            "uploaded": bool(row["uploaded"]),
            "uploaded_at": row["uploaded_at"],

            "encoder_distance_mm":
                row["encoder_distance_mm"],

            "encoder_counts":
                self._json_loads(
                    row["encoder_counts_json"],
                    [],
                ),

            "encoder_checkpoint_at":
                row["encoder_checkpoint_at"],

            "encoder_checkpoint_source":
                row["encoder_checkpoint_source"],

            "encoder_checkpoint_sequence":
                int(
                    row["encoder_checkpoint_sequence"]
                    or 0
                ),

            "encoder_restore_required":
                bool(
                    row["encoder_restore_required"]
                ),

            "encoder_distance_mm": row["encoder_distance_mm"],
            "encoder_counts": self._json_loads(row["encoder_counts_json"], []),
            "encoder_checkpoint_at": row["encoder_checkpoint_at"],
            "encoder_checkpoint_source": row["encoder_checkpoint_source"],
            "encoder_checkpoint_sequence": int(row["encoder_checkpoint_sequence"] or 0),
            "encoder_restore_required": bool(row["encoder_restore_required"]),
            "encoder_restored_at": row["encoder_restored_at"],
            "encoder_session_id": row["encoder_session_id"],
            "encoder_restore_state": row["encoder_restore_state"],
            "encoder_restore_failure": row["encoder_restore_failure"],
            "encoder_restore_requested_at": row["encoder_restore_requested_at"],
        }

    # ------------------------------------------------------------------
    # Jobs
    # ------------------------------------------------------------------

    def get_job_by_uuid(
        self,
        job_uuid: str,
    ) -> Optional[Dict[str, Any]]:
        with self._lock:
            row = self.conn.execute(
                """
                SELECT *
                FROM jobs
                WHERE job_uuid = ?
                """,
                (job_uuid,),
            ).fetchone()

        if row is None:
            return None

        return self._job_row_to_dict(row)

    def get_active_job(
        self,
    ) -> Optional[Dict[str, Any]]:
        placeholders = ",".join(
            "?" for _ in ACTIVE_JOB_STATES
        )

        with self._lock:
            row = self.conn.execute(
                f"""
                SELECT *
                FROM jobs
                WHERE state IN ({placeholders})
                ORDER BY created_at DESC, id DESC
                LIMIT 1
                """,
                ACTIVE_JOB_STATES,
            ).fetchone()

        if row is None:
            return None

        return self._job_row_to_dict(row)

    def list_job_history(
        self,
        *,
        limit: int = 50,
        offset: int = 0,
        search: str = "",
        result_filter: str = "all",
        from_utc: Optional[str] = None,
        to_utc: Optional[str] = None,
    ) -> Dict[str, Any]:
        """
        Return completed historical jobs using database-side
        filtering and pagination.

        Search is case-insensitive and matches:
          - Job UUID
          - Operator initials
          - Blade serial
          - Blade type ID
          - Blade type name
        """
        limit = max(
            1,
            min(
                int(limit),
                200,
            ),
        )

        offset = max(
            0,
            int(offset),
        )

        search = str(
            search
            or ""
        ).strip()

        result_filter = str(
            result_filter
            or "all"
        ).strip().lower()

        conditions = [
            """
            state IN (
                'COMPLETED',
                'FAILED',
                'CANCELLED'
            )
            """
        ]

        parameters: List[Any] = []

        if search:
            conditions.append(
                """
                (
                    LOWER(
                        COALESCE(
                            job_uuid,
                            ''
                        )
                    ) LIKE ? ESCAPE '\\'

                    OR LOWER(
                        COALESCE(
                            operator_initials,
                            ''
                        )
                    ) LIKE ? ESCAPE '\\'

                    OR LOWER(
                        COALESCE(
                            blade_serial,
                            ''
                        )
                    ) LIKE ? ESCAPE '\\'

                    OR LOWER(
                        COALESCE(
                            blade_type_id,
                            ''
                        )
                    ) LIKE ? ESCAPE '\\'

                    OR LOWER(
                        COALESCE(
                            blade_type_name,
                            ''
                        )
                    ) LIKE ? ESCAPE '\\'
                )
                """
            )

            escaped_search = (
                search
                .lower()
                .replace(
                    "\\",
                    "\\\\",
                )
                .replace(
                    "%",
                    "\\%",
                )
                .replace(
                    "_",
                    "\\_",
                )
            )

            pattern = (
                f"%{escaped_search}%"
            )

            parameters.extend(
                [pattern] * 5
            )
        if result_filter == "completed":
            conditions.append(
                "state = 'COMPLETED'"
            )

        elif result_filter == "failed":
            conditions.append(
                "state = 'FAILED'"
            )

        elif result_filter == "cancelled":
            conditions.append(
                "state = 'CANCELLED'"
            )

        elif result_filter != "all":
            raise ValueError(
                "Unsupported job-history "
                f"result filter: "
                f"{result_filter}"
            )

        if from_utc:
            conditions.append(
                "completed_at >= ?"
            )

            parameters.append(
                str(from_utc)
            )

        if to_utc:
            conditions.append(
                "completed_at < ?"
            )

            parameters.append(
                str(to_utc)
            )

        where_sql = (
            " AND ".join(
                conditions
            )
        )

        with self._lock:
            count_row = (
                self.conn.execute(
                    f"""
                    SELECT
                        COUNT(*) AS count
                    FROM jobs
                    WHERE {where_sql}
                    """,
                    tuple(parameters),
                ).fetchone()
            )

            rows = (
                self.conn.execute(
                    f"""
                    SELECT *
                    FROM jobs
                    WHERE {where_sql}
                    ORDER BY
                        completed_at DESC,
                        id DESC
                    LIMIT ?
                    OFFSET ?
                    """,
                    (
                        *parameters,
                        limit,
                        offset,
                    ),
                ).fetchall()
            )

        total = int(
            count_row["count"]
            if count_row is not None
            else 0
        )

        jobs = [
            self._job_row_to_dict(
                row
            )
            for row in rows
        ]

        return {
            "jobs": jobs,
            "total": total,
            "limit": limit,
            "offset": offset,
            "has_more": (
                offset + len(jobs)
                < total
            ),
        }

    def get_job_history_metrics(
        self,
    ) -> Dict[str, Any]:
        """
        Return simple team-lead production metrics.

        Calendar boundaries use the CM5 local timezone and are
        converted to UTC before comparison with the stored UTC
        timestamps.
        """
        now_local = (
            datetime.now()
            .astimezone()
        )

        today_local = (
            now_local.replace(
                hour=0,
                minute=0,
                second=0,
                microsecond=0,
            )
        )

        tomorrow_local = (
            today_local
            + timedelta(days=1)
        )

        seven_day_start_local = (
            today_local
            - timedelta(days=6)
        )

        today_utc = (
            today_local
            .astimezone(timezone.utc)
            .isoformat()
        )

        tomorrow_utc = (
            tomorrow_local
            .astimezone(timezone.utc)
            .isoformat()
        )

        seven_day_start_utc = (
            seven_day_start_local
            .astimezone(timezone.utc)
            .isoformat()
        )

        with self._lock:
            row = self.conn.execute(
                """
                SELECT
                    SUM(
                        CASE
                            WHEN state IN (
                                'COMPLETED',
                                'FAILED'
                            )
                            AND completed_at >= ?
                            AND completed_at < ?
                            THEN 1
                            ELSE 0
                        END
                    ) AS completed_today,

                    AVG(
                        CASE
                            WHEN state IN (
                                'COMPLETED',
                                'FAILED'
                            )
                            AND completed_at >= ?
                            AND completed_at < ?
                            AND started_at IS NOT NULL
                            AND completed_at IS NOT NULL
                            AND julianday(
                                completed_at
                            ) >= julianday(
                                started_at
                            )
                            THEN (
                                julianday(
                                    completed_at
                                )
                                - julianday(
                                    started_at
                                )
                            ) * 86400.0
                            ELSE NULL
                        END
                    ) AS avg_cycle_today,

                    SUM(
                        CASE
                            WHEN state = 'FAILED'
                            AND completed_at >= ?
                            AND completed_at < ?
                            THEN 1
                            ELSE 0
                        END
                    ) AS failed_today,

                    SUM(
                        CASE
                            WHEN state IN (
                                'COMPLETED',
                                'FAILED'
                            )
                            AND completed_at >= ?
                            AND completed_at < ?
                            THEN 1
                            ELSE 0
                        END
                    ) AS completed_last_7_days

                FROM jobs
                """,
                (
                    today_utc,
                    tomorrow_utc,

                    today_utc,
                    tomorrow_utc,

                    today_utc,
                    tomorrow_utc,

                    seven_day_start_utc,
                    tomorrow_utc,
                ),
            ).fetchone()

        return {
            "jobs_completed_today": int(
                row["completed_today"]
                or 0
            ),
            "average_cycle_seconds_today": (
                round(
                    float(
                        row[
                            "avg_cycle_today"
                        ]
                    ),
                    1,
                )
                if row[
                    "avg_cycle_today"
                ] is not None
                else None
            ),
            "failed_jobs_today": int(
                row["failed_today"]
                or 0
            ),
            "jobs_completed_last_7_days": int(
                row[
                    "completed_last_7_days"
                ]
                or 0
            ),
            "updated_at": utc_now_iso(),
        }


    def start_job(
        self,
        *,
        job_uuid: str,
        operator_initials: str,
        blade_serial: str,
        blade_type_id: str,
        blade_type_name: str,
        robot_model: Optional[str],
        fleet_id: Optional[str],
        firmware_version: Optional[str],
        firmware_git: Optional[str],
        configuration_snapshot: Dict[str, Any],
    ) -> Dict[str, Any]:
        """
        Create a persistent READY job.

        The H7 process is not marked RUNNING here. That will be handled
        later when process_status confirms execution.
        """
        created_at = utc_now_iso()

        with self._lock:
            try:
                self.conn.execute("BEGIN IMMEDIATE")

                existing = self.conn.execute(
                    """
                    SELECT job_uuid
                    FROM jobs
                    WHERE state IN (
                        'READY',
                        'STARTING',
                        'RUNNING',
                        'PAUSED',
                        'COMPLETING'
                    )
                    LIMIT 1
                    """
                ).fetchone()

                if existing is not None:
                    raise ValueError(
                        "Another job is already active."
                    )

                cursor = self.conn.execute(
                    """
                    INSERT INTO jobs (
                        job_uuid,

                        operator_initials,
                        blade_serial,
                        blade_type_id,
                        blade_type_name,

                        robot_model,
                        fleet_id,
                        firmware_version,
                        firmware_git,

                        state,
                        created_at,

                        configuration_snapshot_json,
                        uploaded
                    )
                    VALUES (
                        ?,
                        ?, ?, ?, ?,
                        ?, ?, ?, ?,
                        'READY',
                        ?,
                        ?,
                        0
                    )
                    """,
                    (
                        job_uuid,
                        operator_initials,
                        blade_serial,
                        blade_type_id,
                        blade_type_name,
                        robot_model,
                        fleet_id,
                        firmware_version,
                        firmware_git,
                        created_at,
                        self._json_dumps(
                            configuration_snapshot
                        ),
                    ),
                )

                job_id = cursor.lastrowid

                self.conn.execute(
                    """
                    INSERT INTO job_events (
                        job_id,
                        timestamp,
                        event_type,
                        data_json
                    )
                    VALUES (?, ?, ?, ?)
                    """,
                    (
                        job_id,
                        created_at,
                        "job_created",
                        self._json_dumps(
                            {
                                "operator_initials":
                                    operator_initials,
                                "blade_serial":
                                    blade_serial,
                                "blade_type_id":
                                    blade_type_id,
                                "blade_type_name":
                                    blade_type_name,
                                "state": "READY",
                            }
                        ),
                    ),
                )

                self.conn.commit()

            except Exception:
                self.conn.rollback()
                raise

        job = self.get_job_by_uuid(job_uuid)

        if job is None:
            raise RuntimeError(
                "Job was inserted but could not be reloaded."
            )

        return job

    def update_job_state(
        self,
        job_uuid: str,
        state: str,
        *,
        result: Optional[str] = None,
        failure_reason: Optional[str] = None,
    ) -> Optional[Dict[str, Any]]:
        state = state.strip().upper()
        timestamp = utc_now_iso()

        valid_states = {
            "READY",
            "STARTING",
            "RUNNING",
            "PAUSED",
            "COMPLETING",
            "COMPLETED",
            "FAILED",
            "CANCELLED",
        }

        if state not in valid_states:
            raise ValueError(f"Unsupported job state: {state}")

        with self._lock:
            try:
                self.conn.execute("BEGIN IMMEDIATE")

                row = self.conn.execute(
                    """
                    SELECT id, state, started_at
                    FROM jobs
                    WHERE job_uuid = ?
                    """,
                    (job_uuid,),
                ).fetchone()

                if row is None:
                    self.conn.rollback()
                    return None

                started_at = row["started_at"]
                completed_at = None

                if state == "RUNNING" and started_at is None:
                    started_at = timestamp

                if state in {"COMPLETED", "FAILED", "CANCELLED"}:
                    completed_at = timestamp

                self.conn.execute(
                    """
                    UPDATE jobs
                    SET
                        state = ?,
                        started_at = COALESCE(?, started_at),
                        completed_at = COALESCE(?, completed_at),
                        result = COALESCE(?, result),
                        failure_reason = COALESCE(
                            ?,
                            failure_reason
                        )
                    WHERE job_uuid = ?
                    """,
                    (
                        state,
                        started_at,
                        completed_at,
                        result,
                        failure_reason,
                        job_uuid,
                    ),
                )

                # Preserve the final encoder position, but prevent a terminal
                # job from being treated as eligible for encoder restoration.
                if state in {"COMPLETED", "FAILED", "CANCELLED"}:
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
                        (job_uuid,),
                    )

                self.conn.execute(
                    """
                    INSERT INTO job_events (
                        job_id,
                        timestamp,
                        event_type,
                        data_json
                    )
                    VALUES (?, ?, ?, ?)
                    """,
                    (
                        row["id"],
                        timestamp,
                        "job_state_changed",
                        self._json_dumps(
                            {
                                "previous_state": row["state"],
                                "state": state,
                                "result": result,
                                "failure_reason": failure_reason,
                            }
                        ),
                    ),
                )

                self.conn.commit()

            except Exception:
                self.conn.rollback()
                raise

        return self.get_job_by_uuid(job_uuid)

    def end_job(
        self,
        job_uuid: str,
        result: str,
        *,
        failure_reason: Optional[str] = None,
    ) -> Optional[Dict[str, Any]]:
        normalized_result = str(result or "").strip().upper()

        result_aliases = {
            "CANCELED": "CANCELLED",
            "ABORTED": "CANCELLED",
        }
        normalized_result = result_aliases.get(
            normalized_result,
            normalized_result,
        )

        if normalized_result not in {
            "PASS",
            "FAIL",
            "CANCELLED",
        }:
            raise ValueError(
                "result must be PASS, FAIL, or CANCELLED."
            )

        normalized_failure_reason: Optional[str] = None

        if failure_reason is not None:
            if not isinstance(failure_reason, str):
                raise ValueError(
                    "failure_reason must be text."
                )

            normalized_failure_reason = failure_reason.strip()

            if len(normalized_failure_reason) > 500:
                raise ValueError(
                    "failure_reason cannot exceed 500 characters."
                )

            if not normalized_failure_reason:
                normalized_failure_reason = None

        if normalized_result == "PASS":
            state = "COMPLETED"
            normalized_failure_reason = None
        elif normalized_result == "CANCELLED":
            state = "CANCELLED"
            normalized_failure_reason = None
        else:
            state = "FAILED"

        return self.update_job_state(
            job_uuid,
            state,
            result=normalized_result,
            failure_reason=normalized_failure_reason,
        )


    def insert_job_event(
        self,
        job_uuid: str,
        event_type: str,
        data: Any,
    ) -> None:
        with self._lock:
            row = self.conn.execute(
                """
                SELECT id
                FROM jobs
                WHERE job_uuid = ?
                """,
                (job_uuid,),
            ).fetchone()

            if row is None:
                raise ValueError(
                    f"Unknown job UUID: {job_uuid}"
                )

            self.conn.execute(
                """
                INSERT INTO job_events (
                    job_id,
                    timestamp,
                    event_type,
                    data_json
                )
                VALUES (?, ?, ?, ?)
                """,
                (
                    row["id"],
                    utc_now_iso(),
                    event_type,
                    self._json_dumps(data),
                ),
            )

            self.conn.commit()

    def list_jobs(
        self,
        *,
        limit: int = 100,
        offset: int = 0,
    ) -> List[Dict[str, Any]]:
        limit = max(1, min(int(limit), 500))
        offset = max(0, int(offset))

        with self._lock:
            rows = self.conn.execute(
                """
                SELECT *
                FROM jobs
                ORDER BY created_at DESC, id DESC
                LIMIT ? OFFSET ?
                """,
                (limit, offset),
            ).fetchall()

        return [
            self._job_row_to_dict(row)
            for row in rows
        ]

    def get_job_events(
        self,
        job_uuid: str,
    ) -> List[Dict[str, Any]]:
        with self._lock:
            rows = self.conn.execute(
                """
                SELECT
                    e.timestamp,
                    e.event_type,
                    e.data_json
                FROM job_events e
                INNER JOIN jobs j
                    ON j.id = e.job_id
                WHERE j.job_uuid = ?
                ORDER BY e.timestamp ASC, e.id ASC
                """,
                (job_uuid,),
            ).fetchall()

        return [
            {
                "timestamp": row["timestamp"],
                "event_type": row["event_type"],
                "data": self._json_loads(
                    row["data_json"],
                    {},
                ),
            }
            for row in rows
        ]

    def close(self) -> None:
        with self._lock:
            if self.conn:
                self.conn.close()

    def print_database_counts(self) -> None:
        table_names = [
            "robot_identity",
            "jobs",
            "job_events",
            "system_events",
            "robot_types",
            "blade_types",
            "transition_profiles",
            "configuration_state",
            "configuration_events",
        ]

        with self._lock:
            for table_name in table_names:
                row = self.conn.execute(
                    f"SELECT COUNT(*) AS count FROM {table_name}"
                ).fetchone()

                print(
                    f"{table_name}: {row['count']}"
                )