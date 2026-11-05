"""CFAR signal detection on power spectra, at native and at coarser resolutions.

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
   shorter than ``min_bw_bins`` are dropped.

Default ``n_avg`` (when ``None``): :func:`default_n_avg` — the Welch effective averages
from ``spec.averages``, ``spec.meta['overlap']`` (0 if absent) and ``spec.window`` for
``avg == "mean"``; ``1`` for ``max``/``min``/``median`` averaging (exact for ``min`` of
independent segments, conservative for ``max``/``median``).

Multi-resolution CFAR (``cfg.resolutions``, default ``[1, 16, 128]``)
--------------------------------------------------------------------
A flat signal much wider than the training window (``2 (train + guard)`` bins, ~0.7 MHz at
9.8 kHz bins) fills its own training cells and masks itself at native resolution (an
OS-CFAR cell is only detectable while fewer than ``(1 - os_rank) M`` of its cells hold
signal). LTE (10 MHz) and Wi-Fi (20 MHz) are therefore searched on coarser spectra:

* **Coarse spectrum** for ``D > 1``: ``D`` adjacent native bins power-averaged (block mean,
  same per-bin unit as the native spectrum), blocks centred on the spectrum. Interior
  invalid native bins (DC notch, ``valid_mask`` holes) are first replaced by linear
  interpolation of their valid neighbours so a DC spike never raises a coarse bin; a coarse
  bin is valid when more than half of its native bins are valid. ``guard_bins`` /
  ``train_bins`` count *coarse* bins. A pass is **skipped** when the coarse spectrum has
  fewer than ``4 (train_bins + guard_bins)`` bins.
* **Effective averages scale with D**: noise-only native bins are ``Gamma(n_eff)`` and
  their only mutual dependence is the window's leakage correlation (white noise across
  the block). For the **training cells** (noise estimate) the block mean is modelled
  (moment-matched) as ``Gamma(n_eff * D_eff)`` with ``D_eff = D^2 / sum_{i,j<D}
  rho(|i - j|)``: ``D_eff = D`` for independent bins (rectangular window), ``~ D / 2.76``
  for Blackman-Harris (the cruder ``D / ENBW_bins = D / 2.0`` would overstate the
  averaging). Overlapping Welch segments are treated as separable (``n_eff`` per bin times
  ``D_eff``). The coarse training cells are nearly independent; their ``M_eff`` follows
  from the coarse-bin correlation induced by ``rho`` (same formula as above).
* **Exact tail of the coarse cell under test**: the false-alarm rate is set by the far
  tail of the block mean, which the moment-matched gamma underestimates (Blackman-Harris,
  ``D = 16``: x2 at 1e-4, x5 at 1.7e-7). The block mean of one segment is the quadratic
  form ``(1/D) X^H X`` of the ``D`` native bins ``X ~ CN(0, R)``, ``R[i, j] = r(i - j)``
  with ``r`` = DFT of ``w^2`` / ``sum w^2`` (the bins' amplitude correlation; ``rho =
  |r|^2``). With the eigenvalues ``lambda_i`` of ``R`` it is ``sum_i lambda_i / (n_eff D)
  * Gamma(n_eff, 1)`` (:func:`coarse_cell_model`), whose survival the CFAR multiplier uses
  (:class:`~eds.dsp.stats.CellModel`, saddlepoint; :mod:`eds.dsp.stats`). For a
  rectangular window ``R = I`` and this is the exact ``Gamma(n_eff D)``.
* **Bonferroni**: every pass that runs uses ``pfa / n_passes`` (``n_passes`` = native +
  coarse passes actually run), so the expected number of false-alarm bins of all passes
  together is ``<= pfa * n_native`` (coarse spectra have fewer bins). With a single
  runnable pass (``resolutions = [1]`` or a short spectrum) the result is identical to the
  single-resolution CFAR. The returned ``threshold_db`` is the native pass's.
* **Mapping back / refinement**: each coarse run is mapped to its native bins; the seed is
  the maximum of the native spectrum smoothed over ``D`` bins (centred moving average,
  ``s_D``) inside it. The **support** is the run around the seed where ``s_D`` exceeds
  ``noise * max(10^(min_snr_db/10), 1 + 3/sqrt(n_eff_c))`` (``noise`` = the coarse pass
  estimate interpolated to native bins — the native estimate is masked inside a wide
  signal; the second term keeps the smoothed noise, relative std ``1/sqrt(n_eff_c)``, from
  carrying the extension). Box smoothing turns each edge of a flat-topped emitter into a
  linear ramp of ``D`` bins whose midpoint is the true edge, so the edges are refined to the
  **50 % points** of ``s_D - noise`` relative to the in-band plateau (median of
  ``s_D - noise`` at least ``D`` bins inside the support; its maximum when the support is
  shorter than ``2D + 1``), sub-bin interpolated: this removes the ``D``-bin widening and
  equals the -3 dB bandwidth. Emitters narrower than ``D`` come out resolution-limited
  (~``D`` bins wide); they are normally detected (and kept) at a finer resolution.
* **Merging** (passes in ascending ``D``, detections of finer passes "accepted"):

  - a coarse detection whose excess power ``sum(P - noise)`` is at least 50 % carried by
    accepted finer detections overlapping it is the blurred image of those and is dropped;
  - otherwise it is accepted and **supersedes** accepted finer detections fully inside it,
    except those whose peak exceeds its mean in-band level (``sum P / n_bins`` over its
    bins) by >= 10 dB (narrowband carrier inside a wideband signal -> both reported).

  Wide detections use the same :class:`Detection` fields (centroid, interpolated peak,
  ``band_power_db``, median noise of the coarse estimate, ``snr_db = peak - noise``),
  computed on the native spectrum (invalid interior bins interpolated). Detections are
  returned sorted by ``(f_lo_hz, f_hi_hz)``.
"""

