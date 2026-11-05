"""Core data types shared by every subsystem."""

from __future__ import annotations

import math
from dataclasses import asdict, dataclass, field
from typing import Any

import numpy as np


@dataclass(slots=True)
class IQBlock:
    """A *contiguous* block of complex baseband samples from one RX device.

    Invariants (enforced by every HAL backend):

    * ``samples`` is ``complex64`` with shape ``(n_channels, n)`` — always 2-D, even for a
      single channel. Values are normalized so that ADC full scale == 1.0, i.e. for the
      12-bit AD9363 ``I = counts / 2048``. A sample is *clipped* when ``|I|`` or ``|Q|``
      reaches ``clip_level`` (≈ 2047/2048).
    * The block is gap-free. If the hardware dropped samples (USB overflow) *before* the
      first sample of this block, ``overflow`` is True. A HAL never returns a block with a
      gap inside it.
    * ``fc`` / ``fs`` are the **nominal** (commanded) values. PPM correction is applied by
      consumers via the calibration layer (see ``eds.calib.ppm``); ``meta['ppm_hw']`` tells
      whether the hardware already compensated the reference clock.
    * ``settling`` is True when any sample of the block may predate a retune / gain change
      becoming effective (stale kernel buffers, PLL lock). Measurements must discard such
      blocks; ``RxDevice.capture()`` does it automatically.
    """

    samples: np.ndarray
    fs: float
    fc: float
    t_ns: int
    index: int
    seq: int
    gain_db: tuple[float, ...]
    overflow: bool = False
    settling: bool = False
    clip_level: float = 2047.0 / 2048.0
    meta: dict[str, Any] = field(default_factory=dict)

    @property
    def n(self) -> int:
        return int(self.samples.shape[1])

    @property
    def n_channels(self) -> int:
        return int(self.samples.shape[0])

    @property
    def duration_s(self) -> float:
        return self.n / self.fs

    @property
    def t_end_ns(self) -> int:
        """Estimated host time of the sample *after* the last one."""
        return self.t_ns + int(round(self.duration_s * 1e9))

    def channel(self, ch: int = 0) -> np.ndarray:
        return self.samples[ch]


@dataclass(slots=True)
class StreamStats:
    """Counters maintained by a HAL backend (and aggregated by the RX streamer)."""

    blocks: int = 0                 # IQBlocks delivered
    samples: int = 0                # samples delivered per channel
    overflows: int = 0              # overflow *events* reported by driver/hardware
    timeouts: int = 0               # read timeouts
    discarded_samples: int = 0      # partial-block samples discarded around an overflow
    settling_blocks: int = 0        # blocks flagged settling (retune / gain change)
    host_drops: int = 0             # blocks dropped by the *host* queue (consumer too slow)
    start_mono: float = 0.0         # perf_counter() at stream start
    last_mono: float = 0.0          # perf_counter() of last delivered block

    @property
    def elapsed_s(self) -> float:
        return max(self.last_mono - self.start_mono, 0.0)

    @property
    def throughput_sps(self) -> float:
        e = self.elapsed_s
        return self.samples / e if e > 0 else 0.0

    def to_dict(self) -> dict[str, Any]:
        d = asdict(self)
        d["elapsed_s"] = self.elapsed_s
        d["throughput_sps"] = self.throughput_sps
        return d


@dataclass(slots=True)
class DeviceInfo:
    """Static identity of an opened device (goes into every report and status message)."""

    name: str                       # logical name from config, e.g. "rx0" (= bus message src)
    backend: str                    # "soapy" | "sim" | ...
    driver: str = ""                # Soapy driver key, e.g. "plutosdr"
    hardware: str = ""              # hardware key / model string
    serial: str = ""                # unique serial (calibration files are keyed by it)
    firmware: str = ""
    uri: str = ""                   # stream URI / args summary
    profile: str = ""               # device profile name
    extra: dict[str, str] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(slots=True)
class GainRange:
    min_db: float
    max_db: float
    step_db: float = 1.0

    def clamp(self, g: float) -> float:
        """Clamp to [min, max] and quantize to the step grid anchored at ``min_db``
        (round half up; the result is always on the grid and never above ``max_db``)."""
        g = min(max(float(g), self.min_db), self.max_db)
        if self.step_db > 0:
            k = math.floor((g - self.min_db) / self.step_db + 0.5)
            k_max = math.floor((self.max_db - self.min_db) / self.step_db + 1e-9)
            g = self.min_db + min(max(k, 0), k_max) * self.step_db
        return float(g)
