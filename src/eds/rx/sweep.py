"""Wideband scanning (AD9363: 325 MHz – 3.8 GHz) with a <= 20 MHz instantaneous receiver.

A Pluto sees at most ~20 MHz at once, so the band is covered by retuning the LO in steps,
taking one short *burst* per step, and stitching the per-step power spectra on one global
frequency grid. :class:`Sweeper` does exactly that and returns a :class:`SweepResult`
(stitched spectrum, CFAR detections, per-step timing → the KTR "scan speed" figure).

Step plan (:meth:`Sweeper.plan`)
--------------------------------
``df = fs / nfft`` (``nfft = cfg.nfft`` or :func:`~eds.dsp.psd.nfft_for_rbw` when
``cfg.rbw_hz > 0``). Only the central ``U = usable_fraction · fs`` of each step is used
(``K_u = floor(usable_fraction · nfft / 2)`` bins each side of DC — flat analog response,
alias-free). With ``dc_avoid`` the step is ``U/2`` (50 % overlap): every frequency then lies
in two steps and is at least ``U/4`` from the DC of one of them, so the zero-IF DC spike
(LO leakage) of every step can be replaced by a neighbour's clean bins. Without
``dc_avoid`` the step is ``U`` (twice as fast, DC bins end up in the result and are only
marked invalid for detection). The step is an integer number of bins and every centre is
``start + m · df`` (integer ``m``), so all per-step FFT grids coincide with the global grid
``start + j · df`` (clipped to ``[start, stop]``). Every centre lies inside the profile's
official RF range (325 MHz – 3.8 GHz for the AD9363): the first/last centres are pulled
inward when needed and the spacing is redistributed (never larger than the nominal step)
so coverage is preserved; the plan is verified to have no gaps.

Burst capture: why the sweep may use 20 MS/s although USB 2.0 cannot stream it
-------------------------------------------------------------------------------
Continuous streaming of 20 MS/s × 4 B (CS16 I+Q) = 80 MB/s exceeds USB 2.0 (53 MB/s
theoretical, ~24–32 MB/s in practice) — a continuous 20 MS/s stream overflows constantly.
A sweep step, however, needs only ``n_cap = nfft + (averages − 1) · hop`` *contiguous*
samples (5120 samples = 256 µs for the defaults). Inside the Pluto the AD9363 → FPGA → DMA
path fills each libiio kernel buffer at the full sample rate in the Pluto's own memory; the
buffer crosses USB only afterwards. Samples inside one buffer are therefore always
contiguous — a USB bottleneck only creates gaps *between* buffers. The sweeper therefore
(re)starts the stream with ``buffer_size = max(4096, n_cap rounded up to 1024)`` and keeps
every read **buffer-aligned**: the settle flush discards whole buffers and each capture is
exactly one buffer (the first ``n_cap`` samples are analysed). A ``readStream`` never
spans two libiio buffers, so a capture can never straddle an inter-buffer gap even though
the driver does not report USB overflows. Overflows between steps are recorded per step
(``overflow``) and are expected and harmless, because each step is analysed on its own.
(The profile's "USB theoretical limit" warning for 20 MS/s is logged at INFO level for
this reason.)

Settling and stale buffers
--------------------------
After a retune (or gain change) libiio still holds ``kernel_buffers`` buffers recorded with
the OLD settings. The HAL flags ``settle_buffers × buffer_size`` samples as ``settling``
(``cfg.settle_buffers``; ``-1`` = ``kernel_buffers + 1``) and :meth:`RxDevice.flush` reads
and discards them (timed as ``settle``); :meth:`RxDevice.capture` additionally refuses any
block still flagged settling. Without this an emitter of step *k* would appear as a ghost in
step *k+1* at ``f + step``. The settle cost is proportional to ``buffer_size``, which is
the second reason for the small burst buffer (5 × 5120 instead of 5 × 65536 samples per
step with the defaults).

Gain
----
``gain_mode = "fixed"``: ``cfg.gain_db`` (clamped to each band's range, e.g. 71 dB max above
1.3 GHz on the AD9363). ``"auto"``: per step, the burst's time-domain levels are evaluated
with the :class:`~eds.rx.agc.GainController` control law (fast attack on clipping/high
peak, one-shot raise when too low), at most :data:`AUTO_GAIN_MAX_ITER` gain changes per
step, each followed by a new settle; the gain carries over to the next step. Every step
records its gain. Uncalibrated spectra are normalized to the reference gain
``gain_ref = cfg.gain_db`` (``+ gain_ref − gain_step`` dB) and carry the unit
``"dBFS@<gain_ref>dB"`` — the level that would have been read at ``gain_ref`` with a linear
gain stage. A ``calibrate`` callable that returns ``unit == "dBm"`` spectra makes the
result absolute (no normalization, the calibration already includes the gain).

Stitching
---------
For every global bin the contributing step with the largest weight ``U/2 − |f_bb|``
(distance to the usable edge, i.e. the step whose centre is nearest: flattest part of the
analog low-pass and of the alias-free band) wins, considering only bins inside the usable
band and outside the DC notch. The effective notch is ``max(dsp.dc_notch_bins,
window_terms − 1)``: a DC component leaks into exactly ``±(terms − 1)`` bins of a periodic
cosine window (Blackman-Harris: ±3). Notch bins are used only where no other step covers the
frequency (``dc_avoid = False``) and are then excluded from detection.

Detection runs :func:`eds.dsp.detect.detect` once on the stitched spectrum with
``n_avg`` = effective Welch averages (``cfg.averages`` segments at ``dsp.overlap``) and the
validity mask (DC-notch / uncovered bins excluded). The CFAR is narrowband: a flat signal
much wider than its training window (``2 · train_bins`` bins, ~0.6 MHz at the default
9.8 kHz bins) masks itself, so wideband channels (LTE, Wi-Fi) are quantified with
:meth:`SweepResult.band_power_db` rather than relied upon as detections.

Scan speed
----------
``duration_s`` is the wall-clock time from the first retune to the end of stitching and
detection (stream start-up excluded); ``rate_hz_per_s = (stop − start) / duration_s``.
``timing`` holds the totals of ``tune / settle / capture / dsp`` (the final stitching and
detection are counted in ``dsp``) plus ``other_s`` (loop overhead, ``on_step`` callbacks).

Future optimization: AD9361 fastlock profiles
---------------------------------------------
A normal AD9361 retune runs the RF VCO calibration and costs hundreds of µs plus the
attribute-write latency over USB. The chip can store up to 8 RX synthesizer "fastlock"
profiles (``ad9361-phy`` ``fastlock_store`` / ``fastlock_recall``) and recall one in tens of
µs without recalibration. A rolling store/recall scheme through the thin libiio layer
(:mod:`eds.hal.pluto_iio`) would shrink ``t_tune`` for repeated sweeps; it is not used yet
because SoapyPlutoSDR does not expose it and, with the default buffers, ``t_settle``
dominates the step time.

Device state contract: :meth:`Sweeper.run` sets the sweep sample rate, bandwidth and manual
gain mode and leaves the radio tuned to the last step at the last step's gain. The stream is
always (re)started for the sweep (buffer alignment, burst buffer size); afterwards the
streaming state it found is restored (a running stream is restarted with its previous
buffer size, which also resets ``device.stats``), as are ``settle_buffers`` and the gain
mode.
"""

