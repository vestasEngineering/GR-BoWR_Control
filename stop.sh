
#!/usr/bin/env bash
set -euo pipefail

APP="/home/vestas/Documents/Projects/GRLRR_CONTROL/main.py"

# Send SIGINT (Ctrl+C) to all matches of main.py
pkill -INT -f "$APP" || true

# Give it a moment to perform graceful shutdown
sleep 2

# If still running, try SIGTERM
pgrep -f "$APP" >/dev/null && pkill -TERM -f "$APP" || true

# Final fallback (rare): SIGKILL
sleep 1
pgrep -f "$APP" >/dev/null && pkill -KILL -f "$APP" || true
