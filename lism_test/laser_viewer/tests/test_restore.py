import unittest
from pathlib import Path
from types import SimpleNamespace

class RetryDevice:
    def __init__(self):
        self.values={'getsthigh':9900,'getstlow':100,'getedgedelay':88,'getadcgain':4,'getadcbias':214};self.failures={'getsthigh':1}
    def __getattr__(self,name):
        if name.startswith('set'):
            key='get'+name[3:];return lambda address,value:self.values.__setitem__(key,value) or 0
        if name.startswith('get'):
            def getter(address):
                remaining=self.failures.get(name,0)
                if remaining:self.failures[name]=remaining-1;return -10052,0
                return 0,self.values[name]
            return getter
        if name=='updateparam':return lambda address:0
        raise AttributeError(name)

class TestRestore(unittest.TestCase):
 def test_restore_retries(self):
  from laser_viewer.camera import LismCamera
  config=SimpleNamespace(configuration_readback_retries=3,configuration_readback_retry_ms=0,configuration_settle_ms=0)
  camera=LismCamera(config,Path('.'));camera.sdk=SimpleNamespace(RET_OK=0,LISM_ADDR=0xFE);camera.dev=RetryDevice()
  restored=camera.restore_configuration({'st_high':1000,'st_low':100,'edge_delay':88,'adc_gain':0,'adc_bias':768})
  self.assertEqual(restored['st_high'],1000);self.assertNotIn('pixel_count',restored)
if __name__=='__main__':unittest.main()
