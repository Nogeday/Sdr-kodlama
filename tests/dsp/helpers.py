"""Deterministic signal generators for the eds.dsp tests (full scale = 1.0, 0 dBFS = |x| = 1)."""

from __future__ import annotations

import numpy as np

from eds.dsp import Spectrum, bin_frequencies


def tone(n: int, f_bb_hz: float, fs: float, power_dbfs: float, phase: float = 0.0) -> np.ndarray:
    """Complex exponential of the given power (dBFS) at baseband frequency ``f_bb_hz``."""
    amp = 10.0 ** (power_dbfs / 20.0)
    k = np.arange(n, dtype=np.float64)
    return amp * np.exp(1j * (2.0 * np.pi * f_bb_hz * k / fs + phase))


def cnoise(rng: np.random.Generator, n: int, power: float) -> np.ndarray:
    """Circular complex white Gaussian noise with ``E|x|^2 = power``."""
    s = np.sqrt(power / 2.0)
    return s * (rng.standard_normal(n) + 1j * rng.standard_normal(n))


def synthetic_spectrum(p_lin: np.ndarray, fs: float = 1e6, fc: float = 0.0, window: str = "rect",
                       enbw_bins: float = 1.0, averages: int = 1, **meta) -> Spectrum:
    """Spectrum container around an arbitrary power array (full fftshift-ed axis)."""
    n = p_lin.size
    m = {"avg": "mean", "overlap": 0.0}
    m.update(meta)
    return Spectrum(f_hz=bin_frequencies(fc, fs, n), p_lin=np.asarray(p_lin, dtype=np.float64), fc=fc, fs=fs,
                    nfft=n, window=window, enbw_bins=enbw_bins, averages=averages, meta=m)