from __future__ import annotations

import logging
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
    CellModel,
    ca_cfar_alpha,
    effective_averages,
    os_cfar_alpha,
    os_cfar_scale,
    os_rank_index,
)
from .windows import bin_correlation, canonical_window_name, get_window

log = logging.getLogger(__name__)

METHODS: tuple[str, ...] = ("os_cfar", "ca_cfar", "fixed")

#: A coarse pass runs only when its spectrum has at least this many times
#: ``train_bins + guard_bins`` bins.
MIN_COARSE_FACTOR: int = 4
#: A coarse detection whose excess power is at least this fraction carried by finer
#: detections is their blurred image and is dropped.
EXPLAINED_FRACTION: float = 0.5
#: A finer detection inside a wide one survives when its peak exceeds the wide detection's
#: mean in-band level by at least this much (dB).
CARRIER_IN_BAND_DB: float = 10.0
#: The smoothed-spectrum extension threshold is at least ``1 + K / sqrt(n_eff_c)`` times
#: the noise (K standard deviations of the smoothed noise).
EXTENSION_SIGMAS: float = 3.0
#: Hysteresis: the extension threshold lies this many dB below ``min_snr_db``.
EXTENSION_HYSTERESIS_DB: float = 3.0
_RHO_MAX_LAG: int = 32          # native bin correlation beyond this lag is zero for all windows here
_EIG_EXACT_MAX_D: int = 2048    # larger blocks: circulant (symbol) eigenvalues instead of eigvalsh


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


