#!/usr/bin/env python3

from pathlib import Path
import sys

import numpy as np
import matplotlib.pyplot as plt

ROOT = Path(__file__).resolve().parents[1]

sys.path.insert(0, str(ROOT / "sdk"))

from usblismpi26 import *

PIPE_SIZE = 4 * 1024 * 1024

NUM_LINES = 256


def main():

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

    try:

        rc, packet_length = dev.getpacketlength()

        print(
            "Packet Length:",
            packet_length
        )

        image = []

        dev.setstate(
            LISM_ADDR,
            1
        )

        for i in range(NUM_LINES):

            rc, available = dev.waitforpipecount(
                packet_length,
                3000
            )

            if rc != RET_OK:
                raise RuntimeError(
                    f"wait failed line {i}"
                )

            rc, data = dev.getpipe(
                packet_length
            )

            if rc != RET_OK:
                raise RuntimeError(
                    f"getpipe failed line {i}"
                )

            line = np.frombuffer(
                data,
                dtype=">u2"
            )

            image.append(line)

            if i % 25 == 0:
                print(
                    f"Captured {i}/{NUM_LINES}"
                )

        image = np.array(image)

        print()
        print("Image Shape")
        print(image.shape)

        np.save(
            ROOT / "captures" / "capture.npy",
            image
        )

        plt.figure(figsize=(15, 8))

        plt.imshow(
            image,
            cmap="gray",
            aspect="auto"
        )

        plt.colorbar()

        plt.title(
            "256 x 2048 Capture"
        )

        plt.tight_layout()

        outfile = (
            ROOT
            / "captures"
            / "capture_256x2048.png"
        )

        plt.savefig(
            outfile,
            dpi=150
        )

        print()
        print("Saved:")
        print(outfile)

    finally:

        try:
            dev.setstate(
                LISM_ADDR,
                0
            )
        except Exception:
            pass

        dev.close()

        cam.shutdown()


if __name__ == "__main__":
    main()
