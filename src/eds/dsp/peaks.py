"""Spectral peak search, parabolic interpolation and high-precision tone frequency.

* :func:`interp_peak` — 3-point parabolic interpolation on dB values (sub-bin offset and
  interpolated peak level).
* :func:`find_peaks` — strongest local maxima of a :class:`~eds.dsp.psd.Spectrum` with a
  minimum separation, interpolated frequencies.
* :func:`tone_frequency` — frequency of the strongest tone of a record with sub-Hz
  precision (window + zero-padded FFT + parabolic interpolation, refined by maximizing the
  windowed DTFT magnitude with a golden-section search). Used for PPM calibration
  (``eds.calib.ppm``) and the frequency-resolution / accuracy measurements.
"""

from __future__ import annotations

import math
from dataclasses import dataclass

import numpy as np

from ..core.errors import ConfigError, MeasurementError
from ..core.units import db10
from .psd import Spectrum, _as_1d
from .windows import get_window

#: Search half-width as a fraction of fs when only ``f_hint`` is given to tone_frequency.
DEFAULT_SEARCH_FRACTION: float = 0.1

_GOLDEN = (math.sqrt(5.0) - 1.0) / 2.0     # 0.618...


@dataclass(frozen=True)
class Peak:
    """A spectral peak: interpolated frequency/level, integer bin and sub-bin offset."""

    f_hz: float
    p_db: float
    bin: int
    delta: float


def interp_peak(p_db: np.ndarray, k: int) -> tuple[float, float]:
    """Parabolic interpolation through bins ``k-1, k, k+1`` of a dB spectrum.

    Returns ``(delta, peak_db)``: the vertex offset in bins (clamped to ``[-0.5, 0.5]``) and
    the parabola's value there. At the array edges, for non-finite neighbours, or when the
    three points are not a strict maximum (non-negative curvature) ``(0.0, p_db[k])`` is
    returned.
    """
    p = np.asarray(p_db)
    n = p.shape[0]
    k = int(k)
    if not 0 <= k < n:
        raise ConfigError(f"Bin indeksi {k} spektrum dışında (0..{n - 1})")
    b = float(p[k])
    if k == 0 or k == n - 1:
        return 0.0, b
    a, c = float(p[k - 1]), float(p[k + 1])
    if not (math.isfinite(a) and math.isfinite(b) and math.isfinite(c)):
        return 0.0, b
    curv = a - 2.0 * b + c
    if curv >= 0.0:
        return 0.0, b
    delta = 0.5 * (a - c) / curv
    delta = min(0.5, max(-0.5, delta))
    peak = b + 0.5 * (c - a) * delta + 0.5 * curv * delta * delta
    return float(delta), float(peak)


def find_peaks(spec: Spectrum, n: int = 5, min_sep_bins: int = 3, exclude_dc_bins: int = 0,
               threshold_db: float | None = None) -> list[Peak]:
    """The ``n`` strongest local maxima of ``spec.p_db``, power-descending.

    * A local maximum is strictly above its left neighbour and not below its right one
      (plateaus report their first bin); the spectrum ends count as maxima if they exceed
      their only neighbour.
    * Peaks closer than ``min_sep_bins`` to an already accepted (stronger) peak are skipped.
    * ``exclude_dc_bins > 0`` excludes bins with ``|k - k_DC| <= exclude_dc_bins``
      (zero-IF DC spike); ``0`` excludes nothing.
    * ``threshold_db`` (spectrum unit) drops weaker maxima.

    Frequencies and levels are parabolically interpolated (:func:`interp_peak`).
    """
    n = int(n)
    if n <= 0:
        return []
    if int(min_sep_bins) < 0 or int(exclude_dc_bins) < 0:
        raise ConfigError("min_sep_bins ve exclude_dc_bins negatif olamaz")
    p = spec.p_db
    nb = p.size
    cand = np.zeros(nb, dtype=bool)
    if nb == 1:
        cand[0] = True
    else:
        cand[1:-1] = (p[1:-1] > p[:-2]) & (p[1:-1] >= p[2:])
        cand[0] = p[0] > p[1]
        cand[-1] = p[-1] > p[-2]
    if exclude_dc_bins > 0:
        cand &= np.abs(spec.dc_offset_bins) > int(exclude_dc_bins)
    if threshold_db is not None:
        cand &= p >= float(threshold_db)
    idx = np.flatnonzero(cand)
    if idx.size == 0:
        return []
    order = idx[np.argsort(-p[idx], kind="stable")]
    sep = int(min_sep_bins)
    accepted: list[int] = []
    for k in order:
        k = int(k)
        if all(abs(k - a) >= sep for a in accepted):
            accepted.append(k)
            if len(accepted) >= n:
                break
    df = spec.df_hz
    peaks = []
    for k in accepted:
        delta, pk = interp_peak(p, k)
        peaks.append(Peak(f_hz=float(spec.f_hz[k] + delta * df), p_db=pk, bin=k, delta=delta))
    return peaks


def _next_pow2(n: int) -> int:
    return 1 << max(0, int(n - 1).bit_length())


