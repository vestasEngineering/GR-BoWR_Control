#!/usr/bin/env python3

from pathlib import Path
import numpy as np

ROOT = Path(__file__).resolve().parents[1]

image = np.load(
    ROOT / "captures" / "capture.npy"
)

profile = image.mean(axis=0)

print()

print("Profile Statistics")
print("------------------")

print("Min :", profile.min())
print("Max :", profile.max())
print("Mean:", profile.mean())
print("Std :", profile.std())

print()

print("First 50 Pixels")
print(profile[:50])