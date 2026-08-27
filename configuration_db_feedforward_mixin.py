from __future__ import annotations

from typing import Any, Dict

from feedforward_config import FACTORY_FEEDFORWARD, validate_feedforward


class FeedforwardConfigurationDatabaseMixin:
    """Complete mixin for persisted feedforward configuration and audit events."""

    def migrate_feedforward_configuration(self) -> None:
        with self._lock:
            columns = {row["name"] for row in self.conn.execute(
                "PRAGMA table_info(configuration_state)"
            ).fetchall()}
            if "feedforward_json" not in columns:
                self.conn.execute(
                    "ALTER TABLE configuration_state ADD COLUMN feedforward_json TEXT"
                )
            self.conn.execute(
                "UPDATE configuration_state SET feedforward_json = ? "
                "WHERE id = 1 AND (feedforward_json IS NULL OR TRIM(feedforward_json) = '')",
                (self._json_dumps(FACTORY_FEEDFORWARD),),
            )
            self.conn.commit()

    def get_feedforward_configuration(self) -> Dict[str, float]:
        with self._lock:
            row = self.conn.execute(
                "SELECT feedforward_json FROM configuration_state WHERE id = 1"
            ).fetchone()
        if row is None:
            raise RuntimeError("configuration_state row is missing")
        return validate_feedforward(
            self._json_loads(row["feedforward_json"], dict(FACTORY_FEEDFORWARD))
        )

    def save_feedforward_configuration(self, *, values: Dict[str, Any], actor: str,
                                       event_type: str,
                                       restored_defaults: bool) -> None:
        clean = validate_feedforward(values)
        now = self._config_utc_now()
        with self._lock:
            try:
                self.conn.execute("BEGIN IMMEDIATE")
                row = self.conn.execute(
                    "SELECT feedforward_json FROM configuration_state WHERE id = 1"
                ).fetchone()
                before = self._json_loads(row["feedforward_json"], {}) if row else {}
                self.conn.execute(
                    "UPDATE configuration_state SET feedforward_json = ?, updated_at = ? WHERE id = 1",
                    (self._json_dumps(clean), now),
                )
                self._insert_configuration_event_locked(
                    event_type, None, None, actor=actor, source="admin_hmi",
                    before=before, after=clean,
                    details={"restored_factory_defaults": restored_defaults},
                )
                self.conn.commit()
            except Exception:
                self.conn.rollback()
                raise