def tone_frequency(x: np.ndarray, fs: float, fc: float = 0.0, f_hint: float | None = None,
                   search_hz: float | None = None, zero_pad: int = 4,
                   window: str = "blackmanharris") -> float:
    """Absolute frequency ``fc + f_bb`` (Hz) of the strongest tone in ``x``.

    1. ``x`` is windowed (``window``, default Blackman-Harris for -92 dB leakage from other
       components such as the zero-IF DC spike) and FFT-ed with zero padding to the next
       power of two >= ``zero_pad * len(x)``.
    2. The strongest local maximum inside the search region is located and parabolically
       interpolated on dB.
    3. The estimate is refined by maximizing ``|sum w x e^{-j 2 pi f n / fs}|`` with a
       golden-section search within +-1 padded bin (re-centred if the optimum lies on the
       bracket edge).

    Search region: ``f_hint`` (absolute Hz) +- ``search_hz``. Only ``f_hint`` given →
    half-width ``DEFAULT_SEARCH_FRACTION * fs``; only ``search_hz`` given → centred on
    ``fc``. Raises :class:`MeasurementError` if the region holds no spectral maximum.
    The precision approaches the Cramer-Rao bound (well below 0.05 Hz for 2**18 samples at
    1 MS/s and 20 dB SNR).
    """
    xs = _as_1d(x)
    fs = float(fs)
    if not (math.isfinite(fs) and fs > 0.0):
        raise ConfigError(f"fs pozitif olmalı, {fs!r} geldi")
    if isinstance(zero_pad, bool) or int(zero_pad) != zero_pad or int(zero_pad) < 1:
        raise ConfigError(f"zero_pad >= 1 tamsayı olmalı, {zero_pad!r} geldi")
    n = xs.size
    if n < 8:
        raise ConfigError(f"Ton frekansı için en az 8 örnek gerekli, {n} geldi")
    w = get_window(window, n)
    xw = xs * w
    m = _next_pow2(n * int(zero_pad))
    spec = np.fft.fft(xw, m)
    pw = spec.real ** 2 + spec.imag ** 2
    freqs = np.fft.fftfreq(m, d=1.0 / fs)

    region = np.ones(m, dtype=bool)
    if f_hint is not None or search_hz is not None:
        center = (float(f_hint) - float(fc)) if f_hint is not None else 0.0
        half = float(search_hz) if search_hz is not None else DEFAULT_SEARCH_FRACTION * fs
        if not (math.isfinite(center) and math.isfinite(half) and half > 0.0):
            raise ConfigError(f"Geçersiz arama penceresi: merkez {center!r} Hz, yarı genişlik {half!r} Hz")
        region = np.abs(freqs - center) <= half
    # local maxima on the circular padded spectrum
    is_max = (pw > np.roll(pw, 1)) & (pw >= np.roll(pw, -1)) & region
    cand = np.flatnonzero(is_max)
    if cand.size == 0:
        raise MeasurementError("Arama penceresinde ton (spektral tepe) bulunamadı")
    k = int(cand[np.argmax(pw[cand])])
    trio = db10(np.array([pw[(k - 1) % m], pw[k], pw[(k + 1) % m]]))
    delta, _ = interp_peak(trio, 1)
    bin_hz = fs / m
    f0 = float(freqs[k]) + delta * bin_hz

    # golden-section refinement of |DTFT|^2 within +-1 padded bin (re-centred if needed)
    tol = 1e-7 * fs / n
    for _ in range(4):
        mag2 = _local_dtft(xw, fs, f0, 1.5 * bin_hz)
        a, b = -bin_hz, bin_hz
        c = b - _GOLDEN * (b - a)
        d = a + _GOLDEN * (b - a)
        fc_val, fd_val = mag2(c), mag2(d)
        while b - a > tol:
            if fc_val > fd_val:
                b, d, fd_val = d, c, fc_val
                c = b - _GOLDEN * (b - a)
                fc_val = mag2(c)
            else:
                a, c, fc_val = c, d, fd_val
                d = a + _GOLDEN * (b - a)
                fd_val = mag2(d)
        delta_hz = 0.5 * (a + b)
        f0 += delta_hz
        if bin_hz - abs(delta_hz) > 2.0 * tol:
            break
    else:
        raise MeasurementError("Ton frekansı iyileştirmesi yakınsamadı (tek tepe değil)")
    return float(fc) + f0


def _local_dtft(xw: np.ndarray, fs: float, f0: float, max_offset_hz: float):
    """``delta -> |sum_n xw[n] e^{-j 2 pi (f0 + delta) t_n}|^2`` for ``|delta| <= max_offset_hz``.

    The time axis is centred (``t_n = (n - (N-1)/2) / fs``). After one exact downmix by
    ``f0`` the remaining phase ``2 pi delta t`` is bounded by ``theta = 2 pi max_offset_hz
    T/2``; ``e^{-j 2 pi delta t}`` is expanded in a Taylor series around 0 whose moment sums
    ``mu_m = sum y tau^m`` (``tau = t / (T/2)`` in ``[-1, 1]``) are precomputed, with enough
    terms for a truncation error below 1e-16 relative. Each evaluation is then O(terms)
    instead of O(N) transcendental functions, with the accuracy of the direct sum.
    """
    n = xw.size
    half_t = 0.5 * (n - 1) / fs
    t = (np.arange(n, dtype=np.float64) - 0.5 * (n - 1)) / fs
    y = xw * np.exp((-2j * np.pi * f0) * t)
    tau = t / half_t
    theta = 2.0 * np.pi * max_offset_hz * half_t
    terms = 1
    mag = 1.0
    while mag > 1e-17 or terms <= theta:
        mag *= theta / terms
        terms += 1
    moments = np.empty(terms, dtype=np.complex128)
    v = y.copy()
    for m in range(terms):
        moments[m] = v.sum()
        v *= tau
    fact = np.cumprod(np.concatenate(([1.0], np.arange(1, terms, dtype=np.float64))))
    powers = np.arange(terms)

    def mag2(delta: float) -> float:
        z = (-2j * np.pi * delta * half_t) ** powers / fact
        val = complex(np.dot(moments, z))
        return val.real * val.real + val.imag * val.imag

    return mag2
