from pathlib import Path
import sys
ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT))
from lism.device import LismDevice
from lism.diagnostics import print_identity
cam=LismDevice(ROOT/'sdk'); cam.connect()
from usblismpi26 import LISM_ADDR
print_identity(cam,LISM_ADDR)
cam.close()
