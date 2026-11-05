"""Shared stimulus/measurement helpers for the KTR spec measurements: parameter
validation, settled captures, tone location and scalloping-free tone power with noise
correction, adaptive tone level (:func:`adjust_tone`), in-situ level reference
(:func:`reference_level`) and its uncertainty (:class:`ToneProbe`).
"""

from __future__ import annotations

import logging
import math
from dataclasses import dataclass, field
from typing import Any

import numpy as np

from ..core.config import AppConfig
from ..core.errors import ConfigError, MeasurementError
from ..core.types import IQBlock
from ..core.units import db10, fmt_hz
from ..dsp.detect import default_n_avg, usable_mask
from ..dsp.levels import IQLevels, iq_levels
from ..dsp.peaks import tone_frequency
from ..dsp.psd import Spectrum, spectrum_from_block
from ..dsp.stats import gamma_quantile
from .base import Measurement, MeasurementContext
from .common import (
    LevelRef,
    tone,
    welch_len,
)

log = logging.getLogger(__name__)

# =============================================================================== shared helpers
#: Nominal AD936x level model used only for first guesses: dBFS ≈ dBm + G − 5.
LEVEL_MODEL_OFFSET_DB: float = -5.0
#: A tone counts as visible when its noise-corrected power exceeds the noise per bin by this.
VISIBLE_SNR_DB: float = 10.0
#: Half-width (bins) of the band-power window of a tone (Blackman-Harris main lobe is ±4 bins).
TONE_HALFWIDTH_BINS: int = 5
#: Search half-width (bins) around a tone whose apparent frequency is already known.
KNOWN_SEARCH_BINS: int = 3
#: Generator / calibration-table level accuracy assumed when the operator gives none (dB).
DEFAULT_LEVEL_UNCERTAINTY_DB: float = 1.0
#: Two-sided 95 % normal quantile.
Z95: float = 1.959963984540054


def qparam(m: Measurement, key: str) -> Any:
    """``m.p[key]``, or ``m.p['quick_' + key]`` in quick mode when that key exists."""
    if m.ctx.quick and f"quick_{key}" in m.p:
        return m.p[f"quick_{key}"]
    return m.p[key]


def as_float(v: Any, what: str, lo: float | None = None, hi: float | None = None) -> float:
    """Finite float parameter within ``[lo, hi]`` (Turkish :class:`ConfigError` otherwise)."""
    if isinstance(v, bool) or not isinstance(v, (int, float, np.integer, np.floating)) or not math.isfinite(float(v)):
        raise ConfigError(f"{what}: sonlu sayı bekleniyordu, {v!r} geldi")
    f = float(v)
    if lo is not None and f < lo:
        raise ConfigError(f"{what} >= {lo:g} olmalı, {f:g} geldi")
    if hi is not None and f > hi:
        raise ConfigError(f"{what} <= {hi:g} olmalı, {f:g} geldi")
    return f


def as_int(v: Any, what: str, lo: int = 1) -> int:
    """Integer parameter ``>= lo`` (floats with an integral value are accepted)."""
    if isinstance(v, bool) or not isinstance(v, (int, float, np.integer, np.floating)) or int(v) != v or int(v) < lo:
        raise ConfigError(f"{what} >= {lo} tamsayı olmalı, {v!r} geldi")
    return int(v)


def as_float_list(v: Any, what: str, lo: float | None = None, hi: float | None = None) -> list[float]:
    """Non-empty list of finite floats."""
    if not isinstance(v, (list, tuple)) or len(v) == 0:
        raise ConfigError(f"{what}: boş olmayan sayı listesi bekleniyordu, {v!r} geldi")
    return [as_float(x, f"{what}[{i}]", lo, hi) for i, x in enumerate(v)]


def opt_float(v: Any, what: str, lo: float | None = None, hi: float | None = None) -> float | None:
    """Like :func:`as_float` but ``None`` passes through (= derived default)."""
    return None if v is None else as_float(v, what, lo, hi)


def source_power_range(src: Any) -> tuple[float, float]:
    lo, hi = getattr(src, "power_range_db", (-140.0, 0.0))
    return float(lo), float(hi)


def valid_bins(app: AppConfig, n_bins: int) -> np.ndarray:
    """Usable-band mask (``dsp.usable_fraction``, DC ± ``dsp.dc_notch_bins`` excluded)."""
    return usable_mask(n_bins, float(app.dsp.usable_fraction), int(app.dsp.dc_notch_bins))


