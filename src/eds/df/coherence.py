"""Inter-channel coherence for direction finding: sample delay, phase offset, phase drift.

Two independent Plutos (phase 0) are *not* coherent: their streams start at different
instants (integer + fractional sample delay), every retune draws a new LO phase, and
without a shared 40 MHz reference the phase difference rotates continuously at
``f · Δppm``. This module measures all three with a common (cabled) signal:

* :func:`estimate_delay` — delay ``d`` with ``x1[n] ≈ x0[n − d]`` (fractional), from the
  zero-padded FFT cross-correlation (integer peak + parabolic interpolation on ``|R|``),
  refined by a coherence-weighted fit of the cross-spectrum phase slope across the band.
  A delay is only *resolvable* with a wideband common signal (noise source); a pure tone
  gives no delay information (``DelayEstimate.resolvable = False``).
* :func:`phase_offset` — ``arg(Σ x1·conj(x0))`` and the normalized coherence.
* :func:`align` — fractional-delay compensation ``align(x1, d)[n] ≈ x1[n + d] ≈ x0[n]``.
* :func:`check_coherence` — per block: delay, delay-compensated phase; across blocks:
  phase drift (deg/s) with its standard error and phase stability. ``coherent`` iff
  ``|drift| < max(1 °/s, k·σ_drift)`` (``k`` = :data:`DRIFT_K_SIGMA`), coherence > 0.9 and
  phase std < 5° after drift removal.

Drift verdict. Over a short capture the ±1 °/s limit is close to the estimator's
resolution: block phases known to ``σ_b`` each, ``N_b`` blocks over a span ``T``, give
``σ_drift = σ_b / √Σ(t_b − t̄)² ≈ σ_b·√(12/N_b)/T`` — e.g. a weak (−70 dBm, simulator)
noise-like pilot, 4 × 16384 samples at 2 MS/s: ``σ_drift ≈ 0.7 °/s``, and a shared
reference (true drift 0) would fail the bare limit in ~20 % of captures. The drift is
therefore compared against ``max(limit, k·σ_drift)``: false alarms stay below ``2·Q(k)``
(0.3 % for ``k = 3``), while independent references (drift ``360·f·Δe``, ~10⁴ °/s for
0.1 ppm at 1 GHz) are still rejected. ``σ_drift`` uses the larger of two block-phase
variances: the white-noise one from the scatter of sub-block phases inside every block
(many degrees of freedom) and the between-block residual of the linear fit (also sees LO
phase wander, but has only ``N_b − 2`` degrees of freedom). When ``k·σ_drift`` exceeds the
limit the note says that the limit itself could not be verified with this capture (longer
capture / stronger pilot). All blocks are aligned with ONE delay (median of the per-block
estimates): per-block delay noise ``δd`` would add block-to-block phase jitter
``2π·f_bb·δd/fs`` on a wideband pilot — which was the dominant drift error of noise-like
pilots (rms ≈ 1 °/s at −60 dBm, 4 × 16384 samples) before it was removed.

Phase convention: the measured phase is ``arg(x1 · conj(x0))`` (channel 1 relative to the
reference channel 0), drift = d/dt of that phase. For a tone at RF ``f`` and reference
errors ``e0, e1`` the drift is ``360 · f · (1/(1+e1) − 1/(1+e0)) ≈ 360 · f · (e0 − e1)``
deg/s.
"""

from __future__ import annotations

import contextlib
import logging
import math
from collections.abc import Iterator, Sequence
from contextvars import ContextVar
from dataclasses import dataclass, field, fields
from typing import Any

import numpy as np
from numpy.lib.stride_tricks import sliding_window_view

from ..core.errors import ConfigError, MeasurementError
from ..core.timeutil import now_ns
from ..dsp.peaks import tone_frequency
from ..dsp.stats import effective_averages
from ..dsp.windows import get_window

log = logging.getLogger(__name__)

#: Default coherence criteria (ARCHITECTURE / HARDWARE_PLUTO section 5).
DRIFT_LIMIT_DEG_S: float = 1.0
COHERENCE_MIN: float = 0.9
PHASE_STD_MAX_DEG: float = 5.0
#: The drift limit is applied as ``max(DRIFT_LIMIT_DEG_S, DRIFT_K_SIGMA · σ_drift)``.
DRIFT_K_SIGMA: float = 3.0

