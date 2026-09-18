#!/usr/bin/env python3

from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]

sys.path.insert(
    0,
    str(ROOT / "sdk")
)

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

for fn in [
    "getmcuversion",
    "getctgversion",
    "gethw1id",
    "gethw1version",
    "gethw2version",
]:
    rc, value = getattr(dev, fn)(
        LISM_ADDR
    )

    print(
        f"{fn}: {value}"
    )

dev.close()

cam.shutdown()
