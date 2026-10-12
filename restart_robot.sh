#!/usr/bin/env bash
# Replays a reboot of the robot: stop the app, reset the Portenta, start the app.
# With --firmware DIR the portenta_h7_m7 target of DIR is built and flashed first.
# Without it the Portenta runs the image already in its flash, as a real reboot does.
set -euo pipefail

PIO="${PIO:-$HOME/.platformio/penv/bin/pio}"
FIRMWARE_DIR=""

usage() {
    echo "Usage: $0 [--firmware DIR]" >&2
    exit 2
}

while [[ $# -gt 0 ]]; do
    case "$1" in
        --firmware)
            [[ $# -ge 2 ]] || usage
            FIRMWARE_DIR="$2"
            shift 2
            ;;
        *) usage ;;
    esac
done

if [[ -n "$FIRMWARE_DIR" ]]; then
    [[ -f "$FIRMWARE_DIR/platformio.ini" ]] || { echo "No platformio.ini in $FIRMWARE_DIR" >&2; exit 1; }
    FIRMWARE_DIR="$(cd "$FIRMWARE_DIR" && pwd)"
fi

sudo -v

echo "Stopping grbowr-main..."
sudo systemctl stop grbowr-main.service

if [[ -n "$FIRMWARE_DIR" ]]; then
    echo "Building and flashing $FIRMWARE_DIR (portenta_h7_m7)..."
    (cd "$FIRMWARE_DIR" && "$PIO" run -e portenta_h7_m7 -t upload)
fi

echo "Resetting Portenta..."
sudo systemctl restart grbowr-portenta-reset.service

echo "Starting grbowr-main..."
sudo systemctl start grbowr-main.service
systemctl is-active grbowr-main.service