from __future__ import annotations

import logging
import math
import threading
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

import numpy as np

from ..core.config import AgcConfig, DetectConfig, DspConfig, SweepConfig
from ..core.errors import ConfigError, MeasurementError, ProfileViolation
from ..core.timeutil import mono, now_ns
from ..core.units import db10, fmt_hz, lin10
from ..dsp.detect import Detection, detect
from ..dsp.levels import iq_levels
from ..dsp.psd import NFFT_MAX, NFFT_MIN, Spectrum, nfft_for_rbw, welch
from ..dsp.stats import effective_averages
from ..dsp.windows import WINDOW_COEFFS, canonical_window_name, window_info
from ..hal.base import RxDevice
from .agc import GainController

log = logging.getLogger(__name__)

#: ``calibrate(spectrum) -> spectrum`` (e.g. ppm correction + dBm via eds.calib).
Calibrate = Callable[[Spectrum], Spectrum]
#: ``on_step(step_index, step_record, step_spectrum)``; returning ``False`` aborts the sweep.
OnStep = Callable[[int, dict[str, Any], Spectrum], bool | None]

#: Smallest stream buffer used for bursts (per-buffer USB/libiio overhead).
MIN_BUFFER_SIZE: int = 4096
#: Burst buffers are a multiple of this many samples (4 KiB of CS16 = whole USB packets).
BUFFER_GRANULE: int = 1024
#: Maximum number of gain changes per step in ``gain_mode = "auto"``.
AUTO_GAIN_MAX_ITER: int = 2
#: Decimals of the dB values published on the bus (0.01 dB resolution).
VALUE_DECIMALS: int = 2
#: Decimation modes of :meth:`SweepResult.decimate`.
DECIMATE_MODES: tuple[str, ...] = ("max", "mean", "min")

_GAIN_TOL_DB = 1e-6
_EPS_BINS = 1e-9


def _fmt_gain(g: float) -> str:
    v = round(float(g), 2)
    if v == 0.0:
        v = 0.0                         # no "-0"
    return f"{v:.2f}".rstrip("0").rstrip(".")


def gain_referred_unit(gain_ref_db: float) -> str:
    """Unit label of a gain-normalized, uncalibrated sweep, e.g. ``"dBFS@50dB"``."""
    return f"dBFS@{_fmt_gain(gain_ref_db)}dB"