_G2_MAX = 0.9999                 # cap of the per-bin magnitude-squared coherence (weights)
_G2_PFA = 1e-3                   # false-alarm level of the per-bin coherence significance test
_MIN_PARTICIPATION = 8.0         # effective number of coherent bins for a resolvable delay
_MIN_RMS_BW_BINS = 2.0           # Gabor bandwidth (segment bins) for a resolvable delay
_EDGE_GUARD = 8                  # samples dropped at each end after fractional alignment
_SUB_BLOCKS = 16                 # sub-blocks per block for the white phase-noise estimate
_SUB_MIN_LEN = 64                # minimum samples per sub-block
_SUB_MIN_COUNT = 4               # fewer sub-blocks: no within-block estimate
_DRIFT_REFINE_ITER = 4           # derotation / block-phase slope iterations
_DRIFT_REFINE_TOL_RAD = 1e-6     # stop when the residual ramp over the span is below this

_REPORT_SINK: ContextVar[list[Any] | None] = ContextVar("eds_coherence_reports", default=None)


def _c1d(x: Any, what: str) -> np.ndarray:
    a = np.asarray(x)
    if a.ndim == 2 and a.shape[0] == 1:
        a = a[0]
    if a.ndim != 1:
        raise ConfigError(f"{what}: 1-B örnek dizisi bekleniyordu, şekil {a.shape} geldi")
    if a.size == 0:
        raise ConfigError(f"{what}: boş dizi")
    a = a.astype(np.complex128, copy=False)
    if not np.all(np.isfinite(a)):
        raise MeasurementError(f"{what}: NaN/sonsuz örnek var")
    return a


def _pair(x0: Any, x1: Any, min_n: int = 32) -> tuple[np.ndarray, np.ndarray]:
    a, b = _c1d(x0, "x0"), _c1d(x1, "x1")
    if a.size != b.size:
        raise ConfigError(f"x0 ve x1 aynı uzunlukta olmalı ({a.size} / {b.size})")
    if a.size < min_n:
        raise ConfigError(f"En az {min_n} örnek gerekli, {a.size} geldi")
    return a, b


def _next_pow2(n: int) -> int:
    return 1 << max(0, int(n - 1).bit_length())


# ============================================================================ delay
@dataclass
class DelayEstimate:
    """Result of :func:`estimate_delay_ex` (convention ``x1[n] ≈ x0[n − delay]``).

    delay_samples      fractional delay (samples)
    peak_corr          normalized cross-correlation magnitude at the integer peak (0..1)
    integer_lag        integer lag of the correlation peak
    delay_std_samples  standard deviation predicted from the cross-spectral coherence
                       (NaN when not resolvable)
    rms_bw_norm        Gabor RMS bandwidth of the coherent cross-spectrum (cycles/sample)
    resolvable         the common signal is wideband enough to determine the delay
    n                  samples per channel
    fs                 sample rate if given (for :attr:`delay_s`)
    """

    delay_samples: float
    peak_corr: float
    integer_lag: int
    delay_std_samples: float
    rms_bw_norm: float
    resolvable: bool
    n: int
    fs: float | None = None

    @property
    def delay_s(self) -> float:
        if not self.fs:
            raise ConfigError("delay_s için fs gerekli")
        return self.delay_samples / self.fs

    def to_dict(self) -> dict[str, Any]:
        out: dict[str, Any] = {}
        for f in fields(self):
            v = getattr(self, f.name)
            if isinstance(v, float) and not math.isfinite(v):
                v = None
            out[f.name] = v
        return out


