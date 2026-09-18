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

BIAS_VALUES = [
    0,
    128,
    214,
    512,
    768,
    1023,
]

NUM_LINES = 64

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

for bias in BIAS_VALUES:

    print(f"Testing Bias {bias}")

    dev.setadcbias(
        LISM_ADDR,
        bias
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
        figsize=(12,6)
    )

    plt.imshow(
        image,
        cmap="gray",
        aspect="auto"
    )

    plt.title(
        f"Bias={bias}"
    )

    outfile = (
        ROOT
        / "captures"
        / f"bias_{bias}.png"
    )

    plt.savefig(
        outfile,
        dpi=150
    )

    plt.close()

dev.close()

cam.shutdown()