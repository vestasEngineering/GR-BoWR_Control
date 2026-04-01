import serial
import json
import time

ser = serial.Serial("/dev/ttyAMA0", 115200, timeout=1)
print("Opened:", ser.name)

def send_and_drain(msg, wait_s=1.5):
    raw = (json.dumps(msg, separators=(',', ':')) + '\n').encode('utf-8')
    print("\nTX:", msg)
    print("TX RAW:", raw)

    ser.write(raw)
    ser.flush()

    end = time.time() + wait_s
    got_any = False
    while time.time() < end:
        line = ser.readline()
        if line:
            got_any = True
            print("RX RAW:", repr(line))
            print("RX TXT:", line.decode("utf-8", errors="replace").rstrip())
    if not got_any:
        print("RX: <nothing>")

sequence = [
    {"action": "ping"},
    {"action": "reset_encoder"},
    {"action": "set_triggers", "clear": True},

    {"action": "set_triggers",
     "trigger": {"threshold": 0, "activate": 0, "deactivate": 3, "delay": 0}},

    {"action": "set_triggers",
     "trigger": {"threshold": 2000, "activate": 1, "deactivate": 0, "delay": 9}},

    {"action": "set_triggers",
     "trigger": {"threshold": 4000, "activate": 2, "deactivate": 1, "delay": 9}},

    {"action": "set_triggers",
     "trigger": {"threshold": 6000, "activate": 3, "deactivate": 2, "delay": 9}},

    {"hb": 1},
]

for msg in sequence:
    send_and_drain(msg)
    time.sleep(0.2)