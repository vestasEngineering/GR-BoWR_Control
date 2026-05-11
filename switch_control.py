#!/usr/bin/env python3
import os
import signal
import subprocess
import time
import gpiod
import sys
import signal

# =========================
# Configuration
# =========================
GPIO_CHIP = "gpiochip4"   # confirmed from your system
SWITCH_LINE = 17          # BCM GPIO17
POLL_S = 0.05
DEBOUNCE_S = 0.05

APP_DIR = "/home/gr-towr/Documents/GR-LRR_Control"
APP_PYTHON = f"{APP_DIR}/.venv/bin/python3"
MAIN_SCRIPT = f"{APP_DIR}/main.py"
MAIN_SERVICE = "grlrr-main.service"

# =========================
# State
# =========================
app_process = None
last_raw = None
stable_state = None
last_change_t = 0.0



def is_app_running():
    result = subprocess.run(
        ["/bin/systemctl", "is-active", "--quiet", MAIN_SERVICE],
        check=False
    )
    return result.returncode == 0

def start_app():
    if is_app_running():
        print("App already running; ignoring start.")
        return

    result = subprocess.run(
        ["/bin/systemctl", "--no-block", "start", MAIN_SERVICE],
        check=False
    )
    if result.returncode == 0:
        print("App started.")
    else:
        print(f"Failed to start app (rc={result.returncode}).")

def stop_app():
    if not is_app_running():
        print("App not running; ignoring stop.")
        return

    result = subprocess.run(
        ["/bin/systemctl", "--no-block", "stop", MAIN_SERVICE],
        check=False
    )
    if result.returncode == 0:
        print("App stopped.")
    else:
        print(f"Failed to stop app (rc={result.returncode}).")


def apply_switch_state(closed_to_gnd: bool):
    """
    With bias pull-up enabled:
      line value 1 -> switch open  -> OFF
      line value 0 -> switch closed -> ON

    So closed_to_gnd == True means "switch ON" for your latch.
    """
    if closed_to_gnd:
        print("Switch is ON/closed -> start app")
        start_app()
    else:
        print("Switch is OFF/open -> stop app")
        stop_app()

def handle_exit(signum, frame):
    print(f"Received signal {signum}, exiting monitor...")
    sys.exit(0)

def main():
    global last_raw, stable_state, last_change_t

    chip = gpiod.Chip(GPIO_CHIP)
    line = chip.get_line(SWITCH_LINE)

    line.request(
        consumer="grlrr-switch-monitor",
        type=gpiod.LINE_REQ_DIR_IN,
        flags=gpiod.LINE_REQ_FLAG_BIAS_PULL_UP
    )

    print(f"Monitoring {GPIO_CHIP} line {SWITCH_LINE} as latching switch")

    try:
        # Initial read on boot
        raw = line.get_value()   # 1=open/OFF, 0=closed/ON
        last_raw = raw
        stable_state = raw
        last_change_t = time.monotonic()

        apply_switch_state(closed_to_gnd=(stable_state == 0))

        # Continuous monitoring with software debounce
        while True:
            raw = line.get_value()
            now = time.monotonic()

            if raw != last_raw:
                last_raw = raw
                last_change_t = now

            if raw != stable_state and (now - last_change_t) >= DEBOUNCE_S:
                stable_state = raw
                apply_switch_state(closed_to_gnd=(stable_state == 0))

            time.sleep(POLL_S)

    finally:
        line.release()


if __name__ == "__main__":
    signal.signal(signal.SIGTERM, handle_exit)
    signal.signal(signal.SIGINT, handle_exit)
    main()