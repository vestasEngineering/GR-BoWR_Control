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

EXPOSURES = [
    1000,
    5000,
    10000,
    30000,
]

NUM_LINES = 128


def capture(dev, packet_length):

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

    return np.array(image)


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

for exposure in EXPOSURES:

    print(
        f"Testing ST_HIGH={exposure}"
    )

    dev.setsthigh(
        LISM_ADDR,
        exposure
    )

    image = capture(
        dev,
        packet_length
    )

    outfile = (
        ROOT
        / "captures"
        / f"exposure_{exposure}.png"
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
        f"ST_HIGH={exposure}"
    )

    plt.tight_layout()

    plt.savefig(
        outfile,
        dpi=150
    )

    print(outfile)

dev.close()

cam.shutdown()