def _native_rho(window: str, nfft: int) -> np.ndarray:
    """Power correlation of native bins at lags ``0..`` (zero beyond ``nfft // 2``; lags
    near ``nfft`` are circular neighbours inside ONE FFT, not in a stitched spectrum)."""
    lag = max(0, min(_RHO_MAX_LAG, int(nfft) // 2))
    return bin_correlation(window, int(nfft), lag)


@lru_cache(maxsize=256)
def _coarse_stats_cached(window: str, nfft: int, d: int, t: int) -> tuple[float, float]:
    """``(D_eff, M_eff)`` of a ``D``-bin block-mean spectrum (see module docstring)."""
    rho = _native_rho(window, nfft)
    lags = np.arange(-(d - 1), d)
    tri = (d - np.abs(lags)).astype(np.float64)            # pair counts per lag inside/between blocks
    m = 2 * t                                               # one-sided window: m contiguous coarse cells
    cov = np.empty(m)
    for big in range(m):
        k = np.abs(big * d + lags)
        r = np.where(k < rho.size, rho[np.minimum(k, rho.size - 1)], 0.0)
        cov[big] = float(np.sum(tri * r))
    d_eff = d * d / cov[0]
    rho_c = cov / cov[0]
    offsets = np.arange(m)
    lag = np.abs(offsets[:, None] - offsets[None, :])
    return float(d_eff), float(m * m / np.sum(rho_c[lag]))


@lru_cache(maxsize=256)
def _block_eigenvalues(window: str, nfft: int, d: int) -> tuple[float, ...]:
    """Eigenvalues (descending, sum normalised to 1) of the ``d x d`` amplitude-correlation
    matrix ``R[i, j] = r(i - j)`` of adjacent native bins (see module docstring)."""
    w = get_window(window, nfft)
    w2 = w * w
    r_all = np.fft.fft(w2) / float(np.sum(w2))
    lag_max = min(_RHO_MAX_LAG, nfft // 2, d - 1)
    r = r_all[:lag_max + 1]
    if d <= _EIG_EXACT_MAX_D:
        lags = np.arange(d)[:, None] - np.arange(d)[None, :]
        rm = np.zeros((d, d), dtype=np.complex128)
        inside = np.abs(lags) <= lag_max
        lg = lags[inside]
        rm[inside] = np.where(lg >= 0, r[np.abs(lg)], np.conj(r[np.abs(lg)]))
        lam = np.linalg.eigvalsh(rm)
    else:                                   # circulant approximation (edge effects ~ lag_max / d)
        c = np.zeros(d, dtype=np.complex128)
        c[:lag_max + 1] = r
        c[d - lag_max:] = np.conj(r[1:][::-1])
        lam = np.fft.fft(c).real
    lam = np.clip(lam, 0.0, None)
    lam = np.sort(lam)[::-1]
    lam = lam[lam > lam[0] * 1e-12]
    return tuple((lam / np.sum(lam)).tolist())


def coarse_cell_model(window: str, nfft: int, d: int, n_eff: float) -> CellModel:
    """Distribution (mean 1) of a ``d``-bin block mean of ``Gamma(n_eff)`` native noise bins
    of ``window`` / ``nfft``: ``sum_i lambda_i / n_eff * Gamma(n_eff, 1)`` with the
    normalised eigenvalues ``lambda_i`` of the bins' amplitude-correlation matrix."""
    if isinstance(d, bool) or int(d) != d or int(d) < 1:
        raise ConfigError(f"Çözünürlük çarpanı pozitif tamsayı olmalı, {d!r} geldi")
    n = float(n_eff)
    if not (math.isfinite(n) and n > 0.0):
        raise ConfigError(f"n_eff pozitif olmalı, {n_eff!r} geldi")
    lam = _block_eigenvalues(canonical_window_name(window), int(nfft), int(d))
    return CellModel(tuple(v / n for v in lam), n)


def coarse_statistics(window: str, nfft: int, d: int, train_bins: int) -> tuple[float, float]:
    """``(D_eff, M_eff)`` for a ``d``-bin power-averaged spectrum of ``window``/``nfft``.

    ``D_eff = d^2 / sum_{i,j<d} rho(|i-j|)`` (effective independent native bins per coarse
    bin; ``= d`` for a rectangular window) and ``M_eff`` the effective number of
    independent cells of a one-sided coarse training window (``2 * train_bins`` contiguous
    coarse bins).
    """
    if isinstance(d, bool) or int(d) != d or int(d) < 1:
        raise ConfigError(f"Çözünürlük çarpanı pozitif tamsayı olmalı, {d!r} geldi")
    if isinstance(train_bins, bool) or int(train_bins) != train_bins or int(train_bins) < 1:
        raise ConfigError(f"train_bins pozitif tamsayı olmalı, {train_bins!r} geldi")
    return _coarse_stats_cached(canonical_window_name(window), int(nfft), int(d), int(train_bins))


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


def _resolutions(cfg: DetectConfig) -> list[int]:
    res = cfg.resolutions
    if not isinstance(res, (list, tuple)) or len(res) == 0:
        raise ConfigError(f"detect.resolutions boş olmayan bir tamsayı listesi olmalı, {res!r} geldi")
    out: set[int] = set()
    for r in res:
        if isinstance(r, bool) or not isinstance(r, (int, float)) or int(r) != r or int(r) < 1:
            raise ConfigError(f"detect.resolutions elemanları >= 1 tamsayı olmalı, {r!r} geldi")
        out.add(int(r))
    if 1 not in out:
        raise ConfigError(f"detect.resolutions doğal çözünürlüğü (1) içermeli, {list(res)!r} geldi")
    return sorted(out)


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
    _resolutions(cfg)


@dataclass(frozen=True)
class NoiseEstimate:
    """Per-bin noise-mean estimate and the CFAR multiplier that goes with it."""

    noise_lin: np.ndarray    # estimated noise mean per bin (linear, spectrum unit)
    alpha: float             # CFAR multiplier on noise_lin (1.0 for method = fixed)
    n_eff: float             # effective averages used
    m_cells: int             # training cells per bin (M)
    m_eff: float             # effective independent training cells
    rank: int                # OS rank k (1-based; 0 for CA)


def _n_eff(spec: Spectrum, n_avg: float | None) -> float:
    n_eff = default_n_avg(spec) if n_avg is None else float(n_avg)
    if not (math.isfinite(n_eff) and n_eff > 0.0):
        raise ConfigError(f"n_avg pozitif olmalı, {n_avg!r} geldi")
    return n_eff


def _checked_power(spec: Spectrum) -> np.ndarray:
    p = spec.p_lin
    if not np.all(np.isfinite(p)) or np.any(p < 0.0):
        raise MeasurementError("Spektrum güç değerleri sonlu ve negatif olmayan olmalı (NaN/inf/negatif var)")
    return p


def _native_train(n: int, cfg: DetectConfig) -> int:
    guard = int(cfg.guard_bins)
    train = min(int(cfg.train_bins), (n - 2 * guard) // 3)
    if train < 1:
        raise ConfigError(
            f"Spektrum CFAR için çok kısa: {n} bin, guard={guard}, train={cfg.train_bins} "
            f"(en az {2 * guard + 3} bin gerekli)")
    return train


def _estimate(p: np.ndarray, cfg: DetectConfig, guard: int, train: int, n_eff: float, m_eff: float,
              pfa: float) -> NoiseEstimate:
    """CFAR noise-mean estimate + multiplier on an arbitrary power array (see module doc)."""
    m = 2 * train
    peak = float(np.max(p))
    scale = peak if peak > 0.0 else 1.0
    cells = _training_cells((p / scale).astype(np.float32), guard, train)
    method = cfg.method
    if method == "ca_cfar":
        noise = np.mean(cells, axis=1, dtype=np.float64) * scale
        alpha = ca_cfar_alpha(pfa, n_eff, m_eff)
        rank = 0
    else:
        rank = os_rank_index(float(cfg.os_rank), m)
        kth = rank - 1
        order_stat = np.partition(cells, kth, axis=1)[:, kth].astype(np.float64) * scale
        noise = order_stat / os_cfar_scale(n_eff, m, rank)
        if method == "os_cfar":
            m_os = max(1, int(round(m_eff)))
            k_os = min(m_os, max(1, int(round(rank / m * m_os))))
            alpha = os_cfar_alpha(pfa, n_eff, m_os, k_os)
        else:
            alpha = 1.0
    return NoiseEstimate(noise_lin=noise, alpha=float(alpha), n_eff=n_eff, m_cells=m, m_eff=m_eff, rank=rank)


def estimate_noise(spec: Spectrum, cfg: DetectConfig, n_avg: float | None = None, *,
                   pfa: float | None = None) -> NoiseEstimate:
    """Native-resolution CFAR noise-mean estimate per bin and the matching multiplier.

    ``pfa`` overrides ``cfg.pfa`` for the multiplier (:func:`detect` passes the
    per-pass Bonferroni share); see the module docstring.
    """
    _check_cfg(cfg)
    n_eff = _n_eff(spec, n_avg)
    p = _checked_power(spec)
    guard = int(cfg.guard_bins)
    train = _native_train(p.size, cfg)
    pf = float(cfg.pfa) if pfa is None else float(pfa)
    if not (0.0 < pf < 1.0):
        raise ConfigError(f"pfa (0, 1) aralığında olmalı, {pfa!r} geldi")
    m_eff = effective_training_cells(spec.window, spec.nfft, guard, train)
    return _estimate(p, cfg, guard, train, n_eff, m_eff, pf)


def _threshold(est: NoiseEstimate, cfg: DetectConfig, n: int) -> np.ndarray:
    noise = est.noise_lin
    if cfg.method == "fixed":
        thr = np.full(n, float(lin10(float(cfg.fixed_threshold))))
    else:
        thr = est.alpha * noise
    return np.maximum(thr, noise * float(lin10(float(cfg.min_snr_db))))


def _valid_bins(spec: Spectrum, cfg: DetectConfig, valid_mask: np.ndarray | None) -> np.ndarray:
    n = spec.n_bins
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
    return valid


def _make_detection(spec: Spectrum, p: np.ndarray, p_db: np.ndarray, noise_db: np.ndarray | float, s: int,
                    e: int) -> Detection:
    """Detection over native bins ``[s, e]`` (inclusive) of power array ``p``; ``noise_db``
    is the per-bin noise estimate (array) or one level for the whole run (float)."""
    df = spec.df_hz
    f = spec.f_hz
    seg = p[s:e + 1]
    kp = s + int(np.argmax(seg))
    delta, pk_db = interp_peak(p_db, kp)
    total = float(np.sum(seg))
    f_lo = float(f[s] - 0.5 * df)
    f_hi = float(f[e] + 0.5 * df)
    f_center = float(np.dot(f[s:e + 1], seg) / total) if total > 0.0 else 0.5 * (f_lo + f_hi)
    nz = float(noise_db) if isinstance(noise_db, float) else float(np.median(noise_db[s:e + 1]))
    return Detection(
        f_center_hz=f_center,
        f_peak_hz=float(f[kp] + delta * df),
        f_lo_hz=f_lo,
        f_hi_hz=f_hi,
        bw_hz=f_hi - f_lo,
        peak_db=float(pk_db),
        band_power_db=float(db10(total / spec.enbw_bins)),
        snr_db=float(pk_db - nz),
        noise_db=nz,
        n_bins=e - s + 1,
    )


def _native_runs(p: np.ndarray, thr: np.ndarray, valid: np.ndarray, cfg: DetectConfig) -> list[tuple[int, int]]:
    hit = np.flatnonzero((p > thr) & valid)
    if hit.size == 0:
        return []
    breaks = np.flatnonzero(np.diff(hit) - 1 > int(cfg.merge_gap_bins))
    starts = hit[np.concatenate(([0], breaks + 1))]
    ends = hit[np.concatenate((breaks, [hit.size - 1]))]
    return [(s, e) for s, e in zip(starts.tolist(), ends.tolist()) if e - s + 1 >= int(cfg.min_bw_bins)]


# --------------------------------------------------------------------------- multi-resolution
@dataclass(frozen=True)
class ResolutionPass:
    """One coarse CFAR pass (diagnostics; see the module docstring)."""

    d: int                   # native bins per coarse bin
    offset: int              # native index of the first bin of coarse bin 0
    p_lin: np.ndarray        # coarse spectrum (block mean, per-native-bin unit)
    valid: np.ndarray        # coarse validity (> half of the native bins valid)
    noise_left: np.ndarray   # one-sided noise-mean estimates (cells left / right of the CUT)
    noise_right: np.ndarray
    alpha: float             # multiplier of each one-sided test (at pfa / 2)
    threshold_lin: np.ndarray  # alpha * min(left, right), floored at noise * min_snr
    n_eff: float             # n_eff(native) * D_eff
    d_eff: float
    m_eff: float             # effective independent cells of one one-sided window
    pfa: float               # per-pass false-alarm probability (Bonferroni share)

    @property
    def noise_lin(self) -> np.ndarray:
        """Smallest-of noise estimate (the one the decision uses)."""
        return np.minimum(self.noise_left, self.noise_right)


@dataclass
class _Cand:
    lo: int
    hi: int
    det: Detection
    d: int


def _coarse_plan(n: int, cfg: DetectConfig) -> list[int]:
    need = MIN_COARSE_FACTOR * (int(cfg.train_bins) + int(cfg.guard_bins))
    return [d for d in _resolutions(cfg) if d > 1 and n // d >= need]


def _fill_invalid(p: np.ndarray, valid: np.ndarray) -> tuple[np.ndarray, np.ndarray]:
    """``p`` with interior invalid runs linearly interpolated from valid neighbours, and the
    mask of *exterior* invalid bins (before the first / after the last valid bin)."""
    n = p.size
    ext = np.zeros(n, dtype=bool)
    idx = np.flatnonzero(valid)
    if idx.size == 0:
        ext[:] = True
        return p, ext
    ext[:idx[0]] = True
    ext[idx[-1] + 1:] = True
    inner = ~valid & ~ext
    if not np.any(inner):
        return p, ext
    out = p.copy()
    k = np.flatnonzero(inner)
    out[k] = np.interp(k, idx, p[idx])
    return out, ext


def _one_sided(pc: np.ndarray, cfg: DetectConfig, n_c: float, m_eff: float, pfa: float,
               cut: CellModel | None = None) -> tuple[np.ndarray, np.ndarray, float]:
    """One-sided noise-mean estimates (left / right windows of ``2 train`` coarse cells beyond
    ``guard``) and the multiplier of each one-sided test at ``pfa`` (see module doc);
    ``cut`` = exact cell-under-test distribution (``None``: ``Gamma(n_c)``)."""
    n = pc.size
    g, t = int(cfg.guard_bins), int(cfg.train_bins)
    m = 2 * t
    peak = float(np.max(pc))
    scale = peak if peak > 0.0 else 1.0
    win = sliding_window_view((pc / scale).astype(np.float64), m)       # win[j] = pc[j:j+m]
    if cfg.method == "ca_cfar":
        est_w = np.mean(win, axis=1) * scale
        alpha = ca_cfar_alpha(pfa, n_c, m_eff, cut=cut)
    else:
        rank = os_rank_index(float(cfg.os_rank), m)
        est_w = np.partition(win, rank - 1, axis=1)[:, rank - 1] * scale / os_cfar_scale(n_c, m, rank)
        if cfg.method == "os_cfar":
            m_os = max(1, int(round(m_eff)))
            k_os = min(m_os, max(1, int(round(rank / m * m_os))))
            alpha = os_cfar_alpha(pfa, n_c, m_os, k_os, cut=cut)
        else:
            alpha = 1.0
    k = np.arange(n)
    jl = k - g - m                       # left window of CUT k starts here
    jr = k + g + 1                       # right window of CUT k starts here
    has_l = jl >= 0
    has_r = jr <= n - m
    left = np.where(has_l, est_w[np.clip(jl, 0, est_w.size - 1)], np.nan)
    right = np.where(has_r, est_w[np.clip(jr, 0, est_w.size - 1)], np.nan)
    left = np.where(has_l, left, right)  # near the ends only one side fits: use it for both
    right = np.where(has_r, right, left)
    return left, right, float(alpha)


def _coarse_pass(p_work: np.ndarray, valid: np.ndarray, d: int, spec: Spectrum, cfg: DetectConfig,
                 n_eff: float, pfa: float) -> ResolutionPass:
    n = p_work.size
    nc = n // d
    off = (n - nc * d) // 2
    pc = np.mean(p_work[off:off + nc * d].reshape(nc, d), axis=1)
    vc = np.count_nonzero(valid[off:off + nc * d].reshape(nc, d), axis=1) * 2 > d
    d_eff, m_eff = coarse_statistics(spec.window, spec.nfft, d, int(cfg.train_bins))
    n_c = n_eff * d_eff
    cut = coarse_cell_model(spec.window, spec.nfft, d, n_eff) if cfg.method != "fixed" else None
    if cut is not None and cut.weights[-1] >= cut.weights[0] * (1.0 - 1e-9):
        cut = None                          # independent bins (rect): Gamma(n_eff D) is exact
    left, right, alpha = _one_sided(pc, cfg, n_c, m_eff, 0.5 * pfa, cut)
    so = np.minimum(left, right)
    if cfg.method == "fixed":
        thr = np.full(nc, float(lin10(float(cfg.fixed_threshold))))
    else:
        thr = alpha * so
    thr = np.maximum(thr, so * float(lin10(float(cfg.min_snr_db))))
    return ResolutionPass(d=d, offset=off, p_lin=pc, valid=vc, noise_left=left, noise_right=right, alpha=alpha,
                          threshold_lin=thr, n_eff=n_c, d_eff=d_eff, m_eff=m_eff, pfa=pfa)


def _smooth(p: np.ndarray, d: int) -> tuple[np.ndarray, float]:
    """Centred moving average over ``d`` bins (shrinking at the ends) and its centre offset
    ``c_off``: ``s[k]`` averages around native position ``k + c_off``."""
    n = p.size
    h_l = d // 2
    h_r = d - 1 - h_l
    cs = np.concatenate(([0.0], np.cumsum(p, dtype=np.float64)))
    k = np.arange(n)
    a = np.maximum(k - h_l, 0)
    b = np.minimum(k + h_r, n - 1) + 1
    return (cs[b] - cs[a]) / (b - a), 0.5 * (h_r - h_l)


def _support(seed: int, above: np.ndarray, reach: np.ndarray, d: int) -> tuple[int, int]:
    """Run of ``above`` containing ``seed`` (which must be above); neighbouring runs separated
    by dips of at most ``d`` reachable bins (the correlation length of the ``d``-smoothed
    spectrum) are absorbed. The support always starts and ends on an ``above`` bin."""
    r_lo, r_hi = _runs(above)
    i = int(np.searchsorted(r_lo, seed, side="right")) - 1
    a, b = int(r_lo[i]), int(r_hi[i])
    j = i
    while j > 0 and a - int(r_hi[j - 1]) - 1 <= d and reach[int(r_hi[j - 1]) + 1:a].all():
        j -= 1
        a = int(r_lo[j])
    j = i
    while j + 1 < r_lo.size and int(r_lo[j + 1]) - b - 1 <= d and reach[b + 1:int(r_lo[j + 1])].all():
        j += 1
        b = int(r_hi[j])
    return a, b


def _runs(mask: np.ndarray) -> tuple[np.ndarray, np.ndarray]:
    edges = np.flatnonzero(np.diff(np.concatenate(([0], mask.astype(np.int8), [0]))))
    return edges[0::2], edges[1::2] - 1


def _crossing(e: np.ndarray, level: float, k_in: int, k_out: int) -> float:
    """Fractional position between adjacent bins ``k_in`` (>= level) and ``k_out`` (< level)."""
    a, b = float(e[k_in]), float(e[k_out])
    t = (a - level) / (a - b) if a != b else 0.5
    return k_in + (k_out - k_in) * min(max(t, 0.0), 1.0)


def _refine(seed: int, a: int, b: int, ex: np.ndarray, reach: np.ndarray, d: int, c_off: float
            ) -> tuple[int, int]:
    """Native extent ``[lo, hi]`` from the 50 % points of the smoothed excess ``ex``."""
    n = ex.size
    if b - a + 1 > 2 * d + 1:
        plateau = float(np.median(ex[a + d:b - d + 1]))
    else:
        plateau = float(np.max(ex[a:b + 1]))
    half = 0.5 * plateau
    # left edge: inside the support walk right to the first bin >= half, else extend left
    if ex[a] >= half:
        k = a
        lim = max(0, a - d)
        while k - 1 >= lim and reach[k - 1] and ex[k - 1] >= half:
            k -= 1
        x_l = _crossing(ex, half, k, k - 1) if k - 1 >= 0 and reach[k - 1] else k - 0.5
    else:
        k = a
        while k < seed and ex[k] < half:
            k += 1
        x_l = _crossing(ex, half, k, k - 1) if k > a else float(k)
    if ex[b] >= half:
        k = b
        lim = min(n - 1, b + d)
        while k + 1 <= lim and reach[k + 1] and ex[k + 1] >= half:
            k += 1
        x_r = _crossing(ex, half, k, k + 1) if k + 1 < n and reach[k + 1] else k + 0.5
    else:
        k = b
        while k > seed and ex[k] < half:
            k -= 1
        x_r = _crossing(ex, half, k, k + 1) if k < b else float(k)
    # x + c_off = band edge as a bin-edge position (first bin - 1/2, last bin + 1/2), rounded
    lo = int(math.floor(x_l + c_off + 1.0))
    hi = int(math.floor(x_r + c_off))
    lo = min(max(lo, 0), n - 1)
    hi = min(max(hi, 0), n - 1)
    if hi < lo:
        lo = hi = min(max(seed, 0), n - 1)
    return lo, hi


def _wide_candidates(rp: ResolutionPass, spec: Spectrum, cfg: DetectConfig, p_work: np.ndarray,
                     p_work_db: np.ndarray, ext: np.ndarray) -> list[tuple[_Cand, float]]:
    """Refined native-resolution detections of one coarse pass with their noise level."""
    d = rp.d
    hit = (rp.p_lin > rp.threshold_lin) & rp.valid
    if not np.any(hit):
        return []
    s_d, c_off = _smooth(p_work, d)
    ext_fac = max(float(lin10(float(cfg.min_snr_db) - EXTENSION_HYSTERESIS_DB)),
                  1.0 + EXTENSION_SIGMAS / math.sqrt(rp.n_eff))
    reach = ~ext
    so = rp.noise_lin
    c_lo, c_hi = _runs(hit)
    supports: list[tuple[int, int]] = []
    out: list[tuple[_Cand, float]] = []
    for j0, j1 in zip(c_lo.tolist(), c_hi.tolist()):
        span_lo = rp.offset + j0 * d
        span_hi = rp.offset + (j1 + 1) * d - 1
        seed = span_lo + int(np.argmax(s_d[span_lo:span_hi + 1]))
        if any(a <= seed <= b for a, b in supports):
            continue                       # other edge / fragment of a signal already mapped
        # noise of the clean side at the strongest coarse bin of the run (inside a wide
        # signal the estimates are masked; the run's detecting side looks outside it)
        jc = j0 + int(np.argmax(rp.p_lin[j0:j1 + 1] / so[j0:j1 + 1]))
        noise = float(so[jc])
        level = noise * ext_fac
        if not (reach[seed] and s_d[seed] > level):
            log.debug("D=%d: kaba tespit %d..%d düzgünleştirilmiş spektrumda eşiğin altında, atlandı", d, j0, j1)
            continue
        a, b = _support(seed, (s_d > level) & reach, reach, d)
        supports.append((a, b))
        lo, hi = _refine(seed, a, b, s_d - noise, reach, d, c_off)
        if hi - lo + 1 < int(cfg.min_bw_bins):
            continue
        det = _make_detection(spec, p_work, p_work_db, float(db10(noise)), lo, hi)
        out.append((_Cand(lo, hi, det, d), noise))
    return out


def _merge(accepted: list[_Cand], wide: list[tuple[_Cand, float]], p_work: np.ndarray) -> list[_Cand]:
    """Merge the wide candidates of one pass into ``accepted`` (see module docstring)."""
    cum = np.concatenate(([0.0], np.cumsum(p_work)))

    def excess(lo: int, hi: int, noise: float) -> float:
        return float(cum[hi + 1] - cum[lo]) - noise * (hi - lo + 1)

    for w, noise in wide:
        total = excess(w.lo, w.hi, noise)
        if total <= 0.0:
            continue
        overl = [a for a in accepted if a.hi >= w.lo and a.lo <= w.hi]
        explained = sum(excess(max(a.lo, w.lo), min(a.hi, w.hi), noise) for a in overl)
        if explained >= EXPLAINED_FRACTION * total:
            continue
        mean_db = float(db10(float(cum[w.hi + 1] - cum[w.lo]) / (w.hi - w.lo + 1)))
        accepted = [a for a in accepted
                    if not (w.lo <= a.lo and a.hi <= w.hi and a.det.peak_db < mean_db + CARRIER_IN_BAND_DB)]
        accepted.append(w)
    return accepted


def resolution_passes(spec: Spectrum, cfg: DetectConfig, n_avg: float | None = None,
                      valid_mask: np.ndarray | None = None) -> list[ResolutionPass]:
    """The coarse passes :func:`detect` runs for ``spec`` (diagnostics, plots, tests)."""
    _check_cfg(cfg)
    n_eff = _n_eff(spec, n_avg)
    p = _checked_power(spec)
    valid = _valid_bins(spec, cfg, valid_mask)
    plan = _coarse_plan(p.size, cfg)
    pfa = float(cfg.pfa) / (1 + len(plan))
    p_work, _ = _fill_invalid(p, valid)
    return [_coarse_pass(p_work, valid, d, spec, cfg, n_eff, pfa) for d in plan]


def detect(spec: Spectrum, cfg: DetectConfig, n_avg: float | None = None,
           valid_mask: np.ndarray | None = None) -> tuple[list[Detection], np.ndarray]:
    """Detect signals in ``spec``. Returns ``(detections, threshold_db)``.

    ``threshold_db`` has one value per bin (spectrum dB unit): the native-resolution pass
    threshold (at its Bonferroni share ``pfa / n_passes`` of the false-alarm budget).
    ``valid_mask`` (bool, one per bin; e.g. :func:`usable_mask`) restricts where detections
    may occur; the threshold is computed everywhere. See the module docstring.
    """
    _check_cfg(cfg)
    n_eff = _n_eff(spec, n_avg)
    p = _checked_power(spec)
    n = p.size
    plan = _coarse_plan(n, cfg)
    pfa_pass = float(cfg.pfa) / (1 + len(plan))
    est = estimate_noise(spec, cfg, n_eff, pfa=pfa_pass)
    thr = _threshold(est, cfg, n)
    threshold_db = db10(thr)
    valid = _valid_bins(spec, cfg, valid_mask)
    runs = _native_runs(p, thr, valid, cfg)
    if not plan:
        if not runs:
            return [], threshold_db
        p_db = spec.p_db
        noise_db = db10(est.noise_lin)
        return [_make_detection(spec, p, p_db, noise_db, s, e) for s, e in runs], threshold_db

    p_work, ext = _fill_invalid(p, valid)
    p_db = db10(p)
    noise_db = db10(est.noise_lin)
    accepted = [_Cand(s, e, _make_detection(spec, p, p_db, noise_db, s, e), 1) for s, e in runs]
    p_work_db = p_db if p_work is p else db10(p_work)
    for d in plan:
        rp = _coarse_pass(p_work, valid, d, spec, cfg, n_eff, pfa_pass)
        wide = _wide_candidates(rp, spec, cfg, p_work, p_work_db, ext)
        if wide:
            accepted = _merge(accepted, wide, p_work)
    accepted.sort(key=lambda c: (c.det.f_lo_hz, c.det.f_hi_hz))
    return [c.det for c in accepted], threshold_db
