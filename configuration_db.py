from __future__ import annotations

import sqlite3
from typing import Any, Dict, List, Optional
import math

class ConfigurationDatabaseMixin:
    """DB-backed robot/blade configuration methods for DatabaseService."""

    def create_configuration_tables(self) -> None:
        with self._lock:
            self.conn.executescript(
                """
                CREATE TABLE IF NOT EXISTS robot_types (
                    id TEXT PRIMARY KEY,
                    name TEXT NOT NULL,

                    installed_actuator_channels INTEGER NOT NULL
                        DEFAULT 4
                        CHECK (
                            installed_actuator_channels
                            BETWEEN 1 AND 8
                        ),

                    enabled INTEGER NOT NULL
                        DEFAULT 1
                        CHECK (enabled IN (0, 1)),

                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS blade_types (
                    id TEXT PRIMARY KEY,
                    name TEXT NOT NULL,

                    max_transitions INTEGER NOT NULL
                        CHECK (
                            max_transitions
                            BETWEEN 1 AND 8
                        ),

                    source TEXT NOT NULL
                        DEFAULT 'factory'
                        CHECK (
                            source IN (
                                'factory',
                                'user_created'
                            )
                        ),

                    enabled INTEGER NOT NULL
                        DEFAULT 1
                        CHECK (
                            enabled IN (0, 1)
                        ),

                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS transition_profiles (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    robot_type_id TEXT NOT NULL,
                    blade_type_id TEXT NOT NULL,
                    default_values_json TEXT NOT NULL,
                    override_values_json TEXT,
                    default_revision INTEGER NOT NULL DEFAULT 1,
                    override_revision INTEGER NOT NULL DEFAULT 0,
                    default_source TEXT NOT NULL DEFAULT 'factory',
                    override_updated_by TEXT,
                    override_updated_at TEXT,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    UNIQUE (robot_type_id, blade_type_id),
                    FOREIGN KEY (robot_type_id) REFERENCES robot_types(id),
                    FOREIGN KEY (blade_type_id) REFERENCES blade_types(id)
                );

                CREATE TABLE IF NOT EXISTS configuration_state (
                    id INTEGER PRIMARY KEY CHECK (id = 1),
                    selected_robot_type_id TEXT,
                    selected_blade_type_id TEXT,
                    units TEXT NOT NULL DEFAULT 'm',
                    encoder_scale REAL NOT NULL DEFAULT 1000.0,
                    motor_direction_json TEXT NOT NULL DEFAULT '{}',
                    motor_tuning_json TEXT NOT NULL DEFAULT '{}',
                    applied_profile_id INTEGER,
                    applied_source TEXT,
                    applied_values_json TEXT,
                    applied_at TEXT,
                    updated_at TEXT NOT NULL,
                    FOREIGN KEY (selected_robot_type_id) REFERENCES robot_types(id),
                    FOREIGN KEY (selected_blade_type_id) REFERENCES blade_types(id),
                    FOREIGN KEY (applied_profile_id) REFERENCES transition_profiles(id)
                );

                CREATE TABLE IF NOT EXISTS configuration_events (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    timestamp TEXT NOT NULL,
                    event_type TEXT NOT NULL,
                    robot_type_id TEXT,
                    blade_type_id TEXT,
                    actor TEXT,
                    source TEXT,
                    before_json TEXT,
                    after_json TEXT,
                    details_json TEXT
                );

                CREATE INDEX IF NOT EXISTS idx_config_events_timestamp
                ON configuration_events(timestamp);

                CREATE INDEX IF NOT EXISTS idx_transition_profiles_pair
                ON transition_profiles(robot_type_id, blade_type_id);
                """
            )
            self.conn.execute(
                """
                INSERT INTO configuration_state (id, updated_at)
                VALUES (1, ?)
                ON CONFLICT(id) DO NOTHING
                """,
                (self._config_utc_now(),),
            )
            self.conn.commit()

    @staticmethod
    def _config_utc_now() -> str:
        from datetime import datetime, timezone
        return datetime.now(timezone.utc).isoformat()

    @staticmethod
    def _validate_transition_values(
        values: Any,
        max_count: int = 8,
    ) -> List[float]:
        if not isinstance(values, list):
            raise ValueError(
                "Transition values must be a list."
            )

        maximum = min(
            int(max_count),
            8,
        )

        if not 1 <= len(values) <= maximum:
            raise ValueError(
                "Transition count must be between "
                f"1 and {maximum}."
            )

        clean: List[float] = []

        for value in values:
            if (
                isinstance(value, bool)
                or not isinstance(
                    value,
                    (int, float),
                )
            ):
                raise ValueError(
                    "Every transition must be numeric."
                )

            number = float(value)

            if not math.isfinite(number):
                raise ValueError(
                    "Transition values must be finite."
                )

            if number < 0:
                raise ValueError(
                    "Transition values cannot be negative."
                )

            clean.append(number)

        for index in range(
            1,
            len(clean),
        ):
            if clean[index] < clean[index - 1]:
                raise ValueError(
                    "Transition values must be ascending."
                )

        return clean

    def upsert_robot_type(
        self,
        robot_id: str,
        name: str,
        channels: int,
    ) -> None:
        robot_id = robot_id.strip()
        name = name.strip()
        channels = int(channels)

        if not robot_id:
            raise ValueError("Robot ID is required.")

        if not name:
            raise ValueError("Robot name is required.")

        if not 1 <= channels <= 8:
            raise ValueError(
                "Robot channel count must be between 1 and 8."
            )

        now = self._config_utc_now()

        with self._lock:
            self.conn.execute(
                """
                INSERT INTO robot_types (
                    id,
                    name,
                    installed_actuator_channels,
                    enabled,
                    created_at,
                    updated_at
                )
                VALUES (?, ?, ?, 1, ?, ?)

                ON CONFLICT(id) DO UPDATE SET
                    name = excluded.name,
                    installed_actuator_channels =
                        excluded.installed_actuator_channels,
                    enabled = 1,
                    updated_at = excluded.updated_at
                """,
                (
                    robot_id,
                    name,
                    channels,
                    now,
                    now,
                ),
            )

            self.conn.commit()

    def upsert_blade_type(self, blade_id: str, name: str, max_transitions: int) -> None:
        max_transitions = int(max_transitions)
        if not 1 <= max_transitions <= 8:
            raise ValueError("max_transitions must be between 1 and 8.")
        now = self._config_utc_now()
        with self._lock:
            self.conn.execute(
                """
                INSERT INTO blade_types (id, name, max_transitions, created_at, updated_at)
                VALUES (?, ?, ?, ?, ?)
                ON CONFLICT(id) DO UPDATE SET
                    name = excluded.name,
                    max_transitions = excluded.max_transitions,
                    enabled = 1,
                    updated_at = excluded.updated_at
                """,
                (blade_id.strip(), name.strip(), max_transitions, now, now),
            )
            self.conn.commit()

    def upsert_default_transitions(
        self, *, robot_type_id: str, blade_type_id: str,
        values: List[float], source: str = "factory"
    ) -> Dict[str, Any]:
        now = self._config_utc_now()
        with self._lock:
            blade = self.conn.execute(
                "SELECT max_transitions FROM blade_types WHERE id = ? AND enabled = 1",
                (blade_type_id,),
            ).fetchone()
            robot = self.conn.execute(
                """
                SELECT
                    installed_actuator_channels AS channels
                FROM robot_types
                WHERE id = ?
                AND enabled = 1
                """,
                (robot_type_id,),
            ).fetchone()
            if blade is None or robot is None:
                raise ValueError("Unknown or disabled robot/blade type.")
            limit = min(int(blade["max_transitions"]), 8)
            clean = self._validate_transition_values(values, limit)
            existing = self.conn.execute(
                "SELECT default_values_json FROM transition_profiles WHERE robot_type_id=? AND blade_type_id=?",
                (robot_type_id, blade_type_id),
            ).fetchone()
            before = self._json_loads(existing["default_values_json"], None) if existing else None
            self.conn.execute(
                """
                INSERT INTO transition_profiles (
                    robot_type_id, blade_type_id, default_values_json,
                    default_revision, default_source, created_at, updated_at
                ) VALUES (?, ?, ?, 1, ?, ?, ?)
                ON CONFLICT(robot_type_id, blade_type_id) DO UPDATE SET
                    default_values_json = excluded.default_values_json,
                    default_revision = transition_profiles.default_revision + 1,
                    default_source = excluded.default_source,
                    updated_at = excluded.updated_at
                """,
                (robot_type_id, blade_type_id, self._json_dumps(clean), source, now, now),
            )
            self._insert_configuration_event_locked(
                "default_transitions_updated", robot_type_id, blade_type_id,
                actor="migration_or_cloud", source=source, before=before, after=clean,
            )
            self.conn.commit()
        return self.get_transition_profile(robot_type_id, blade_type_id)

    def set_transition_override(
        self, *, robot_type_id: str, blade_type_id: str,
        values: List[float], actor: str
    ) -> Dict[str, Any]:
        now = self._config_utc_now()
        with self._lock:
            try:
                self.conn.execute("BEGIN IMMEDIATE")
                row = self.conn.execute(
                    """
                    SELECT
                        p.override_values_json,
                        b.max_transitions,
                        r.installed_actuator_channels AS channels
                    FROM transition_profiles p
                    JOIN blade_types b
                        ON b.id = p.blade_type_id
                    JOIN robot_types r
                        ON r.id = p.robot_type_id
                    WHERE p.robot_type_id = ?
                    AND p.blade_type_id = ?
                    """,
                    (
                        robot_type_id,
                        blade_type_id,
                    ),
                ).fetchone()
                if row is None:
                    raise ValueError("No default transition profile exists for this robot/blade pair.")
                clean = self._validate_transition_values(values, min(int(row["max_transitions"]), 8))
                before = self._json_loads(row["override_values_json"], None)
                self.conn.execute(
                    """
                    UPDATE transition_profiles SET
                        override_values_json=?,
                        override_revision=override_revision+1,
                        override_updated_by=?, override_updated_at=?, updated_at=?
                    WHERE robot_type_id=? AND blade_type_id=?
                    """,
                    (self._json_dumps(clean), actor, now, now, robot_type_id, blade_type_id),
                )
                self._insert_configuration_event_locked(
                    "transition_override_saved", robot_type_id, blade_type_id,
                    actor=actor, source="operator_override", before=before, after=clean,
                )
                self.conn.commit()
            except Exception:
                self.conn.rollback()
                raise
        return self.get_transition_profile(robot_type_id, blade_type_id)

    def clear_transition_override(
        self, *, robot_type_id: str, blade_type_id: str, actor: str
    ) -> Dict[str, Any]:
        now = self._config_utc_now()
        with self._lock:
            try:
                self.conn.execute("BEGIN IMMEDIATE")
                row = self.conn.execute(
                    "SELECT override_values_json FROM transition_profiles WHERE robot_type_id=? AND blade_type_id=?",
                    (robot_type_id, blade_type_id),
                ).fetchone()
                if row is None:
                    raise ValueError("Transition profile not found.")
                before = self._json_loads(row["override_values_json"], None)
                self.conn.execute(
                    """
                    UPDATE transition_profiles SET
                        override_values_json=NULL,
                        override_revision=override_revision+1,
                        override_updated_by=?, override_updated_at=?, updated_at=?
                    WHERE robot_type_id=? AND blade_type_id=?
                    """,
                    (actor, now, now, robot_type_id, blade_type_id),
                )
                self._insert_configuration_event_locked(
                    "transition_override_cleared", robot_type_id, blade_type_id,
                    actor=actor, source="factory_default", before=before, after=None,
                )
                self.conn.commit()
            except Exception:
                self.conn.rollback()
                raise
        return self.get_transition_profile(robot_type_id, blade_type_id)

    def get_transition_profile(self, robot_type_id: str, blade_type_id: str) -> Dict[str, Any]:
        with self._lock:
            row = self.conn.execute(
                """
                SELECT
                    p.*,
                    r.installed_actuator_channels AS channels,
                    b.max_transitions
                FROM transition_profiles p
                JOIN robot_types r
                    ON r.id = p.robot_type_id
                JOIN blade_types b
                    ON b.id = p.blade_type_id
                WHERE p.robot_type_id = ?
                AND p.blade_type_id = ?
                """,
                (
                    robot_type_id,
                    blade_type_id,
                ),
            ).fetchone()
        if row is None:
            raise ValueError("Transition profile not found.")
        defaults = self._json_loads(row["default_values_json"], [])
        override = self._json_loads(row["override_values_json"], None)
        effective = override if override is not None else defaults
        return {
            "profile_id": row["id"],
            "robot_type_id": row["robot_type_id"],
            "blade_type_id": row["blade_type_id"],
            "channels": row["channels"],
            "max_transitions": row["max_transitions"],
            "default_values": defaults,
            "override_values": override,
            "effective_values": effective,
            "effective_source": "operator_override" if override is not None else "factory_default",
            "has_override": override is not None,
            "default_revision": row["default_revision"],
            "override_revision": row["override_revision"],
            "override_updated_by": row["override_updated_by"],
            "override_updated_at": row["override_updated_at"],
        }

    def save_motor_directions(
        self,
        *,
        directions: Dict[str, int],
        actor: str,
        event_type: str,
        restored_defaults: bool,
        transaction_id: str,
    ) -> None:
        from motor_direction_config import (
            validate_motor_directions,
        )

        clean = validate_motor_directions(
            directions
        )

        now = self._config_utc_now()

        with self._lock:
            try:
                self.conn.execute(
                    "BEGIN IMMEDIATE"
                )

                row = self.conn.execute(
                    """
                    SELECT motor_direction_json
                    FROM configuration_state
                    WHERE id = 1
                    """
                ).fetchone()

                if row is None:
                    raise RuntimeError(
                        "The configuration_state "
                        "singleton row is missing."
                    )

                before = self._json_loads(
                    row[
                        "motor_direction_json"
                    ],
                    {},
                )

                cursor = self.conn.execute(
                    """
                    UPDATE configuration_state
                    SET
                        motor_direction_json = ?,
                        updated_at = ?
                    WHERE id = 1
                    """,
                    (
                        self._json_dumps(
                            clean
                        ),
                        now,
                    ),
                )

                if cursor.rowcount != 1:
                    raise RuntimeError(
                        "Motor direction update "
                        "did not modify the "
                        "configuration_state row."
                    )

                self._insert_configuration_event_locked(
                    event_type,
                    None,
                    None,
                    actor=actor,
                    source="admin_hmi",
                    before=before,
                    after=clean,
                    details={
                        "restored_factory_defaults":
                            restored_defaults,
                        "transaction_id":
                            transaction_id,
                    },
                )

                self.conn.commit()

                verified_row = (
                    self.conn.execute(
                        """
                        SELECT
                            motor_direction_json
                        FROM configuration_state
                        WHERE id = 1
                        """
                    ).fetchone()
                )

                if verified_row is None:
                    raise RuntimeError(
                        "Motor directions could not "
                        "be reloaded after commit."
                    )

                verified = (
                    validate_motor_directions(
                        self._json_loads(
                            verified_row[
                                "motor_direction_json"
                            ],
                            {},
                        )
                    )
                )

                if verified != clean:
                    raise RuntimeError(
                        "Motor direction database "
                        "verification failed after "
                        "commit."
                    )

            except Exception:
                self.conn.rollback()
                raise

    def get_configuration_catalog(self) -> Dict[str, Any]:
        with self._lock:
            robots = self.conn.execute(
                """
                SELECT
                    id,
                    name,
                    installed_actuator_channels AS channels
                FROM robot_types
                WHERE enabled = 1
                ORDER BY name
                """
            ).fetchall()

            blades = self.conn.execute(
                """
                SELECT
                    id,
                    name,
                    max_transitions,
                    source
                FROM blade_types
                WHERE enabled = 1
                ORDER BY name
                """
            ).fetchall()

            archived_blades = self.conn.execute(
                """
                SELECT
                    id,
                    name,
                    max_transitions,
                    source
                FROM blade_types
                WHERE enabled = 0
                ORDER BY name
                """
            ).fetchall()

            profiles = self.conn.execute(
                """
                SELECT
                    robot_type_id,
                    blade_type_id
                FROM transition_profiles
                ORDER BY robot_type_id, blade_type_id
                """
            ).fetchall()

            state = self.conn.execute(
                """
                SELECT *
                FROM configuration_state
                WHERE id=1
                """
            ).fetchone()

        matrix: Dict[str, Dict[str, Any]] = {}

        for p in profiles:
            profile = self.get_transition_profile(
                p["robot_type_id"],
                p["blade_type_id"],
            )

            matrix.setdefault(
                p["robot_type_id"],
                {},
            )[p["blade_type_id"]] = profile

        return {
            "version": 2,
            "robots": [dict(r) for r in robots],
            "blade_types": [dict(b) for b in blades],
            "archived_blade_types": [
                dict(blade)
                for blade in archived_blades
            ],
            "profiles": matrix,
            "selection": {
                "robot_id":
                    state["selected_robot_type_id"]
                    if state else None,
                "blade_id":
                    state["selected_blade_type_id"]
                    if state else None,
            },
            "units":
                state["units"]
                if state else "m",
            "encoder_scale":
                state["encoder_scale"]
                if state else 1000.0,
            "motor_direction":
                self._json_loads(
                    state["motor_direction_json"],
                    {},
                )
                if state else {},
            "motor_tuning":
                self._json_loads(
                    state["motor_tuning_json"],
                    {},
                )
                if state else {},
        }

    def save_configuration_state(self, *, robot_type_id: Optional[str] = None,
                                 blade_type_id: Optional[str] = None,
                                 motor_direction: Optional[Dict[str, int]] = None,
                                 motor_tuning: Optional[Dict[str, Any]] = None) -> None:
        now = self._config_utc_now()
        with self._lock:
            self.conn.execute(
                """
                UPDATE configuration_state SET
                    selected_robot_type_id=COALESCE(?, selected_robot_type_id),
                    selected_blade_type_id=COALESCE(?, selected_blade_type_id),
                    motor_direction_json=COALESCE(?, motor_direction_json),
                    motor_tuning_json=COALESCE(?, motor_tuning_json),
                    updated_at=? WHERE id=1
                """,
                (robot_type_id, blade_type_id,
                 self._json_dumps(motor_direction) if motor_direction is not None else None,
                 self._json_dumps(motor_tuning) if motor_tuning is not None else None, now),
            )
            self.conn.commit()

    def mark_profile_applied(self, profile: Dict[str, Any]) -> None:
        now = self._config_utc_now()
        with self._lock:
            self.conn.execute(
                """
                UPDATE configuration_state SET
                    selected_robot_type_id=?, selected_blade_type_id=?,
                    applied_profile_id=?, applied_source=?, applied_values_json=?,
                    applied_at=?, updated_at=? WHERE id=1
                """,
                (profile["robot_type_id"], profile["blade_type_id"], profile["profile_id"],
                 profile["effective_source"], self._json_dumps(profile["effective_values"]), now, now),
            )
            self._insert_configuration_event_locked(
                "transition_profile_applied", profile["robot_type_id"], profile["blade_type_id"],
                actor="system", source=profile["effective_source"], before=None,
                after=profile["effective_values"],
            )
            self.conn.commit()

    def get_applied_transition_profile_snapshot(
        self,
    ) -> Optional[Dict[str, Any]]:
        """
        Return the transition values last recorded as applied to the H7.

        This reads configuration_state.applied_values_json rather than the
        currently selected or edited profile. The Control HMI must display
        what was applied, not what an administrator is viewing or editing.
        """
        with self._lock:
            row = self.conn.execute(
                """
                SELECT
                    applied_profile_id,
                    selected_robot_type_id,
                    selected_blade_type_id,
                    applied_source,
                    applied_values_json,
                    applied_at
                FROM configuration_state
                WHERE id = 1
                """
            ).fetchone()

        if row is None or row["applied_profile_id"] is None:
            return None

        values = self._json_loads(
            row["applied_values_json"],
            [],
        )

        clean_values = self._validate_transition_values(
            values,
            8,
        )

        return {
            "profile_id": int(row["applied_profile_id"]),
            "robot_id": row["selected_robot_type_id"],
            "blade_id": row["selected_blade_type_id"],
            "effective_source": row["applied_source"],
            "transition_values": clean_values,
            "applied_at": row["applied_at"],
        }


    def _insert_configuration_event_locked(self, event_type: str, robot_type_id: Optional[str],
                                           blade_type_id: Optional[str], *, actor: Optional[str],
                                           source: Optional[str], before: Any, after: Any,
                                           details: Any = None) -> None:
        self.conn.execute(
            """
            INSERT INTO configuration_events (
                timestamp,event_type,robot_type_id,blade_type_id,actor,source,
                before_json,after_json,details_json
            ) VALUES (?,?,?,?,?,?,?,?,?)
            """,
            (self._config_utc_now(), event_type, robot_type_id, blade_type_id, actor, source,
             self._json_dumps(before) if before is not None else None,
             self._json_dumps(after) if after is not None else None,
             self._json_dumps(details) if details is not None else None),
        )

    def create_blade_type(
        self,
        *,
        blade_id: str,
        name: str,
        max_transitions: int,
        copy_from_blade_id: str,
        actor: str,
    ) -> Dict[str, Any]:

        blade_id = blade_id.strip().lower()
        name = name.strip()
        copy_from_blade_id = (
            copy_from_blade_id.strip()
        )
        actor = actor.strip().upper()

        if not blade_id:
            raise ValueError(
                "blade_id is required."
            )

        import re

        if not re.fullmatch(
            r"[a-z0-9][a-z0-9-]{1,63}",
            blade_id,
        ):
            raise ValueError(
                "blade_id must contain 2 to 64 "
                "lowercase letters, numbers, "
                "or hyphens."
            )

        if not name:
            raise ValueError(
                "Blade name is required."
            )

        max_transitions = int(
            max_transitions
        )

        if not 1 <= max_transitions <= 8:
            raise ValueError(
                "max_transitions must be "
                "between 1 and 8."
            )

        if not copy_from_blade_id:
            raise ValueError(
                "copy_from_blade_id is required."
            )

        if not re.fullmatch(
            r"[A-Z]{2,6}",
            actor,
        ):
            raise ValueError(
                "actor_initials must contain "
                "2 to 6 letters."
            )

        now = self._config_utc_now()

        with self._lock:
            try:
                self.conn.execute(
                    "BEGIN IMMEDIATE"
                )

                existing = self.conn.execute(
                    """
                    SELECT id
                    FROM blade_types
                    WHERE id = ?
                    """,
                    (blade_id,),
                ).fetchone()

                if existing is not None:
                    raise ValueError(
                        f"Blade '{blade_id}' "
                        "already exists."
                    )

                source_blade = (
                    self.conn.execute(
                        """
                        SELECT
                            id,
                            name,
                            max_transitions
                        FROM blade_types
                        WHERE id = ?
                        AND enabled = 1
                        """,
                        (
                            copy_from_blade_id,
                        ),
                    ).fetchone()
                )

                if source_blade is None:
                    raise ValueError(
                        "The source blade does "
                        "not exist or is archived."
                    )

                source_profiles = (
                    self.conn.execute(
                        """
                        SELECT *
                        FROM transition_profiles
                        WHERE blade_type_id = ?
                        ORDER BY robot_type_id
                        """,
                        (
                            copy_from_blade_id,
                        ),
                    ).fetchall()
                )

                if not source_profiles:
                    raise ValueError(
                        "The source blade has no "
                        "transition profiles."
                    )

                for profile in source_profiles:
                    defaults = self._json_loads(
                        profile[
                            "default_values_json"
                        ],
                        [],
                    )

                    if (
                        len(defaults)
                        > max_transitions
                    ):
                        raise ValueError(
                            "The source blade has "
                            f"{len(defaults)} default "
                            "transitions, which exceeds "
                            "the new blade maximum of "
                            f"{max_transitions}."
                        )

                self.conn.execute(
                    """
                    INSERT INTO blade_types (
                        id,
                        name,
                        max_transitions,
                        source,
                        enabled,
                        created_at,
                        updated_at
                    )
                    VALUES (?, ?, ?, 'user_created', 1, ?, ?)
                    """,
                    (
                        blade_id,
                        name,
                        max_transitions,
                        now,
                        now,
                    ),
                )

                copied_robot_ids = []

                for profile in source_profiles:
                    self.conn.execute(
                        """
                        INSERT INTO transition_profiles (
                            robot_type_id,
                            blade_type_id,
                            default_values_json,
                            override_values_json,
                            default_revision,
                            override_revision,
                            default_source,
                            override_updated_by,
                            override_updated_at,
                            created_at,
                            updated_at
                        )
                        VALUES (
                            ?, ?, ?, NULL,
                            1, 0, ?,
                            NULL, NULL,
                            ?, ?
                        )
                        """,
                        (
                            profile[
                                "robot_type_id"
                            ],
                            blade_id,
                            profile[
                                "default_values_json"
                            ],
                            (
                                "copied_from:"
                                f"{copy_from_blade_id}"
                            ),
                            now,
                            now,
                        ),
                    )

                    copied_robot_ids.append(
                        profile[
                            "robot_type_id"
                        ]
                    )

                self._insert_configuration_event_locked(
                    "blade_type_created",
                    None,
                    blade_id,
                    actor=actor,
                    source="admin_hmi",
                    before=None,
                    after={
                        "id": blade_id,
                        "name": name,
                        "max_transitions":
                            max_transitions,
                    },
                    details={
                        "identity_type":
                            "self_entered_vestas_initials",
                        "authorization_role":
                            "admin",
                        "copy_from_blade_id":
                            copy_from_blade_id,
                        "copied_robot_type_ids":
                            copied_robot_ids,
                    },
                )

                self.conn.commit()

            except Exception:
                self.conn.rollback()
                raise

        return {
            "id": blade_id,
            "name": name,
            "max_transitions":
                max_transitions,
        }


    def archive_blade_type(
        self,
        blade_id: str,
        actor: str,
    ) -> None:
        blade_id = blade_id.strip()
        actor = actor.strip().upper()
        now = self._config_utc_now()

        with self._lock:
            try:
                self.conn.execute(
                    "BEGIN IMMEDIATE"
                )

                blade = self.conn.execute(
                    """
                    SELECT
                        id,
                        name,
                        max_transitions,
                        enabled
                    FROM blade_types
                    WHERE id = ?
                    """,
                    (blade_id,),
                ).fetchone()

                if blade is None:
                    raise ValueError(
                        "Blade type not found."
                    )

                if not bool(blade["enabled"]):
                    raise ValueError(
                        "Blade type is already archived."
                    )

                state = self.conn.execute(
                    """
                    SELECT
                        selected_blade_type_id
                    FROM configuration_state
                    WHERE id = 1
                    """
                ).fetchone()

                if (
                    state is not None
                    and state[
                        "selected_blade_type_id"
                    ]
                    == blade_id
                ):
                    raise ValueError(
                        "The currently selected blade "
                        "cannot be archived. Select and "
                        "apply another blade first."
                    )

                active_job = self.conn.execute(
                    """
                    SELECT job_uuid
                    FROM jobs
                    WHERE blade_type_id = ?
                    AND state IN (
                        'READY',
                        'STARTING',
                        'RUNNING',
                        'PAUSED',
                        'COMPLETING'
                    )
                    LIMIT 1
                    """,
                    (blade_id,),
                ).fetchone()

                if active_job is not None:
                    raise ValueError(
                        "The blade type is used by "
                        "an active job and cannot be "
                        "archived."
                    )

                self.conn.execute(
                    """
                    UPDATE blade_types
                    SET
                        enabled = 0,
                        updated_at = ?
                    WHERE id = ?
                    """,
                    (
                        now,
                        blade_id,
                    ),
                )

                self._insert_configuration_event_locked(
                    "blade_type_archived",
                    None,
                    blade_id,
                    actor=actor,
                    source="admin_hmi",
                    before=dict(blade),
                    after={
                        **dict(blade),
                        "enabled": 0,
                    },
                    details={
                        "identity_type":
                            "self_entered_vestas_initials",
                        "authorization_role":
                            "admin",
                    },
                )

                self.conn.commit()

            except Exception:
                self.conn.rollback()
                raise

    def restore_blade_type(
        self,
        blade_id: str,
        actor: str,
    ) -> None:
        blade_id = blade_id.strip()
        actor = actor.strip().upper()
        now = self._config_utc_now()

        with self._lock:
            try:
                self.conn.execute(
                    "BEGIN IMMEDIATE"
                )

                blade = self.conn.execute(
                    """
                    SELECT
                        id,
                        name,
                        max_transitions,
                        enabled
                    FROM blade_types
                    WHERE id = ?
                    """,
                    (blade_id,),
                ).fetchone()

                if blade is None:
                    raise ValueError(
                        "Blade type not found."
                    )

                if bool(blade["enabled"]):
                    raise ValueError(
                        "Blade type is already active."
                    )

                profile_count = (
                    self.conn.execute(
                        """
                        SELECT COUNT(*) AS count
                        FROM transition_profiles
                        WHERE blade_type_id = ?
                        """,
                        (blade_id,),
                    ).fetchone()
                )

                if (
                    profile_count is None
                    or int(
                        profile_count["count"]
                    )
                    == 0
                ):
                    raise ValueError(
                        "The archived blade has no "
                        "transition profiles and cannot "
                        "be restored for operation."
                    )

                self.conn.execute(
                    """
                    UPDATE blade_types
                    SET
                        enabled = 1,
                        updated_at = ?
                    WHERE id = ?
                    """,
                    (
                        now,
                        blade_id,
                    ),
                )

                self._insert_configuration_event_locked(
                    "blade_type_restored",
                    None,
                    blade_id,
                    actor=actor,
                    source="admin_hmi",
                    before=dict(blade),
                    after={
                        **dict(blade),
                        "enabled": 1,
                    },
                    details={
                        "identity_type":
                            "self_entered_vestas_initials",
                        "authorization_role":
                            "admin",
                    },
                )

                self.conn.commit()

            except Exception:
                self.conn.rollback()
                raise


    def update_blade_type(
        self,
        *,
        blade_id: str,
        name: str,
        max_transitions: int,
        actor: str,
    ) -> Dict[str, Any]:
        blade_id = blade_id.strip()
        name = name.strip()
        actor = actor.strip().upper()
        max_transitions = int(
            max_transitions
        )

        if not name:
            raise ValueError(
                "Blade name is required."
            )

        if not 1 <= max_transitions <= 8:
            raise ValueError(
                "max_transitions must be "
                "between 1 and 8."
            )

        now = self._config_utc_now()

        with self._lock:
            try:
                self.conn.execute(
                    "BEGIN IMMEDIATE"
                )

                blade = self.conn.execute(
                    """
                    SELECT
                        id,
                        name,
                        max_transitions,
                        enabled
                    FROM blade_types
                    WHERE id = ?
                    """,
                    (blade_id,),
                ).fetchone()

                if blade is None:
                    raise ValueError(
                        "Blade type not found."
                    )

                profiles = self.conn.execute(
                    """
                    SELECT
                        default_values_json,
                        override_values_json
                    FROM transition_profiles
                    WHERE blade_type_id = ?
                    """,
                    (blade_id,),
                ).fetchall()

                for profile in profiles:
                    defaults = self._json_loads(
                        profile[
                            "default_values_json"
                        ],
                        [],
                    )

                    override = self._json_loads(
                        profile[
                            "override_values_json"
                        ],
                        None,
                    )

                    if (
                        len(defaults)
                        > max_transitions
                    ):
                        raise ValueError(
                            "An existing default profile "
                            "contains more transitions "
                            "than the requested maximum."
                        )

                    if (
                        override is not None
                        and len(override)
                        > max_transitions
                    ):
                        raise ValueError(
                            "An existing override contains "
                            "more transitions than the "
                            "requested maximum."
                        )

                self.conn.execute(
                    """
                    UPDATE blade_types
                    SET
                        name = ?,
                        max_transitions = ?,
                        updated_at = ?
                    WHERE id = ?
                    """,
                    (
                        name,
                        max_transitions,
                        now,
                        blade_id,
                    ),
                )

                after = {
                    "id": blade_id,
                    "name": name,
                    "max_transitions":
                        max_transitions,
                    "enabled":
                        blade["enabled"],
                }

                self._insert_configuration_event_locked(
                    "blade_type_updated",
                    None,
                    blade_id,
                    actor=actor,
                    source="admin_hmi",
                    before=dict(blade),
                    after=after,
                    details={
                        "identity_type":
                            "self_entered_vestas_initials",
                        "authorization_role":
                            "admin",
                    },
                )

                self.conn.commit()

            except Exception:
                self.conn.rollback()
                raise

        return {
            "id": blade_id,
            "name": name,
            "max_transitions":
                max_transitions,
        }

    def seed_default_configuration_if_empty(self) -> None:
        with self._lock:
            row = self.conn.execute(
                """
                SELECT COUNT(*) AS count
                FROM robot_types
                """
            ).fetchone()

        if row["count"] > 0:
            return

        self.upsert_robot_type(
            robot_id="grlrr-standard",
            name="GRLRR Standard",
            channels=4,
        )

        self.upsert_blade_type(
            blade_id="default-blade",
            name="Default Blade",
            max_transitions=4,
        )

        self.upsert_default_transitions(
            robot_type_id="grlrr-standard",
            blade_type_id="default-blade",
            values=[
                0.25,
                0.50,
                0.75,
                1.00,
            ],
            source="factory",
        )

        self.save_configuration_state(
            robot_type_id="grlrr-standard",
            blade_type_id="default-blade",
        )

    def migrate_configuration_tables(
        self,
    ) -> None:
        with self._lock:
            columns = {
                row["name"]
                for row in self.conn.execute(
                    """
                    PRAGMA table_info(
                        blade_types
                    )
                    """
                ).fetchall()
            }

            if "source" not in columns:
                self.conn.execute(
                    """
                    ALTER TABLE blade_types
                    ADD COLUMN source TEXT
                    NOT NULL DEFAULT 'factory'
                    """
                )

            self.conn.commit()

    def delete_blade_type(
        self,
        blade_id: str,
        actor: str,
    ) -> None:
        blade_id = blade_id.strip()
        actor = actor.strip().upper()

        import re

        if not re.fullmatch(
            r"[A-Z]{2,6}",
            actor,
        ):
            raise ValueError(
                "actor_initials must contain "
                "2 to 6 letters."
            )

        with self._lock:
            try:
                self.conn.execute(
                    "BEGIN IMMEDIATE"
                )

                blade = self.conn.execute(
                    """
                    SELECT
                        id,
                        name,
                        max_transitions,
                        source,
                        enabled
                    FROM blade_types
                    WHERE id = ?
                    """,
                    (blade_id,),
                ).fetchone()

                if blade is None:
                    raise ValueError(
                        "Blade type not found."
                    )

                if blade["source"] != "user_created":
                    raise ValueError(
                        "Factory blade types cannot "
                        "be permanently deleted."
                    )

                if bool(blade["enabled"]):
                    raise ValueError(
                        "Archive the blade type before "
                        "permanently deleting it."
                    )

                state = self.conn.execute(
                    """
                    SELECT
                        selected_blade_type_id,
                        applied_profile_id
                    FROM configuration_state
                    WHERE id = 1
                    """
                ).fetchone()

                if (
                    state is not None
                    and state["selected_blade_type_id"]
                    == blade_id
                ):
                    raise ValueError(
                        "The selected blade type "
                        "cannot be deleted."
                    )

                applied_profile = self.conn.execute(
                    """
                    SELECT id
                    FROM transition_profiles
                    WHERE blade_type_id = ?
                    AND id = ?
                    """,
                    (
                        blade_id,
                        (
                            state["applied_profile_id"]
                            if state is not None
                            else None
                        ),
                    ),
                ).fetchone()

                if applied_profile is not None:
                    raise ValueError(
                        "The applied blade profile "
                        "cannot be deleted. Apply "
                        "another blade first."
                    )

                job_usage = self.conn.execute(
                    """
                    SELECT
                        COUNT(*) AS count
                    FROM jobs
                    WHERE blade_type_id = ?
                    """,
                    (blade_id,),
                ).fetchone()

                job_count = int(
                    job_usage["count"]
                    if job_usage is not None
                    else 0
                )

                if job_count > 0:
                    raise ValueError(
                        "This blade type has been used "
                        f"by {job_count} job(s). It can "
                        "be archived, but it cannot be "
                        "permanently deleted."
                    )

                before = dict(blade)

                # Record the event before deleting the blade.
                #
                # configuration_events does not use a foreign
                # key to blade_types, so this audit record remains.
                self._insert_configuration_event_locked(
                    "blade_type_deleted",
                    None,
                    blade_id,
                    actor=actor,
                    source="admin_hmi",
                    before=before,
                    after=None,
                    details={
                        "identity_type":
                            "self_entered_vestas_initials",
                        "authorization_role":
                            "admin",
                        "deletion_type":
                            "permanent",
                    },
                )

                # transition_profiles does not currently specify
                # ON DELETE CASCADE, so delete the child profiles
                # explicitly first.
                self.conn.execute(
                    """
                    DELETE FROM transition_profiles
                    WHERE blade_type_id = ?
                    """,
                    (blade_id,),
                )

                self.conn.execute(
                    """
                    DELETE FROM blade_types
                    WHERE id = ?
                    """,
                    (blade_id,),
                )

                self.conn.commit()

            except Exception:
                self.conn.rollback()
                raise