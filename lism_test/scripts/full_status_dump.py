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

print("Devices:", count)

rc, dev = cam.opendevicebyindex_ex(
    0,
    PIPE_SIZE,
    0
)

if rc != RET_OK:
    raise RuntimeError(rc)

checks = [
    "getinitstatus",
    "getcomresult",
    "getctgstate",
    "getstate",
    "getpixelcount",
    "getlinesperframe",
    "getmodeconfig",
    "getifconfig",
    "getadcgain",
    "getadcbias",
    "getadcoffset",
]

for name in checks:
    rc, value = getattr(dev, name)(LISM_ADDR)
    print(
        f"{name:20s} rc={rc:3d} value={value}"
    )

dev.close()
cam.shutdown()
