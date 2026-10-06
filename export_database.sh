#!/bin/bash

set -e

APP_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
SOURCE_DB="$APP_DIR/grbowr.db"
EXPORT_DB="$APP_DIR/grbowr-download.db"

rm -f "$EXPORT_DB"

sqlite3 "$SOURCE_DB" ".backup '$EXPORT_DB'"

RESULT=$(sqlite3 "$EXPORT_DB" "PRAGMA integrity_check;")
JOB_COUNT=$(sqlite3 "$EXPORT_DB" "SELECT COUNT(*) FROM jobs;")
EVENT_COUNT=$(sqlite3 "$EXPORT_DB" "SELECT COUNT(*) FROM job_events;")

if [ "$RESULT" != "ok" ]; then
    echo "Database export failed integrity check: $RESULT"
    exit 1
fi

echo "Database export completed."
echo "File: $EXPORT_DB"
echo "Jobs: $JOB_COUNT"
echo "Job events: $EVENT_COUNT"