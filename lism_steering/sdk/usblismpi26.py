"""
ctypes binding for the USB LISM-PI26xx library.

Supported platforms:
- Windows 32/64 bit
- Linux 32/64 bit, including ARM32 and AArch64

The module provides:
- USBLISMPI26: legacy single-device API, compatible with earlier Python code
- USBLISMPI26Device: handle-based multi-device API for the *_ex exports

The library is selected automatically when no path is supplied:
- Windows 32 bit: usblismpi2632.dll
- Windows 64 bit: usblismpi2664.dll
- Linux 32 bit: libusblismpi2632.so
- Linux 64 bit: libusblismpi2664.so
"""

from __future__ import annotations

import ctypes as _ct
import os as _os
import sys as _sys
from pathlib import Path as _Path
from typing import Optional, Tuple, Union


# ---- Common constants ----
RET_OK = 0x00000000
LS_THREAD_PRIORITY_NORMAL = 0
LS_THREAD_PRIORITY_ABOVE_NORMAL = 1
LS_THREAD_PRIORITY_HIGH = 2

# BEGIN GENERATED LS ERROR CODES
if _os.name == "nt":
    LS_OK = 0
    LS_ERR_NO_DEVICES_ATTACHED = 0x20000001
    LS_ERR_DEVICE_INDEX_OUT_OF_RANGE = 0x20000002
    LS_ERR_SERIAL_NUM_NOT_FOUND = 0x20000003
    LS_ERR_INVALID_HANDLE = 6
    LS_ERR_DEVICE_ACTIVE = 0x20000011
    LS_ERR_CONTROL_TRANSFER = 0x20000015
    LS_ERR_TIMEOUT = 1460
    LS_ERR_DEVICE_ALREADY_OPEN = 0x20000013
    LS_ERR_NOT_IMPLEMENTED = 0x20000014
    LS_ERR_ALLOCATE_THREADPARAM_MEM = 0x20000004
    LS_ERR_PATH_INVALID_HANDLE = 0x20000006
    LS_ERR_INVALID_DATALEN = 0x21000001
    LS_ERR_I2C_NACK = 0x21000004
    LS_ERR_I2C_ERROR = 0x21000005
    LS_ERR_I2C_UNKNOWN = 0x21000006
    LS_ERR_I2C_MUX_DISABLED = 0x21000007
    LS_ERR_INVALID_PACKETLENGTH = 0x21000002
    LS_ERR_CMD_NOT_SUPPORTED = 0x21000003
    LS_ERR_PIPE_CANCELLED = 109
    LS_ERR_INVALID_TIMEOUT = 0x21000008
    LS_ERR_INVALID_THREAD_PRIORITY = 0x21000009
    LS_ERR_THREAD_PRIORITY_FAILED = 0x2100000A
else:
    LS_OK = 0
    LS_ERR_NO_DEVICES_ATTACHED = -10001
    LS_ERR_DEVICE_INDEX_OUT_OF_RANGE = -10002
    LS_ERR_SERIAL_NUM_NOT_FOUND = -10003
    LS_ERR_INVALID_HANDLE = -10004
    LS_ERR_DEVICE_ACTIVE = -10005
    LS_ERR_CONTROL_TRANSFER = -10006
    LS_ERR_TIMEOUT = -10007
    LS_ERR_DEVICE_ALREADY_OPEN = -10008
    LS_ERR_NOT_IMPLEMENTED = -10009
    LS_ERR_ALLOCATE_THREADPARAM_MEM = -10010
    LS_ERR_PATH_INVALID_HANDLE = -10012
    LS_ERR_INVALID_DATALEN = -10051
    LS_ERR_I2C_NACK = -10052
    LS_ERR_I2C_ERROR = -10053
    LS_ERR_I2C_UNKNOWN = -10054
    LS_ERR_I2C_MUX_DISABLED = -10055
    LS_ERR_INVALID_PACKETLENGTH = -10056
    LS_ERR_CMD_NOT_SUPPORTED = -10057
    LS_ERR_PIPE_CANCELLED = -10058
    LS_ERR_INVALID_TIMEOUT = -10059
    LS_ERR_INVALID_THREAD_PRIORITY = -10060
    LS_ERR_THREAD_PRIORITY_FAILED = -10061
# END GENERATED LS ERROR CODES

_512 = 512
_1kB = _512 * 2
_2kB = _1kB * 2
_4kB = _2kB * 2
_8kB = _4kB * 2
_16kB = _8kB * 2
_32kB = _16kB * 2
_64kB = _32kB * 2
_128kB = _64kB * 2
_256kB = _128kB * 2
_512kB = _256kB * 2
_1MB = _512kB * 2
_2MB = _1MB * 2
_4MB = _2MB * 2
_8MB = _4MB * 2
_16MB = _8MB * 2
_32MB = _16MB * 2
_64MB = _32MB * 2
_128MB = _64MB * 2
_256MB = _128MB * 2

_1ms = 1
_1s = 1000 * _1ms

LISM_ADDR = 0xFE

MODE_FREE_RUNNING = 0x00
MODE_EXT_RISING_EDGE_SL = 0x01
MODE_EXT_HIGH_LEVEL = 0x02
MODE_INT_SOFT_TRIGGER = 0x03
MODE_QUAD_ENC_TRIGGER = 0x04
MODE_EXT_RISING_EDGE_ML = 0x05
MODE_ENCODER_DIR_CW = 0x00
MODE_ENCODER_DIR_CCW = 0x10
MODE_ENCODER_CNT_DISABLE = 0x00
MODE_ENCODER_CNT_ENABLE = 0x20

IF_DCLK_400KHZ = 0x00
IF_DCLK_500KHZ = 0x01
IF_DCLK_800KHZ = 0x02
IF_DCLK_1MHZ = 0x03
IF_DCLK_2MHZ = 0x04
IF_DCLK_4MHZ = 0x05
IF_DCLK_10MHZ = 0x06
IF_DCLK_20MHZ = 0x07
IF_DCLK_POL_RISING_EDGE = 0x00
IF_DCLK_POL_FALLING_EDGE = 0x08
IF_LINE_VALID_HIGH = 0x00
IF_LINE_VALID_LOW = 0x10
IF_FRAME_VALID_HIGH = 0x00
IF_FRAME_VALID_LOW = 0x20
IF_TRIGGER_OUTPUT_HIGH = 0x00
IF_TRIGGER_OUTPUT_LOW = 0x40

ADC_VREF_1V6 = 0x00
ADC_VREF_2V0 = 0x01

DEFAULT_TIMEOUT_MS = 1000


# ---- ctypes aliases ----
UInt8 = _ct.c_uint8
UInt16 = _ct.c_uint16
UInt32 = _ct.c_uint32
Int8 = _ct.c_int8
Int32 = _ct.c_int32
PAnsiChar = _ct.c_char_p
PVOID = _ct.c_void_p
LSHandle = _ct.c_void_p
LSResult = UInt32 if _os.name == "nt" else Int32

P_UInt8 = _ct.POINTER(UInt8)
P_UInt16 = _ct.POINTER(UInt16)
P_UInt32 = _ct.POINTER(UInt32)
P_Int8 = _ct.POINTER(Int8)
P_Int32 = _ct.POINTER(Int32)
P_LSHandle = _ct.POINTER(LSHandle)

PathLike = Union[str, bytes, _os.PathLike]


class USBLISMPI26LibraryError(RuntimeError):
    """Raised when the shared library or a required export cannot be loaded."""


def default_library_name() -> str:
    """Return the unversioned DLL/SO name for the running Python process."""
    is_64_bit = _ct.sizeof(_ct.c_void_p) == 8
    if _os.name == "nt":
        return "usblismpi2664.dll" if is_64_bit else "usblismpi2632.dll"
    if _sys.platform.startswith("linux"):
        return "libusblismpi2664.so" if is_64_bit else "libusblismpi2632.so"
    raise OSError("Only Windows and Linux are supported")


def _resolve_library_path(path: Optional[PathLike]) -> str:
    if path is not None:
        return _os.fsdecode(_os.fspath(path))

    name = default_library_name()
    candidates = (
        _Path(__file__).resolve().parent / name,
        _Path.cwd() / name,
    )
    for candidate in candidates:
        if candidate.exists():
            return str(candidate)
    return name


def _text_encoding() -> str:
    return "mbcs" if _os.name == "nt" else "utf-8"


def _to_bytes(value: Optional[Union[str, bytes]]) -> Optional[bytes]:
    if value is None or isinstance(value, bytes):
        return value
    return value.encode(_text_encoding(), errors="replace")


def _from_c_string(value: Optional[bytes]) -> Optional[str]:
    if not value:
        return None
    return value.decode(_text_encoding(), errors="replace")


def _buffer_from_bytes(data: Union[bytes, bytearray, memoryview]):
    raw = bytes(data)
    if not raw:
        return raw, None, PVOID()
    buffer = (UInt8 * len(raw)).from_buffer_copy(raw)
    return raw, buffer, _ct.cast(buffer, PVOID)


def _validate_u16_length(length: int) -> None:
    if length < 0 or length > 0xFFFF:
        raise ValueError("length must be in the range 0..65535")