def noise_mean_lin(spec: Spectrum, valid: np.ndarray | None = None) -> float:
    """Unbiased noise *mean* per bin (linear, spectrum unit) from the median of the valid bins.

    Averaged noise bins are ``Gamma(n_eff, 1/n_eff)``-distributed; their median is
    ``gamma_quantile(0.5, n_eff) / n_eff`` times the mean, which is divided out. The median
    itself is robust against the few bins occupied by tones/spurs.
    """
    p = spec.p_lin
    if valid is not None and valid.shape == p.shape and np.any(valid):
        p = p[valid]
    med = float(np.median(p))
    n_eff = max(float(default_n_avg(spec)), 1e-3)
    return med / (gamma_quantile(0.5, n_eff) / n_eff)


def noise_mean_db(spec: Spectrum, valid: np.ndarray | None = None) -> float:
    return float(db10(noise_mean_lin(spec, valid)))


def locate_peak(spec: Spectrum, f_hz: float, search_hz: float) -> int:
    """Index of the strongest bin within ``f_hz ± search_hz`` (the nearest bin if none inside)."""
    f = spec.f_hz
    mask = np.abs(f - float(f_hz)) <= float(search_hz)
    if not np.any(mask):
        return int(np.argmin(np.abs(f - float(f_hz))))
    idx = np.flatnonzero(mask)
    return int(idx[np.argmax(spec.p_lin[idx])])


def nearest_bin(spec: Spectrum, f_hz: float) -> int:
    return int(np.argmin(np.abs(spec.f_hz - float(f_hz))))


def tone_band_lin(spec: Spectrum, k: int, halfwidth_bins: int = TONE_HALFWIDTH_BINS,
                  noise_lin: float = 0.0) -> float:
    """Scalloping-free power of a tone centred near bin ``k``: ``(Σ P − n·noise) / ENBW_bins``
    over ``k ± halfwidth_bins`` (the noise-mean contribution of the bins is removed; may be
    ``<= 0`` when the tone is buried in noise)."""
    n = spec.n_bins
    lo, hi = max(0, int(k) - halfwidth_bins), min(n - 1, int(k) + halfwidth_bins)
    total = float(np.sum(spec.p_lin[lo:hi + 1])) - noise_lin * (hi - lo + 1)
    return total / spec.enbw_bins


def tone_band_db(spec: Spectrum, k: int, halfwidth_bins: int = TONE_HALFWIDTH_BINS, noise_lin: float = 0.0) -> float:
    return float(db10(max(tone_band_lin(spec, k, halfwidth_bins, noise_lin), 1e-30)))


def freq_search_hz(dev: Any, f_hz: float, bin_hz: float) -> float:
    """Search half-width for a tone whose reference error is still unknown: 1.5 × the
    profile's TCXO tolerance at ``f_hz`` plus 8 bins."""
    ppm = float(getattr(dev.profile, "ref_clock_ppm", 25.0))
    return 1.5 * ppm * 1e-6 * abs(float(f_hz)) + 8.0 * float(bin_hz)


def settled_capture(dev: Any, n: int) -> IQBlock:
    """Capture after discarding the device's stale buffers (``mark_settling`` + ``capture``).

    Needed after every stimulus change: the driver's kernel buffers still hold samples taken
    before the change (hardware and the real-time simulator alike)."""
    dev.mark_settling()
    return dev.capture(int(n))


def nominal_setting(ctx: MeasurementContext, dev: Any, target_dbfs: float, ref: LevelRef | None) -> float:
    """First guess of the source setting that gives ``target_dbfs`` at the ADC."""
    src = ctx.source
    lo, hi = source_power_range(src)
    if getattr(src, "absolute_power", False):
        if ref is not None and ref.calibrated:
            guess = target_dbfs + ref.offset_db
        else:
            guess = target_dbfs - float(dev.gains[0]) - LEVEL_MODEL_OFFSET_DB
    else:
        guess = hi - 40.0
    return float(np.clip(guess, lo, hi))


