#!/usr/bin/env python3

from pathlib import Path
import sys

import numpy as np
import matplotlib.pyplot as plt

ROOT = Path(__file__).resolve().parents[1]

image = np.load(
    ROOT / "captures" / "capture.npy"
)

profile = image.mean(axis=0)

plt.figure(figsize=(15, 4))

plt.plot(profile)

plt.title(
    "Average Line Profile"
)

plt.xlabel("Pixel")

plt.ylabel("Intensity")

plt.grid(True)

outfile = ROOT / "captures" / "average_profile.png"

plt.savefig(
    outfile,
    dpi=150
)

print(outfile)