#!/usr/bin/env bash
set -euo pipefail

# Activate venv
source .venv/bin/activate

# Ensure hotspot is up; requires NetworkManager and suitable permissions
#connection_name=$(nmcli -g name connection show | head -1 || true)
#if [[ "$connection_name" == "Hotspot" ]]; then
#    echo "Hotspot already enabled"
#else
#    echo "Enabling hotspot..."
#    nmcli dev wifi hotspot ifname wlan0 ssid grlrr2024 password grlrr2024
#fi

echo "Starting main app..."
# Use absolute path, run in foreground so the process is parented to this script
exec python3 main.py