class _Library:
    """Loads the DLL/SO and binds all public non-service exports."""

    def __init__(self, path: Optional[PathLike] = None):
        self.is_windows = _os.name == "nt"
        self.path = _resolve_library_path(path)
        try:
            self.dll = _ct.WinDLL(self.path) if self.is_windows else _ct.CDLL(self.path)
        except OSError as exc:
            raise USBLISMPI26LibraryError(
                "Could not load {!r}: {}".format(self.path, exc)
            ) from exc
        self._bind_legacy()
        self._bind_ex()

    def _bind(self, name: str, restype, argtypes):
        try:
            function = getattr(self.dll, name)
        except AttributeError as exc:
            raise USBLISMPI26LibraryError(
                "Required export {!r} was not found in {!r}".format(name, self.path)
            ) from exc
        function.restype = restype
        function.argtypes = argtypes
        setattr(self, name, function)

    def _bind_many(self, definitions):
        for name, restype, argtypes in definitions:
            self._bind(name, restype, argtypes)

    def _bind_legacy(self):
        if self.is_windows:
            definitions = [
                ("ls_libversion", UInt16, []),
                ("ls_initialize", UInt32, [UInt32, UInt32, UInt32, Int32, PAnsiChar]),
                ("ls_enumdevices", Int32, []),
                ("ls_getfwversion", UInt16, [Int32]),
                ("ls_getvendorname", PAnsiChar, [Int32]),
                ("ls_getproductname", PAnsiChar, [Int32]),
                ("ls_getserialnumber", PAnsiChar, [Int32]),
                ("ls_devicecount", UInt8, []),
                ("ls_currentdeviceindex", Int32, []),
                ("ls_opendevicebyindex", UInt32, [Int32]),
                ("ls_opendevicebyserial", UInt32, [PAnsiChar]),
                ("ls_closedevice", UInt32, []),
                ("ls_waitforpipe", UInt32, [UInt32]),
                ("ls_waitforpipecount", UInt32, [Int32, P_Int32, UInt32]),
                ("ls_getpipe", UInt32, [PVOID, UInt32, P_UInt32]),
                ("ls_setpacketlength", UInt32, [UInt32]),
                ("ls_seteptimeout", UInt32, [UInt32]),
                ("ls_geteptimeout", UInt32, []),
                ("ls_getfps", UInt32, []),
                ("ls_geterrorstring", PAnsiChar, [UInt32]),
                ("ls_customfirmware", UInt32, [P_UInt32]),
                ("ls_setcfg1", UInt32, [UInt8]),
                ("ls_getcfg1", UInt32, [P_UInt8]),
                ("ls_getpacketlength", UInt32, [P_UInt32]),
                ("ls_setmuxchannel", UInt32, [UInt8]),
                ("ls_getmuxchannel", UInt32, [P_Int8]),
                ("ls_readi2c_ext", UInt32, [UInt8, PVOID, P_UInt16]),
                ("ls_writei2c_ext", UInt32, [UInt8, PVOID, UInt16]),
                ("ls_readi2c", UInt32, [UInt8, PVOID, P_UInt16]),
                ("ls_writei2c", UInt32, [UInt8, PVOID, UInt16]),
                ("ls_i2cread1byte", UInt32, [UInt8, UInt8, P_UInt8]),
                ("ls_i2cread2bytes", UInt32, [UInt8, UInt8, P_UInt16]),
                ("ls_i2cread4bytes", UInt32, [UInt8, UInt8, P_UInt32]),
                ("ls_i2cwrite1byte", UInt32, [UInt8, UInt8, UInt8]),
                ("ls_i2cwrite2bytes", UInt32, [UInt8, UInt8, UInt16]),
                ("ls_i2cwrite4bytes", UInt32, [UInt8, UInt8, UInt32]),
                ("ls_i2cwritecmd", UInt32, [UInt8, UInt8]),
                ("ls_hw_reset", UInt32, [UInt8]),
                ("ls_suspend", UInt32, [UInt8]),
                ("ls_resume", UInt32, [UInt8]),
                ("ls_updateparam", UInt32, [UInt8]),
                ("ls_setstate", UInt32, [UInt8, UInt8]),
                ("ls_setmodeconfig", UInt32, [UInt8, UInt8]),
                ("ls_setifconfig", UInt32, [UInt8, UInt8]),
                ("ls_setstpulse", UInt32, [UInt8, UInt32, UInt32]),
                ("ls_setsthigh", UInt32, [UInt8, UInt32]),
                ("ls_setstlow", UInt32, [UInt8, UInt32]),
                ("ls_setlinesperframe", UInt32, [UInt8, UInt16]),
                ("ls_setquadcount", UInt32, [UInt8, UInt32]),
                ("ls_setsofttriggertime", UInt32, [UInt8, UInt32]),
                ("ls_settriggerdelay", UInt32, [UInt8, UInt32]),
                ("ls_settriggerwidth", UInt32, [UInt8, UInt32]),
                ("ls_setpixelcount", UInt32, [UInt8, UInt16]),
                ("ls_setedgedelay", UInt32, [UInt8, UInt8]),
                ("ls_setadcvref", UInt32, [UInt8, UInt8]),
                ("ls_setadcgain", UInt32, [UInt8, UInt8]),
                ("ls_setadcoffset", UInt32, [UInt8, UInt16]),
                ("ls_setadcbias", UInt32, [UInt8, UInt16]),
                ("ls_setslaveaddress", UInt32, [UInt8, UInt8]),
                ("ls_saveslaveaddress", UInt32, [UInt8]),
                ("ls_savesettings", UInt32, [UInt8]),
                ("ls_reloadsettings", UInt32, [UInt8]),
                ("ls_resetsettings", UInt32, [UInt8]),
                ("ls_getmcuversion", UInt32, [UInt8, P_UInt16]),
                ("ls_getctgversion", UInt32, [UInt8, P_UInt16]),
                ("ls_getctgstate", UInt32, [UInt8, P_UInt8]),
                ("ls_getstate", UInt32, [UInt8, P_UInt8]),
                ("ls_getmodeconfig", UInt32, [UInt8, P_UInt8]),
                ("ls_getifconfig", UInt32, [UInt8, P_UInt8]),
                ("ls_getstpulse", UInt32, [UInt8, P_UInt32, P_UInt32]),
                ("ls_getsthigh", UInt32, [UInt8, P_UInt32]),
                ("ls_getstlow", UInt32, [UInt8, P_UInt32]),
                ("ls_getlinesperframe", UInt32, [UInt8, P_UInt16]),
                ("ls_getquadcount", UInt32, [UInt8, P_UInt32]),
                ("ls_getsofttriggertime", UInt32, [UInt8, P_UInt32]),
                ("ls_gettriggerdelay", UInt32, [UInt8, P_UInt32]),
                ("ls_gettriggerwidth", UInt32, [UInt8, P_UInt32]),
                ("ls_getpixelcount", UInt32, [UInt8, P_UInt16]),
                ("ls_getedgedelay", UInt32, [UInt8, P_UInt8]),
                ("ls_getadcvref", UInt32, [UInt8, P_UInt8]),
                ("ls_getadcgain", UInt32, [UInt8, P_UInt8]),
                ("ls_getadcoffset", UInt32, [UInt8, P_UInt16]),
                ("ls_getadcbias", UInt32, [UInt8, P_UInt16]),
                ("ls_getslaveaddress", UInt32, [UInt8, P_UInt8]),
                ("ls_gethw1id", UInt32, [UInt8, P_UInt16]),
                ("ls_gethw1version", UInt32, [UInt8, P_UInt16]),
                ("ls_gethw2version", UInt32, [UInt8, P_UInt16]),
                ("ls_getgainmult", UInt32, [UInt8, P_UInt16]),
                ("ls_getgaindiv", UInt32, [UInt8, P_UInt16]),
                ("ls_getinitstatus", UInt32, [UInt8, P_UInt8]),
                ("ls_getcomresult", UInt32, [UInt8, P_UInt8]),
                ("ls_shutdown", UInt32, []),
            ]
        else:
            definitions = [
                ("ls_libversion", UInt16, []),
                ("ls_initialize", None, [Int32, Int32]),
                ("ls_enumdevices", Int32, []),
                ("ls_getfwversion", UInt16, [Int32]),
                ("ls_getvendorname", PAnsiChar, [Int32]),
                ("ls_getproductname", PAnsiChar, [Int32]),
                ("ls_getserialnumber", PAnsiChar, [Int32]),
                ("ls_devicecount", UInt8, []),
                ("ls_currentdeviceindex", Int32, []),
                ("ls_opendevicebyindex", Int32, [Int32]),
                ("ls_opendevicebyserial", Int32, [PAnsiChar]),
                ("ls_closedevice", Int32, []),
                ("ls_waitforpipe", Int32, [UInt32]),
                ("ls_waitforpipecount", Int32, [Int32, P_Int32, UInt32]),
                ("ls_getpipe", Int32, [PVOID, UInt32]),
                ("ls_setpacketlength", Int32, [Int32]),
                ("ls_seteptimeout", Int32, [UInt32]),
                ("ls_geteptimeout", UInt32, []),
                ("ls_getfps", UInt32, []),
                ("ls_geterrorstring", PAnsiChar, [Int32]),
                ("ls_customfirmware", Int32, [P_UInt32, UInt32]),
                ("ls_setcfg1", Int32, [UInt8, UInt32]),
                ("ls_getcfg1", Int32, [P_UInt8, UInt32]),
                ("ls_getpacketlength", Int32, [P_Int32, UInt32]),
                ("ls_setmuxchannel", Int32, [UInt8, UInt32]),
                ("ls_getmuxchannel", Int32, [P_Int8, UInt32]),
                ("ls_readi2c_ext", Int32, [UInt8, PVOID, P_UInt16, UInt32]),
                ("ls_writei2c_ext", Int32, [UInt8, PVOID, UInt16, UInt32]),
                ("ls_readi2c", Int32, [UInt8, PVOID, P_UInt16, UInt32]),
                ("ls_writei2c", Int32, [UInt8, PVOID, UInt16, UInt32]),
                ("ls_i2cread1byte", Int32, [UInt8, UInt8, P_UInt8, UInt32]),
                ("ls_i2cread2bytes", Int32, [UInt8, UInt8, P_UInt16, UInt32]),
                ("ls_i2cread4bytes", Int32, [UInt8, UInt8, P_UInt32, UInt32]),
                ("ls_i2cwrite1byte", Int32, [UInt8, UInt8, UInt8, UInt32]),
                ("ls_i2cwrite2bytes", Int32, [UInt8, UInt8, UInt16, UInt32]),
                ("ls_i2cwrite4bytes", Int32, [UInt8, UInt8, UInt32, UInt32]),
                ("ls_i2cwritecmd", Int32, [UInt8, UInt8, UInt32]),
                ("ls_hw_reset", Int32, [UInt8, UInt32]),
                ("ls_suspend", Int32, [UInt8, UInt32]),
                ("ls_resume", Int32, [UInt8, UInt32]),
                ("ls_updateparam", Int32, [UInt8, UInt32]),
                ("ls_setstate", Int32, [UInt8, UInt8, UInt32]),
                ("ls_setmodeconfig", Int32, [UInt8, UInt8, UInt32]),
                ("ls_setifconfig", Int32, [UInt8, UInt8, UInt32]),
                ("ls_setstpulse", Int32, [UInt8, UInt32, UInt32, UInt32]),
                ("ls_setsthigh", Int32, [UInt8, UInt32, UInt32]),
                ("ls_setstlow", Int32, [UInt8, UInt32, UInt32]),
                ("ls_setlinesperframe", Int32, [UInt8, UInt16, UInt32]),
                ("ls_setquadcount", Int32, [UInt8, UInt32, UInt32]),
                ("ls_setsofttriggertime", Int32, [UInt8, UInt32, UInt32]),
                ("ls_settriggerdelay", Int32, [UInt8, UInt32, UInt32]),
                ("ls_settriggerwidth", Int32, [UInt8, UInt32, UInt32]),
                ("ls_setpixelcount", Int32, [UInt8, UInt16, UInt32]),
                ("ls_setedgedelay", Int32, [UInt8, UInt8, UInt32]),
                ("ls_setadcvref", Int32, [UInt8, UInt8, UInt32]),
                ("ls_setadcgain", Int32, [UInt8, UInt8, UInt32]),
                ("ls_setadcoffset", Int32, [UInt8, UInt16, UInt32]),
                ("ls_setadcbias", Int32, [UInt8, UInt16, UInt32]),
                ("ls_setslaveaddress", Int32, [UInt8, UInt8, UInt32]),
                ("ls_saveslaveaddress", Int32, [UInt8, UInt32]),
                ("ls_savesettings", Int32, [UInt8, UInt32]),
                ("ls_reloadsettings", Int32, [UInt8, UInt32]),
                ("ls_resetsettings", Int32, [UInt8, UInt32]),
                ("ls_getmcuversion", Int32, [UInt8, P_UInt16, UInt32]),
                ("ls_getctgversion", Int32, [UInt8, P_UInt16, UInt32]),
                ("ls_getctgstate", Int32, [UInt8, P_UInt8, UInt32]),
                ("ls_getstate", Int32, [UInt8, P_UInt8, UInt32]),
                ("ls_getmodeconfig", Int32, [UInt8, P_UInt8, UInt32]),
                ("ls_getifconfig", Int32, [UInt8, P_UInt8, UInt32]),
                ("ls_getstpulse", Int32, [UInt8, P_UInt32, P_UInt32, UInt32]),
                ("ls_getsthigh", Int32, [UInt8, P_UInt32, UInt32]),
                ("ls_getstlow", Int32, [UInt8, P_UInt32, UInt32]),
                ("ls_getlinesperframe", Int32, [UInt8, P_UInt16, UInt32]),
                ("ls_getquadcount", Int32, [UInt8, P_UInt32, UInt32]),
                ("ls_getsofttriggertime", Int32, [UInt8, P_UInt32, UInt32]),
                ("ls_gettriggerdelay", Int32, [UInt8, P_UInt32, UInt32]),
                ("ls_gettriggerwidth", Int32, [UInt8, P_UInt32, UInt32]),
                ("ls_getpixelcount", Int32, [UInt8, P_UInt16, UInt32]),
                ("ls_getedgedelay", Int32, [UInt8, P_UInt8, UInt32]),
                ("ls_getadcvref", Int32, [UInt8, P_UInt8, UInt32]),
                ("ls_getadcgain", Int32, [UInt8, P_UInt8, UInt32]),
                ("ls_getadcoffset", Int32, [UInt8, P_UInt16, UInt32]),
                ("ls_getadcbias", Int32, [UInt8, P_UInt16, UInt32]),
                ("ls_getslaveaddress", Int32, [UInt8, P_UInt8, UInt32]),
                ("ls_gethw1id", Int32, [UInt8, P_UInt16, UInt32]),
                ("ls_gethw1version", Int32, [UInt8, P_UInt16, UInt32]),
                ("ls_gethw2version", Int32, [UInt8, P_UInt16, UInt32]),
                ("ls_getgainmult", Int32, [UInt8, P_UInt16, UInt32]),
                ("ls_getgaindiv", Int32, [UInt8, P_UInt16, UInt32]),
                ("ls_getinitstatus", Int32, [UInt8, P_UInt8, UInt32]),
                ("ls_getcomresult", Int32, [UInt8, P_UInt8, UInt32]),
                ("ls_shutdown", Int32, []),
            ]
        self._bind_many(definitions)

    def _bind_ex(self):
        definitions = [
            ("ls_seteptimeout_ex", LSResult, [LSHandle, UInt32]),
            ("ls_geteptimeout_ex", LSResult, [LSHandle, P_UInt32]),
            ("ls_setthreadpriority_ex", LSResult, [LSHandle, Int32]),
            ("ls_getthreadpriority_ex", LSResult, [LSHandle, P_Int32]),
            ("ls_opendevicebyindex_ex", LSResult, [Int32, UInt32, UInt32, P_LSHandle]),
            ("ls_opendevicebyserial_ex", LSResult, [PAnsiChar, UInt32, UInt32, P_LSHandle]),
            ("ls_closedevice_ex", LSResult, [LSHandle]),
            ("ls_getpipe_ex", LSResult, [LSHandle, PVOID, UInt32, P_UInt32]),
            ("ls_waitforpipe_ex", LSResult, [LSHandle, UInt32]),
            ("ls_waitforpipecount_ex", LSResult, [LSHandle, Int32, P_Int32, UInt32]),
            ("ls_getfps_ex", UInt32, [LSHandle]),
            ("ls_getpacketlength_ex", LSResult, [LSHandle, P_UInt32, UInt32]),
            ("ls_customfirmware_ex", LSResult, [LSHandle, P_UInt32, UInt32]),
            ("ls_setcfg1_ex", LSResult, [LSHandle, UInt8, UInt32]),
            ("ls_getcfg1_ex", LSResult, [LSHandle, P_UInt8, UInt32]),
            ("ls_setslaveaddress_ex", LSResult, [LSHandle, UInt8, UInt8, UInt32]),
            ("ls_getslaveaddress_ex", LSResult, [LSHandle, UInt8, P_UInt8, UInt32]),
            ("ls_saveslaveaddress_ex", LSResult, [LSHandle, UInt8, UInt32]),
            ("ls_setmuxchannel_ex", LSResult, [LSHandle, UInt8, UInt32]),
            ("ls_getmuxchannel_ex", LSResult, [LSHandle, P_Int8, UInt32]),
            ("ls_readi2c_ex", LSResult, [LSHandle, UInt8, PVOID, P_UInt16, UInt32]),
            ("ls_writei2c_ex", LSResult, [LSHandle, UInt8, PVOID, UInt16, UInt32]),
            ("ls_readi2c_ext_ex", LSResult, [LSHandle, UInt8, PVOID, P_UInt16, UInt32]),
            ("ls_writei2c_ext_ex", LSResult, [LSHandle, UInt8, PVOID, UInt16, UInt32]),
            ("ls_i2cread1byte_ex", LSResult, [LSHandle, UInt8, UInt8, P_UInt8, UInt32]),
            ("ls_i2cread2bytes_ex", LSResult, [LSHandle, UInt8, UInt8, P_UInt16, UInt32]),
            ("ls_i2cread4bytes_ex", LSResult, [LSHandle, UInt8, UInt8, P_UInt32, UInt32]),
            ("ls_i2cwrite1byte_ex", LSResult, [LSHandle, UInt8, UInt8, UInt8, UInt32]),
            ("ls_i2cwrite2bytes_ex", LSResult, [LSHandle, UInt8, UInt8, UInt16, UInt32]),
            ("ls_i2cwrite4bytes_ex", LSResult, [LSHandle, UInt8, UInt8, UInt32, UInt32]),
            ("ls_i2cwritecmd_ex", LSResult, [LSHandle, UInt8, UInt8, UInt32]),
            ("ls_hw_reset_ex", LSResult, [LSHandle, UInt8, UInt32]),
            ("ls_resume_ex", LSResult, [LSHandle, UInt8, UInt32]),
            ("ls_suspend_ex", LSResult, [LSHandle, UInt8, UInt32]),
            ("ls_updateparam_ex", LSResult, [LSHandle, UInt8, UInt32]),
            ("ls_setstate_ex", LSResult, [LSHandle, UInt8, UInt8, UInt32]),
            ("ls_setmodeconfig_ex", LSResult, [LSHandle, UInt8, UInt8, UInt32]),
            ("ls_setifconfig_ex", LSResult, [LSHandle, UInt8, UInt8, UInt32]),
            ("ls_setsthigh_ex", LSResult, [LSHandle, UInt8, UInt32, UInt32]),
            ("ls_setstlow_ex", LSResult, [LSHandle, UInt8, UInt32, UInt32]),
            ("ls_setstpulse_ex", LSResult, [LSHandle, UInt8, UInt32, UInt32, UInt32]),
            ("ls_setlinesperframe_ex", LSResult, [LSHandle, UInt8, UInt16, UInt32]),
            ("ls_setquadcount_ex", LSResult, [LSHandle, UInt8, UInt32, UInt32]),
            ("ls_setsofttriggertime_ex", LSResult, [LSHandle, UInt8, UInt32, UInt32]),
            ("ls_settriggerdelay_ex", LSResult, [LSHandle, UInt8, UInt32, UInt32]),
            ("ls_settriggerwidth_ex", LSResult, [LSHandle, UInt8, UInt32, UInt32]),
            ("ls_setpixelcount_ex", LSResult, [LSHandle, UInt8, UInt16, UInt32]),
            ("ls_setedgedelay_ex", LSResult, [LSHandle, UInt8, UInt8, UInt32]),
            ("ls_setadcvref_ex", LSResult, [LSHandle, UInt8, UInt8, UInt32]),
            ("ls_setadcgain_ex", LSResult, [LSHandle, UInt8, UInt8, UInt32]),
            ("ls_setadcoffset_ex", LSResult, [LSHandle, UInt8, UInt16, UInt32]),
            ("ls_setadcbias_ex", LSResult, [LSHandle, UInt8, UInt16, UInt32]),
            ("ls_getctgstate_ex", LSResult, [LSHandle, UInt8, P_UInt8, UInt32]),
            ("ls_getstate_ex", LSResult, [LSHandle, UInt8, P_UInt8, UInt32]),
            ("ls_getmodeconfig_ex", LSResult, [LSHandle, UInt8, P_UInt8, UInt32]),
            ("ls_getifconfig_ex", LSResult, [LSHandle, UInt8, P_UInt8, UInt32]),
            ("ls_getsthigh_ex", LSResult, [LSHandle, UInt8, P_UInt32, UInt32]),
            ("ls_getstlow_ex", LSResult, [LSHandle, UInt8, P_UInt32, UInt32]),
            ("ls_getstpulse_ex", LSResult, [LSHandle, UInt8, P_UInt32, P_UInt32, UInt32]),
            ("ls_getlinesperframe_ex", LSResult, [LSHandle, UInt8, P_UInt16, UInt32]),
            ("ls_getquadcount_ex", LSResult, [LSHandle, UInt8, P_UInt32, UInt32]),
            ("ls_getsofttriggertime_ex", LSResult, [LSHandle, UInt8, P_UInt32, UInt32]),
            ("ls_gettriggerdelay_ex", LSResult, [LSHandle, UInt8, P_UInt32, UInt32]),
            ("ls_gettriggerwidth_ex", LSResult, [LSHandle, UInt8, P_UInt32, UInt32]),
            ("ls_getpixelcount_ex", LSResult, [LSHandle, UInt8, P_UInt16, UInt32]),
            ("ls_getedgedelay_ex", LSResult, [LSHandle, UInt8, P_UInt8, UInt32]),
            ("ls_getadcvref_ex", LSResult, [LSHandle, UInt8, P_UInt8, UInt32]),
            ("ls_getadcgain_ex", LSResult, [LSHandle, UInt8, P_UInt8, UInt32]),
            ("ls_getgainmult_ex", LSResult, [LSHandle, UInt8, P_UInt16, UInt32]),
            ("ls_getgaindiv_ex", LSResult, [LSHandle, UInt8, P_UInt16, UInt32]),
            ("ls_getadcoffset_ex", LSResult, [LSHandle, UInt8, P_UInt16, UInt32]),
            ("ls_getadcbias_ex", LSResult, [LSHandle, UInt8, P_UInt16, UInt32]),
            ("ls_savesettings_ex", LSResult, [LSHandle, UInt8, UInt32]),
            ("ls_reloadsettings_ex", LSResult, [LSHandle, UInt8, UInt32]),
            ("ls_resetsettings_ex", LSResult, [LSHandle, UInt8, UInt32]),
            ("ls_getinitstatus_ex", LSResult, [LSHandle, UInt8, P_UInt8, UInt32]),
            ("ls_getcomresult_ex", LSResult, [LSHandle, UInt8, P_UInt8, UInt32]),
            ("ls_getmcuversion_ex", LSResult, [LSHandle, UInt8, P_UInt16, UInt32]),
            ("ls_getctgversion_ex", LSResult, [LSHandle, UInt8, P_UInt16, UInt32]),
            ("ls_gethw1id_ex", LSResult, [LSHandle, UInt8, P_UInt16, UInt32]),
            ("ls_gethw1version_ex", LSResult, [LSHandle, UInt8, P_UInt16, UInt32]),
            ("ls_gethw2version_ex", LSResult, [LSHandle, UInt8, P_UInt16, UInt32]),
        ]
        self._bind_many(definitions)


