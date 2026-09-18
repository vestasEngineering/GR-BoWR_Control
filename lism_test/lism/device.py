from pathlib import Path
import sys

class LismDevice:
    def __init__(self,sdk_dir):
        sdk_dir=Path(sdk_dir)
        sys.path.insert(0,str(sdk_dir))
        from usblismpi26 import USBLISMPI26,_4MB,RET_OK
        self._4MB=_4MB; self.RET_OK=RET_OK
        self.api=USBLISMPI26(str(sdk_dir/'libusblismpi2664.so'))
        self.dev=None
    def connect(self):
        self.api.initialize(self._4MB,4096)
        count=self.api.enumdevices()
        if count<=0: raise RuntimeError('No devices found')
        rc,dev=self.api.opendevicebyindex_ex(0,self._4MB,0)
        if rc!=self.RET_OK: raise RuntimeError(f'open failed {rc}')
        self.dev=dev
        return count
    def close(self):
        if self.dev: self.dev.close()
        self.api.shutdown()