# =============================================================================== plan
def plan_offsets(n_grid: int, k_usable: int, notch: int, step_bins: int, dc_avoid: bool,
                 m_min: int, m_max: int) -> np.ndarray:
    """Step centres as integer bin offsets ``m`` from the first grid bin (see module doc).

    The grid has bins ``0 .. n_grid - 1``; a step centred at ``m`` uses grid bins ``m + o``
    with ``|o| <= k_usable`` and, if ``dc_avoid``, ``|o| > notch``. Centres are restricted to
    ``[m_min, m_max]`` (official RF range). Raises :class:`ConfigError` if no gap-free plan
    exists (degenerate ranges only).
    """
    j_last = n_grid - 1
    s = int(step_bins)
    if dc_avoid:
        half = k_usable - notch                  # usable offsets on one side of the notch
        if n_grid <= half:
            # The whole range fits in one side band: one step, band midway between DC and edge.
            slack = half - n_grid
            for m in (-(notch + 1) - slack // 2, j_last + notch + 1 + slack // 2):
                if m_min <= m <= m_max:
                    return np.array([m], dtype=np.int64)
        n = max(2, math.ceil(j_last / s) + 1)
        excess = (n - 1) * s - j_last
        m0 = max(-(excess // 2), m_min)                         # m0 <= 0: grid bin 0 not in a notch
        m1 = min(j_last + (excess - excess // 2), m_max)       # m1 >= j_last
    else:
        n = max(1, math.ceil((j_last - 2 * k_usable) / s) + 1)
        if n == 1:
            m = j_last // 2
            return np.array([min(max(m, m_min), m_max)], dtype=np.int64)
        excess = (n - 1) * s + 2 * k_usable - j_last
        m0 = max(k_usable - excess // 2, m_min)
        m1 = min(j_last - k_usable + (excess - excess // 2), m_max)
    offs = np.rint(np.linspace(m0, m1, n)).astype(np.int64)
    offs = np.unique(offs)
    cov = plan_coverage(offs, n_grid, k_usable, notch, allow_notch=not dc_avoid)
    if not np.all(cov):
        gap = int(np.flatnonzero(~cov)[0])
        raise ConfigError(f"Tarama planı boşluksuz kurulamadı (ilk boşluk: {gap}. bin); "
                          "aralık, cihazın resmî frekans aralığına göre çok dar/kenarda")
    return offs


def plan_coverage(offsets: np.ndarray, n_grid: int, k_usable: int, notch: int,
                  allow_notch: bool) -> np.ndarray:
    """Boolean mask of grid bins covered by the steps at ``offsets`` (usable bins only; DC
    notch bins count only if ``allow_notch``)."""
    acc = np.zeros(n_grid + 1, dtype=np.int64)

    def mark(lo: int, hi: int) -> None:
        lo, hi = max(lo, 0), min(hi, n_grid - 1)
        if lo <= hi:
            acc[lo] += 1
            acc[hi + 1] -= 1

    for m in np.asarray(offsets, dtype=np.int64).tolist():
        if allow_notch or notch < 0:
            mark(m - k_usable, m + k_usable)
        else:
            mark(m - k_usable, m - notch - 1)
            mark(m + notch + 1, m + k_usable)
    return np.cumsum(acc[:-1]) > 0


# =============================================================================== result
@dataclass
class SweepResult:
    """Stitched sweep spectrum + detections + timing (see the module docstring).

    ``f_hz`` is the uniform global grid ``start_hz + j · df_hz``; ``p_db`` the stitched power
    per bin in ``unit`` (``"dBm"`` or ``"dBFS@<g>dB"``); ``valid`` marks bins that came from a
    clean (non-DC) bin of some step; ``source_step`` the index of the step each bin came
    from (-1 = not covered, filled by interpolation). ``steps`` holds one record per step:
    ``fc, gain_db, t_tune, t_settle, t_capture, t_dsp, overflow`` (+ ``index, peak_dbfs,
    clip_fraction, gain_iters, t_ns, bins_used``).
    """

    f_hz: np.ndarray
    p_db: np.ndarray
    unit: str
    calibrated: bool
    rbw_hz: float
    n_steps: int
    step_hz: float
    steps: list[dict[str, Any]]
    duration_s: float
    rate_hz_per_s: float
    timing: dict[str, float]
    detections: list[Detection]
    t_start_ns: int
    t_end_ns: int
    sweep_id: int
    start_hz: float = 0.0
    stop_hz: float = 0.0
    df_hz: float = 0.0
    fs_hz: float = 0.0
    nfft: int = 0
    window: str = "blackmanharris"
    enbw_bins: float = 1.0
    averages: int = 1
    gain_ref_db: float = 0.0
    gain_mode: str = "fixed"
    valid: np.ndarray = field(default_factory=lambda: np.zeros(0, dtype=bool))
    source_step: np.ndarray = field(default_factory=lambda: np.zeros(0, dtype=np.int64))
    threshold_db: np.ndarray = field(default_factory=lambda: np.zeros(0))

    @property
    def n_bins(self) -> int:
        return int(self.p_db.size)

    @property
    def noise_floor_db(self) -> float:
        """Median of ``p_db`` over the valid bins (``unit``, per RBW)."""
        p = self.p_db[self.valid] if self.valid.size == self.p_db.size and np.any(self.valid) else self.p_db
        return float(np.median(p))

    @property
    def clipped_steps(self) -> list[int]:
        """Indices of steps whose final burst had clipped samples."""
        return [int(s["index"]) for s in self.steps if float(s.get("clip_fraction", 0.0)) > 0.0]

    def band_power_db(self, f_lo: float, f_hi: float) -> float:
        """``10 log10(sum P[k] / ENBW_bins)`` over grid bins in ``[f_lo, f_hi]`` (tone/channel
        power, scalloping-free; ARCHITECTURE §2)."""
        f_lo, f_hi = float(f_lo), float(f_hi)
        if not (math.isfinite(f_lo) and math.isfinite(f_hi)) or f_hi < f_lo:
            raise ConfigError(f"Geçersiz bant: [{f_lo!r}, {f_hi!r}] Hz")
        m = (self.f_hz >= f_lo) & (self.f_hz <= f_hi)
        if not np.any(m):
            raise MeasurementError(f"[{fmt_hz(f_lo)}, {fmt_hz(f_hi)}] bandında tarama bini yok")
        return float(db10(np.sum(lin10(self.p_db[m])) / self.enbw_bins))

    def decimate(self, max_points: int, mode: str = "max") -> tuple[np.ndarray, np.ndarray]:
        """Reduce to at most ``max_points`` points of ``D = ceil(n / max_points)`` bins each.

        ``mode``: ``"max"`` (max-hold, peak preserving — a narrow emitter never disappears),
        ``"mean"`` (power average) or ``"min"``. Returned frequencies are the centres of full
        ``D``-bin groups on a uniform axis ``f[0] + (i·D + (D−1)/2)·df`` (the last group may be
        partial).
        """
        if isinstance(max_points, bool) or int(max_points) != max_points or int(max_points) < 1:
            raise ConfigError(f"max_points pozitif tamsayı olmalı, {max_points!r} geldi")
        if mode not in DECIMATE_MODES:
            raise ConfigError(f"Desimasyon türü {mode!r} geçersiz; seçenekler: {list(DECIMATE_MODES)}")
        d = self.decimation_for(int(max_points))
        if d == 1:
            return self.f_hz.copy(), self.p_db.copy()
        n = self.p_db.size
        m = math.ceil(n / d)
        full = (n // d) * d
        p = self.p_db
        head = p[:full].reshape(-1, d)
        tail = p[full:]
        if mode == "max":
            out = head.max(axis=1)
            last = tail.max() if tail.size else None
        elif mode == "min":
            out = head.min(axis=1)
            last = tail.min() if tail.size else None
        else:
            out = db10(np.mean(lin10(head), axis=1))
            last = float(db10(np.mean(lin10(tail)))) if tail.size else None
        if last is not None:
            out = np.append(out, last)
        f = self.f_hz[0] + (np.arange(m) * d + (d - 1) / 2.0) * self.df_hz
        return f, np.asarray(out, dtype=np.float64)

    def decimation_for(self, max_points: int) -> int:
        """Bins per published point for ``max_points``."""
        return max(1, math.ceil(self.p_db.size / max(int(max_points), 1)))

    def to_spectrum(self) -> Spectrum:
        """The stitched spectrum as a :class:`~eds.dsp.psd.Spectrum` (copies data).

        ``fc`` is the grid centre and the DC notch is disabled (``meta['dc_notch_bins'] = -1``)
        because the stitched grid has no DC; ``meta['valid']`` holds the validity mask."""
        n = self.f_hz.size
        return Spectrum(
            f_hz=self.f_hz.copy(), p_lin=lin10(self.p_db), fc=float(self.f_hz[n // 2]), fs=self.fs_hz,
            nfft=self.nfft, window=self.window, enbw_bins=self.enbw_bins, averages=self.averages,
            unit=self.unit, t_ns=self.t_start_ns, gain_db=(self.gain_ref_db,), overflow=False,
            meta={"stitched": True, "sweep_id": self.sweep_id, "avg": "mean", "dc_notch_bins": -1,
                  "usable_fraction": 1.0, "valid": self.valid.copy(), "n_steps": self.n_steps},
        )

    def to_bus_payload(self, device_name: str, max_points: int = 8192) -> dict[str, Any]:
        """``rx.sweep`` message data (contracts/schemas/rx.sweep.schema.json): max-hold
        decimated values (0.01 dB), detections and the timing budget."""
        if not isinstance(device_name, str) or not device_name:
            raise ConfigError("device_name boş olmayan bir metin olmalı")
        d = self.decimation_for(int(max_points))
        f, p = self.decimate(int(max_points), "max")
        return {
            "device": device_name,
            "sweep_id": int(self.sweep_id),
            "t_start_ns": int(self.t_start_ns),
            "t_end_ns": int(self.t_end_ns),
            "f_start_hz": float(self.start_hz),
            "f_stop_hz": float(self.stop_hz),
            "rbw_hz": float(self.rbw_hz),
            "n_steps": int(self.n_steps),
            "step_hz": float(self.step_hz),
            "duration_s": float(self.duration_s),
            "rate_hz_per_s": float(self.rate_hz_per_s),
            "unit": self.unit,
            "calibrated": bool(self.calibrated),
            "decimation": int(d),
            "f_point_start_hz": float(f[0]),
            "f_point_step_hz": float(d * self.df_hz),
            "values": [round(float(v), VALUE_DECIMALS) for v in p],
            "detections": [det.to_dict() for det in self.detections],
            "timing": {k: float(self.timing[k]) for k in ("tune_s", "settle_s", "capture_s", "dsp_s")},
        }


# =============================================================================== stitcher
class _Stitcher:
    """Per-bin best-weight selection on the global grid (see module docstring)."""

    def __init__(self, start_hz: float, df_hz: float, n_grid: int, k_usable: int, notch: int):
        self.start = float(start_hz)
        self.df = float(df_hz)
        self.n = int(n_grid)
        self.k_u = int(k_usable)
        self.notch = int(notch)
        self.w = np.full(self.n, -np.inf)
        self.p = np.zeros(self.n)
        self.src = np.full(self.n, -1, dtype=np.int64)
        self.dc = np.zeros(self.n, dtype=bool)

    def add(self, step: int, spec: Spectrum, offset_db: float) -> None:
        a = np.abs(spec.dc_offset_bins)
        j = np.rint((spec.f_hz - self.start) / self.df).astype(np.int64)
        use = (a <= self.k_u) & (j >= 0) & (j < self.n)
        if not np.any(use):
            return
        a = a[use]
        j = j[use]
        w = (self.k_u - a).astype(np.float64)
        in_notch = a <= self.notch
        w[in_notch] -= 2.0 * (self.k_u + 1)          # notch bins lose against any clean bin
        better = w > self.w[j]
        jb = j[better]
        self.w[jb] = w[better]
        self.p[jb] = spec.p_db[use][better] + float(offset_db)
        self.src[jb] = step
        self.dc[jb] = in_notch[better]

    def result(self) -> tuple[np.ndarray, np.ndarray, np.ndarray, np.ndarray]:
        covered = self.src >= 0
        if not np.any(covered):
            raise MeasurementError("Tarama sonucu boş: hiçbir adım tarama aralığına bin katmadı")
        p = self.p.copy()
        if not np.all(covered):
            idx = np.flatnonzero(covered)
            holes = np.flatnonzero(~covered)
            p[holes] = np.interp(holes, idx, p[idx])
            # Typical cause: a ppm-corrected axis has bin spacing df·(1+e), so nearest-bin
            # mapping skips a grid bin every ~1/e bins. Holes stay marked (valid = False,
            # source_step = -1) and never produce detections.
            log.info("Tarama birleştirmesinde %d bin kapsanmadı (komşulardan dolduruldu, tespit dışı)", holes.size)
        f = self.start + np.arange(self.n, dtype=np.float64) * self.df
        return f, p, covered & ~self.dc, self.src.copy()


# =============================================================================== sweeper
class Sweeper:
    """Step-and-stitch wideband scanner for one :class:`~eds.hal.base.RxDevice`.

    ``calibrate(spec) -> spec`` is applied to every step spectrum (default: identity, unit
    ``dBFS``); a result in ``dBm`` makes the sweep calibrated. ``agc`` configures the control
    law of ``gain_mode = "auto"`` (default :class:`~eds.core.config.AgcConfig`). ``timeout_s``
    is the read timeout of every flush/capture. Multi-channel devices are swept on channel 0.
    """

    def __init__(self, device: RxDevice, cfg: SweepConfig, dsp: DspConfig, detect: DetectConfig,
                 calibrate: Calibrate | None = None, agc: AgcConfig | None = None, *,
                 timeout_s: float = 2.0):
        if calibrate is not None and not callable(calibrate):
            raise ConfigError("calibrate çağrılabilir (callable) olmalı: calibrate(spectrum) -> spectrum")
        if not (isinstance(timeout_s, (int, float)) and math.isfinite(timeout_s) and timeout_s > 0):
            raise ConfigError(f"timeout_s pozitif sonlu bir sayı olmalı, {timeout_s!r} geldi")
        self._dev = device
        self._cfg = cfg
        self._dsp = dsp
        self._det = detect
        self._calibrate = calibrate
        self._agc_cfg = agc if agc is not None else AgcConfig()
        self._gc = GainController(device, self._agc_cfg)
        self._timeout_s = float(timeout_s)
        self._abort = threading.Event()
        self._run_lock = threading.Lock()
        self._sweep_id = 0
        self._derive()
        self._offsets = plan_offsets(self._n_grid, self._k_u, self._notch, self._step_bins, self._dc_avoid,
                                     self._m_min, self._m_max)

    # ------------------------------------------------------------------ configuration
    def _derive(self) -> None:
        cfg, dsp, prof = self._cfg, self._dsp, self._dev.profile
        nums = {"start_hz": cfg.start_hz, "stop_hz": cfg.stop_hz, "sample_rate_hz": cfg.sample_rate_hz,
                "bandwidth_hz": cfg.bandwidth_hz, "gain_db": cfg.gain_db, "rbw_hz": cfg.rbw_hz,
                "usable_fraction": cfg.usable_fraction}
        for k, v in nums.items():
            if isinstance(v, bool) or not isinstance(v, (int, float)) or not math.isfinite(float(v)):
                raise ConfigError(f"sweep.{k} sonlu bir sayı olmalı, {v!r} geldi")
        start, stop = float(cfg.start_hz), float(cfg.stop_hz)
        if not start < stop:
            raise ConfigError(f"sweep.start_hz ({fmt_hz(start)}) sweep.stop_hz'den ({fmt_hz(stop)}) küçük olmalı")
        errs: list[str] = []
        if start < prof.freq_min_hz or stop > prof.freq_max_hz:
            errs.append(f"Tarama aralığı [{fmt_hz(start)} – {fmt_hz(stop)}] {prof.chip} resmî frekans aralığı "
                        f"dışında [{fmt_hz(prof.freq_min_hz)} – {fmt_hz(prof.freq_max_hz)}]")
        fs, bw = float(cfg.sample_rate_hz), float(cfg.bandwidth_hz)
        e2, warns = prof.check_rx(sample_rate_hz=fs, bandwidth_hz=bw, n_channels=1)
        errs.extend(e2)
        g_lo, g_hi = self._gain_span(start, stop)
        if not (g_lo <= float(cfg.gain_db) <= g_hi):
            errs.append(f"sweep.gain_db = {float(cfg.gain_db):g} dB, tarama bandı için [{g_lo:g} – {g_hi:g} dB] "
                        "aralığı dışında")
        if errs:
            raise ProfileViolation(errs)
        for w in warns:
            log.info("%s (tarama burst yakalar; adımlar arası USB taşması sonucu etkilemez): %s", self._dev.name, w)
        if cfg.gain_mode not in ("fixed", "auto"):
            raise ConfigError(f"sweep.gain_mode = {cfg.gain_mode!r} geçersiz; seçenekler: ['auto', 'fixed']")
        if isinstance(cfg.averages, bool) or int(cfg.averages) != cfg.averages or int(cfg.averages) < 1:
            raise ConfigError(f"sweep.averages pozitif tamsayı olmalı, {cfg.averages!r} geldi")
        frac = float(cfg.usable_fraction)
        if not (0.0 < frac <= 1.0):
            raise ConfigError(f"sweep.usable_fraction (0, 1] aralığında olmalı, {frac!r} geldi")
        overlap = float(dsp.overlap)
        if not (0.0 <= overlap < 1.0):
            raise ConfigError(f"dsp.overlap [0, 1) aralığında olmalı, {dsp.overlap!r} geldi")
        if isinstance(cfg.settle_buffers, bool) or int(cfg.settle_buffers) != cfg.settle_buffers:
            raise ConfigError(f"sweep.settle_buffers tamsayı olmalı, {cfg.settle_buffers!r} geldi")
        window = canonical_window_name(cfg.window)
        if float(cfg.rbw_hz) > 0.0:
            nfft = nfft_for_rbw(float(cfg.rbw_hz), fs, window)
        else:
            if isinstance(cfg.nfft, bool) or int(cfg.nfft) != cfg.nfft or not (NFFT_MIN <= int(cfg.nfft) <= NFFT_MAX):
                raise ConfigError(f"sweep.nfft [{NFFT_MIN}, {NFFT_MAX}] aralığında tamsayı olmalı, {cfg.nfft!r} geldi")
            nfft = int(cfg.nfft)
        df = fs / nfft
        k_u = int(math.floor(frac * nfft / 2.0 + _EPS_BINS))
        notch = max(int(dsp.dc_notch_bins), len(WINDOW_COEFFS[window]) - 1)
        dc_avoid = bool(cfg.dc_avoid)
        if k_u < 1:
            raise ConfigError(f"Kullanılabilir bant {k_u} bin: sweep.usable_fraction/nfft çok küçük")
        if dc_avoid and k_u < 3 * notch + 2:
            raise ConfigError(f"DC kaçınma için kullanılabilir yarı bant ({k_u} bin) DC çentiğine ({notch} bin) göre "
                              f"çok dar; en az {3 * notch + 2} bin gerekli (nfft veya usable_fraction artırın)")
        step_bins = k_u if dc_avoid else min(int(math.floor(frac * nfft + _EPS_BINS)), 2 * k_u + 1)
        n_grid = int(math.floor((stop - start) / df + _EPS_BINS)) + 1
        min_bins = 2 * int(self._det.guard_bins) + 3
        if n_grid < min_bins:
            raise ConfigError(f"Tarama aralığı {n_grid} bin ({fmt_hz(stop - start)}); CFAR tespiti için en az "
                              f"{min_bins} bin ({fmt_hz(min_bins * df)}) gerekli")
        hop = max(1, int(round(nfft * (1.0 - overlap))))
        n_cap = nfft + (int(cfg.averages) - 1) * hop
        self._start, self._stop = start, stop
        self._fs, self._bw = fs, bw
        self._window = window
        self._nfft = nfft
        self._df = df
        self._k_u = k_u
        self._notch = notch
        self._dc_avoid = dc_avoid
        self._step_bins = step_bins
        self._n_grid = n_grid
        self._m_min = int(math.ceil((prof.freq_min_hz - start) / df - _EPS_BINS))
        self._m_max = int(math.floor((prof.freq_max_hz - start) / df + _EPS_BINS))
        self._overlap = overlap
        self._averages = int(cfg.averages)
        self._n_cap = n_cap
        self._buffer_size = max(MIN_BUFFER_SIZE, math.ceil(n_cap / BUFFER_GRANULE) * BUFFER_GRANULE)
        self._settle_buffers = int(cfg.settle_buffers) if int(cfg.settle_buffers) >= 0 else prof.kernel_buffers + 1
        self._enbw = window_info(window, nfft).enbw_bins
        self._n_eff = effective_averages(self._averages, overlap, window, nfft)
        self._gain_ref = float(cfg.gain_db)
        self._auto = cfg.gain_mode == "auto"

    def _gain_span(self, start: float, stop: float) -> tuple[float, float]:
        prof = self._dev.profile
        bands = [b for b in prof.gain_bands if b.f_min_hz <= stop and b.f_max_hz > start]
        if not bands:
            gr = prof.gain_range(start)
            return gr.min_db, gr.max_db
        return min(b.gain_min_db for b in bands), max(b.gain_max_db for b in bands)

    # ------------------------------------------------------------------ public properties
    @property
    def device(self) -> RxDevice:
        return self._dev

    @property
    def nfft(self) -> int:
        return self._nfft

    @property
    def df_hz(self) -> float:
        """Bin spacing of every step and of the stitched grid (``fs / nfft``)."""
        return self._df

    @property
    def rbw_hz(self) -> float:
        return self._enbw * self._fs / self._nfft

    @property
    def step_hz(self) -> float:
        """Nominal centre spacing (``U/2`` with ``dc_avoid``, else ``U``, whole bins)."""
        return self._step_bins * self._df

    @property
    def usable_half_bins(self) -> int:
        return self._k_u

    @property
    def dc_notch_bins(self) -> int:
        """Effective DC notch half-width in bins (``max(dsp.dc_notch_bins, window terms - 1)``)."""
        return self._notch

    @property
    def n_capture(self) -> int:
        """Samples captured per step (``cfg.averages`` Welch segments at ``dsp.overlap``)."""
        return self._n_cap

    @property
    def buffer_size(self) -> int:
        """Stream buffer of the bursts (``max(4096, n_capture rounded up to 1024)``); every
        capture reads exactly one such buffer."""
        return self._buffer_size

    @property
    def n_bins(self) -> int:
        """Bins of the stitched grid ``[start, stop]``."""
        return self._n_grid

    @property
    def effective_averages(self) -> float:
        return self._n_eff

    def plan(self) -> list[float]:
        """Step centre frequencies (Hz), ascending, all within the profile's official range."""
        lo, hi = self._dev.profile.freq_min_hz, self._dev.profile.freq_max_hz
        return [min(max(self._start + int(m) * self._df, lo), hi) for m in self._offsets.tolist()]

    def abort(self) -> None:
        """Request the running sweep to stop after the current step (thread-safe);
        :meth:`run` then raises :class:`MeasurementError`."""
        self._abort.set()

    # ------------------------------------------------------------------ run
    def run(self, on_step: OnStep | None = None) -> SweepResult:
        """Execute one full sweep (see the module docstring). Not re-entrant."""
        if on_step is not None and not callable(on_step):
            raise ConfigError("on_step çağrılabilir (callable) olmalı")
        if not self._run_lock.acquire(blocking=False):
            raise MeasurementError(f"{self._dev.name}: bu Sweeper zaten bir tarama yürütüyor")
        try:
            self._abort.clear()
            state = self._prepare_device()
            try:
                result = self._sweep(self.plan(), on_step)
            except BaseException:
                self._restore_device(state, raise_errors=False)
                raise
            self._restore_device(state, raise_errors=True)
            self._sweep_id += 1
            return result
        finally:
            self._run_lock.release()

    def _prepare_device(self) -> dict[str, Any]:
        dev = self._dev
        state = {"settle_buffers": dev.settle_buffers, "gain_mode": dev.gain_mode,
                 "streaming": dev.streaming, "buffer_size": dev.buffer_size, "restarted": False}
        try:
            dev.settle_buffers = self._settle_buffers
            dev.set_sample_rate(self._fs)
            dev.set_bandwidth(self._bw)
            if dev.gain_mode != "manual":
                dev.set_gain_mode("manual")
            if dev.streaming:               # restart: burst buffer size + buffer-aligned reads
                dev.stop_stream()
                state["restarted"] = True
            dev.start_stream(self._buffer_size)
        except BaseException:
            self._restore_device(state, raise_errors=False)
            raise
        return state

    def _restore_device(self, state: dict[str, Any], raise_errors: bool) -> None:
        dev = self._dev
        errors: list[BaseException] = []
        try:
            if not state["streaming"]:
                dev.stop_stream()
                dev.buffer_size = int(state["buffer_size"])
            elif state["restarted"] or not dev.streaming:
                dev.stop_stream()
                dev.start_stream(int(state["buffer_size"]))
        except Exception as e:
            errors.append(e)
        dev.settle_buffers = int(state["settle_buffers"])
        if state["gain_mode"] != dev.gain_mode:
            try:
                dev.set_gain_mode(state["gain_mode"])
            except Exception as e:
                errors.append(e)
        for e in errors:
            log.error("%s: tarama sonrası cihaz durumu geri yüklenemedi: %s", dev.name, e)
        if errors and raise_errors:
            raise errors[0]

    def _apply_calibration(self, spec: Spectrum) -> Spectrum:
        if self._calibrate is None:
            return spec
        out = self._calibrate(spec)
        if not isinstance(out, Spectrum):
            raise ConfigError(f"calibrate bir Spectrum döndürmeli, {type(out).__name__} döndürdü")
        if out.unit not in ("dBm", "dBFS"):
            raise ConfigError(f"calibrate birimi 'dBm' ya da 'dBFS' olmalı, {out.unit!r} döndürdü")
        return out

    def _sweep(self, centers: list[float], on_step: OnStep | None) -> SweepResult:
        dev = self._dev
        to = self._timeout_s
        stitch = _Stitcher(self._start, self._df, self._n_grid, self._k_u, self._notch)
        steps: list[dict[str, Any]] = []
        tot = {"tune_s": 0.0, "settle_s": 0.0, "capture_s": 0.0, "dsp_s": 0.0}
        calibrated: bool | None = None
        gain = dev.gain_range(centers[0]).clamp(self._gain_ref)
        t_start_ns = now_ns()
        t0 = mono()
        for i, fc in enumerate(centers):
            if self._abort.is_set():
                raise MeasurementError(f"{dev.name}: tarama iptal edildi ({i}/{len(centers)} adım)")
            ta = mono()
            dev.set_center_freq(fc)
            want = dev.gain_range(fc).clamp(self._gain_ref if not self._auto else gain)
            if abs(float(dev.gains[0]) - want) > _GAIN_TOL_DB:
                dev.set_gain(want)
            tb = mono()
            dev.flush(to)
            t_tune, t_settle, t_capture = tb - ta, mono() - tb, 0.0
            iters = 0
            while True:
                tc = mono()
                blk = dev.capture(self._buffer_size, to)        # exactly one (aligned) buffer
                t_capture += mono() - tc
                x = blk.samples[0, :self._n_cap]
                lv = iq_levels(x, blk.clip_level)
                g = float(blk.gain_db[0])
                if self._auto and iters < AUTO_GAIN_MAX_ITER:
                    new = self._gc.propose(lv, g, one_shot=True)
                    if new is not None:
                        ts = mono()
                        dev.set_gain(new)
                        dev.flush(to)
                        t_settle += mono() - ts
                        iters += 1
                        continue
                break
            gain = g
            td = mono()
            spec = welch(x, blk.fs, self._nfft, self._window, self._overlap, "mean", fc=blk.fc, t_ns=blk.t_ns)
            spec.gain_db = (g,)
            spec.overflow = bool(blk.overflow)
            spec.meta.update({"usable_fraction": float(self._cfg.usable_fraction), "dc_notch_bins": self._notch,
                              "seq": int(blk.seq), "index": int(blk.index), "sweep_step": i})
            spec = self._apply_calibration(spec)
            cal = spec.unit == "dBm"
            if calibrated is None:
                calibrated = cal
            elif calibrated != cal:
                raise MeasurementError("calibrate tutarsız: bazı adımlar dBm, bazıları dBFS döndürdü")
            stitch.add(i, spec, 0.0 if cal else self._gain_ref - g)
            t_dsp = mono() - td
            step = {"index": i, "fc": float(dev.fc), "gain_db": g, "t_tune": t_tune, "t_settle": t_settle,
                    "t_capture": t_capture, "t_dsp": t_dsp, "overflow": bool(blk.overflow),
                    "peak_dbfs": float(lv.peak_dbfs), "clip_fraction": float(lv.clip_fraction),
                    "gain_iters": iters, "t_ns": int(blk.t_ns)}
            steps.append(step)
            tot["tune_s"] += t_tune
            tot["settle_s"] += t_settle
            tot["capture_s"] += t_capture
            tot["dsp_s"] += t_dsp
            if on_step is not None and on_step(i, dict(step), spec) is False:
                raise MeasurementError(f"{dev.name}: tarama on_step tarafından iptal edildi ({i + 1}/{len(centers)} adım)")

        tp = mono()
        f, p_db, valid, src = stitch.result()
        unit = "dBm" if calibrated else gain_referred_unit(self._gain_ref)
        stitched = Spectrum(
            f_hz=f, p_lin=lin10(p_db), fc=float(f[f.size // 2]), fs=self._fs, nfft=self._nfft, window=self._window,
            enbw_bins=self._enbw, averages=self._averages, unit=unit, t_ns=t_start_ns, gain_db=(self._gain_ref,),
            meta={"overlap": self._overlap, "avg": "mean", "dc_notch_bins": -1, "usable_fraction": 1.0},
        )
        detections, threshold_db = detect(stitched, self._det, n_avg=self._n_eff, valid_mask=valid)
        tot["dsp_s"] += mono() - tp
        duration = mono() - t0
        t_end_ns = now_ns()
        used = np.bincount(src[src >= 0], minlength=len(centers))
        for s in steps:
            s["bins_used"] = int(used[s["index"]])
        timing = dict(tot)
        timing["other_s"] = max(duration - sum(tot.values()), 0.0)
        return SweepResult(
            f_hz=f, p_db=p_db, unit=unit, calibrated=bool(calibrated), rbw_hz=self.rbw_hz, n_steps=len(centers),
            step_hz=self.step_hz, steps=steps, duration_s=duration,
            rate_hz_per_s=(self._stop - self._start) / duration if duration > 0 else 0.0,
            timing=timing, detections=detections, t_start_ns=t_start_ns, t_end_ns=t_end_ns,
            sweep_id=self._sweep_id, start_hz=self._start, stop_hz=self._stop, df_hz=self._df, fs_hz=self._fs,
            nfft=self._nfft, window=self._window, enbw_bins=self._enbw, averages=self._averages,
            gain_ref_db=self._gain_ref, gain_mode=self._cfg.gain_mode, valid=valid, source_step=src,
            threshold_db=threshold_db,
        )
