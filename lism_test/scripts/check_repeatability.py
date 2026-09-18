#!/usr/bin/env python3

from pathlib import Path
import sys

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

rc, packet_len = dev.getpacketlength()

dev.setstate(
    LISM_ADDR,
    1
)

packets = []

for i in range(5):

    dev.waitforpipecount(
        packet_len,
        3000
    )

    rc, data = dev.getpipe(
        packet_len
    )

    packets.append(data)

for i in range(1, 5):

    identical = packets[0] == packets[i]

    print(
        f"Packet 0 vs Packet {i}: {identical}"
    )

dev.setstate(
    LISM_ADDR,
    0
)

dev.close()

cam.shutdown()