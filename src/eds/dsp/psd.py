"""Power spectra (spectrum-analyzer scaling) and the :class:`Spectrum` container.

Conventions (docs/ARCHITECTURE.md section 2 — mandatory):

* **Power-spectrum scaling**: ``P[k] = |sum_n w[n] x[n] e^{-j 2 pi k n / N}|^2 / (sum w)^2``.
  A complex exponential of amplitude ``A`` centred on bin ``k`` reads ``A^2`` (``0 dBFS``
  for ``|x| = 1``) in that bin, independent of the window. White noise of variance
  ``s2`` reads ``s2 * ENBW_bins / N`` per bin, i.e. the noise power in ``RBW``.
* ``RBW = ENBW_bins * fs / N``; density ``dB/Hz = P_dB - 10 log10(RBW)``.
* **Band power** ``sum P[k] / ENBW_bins`` over the bins of the band (independent of
  scalloping; use it for tone / channel power).
* **Frequency axis** after ``fftshift``: ``f[k] = fc + (k - N/2) fs / N`` (DC at index
  ``N/2``; for odd ``N`` the DC index is ``N // 2``).
"""

from __future__ import annotations

import copy as _copy
import logging
import math
from dataclasses import dataclass, field, replace
from typing import Any

import numpy as np
from numpy.lib.stride_tricks import sliding_window_view

from ..core.errors import ConfigError, MeasurementError
from ..core.types import IQBlock
from ..core.units import db10, lin10
from .windows import canonical_window_name, get_window, window_info

log = logging.getLogger(__name__)

AVG_MODES: tuple[str, ...] = ("mean", "max", "min", "median")

#: Defaults used by :attr:`Spectrum.noise_floor_db` when ``meta`` does not override them
#: (same values as ``DspConfig`` defaults).
DEFAULT_USABLE_FRACTION: float = 0.8
DEFAULT_DC_NOTCH_BINS: int = 3

NFFT_MIN: int = 64
NFFT_MAX: int = 2 ** 22

_CHUNK_ELEMENTS = 2 ** 20      # complex128 elements per FFT chunk (~16 MB)


