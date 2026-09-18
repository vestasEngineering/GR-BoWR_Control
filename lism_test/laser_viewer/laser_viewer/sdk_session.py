from __future__ import annotations
import sys, threading
from pathlib import Path
class SdkSessionError(RuntimeError): pass
class LismSdkSession:
    def __init__(self,config,base):
        self.config=config; self.base=Path(base); self.sdk=None; self.api=None; self.handles={}; self.lock=threading.RLock(); self.closed=False
    def open(self):
        with self.lock:
            if self.api:return
            d=self.config.resolve(self.base,self.config.sdk_directory)
            if str(d) not in sys.path:sys.path.insert(0,str(d))
            import usblismpi26 as sdk
            self.sdk=sdk; self.api=sdk.USBLISMPI26(str(self.config.resolve(self.base,self.config.library_path)))
            if self.api.libversion()<0x0200:raise SdkSessionError("Vendor library Version 2.0 or later is required")
            n=self.api.enumdevices()
            if n<0:raise SdkSessionError(f"Enumeration failed: {n}")
            self.serials=tuple(self.api.getserialnumber(i) or "" for i in range(n))
    def open_device(self,serial,pipe,packet):
        with self.lock:
            self.open()
            if serial in self.handles:raise SdkSessionError(f"{serial} is already open")
            if serial not in self.serials:raise SdkSessionError(f"{serial} not found; enumerated {self.serials}")
            rc,dev=self.api.opendevicebyserial_ex(serial,pipe,packet)
            if rc!=self.sdk.RET_OK or dev is None:raise SdkSessionError(f"Open {serial} failed: {rc} {self.api.geterrorstring(rc)}")
            self.handles[serial]=dev; return dev
    def close_device(self,serial):
        with self.lock:dev=self.handles.pop(serial,None)
        if dev is not None:
            rc=dev.close()
            if rc!=self.sdk.RET_OK:raise SdkSessionError(f"Close {serial} failed: {rc}")
    def close(self):
        with self.lock:
            if self.closed:return
            items=list(self.handles.items());self.handles.clear();api=self.api;self.closed=True
        errors=[]
        for s,d in items:
            try:d.close()
            except Exception as e:errors.append(f"{s}: {e}")
        if api:
            try:api.shutdown()
            except Exception as e:errors.append(f"shutdown: {e}")
        if errors:raise SdkSessionError('; '.join(errors))
