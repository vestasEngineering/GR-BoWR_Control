from __future__ import annotations

import json
from typing import Any, Dict, Optional, Sequence


FACTORY_MOTOR_DIRECTIONS = (
    1,
    -1,
    -1,
    1,
)

FACTORY_ENCODER_DIRECTIONS = (
    1,
    -1,
    1,
    -1,
)


class DriveDirectionConfigurationDatabaseMixin:
    """SQLite persistence for drive installation directions.

    The current motor-output and encoder-normalization directions are stored
    together as one singleton configuration. The host class must provide
    ``self.conn`` and ``self._lock``.
    """

    def create_drive_direction_configuration_tables(self) -> None:
        """Create the current configuration and append-only event tables."""
        with self._lock:
            self.conn.execute(
                """
                CREATE TABLE IF NOT EXISTS drive_direction_configuration (
                    singleton_id INTEGER PRIMARY KEY
                        CHECK (singleton_id = 1),
                    schema_version INTEGER NOT NULL DEFAULT 2,
                    motor_1 INTEGER NOT NULL CHECK (motor_1 IN (-1, 1)),
                    motor_2 INTEGER NOT NULL CHECK (motor_2 IN (-1, 1)),
                    motor_3 INTEGER NOT NULL CHECK (motor_3 IN (-1, 1)),
                    motor_4 INTEGER NOT NULL CHECK (motor_4 IN (-1, 1)),
                    encoder_1 INTEGER NOT NULL CHECK (encoder_1 IN (-1, 1)),
                    encoder_2 INTEGER NOT NULL CHECK (encoder_2 IN (-1, 1)),
                    encoder_3 INTEGER NOT NULL CHECK (encoder_3 IN (-1, 1)),
                    encoder_4 INTEGER NOT NULL CHECK (encoder_4 IN (-1, 1)),
                    updated_at TEXT NOT NULL,
                    updated_by TEXT,
                    transaction_id TEXT NOT NULL
                )
                """
            )

            self.conn.execute(
                """
                CREATE TABLE IF NOT EXISTS drive_direction_configuration_events (
                    event_id INTEGER PRIMARY KEY AUTOINCREMENT,
                    created_at TEXT NOT NULL,
                    actor_initials TEXT,
                    transaction_id TEXT NOT NULL,
                    motor_directions_json TEXT NOT NULL,
                    encoder_directions_json TEXT NOT NULL,
                    source TEXT NOT NULL,
                    CHECK (json_valid(motor_directions_json)),
                    CHECK (json_valid(encoder_directions_json))
                )
                """
            )

            self.conn.execute(
                """
                CREATE UNIQUE INDEX IF NOT EXISTS
                    idx_drive_direction_event_transaction
                ON drive_direction_configuration_events(transaction_id)
                """
            )

            self.conn.commit()

    def migrate_drive_direction_configuration(self) -> None:
        """Seed the singleton row with the firmware's existing sign behavior."""
        with self._lock:
            self.conn.execute(
                """
                INSERT OR IGNORE INTO drive_direction_configuration (
                    singleton_id,
                    schema_version,
                    motor_1,
                    motor_2,
                    motor_3,
                    motor_4,
                    encoder_1,
                    encoder_2,
                    encoder_3,
                    encoder_4,
                    updated_at,
                    updated_by,
                    transaction_id
                )
                VALUES (
                    1,
                    2,
                    1,
                    -1,
                    -1,
                    1,
                    1,
                    -1,
                    1,
                    -1,
                    CURRENT_TIMESTAMP,
                    NULL,
                    'migration-factory-defaults'
                )
                """
            )

            self.conn.execute(
                """
                INSERT OR IGNORE INTO drive_direction_configuration_events (
                    created_at,
                    actor_initials,
                    transaction_id,
                    motor_directions_json,
                    encoder_directions_json,
                    source
                )
                VALUES (
                    CURRENT_TIMESTAMP,
                    NULL,
                    'migration-factory-defaults',
                    '[1,-1,-1,1]',
                    '[1,-1,1,-1]',
                    'database_migration'
                )
                """
            )

            self.conn.commit()

    def get_drive_direction_configuration(self) -> Optional[Dict[str, Any]]:
        """Return the persisted four-axis motor and encoder directions."""
        with self._lock:
            row = self.conn.execute(
                """
                SELECT
                    schema_version,
                    motor_1,
                    motor_2,
                    motor_3,
                    motor_4,
                    encoder_1,
                    encoder_2,
                    encoder_3,
                    encoder_4,
                    updated_at,
                    updated_by,
                    transaction_id
                FROM drive_direction_configuration
                WHERE singleton_id = 1
                """
            ).fetchone()

        if row is None:
            return None

        motor_directions = [
            int(row["motor_1"]),
            int(row["motor_2"]),
            int(row["motor_3"]),
            int(row["motor_4"]),
        ]

        encoder_directions = [
            int(row["encoder_1"]),
            int(row["encoder_2"]),
            int(row["encoder_3"]),
            int(row["encoder_4"]),
        ]

        self._validate_drive_direction_values(
            motor_directions,
            "motor_directions",
        )
        self._validate_drive_direction_values(
            encoder_directions,
            "encoder_directions",
        )

        return {
            "schema_version": int(row["schema_version"]),
            "motor_directions": motor_directions,
            "encoder_directions": encoder_directions,
            "updated_at": row["updated_at"],
            "updated_by": row["updated_by"],
            "transaction_id": row["transaction_id"],
        }

    def save_drive_direction_configuration(
        self,
        motor_directions: Sequence[int],
        encoder_directions: Sequence[int],
        actor_initials: Optional[str],
        transaction_id: str,
    ) -> Dict[str, Any]:
        """Persist one H7-confirmed configuration and its audit event."""
        normalized_motor = self._validate_drive_direction_values(
            motor_directions,
            "motor_directions",
        )
        normalized_encoder = self._validate_drive_direction_values(
            encoder_directions,
            "encoder_directions",
        )

        normalized_transaction_id = str(transaction_id).strip()
        if not normalized_transaction_id:
            raise ValueError("transaction_id is required.")

        normalized_actor = (
            str(actor_initials).strip().upper()
            if actor_initials is not None
            else None
        )
        if normalized_actor == "":
            normalized_actor = None

        motor_json = json.dumps(normalized_motor, separators=(",", ":"))
        encoder_json = json.dumps(normalized_encoder, separators=(",", ":"))

        with self._lock:
            try:
                self.conn.execute("BEGIN IMMEDIATE")

                self.conn.execute(
                    """
                    INSERT INTO drive_direction_configuration (
                        singleton_id,
                        schema_version,
                        motor_1,
                        motor_2,
                        motor_3,
                        motor_4,
                        encoder_1,
                        encoder_2,
                        encoder_3,
                        encoder_4,
                        updated_at,
                        updated_by,
                        transaction_id
                    )
                    VALUES (
                        1,
                        2,
                        ?,
                        ?,
                        ?,
                        ?,
                        ?,
                        ?,
                        ?,
                        ?,
                        CURRENT_TIMESTAMP,
                        ?,
                        ?
                    )
                    ON CONFLICT(singleton_id)
                    DO UPDATE SET
                        schema_version = excluded.schema_version,
                        motor_1 = excluded.motor_1,
                        motor_2 = excluded.motor_2,
                        motor_3 = excluded.motor_3,
                        motor_4 = excluded.motor_4,
                        encoder_1 = excluded.encoder_1,
                        encoder_2 = excluded.encoder_2,
                        encoder_3 = excluded.encoder_3,
                        encoder_4 = excluded.encoder_4,
                        updated_at = excluded.updated_at,
                        updated_by = excluded.updated_by,
                        transaction_id = excluded.transaction_id
                    """,
                    (
                        normalized_motor[0],
                        normalized_motor[1],
                        normalized_motor[2],
                        normalized_motor[3],
                        normalized_encoder[0],
                        normalized_encoder[1],
                        normalized_encoder[2],
                        normalized_encoder[3],
                        normalized_actor,
                        normalized_transaction_id,
                    ),
                )

                self.conn.execute(
                    """
                    INSERT OR IGNORE INTO drive_direction_configuration_events (
                        created_at,
                        actor_initials,
                        transaction_id,
                        motor_directions_json,
                        encoder_directions_json,
                        source
                    )
                    VALUES (
                        CURRENT_TIMESTAMP,
                        ?,
                        ?,
                        ?,
                        ?,
                        'h7_confirmed_operator_configuration'
                    )
                    """,
                    (
                        normalized_actor,
                        normalized_transaction_id,
                        motor_json,
                        encoder_json,
                    ),
                )

                self.conn.commit()
            except Exception:
                self.conn.rollback()
                raise

        return {
            "schema_version": 2,
            "motor_directions": list(normalized_motor),
            "encoder_directions": list(normalized_encoder),
            "updated_by": normalized_actor,
            "transaction_id": normalized_transaction_id,
        }

    @staticmethod
    def _validate_drive_direction_values(
        values: Sequence[int],
        field_name: str,
    ) -> list[int]:
        """Validate and normalize one four-axis direction sequence."""
        if not isinstance(values, (list, tuple)):
            raise ValueError(f"{field_name} must be a list or tuple.")

        if len(values) != 4:
            raise ValueError(f"{field_name} must contain exactly four values.")

        normalized: list[int] = []

        for index, raw_value in enumerate(values):
            if isinstance(raw_value, bool):
                raise ValueError(f"{field_name}[{index}] must be 1 or -1.")

            try:
                value = int(raw_value)
            except (TypeError, ValueError) as error:
                raise ValueError(
                    f"{field_name}[{index}] must be 1 or -1."
                ) from error

            if value not in (-1, 1):
                raise ValueError(f"{field_name}[{index}] must be 1 or -1.")

            normalized.append(value)

        return normalized