class USBLISMPI26:
    """High-level wrapper for the legacy single-device API."""

    def __init__(self, library_path: Optional[PathLike] = None):
        self._w = _Library(library_path)

    @property
    def library_path(self) -> str:
        return self._w.path

    @property
    def raw(self) -> _Library:
        """Return the low-level ctypes binding."""
        return self._w

    def _legacy_call(self, name: str, *args, timeout_ms: int = DEFAULT_TIMEOUT_MS) -> int:
        function = getattr(self._w, name)
        if self._w.is_windows:
            return int(function(*args))
        return int(function(*args, UInt32(timeout_ms)))

    def _legacy_get(self, name: str, ctype, addr: int, timeout_ms: int):
        value = ctype(0)
        result = self._legacy_call(
            name, UInt8(addr), _ct.byref(value), timeout_ms=timeout_ms
        )
        return result, int(value.value)

    # --- Lifecycle and device discovery ---
    def initialize(
        self,
        pipe_size: int,
        packet_length: int,
        thread_class: int = 0,
        thread_prio: int = 0,
        msg_id: Optional[Union[str, bytes]] = None,
    ) -> int:
        if self._w.is_windows:
            return int(
                self._w.ls_initialize(
                    UInt32(pipe_size),
                    UInt32(packet_length),
                    UInt32(thread_class),
                    Int32(thread_prio),
                    _to_bytes(msg_id),
                )
            )
        self._w.ls_initialize(Int32(pipe_size), Int32(packet_length))
        return RET_OK

    def enumdevices(self) -> int:
        return int(self._w.ls_enumdevices())

    def getfwversion(self, index: int) -> int:
        return int(self._w.ls_getfwversion(Int32(index)))

    def getvendorname(self, index: int) -> Optional[str]:
        return _from_c_string(self._w.ls_getvendorname(Int32(index)))

    def getproductname(self, index: int) -> Optional[str]:
        return _from_c_string(self._w.ls_getproductname(Int32(index)))

    def getserialnumber(self, index: int) -> Optional[str]:
        return _from_c_string(self._w.ls_getserialnumber(Int32(index)))

    def devicecount(self) -> int:
        return int(self._w.ls_devicecount())

    def currentdeviceindex(self) -> int:
        return int(self._w.ls_currentdeviceindex())

    def opendevicebyindex(self, index: int) -> int:
        return int(self._w.ls_opendevicebyindex(Int32(index)))

    def opendevicebyserial(self, serial: Union[str, bytes]) -> int:
        return int(self._w.ls_opendevicebyserial(_to_bytes(serial)))

    def closedevice(self) -> int:
        return int(self._w.ls_closedevice())

    def shutdown(self) -> int:
        return int(self._w.ls_shutdown())

    def free(self) -> int:
        """Compatibility alias for shutdown()."""
        return self.shutdown()

    def libversion(self) -> int:
        return int(self._w.ls_libversion())

    # --- Handle-based device creation ---
    def opendevicebyindex_ex(
        self, index: int, pipe_size: int, packet_length: int
    ) -> Tuple[int, Optional["USBLISMPI26Device"]]:
        handle = LSHandle()
        result = int(
            self._w.ls_opendevicebyindex_ex(
                Int32(index), UInt32(pipe_size), UInt32(packet_length), _ct.byref(handle)
            )
        )
        if result != RET_OK or not handle.value:
            return result, None
        return result, USBLISMPI26Device(self, handle)

    def opendevicebyserial_ex(
        self,
        serial: Union[str, bytes],
        pipe_size: int,
        packet_length: int,
    ) -> Tuple[int, Optional["USBLISMPI26Device"]]:
        handle = LSHandle()
        result = int(
            self._w.ls_opendevicebyserial_ex(
                _to_bytes(serial),
                UInt32(pipe_size),
                UInt32(packet_length),
                _ct.byref(handle),
            )
        )
        if result != RET_OK or not handle.value:
            return result, None
        return result, USBLISMPI26Device(self, handle)

    # Shorter aliases for new Python code.
    open_ex_by_index = opendevicebyindex_ex
    open_ex_by_serial = opendevicebyserial_ex

    # --- Data transfer ---
    def waitforpipe(self, timeout_ms: int) -> int:
        return int(self._w.ls_waitforpipe(UInt32(timeout_ms)))

    def waitforpipecount(self, count: int, timeout_ms: int) -> Tuple[int, int]:
        available = Int32(0)
        result = int(
            self._w.ls_waitforpipecount(
                Int32(count), _ct.byref(available), UInt32(timeout_ms)
            )
        )
        return result, int(available.value)

    def getpipe(self, nbytes: int) -> Tuple[int, bytes]:
        if nbytes < 0:
            raise ValueError("nbytes must not be negative")
        buffer = (UInt8 * nbytes)()
        if self._w.is_windows:
            read_count = UInt32(0)
            result = int(
                self._w.ls_getpipe(
                    _ct.cast(buffer, PVOID) if nbytes else PVOID(),
                    UInt32(nbytes),
                    _ct.byref(read_count),
                )
            )
            return result, bytes(buffer[: read_count.value])

        linux_result = int(
            self._w.ls_getpipe(
                _ct.cast(buffer, PVOID) if nbytes else PVOID(), UInt32(nbytes)
            )
        )
        if linux_result < 0:
            return linux_result, b""
        return RET_OK, bytes(buffer[:linux_result])

    def getpipe_available_result(self) -> Tuple[int, int]:
        if self._w.is_windows:
            available = UInt32(0)
            result = int(self._w.ls_getpipe(PVOID(), UInt32(0), _ct.byref(available)))
            return result, int(available.value)

        linux_result = int(self._w.ls_getpipe(PVOID(), UInt32(0)))
        if linux_result < 0:
            return linux_result, 0
        return RET_OK, linux_result

    def getpipe_available(self) -> int:
        """Backward-compatible fill-level query; returns only the byte count."""
        return self.getpipe_available_result()[1]

    def setpacketlength(self, packet_len: int) -> int:
        if self._w.is_windows:
            return int(self._w.ls_setpacketlength(UInt32(packet_len)))
        return int(self._w.ls_setpacketlength(Int32(packet_len)))

    def seteptimeout(self, timeout_ms: int) -> int:
        return int(self._w.ls_seteptimeout(UInt32(timeout_ms)))

    def geteptimeout(self) -> int:
        return int(self._w.ls_geteptimeout())

    def getfps(self) -> int:
        return int(self._w.ls_getfps())

    def geterrorstring(self, error: int) -> Optional[str]:
        if self._w.is_windows:
            value = self._w.ls_geterrorstring(UInt32(error))
        else:
            value = self._w.ls_geterrorstring(Int32(error))
        return _from_c_string(value)

    def customfirmware(self, timeout_ms: int = DEFAULT_TIMEOUT_MS) -> Tuple[int, int]:
        value = UInt32(0)
        result = self._legacy_call(
            "ls_customfirmware", _ct.byref(value), timeout_ms=timeout_ms
        )
        return result, int(value.value)

    def setcfg1(self, cfg1: int, timeout_ms: int = DEFAULT_TIMEOUT_MS) -> int:
        return self._legacy_call("ls_setcfg1", UInt8(cfg1), timeout_ms=timeout_ms)

    def getcfg1(self, timeout_ms: int = DEFAULT_TIMEOUT_MS) -> Tuple[int, int]:
        value = UInt8(0)
        result = self._legacy_call(
            "ls_getcfg1", _ct.byref(value), timeout_ms=timeout_ms
        )
        return result, int(value.value)

    def getpacketlength(self, timeout_ms: int = DEFAULT_TIMEOUT_MS) -> Tuple[int, int]:
        if self._w.is_windows:
            value = UInt32(0)
            result = int(self._w.ls_getpacketlength(_ct.byref(value)))
        else:
            value = Int32(0)
            result = int(
                self._w.ls_getpacketlength(_ct.byref(value), UInt32(timeout_ms))
            )
        return result, int(value.value)

    # --- I2C ---
    def setmuxchannel(self, channel: int, timeout_ms: int = DEFAULT_TIMEOUT_MS) -> int:
        return self._legacy_call(
            "ls_setmuxchannel", UInt8(channel), timeout_ms=timeout_ms
        )

    def getmuxchannel(self, timeout_ms: int = DEFAULT_TIMEOUT_MS) -> Tuple[int, int]:
        value = Int8(0)
        result = self._legacy_call(
            "ls_getmuxchannel", _ct.byref(value), timeout_ms=timeout_ms
        )
        return result, int(value.value)

    def _legacy_read_i2c(
        self, name: str, address: int, length: int, timeout_ms: int
    ) -> Tuple[int, bytes, int]:
        _validate_u16_length(length)
        buffer = (UInt8 * length)()
        actual = UInt16(length)
        result = self._legacy_call(
            name,
            UInt8(address),
            _ct.cast(buffer, PVOID) if length else PVOID(),
            _ct.byref(actual),
            timeout_ms=timeout_ms,
        )
        return result, bytes(buffer[: actual.value]), int(actual.value)

    def _legacy_write_i2c(
        self,
        name: str,
        address: int,
        data: Union[bytes, bytearray, memoryview],
        timeout_ms: int,
    ) -> int:
        raw, buffer, pointer = _buffer_from_bytes(data)
        _validate_u16_length(len(raw))
        return self._legacy_call(
            name, UInt8(address), pointer, UInt16(len(raw)), timeout_ms=timeout_ms
        )

    def readi2c_ext(
        self, address: int, length: int, timeout_ms: int = DEFAULT_TIMEOUT_MS
    ) -> Tuple[int, bytes, int]:
        return self._legacy_read_i2c(
            "ls_readi2c_ext", address, length, timeout_ms
        )

    def writei2c_ext(
        self,
        address: int,
        data: Union[bytes, bytearray, memoryview],
        timeout_ms: int = DEFAULT_TIMEOUT_MS,
    ) -> int:
        return self._legacy_write_i2c(
            "ls_writei2c_ext", address, data, timeout_ms
        )

    def readi2c(
        self, address: int, length: int, timeout_ms: int = DEFAULT_TIMEOUT_MS
    ) -> Tuple[int, bytes, int]:
        return self._legacy_read_i2c("ls_readi2c", address, length, timeout_ms)

    def writei2c(
        self,
        address: int,
        data: Union[bytes, bytearray, memoryview],
        timeout_ms: int = DEFAULT_TIMEOUT_MS,
    ) -> int:
        return self._legacy_write_i2c("ls_writei2c", address, data, timeout_ms)

    def i2cread1byte(self, address: int, register: int, timeout_ms: int = DEFAULT_TIMEOUT_MS) -> Tuple[int, int]:
        value = UInt8(0)
        result = self._legacy_call("ls_i2cread1byte", UInt8(address), UInt8(register), _ct.byref(value), timeout_ms=timeout_ms)
        return result, int(value.value)

    def i2cread2bytes(self, address: int, register: int, timeout_ms: int = DEFAULT_TIMEOUT_MS) -> Tuple[int, int]:
        value = UInt16(0)
        result = self._legacy_call("ls_i2cread2bytes", UInt8(address), UInt8(register), _ct.byref(value), timeout_ms=timeout_ms)
        return result, int(value.value)

    def i2cread4bytes(self, address: int, register: int, timeout_ms: int = DEFAULT_TIMEOUT_MS) -> Tuple[int, int]:
        value = UInt32(0)
        result = self._legacy_call("ls_i2cread4bytes", UInt8(address), UInt8(register), _ct.byref(value), timeout_ms=timeout_ms)
        return result, int(value.value)

    def i2cwrite1byte(self, address: int, register: int, value: int, timeout_ms: int = DEFAULT_TIMEOUT_MS) -> int:
        return self._legacy_call("ls_i2cwrite1byte", UInt8(address), UInt8(register), UInt8(value), timeout_ms=timeout_ms)

    def i2cwrite2bytes(self, address: int, register: int, value: int, timeout_ms: int = DEFAULT_TIMEOUT_MS) -> int:
        return self._legacy_call("ls_i2cwrite2bytes", UInt8(address), UInt8(register), UInt16(value), timeout_ms=timeout_ms)

    def i2cwrite4bytes(self, address: int, register: int, value: int, timeout_ms: int = DEFAULT_TIMEOUT_MS) -> int:
        return self._legacy_call("ls_i2cwrite4bytes", UInt8(address), UInt8(register), UInt32(value), timeout_ms=timeout_ms)

    def i2cwritecmd(self, address: int, command: int, timeout_ms: int = DEFAULT_TIMEOUT_MS) -> int:
        return self._legacy_call("ls_i2cwritecmd", UInt8(address), UInt8(command), timeout_ms=timeout_ms)

    # --- CTG/configuration setters ---
    def hw_reset(self, addr: int, timeout_ms: int = DEFAULT_TIMEOUT_MS) -> int:
        return self._legacy_call("ls_hw_reset", UInt8(addr), timeout_ms=timeout_ms)

    def suspend(self, addr: int, timeout_ms: int = DEFAULT_TIMEOUT_MS) -> int:
        return self._legacy_call("ls_suspend", UInt8(addr), timeout_ms=timeout_ms)

    def resume(self, addr: int, timeout_ms: int = DEFAULT_TIMEOUT_MS) -> int:
        return self._legacy_call("ls_resume", UInt8(addr), timeout_ms=timeout_ms)

    def updateparam(self, addr: int, timeout_ms: int = DEFAULT_TIMEOUT_MS) -> int:
        return self._legacy_call("ls_updateparam", UInt8(addr), timeout_ms=timeout_ms)

    def setstate(self, addr: int, state: int, timeout_ms: int = DEFAULT_TIMEOUT_MS) -> int:
        return self._legacy_call("ls_setstate", UInt8(addr), UInt8(state), timeout_ms=timeout_ms)

    def setmodeconfig(self, addr: int, config: int, timeout_ms: int = DEFAULT_TIMEOUT_MS) -> int:
        return self._legacy_call("ls_setmodeconfig", UInt8(addr), UInt8(config), timeout_ms=timeout_ms)

    def setifconfig(self, addr: int, config: int, timeout_ms: int = DEFAULT_TIMEOUT_MS) -> int:
        return self._legacy_call("ls_setifconfig", UInt8(addr), UInt8(config), timeout_ms=timeout_ms)

    def setstpulse(self, addr: int, high: int, low: int, timeout_ms: int = DEFAULT_TIMEOUT_MS) -> int:
        return self._legacy_call("ls_setstpulse", UInt8(addr), UInt32(high), UInt32(low), timeout_ms=timeout_ms)

    def setsthigh(self, addr: int, high: int, timeout_ms: int = DEFAULT_TIMEOUT_MS) -> int:
        return self._legacy_call("ls_setsthigh", UInt8(addr), UInt32(high), timeout_ms=timeout_ms)

    def setstlow(self, addr: int, low: int, timeout_ms: int = DEFAULT_TIMEOUT_MS) -> int:
        return self._legacy_call("ls_setstlow", UInt8(addr), UInt32(low), timeout_ms=timeout_ms)

    def setlinesperframe(self, addr: int, lines: int, timeout_ms: int = DEFAULT_TIMEOUT_MS) -> int:
        return self._legacy_call("ls_setlinesperframe", UInt8(addr), UInt16(lines), timeout_ms=timeout_ms)

    def setquadcount(self, addr: int, count: int, timeout_ms: int = DEFAULT_TIMEOUT_MS) -> int:
        return self._legacy_call("ls_setquadcount", UInt8(addr), UInt32(count), timeout_ms=timeout_ms)

    def setsofttriggertime(self, addr: int, time_value: int, timeout_ms: int = DEFAULT_TIMEOUT_MS) -> int:
        return self._legacy_call("ls_setsofttriggertime", UInt8(addr), UInt32(time_value), timeout_ms=timeout_ms)

    def settriggerdelay(self, addr: int, delay: int, timeout_ms: int = DEFAULT_TIMEOUT_MS) -> int:
        return self._legacy_call("ls_settriggerdelay", UInt8(addr), UInt32(delay), timeout_ms=timeout_ms)

    def settriggerwidth(self, addr: int, width: int, timeout_ms: int = DEFAULT_TIMEOUT_MS) -> int:
        return self._legacy_call("ls_settriggerwidth", UInt8(addr), UInt32(width), timeout_ms=timeout_ms)

    def setpixelcount(self, addr: int, count: int, timeout_ms: int = DEFAULT_TIMEOUT_MS) -> int:
        return self._legacy_call("ls_setpixelcount", UInt8(addr), UInt16(count), timeout_ms=timeout_ms)

    def setedgedelay(self, addr: int, edges: int, timeout_ms: int = DEFAULT_TIMEOUT_MS) -> int:
        return self._legacy_call("ls_setedgedelay", UInt8(addr), UInt8(edges), timeout_ms=timeout_ms)

    def setadcvref(self, addr: int, vref: int, timeout_ms: int = DEFAULT_TIMEOUT_MS) -> int:
        return self._legacy_call("ls_setadcvref", UInt8(addr), UInt8(vref), timeout_ms=timeout_ms)

    def setadcgain(self, addr: int, gain: int, timeout_ms: int = DEFAULT_TIMEOUT_MS) -> int:
        return self._legacy_call("ls_setadcgain", UInt8(addr), UInt8(gain), timeout_ms=timeout_ms)

    def setadcoffset(self, addr: int, offset: int, timeout_ms: int = DEFAULT_TIMEOUT_MS) -> int:
        return self._legacy_call("ls_setadcoffset", UInt8(addr), UInt16(offset), timeout_ms=timeout_ms)

    def setadcbias(self, addr: int, bias: int, timeout_ms: int = DEFAULT_TIMEOUT_MS) -> int:
        return self._legacy_call("ls_setadcbias", UInt8(addr), UInt16(bias), timeout_ms=timeout_ms)

    def setslaveaddress(self, addr: int, new_addr: int, timeout_ms: int = DEFAULT_TIMEOUT_MS) -> int:
        return self._legacy_call("ls_setslaveaddress", UInt8(addr), UInt8(new_addr), timeout_ms=timeout_ms)

    def saveslaveaddress(self, addr: int, timeout_ms: int = DEFAULT_TIMEOUT_MS) -> int:
        return self._legacy_call("ls_saveslaveaddress", UInt8(addr), timeout_ms=timeout_ms)

    def savesettings(self, addr: int, timeout_ms: int = DEFAULT_TIMEOUT_MS) -> int:
        return self._legacy_call("ls_savesettings", UInt8(addr), timeout_ms=timeout_ms)

    def reloadsettings(self, addr: int, timeout_ms: int = DEFAULT_TIMEOUT_MS) -> int:
        return self._legacy_call("ls_reloadsettings", UInt8(addr), timeout_ms=timeout_ms)

    def resetsettings(self, addr: int, timeout_ms: int = DEFAULT_TIMEOUT_MS) -> int:
        return self._legacy_call("ls_resetsettings", UInt8(addr), timeout_ms=timeout_ms)

    # --- CTG/configuration getters ---
    def getmcuversion(self, addr: int, timeout_ms: int = DEFAULT_TIMEOUT_MS):
        return self._legacy_get("ls_getmcuversion", UInt16, addr, timeout_ms)

    def getctgversion(self, addr: int, timeout_ms: int = DEFAULT_TIMEOUT_MS):
        return self._legacy_get("ls_getctgversion", UInt16, addr, timeout_ms)

    def getctgstate(self, addr: int, timeout_ms: int = DEFAULT_TIMEOUT_MS):
        return self._legacy_get("ls_getctgstate", UInt8, addr, timeout_ms)

    def getstate(self, addr: int, timeout_ms: int = DEFAULT_TIMEOUT_MS):
        return self._legacy_get("ls_getstate", UInt8, addr, timeout_ms)

    def getmodeconfig(self, addr: int, timeout_ms: int = DEFAULT_TIMEOUT_MS):
        return self._legacy_get("ls_getmodeconfig", UInt8, addr, timeout_ms)

    def getifconfig(self, addr: int, timeout_ms: int = DEFAULT_TIMEOUT_MS):
        return self._legacy_get("ls_getifconfig", UInt8, addr, timeout_ms)

    def getstpulse(self, addr: int, timeout_ms: int = DEFAULT_TIMEOUT_MS):
        high = UInt32(0)
        low = UInt32(0)
        result = self._legacy_call("ls_getstpulse", UInt8(addr), _ct.byref(high), _ct.byref(low), timeout_ms=timeout_ms)
        return result, int(high.value), int(low.value)

    def getsthigh(self, addr: int, timeout_ms: int = DEFAULT_TIMEOUT_MS):
        return self._legacy_get("ls_getsthigh", UInt32, addr, timeout_ms)

    def getstlow(self, addr: int, timeout_ms: int = DEFAULT_TIMEOUT_MS):
        return self._legacy_get("ls_getstlow", UInt32, addr, timeout_ms)

    def getlinesperframe(self, addr: int, timeout_ms: int = DEFAULT_TIMEOUT_MS):
        return self._legacy_get("ls_getlinesperframe", UInt16, addr, timeout_ms)

    def getquadcount(self, addr: int, timeout_ms: int = DEFAULT_TIMEOUT_MS):
        return self._legacy_get("ls_getquadcount", UInt32, addr, timeout_ms)

    def getsofttriggertime(self, addr: int, timeout_ms: int = DEFAULT_TIMEOUT_MS):
        return self._legacy_get("ls_getsofttriggertime", UInt32, addr, timeout_ms)

    def gettriggerdelay(self, addr: int, timeout_ms: int = DEFAULT_TIMEOUT_MS):
        return self._legacy_get("ls_gettriggerdelay", UInt32, addr, timeout_ms)

    def gettriggerwidth(self, addr: int, timeout_ms: int = DEFAULT_TIMEOUT_MS):
        return self._legacy_get("ls_gettriggerwidth", UInt32, addr, timeout_ms)

    def getpixelcount(self, addr: int, timeout_ms: int = DEFAULT_TIMEOUT_MS):
        return self._legacy_get("ls_getpixelcount", UInt16, addr, timeout_ms)

    def getedgedelay(self, addr: int, timeout_ms: int = DEFAULT_TIMEOUT_MS):
        return self._legacy_get("ls_getedgedelay", UInt8, addr, timeout_ms)

    def getadcvref(self, addr: int, timeout_ms: int = DEFAULT_TIMEOUT_MS):
        return self._legacy_get("ls_getadcvref", UInt8, addr, timeout_ms)

    def getadcgain(self, addr: int, timeout_ms: int = DEFAULT_TIMEOUT_MS):
        return self._legacy_get("ls_getadcgain", UInt8, addr, timeout_ms)

    def getadcoffset(self, addr: int, timeout_ms: int = DEFAULT_TIMEOUT_MS):
        return self._legacy_get("ls_getadcoffset", UInt16, addr, timeout_ms)

    def getadcbias(self, addr: int, timeout_ms: int = DEFAULT_TIMEOUT_MS):
        return self._legacy_get("ls_getadcbias", UInt16, addr, timeout_ms)

    def getslaveaddress(self, addr: int, timeout_ms: int = DEFAULT_TIMEOUT_MS):
        return self._legacy_get("ls_getslaveaddress", UInt8, addr, timeout_ms)

    def gethw1id(self, addr: int, timeout_ms: int = DEFAULT_TIMEOUT_MS):
        return self._legacy_get("ls_gethw1id", UInt16, addr, timeout_ms)

    def gethw1version(self, addr: int, timeout_ms: int = DEFAULT_TIMEOUT_MS):
        return self._legacy_get("ls_gethw1version", UInt16, addr, timeout_ms)

    def gethw2version(self, addr: int, timeout_ms: int = DEFAULT_TIMEOUT_MS):
        return self._legacy_get("ls_gethw2version", UInt16, addr, timeout_ms)

    def getgainmult(self, addr: int, timeout_ms: int = DEFAULT_TIMEOUT_MS):
        return self._legacy_get("ls_getgainmult", UInt16, addr, timeout_ms)

    def getgaindiv(self, addr: int, timeout_ms: int = DEFAULT_TIMEOUT_MS):
        return self._legacy_get("ls_getgaindiv", UInt16, addr, timeout_ms)

    def getinitstatus(self, addr: int, timeout_ms: int = DEFAULT_TIMEOUT_MS):
        return self._legacy_get("ls_getinitstatus", UInt8, addr, timeout_ms)

    def getcomresult(self, addr: int, timeout_ms: int = DEFAULT_TIMEOUT_MS):
        return self._legacy_get("ls_getcomresult", UInt8, addr, timeout_ms)


