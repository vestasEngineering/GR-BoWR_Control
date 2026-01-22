
#!/usr/bin/env python3
from gpiozero import Button
from signal import pause
import subprocess
import time
import os
import signal

# ===== Config =====
SWITCH_PIN = 17
DEBOUNCE_S = 0.05
LONG_PRESS_S = 2.0  # hold 2s to stop with SIGINT

START_SCRIPT = "/home/vestas/Documents/Projects/GRLRR_CONTROL/run.sh"
STOP_SCRIPT  = "/home/vestas/Documents/Projects/GRLRR_CONTROL/stop.sh"

# ===== State =====
app_process = None
server_running = False
press_t0 = [0.0]

print("Monitoring button on GPIO17 (short=start, long=stop)")

def start_server():
    global app_process, server_running
    if server_running:
        print("Already running; ignoring start.")
        return
    try:
        # Start run.sh asynchronously.
        # Because run.sh uses `exec python ...`, app_process will be the main.py proc.
        app_process = subprocess.Popen(["bash", START_SCRIPT])
        server_running = True
        print("Start: server starting...")
    except Exception as e:
        print(f"Failed to start server: {e}")

def stop_server_via_sigint():
    global app_process, server_running
    if not server_running:
        print("Not running; ignoring stop.")
        return
    print("Long press: stopping server with SIGINT...")
    try:
        # Primary path: use stop.sh (pkill -INT by path)
        subprocess.run(["bash", STOP_SCRIPT], check=False)

        # If we still have a live handle, ensure it exits
        if app_process and app_process.poll() is None:
            try:
                # First, try a polite SIGINT to the exact process we launched
                app_process.send_signal(signal.SIGINT)
            except Exception:
                pass
            # Wait briefly; if still running, escalate
            try:
                app_process.wait(timeout=5)
            except Exception:
                app_process.terminate()
                try:
                    app_process.wait(timeout=3)
                except Exception:
                    app_process.kill()

        server_running = False
        app_process = None
        print("Server stopped.")
    except Exception as e:
        print(f"Failed to stop server: {e}")

def on_pressed():
    press_t0[0] = time.monotonic()

def on_released():
    dt = time.monotonic() - press_t0[0]
    if dt >= LONG_PRESS_S:
        # Long press → stop
        stop_server_via_sigint()
    else:
        # Short press → start (only if not already running)
        start_server()

button = Button(SWITCH_PIN, pull_up=True, bounce_time=DEBOUNCE_S)
button.when_pressed  = on_pressed
button.when_released = on_released

pause()
