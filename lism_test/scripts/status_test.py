# scripts/status_test.py
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]

PIPE_SIZE = 4 * 1024 * 1024

sys.path.insert(0, str(ROOT / "sdk"))

from usblismpi26 import *

cam = USBLISMPI26(
    str(ROOT / "sdk" / "libusblismpi2664.so")
)

cam.initialize(4 * 1024 * 1024, 4096)

count = cam.enumdevices()

print(f"Devices: {count}")

if count <= 0:
    raise RuntimeError("No device detected")

PIPE_SIZE = 4 * 1024 * 1024

result, dev = cam.opendevicebyindex_ex(
    0,
    PIPE_SIZE,
    0
)

print("Open Result:", result)

if result != RET_OK:
    raise RuntimeError(
        cam.geterrorstring(result)
    )

print("Vendor :", cam.getvendorname(0))
print("Product:", cam.getproductname(0))
print("Serial :", cam.getserialnumber(0))

result, value = dev.getinitstatus(
    LISM_ADDR
)

print(
    f"INIT_STATUS = 0x{value:02X}"
)

dev.close()
cam.shutdown()