class USBLISMPI26Device:
    """One open device using the handle-based *_ex API."""

    def __init__(self, owner: USBLISMPI26, handle: LSHandle):
        self._owner = owner
        self._w = owner._w
        self._handle = LSHandle(handle.value)
        self._closed = False

    @property
    def handle(self) -> int:
        return int(self._handle.value or 0)

    @property
    def closed(self) -> bool:
        return self._closed

    def _require_open(self) -> None:
        if self._closed or not self._handle.value:
            raise RuntimeError("The USB LISM-PI26xx device handle is closed")

    def _call(self, name: str, *args) -> int:
        self._require_open()
        return int(getattr(self._w, name)(self._handle, *args))

    def _get(self, name: str, ctype, addr: int, timeout_ms: int):
        value = ctype(0)
        result = self._call(name, UInt8(addr), _ct.byref(value), UInt32(timeout_ms))
        return result, int(value.value)

    def close(self) -> int:
        if self._closed:
            return RET_OK
        result = int(self._w.ls_closedevice_ex(self._handle))
        if result == RET_OK:
            self._closed = True
            self._handle = LSHandle()
        return result

    def __enter__(self):
        self._require_open()
        return self

    def __exit__(self, exc_type, exc_value, traceback):
        self.close()
        return False

    # --- Data transfer ---
    def seteptimeout(self, timeout_ms: int) -> int:
        return self._call("ls_seteptimeout_ex", UInt32(timeout_ms))

    def geteptimeout(self) -> Tuple[int, int]:
        value = UInt32(0)
        result = self._call("ls_geteptimeout_ex", _ct.byref(value))
        return result, int(value.value)

    def setthreadpriority(self, priority: int) -> int:
        return self._call("ls_setthreadpriority_ex", Int32(priority))

    def getthreadpriority(self) -> Tuple[int, int]:
        value = Int32(LS_THREAD_PRIORITY_NORMAL)
        result = self._call("ls_getthreadpriority_ex", _ct.byref(value))
        return result, int(value.value)

    def waitforpipe(self, timeout_ms: int) -> int:
        return self._call("ls_waitforpipe_ex", UInt32(timeout_ms))

    def waitforpipecount(self, count: int, timeout_ms: int) -> Tuple[int, int]:
        available = Int32(0)
        result = self._call("ls_waitforpipecount_ex", Int32(count), _ct.byref(available), UInt32(timeout_ms))
        return result, int(available.value)

    def getpipe(self, nbytes: int) -> Tuple[int, bytes]:
        if nbytes < 0:
            raise ValueError("nbytes must not be negative")
        buffer = (UInt8 * nbytes)()
        read_count = UInt32(0)
        result = self._call("ls_getpipe_ex", _ct.cast(buffer, PVOID) if nbytes else PVOID(), UInt32(nbytes), _ct.byref(read_count))
        return result, bytes(buffer[: read_count.value])

    def getpipe_available_result(self) -> Tuple[int, int]:
        available = UInt32(0)
        result = self._call("ls_getpipe_ex", PVOID(), UInt32(0), _ct.byref(available))
        return result, int(available.value)

    def getpipe_available(self) -> int:
        return self.getpipe_available_result()[1]

    def getfps(self) -> int:
        self._require_open()
        return int(self._w.ls_getfps_ex(self._handle))

    def getpacketlength(self, timeout_ms: int = DEFAULT_TIMEOUT_MS) -> Tuple[int, int]:
        value = UInt32(0)
        result = self._call("ls_getpacketlength_ex", _ct.byref(value), UInt32(timeout_ms))
        return result, int(value.value)

    def customfirmware(self, timeout_ms: int = DEFAULT_TIMEOUT_MS) -> Tuple[int, int]:
        value = UInt32(0)
        result = self._call("ls_customfirmware_ex", _ct.byref(value), UInt32(timeout_ms))
        return result, int(value.value)

    def setcfg1(self, cfg1: int, timeout_ms: int = DEFAULT_TIMEOUT_MS) -> int:
        return self._call("ls_setcfg1_ex", UInt8(cfg1), UInt32(timeout_ms))

    def getcfg1(self, timeout_ms: int = DEFAULT_TIMEOUT_MS) -> Tuple[int, int]:
        value = UInt8(0)
        result = self._call("ls_getcfg1_ex", _ct.byref(value), UInt32(timeout_ms))
        return result, int(value.value)

    # --- I2C ---
    def setslaveaddress(self, addr: int, new_addr: int, timeout_ms: int = DEFAULT_TIMEOUT_MS) -> int:
        return self._call("ls_setslaveaddress_ex", UInt8(addr), UInt8(new_addr), UInt32(timeout_ms))

    def getslaveaddress(self, addr: int, timeout_ms: int = DEFAULT_TIMEOUT_MS) -> Tuple[int, int]:
        return self._get("ls_getslaveaddress_ex", UInt8, addr, timeout_ms)

    def saveslaveaddress(self, addr: int, timeout_ms: int = DEFAULT_TIMEOUT_MS) -> int:
        return self._call("ls_saveslaveaddress_ex", UInt8(addr), UInt32(timeout_ms))

    def setmuxchannel(self, channel: int, timeout_ms: int = DEFAULT_TIMEOUT_MS) -> int:
        return self._call("ls_setmuxchannel_ex", UInt8(channel), UInt32(timeout_ms))

    def getmuxchannel(self, timeout_ms: int = DEFAULT_TIMEOUT_MS) -> Tuple[int, int]:
        value = Int8(0)
        result = self._call("ls_getmuxchannel_ex", _ct.byref(value), UInt32(timeout_ms))
        return result, int(value.value)

    def _read_i2c(self, name: str, address: int, length: int, timeout_ms: int) -> Tuple[int, bytes, int]:
        _validate_u16_length(length)
        buffer = (UInt8 * length)()
        actual = UInt16(length)
        result = self._call(name, UInt8(address), _ct.cast(buffer, PVOID) if length else PVOID(), _ct.byref(actual), UInt32(timeout_ms))
        return result, bytes(buffer[: actual.value]), int(actual.value)

    def _write_i2c(self, name: str, address: int, data: Union[bytes, bytearray, memoryview], timeout_ms: int) -> int:
        raw, buffer, pointer = _buffer_from_bytes(data)
        _validate_u16_length(len(raw))
        return self._call(name, UInt8(address), pointer, UInt16(len(raw)), UInt32(timeout_ms))

    def readi2c(self, address: int, length: int, timeout_ms: int = DEFAULT_TIMEOUT_MS) -> Tuple[int, bytes, int]:
        return self._read_i2c("ls_readi2c_ex", address, length, timeout_ms)

    def writei2c(self, address: int, data: Union[bytes, bytearray, memoryview], timeout_ms: int = DEFAULT_TIMEOUT_MS) -> int:
        return self._write_i2c("ls_writei2c_ex", address, data, timeout_ms)

    def readi2c_ext(self, address: int, length: int, timeout_ms: int = DEFAULT_TIMEOUT_MS) -> Tuple[int, bytes, int]:
        return self._read_i2c("ls_readi2c_ext_ex", address, length, timeout_ms)

    def writei2c_ext(self, address: int, data: Union[bytes, bytearray, memoryview], timeout_ms: int = DEFAULT_TIMEOUT_MS) -> int:
        return self._write_i2c("ls_writei2c_ext_ex", address, data, timeout_ms)

    def i2cread1byte(self, address: int, register: int, timeout_ms: int = DEFAULT_TIMEOUT_MS) -> Tuple[int, int]:
        value = UInt8(0)
        result = self._call("ls_i2cread1byte_ex", UInt8(address), UInt8(register), _ct.byref(value), UInt32(timeout_ms))
        return result, int(value.value)

    def i2cread2bytes(self, address: int, register: int, timeout_ms: int = DEFAULT_TIMEOUT_MS) -> Tuple[int, int]:
        value = UInt16(0)
        result = self._call("ls_i2cread2bytes_ex", UInt8(address), UInt8(register), _ct.byref(value), UInt32(timeout_ms))
        return result, int(value.value)

    def i2cread4bytes(self, address: int, register: int, timeout_ms: int = DEFAULT_TIMEOUT_MS) -> Tuple[int, int]:
        value = UInt32(0)
        result = self._call("ls_i2cread4bytes_ex", UInt8(address), UInt8(register), _ct.byref(value), UInt32(timeout_ms))
        return result, int(value.value)

    def i2cwrite1byte(self, address: int, register: int, value: int, timeout_ms: int = DEFAULT_TIMEOUT_MS) -> int:
        return self._call("ls_i2cwrite1byte_ex", UInt8(address), UInt8(register), UInt8(value), UInt32(timeout_ms))

    def i2cwrite2bytes(self, address: int, register: int, value: int, timeout_ms: int = DEFAULT_TIMEOUT_MS) -> int:
        return self._call("ls_i2cwrite2bytes_ex", UInt8(address), UInt8(register), UInt16(value), UInt32(timeout_ms))

    def i2cwrite4bytes(self, address: int, register: int, value: int, timeout_ms: int = DEFAULT_TIMEOUT_MS) -> int:
        return self._call("ls_i2cwrite4bytes_ex", UInt8(address), UInt8(register), UInt32(value), UInt32(timeout_ms))

    def i2cwritecmd(self, address: int, command: int, timeout_ms: int = DEFAULT_TIMEOUT_MS) -> int:
        return self._call("ls_i2cwritecmd_ex", UInt8(address), UInt8(command), UInt32(timeout_ms))

    # --- CTG/configuration setters ---
    def hw_reset(self, addr: int, timeout_ms: int = DEFAULT_TIMEOUT_MS) -> int:
        return self._call("ls_hw_reset_ex", UInt8(addr), UInt32(timeout_ms))

    def resume(self, addr: int, timeout_ms: int = DEFAULT_TIMEOUT_MS) -> int:
        return self._call("ls_resume_ex", UInt8(addr), UInt32(timeout_ms))

    def suspend(self, addr: int, timeout_ms: int = DEFAULT_TIMEOUT_MS) -> int:
        return self._call("ls_suspend_ex", UInt8(addr), UInt32(timeout_ms))

    def updateparam(self, addr: int, timeout_ms: int = DEFAULT_TIMEOUT_MS) -> int:
        return self._call("ls_updateparam_ex", UInt8(addr), UInt32(timeout_ms))

    def setstate(self, addr: int, state: int, timeout_ms: int = DEFAULT_TIMEOUT_MS) -> int:
        return self._call("ls_setstate_ex", UInt8(addr), UInt8(state), UInt32(timeout_ms))

    def setmodeconfig(self, addr: int, mode: int, timeout_ms: int = DEFAULT_TIMEOUT_MS) -> int:
        return self._call("ls_setmodeconfig_ex", UInt8(addr), UInt8(mode), UInt32(timeout_ms))

    def setifconfig(self, addr: int, config: int, timeout_ms: int = DEFAULT_TIMEOUT_MS) -> int:
        return self._call("ls_setifconfig_ex", UInt8(addr), UInt8(config), UInt32(timeout_ms))

    def setsthigh(self, addr: int, high: int, timeout_ms: int = DEFAULT_TIMEOUT_MS) -> int:
        return self._call("ls_setsthigh_ex", UInt8(addr), UInt32(high), UInt32(timeout_ms))

    def setstlow(self, addr: int, low: int, timeout_ms: int = DEFAULT_TIMEOUT_MS) -> int:
        return self._call("ls_setstlow_ex", UInt8(addr), UInt32(low), UInt32(timeout_ms))

    def setstpulse(self, addr: int, high: int, low: int, timeout_ms: int = DEFAULT_TIMEOUT_MS) -> int:
        return self._call("ls_setstpulse_ex", UInt8(addr), UInt32(high), UInt32(low), UInt32(timeout_ms))

    def setlinesperframe(self, addr: int, lines: int, timeout_ms: int = DEFAULT_TIMEOUT_MS) -> int:
        return self._call("ls_setlinesperframe_ex", UInt8(addr), UInt16(lines), UInt32(timeout_ms))

    def setquadcount(self, addr: int, count: int, timeout_ms: int = DEFAULT_TIMEOUT_MS) -> int:
        return self._call("ls_setquadcount_ex", UInt8(addr), UInt32(count), UInt32(timeout_ms))

    def setsofttriggertime(self, addr: int, time_value: int, timeout_ms: int = DEFAULT_TIMEOUT_MS) -> int:
        return self._call("ls_setsofttriggertime_ex", UInt8(addr), UInt32(time_value), UInt32(timeout_ms))

    def settriggerdelay(self, addr: int, delay: int, timeout_ms: int = DEFAULT_TIMEOUT_MS) -> int:
        return self._call("ls_settriggerdelay_ex", UInt8(addr), UInt32(delay), UInt32(timeout_ms))

    def settriggerwidth(self, addr: int, width: int, timeout_ms: int = DEFAULT_TIMEOUT_MS) -> int:
        return self._call("ls_settriggerwidth_ex", UInt8(addr), UInt32(width), UInt32(timeout_ms))

    def setpixelcount(self, addr: int, count: int, timeout_ms: int = DEFAULT_TIMEOUT_MS) -> int:
        return self._call("ls_setpixelcount_ex", UInt8(addr), UInt16(count), UInt32(timeout_ms))

    def setedgedelay(self, addr: int, edges: int, timeout_ms: int = DEFAULT_TIMEOUT_MS) -> int:
        return self._call("ls_setedgedelay_ex", UInt8(addr), UInt8(edges), UInt32(timeout_ms))

    def setadcvref(self, addr: int, vref: int, timeout_ms: int = DEFAULT_TIMEOUT_MS) -> int:
        return self._call("ls_setadcvref_ex", UInt8(addr), UInt8(vref), UInt32(timeout_ms))

    def setadcgain(self, addr: int, gain: int, timeout_ms: int = DEFAULT_TIMEOUT_MS) -> int:
        return self._call("ls_setadcgain_ex", UInt8(addr), UInt8(gain), UInt32(timeout_ms))

    def setadcoffset(self, addr: int, offset: int, timeout_ms: int = DEFAULT_TIMEOUT_MS) -> int:
        return self._call("ls_setadcoffset_ex", UInt8(addr), UInt16(offset), UInt32(timeout_ms))

    def setadcbias(self, addr: int, bias: int, timeout_ms: int = DEFAULT_TIMEOUT_MS) -> int:
        return self._call("ls_setadcbias_ex", UInt8(addr), UInt16(bias), UInt32(timeout_ms))

    def savesettings(self, addr: int, timeout_ms: int = DEFAULT_TIMEOUT_MS) -> int:
        return self._call("ls_savesettings_ex", UInt8(addr), UInt32(timeout_ms))

    def reloadsettings(self, addr: int, timeout_ms: int = DEFAULT_TIMEOUT_MS) -> int:
        return self._call("ls_reloadsettings_ex", UInt8(addr), UInt32(timeout_ms))

    def resetsettings(self, addr: int, timeout_ms: int = DEFAULT_TIMEOUT_MS) -> int:
        return self._call("ls_resetsettings_ex", UInt8(addr), UInt32(timeout_ms))

    # --- CTG/configuration getters ---
    def getctgstate(self, addr: int, timeout_ms: int = DEFAULT_TIMEOUT_MS):
        return self._get("ls_getctgstate_ex", UInt8, addr, timeout_ms)

    def getstate(self, addr: int, timeout_ms: int = DEFAULT_TIMEOUT_MS):
        return self._get("ls_getstate_ex", UInt8, addr, timeout_ms)

    def getmodeconfig(self, addr: int, timeout_ms: int = DEFAULT_TIMEOUT_MS):
        return self._get("ls_getmodeconfig_ex", UInt8, addr, timeout_ms)

    def getifconfig(self, addr: int, timeout_ms: int = DEFAULT_TIMEOUT_MS):
        return self._get("ls_getifconfig_ex", UInt8, addr, timeout_ms)

    def getsthigh(self, addr: int, timeout_ms: int = DEFAULT_TIMEOUT_MS):
        return self._get("ls_getsthigh_ex", UInt32, addr, timeout_ms)

    def getstlow(self, addr: int, timeout_ms: int = DEFAULT_TIMEOUT_MS):
        return self._get("ls_getstlow_ex", UInt32, addr, timeout_ms)

    def getstpulse(self, addr: int, timeout_ms: int = DEFAULT_TIMEOUT_MS):
        high = UInt32(0)
        low = UInt32(0)
        result = self._call("ls_getstpulse_ex", UInt8(addr), _ct.byref(high), _ct.byref(low), UInt32(timeout_ms))
        return result, int(high.value), int(low.value)

    def getlinesperframe(self, addr: int, timeout_ms: int = DEFAULT_TIMEOUT_MS):
        return self._get("ls_getlinesperframe_ex", UInt16, addr, timeout_ms)

    def getquadcount(self, addr: int, timeout_ms: int = DEFAULT_TIMEOUT_MS):
        return self._get("ls_getquadcount_ex", UInt32, addr, timeout_ms)

    def getsofttriggertime(self, addr: int, timeout_ms: int = DEFAULT_TIMEOUT_MS):
        return self._get("ls_getsofttriggertime_ex", UInt32, addr, timeout_ms)

    def gettriggerdelay(self, addr: int, timeout_ms: int = DEFAULT_TIMEOUT_MS):
        return self._get("ls_gettriggerdelay_ex", UInt32, addr, timeout_ms)

    def gettriggerwidth(self, addr: int, timeout_ms: int = DEFAULT_TIMEOUT_MS):
        return self._get("ls_gettriggerwidth_ex", UInt32, addr, timeout_ms)

    def getpixelcount(self, addr: int, timeout_ms: int = DEFAULT_TIMEOUT_MS):
        return self._get("ls_getpixelcount_ex", UInt16, addr, timeout_ms)

    def getedgedelay(self, addr: int, timeout_ms: int = DEFAULT_TIMEOUT_MS):
        return self._get("ls_getedgedelay_ex", UInt8, addr, timeout_ms)

    def getadcvref(self, addr: int, timeout_ms: int = DEFAULT_TIMEOUT_MS):
        return self._get("ls_getadcvref_ex", UInt8, addr, timeout_ms)

    def getadcgain(self, addr: int, timeout_ms: int = DEFAULT_TIMEOUT_MS):
        return self._get("ls_getadcgain_ex", UInt8, addr, timeout_ms)

    def getgainmult(self, addr: int, timeout_ms: int = DEFAULT_TIMEOUT_MS):
        return self._get("ls_getgainmult_ex", UInt16, addr, timeout_ms)

    def getgaindiv(self, addr: int, timeout_ms: int = DEFAULT_TIMEOUT_MS):
        return self._get("ls_getgaindiv_ex", UInt16, addr, timeout_ms)

    def getadcoffset(self, addr: int, timeout_ms: int = DEFAULT_TIMEOUT_MS):
        return self._get("ls_getadcoffset_ex", UInt16, addr, timeout_ms)

    def getadcbias(self, addr: int, timeout_ms: int = DEFAULT_TIMEOUT_MS):
        return self._get("ls_getadcbias_ex", UInt16, addr, timeout_ms)

    def getinitstatus(self, addr: int, timeout_ms: int = DEFAULT_TIMEOUT_MS):
        return self._get("ls_getinitstatus_ex", UInt8, addr, timeout_ms)

    def getcomresult(self, addr: int, timeout_ms: int = DEFAULT_TIMEOUT_MS):
        return self._get("ls_getcomresult_ex", UInt8, addr, timeout_ms)

    def getmcuversion(self, addr: int, timeout_ms: int = DEFAULT_TIMEOUT_MS):
        return self._get("ls_getmcuversion_ex", UInt16, addr, timeout_ms)

    def getctgversion(self, addr: int, timeout_ms: int = DEFAULT_TIMEOUT_MS):
        return self._get("ls_getctgversion_ex", UInt16, addr, timeout_ms)

    def gethw1id(self, addr: int, timeout_ms: int = DEFAULT_TIMEOUT_MS):
        return self._get("ls_gethw1id_ex", UInt16, addr, timeout_ms)

    def gethw1version(self, addr: int, timeout_ms: int = DEFAULT_TIMEOUT_MS):
        return self._get("ls_gethw1version_ex", UInt16, addr, timeout_ms)

    def gethw2version(self, addr: int, timeout_ms: int = DEFAULT_TIMEOUT_MS):
        return self._get("ls_gethw2version_ex", UInt16, addr, timeout_ms)