def level_uncertainty_db(ctx: MeasurementContext, ref: LevelRef | None, override: float | None) -> float | None:
    """Systematic level uncertainty (≈95 %, dB) of absolute values.

    ``override`` (parameter) wins. Otherwise: simulator source (exact by construction) → 0;
    any other absolute generator or a calibration table → :data:`DEFAULT_LEVEL_UNCERTAINTY_DB`;
    uncalibrated → ``None`` (no absolute level)."""
    if override is not None:
        return float(override)
    src = ctx.source
    calibrated = (ref is not None and ref.calibrated) or bool(getattr(src, "absolute_power", False))
    if not calibrated:
        return None
    from ..hal.sim.source import SimSignalSource

    from_table = ref is not None and ref.method == "kalibrasyon tablosu"
    if isinstance(src, SimSignalSource) and not from_table:
        return 0.0
    return DEFAULT_LEVEL_UNCERTAINTY_DB


@dataclass
class ToneProbe:
    """One settled measurement of the stimulus tone."""

    f_src_hz: float           # source frequency setting
    setting: float            # source power setting (dBm if absolute, else relative dB)
    f_app_hz: float           # apparent RF frequency (fine estimate when visible)
    level_dbfs: float         # noise-corrected, scalloping-free tone power at the ADC
    noise_dbfs: float         # noise mean per bin (power in one RBW)
    levels: IQLevels          # time-domain peak / clipping of the capture
    k: int                    # peak bin
    spec: Spectrum = field(repr=False)

    @property
    def snr_db(self) -> float:
        return self.level_dbfs - self.noise_dbfs

    @property
    def visible(self) -> bool:
        return self.snr_db >= VISIBLE_SNR_DB

    @property
    def clipped(self) -> bool:
        return self.levels.clip_fraction > 0.0

    @property
    def offset_db(self) -> float:
        """ADC level minus source setting (``dBFS = setting + offset_db``)."""
        return self.level_dbfs - self.setting

    @property
    def e(self) -> float:
        """In-situ relative reference error between source and receiver: ``f_src / f_app − 1``
        (ARCHITECTURE ppm convention: ``f_app = f_true / (1 + e)``)."""
        return self.f_src_hz / self.f_app_hz - 1.0


def probe_tone(ctx: MeasurementContext, dev: Any, f_src: float, setting: float, *, nfft: int, averages: int,
               window: str, overlap: float, search_hz: float, f_expected: float | None = None,
               fine_freq: bool = True) -> ToneProbe:
    """Tone on at ``f_src``/``setting`` → settled capture → level, clipping and frequency.

    The tone is searched within ``f_expected ± search_hz`` (``f_expected`` defaults to
    ``f_src``); ``fine_freq`` refines the apparent frequency with
    :func:`eds.dsp.peaks.tone_frequency` (only when the tone is visible)."""
    with tone(ctx, f_src, setting):
        blk = settled_capture(dev, welch_len(nfft, averages, overlap))
    x = blk.samples[0]
    lv = iq_levels(x, blk.clip_level)
    spec = spectrum_from_block(blk, nfft, window, overlap)
    valid = valid_bins(ctx.app, spec.n_bins)
    nz = noise_mean_lin(spec, valid)
    f_exp = float(f_src if f_expected is None else f_expected)
    k = locate_peak(spec, f_exp, search_hz)
    level = tone_band_db(spec, k, TONE_HALFWIDTH_BINS, nz)
    f_app = float(spec.f_hz[k])
    if fine_freq and level - db10(nz) >= VISIBLE_SNR_DB:
        try:
            f_app = tone_frequency(x, spec.fs, fc=spec.fc, f_hint=float(spec.f_hz[k]), search_hz=2.0 * spec.df_hz)
        except MeasurementError as e:
            log.warning("ton frekansı ince kestirimi başarısız (%s); bin merkezi kullanılıyor", e)
    return ToneProbe(f_src_hz=float(f_src), setting=float(setting), f_app_hz=float(f_app), level_dbfs=float(level),
                     noise_dbfs=float(db10(nz)), levels=lv, k=k, spec=spec)


