#!/usr/bin/env python3

from pathlib import Path
import sys

import numpy as np
import matplotlib.pyplot as plt

ROOT = Path(__file__).resolve().parents[1]

sys.path.insert(
    0,
    str(ROOT / "sdk")
)

from usblismpi26 import *

PIPE_SIZE = 4 * 1024 * 1024

GAIN_VALUES = [
    0,
    8,
    16,
    24,
    32,
    48,
    63,
]

NUM_LINES = 64

BIAS = 768

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

rc, packet_length = dev.getpacketlength()

dev.setadcbias(
    LISM_ADDR,
    BIAS
)

for gain in GAIN_VALUES:

    print(f"Gain={gain}")

    dev.setadcgain(
        LISM_ADDR,
        gain
    )

    image = []

    dev.setstate(
        LISM_ADDR,
        1
    )

    for _ in range(NUM_LINES):

        dev.waitforpipecount(
            packet_length,
            3000
        )

        rc, data = dev.getpipe(
            packet_length
        )

        line = np.frombuffer(
            data,
            dtype=">u2"
        )

        image.append(line)

    dev.setstate(
        LISM_ADDR,
        0
    )

    image = np.array(image)

    print(
        f"min={image.min()} "
        f"max={image.max()} "
        f"mean={image.mean():.1f}"
    )

    plt.figure(
        figsize=(12, 6)
    )

    plt.imshow(
        image,
        cmap="gray",
        aspect="auto"
    )

    plt.title(
        f"Gain={gain}"
    )

    outfile = (
        ROOT
        / "captures"
        / f"gain_{gain}.png"
    )

    plt.savefig(
        outfile,
        dpi=150
    )

    plt.close()

dev.close()

cam.shutdown()