@dataclass
class Spectrum:
    """One power spectrum (one channel), fftshift-ed, power-spectrum scaled.

    f_hz       absolute RF bin centres (Hz), ``fc + (k - N/2) fs / N`` for a full spectrum;
               a :meth:`slice` keeps a contiguous subset of bins
    p_lin      linear power per bin (power-spectrum scaling, ``unit`` linear, e.g. FS^2 or mW)
    fc, fs     centre frequency / sample rate (Hz) of the capture
    nfft       FFT length ``N`` (bin spacing ``fs / N``; not the number of bins after slicing)
    window     canonical window name; ``enbw_bins`` its ENBW in bins
    averages   number of averaged segments (Welch); see ``meta['overlap']``, ``meta['avg']``
    unit       ``"dBFS"`` (uncalibrated) or e.g. ``"dBm"`` after :meth:`with_offset_db`
    t_ns       host time of the first sample (``IQBlock.t_ns``)
    gain_db    RX gain(s) in effect for the analysed channel
    overflow   the source block followed a sample gap (driver overflow)
    meta       free-form provenance (``overlap``, ``hop``, ``avg``, ``segments``,
               ``n_samples``, ``zero_padded``; optional ``usable_fraction``,
               ``dc_notch_bins`` overriding the noise-floor defaults; ``ppm_applied``)
    """

    f_hz: np.ndarray
    p_lin: np.ndarray
    fc: float
    fs: float
    nfft: int
    window: str
    enbw_bins: float
    averages: int
    unit: str = "dBFS"
    t_ns: int = 0
    gain_db: tuple[float, ...] = ()
    overflow: bool = False
    meta: dict[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        self.f_hz = np.asarray(self.f_hz, dtype=np.float64)
        self.p_lin = np.asarray(self.p_lin, dtype=np.float64)
        if self.f_hz.ndim != 1 or self.f_hz.shape != self.p_lin.shape:
            raise ConfigError(
                f"Spektrum: f_hz ve p_lin aynı uzunlukta 1-B dizi olmalı ({self.f_hz.shape} / {self.p_lin.shape})")
        if self.f_hz.size == 0:
            raise ConfigError("Spektrum boş olamaz")
        self.fc = float(self.fc)
        self.fs = float(self.fs)
        if not (math.isfinite(self.fs) and self.fs > 0.0):
            raise ConfigError(f"Spektrum: fs pozitif olmalı, {self.fs!r} geldi")
        self.nfft = int(self.nfft)
        if self.nfft < 1:
            raise ConfigError(f"Spektrum: nfft pozitif olmalı, {self.nfft!r} geldi")
        self.enbw_bins = float(self.enbw_bins)
        if not (self.enbw_bins > 0.0):
            raise ConfigError(f"Spektrum: enbw_bins pozitif olmalı, {self.enbw_bins!r} geldi")
        self.averages = int(self.averages)
        self.t_ns = int(self.t_ns)
        self.gain_db = tuple(float(g) for g in self.gain_db)
        self.overflow = bool(self.overflow)

    # ------------------------------------------------------------------ derived quantities
    @property
    def n_bins(self) -> int:
        return int(self.p_lin.size)

    @property
    def df_hz(self) -> float:
        """Bin spacing ``fs / nfft`` (Hz)."""
        return self.fs / self.nfft

    @property
    def rbw_hz(self) -> float:
        """Resolution (noise-equivalent) bandwidth ``ENBW_bins * fs / nfft`` (Hz)."""
        return self.enbw_bins * self.fs / self.nfft

    @property
    def f_rel_hz(self) -> np.ndarray:
        """Bin centres relative to ``fc`` (baseband frequency, Hz)."""
        return self.f_hz - self.fc

    @property
    def dc_offset_bins(self) -> np.ndarray:
        """Integer bin offset of every bin from DC (``fc``); 0 = the DC bin."""
        return np.rint((self.f_hz - self.fc) / self.df_hz).astype(np.int64)

    @property
    def p_db(self) -> np.ndarray:
        """Power per bin in dB (``unit``)."""
        return db10(self.p_lin)

    def to_db(self) -> np.ndarray:
        """Same as :attr:`p_db`."""
        return self.p_db

    @property
    def density_db_hz(self) -> np.ndarray:
        """Power density ``P_dB - 10 log10(RBW)`` (``unit``/Hz)."""
        return self.p_db - 10.0 * math.log10(self.rbw_hz)

    def usable_bins(self) -> np.ndarray:
        """Bins inside the central usable fraction of ``fs`` and outside the DC notch.

        Fraction / notch come from ``meta['usable_fraction']`` / ``meta['dc_notch_bins']``
        when present, else :data:`DEFAULT_USABLE_FRACTION` / :data:`DEFAULT_DC_NOTCH_BINS`.
        """
        frac = float(self.meta.get("usable_fraction", DEFAULT_USABLE_FRACTION))
        notch = int(self.meta.get("dc_notch_bins", DEFAULT_DC_NOTCH_BINS))
        k = np.abs(self.dc_offset_bins)
        return (k <= math.floor(frac * self.nfft / 2.0)) & (k > notch)

    @property
    def noise_floor_db(self) -> float:
        """Median of :attr:`p_db` over the usable bins (DC +- notch excluded).

        The median is robust to signals; note that for exponential (single-average) noise
        it lies ``10 log10(ln 2) = -1.59 dB`` below the mean, the bias vanishing as the
        number of averages grows. Falls back to all bins if no bin is usable (narrow slice).
        """
        p = self.p_db
        mask = self.usable_bins()
        if np.any(mask):
            p = p[mask]
        return float(np.median(p))

    # ------------------------------------------------------------------ band operations
    def _band_mask(self, f_lo: float, f_hi: float) -> np.ndarray:
        f_lo, f_hi = float(f_lo), float(f_hi)
        if not (math.isfinite(f_lo) and math.isfinite(f_hi)) or f_hi < f_lo:
            raise ConfigError(f"Geçersiz bant: [{f_lo!r}, {f_hi!r}] Hz")
        mask = (self.f_hz >= f_lo) & (self.f_hz <= f_hi)
        if not np.any(mask):
            raise MeasurementError(
                f"[{f_lo:.6g}, {f_hi:.6g}] Hz bandında spektrum bini yok "
                f"(spektrum {self.f_hz[0]:.6g} … {self.f_hz[-1]:.6g} Hz, bin aralığı {self.df_hz:.6g} Hz)")
        return mask

    def band_power(self, f_lo: float, f_hi: float) -> float:
        """Linear power in ``[f_lo, f_hi]``: ``sum(P[k]) / ENBW_bins`` over bins whose centre
        lies in the band. Raises :class:`MeasurementError` if no bin centre is inside."""
        mask = self._band_mask(f_lo, f_hi)
        return float(np.sum(self.p_lin[mask]) / self.enbw_bins)

    def band_power_db(self, f_lo: float, f_hi: float) -> float:
        """:meth:`band_power` in dB (``unit``)."""
        return float(db10(self.band_power(f_lo, f_hi)))

    def slice(self, f_lo: float, f_hi: float) -> Spectrum:
        """Sub-spectrum with the bins whose centres lie in ``[f_lo, f_hi]`` (copies data)."""
        mask = self._band_mask(f_lo, f_hi)
        return replace(self, f_hz=self.f_hz[mask].copy(), p_lin=self.p_lin[mask].copy(),
                       meta=_copy.deepcopy(self.meta))

    def with_offset_db(self, offset_db: float | np.ndarray, unit: str | None = None) -> Spectrum:
        """New spectrum with ``P_dB + offset_db`` (scalar or per-bin array), e.g. dBFS→dBm.

        ``unit`` is the unit of the result (unchanged if ``None``)."""
        off = np.asarray(offset_db, dtype=np.float64)
        if off.ndim not in (0, 1) or (off.ndim == 1 and off.shape != self.p_lin.shape):
            raise ConfigError(f"Ofset skaler ya da {self.p_lin.shape} boyutunda olmalı, {off.shape} geldi")
        if not np.all(np.isfinite(off)):
            raise ConfigError("Ofset sonlu olmalı")
        return replace(self, f_hz=self.f_hz.copy(), p_lin=self.p_lin * lin10(off),
                       unit=self.unit if unit is None else str(unit), meta=_copy.deepcopy(self.meta))

    def with_freq_correction(self, ppm: float) -> Spectrum:
        """Apply the reference-clock correction ``f_true = f_app (1 + e)``, ``e = ppm 1e-6``.

        ``ppm`` follows the ARCHITECTURE sign convention (positive = TCXO fast). On AD936x
        LO *and* sample clock derive from the reference, so ``fc``, ``fs`` and every bin
        centre scale by ``(1 + e)``; the axis identity ``f = fc + (k - N/2) fs / N`` and the
        bin-to-DC mapping are preserved. ``meta['ppm_applied']`` accumulates the total.
        """
        ppm = float(ppm)
        if not math.isfinite(ppm):
            raise ConfigError(f"ppm sonlu olmalı, {ppm!r} geldi")
        s = 1.0 + ppm * 1e-6
        meta = _copy.deepcopy(self.meta)
        prev = float(meta.get("ppm_applied", 0.0))
        meta["ppm_applied"] = ((1.0 + prev * 1e-6) * s - 1.0) * 1e6
        return replace(self, f_hz=self.f_hz * s, p_lin=self.p_lin.copy(), fc=self.fc * s,
                       fs=self.fs * s, meta=meta)

    def copy(self) -> Spectrum:
        """Deep copy (arrays and meta)."""
        return replace(self, f_hz=self.f_hz.copy(), p_lin=self.p_lin.copy(), meta=_copy.deepcopy(self.meta))


# --------------------------------------------------------------------------- estimation
def _as_1d(x: np.ndarray) -> np.ndarray:
    a = np.asarray(x)
    if a.ndim == 2 and a.shape[0] == 1:
        a = a[0]
    if a.ndim != 1:
        raise ConfigError(f"Tek kanallı 1-B örnek dizisi bekleniyordu, şekil {a.shape} geldi")
    if a.size == 0:
        raise ConfigError("Örnek dizisi boş")
    if not (np.iscomplexobj(a) or np.issubdtype(a.dtype, np.floating)):
        a = a.astype(np.float64)
    if not np.all(np.isfinite(a)):
        raise MeasurementError("Örneklerde NaN/sonsuz değer var")
    return a


def _check_nfft(nfft: int) -> int:
    if isinstance(nfft, bool) or int(nfft) != nfft or int(nfft) < 2:
        raise ConfigError(f"nfft >= 2 tamsayı olmalı, {nfft!r} geldi")
    return int(nfft)


def bin_frequencies(fc: float, fs: float, nfft: int) -> np.ndarray:
    """fftshift-ed absolute bin centres ``fc + (k - N//2) fs / N``."""
    return float(fc) + (np.arange(nfft, dtype=np.float64) - (nfft // 2)) * (float(fs) / nfft)


def welch(x: np.ndarray, fs: float, nfft: int, window: str = "blackmanharris", overlap: float = 0.5,
          avg: str = "mean", fc: float = 0.0, t_ns: int = 0) -> Spectrum:
    """Welch power spectrum of complex (or real) samples ``x`` (shape ``(n,)`` or ``(1, n)``).

    Segments of ``nfft`` samples with ``hop = round(nfft (1 - overlap))`` (at least one);
    no detrending (DC is a signal on zero-IF receivers). Each segment's
    ``|FFT(w x)|^2 / (sum w)^2`` is combined with ``avg`` = ``mean | max | min | median``
    (float64 accumulation) and fftshift-ed. If ``len(x) < nfft`` the single segment is
    zero-padded (``meta['zero_padded'] = True``; a tone then reads ``len(x)/nfft``-weighted
    window energy, i.e. lower). ``averages`` = number of segments.
    """
    xs = _as_1d(x)
    nfft = _check_nfft(nfft)
    fs = float(fs)
    if not (math.isfinite(fs) and fs > 0.0):
        raise ConfigError(f"fs pozitif olmalı, {fs!r} geldi")
    ov = float(overlap)
    if not (0.0 <= ov < 1.0):
        raise ConfigError(f"Örtüşme [0, 1) aralığında olmalı, {overlap!r} geldi")
    if avg not in AVG_MODES:
        raise ConfigError(f"Ortalama türü {avg!r} geçersiz; seçenekler: {list(AVG_MODES)}")
    wname = canonical_window_name(window)
    w = get_window(wname, nfft)
    info = window_info(wname, nfft)
    scale = 1.0 / float(np.sum(w)) ** 2

    n = xs.size
    hop = max(1, int(round(nfft * (1.0 - ov))))
    zero_padded = n < nfft
    if zero_padded:
        buf = np.zeros(nfft, dtype=np.complex128 if np.iscomplexobj(xs) else np.float64)
        buf[:n] = xs
        segs = buf[None, :]
        n_seg = 1
    else:
        n_seg = 1 + (n - nfft) // hop
        segs = sliding_window_view(xs, nfft)[::hop][:n_seg]

    chunk = max(1, _CHUNK_ELEMENTS // nfft)
    init = {"mean": 0.0, "max": -np.inf, "min": np.inf, "median": 0.0}[avg]
    acc = np.full(nfft, init, dtype=np.float64)
    parts: list[np.ndarray] = []
    for start in range(0, n_seg, chunk):
        spec = np.fft.fft(segs[start:start + chunk] * w, axis=1)
        pw = spec.real ** 2 + spec.imag ** 2
        if avg == "mean":
            acc += np.sum(pw, axis=0, dtype=np.float64)
        elif avg == "max":
            np.maximum(acc, np.max(pw, axis=0), out=acc)
        elif avg == "min":
            np.minimum(acc, np.min(pw, axis=0), out=acc)
        else:
            parts.append(pw)
    if avg == "median":
        p = np.median(np.concatenate(parts, axis=0), axis=0)
    elif avg == "mean":
        p = acc / n_seg
    else:
        p = acc
    p = np.fft.fftshift(p * scale)

    meta: dict[str, Any] = {
        "overlap": ov, "hop": hop, "avg": avg, "segments": n_seg, "n_samples": int(n),
        "zero_padded": bool(zero_padded),
    }
    return Spectrum(f_hz=bin_frequencies(fc, fs, nfft), p_lin=p, fc=float(fc), fs=fs, nfft=nfft,
                    window=wname, enbw_bins=info.enbw_bins, averages=n_seg, t_ns=int(t_ns), meta=meta)


def spectrum_from_block(block: IQBlock, nfft: int, window: str = "blackmanharris", overlap: float = 0.5,
                        avg: str = "mean", channel: int = 0) -> Spectrum:
    """:func:`welch` of one channel of an :class:`IQBlock` (fc/fs/t_ns/gain/overflow carried).

    ``gain_db`` of the result holds the gain of the analysed channel only (the block's
    single value when it carries one gain for all channels). ``meta`` additionally records
    ``channel``, ``seq``, ``index``, ``settling``, ``clip_level`` and a copy of
    ``block.meta`` under ``block_meta``.
    """
    samples = np.asarray(block.samples)
    if samples.ndim != 2:
        raise ConfigError(f"IQBlock.samples (n_ch, n) şeklinde olmalı, {samples.shape} geldi")
    ch = int(channel)
    if not 0 <= ch < samples.shape[0]:
        raise ConfigError(f"Kanal {channel} yok (blokta {samples.shape[0]} kanal var)")
    spec = welch(samples[ch], block.fs, nfft, window, overlap, avg, fc=block.fc, t_ns=block.t_ns)
    gains = tuple(block.gain_db)
    if len(gains) > ch:
        spec.gain_db = (float(gains[ch]),)
    elif len(gains) == 1:
        spec.gain_db = (float(gains[0]),)
    else:
        spec.gain_db = tuple(float(g) for g in gains)
    spec.overflow = bool(block.overflow)
    spec.meta.update({
        "channel": ch, "seq": int(block.seq), "index": int(block.index), "settling": bool(block.settling),
        "clip_level": float(block.clip_level), "block_meta": _copy.deepcopy(dict(block.meta)),
    })
    return spec


def nfft_for_rbw(rbw_hz: float, fs: float, window: str = "blackmanharris") -> int:
    """Smallest power of two ``N`` with ``ENBW_bins * fs / N <= rbw_hz``, clamped to
    ``[NFFT_MIN, NFFT_MAX]`` (64 .. 2**22). A warning is logged when clamped at the top."""
    rbw_hz, fs = float(rbw_hz), float(fs)
    if not (math.isfinite(rbw_hz) and rbw_hz > 0.0):
        raise ConfigError(f"RBW pozitif olmalı, {rbw_hz!r} geldi")
    if not (math.isfinite(fs) and fs > 0.0):
        raise ConfigError(f"fs pozitif olmalı, {fs!r} geldi")
    wname = canonical_window_name(window)
    # ENBW of a periodic cosine window is independent of N for N > 2*(terms-1) (exact for
    # N >= NFFT_MIN), so the smallest admissible N follows directly.
    enbw = window_info(wname, NFFT_MIN).enbw_bins
    n = NFFT_MIN
    while n <= NFFT_MAX:
        if enbw * fs / n <= rbw_hz:
            return n
        n *= 2
    log.warning("İstenen RBW %.6g Hz, fs=%.6g Hz ile %d noktalı FFT'de bile sağlanamıyor; nfft=%d kullanılıyor",
                rbw_hz, fs, NFFT_MAX, NFFT_MAX)
    return NFFT_MAX
