#!/usr/bin/env python3

from pathlib import Path
from datetime import datetime
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

LINES_TO_CAPTURE = 128


def save_plot(image, filename, title):

    plt.figure(figsize=(12, 6))

    plt.imshow(
        image,
        cmap="gray",
        aspect="auto"
    )

    plt.title(title)

    plt.tight_layout()

    plt.savefig(
        filename,
        dpi=150
    )

    plt.close()


def main():

    timestamp = datetime.now().strftime(
        "%Y%m%d_%H%M%S"
    )

    report_dir = (
        ROOT
        / "reports"
        / timestamp
    )

    report_dir.mkdir(
        parents=True,
        exist_ok=True
    )

    cam = USBLISMPI26(
        str(
            ROOT
            / "sdk"
            / "libusblismpi2664.so"
        )
    )

    cam.initialize(
        PIPE_SIZE,
        4096
    )

    html = []

    html.append(
        "<h1>LISM Validation Report</h1>"
    )

    html.append(
        f"<p>Generated: {datetime.now()}</p>"
    )

    device_count = cam.enumdevices()

    html.append(
        f"<h2>Device Count</h2><p>{device_count}</p>"
    )

    if device_count <= 0:

        html.append(
            "<h2>FAIL</h2><p>No devices detected.</p>"
        )

        return

    result, dev = cam.opendevicebyindex_ex(
        0,
        PIPE_SIZE,
        0
    )

    html.append(
        f"<h2>Open Result</h2><p>{result}</p>"
    )

    try:

        vendor = cam.getvendorname(0)
        product = cam.getproductname(0)
        serial = cam.getserialnumber(0)
        fw = cam.getfwversion(0)

        html.append(
            "<h2>USB Device</h2>"
        )

        html.append(
            f"""
            <ul>
            <li>Vendor: {vendor}</li>
            <li>Product: {product}</li>
            <li>Serial: {serial}</li>
            <li>Firmware: {fw}</li>
            </ul>
            """
        )

        diagnostics = {
            "MCU Version":
                dev.getmcuversion(
                    LISM_ADDR
                )[1],

            "CTG Version":
                dev.getctgversion(
                    LISM_ADDR
                )[1],

            "HW1 ID":
                dev.gethw1id(
                    LISM_ADDR
                )[1],

            "HW1 Version":
                dev.gethw1version(
                    LISM_ADDR
                )[1],

            "HW2 Version":
                dev.gethw2version(
                    LISM_ADDR
                )[1],

            "INIT_STATUS":
                dev.getinitstatus(
                    LISM_ADDR
                )[1],

            "COM_RESULT":
                dev.getcomresult(
                    LISM_ADDR
                )[1],
        }

        html.append(
            "<h2>Diagnostics</h2><ul>"
        )

        for k, v in diagnostics.items():

            html.append(
                f"<li>{k}: {v}</li>"
            )

        html.append(
            "</ul>"
        )

        config = {
            "Pixel Count":
                dev.getpixelcount(
                    LISM_ADDR
                )[1],

            "Lines Per Frame":
                dev.getlinesperframe(
                    LISM_ADDR
                )[1],

            "ST High":
                dev.getsthigh(
                    LISM_ADDR
                )[1],

            "ST Low":
                dev.getstlow(
                    LISM_ADDR
                )[1],

            "Edge Delay":
                dev.getedgedelay(
                    LISM_ADDR
                )[1],

            "ADC Gain":
                dev.getadcgain(
                    LISM_ADDR
                )[1],

            "ADC Bias":
                dev.getadcbias(
                    LISM_ADDR
                )[1],

            "ADC Offset":
                dev.getadcoffset(
                    LISM_ADDR
                )[1],
        }

        html.append(
            "<h2>Configuration</h2><ul>"
        )

        for k, v in config.items():

            html.append(
                f"<li>{k}: {v}</li>"
            )

        html.append(
            "</ul>"
        )

        packet_length = (
            dev.getpacketlength()[1]
        )

        image = []

        dev.setstate(
            LISM_ADDR,
            1
        )

        for _ in range(
            LINES_TO_CAPTURE
        ):

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

        np.save(
            report_dir
            / "capture.npy",
            image
        )

        stats = {
            "Min":
                int(image.min()),
            "Max":
                int(image.max()),
            "Mean":
                float(image.mean()),
            "Std":
                float(image.std()),
        }

        html.append(
            "<h2>Capture Statistics</h2><ul>"
        )

        for k, v in stats.items():

            html.append(
                f"<li>{k}: {v}</li>"
            )

        html.append(
            "</ul>"
        )

        capture_png = (
            report_dir
            / "capture.png"
        )

        save_plot(
            image,
            capture_png,
            "Capture"
        )

        profile = image.mean(
            axis=0
        )

        plt.figure(
            figsize=(12, 4)
        )

        plt.plot(profile)

        plt.title(
            "Average Profile"
        )

        plt.grid(True)

        profile_png = (
            report_dir
            / "profile.png"
        )

        plt.savefig(
            profile_png,
            dpi=150
        )

        plt.close()

        html.append(
            "<h2>Images</h2>"
        )

        html.append(
            'capture.png'
        )

        html.append(
            'profile.png'
        )

    finally:

        try:
            dev.close()
        except:
            pass

        cam.shutdown()

    report_file = (
        report_dir
        / "report.html"
    )

    report_file.write_text(
        "\n".join(html)
    )

    print()
    print("Report Generated:")
    print(report_file)
    print()


if __name__ == "__main__":
    main()