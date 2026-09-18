#!/usr/bin/env python3

from pathlib import Path
import sys
import numpy as np

ROOT = Path(__file__).resolve().parents[1]

sys.path.insert(0, str(ROOT / "sdk"))

from usblismpi26 import *

PIPE_SIZE = 4 * 1024 * 1024

cam = USBLISMPI26(
    str(ROOT / "sdk" / "libusblismpi2664.so")
)

cam.initialize(
    PIPE_SIZE,
    4096
)

count = cam.enumdevices()

rc, dev = cam.opendevicebyindex_ex(
    0,
    PIPE_SIZE,
    0
)

if rc != RET_OK:
    raise RuntimeError(rc)

rc, packet_length = dev.getpacketlength()

print("Packet Length:", packet_length)

rc = dev.setstate(
    LISM_ADDR,
    1
)

for line_number in range(10):

    rc, available = dev.waitforpipecount(
        packet_length,
        3000
    )

    rc, data = dev.getpipe(
        packet_length
    )

    pixels = np.frombuffer(
        data,
        dtype=">u2"
    )

    print()
    print(f"LINE {line_number}")

    print(
        pixels[:20]
    )

dev.setstate(
    LISM_ADDR,
    0
)

dev.close()

cam.shutdown()