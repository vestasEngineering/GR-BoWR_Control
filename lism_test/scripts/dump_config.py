# scripts/dump_config.py

from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]

sys.path.insert(0, str(ROOT / "sdk"))

from usblismpi26 import *

PIPE_SIZE = 4 * 1024 * 1024

cam = USBLISMPI26(
    str(ROOT / "sdk" / "libusblismpi2664.so")
)

cam.initialize(PIPE_SIZE, 4096)

count = cam.enumdevices()

rc, dev = cam.opendevicebyindex_ex(
    0,
    PIPE_SIZE,
    0
)

if rc != RET_OK:
    raise RuntimeError(f"open failed {rc}")

def show(name, result):
    print(f"{name:20} = {result}")

rc, pixel_count = dev.getpixelcount(LISM_ADDR)
show("Pixel Count", pixel_count)

rc, lines_per_frame = dev.getlinesperframe(LISM_ADDR)
show("Lines Per Frame", lines_per_frame)

rc, cfg1 = dev.getcfg1()
show("CFG1", hex(cfg1))

rc, mode = dev.getmodeconfig(LISM_ADDR)
show("Mode Config", hex(mode))

rc, ifcfg = dev.getifconfig(LISM_ADDR)
show("IF Config", hex(ifcfg))

rc, packet_length = dev.getpacketlength()
show("Packet Length", packet_length)

rc, gain = dev.getadcgain(LISM_ADDR)
show("ADC Gain", gain)

rc, bias = dev.getadcbias(LISM_ADDR)
show("ADC Bias", bias)

rc, offset = dev.getadcoffset(LISM_ADDR)
show("ADC Offset", offset)

rc, st_high = dev.getsthigh(LISM_ADDR)
show("ST High", st_high)

rc, st_low = dev.getstlow(LISM_ADDR)
show("ST Low", st_low)

dev.close()
cam.shutdown()
