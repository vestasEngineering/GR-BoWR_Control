from __future__ import annotations

from typing import Any, Dict, List, Optional
from datetime import datetime, timezone


class ServiceDatabaseMixin:
    """Persisted diagnostics and unified service-event queries."""

    def create_service_tables(self) -> None:
        with self._lock:
            self.conn.executescript(
                """
                CREATE TABLE IF NOT EXISTS diagnostic_runs (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    run_uuid TEXT NOT NULL UNIQUE,
                    diagnostic_id TEXT NOT NULL,
                    module_id TEXT NOT NULL,
                    state TEXT NOT NULL CHECK (
                        state IN ('QUEUED','RUNNING','PASSED','FAILED','ABORTED','TIMED_OUT')
                    ),
                    requested_by TEXT,
                    started_at TEXT NOT NULL,
                    completed_at TEXT,
                    message TEXT,
                    result_json TEXT,
                    transaction_id TEXT,
                    session_id TEXT
                );

                CREATE INDEX IF NOT EXISTS idx_diagnostic_runs_started
                ON diagnostic_runs(started_at DESC);

                CREATE INDEX IF NOT EXISTS idx_diagnostic_runs_module
                ON diagnostic_runs(module_id, started_at DESC);
                """
            )
            self.conn.commit()

    def create_diagnostic_run(
        self,
        *,
        run_uuid: str,
        diagnostic_id: str,
        module_id: str,
        requested_by: Optional[str],
        transaction_id: str,
        session_id: Optional[str],
    ) -> Dict[str, Any]:
        now = datetime.now(timezone.utc).isoformat()
        with self._lock:
            self.conn.execute(
                """
                INSERT INTO diagnostic_runs (
                    run_uuid, diagnostic_id, module_id, state,
                    requested_by, started_at, transaction_id, session_id
                ) VALUES (?, ?, ?, 'QUEUED', ?, ?, ?, ?)
                """,
                (run_uuid, diagnostic_id, module_id, requested_by, now,
                 transaction_id, session_id),
            )
            self.conn.commit()
        return self.get_diagnostic_run(run_uuid)

    def update_diagnostic_run(
        self,
        run_uuid: str,
        *,
        state: str,
        message: Optional[str] = None,
        result: Optional[Dict[str, Any]] = None,
    ) -> Dict[str, Any]:
        normalized = state.strip().upper()
        terminal = normalized in {'PASSED', 'FAILED', 'ABORTED', 'TIMED_OUT'}
        if normalized not in {'QUEUED','RUNNING','PASSED','FAILED','ABORTED','TIMED_OUT'}:
            raise ValueError(f'Unsupported diagnostic state: {state}')
        completed_at = datetime.now(timezone.utc).isoformat() if terminal else None
        with self._lock:
            self.conn.execute(
                """
                UPDATE diagnostic_runs
                SET state = ?, message = COALESCE(?, message),
                    result_json = COALESCE(?, result_json),
                    completed_at = COALESCE(?, completed_at)
                WHERE run_uuid = ?
                """,
                (normalized, message,
                 self._json_dumps(result) if result is not None else None,
                 completed_at, run_uuid),
            )
            self.conn.commit()
        return self.get_diagnostic_run(run_uuid)

    def get_diagnostic_run(self, run_uuid: str) -> Dict[str, Any]:
        with self._lock:
            row = self.conn.execute(
                "SELECT * FROM diagnostic_runs WHERE run_uuid = ?", (run_uuid,)
            ).fetchone()
        if row is None:
            raise ValueError('Diagnostic run not found.')
        return self._diagnostic_row(row)

    def get_latest_diagnostic_for_module(self, module_id: str) -> Optional[Dict[str, Any]]:
        with self._lock:
            row = self.conn.execute(
                """
                SELECT * FROM diagnostic_runs
                WHERE module_id = ?
                ORDER BY started_at DESC, id DESC LIMIT 1
                """,
                (module_id,),
            ).fetchone()
        return None if row is None else self._diagnostic_row(row)

    def _diagnostic_row(self, row) -> Dict[str, Any]:
        return {
            'run_id': row['run_uuid'],
            'diagnostic_id': row['diagnostic_id'],
            'module_id': row['module_id'],
            'state': row['state'].lower(),
            'requested_by': row['requested_by'],
            'started_at': row['started_at'],
            'completed_at': row['completed_at'],
            'message': row['message'],
            'result': self._json_loads(row['result_json'], None),
            'transaction_id': row['transaction_id'],
            'session_id': row['session_id'],
        }

    def list_service_events(
        self,
        *,
        limit: int = 50,
        offset: int = 0,
        search: str = '',
        level: str = 'all',
        source: str = 'all',
    ) -> Dict[str, Any]:
        limit = max(1, min(int(limit), 200))
        offset = max(0, int(offset))
        search = str(search or '').strip().lower()
        level = str(level or 'all').strip().lower()
        source = str(source or 'all').strip().lower()
        if level not in {'all','debug','info','warning','error','critical'}:
            raise ValueError('Unsupported event level filter.')
        if source not in {'all','system','job','configuration','diagnostic'}:
            raise ValueError('Unsupported event source filter.')

        union_sql = """
            SELECT 'system:' || id AS event_id, timestamp, LOWER(level) AS level,
                   event_type, message, 'system' AS source,
                   NULL AS module_id, NULL AS job_uuid, NULL AS actor,
                   data_json AS data_json
            FROM system_events
            UNION ALL
            SELECT 'job:' || e.id, e.timestamp, 'info', e.event_type,
                   e.event_type, 'job', NULL, j.job_uuid, NULL, e.data_json
            FROM job_events e JOIN jobs j ON j.id = e.job_id
            UNION ALL
            SELECT 'config:' || id, timestamp, 'info', event_type,
                   event_type, 'configuration', NULL, NULL, actor,
                   details_json
            FROM configuration_events
            UNION ALL
            SELECT 'diagnostic:' || id, started_at,
                   CASE state WHEN 'FAILED' THEN 'error'
                              WHEN 'TIMED_OUT' THEN 'error'
                              WHEN 'ABORTED' THEN 'warning'
                              ELSE 'info' END,
                   diagnostic_id,
                   COALESCE(message, diagnostic_id), 'diagnostic', module_id,
                   NULL, requested_by, result_json
            FROM diagnostic_runs
        """
        conditions = ['1=1']
        params: List[Any] = []
        if level != 'all':
            conditions.append('level = ?')
            params.append(level)
        if source != 'all':
            conditions.append('source = ?')
            params.append(source)
        if search:
            escaped = search.replace('\\', '\\\\').replace('%', '\\%').replace('_', '\\_')
            pattern = f'%{escaped}%'
            conditions.append("(LOWER(event_type) LIKE ? ESCAPE '\\' OR LOWER(message) LIKE ? ESCAPE '\\' OR LOWER(COALESCE(job_uuid,'')) LIKE ? ESCAPE '\\' OR LOWER(COALESCE(module_id,'')) LIKE ? ESCAPE '\\')")
            params.extend([pattern] * 4)
        where_sql = ' AND '.join(conditions)
        with self._lock:
            count = self.conn.execute(
                f'SELECT COUNT(*) AS count FROM ({union_sql}) WHERE {where_sql}',
                tuple(params),
            ).fetchone()
            rows = self.conn.execute(
                f"""
                SELECT * FROM ({union_sql})
                WHERE {where_sql}
                ORDER BY timestamp DESC, event_id DESC
                LIMIT ? OFFSET ?
                """,
                (*params, limit, offset),
            ).fetchall()
        items = [{
            'id': row['event_id'],
            'timestamp': row['timestamp'],
            'level': row['level'],
            'event_type': row['event_type'],
            'message': row['message'],
            'source': row['source'],
            'module_id': row['module_id'],
            'job_uuid': row['job_uuid'],
            'actor': row['actor'],
            'data': self._json_loads(row['data_json'], {}),
        } for row in rows]
        total = int(count['count'] if count else 0)
        return {'items': items, 'total': total, 'limit': limit, 'offset': offset,
                'has_more': offset + len(items) < total}

