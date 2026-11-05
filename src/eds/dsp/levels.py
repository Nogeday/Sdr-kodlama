"""Time-domain IQ level statistics (peak, RMS, clipping, DC) in dBFS.

Conventions (docs/ARCHITECTURE.md section 2): full scale = 1.0 (``I = counts / 2048``);
0 dBFS = power of a full-scale complex exponential (``mean(|x|^2) = 1``); a sample is
clipped when ``max(|I|, |Q|) >= clip_level`` (≈ 2047/2048).

The *peak* is the per-rail peak ``max(|I|, |Q|)``, because that is what the ADC clips —
not ``|x|`` (a full-scale complex exponential has ``|x| = 1`` but rails at 1 only at the
axis crossings). Headroom to clipping is therefore ``-peak_dbfs``.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass
from typing import Any

import numpy as np

from ..core.errors import ConfigError
from ..core.units import db10, db20

DEFAULT_CLIP_LEVEL: float = 2047.0 / 2048.0


@dataclass(frozen=True)
class IQLevels:
    """Level summary of a block of IQ samples (all channels aggregated).

    peak_dbfs      ``20 log10(max over samples/channels of max(|I|, |Q|))``
    rms_dbfs       ``10 log10(mean |x|^2)`` over all channels
    clip_fraction  fraction of complex samples with ``|I|`` or ``|Q| >= clip_level``
    dc_dbfs        ``20 log10 |mean(x)|`` — worst (largest) channel
    n              total number of complex samples analysed (all channels)
    """

    peak_dbfs: float
    rms_dbfs: float
    clip_fraction: float
    dc_dbfs: float
    n: int

    @property
    def headroom_db(self) -> float:
        """dB between the per-rail peak and full scale (positive = room left)."""
        return -self.peak_dbfs

    @property
    def crest_db(self) -> float:
        """Peak-to-RMS ratio (dB)."""
        return self.peak_dbfs - self.rms_dbfs

    def to_dict(self) -> dict[str, Any]:
        return {k: (int(v) if k == "n" else float(v)) for k, v in asdict(self).items()}


def iq_levels(x: np.ndarray, clip_level: float = DEFAULT_CLIP_LEVEL) -> IQLevels:
    """Peak / RMS / clip fraction / DC of ``x`` (shape ``(n,)`` or ``(n_ch, n)``).

    Real input is treated as ``I`` only (``Q = 0``). Accumulation is in float64.
    """
    a = np.asarray(x)
    if a.ndim == 1:
        a = a[None, :]
    if a.ndim != 2:
        raise ConfigError(f"IQ dizisi (n,) ya da (n_ch, n) şeklinde olmalı, {a.shape} geldi")
    if a.size == 0:
        raise ConfigError("IQ dizisi boş")
    cl = float(clip_level)
    if not cl > 0.0:
        raise ConfigError(f"Kırpma seviyesi pozitif olmalı, {clip_level!r} geldi")
    re = np.abs(a.real)
    im = np.abs(a.imag) if np.iscomplexobj(a) else np.zeros_like(re)
    peak = float(max(np.max(re), np.max(im)))
    power = float(np.mean(re.astype(np.float64) ** 2 + im.astype(np.float64) ** 2))
    clipped = int(np.count_nonzero((re >= cl) | (im >= cl)))
    dc = float(np.max(np.abs(np.mean(a, axis=1, dtype=np.complex128 if np.iscomplexobj(a) else np.float64))))
    return IQLevels(
        peak_dbfs=float(db20(peak)),
        rms_dbfs=float(db10(power)),
        clip_fraction=clipped / a.size,
        dc_dbfs=float(db20(dc)),
        n=int(a.size),
    )
