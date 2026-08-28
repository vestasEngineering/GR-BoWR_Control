#!/usr/bin/env python3

from database_service import DB_FILE
import sqlite3


def main() -> None:
    print(f"Database: {DB_FILE.resolve()}")

    conn = sqlite3.connect(str(DB_FILE))

    try:
        conn.execute("PRAGMA foreign_keys = ON")
        conn.execute("BEGIN IMMEDIATE")

        job_count = conn.execute(
            "SELECT COUNT(*) FROM jobs"
        ).fetchone()[0]

        job_event_count = conn.execute(
            "SELECT COUNT(*) FROM job_events"
        ).fetchone()[0]

        system_event_count = conn.execute(
            "SELECT COUNT(*) FROM system_events"
        ).fetchone()[0]

        print(
            f"Removing {job_count} jobs, "
            f"{job_event_count} job events, "
            f"{system_event_count} system events..."
        )

        conn.execute("DELETE FROM job_events")
        conn.execute("DELETE FROM jobs")
        conn.execute("DELETE FROM system_events")

        conn.execute(
            """
            DELETE FROM sqlite_sequence
            WHERE name IN (
                'jobs',
                'job_events',
                'system_events'
            )
            """
        )

        conn.commit()

        print("History cleared successfully.")

    except Exception:
        conn.rollback()
        raise

    finally:
        conn.close()


if __name__ == "__main__":
    main()