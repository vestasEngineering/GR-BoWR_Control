from __future__ import annotations
from dataclasses import dataclass
from typing import Optional
import numpy as np

@dataclass(frozen=True)
class DeviceSignature:
    serial: str
    pixel_count: int
    packet_length: int
    st_high: int
    st_low: int
    edge_delay: int
    adc_gain: int
    adc_bias: int
    adc_offset: int
    def as_dict(self) -> dict:
        return self.__dict__.copy()

@dataclass(frozen=True)
class Detection:
    detected: bool
    peak_pixel: Optional[int]
    centroid: Optional[float]
    amplitude: float
    threshold: float
    snr: float
    low_clipped: bool
    high_clipped: bool
    region_start: Optional[int]
    region_end: Optional[int]
    region_width: int
    integrated_signal: float
    polarity: str
    corrected: np.ndarray
    @property
    def saturated(self) -> bool:
        return self.low_clipped or self.high_clipped
