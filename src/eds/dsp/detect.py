"""CFAR signal detection on power spectra.

Pipeline of :func:`detect`:

1. **Noise estimate per bin** from training cells (``train_bins`` on each side of the cell
   under test, ``guard_bins`` on each side skipped). Near the spectrum ends, where one side
   does not fit, a one-sided window of ``2 * train_bins`` cells on the other side is used
   (no wrap-around), so every bin has the same number ``M = 2 * train_bins`` of cells.

   * ``os_cfar``: rank-``k`` order statistic (``k = round(os_rank * M)``) divided by
     ``c_k`` = :func:`~eds.dsp.stats.os_cfar_scale` → estimate of the noise *mean*.
     Robust to signals and to the DC spike inside the training window (default).
   * ``ca_cfar``: arithmetic mean of the training cells.
   * ``fixed``: threshold = ``cfg.fixed_threshold`` (spectrum unit); the OS estimate is
     still computed for noise / SNR reporting.

2. **Multiplier** (exact finite-M correction, see :mod:`eds.dsp.stats`): noise bins are
   modelled as ``Gamma(n_eff, 1/n_eff)`` with ``n_eff`` the effective number of Welch
   averages. Neighbouring bins of a tapered window are correlated (Blackman-Harris:
   rho = 0.67, 0.19, 0.02 for 1, 2, 3 bins), which makes the training cells worth fewer
   independent cells. The **effective number of training cells**

       M_eff = M^2 / sum_{i,j} rho(|o_i - o_j|)

   (``o`` = cell offsets, ``rho`` = :func:`~eds.dsp.windows.bin_correlation`) is used in
   the finite-M correction (CA: directly; OS: rounded, with the same rank fraction). For a
   rectangular window (independent bins) ``M_eff = M`` and the false-alarm rate is exact.

3. ``threshold = max(alpha * noise, noise * 10^(min_snr_db/10))`` for every method.

4. Bins with ``P > threshold`` that are valid (``valid_mask`` and, if ``cfg.exclude_dc``,
   outside DC +- ``meta['dc_notch_bins']`` (default 3)) form contiguous runs; runs separated
   by ``<= merge_gap_bins`` non-detected bins are merged (the gap is included), runs
   shorter than ``min_bw_bins`` are dropped. Detections are returned in ascending
   frequency.

Default ``n_avg`` (when ``None``): :func:`default_n_avg` — the Welch effective averages
from ``spec.averages``, ``spec.meta['overlap']`` (0 if absent) and ``spec.window`` for
``avg == "mean"``; ``1`` for ``max``/``min``/``median`` averaging (exact for ``min`` of
independent segments, conservative for ``max``/``median``).
"""

from __future__ import annotations

import math
from dataclasses import asdict, dataclass
from functools import lru_cache
from typing import Any

import numpy as np
from numpy.lib.stride_tricks import sliding_window_view

from ..core.config import DetectConfig
from ..core.errors import ConfigError, MeasurementError
from ..core.units import db10, lin10
from .peaks import interp_peak
from .psd import DEFAULT_DC_NOTCH_BINS, Spectrum
from .stats import (
    ca_cfar_alpha,
    effective_averages,
    os_cfar_alpha,
    os_cfar_scale,
    os_rank_index,
)
from .windows import bin_correlation, canonical_window_name

METHODS: tuple[str, ...] = ("os_cfar", "ca_cfar", "fixed")


@dataclass
class Detection:
    """One detected signal (frequencies in Hz, levels in the spectrum's dB unit)."""

    f_center_hz: float      # power-weighted centroid of the run
    f_peak_hz: float        # interpolated frequency of the strongest bin
    f_lo_hz: float          # lower edge (first bin centre - df/2)
    f_hi_hz: float          # upper edge (last bin centre + df/2)
    bw_hz: float            # f_hi - f_lo
    peak_db: float          # interpolated peak level
    band_power_db: float    # sum P / ENBW_bins over the run
    snr_db: float           # peak_db - noise_db
    noise_db: float         # median noise estimate over the run
    n_bins: int

    def to_dict(self) -> dict[str, Any]:
        """JSON-safe dict (plain float / int values)."""
        return {k: (int(v) if k == "n_bins" else float(v)) for k, v in asdict(self).items()}


