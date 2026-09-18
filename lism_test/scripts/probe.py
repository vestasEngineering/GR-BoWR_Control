from pathlib import Path
import sys
ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT))
from lism.device import LismDevice
cam=LismDevice(ROOT/'sdk')
count=cam.connect()
print('Devices',count)
cam.close()
