#!/usr/bin/env python3

from pathlib import Path
import sys

import numpy as np
import matplotlib.pyplot as plt

ROOT = Path(__file__).resolve().parents[1]

sys.path.insert(0, str(ROOT / "sdk"))

from usblismpi26 import (
    USBLISMPI26,
    RET_OK,
    LISM_ADDR,
)

PIPE_SIZE = 4 * 1024 * 1024


def save_plot(y, title, filename):
    plt.figure(figsize=(12, 4))
    plt.plot(y)
    plt.title(title)
    plt.xlabel("Pixel")
    plt.ylabel("Value")
    plt.grid(True)
    plt.tight_layout()
    plt.savefig(filename, dpi=150)
    print(f"Saved: {filename}")


def main():

    sdk_library = ROOT / "sdk" / "libusblismpi2664.so"

    print(f"SDK Library: {sdk_library}")

    cam = USBLISMPI26(str(sdk_library))

    print("Initializing SDK...")

    cam.initialize(
        PIPE_SIZE,
        4096
    )

    device_count = cam.enumdevices()

    print(f"Devices Found: {device_count}")

    if device_count <= 0:
        raise RuntimeError("No LISM device found")

    result, dev = cam.opendevicebyindex_ex(
        0,
        PIPE_SIZE,
        0
    )

    print(f"Open Result: {result}")

    if result != RET_OK:
        raise RuntimeError(f"Open failed: {result}")

    try:

        result, packet_length = dev.getpacketlength()

        print("Packet Length:", packet_length)

        result, pixel_count = dev.getpixelcount(
            LISM_ADDR
        )

        print("Pixel Count:", pixel_count)

        result, cfg1 = dev.getcfg1()

        print(f"CFG1 = 0x{cfg1:02X}")

        result, com_result = dev.getcomresult(
            LISM_ADDR
        )

        print(f"COM_RESULT = 0x{com_result:02X}")

        print()

        print("Starting acquisition...")

        result = dev.setstate(
            LISM_ADDR,
            1
        )

        print("setstate:", result)

        if result != RET_OK:
            raise RuntimeError(
                f"Failed to start acquisition: {result}"
            )

        result, available = dev.waitforpipecount(
            packet_length,
            3000
        )

        print("waitforpipecount:", result)
        print("available bytes :", available)

        if result != RET_OK:
            raise RuntimeError(
                f"waitforpipecount failed: {result}"
            )

        result, data = dev.getpipe(
            packet_length
        )

        print("getpipe result :", result)
        print("bytes received :", len(data))

        if result != RET_OK:
            raise RuntimeError(
                f"getpipe failed: {result}"
            )

        print()
        print("FIRST 64 RAW BYTES")
        print("------------------")
        print(data[:64].hex())

        packet_file = ROOT / "captures" / "first_packet.bin"

        packet_file.parent.mkdir(
            exist_ok=True
        )

        with open(packet_file, "wb") as f:
            f.write(data)

        print()
        print(f"Saved: {packet_file}")

        pixels_be = np.frombuffer(
            data,
            dtype=">u2"
        )

        pixels_le = np.frombuffer(
            data,
            dtype="<u2"
        )

        print()
        print("BIG ENDIAN")
        print("----------")

        print("Count:", len(pixels_be))
        print("Min  :", int(pixels_be.min()))
        print("Max  :", int(pixels_be.max()))
        print("Mean :", float(pixels_be.mean()))

        print()
        print("First 20 Pixels")
        print(pixels_be[:20])

        print()
        print("LITTLE ENDIAN")
        print("-------------")

        print("Count:", len(pixels_le))
        print("Min  :", int(pixels_le.min()))
        print("Max  :", int(pixels_le.max()))
        print("Mean :", float(pixels_le.mean()))

        print()
        print("First 20 Pixels")
        print(pixels_le[:20])

        np.savetxt(
            ROOT / "captures" / "first_line_be.csv",
            pixels_be,
            fmt="%d",
            delimiter=","
        )

        np.savetxt(
            ROOT / "captures" / "first_line_le.csv",
            pixels_le,
            fmt="%d",
            delimiter=","
        )

        save_plot(
            pixels_be,
            "Big Endian Decode",
            ROOT / "captures" / "big_endian.png"
        )

        save_plot(
            pixels_le,
            "Little Endian Decode",
            ROOT / "captures" / "little_endian.png"
        )

        plt.figure(figsize=(10, 4))

        plt.hist(
            pixels_be,
            bins=256
        )

        plt.title(
            "Histogram (Big Endian)"
        )

        plt.tight_layout()

        hist_file = ROOT / "captures" / "histogram.png"

        plt.savefig(
            hist_file,
            dpi=150
        )

        print(f"Saved: {hist_file}")

    finally:

        print()

        print("Stopping acquisition...")

        try:
            dev.setstate(
                LISM_ADDR,
                0
            )
        except Exception:
            pass

        print("Closing device...")

        try:
            dev.close()
        except Exception:
            pass

        cam.shutdown()

        print("Done")


if __name__ == "__main__":
    main()