__all__ = [
    "USBLISMPI26",
    "USBLISMPI26Device",
    "USBLISMPI26LibraryError",
    "default_library_name",
    # BEGIN GENERATED LS ERROR EXPORTS
    "LS_OK",
    "LS_ERR_NO_DEVICES_ATTACHED",
    "LS_ERR_DEVICE_INDEX_OUT_OF_RANGE",
    "LS_ERR_SERIAL_NUM_NOT_FOUND",
    "LS_ERR_INVALID_HANDLE",
    "LS_ERR_DEVICE_ACTIVE",
    "LS_ERR_CONTROL_TRANSFER",
    "LS_ERR_TIMEOUT",
    "LS_ERR_DEVICE_ALREADY_OPEN",
    "LS_ERR_NOT_IMPLEMENTED",
    "LS_ERR_ALLOCATE_THREADPARAM_MEM",
    "LS_ERR_PATH_INVALID_HANDLE",
    "LS_ERR_INVALID_DATALEN",
    "LS_ERR_I2C_NACK",
    "LS_ERR_I2C_ERROR",
    "LS_ERR_I2C_UNKNOWN",
    "LS_ERR_I2C_MUX_DISABLED",
    "LS_ERR_INVALID_PACKETLENGTH",
    "LS_ERR_CMD_NOT_SUPPORTED",
    "LS_ERR_PIPE_CANCELLED",
    "LS_ERR_INVALID_TIMEOUT",
    "LS_ERR_INVALID_THREAD_PRIORITY",
    "LS_ERR_THREAD_PRIORITY_FAILED",
    # END GENERATED LS ERROR EXPORTS
    "LS_THREAD_PRIORITY_NORMAL",
    "LS_THREAD_PRIORITY_ABOVE_NORMAL",
    "LS_THREAD_PRIORITY_HIGH",
    "RET_OK",
    "LISM_ADDR",
    "DEFAULT_TIMEOUT_MS",
    "MODE_FREE_RUNNING",
    "MODE_EXT_RISING_EDGE_SL",
    "MODE_EXT_HIGH_LEVEL",
    "MODE_INT_SOFT_TRIGGER",
    "MODE_QUAD_ENC_TRIGGER",
    "MODE_EXT_RISING_EDGE_ML",
    "MODE_ENCODER_DIR_CW",
    "MODE_ENCODER_DIR_CCW",
    "MODE_ENCODER_CNT_DISABLE",
    "MODE_ENCODER_CNT_ENABLE",
    "IF_DCLK_400KHZ",
    "IF_DCLK_500KHZ",
    "IF_DCLK_800KHZ",
    "IF_DCLK_1MHZ",
    "IF_DCLK_2MHZ",
    "IF_DCLK_4MHZ",
    "IF_DCLK_10MHZ",
    "IF_DCLK_20MHZ",
    "IF_DCLK_POL_RISING_EDGE",
    "IF_DCLK_POL_FALLING_EDGE",
    "IF_LINE_VALID_HIGH",
    "IF_LINE_VALID_LOW",
    "IF_FRAME_VALID_HIGH",
    "IF_FRAME_VALID_LOW",
    "IF_TRIGGER_OUTPUT_HIGH",
    "IF_TRIGGER_OUTPUT_LOW",
    "ADC_VREF_1V6",
    "ADC_VREF_2V0",
]