def adjust_tone(ctx: MeasurementContext, dev: Any, f_src: float, target_dbfs: float, *, nfft: int, averages: int,
                window: str, overlap: float, ref: LevelRef | None = None, setting: float | None = None,
                tol_db: float = 0.5, max_iter: int = 8, search_hz: float | None = None,
                f_expected: float | None = None) -> ToneProbe:
    """Adapt the source setting until the tone reads ``target_dbfs ± tol_db`` at the ADC.

    Works for absolute and relative sources (the reading defines the setting→dBFS offset).
    Invisible tone → +20 dB, clipping → −20 dB, otherwise a proportional correction. Returns
    the last visible, unclipped probe (the caller checks its level when the source limit
    stopped the loop). Raises :class:`MeasurementError` when the tone never becomes visible
    or always clips within the source's range."""
    src = ctx.source
    if src is None:
        raise MeasurementError("Bu adım sinyal kaynağı gerektiriyor")
    lo, hi = source_power_range(src)
    s = nominal_setting(ctx, dev, target_dbfs, ref) if setting is None else float(np.clip(setting, lo, hi))
    search = freq_search_hz(dev, f_src, dev.fs / nfft) if search_hz is None else float(search_hz)
    best: ToneProbe | None = None
    for _ in range(max_iter):
        pr = probe_tone(ctx, dev, f_src, s, nfft=nfft, averages=averages, window=window, overlap=overlap,
                        search_hz=search, f_expected=f_expected)
        if pr.clipped:
            if s <= lo:
                raise MeasurementError(
                    f"Ton ({fmt_hz(f_src)}) kaynağın en düşük gücünde ({lo:g}) bile kırpıyor — zayıflatıcı ekleyin")
            s = max(lo, s - 20.0)
            continue
        if not pr.visible:
            if s >= hi:
                raise MeasurementError(
                    f"Ton ({fmt_hz(f_src)}) kaynağın en yüksek gücünde ({hi:g}) bile görülmüyor — kablo/zayıflatıcı/"
                    "kaynak bağlantısını kontrol edin")
            s = min(hi, s + 20.0)
            continue
        best = pr
        err = float(target_dbfs) - pr.level_dbfs
        if abs(err) <= tol_db:
            return pr
        new = float(np.clip(s + err, lo, hi))
        if abs(new - s) < 1e-6:
            log.warning("Kaynak sınırı: ton %.1f dBFS hedefine ulaşamadı (okunan %.1f dBFS)", target_dbfs,
                        pr.level_dbfs)
            return pr
        s = new
    if best is None:
        raise MeasurementError(f"Ton ({fmt_hz(f_src)}) seviyesi {max_iter} denemede ayarlanamadı")
    return best


def known_search_hz(bin_hz: float) -> float:
    """Narrow search half-width once the apparent frequency is known (``KNOWN_SEARCH_BINS``)."""
    return KNOWN_SEARCH_BINS * float(bin_hz)


def reference_level(ctx: MeasurementContext, dev: Any, f_hz: float, *, nfft: int, averages: int, window: str,
                    overlap: float, probe_frac: float = 0.125, probe_dbfs: float = -25.0) -> LevelRef:
    """dBFS → dBm offset at the device's current fc / gain (order of
    :func:`eds.measure.common.level_reference`).

    The calibration-table and uncalibrated cases are delegated to ``level_reference``. The
    in-situ case (absolute source, no table) is measured here with :func:`adjust_tone`, which
    *searches* the probe tone within the TCXO tolerance: ``level_reference`` integrates ±5 bins
    around the nominal probe frequency, which misses the tone when the reference error
    (e.g. 11.7 ppm = 10.7 kHz at 915 MHz) exceeds 5 bins (narrow RBW)."""
    cal = ctx.cal
    table = getattr(cal, "power", None) if cal is not None else None
    src = ctx.source
    if table is not None:
        return LevelRef(float(table.offset_db(f_hz, float(dev.gains[0]))), "kalibrasyon tablosu", True, "dBm")
    if src is None or not getattr(src, "absolute_power", False):
        return LevelRef(0.0, "kalibrasyonsuz (dBFS)", False, "dBFS")
    f_probe = float(dev.fc) + probe_frac * float(dev.fs)
    with ctx.isolated_source():
        pr = adjust_tone(ctx, dev, f_probe, probe_dbfs, nfft=nfft, averages=averages, window=window, overlap=overlap,
                         tol_db=3.0)
    if pr.snr_db < 30.0:
        log.warning("Seviye referansı: prob tonu SNR %.1f dB (< 30 dB) — referans güveni düşük", pr.snr_db)
    return LevelRef(float(pr.setting - pr.level_dbfs),
                    f"kaynak referanslı (in-situ, {pr.setting:.1f} dBm @ {fmt_hz(f_probe)})", True, "dBm")