def estimate_delay_ex(x0: Any, x1: Any, fs: float | None = None, max_lag: int | None = None) -> DelayEstimate:
    """Delay estimate with diagnostics (see module docstring and :class:`DelayEstimate`).

    1. Means removed (zero-IF DC offsets are not a common signal), cross-correlation
       ``R[l] = Σ x1[k] conj(x0[k − l])`` by zero-padded FFT, integer peak within
       ``|l| ≤ max_lag`` (default ``n // 2``), parabolic interpolation on ``|R|``.
    2. After removing the integer lag, Welch cross-spectrum (Hann, 50 % overlap) and a
       weighted least-squares fit of its phase ``φ − ω·r`` over frequency; weights
       ``γ² / (1 − γ²)`` (inverse phase variance from the bias-corrected magnitude-squared
       coherence). Iterated three times.
    """
    a, b = _pair(x0, x1)
    n = a.size
    if fs is not None:
        fs = float(fs)
        if not (math.isfinite(fs) and fs > 0):
            raise ConfigError(f"fs pozitif olmalı, {fs!r} geldi")
    lag_max = n // 2 if max_lag is None else int(max_lag)
    if lag_max < 0 or lag_max > n - 1:
        raise ConfigError(f"max_lag 0..{n - 1} aralığında olmalı, {max_lag!r} geldi")
    a = a - a.mean()
    b = b - b.mean()
    ea = float(np.vdot(a, a).real)
    eb = float(np.vdot(b, b).real)
    if ea <= 0.0 or eb <= 0.0:
        raise MeasurementError("Gecikme kestirimi: kanallardan birinde sinyal yok (sıfır enerji)")
    m = _next_pow2(2 * n)
    r = np.fft.ifft(np.fft.fft(b, m) * np.conj(np.fft.fft(a, m)))
    lags = np.arange(-lag_max, lag_max + 1)
    mag = np.abs(r[lags % m])
    i = int(np.argmax(mag))
    lag = int(lags[i])
    peak = float(min(mag[i] / math.sqrt(ea * eb), 1.0))
    delta = 0.0
    if 0 < i < mag.size - 1:
        y0, y1, y2 = float(mag[i - 1]), float(mag[i]), float(mag[i + 1])
        den = y0 - 2.0 * y1 + y2
        if den < 0.0:
            delta = float(np.clip(0.5 * (y0 - y2) / den, -0.5, 0.5))
    d0 = lag + delta

    # ---- phase-slope refinement on the integer-aligned overlap
    if lag >= 0:
        aa, bb = a[:n - lag], b[lag:]
    else:
        aa, bb = a[-lag:], b[:n + lag]
    n_ov = aa.size
    if n_ov < 128:
        return DelayEstimate(d0, peak, lag, math.nan, 0.0, False, n, fs)
    seg = int(min(1024, max(64, 1 << int(math.floor(math.log2(n_ov / 16.0)))))) if n_ov >= 1024 else 64
    hop = seg // 2
    w = np.asarray(get_window("hann", seg), dtype=np.float64)
    sa = sliding_window_view(aa, seg)[::hop]
    sb = sliding_window_view(bb, seg)[::hop]
    k_seg = sa.shape[0]
    fa = np.fft.fft(sa * w, axis=1)
    fb = np.fft.fft(sb * w, axis=1)
    c = np.sum(fb * np.conj(fa), axis=0)
    paa = np.sum(fa.real ** 2 + fa.imag ** 2, axis=0)
    pbb = np.sum(fb.real ** 2 + fb.imag ** 2, axis=0)
    den2 = paa * pbb
    g2_raw = np.divide(np.abs(c) ** 2, den2, out=np.zeros(seg), where=den2 > 0)
    # Significance: for uncorrelated channels the estimate is Beta(1, K_eff - 1) distributed,
    # P(γ² > x) = (1 - x)^(K_eff - 1). Bins below the 1e-3 false-alarm level carry no
    # coherent signal and get zero weight (otherwise ~1000 noise bins add up).
    k_eff = effective_averages(k_seg, 0.5, "hann", seg) if k_seg > 1 else 1.0
    thr = 1.0 - _G2_PFA ** (1.0 / (k_eff - 1.0)) if k_eff > 1.5 else 1.0
    g2 = (k_seg * g2_raw - 1.0) / (k_seg - 1.0) if k_seg > 1 else g2_raw   # remove the 1/K bias
    g2 = np.where(g2_raw > thr, np.clip(g2, 0.0, _G2_MAX), 0.0)
    wts = g2 / (1.0 - g2)
    sw = float(np.sum(wts))
    omega = 2.0 * np.pi * np.fft.fftfreq(seg)
    if sw <= 0.0:
        return DelayEstimate(d0, peak, lag, math.nan, 0.0, False, n, fs)
    res = d0 - lag
    for _ in range(3):
        cr = c * np.exp(1j * omega * res)
        mag_cr = np.abs(cr)
        unit = np.divide(cr, mag_cr, out=np.zeros_like(cr), where=mag_cr > 0)
        phi = float(np.angle(np.sum(wts * unit)))
        psi = np.angle(cr * np.exp(-1j * phi))
        wbar = float(np.sum(wts * omega) / sw)
        dw = omega - wbar
        sxx = float(np.sum(wts * dw * dw))
        if sxx <= 0.0:
            break
        slope = float(np.sum(wts * dw * psi) / sxx)
        res -= slope
    # Resolvability from the *coherent cross-power* u = |C|·γ² (a tone, or two tones, put
    # it into a few bins; a wideband common signal spreads it over many).
    u = np.abs(c) * g2
    su = float(np.sum(u))
    if su > 0.0:
        ubar = float(np.sum(u * omega) / su)
        rms_bw = math.sqrt(max(float(np.sum(u * (omega - ubar) ** 2) / su), 0.0)) / (2.0 * np.pi)
        participation = su * su / float(np.sum(u * u))
    else:
        rms_bw, participation = 0.0, 0.0
    resolvable = participation >= _MIN_PARTICIPATION and rms_bw * seg >= _MIN_RMS_BW_BINS
    wbar = float(np.sum(wts * omega) / sw)
    sxx = float(np.sum(wts * (omega - wbar) ** 2))
    std = 1.0 / math.sqrt(2.0 * k_seg * sxx) if sxx > 0 else math.nan
    if not resolvable:
        return DelayEstimate(d0, peak, lag, math.nan, rms_bw, False, n, fs)
    return DelayEstimate(lag + res, peak, lag, std, rms_bw, True, n, fs)


