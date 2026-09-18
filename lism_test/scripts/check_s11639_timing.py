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

items = [
    "getedgedelay",
    "getpixelcount",
    "getsthigh",
    "getstlow",
]

for item in items:

    rc, value = getattr(
        dev,
        item
    )(LISM_ADDR)

    print(
        f"{item:20s} = {value}"
    )

dev.close()

cam.shutdown()