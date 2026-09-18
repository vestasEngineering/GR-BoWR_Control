#!/usr/bin/env python3

from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]

sys.path.insert(0, str(ROOT / "sdk"))

from usblismpi26 import *

PIPE_SIZE = 4 * 1024 * 1024


def decode_cfg1(cfg1: int):

    counter_enable = cfg1 & 0x01
    counter_format = (cfg1 >> 1) & 0x01
    image_count = ((cfg1 >> 2) & 0x1F) + 1

    print(f"CFG1 Raw            : 0x{cfg1:02X}")
    print(f"Counter Enable     : {counter_enable}")
    print(f"Counter Format     : {counter_format}")
    print(f"Image Count        : {image_count}")
    print(f"Reserved Bit 7     : {(cfg1 >> 7) & 0x01}")


cam = USBLISMPI26(
    str(ROOT / "sdk" / "libusblismpi2664.so")
)

cam.initialize(
    PIPE_SIZE,
    4096
)

count = cam.enumdevices()

print(f"Devices Found: {count}")

if count <= 0:
    raise RuntimeError(
        "No device detected"
    )

result, dev = cam.opendevicebyindex_ex(
    0,
    PIPE_SIZE,
    0
)

if result != RET_OK:
    raise RuntimeError(
        f"Open failed: {result}"
    )

try:

    print()
    print("BEFORE")
    print("======")

    rc, cfg1 = dev.getcfg1()

    decode_cfg1(cfg1)

    rc, packet = dev.getpacketlength()

    print("Packet Length:", packet)

    print()
    print("Stopping acquisition")

    dev.setstate(
        LISM_ADDR,
        0
    )

    print("Applying known configuration")

    #
    # 1 image per USB transfer
    # no metadata
    #

    cfg1 = 0x00

    rc = dev.setcfg1(
        cfg1
    )

    print("setcfg1:", rc)

    #
    # synchronize CTG state
    #

    rc = dev.resume(
        LISM_ADDR
    )

    print("resume:", rc)

    rc = dev.updateparam(
        LISM_ADDR
    )

    print("updateparam:", rc)

    #
    # verify
    #

    print()
    print("AFTER")
    print("=====")

    rc, cfg1 = dev.getcfg1()

    decode_cfg1(cfg1)

    rc, packet = dev.getpacketlength()

    print("Packet Length:", packet)

    rc, com = dev.getcomresult(
        LISM_ADDR
    )

    print(f"COM_RESULT = 0x{com:02X}")

finally:

    dev.close()

    cam.shutdown()

    print()
    print("Done")