def estimate_delay(x0: Any, x1: Any, fs: float | None = None, max_lag: int | None = None) -> tuple[float, float]:
    """``(delay_samples, peak_corr)`` with ``x1[n] ≈ x0[n − delay]`` (fractional samples).

    See :func:`estimate_delay_ex` for the method and the ``resolvable`` diagnostic (a
    narrowband common signal cannot determine a delay).
    """
    d = estimate_delay_ex(x0, x1, fs, max_lag)
    return d.delay_samples, d.peak_corr


def phase_offset(x0: Any, x1: Any) -> tuple[float, float]:
    """``(arg(Σ x1·conj(x0)) [rad], coherence)``; coherence = ``|Σ x1 x0*| / √(Σ|x0|² Σ|x1|²)``."""
    a, b = _pair(x0, x1, min_n=1)
    s = complex(np.vdot(a, b))
    e = float(np.vdot(a, a).real) * float(np.vdot(b, b).real)
    if e <= 0.0:
        raise MeasurementError("Faz ofseti: kanallardan birinde sinyal yok (sıfır enerji)")
    return float(np.angle(s)), float(min(abs(s) / math.sqrt(e), 1.0))


def align(x1: Any, delay: float) -> np.ndarray:
    """Compensate a delay: ``y[n] = x1[n + delay]`` (so ``y ≈ x0`` if ``x1[n] = x0[n − delay]``).

    The integer part is an exact shift (zeros where no sample exists), the fractional part
    an FFT phase ramp ``e^{+jωδ}`` (circular). Use :func:`valid_slice` to drop the edge
    samples that are zero or wrap-contaminated. Returns complex128 of the same length.
    """
    x = _c1d(x1, "x1")
    n = x.size
    d = float(delay)
    if not math.isfinite(d):
        raise ConfigError(f"Gecikme sonlu olmalı, {delay!r} geldi")
    if abs(d) >= n - 1:
        raise ConfigError(f"Gecikme {d:g} örnek, kayıt uzunluğu {n} için çok büyük")
    whole = int(round(d))
    frac = d - whole
    y = np.zeros(n, dtype=np.complex128)
    if whole >= 0:
        y[:n - whole] = x[whole:]
    else:
        y[-whole:] = x[:n + whole]
    if frac != 0.0:
        omega = 2.0 * np.pi * np.fft.fftfreq(n)
        y = np.fft.ifft(np.fft.fft(y) * np.exp(1j * omega * frac))
    return y


def valid_slice(n: int, delay: float, guard: int = _EDGE_GUARD) -> slice:
    """Samples of ``align(x, delay)`` (length ``n``) not affected by the shift edges."""
    d = float(delay)
    start = int(guard) + max(0, math.ceil(-d))
    stop = int(n) - int(guard) - max(0, math.ceil(d))
    if stop - start < 16:
        raise ConfigError(f"Gecikme {d:g} örnek ile {n} örneklik kayıtta geçerli bölge kalmıyor")
    return slice(start, stop)