def usable_mask(n: int, usable_fraction: float, dc_notch_bins: int) -> np.ndarray:
    """Validity mask for an ``n``-bin fftshift-ed spectrum (DC at index ``n // 2``).

    True for bins with ``|k - n//2| <= floor(usable_fraction * n / 2)`` (central, flat,
    alias-free part of the band), False for the DC notch ``|k - n//2| <= dc_notch_bins``
    (``dc_notch_bins < 0`` disables the notch; ``0`` removes only the DC bin).
    """
    if isinstance(n, bool) or int(n) != n or int(n) < 1:
        raise ConfigError(f"Bin sayısı pozitif tamsayı olmalı, {n!r} geldi")
    frac = float(usable_fraction)
    if not (0.0 < frac <= 1.0):
        raise ConfigError(f"usable_fraction (0, 1] aralığında olmalı, {usable_fraction!r} geldi")
    n = int(n)
    k = np.abs(np.arange(n) - n // 2)
    mask = k <= math.floor(frac * n / 2.0)
    notch = int(dc_notch_bins)
    if notch >= 0:
        mask &= k > notch
    return mask


def default_n_avg(spec: Spectrum) -> float:
    """Effective number of independent averages of ``spec`` (see module docstring)."""
    if spec.meta.get("avg", "mean") != "mean":
        return 1.0
    n_seg = max(1, int(spec.averages))
    overlap = float(spec.meta.get("overlap", 0.0))
    return effective_averages(n_seg, overlap, spec.window, spec.nfft)


def effective_training_cells(window: str, nfft: int, guard_bins: int, train_bins: int) -> float:
    """``M_eff = M^2 / sum_{i,j} rho(|o_i - o_j|)`` for the two-sided training layout."""
    return _effective_training_cells_cached(canonical_window_name(window), int(nfft), int(guard_bins),
                                            int(train_bins))


@lru_cache(maxsize=256)
def _effective_training_cells_cached(window: str, nfft: int, g: int, t: int) -> float:
    offsets = np.concatenate((np.arange(-g - t, -g), np.arange(g + 1, g + t + 1)))
    m = offsets.size
    rho = bin_correlation(window, nfft, 2 * (g + t))
    lag = np.abs(offsets[:, None] - offsets[None, :])
    return float(m * m / np.sum(rho[lag]))


def _training_cells(p: np.ndarray, guard: int, train: int) -> np.ndarray:
    """``(n, 2*train)`` matrix of training cells per cell under test (see module doc)."""
    n = p.size
    m = 2 * train
    out = np.empty((n, m), dtype=p.dtype)
    v1 = sliding_window_view(p, train)
    v2 = sliding_window_view(p, m)
    i0 = guard + train                 # first CUT with a full left window
    i1 = n - 1 - guard - train         # last CUT with a full right window
    if i1 >= i0:
        cnt = i1 - i0 + 1
        out[i0:i1 + 1, :train] = v1[0:cnt]
        out[i0:i1 + 1, train:] = v1[i0 + guard + 1:i0 + guard + 1 + cnt]
    left_end = min(i0, n)              # right-only windows for CUT 0..left_end-1
    if left_end > 0:
        out[:left_end] = v2[guard + 1:guard + 1 + left_end]
    right_start = max(i1 + 1, left_end)  # left-only windows for CUT right_start..n-1
    if right_start < n:
        out[right_start:] = v2[right_start - guard - m:n - guard - m]
    return out


def _check_cfg(cfg: DetectConfig) -> None:
    if cfg.method not in METHODS:
        raise ConfigError(f"detect.method = {cfg.method!r} geçersiz; seçenekler: {list(METHODS)}")
    if not (0.0 < float(cfg.pfa) < 1.0):
        raise ConfigError(f"detect.pfa (0, 1) aralığında olmalı, {cfg.pfa!r} geldi")
    if int(cfg.guard_bins) < 0 or int(cfg.train_bins) < 1:
        raise ConfigError("detect.guard_bins >= 0 ve detect.train_bins >= 1 olmalı")
    if not (0.0 < float(cfg.os_rank) <= 1.0):
        raise ConfigError(f"detect.os_rank (0, 1] aralığında olmalı, {cfg.os_rank!r} geldi")
    if int(cfg.merge_gap_bins) < 0 or int(cfg.min_bw_bins) < 1:
        raise ConfigError("detect.merge_gap_bins >= 0 ve detect.min_bw_bins >= 1 olmalı")


@dataclass(frozen=True)
class NoiseEstimate:
    """Per-bin noise-mean estimate and the CFAR multiplier that goes with it."""

    noise_lin: np.ndarray    # estimated noise mean per bin (linear, spectrum unit)
    alpha: float             # CFAR multiplier on noise_lin (1.0 for method = fixed)
    n_eff: float             # effective averages used
    m_cells: int             # training cells per bin (M)
    m_eff: float             # effective independent training cells
    rank: int                # OS rank k (1-based; 0 for CA)


def estimate_noise(spec: Spectrum, cfg: DetectConfig, n_avg: float | None = None) -> NoiseEstimate:
    """CFAR noise-mean estimate per bin and the matching multiplier (see module doc)."""
    _check_cfg(cfg)
    n_eff = default_n_avg(spec) if n_avg is None else float(n_avg)
    if not (math.isfinite(n_eff) and n_eff > 0.0):
        raise ConfigError(f"n_avg pozitif olmalı, {n_avg!r} geldi")
    p = spec.p_lin
    if not np.all(np.isfinite(p)) or np.any(p < 0.0):
        raise MeasurementError("Spektrum güç değerleri sonlu ve negatif olmayan olmalı (NaN/inf/negatif var)")
    n = p.size
    guard = int(cfg.guard_bins)
    train = min(int(cfg.train_bins), (n - 2 * guard) // 3)
    if train < 1:
        raise ConfigError(
            f"Spektrum CFAR için çok kısa: {n} bin, guard={guard}, train={cfg.train_bins} "
            f"(en az {2 * guard + 3} bin gerekli)")
    m = 2 * train
    m_eff = effective_training_cells(spec.window, spec.nfft, guard, train)

    peak = float(np.max(p))
    scale = peak if peak > 0.0 else 1.0
    cells = _training_cells((p / scale).astype(np.float32), guard, train)

    method = cfg.method
    if method == "ca_cfar":
        noise = np.mean(cells, axis=1, dtype=np.float64) * scale
        alpha = ca_cfar_alpha(float(cfg.pfa), n_eff, m_eff)
        rank = 0
    else:
        rank = os_rank_index(float(cfg.os_rank), m)
        kth = rank - 1
        order_stat = np.partition(cells, kth, axis=1)[:, kth].astype(np.float64) * scale
        noise = order_stat / os_cfar_scale(n_eff, m, rank)
        if method == "os_cfar":
            m_os = max(1, int(round(m_eff)))
            k_os = min(m_os, max(1, int(round(rank / m * m_os))))
            alpha = os_cfar_alpha(float(cfg.pfa), n_eff, m_os, k_os)
        else:
            alpha = 1.0
    return NoiseEstimate(noise_lin=noise, alpha=float(alpha), n_eff=n_eff, m_cells=m, m_eff=m_eff, rank=rank)


def detect(spec: Spectrum, cfg: DetectConfig, n_avg: float | None = None,
           valid_mask: np.ndarray | None = None) -> tuple[list[Detection], np.ndarray]:
    """Detect signals in ``spec``. Returns ``(detections, threshold_db)``.

    ``threshold_db`` has one value per bin (spectrum dB unit). ``valid_mask`` (bool, one per
    bin; e.g. :func:`usable_mask`) restricts where detections may occur; the threshold is
    computed everywhere. See the module docstring for the algorithm.
    """
    est = estimate_noise(spec, cfg, n_avg)
    p = spec.p_lin
    n = p.size
    noise = est.noise_lin
    if cfg.method == "fixed":
        thr = np.full(n, float(lin10(float(cfg.fixed_threshold))))
    else:
        thr = est.alpha * noise
    thr = np.maximum(thr, noise * float(lin10(float(cfg.min_snr_db))))
    threshold_db = db10(thr)

    valid = np.ones(n, dtype=bool)
    if valid_mask is not None:
        vm = np.asarray(valid_mask)
        if vm.shape != (n,):
            raise ConfigError(f"valid_mask boyutu {vm.shape}, spektrum {n} bin")
        valid &= vm.astype(bool)
    if cfg.exclude_dc:
        notch = int(spec.meta.get("dc_notch_bins", DEFAULT_DC_NOTCH_BINS))
        if notch >= 0:
            valid &= np.abs(spec.dc_offset_bins) > notch

    hit = np.flatnonzero((p > thr) & valid)
    if hit.size == 0:
        return [], threshold_db
    breaks = np.flatnonzero(np.diff(hit) - 1 > int(cfg.merge_gap_bins))
    starts = hit[np.concatenate(([0], breaks + 1))]
    ends = hit[np.concatenate((breaks, [hit.size - 1]))]

    p_db = spec.p_db
    noise_db = db10(noise)
    df = spec.df_hz
    f = spec.f_hz
    dets: list[Detection] = []
    for s, e in zip(starts.tolist(), ends.tolist()):
        nb = e - s + 1
        if nb < int(cfg.min_bw_bins):
            continue
        seg = p[s:e + 1]
        kp = s + int(np.argmax(seg))
        delta, pk_db = interp_peak(p_db, kp)
        total = float(np.sum(seg))
        f_lo = float(f[s] - 0.5 * df)
        f_hi = float(f[e] + 0.5 * df)
        f_center = float(np.dot(f[s:e + 1], seg) / total) if total > 0.0 else 0.5 * (f_lo + f_hi)
        nz = float(np.median(noise_db[s:e + 1]))
        dets.append(Detection(
            f_center_hz=f_center,
            f_peak_hz=float(f[kp] + delta * df),
            f_lo_hz=f_lo,
            f_hi_hz=f_hi,
            bw_hz=f_hi - f_lo,
            peak_db=float(pk_db),
            band_power_db=float(db10(total / spec.enbw_bins)),
            snr_db=float(pk_db - nz),
            noise_db=nz,
            n_bins=nb,
        ))
    return dets, threshold_db