# ============================================================================ coherence
def _block_phase_var(blocks: Sequence[np.ndarray], fs: float) -> float:
    """White-noise variance (rad²) of one block phase ``arg(Σ z)``, from the scatter of
    sub-block phases inside the (Δf-derotated) blocks ``z``; NaN when the blocks are too short.

    Every block is split into ``M`` equal sub-blocks (sums ``C_m``, ``M`` ≤ 16 and ≥ 64
    samples each, common to all blocks). The deviations ``δ_m = arg(C_m·conj(Σ C))`` are
    modelled as ``a_b + β·τ_m`` plus white noise — one offset per block and one common
    intra-block slope ``β`` (residual frequency left by the Δf derotation), ``τ_m`` the
    sub-block centre time relative to the block centre. The pooled residual variance
    ``σ_c²`` of one sub-block phase (``nb·M − nb − 1`` degrees of freedom) gives the block
    phase variance ``σ_c² / M`` (equal-power sub-blocks, noise uncorrelated between them —
    true for thermal noise and noise-like pilots, whose correlation time is a few samples).
    """
    sizes = [int(z.size) for z in blocks]
    m = min(_SUB_BLOCKS, min(sizes) // _SUB_MIN_LEN)
    nb = len(sizes)
    if m < _SUB_MIN_COUNT or nb * m - nb - 1 < 1:
        return math.nan
    dev: list[np.ndarray] = []
    tau: list[np.ndarray] = []
    for z in blocks:
        ln = z.size // m
        c = np.sum(np.asarray(z[:m * ln]).reshape(m, ln), axis=1)
        d = np.angle(c * np.conj(np.sum(c)))
        dev.append(d - d.mean())
        tau.append((np.arange(m, dtype=np.float64) - 0.5 * (m - 1)) * ln / fs)
    dv, tv = np.concatenate(dev), np.concatenate(tau)
    stt = float(np.sum(tv * tv))
    beta = float(np.sum(tv * dv)) / stt if stt > 0 else 0.0
    res = dv - beta * tv
    var_c = float(np.sum(res * res)) / (nb * m - nb - 1)
    return var_c / m


@dataclass
class CoherenceReport:
    """Coherence of channel ``channel`` relative to ``ref_channel`` (see module docstring).

    delay_samples          median per-block delay (``x1[n] ≈ x0[n − d]``)
    phase_deg              circular mean of the per-block phases ``arg(x1·conj(x0))``
    coherence              mean per-block coherence (delay compensated, no drift removal:
                           what a DF integration over one block sees)
    phase_drift_deg_per_s  d/dt of the phase (fine frequency + block-phase fit)
    drift_std_deg_per_s    standard error of the drift: the larger of the within-block
                           (sub-block phase scatter) and between-block (fit residual) estimates
    phase_std_deg          std of the block phases after removing the linear drift
    coherent               all three criteria met (drift against ``criteria
                           ["drift_threshold_deg_s"] = max(limit, k·drift_std_deg_per_s)``)
    note                   Turkish one-line verdict / reason
    """

    delay_samples: float
    phase_deg: float
    coherence: float
    phase_drift_deg_per_s: float
    phase_std_deg: float
    n_blocks: int
    coherent: bool
    note: str = ""
    drift_std_deg_per_s: float = math.nan
    freq_offset_hz: float = 0.0
    delay_std_samples: float = math.nan
    delay_resolvable: bool = False
    peak_corr: float = math.nan
    fs_hz: float = 0.0
    n: int = 0
    ref_channel: int = 0
    channel: int = 1
    t_ns: int = 0
    block_phases_deg: list[float] = field(default_factory=list)
    block_delays: list[float] = field(default_factory=list)
    block_coherence: list[float] = field(default_factory=list)
    criteria: dict[str, float] = field(default_factory=dict)
    pairs: list[dict[str, Any]] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        """JSON-safe dict (non-finite floats → None)."""
        def clean(v: Any) -> Any:
            if isinstance(v, float):
                return v if math.isfinite(v) else None
            if isinstance(v, list):
                return [clean(x) for x in v]
            if isinstance(v, dict):
                return {str(k): clean(x) for k, x in v.items()}
            return v
        return {f.name: clean(getattr(self, f.name)) for f in fields(self)}


def drift_threshold(limit_deg_s: float, drift_std_deg_s: float, k_sigma: float = DRIFT_K_SIGMA) -> float:
    """Effective drift limit ``max(limit, k·σ)`` (``limit`` alone when ``σ`` is not finite)."""
    lim = float(limit_deg_s)
    se = float(drift_std_deg_s)
    return max(lim, float(k_sigma) * se) if math.isfinite(se) else lim


def _verdict(drift: float, drift_se: float, coh: float, pstd: float, lim: dict[str, float],
             resolvable: bool) -> tuple[bool, str]:
    reasons: list[str] = []
    thr = lim["drift_threshold_deg_s"]
    widened = thr > lim["drift_deg_s"]
    if not math.isfinite(drift) or abs(drift) >= thr:
        bound = (f"±{thr:.3g} °/s = {lim['drift_k_sigma']:g}σ belirsizlik, sınır ±{lim['drift_deg_s']:g}" if widened
                 else f"sınır ±{lim['drift_deg_s']:g}")
        reasons.append(f"faz kayması {drift:+.3g} °/s ({bound}): kanallar ortak referans "
                       "saatini paylaşmıyor ya da referans kilitli değil — ortak 40 MHz referans gerekli")
    if not coh > lim["coherence_min"]:
        reasons.append(f"koherens {coh:.3f} < {lim['coherence_min']:g}: ortak sinyal yok/zayıf ya da faz blok "
                       "içinde dönüyor (kablolu kalibrasyon kaynağını ve seviyesini kontrol edin)")
    if not pstd < lim["phase_std_max_deg"]:
        reasons.append(f"faz std {pstd:.2f}° ≥ {lim['phase_std_max_deg']:g}°: faz kararsız")
    ok = not reasons
    if ok:
        msg = f"Koherent: faz kayması {drift:+.3f} °/s, koherens {coh:.3f}, faz std {pstd:.2f}°"
        if widened:
            msg += (f". Kayma belirsizliği σ = {drift_se:.2g} °/s: ±{lim['drift_deg_s']:g} °/s sınırı bu "
                    f"kayıtla doğrulanamadı (kayma ±{thr:.2g} °/s içinde = {lim['drift_k_sigma']:g}σ); daha uzun "
                    "yakalama (blok sayısı × örnek) ya da daha güçlü ortak sinyal kullanın")
    else:
        msg = "Koherent DEĞİL: " + "; ".join(reasons)
    if not resolvable:
        msg += (". Örnek gecikmesi çözülemedi (ortak sinyal dar bantlı): hizalama için geniş bantlı gürültü "
                "kaynağı kullanın")
    return ok, msg


def analyze_coherence(x0_blocks: Sequence[Any], x1_blocks: Sequence[Any], fs: float,
                      t_s: Sequence[float] | None = None, *, max_lag: int | None = None,
                      drift_limit_deg_s: float = DRIFT_LIMIT_DEG_S, coherence_min: float = COHERENCE_MIN,
                      phase_std_max_deg: float = PHASE_STD_MAX_DEG,
                      drift_k_sigma: float = DRIFT_K_SIGMA) -> CoherenceReport:
    """Coherence report from paired blocks (pure computation behind :func:`check_coherence`).

    ``t_s[b]`` is the time (s, sample clock) of the first sample of block ``b``; default
    ``b · n / fs`` (contiguous blocks). Per block: delay (:func:`estimate_delay_ex`; blocks
    with an unresolvable delay use the median of the resolvable ones, else 0), alignment,
    ``z = x1·conj(x0)``, coherence, raw phase and the frequency of ``z``
    (:func:`~eds.dsp.peaks.tone_frequency`). The drift is ``2π·Δf`` (median block
    frequency) plus the slope of the unwrapped, Δf-derotated block phases versus time;
    ``phase_std_deg`` is the residual of that linear fit. The drift's standard error is
    ``√(max(σ_res², σ_b²) / Σ(t − t̄)²)`` with ``σ_res²`` the fit residual variance and
    ``σ_b²`` the white-noise block-phase variance from :func:`_block_phase_var`; the drift
    criterion is ``|drift| < max(drift_limit_deg_s, drift_k_sigma · σ_drift)``
    (``criteria["drift_threshold_deg_s"]``; ``drift_k_sigma = 0`` applies the bare limit).
    """
    if (isinstance(drift_k_sigma, bool) or not isinstance(drift_k_sigma, (int, float))
            or not (math.isfinite(drift_k_sigma) and drift_k_sigma >= 0.0)):
        raise ConfigError(f"drift_k_sigma sonlu ve negatif olmayan bir sayı olmalı, {drift_k_sigma!r} geldi")
    k_sigma = float(drift_k_sigma)
    nb = len(x0_blocks)
    if nb != len(x1_blocks):
        raise ConfigError(f"x0/x1 blok sayıları farklı ({nb} / {len(x1_blocks)})")
    if nb < 3:
        raise ConfigError(f"Faz kayması kestirimi için en az 3 blok gerekli, {nb} geldi")
    fs = float(fs)
    if not (math.isfinite(fs) and fs > 0):
        raise ConfigError(f"fs pozitif olmalı, {fs!r} geldi")
    pairs = [_pair(a, b) for a, b in zip(x0_blocks, x1_blocks)]
    n = pairs[0][0].size
    if any(p[0].size != n for p in pairs):
        raise ConfigError("Tüm bloklar aynı uzunlukta olmalı")
    ts = np.arange(nb, dtype=np.float64) * n / fs if t_s is None else np.asarray(t_s, dtype=np.float64)
    if ts.shape != (nb,) or not np.all(np.isfinite(ts)) or np.any(np.diff(ts) <= 0):
        raise ConfigError("t_s blok sayısı kadar, sonlu ve artan olmalı")
    lim = {"drift_deg_s": float(drift_limit_deg_s), "coherence_min": float(coherence_min),
           "phase_std_max_deg": float(phase_std_max_deg)}

    dels = [estimate_delay_ex(a, b, fs, max_lag) for a, b in pairs]
    res_d = [d.delay_samples for d in dels if d.resolvable]
    # One delay for all blocks: the stream delay is constant over a contiguous capture, and
    # per-block estimates would turn their own noise into block-to-block phase jitter
    # (2π·f_bb·δd/fs) that masquerades as drift. A delay slip between blocks then shows up as
    # a coherence / phase-stability failure, which it is for DF.
    d_common = float(np.median(res_d)) if res_d else 0.0
    d_blk = [d.delay_samples if d.resolvable else d_common for d in dels]

    raw_ph: list[float] = []
    cohs: list[float] = []
    f_blk: list[float] = []
    zs: list[tuple[np.ndarray, int]] = []
    d = d_common
    for a, b in pairs:
        ba = align(b, d) if d != 0.0 else b
        sl = valid_slice(n, d) if d != 0.0 else slice(0, n)
        a_v, b_v = a[sl], ba[sl]
        z = b_v * np.conj(a_v)
        s = complex(np.sum(z))
        e = float(np.vdot(a_v, a_v).real) * float(np.vdot(b_v, b_v).real)
        if e <= 0.0:
            raise MeasurementError("Koherens: kanallardan birinde sinyal yok (sıfır enerji)")
        cohs.append(min(abs(s) / math.sqrt(e), 1.0))
        raw_ph.append(float(np.angle(s)))
        try:
            f_blk.append(float(tone_frequency(z, fs)))
        except MeasurementError as err:
            log.debug("koherens: blok frekansı bulunamadı: %s", err)
            f_blk.append(math.nan)
        zs.append((z, sl.start))
    finite_f = [f for f in f_blk if math.isfinite(f)]
    dfreq = float(np.median(finite_f)) if finite_f else 0.0

    tc = np.array([t0 + (start + 0.5 * (z.size - 1)) / fs for (z, start), t0 in zip(zs, ts)])
    tts = [t0 + (start + np.arange(z.size, dtype=np.float64)) / fs for (z, start), t0 in zip(zs, ts)]
    # The frequency is refined with the block-phase slope until the derotation leaves no
    # residual ramp: a block phase arg(Σ z e^{-j2πΔf t}) of a fluctuating-envelope (noise-like)
    # pilot is the ramp at the block's power centroid, not at its centre, so an uncorrected
    # Δf error adds block-to-block phase jitter that the within-block scatter does not see.
    for it in range(_DRIFT_REFINE_ITER):
        derot = [z * np.exp(-2j * np.pi * dfreq * tt) for (z, _s), tt in zip(zs, tts)]
        unw = np.unwrap([float(np.angle(np.sum(zd))) for zd in derot])
        slope, icept = np.polyfit(tc - tc[0], unw, 1)
        if it == _DRIFT_REFINE_ITER - 1 or abs(slope) * float(tc[-1] - tc[0]) < _DRIFT_REFINE_TOL_RAD:
            break
        dfreq += float(slope) / (2.0 * np.pi)
    resid = unw - (slope * (tc - tc[0]) + icept)
    var_res = float(np.sum(resid ** 2)) / (nb - 2)              # 2 fitted parameters, nb >= 3
    pstd = float(np.degrees(math.sqrt(var_res)))
    sxx = float(np.sum((tc - tc.mean()) ** 2))
    var_white = _block_phase_var(derot, fs)
    var_slope = max(var_res, var_white) if math.isfinite(var_white) else var_res
    slope_se = float(np.degrees(math.sqrt(var_slope / sxx))) if sxx > 0 else math.nan
    drift = float(np.degrees(2.0 * np.pi * dfreq + slope))
    coh = float(np.mean(cohs))
    phase_deg = float(np.degrees(np.angle(np.sum(np.exp(1j * np.asarray(raw_ph))))))
    resolvable = bool(res_d)
    lim["drift_k_sigma"] = k_sigma
    lim["drift_threshold_deg_s"] = drift_threshold(lim["drift_deg_s"], slope_se, k_sigma)
    ok, note = _verdict(drift, slope_se, coh, pstd, lim, resolvable)
    d_std = [d.delay_std_samples for d in dels if d.resolvable and math.isfinite(d.delay_std_samples)]
    return CoherenceReport(
        delay_samples=d_common, phase_deg=phase_deg, coherence=coh,
        phase_drift_deg_per_s=drift, phase_std_deg=pstd, n_blocks=nb, coherent=ok, note=note,
        drift_std_deg_per_s=slope_se, freq_offset_hz=float(drift / 360.0),
        delay_std_samples=float(np.median(d_std)) if d_std else math.nan,
        delay_resolvable=resolvable, peak_corr=float(np.median([d.peak_corr for d in dels])), fs_hz=fs, n=n,
        block_phases_deg=[float(v) for v in np.degrees(raw_ph)], block_delays=[float(v) for v in d_blk],
        block_coherence=[float(v) for v in cohs], criteria=lim,
    )


def check_coherence(device: Any, n: int = 65536, blocks: int = 8, *, ref_channel: int = 0,
                    channels: Sequence[int] | None = None, max_lag: int | None = None,
                    drift_limit_deg_s: float = DRIFT_LIMIT_DEG_S, coherence_min: float = COHERENCE_MIN,
                    phase_std_max_deg: float = PHASE_STD_MAX_DEG, timeout_s: float = 2.0,
                    max_attempts: int = 3, drift_k_sigma: float = DRIFT_K_SIGMA) -> CoherenceReport:
    """Measure the coherence of a ≥ 2-channel device (``DeviceGroup`` or coherent receiver).

    A common signal must reach every channel (cabled noise source / generator through a
    splitter, or the simulator's ``cabled`` emitter). ``blocks`` contiguous blocks of ``n``
    samples are captured (the first one settled); an overflow or settling block inside the
    sequence restarts it (alignment lost), up to ``max_attempts``. Each channel in
    ``channels`` (default: all except ``ref_channel``) is analysed against the reference
    with :func:`analyze_coherence`. The returned report is the worst pair (non-coherent
    first, then lowest coherence) with ``coherent = all pairs coherent``; with more than
    one pair every pair's report is in ``pairs``. The stream is stopped again if it was not
    running before. Inside :func:`collect_reports` the returned report is also appended to
    the collector.
    """
    nch = int(device.num_channels)
    if nch < 2:
        raise ConfigError(f"{device.name}: koherens kontrolü en az 2 RX kanalı gerektirir ({nch} kanal var)")
    if isinstance(n, bool) or int(n) < 256:
        raise ConfigError(f"n en az 256 olmalı, {n!r} geldi")
    if isinstance(blocks, bool) or int(blocks) < 3:
        raise ConfigError(f"blocks en az 3 olmalı, {blocks!r} geldi")
    n, blocks = int(n), int(blocks)
    ref = int(ref_channel)
    if not 0 <= ref < nch:
        raise ConfigError(f"Referans kanal {ref} yok ({nch} kanal)")
    chans = [c for c in (range(nch) if channels is None else channels) if int(c) != ref]
    if not chans or any(not 0 <= int(c) < nch for c in chans):
        raise ConfigError(f"Geçersiz kanal listesi {list(channels or [])} ({nch} kanal, referans {ref})")
    was_streaming = bool(device.streaming)
    blks: list[Any] = []
    t_ns = now_ns()
    try:
        for attempt in range(max(1, int(max_attempts))):
            first = device.capture(n, timeout_s)
            blks = [first]
            t_ns = int(first.t_ns)
            for _ in range(blocks - 1):
                b = device.read(n, timeout_s)
                if b.overflow or b.settling:
                    log.warning("%s: koherens yakalamasında %s (deneme %d): hizalama kayboldu, yeniden başlatılıyor",
                                device.name, "overflow" if b.overflow else "settling", attempt + 1)
                    blks = []
                    break
                blks.append(b)
            if blks:
                break
        else:
            raise MeasurementError(
                f"{device.name}: {max_attempts} denemede kesintisiz {blocks}×{n} örnek alınamadı (overflow): "
                "örnekleme hızını düşürün ya da USB bağlantısını kontrol edin.")
    finally:
        if not was_streaming and device.streaming:
            device.stop_stream()
    fs = float(blks[0].fs)
    t_s = [(b.index - blks[0].index) / fs for b in blks]
    reports = []
    for c in chans:
        rep = analyze_coherence([b.samples[ref] for b in blks], [b.samples[int(c)] for b in blks], fs, t_s,
                                max_lag=max_lag, drift_limit_deg_s=drift_limit_deg_s, coherence_min=coherence_min,
                                phase_std_max_deg=phase_std_max_deg, drift_k_sigma=drift_k_sigma)
        rep.ref_channel, rep.channel, rep.t_ns = ref, int(c), t_ns
        reports.append(rep)
    main = sorted(reports, key=lambda r: (r.coherent, r.coherence))[0]
    if len(reports) > 1:
        main.pairs = [r.to_dict() for r in reports]
        main.coherent = all(r.coherent for r in reports)
    log.info("%s: kanal %d↔%d: %s", device.name, main.ref_channel, main.channel, main.note)
    sink = _REPORT_SINK.get()
    if sink is not None:
        sink.append(main)
    return main


@contextlib.contextmanager
def collect_reports() -> Iterator[list[CoherenceReport]]:
    """Collect every report :func:`check_coherence` returns inside the ``with`` block (same
    thread / context), for a caller that does not get the report back itself — e.g. the
    ``eds cal coherence`` exit code (3 = not coherent) set around the subcommand handler::

        with collect_reports() as reports:
            run_something_that_calls_check_coherence()
        coherent = bool(reports) and all(r.coherent for r in reports)
    """
    sink: list[CoherenceReport] = []
    token = _REPORT_SINK.set(sink)
    try:
        yield sink
    finally:
        _REPORT_SINK.reset(token)
