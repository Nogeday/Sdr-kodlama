"""Tarama hızı [GHz/s] — wideband sweep rate, per-step timing budget and revisit time.

Definition (docs/MEASUREMENTS.md section 4)::

    S        = (f_stop − f_start) / T_sweep                         [GHz/s]
    T_sweep  = Σ_steps (t_tune + t_settle + t_capture + t_dsp)       (wall clock, perf_counter)
    T_revisit(325 MHz – 3.8 GHz) = full span / S  +  gap between consecutive sweeps

Procedure: for every sweep configuration ``{fs, rbw_hz}`` (optional ``averages``, ``nfft``,
``bw``) the application's ``[sweep]`` section is overridden (start/stop, sample rate, analog
bandwidth = fs clamped to the profile, RBW) and an :class:`~eds.rx.sweep.Sweeper` runs
``repeats`` complete sweeps back to back with the application's ``[dsp]`` / ``[detect]``
settings and — when the DUT has a stored calibration — the same per-step calibration as the
RX node. Each :class:`~eds.rx.sweep.SweepResult` carries the wall-clock duration and the
tune / settle / capture / dsp totals (the final stitching + CFAR count as dsp).

Headline value: median rate of the FIRST (reference) configuration. Secondary metrics:
rate of every configuration, mean per-step milliseconds of each timing component, number
of steps and step size, detections, the gap between consecutive sweeps (stream restart)
and the revisit time of the full official band of the profile (extrapolated with the
measured rate when the swept span is smaller, e.g. ``--quick``).

On a Pluto the step time is dominated by the retune cost: the libiio attribute write over
USB, the PLL lock and the stale kernel buffers that must be flushed after every retune
(``(kernel_buffers + 1) × buffer_size`` samples, the "settle" item). AD9361 fastlock
profiles (store/recall of up to 8 synthesizer settings) are the future optimisation of
``t_tune``; see :mod:`eds.rx.sweep`.

A configuration the profile rejects (:class:`~eds.core.errors.ProfileViolation`, e.g. a
sample rate above the chip limit) is skipped and listed in ``extra['skipped_configs']``.
On a simulator that is not real-time the timing is NOT meaningful; the result says so.
"""

from __future__ import annotations

import dataclasses
import logging
import math
import os
import platform
from dataclasses import dataclass
from typing import Any

import numpy as np

from ...core.errors import ConfigError, MeasurementError, ProfileViolation
from ...core.units import fmt_hz
from ...dsp.psd import Spectrum
from ...rx.sweep import Sweeper, SweepResult
from ..base import FigureSpec, Measurement, MeasurementResult, Series, register
from ..common import base_conditions, spectrum_screenshot
from ..targets import REASON_NONREALTIME

log = logging.getLogger(__name__)

#: Timing components of one sweep step (keys of ``SweepResult.timing`` without ``_s``).
TIMING_KEYS: tuple[str, ...] = ("tune", "settle", "capture", "dsp")
TIMING_LABELS: dict[str, str] = {
    "tune": "ayar (tune)", "settle": "oturma (settle)", "capture": "yakalama", "dsp": "işleme (DSP)",
}
#: Accepted keys of one sweep configuration.
CONFIG_KEYS: frozenset[str] = frozenset({"fs", "rbw_hz", "averages", "nfft", "bw"})
DEFAULT_CONFIGS: tuple[dict[str, float], ...] = (
    {"fs": 20e6, "rbw_hz": 50e3},
    {"fs": 20e6, "rbw_hz": 10e3},
    {"fs": 10e6, "rbw_hz": 10e3},
)
NONREALTIME_WARNING = ("UYARI: gerçek zamanlı olmayan simülatör (sim.realtime = false) — örnekler duvar saatinden "
                       "bağımsız üretilir; ölçülen tarama hızı fiziksel olarak ANLAMLI DEĞİLDİR.")
#: Maximum number of detection markers on the stitched-sweep screenshot.
MAX_MARKERS = 8
#: Strongest detections of the last reference sweep listed in ``extra['detections_hz']``.
MAX_LISTED_DETECTIONS = 32


@dataclass(frozen=True)
class ScanConfig:
    """One sweep configuration (resolved against ``[sweep]`` and the device profile)."""

    index: int
    fs: float
    rbw_hz: float
    averages: int
    nfft: int
    bw: float

    @property
    def label(self) -> str:
        """Label with the REQUESTED resolution (before a sweeper exists, e.g. a skipped config)."""
        res = f"istenen RBW {fmt_hz(self.rbw_hz, 4)}" if self.rbw_hz > 0 else f"N = {self.nfft}"
        return f"#{self.index + 1}: {self.fs / 1e6:g} MS/s, {res}, {self.averages} ort."

    def label_actual(self, rbw_hz: float, nfft: int) -> str:
        """Label with the resolution the sweeper actually uses (RBW = ENBW·fs/nfft)."""
        return f"#{self.index + 1}: {self.fs / 1e6:g} MS/s, RBW {fmt_hz(rbw_hz, 4)} (N = {nfft}), {self.averages} ort."


@dataclass
class ConfigRun:
    """Every sweep of one configuration + the derived statistics."""

    cfg: ScanConfig
    sweeper: Sweeper
    sweeps: list[SweepResult]

    @property
    def label(self) -> str:
        return self.cfg.label_actual(self.sweeper.rbw_hz, self.sweeper.nfft)

    @property
    def rates_ghz(self) -> list[float]:
        return [r.rate_hz_per_s / 1e9 for r in self.sweeps]

    @property
    def rate_ghz(self) -> float:
        return float(np.median(self.rates_ghz))

    @property
    def n_steps(self) -> int:
        return int(self.sweeps[0].n_steps)

    @property
    def duration_s(self) -> float:
        return float(np.median([r.duration_s for r in self.sweeps]))

    def per_step_ms(self) -> dict[str, float]:
        """Mean milliseconds per step of each timing component (+ ``other``), over all sweeps."""
        steps = sum(r.n_steps for r in self.sweeps)
        out = {k: 1e3 * sum(r.timing[f"{k}_s"] for r in self.sweeps) / steps for k in TIMING_KEYS}
        out["other"] = 1e3 * sum(r.timing.get("other_s", 0.0) for r in self.sweeps) / steps
        return out

    def timing_sum_ratio(self) -> float:
        """``Σ (tune + settle + capture + dsp) / duration`` over all sweeps (1 = fully accounted)."""
        parts = sum(sum(r.timing[f"{k}_s"] for k in TIMING_KEYS) for r in self.sweeps)
        total = sum(r.duration_s for r in self.sweeps)
        return parts / total if total > 0 else math.nan

    def gaps_s(self) -> list[float]:
        """Wall-clock gaps between consecutive sweeps (end of one → first retune of the next)."""
        return [max((b.t_start_ns - a.t_end_ns) * 1e-9, 0.0) for a, b in zip(self.sweeps, self.sweeps[1:])]

    def detections(self) -> int:
        return int(np.median([len(r.detections) for r in self.sweeps]))


def _finite(v: Any, what: str, lo: float | None = None) -> float:
    if isinstance(v, bool) or not isinstance(v, (int, float)) or not math.isfinite(float(v)):
        raise ConfigError(f"scan_speed.{what} sonlu bir sayı olmalı, {v!r} geldi")
    if lo is not None and float(v) < lo:
        raise ConfigError(f"scan_speed.{what} en az {lo:g} olmalı, {v!r} geldi")
    return float(v)


def _int(v: Any, what: str, lo: int = 1) -> int:
    if isinstance(v, bool) or not isinstance(v, (int, float)) or int(v) != v or int(v) < lo:
        raise ConfigError(f"scan_speed.{what} en az {lo} olan bir tamsayı olmalı, {v!r} geldi")
    return int(v)


@register
class ScanSpeedMeasurement(Measurement):
    spec_id = "scan_speed"
    title = "Tarama hızı"
    unit = "GHz/s"
    requires = frozenset()
    default_params = {
        "start_hz": None,                  # None = [sweep] start_hz (quick: quick_start_hz)
        "stop_hz": None,                   # None = [sweep] stop_hz (quick: quick_stop_hz)
        "quick_start_hz": 325e6,
        "quick_stop_hz": 825e6,
        "configs": [dict(c) for c in DEFAULT_CONFIGS],   # first = reference (headline value)
        "repeats": 2,
        "quick_repeats": 1,
    }

    # ------------------------------------------------------------------ parameters
    def _span(self) -> tuple[float, float]:
        p, sw = self.p, self.ctx.app.sweep
        quick = bool(self.ctx.quick)
        start = p.get("start_hz")
        stop = p.get("stop_hz")
        if start is None:
            start = p["quick_start_hz"] if quick else sw.start_hz
        if stop is None:
            stop = p["quick_stop_hz"] if quick else sw.stop_hz
        start, stop = _finite(start, "start_hz", 0.0), _finite(stop, "stop_hz", 0.0)
        if not start < stop:
            raise ConfigError(f"scan_speed: start_hz ({fmt_hz(start)}) stop_hz'den ({fmt_hz(stop)}) küçük olmalı")
        return start, stop

    def _configs(self) -> list[ScanConfig]:
        raw = self.p.get("configs")
        if not isinstance(raw, (list, tuple)) or not raw:
            raise ConfigError("scan_speed.configs boş olmayan bir liste olmalı (ör. [{fs = 20e6, rbw_hz = 50e3}])")
        sw, prof = self.ctx.app.sweep, self.ctx.dut.profile
        out: list[ScanConfig] = []
        for i, c in enumerate(raw):
            if not isinstance(c, dict):
                raise ConfigError(f"scan_speed.configs[{i}] bir tablo olmalı, {c!r} geldi")
            unknown = set(c) - CONFIG_KEYS
            if unknown:
                raise ConfigError(f"scan_speed.configs[{i}]: bilinmeyen anahtar(lar) {sorted(unknown)}; "
                                  f"geçerli: {sorted(CONFIG_KEYS)}")
            if "fs" not in c:
                raise ConfigError(f"scan_speed.configs[{i}]: 'fs' (örnekleme hızı, Hz) gerekli")
            fs = _finite(c["fs"], f"configs[{i}].fs", 1.0)
            rbw = _finite(c.get("rbw_hz", sw.rbw_hz), f"configs[{i}].rbw_hz", 0.0)
            avg = _int(c.get("averages", sw.averages), f"configs[{i}].averages")
            nfft = _int(c.get("nfft", sw.nfft), f"configs[{i}].nfft")
            bw = c.get("bw")
            bw = min(max(fs, prof.bw_min_hz), prof.bw_max_hz) if bw is None else _finite(bw, f"configs[{i}].bw", 1.0)
            out.append(ScanConfig(index=i, fs=fs, rbw_hz=rbw, averages=avg, nfft=nfft, bw=bw))
        return out[:1] if self.ctx.quick else out

    def _repeats(self) -> int:
        key = "quick_repeats" if self.ctx.quick else "repeats"
        return _int(self.p[key], key)

    def _calibrate_fn(self):
        """Per-step calibration identical to the RX node's (ppm axis; dBm when a power table
        exists and ``[cal] use_power_cal``); ``None`` without stored calibration."""
        cal = self.ctx.cal
        if cal is None or (cal.ppm is None and cal.power is None):
            return None
        from ...calib.apply import apply_calibration

        app, dev = self.ctx.app, self.ctx.dut
        used = cal if (app.cal.use_power_cal or cal.power is None) else dataclasses.replace(cal, power=None)
        if used.ppm is None and used.power is None:
            return None
        mode = app.cal.ppm_mode

        def calibrate(spec: Spectrum) -> Spectrum:
            return apply_calibration(spec, used, mode, ppm_hw=float(dev.ppm_hw))

        return calibrate

    @staticmethod
    def _is_nonrealtime_sim(dev: Any) -> bool:
        info = getattr(dev, "info", None)
        if info is None or getattr(info, "backend", "") != "sim":
            return False
        return str(info.extra.get("realtime", "true")).lower() == "false"

    # ------------------------------------------------------------------ run
    def run(self) -> MeasurementResult:
        app, dev = self.ctx.app, self.ctx.dut
        start, stop = self._span()
        configs = self._configs()
        repeats = self._repeats()
        calibrate = self._calibrate_fn()
        nonrt = self._is_nonrealtime_sim(dev)
        if nonrt:
            log.warning(NONREALTIME_WARNING)
        runs: list[ConfigRun] = []
        skipped: list[dict[str, Any]] = []
        for sc in configs:
            cfg = dataclasses.replace(app.sweep, start_hz=start, stop_hz=stop, sample_rate_hz=sc.fs,
                                      bandwidth_hz=sc.bw, rbw_hz=sc.rbw_hz, averages=sc.averages, nfft=sc.nfft)
            try:
                sweeper = Sweeper(dev, cfg, app.dsp, app.detect, calibrate=calibrate, agc=app.agc,
                                  timeout_s=float(app.rx.read_timeout_s))
                sweeps = []
                for k in range(repeats):
                    res = sweeper.run()
                    log.info("scan_speed %s tekrar %d/%d: %d adım, %.3f s, %.3f GHz/s",
                             sc.label_actual(sweeper.rbw_hz, sweeper.nfft), k + 1, repeats,
                             res.n_steps, res.duration_s, res.rate_hz_per_s / 1e9)
                    sweeps.append(res)
            except ProfileViolation as e:
                log.warning("scan_speed %s profil sınırı nedeniyle atlandı: %s", sc.label, e)
                skipped.append({"config": sc.label, "fs_hz": sc.fs, "rbw_hz": sc.rbw_hz, "reason": str(e)})
                continue
            runs.append(ConfigRun(sc, sweeper, sweeps))
        if not runs:
            raise MeasurementError("Hiçbir tarama konfigürasyonu çalıştırılamadı (hepsi profil sınırı dışında): "
                                   + "; ".join(s["reason"] for s in skipped))
        return self._result(start, stop, repeats, runs, skipped, configs, calibrate is not None, nonrt)

    # ------------------------------------------------------------------ result
    def _result(self, start: float, stop: float, repeats: int, runs: list[ConfigRun],
                skipped: list[dict[str, Any]], configs: list[ScanConfig], calibrated: bool,
                nonrt: bool) -> MeasurementResult:
        dev, app = self.ctx.dut, self.ctx.app
        prof = dev.profile
        ref = runs[0]
        span = stop - start
        full_span = float(prof.freq_max_hz - prof.freq_min_hz)
        rates = ref.rates_ghz
        value = ref.rate_ghz
        ms = ref.per_step_ms()
        # gap between consecutive sweeps (stream restart): the reference configuration's own,
        # else the median over every configuration that ran ≥ 2 sweeps
        gaps_ref = ref.gaps_s()
        gaps_all = [g for r in runs for g in r.gaps_s()]
        gap_s = float(np.median(gaps_ref or gaps_all)) if (gaps_ref or gaps_all) else None
        extrapolated = not (start <= prof.freq_min_hz and stop >= prof.freq_max_hz)
        revisit = full_span / (value * 1e9) + (gap_s or 0.0)
        last = ref.sweeps[-1]
        sw = ref.sweeper
        ref_is_first = ref.cfg.index == configs[0].index

        per_config = []
        for r in runs:
            m = r.per_step_ms()
            g = r.gaps_s()
            gap_cfg = float(np.median(g)) if g else gap_s
            per_config.append({
                "config": r.label, "fs_hz": r.cfg.fs, "bw_hz": r.cfg.bw, "rbw_hz": r.sweeper.rbw_hz,
                "rbw_requested_hz": r.cfg.rbw_hz, "nfft": r.sweeper.nfft, "averages": r.cfg.averages,
                "rate_ghz_per_s": r.rate_ghz, "rate_min_ghz_per_s": min(r.rates_ghz),
                "rate_max_ghz_per_s": max(r.rates_ghz), "duration_s": r.duration_s, "n_steps": r.n_steps,
                "step_hz": r.sweeper.step_hz, "buffer_size": r.sweeper.buffer_size,
                "n_capture": r.sweeper.n_capture, "tune_ms": m["tune"], "settle_ms": m["settle"],
                "capture_ms": m["capture"], "dsp_ms": m["dsp"], "other_ms": m["other"],
                "timing_sum_ratio": r.timing_sum_ratio(), "gap_s": float(np.median(g)) if g else None,
                "detections": r.detections(),
                "revisit_full_s": full_span / (r.rate_ghz * 1e9) + (gap_cfg or 0.0),
            })

        step_total = sum(ms[k] for k in TIMING_KEYS)
        share = {k: 100.0 * ms[k] / step_total if step_total > 0 else math.nan for k in TIMING_KEYS}
        dominant = max(TIMING_KEYS, key=lambda k: ms[k])
        settle_floor_ms = 1e3 * dev.settle_buffers * sw.buffer_size / ref.cfg.fs if ref.cfg.fs else math.nan
        overflow_steps = sum(int(s["overflow"]) for r in ref.sweeps for s in r.steps)
        extra: dict[str, Any] = {
            "rate_ghz_per_s": value,
            "rates_ghz_per_s": rates,
            "rate_worst_ghz_per_s": min(rates),
            "sweep_duration_s": ref.duration_s,
            "n_steps": ref.n_steps,
            "step_hz": sw.step_hz,
            "rbw_hz": sw.rbw_hz,
            "nfft": sw.nfft,
            "averages": ref.cfg.averages,
            "n_capture": sw.n_capture,
            "buffer_size": sw.buffer_size,
            "tune_ms": ms["tune"], "settle_ms": ms["settle"], "capture_ms": ms["capture"], "dsp_ms": ms["dsp"],
            "other_ms": ms["other"],
            "step_ms": step_total,
            "timing_share_pct": share,
            "dominant_component": dominant,
            "timing_sum_ratio": ref.timing_sum_ratio(),
            "settle_floor_ms": settle_floor_ms,
            "gap_between_sweeps_s": gap_s,
            "revisit_full_s": revisit,
            "revisit_extrapolated": extrapolated,
            "full_band_hz": [float(prof.freq_min_hz), float(prof.freq_max_hz)],
            "detections": ref.detections(),
            "detections_hz": [float(d.f_peak_hz) for d in sorted(last.detections, key=lambda d: d.snr_db,
                                                                 reverse=True)[:MAX_LISTED_DETECTIONS]],
            "overflow_steps": overflow_steps,
            "calibrated_steps": calibrated,
            "unit_spectrum": last.unit,
            "configs": per_config,
            "skipped_configs": skipped,
        }
        if not ref_is_first:
            extra["reference_note"] = (f"Referans konfigürasyon {configs[0].label} profil sınırı nedeniyle atlandı; "
                                       f"değer {ref.label} için verildi")
        if nonrt:
            extra["warning"] = NONREALTIME_WARNING

        conditions = base_conditions(dev)
        conditions.pop("fc_hz", None)                 # a sweep has no single centre frequency
        conditions.update({
            "start_hz": start, "stop_hz": stop, "span_hz": span, "fs_hz": ref.cfg.fs, "bw_hz": ref.cfg.bw,
            "rbw_hz": sw.rbw_hz, "rbw_requested_hz": ref.cfg.rbw_hz, "nfft": sw.nfft, "averages": ref.cfg.averages, "window": last.window,
            "overlap": float(app.dsp.overlap), "gain_db": float(app.sweep.gain_db),
            "sweep_gain_mode": app.sweep.gain_mode, "usable_fraction": float(app.sweep.usable_fraction),
            "dc_avoid": bool(app.sweep.dc_avoid), "settle_buffers": int(dev.settle_buffers),
            "buffer_size": sw.buffer_size, "step_hz": sw.step_hz, "repeats": repeats,
            "detect_method": app.detect.method, "pfa": float(app.detect.pfa),
            "host": platform.node() or "-", "host_platform": platform.platform(), "cpu_count": os.cpu_count() or 0,
        })

        unc = float(np.std(rates, ddof=1)) if len(rates) >= 2 else None
        rev_txt = f"{revisit:.2f} s" + (" (ölçülen hızla ekstrapolasyon)" if extrapolated else "")
        summary = (
            f"{fmt_hz(start, 4)}–{fmt_hz(stop, 4)} ({fmt_hz(span, 4)}) aralığı {ref.cfg.fs / 1e6:g} MS/s burst, "
            f"RBW {fmt_hz(sw.rbw_hz, 3)} ve {ref.cfg.averages} ortalama ile {value:.3f} GHz/s hızla tarandı "
            f"(tam tarama {ref.duration_s:.2f} s, {ref.n_steps} adım; adım başına tune {ms['tune']:.2f} ms, settle "
            f"{ms['settle']:.2f} ms, yakalama {ms['capture']:.2f} ms, işleme {ms['dsp']:.2f} ms); "
            f"{fmt_hz(prof.freq_min_hz, 4)}–{fmt_hz(prof.freq_max_hz, 4)} yeniden ziyaret {rev_txt}."
        )
        if nonrt:
            summary = f"{NONREALTIME_WARNING} {summary}"
        method = (
            f"eds.rx.sweep.Sweeper [{fmt_hz(start, 4)}, {fmt_hz(stop, 4)}] aralığını {fmt_hz(sw.step_hz, 4)} "
            "adımlarla tarar; her adımda tune/settle/yakalama/işleme süreleri perf_counter ile ölçülür ve tarama hızı "
            f"= açıklık / duvar saati süresi olarak hesaplanır (değer: referans konfigürasyonun {len(rates)} "
            "taramasının medyanı; tespit dahil). Pluto'da adım süresine retune maliyeti hâkimdir: libiio öznitelik "
            "yazma (USB) + PLL kilidi + bayat çekirdek tamponlarının ((kernel_buffers + 1) × buffer_size örnek) "
            "atılması; AD9361 fastlock profilleri t_tune'u kısaltacak gelecekteki optimizasyondur. Tam bant yeniden "
            "ziyaret süresi ölçülen hızla resmî aralığa ölçeklenir ve ardışık taramalar arası boşluk eklenir; bundan "
            "kısa süreli sinyaller kaçırılabilir (POI)."
        )
        res = self.new_result(value, summary=summary, method=method, conditions=conditions, extra=extra,
                              uncertainty=unc)
        if nonrt:
            res.blockers.append(REASON_NONREALTIME)
        res.table = self._table(runs)
        res.figures = [self._breakdown_figure(runs, start, stop), self._screenshot(last, start, stop, value)]
        return res

    @staticmethod
    def _table(runs: list[ConfigRun]) -> list[dict[str, Any]]:
        rows = []
        for r in runs:
            for k, s in enumerate(r.sweeps):
                n = max(s.n_steps, 1)
                gap = (r.sweeps[k + 1].t_start_ns - s.t_end_ns) * 1e-9 if k + 1 < len(r.sweeps) else None
                rows.append({
                    "config": r.label, "repeat": k + 1, "fs_hz": r.cfg.fs, "rbw_hz": s.rbw_hz, "nfft": s.nfft,
                    "averages": s.averages, "n_steps": s.n_steps, "step_hz": s.step_hz, "duration_s": s.duration_s,
                    "rate_ghz_per_s": s.rate_hz_per_s / 1e9,
                    **{f"{key}_ms": 1e3 * s.timing[f"{key}_s"] / n for key in TIMING_KEYS},
                    "other_ms": 1e3 * s.timing.get("other_s", 0.0) / n,
                    "detections": len(s.detections), "overflow_steps": sum(int(st["overflow"]) for st in s.steps),
                    "gap_s": gap,
                })
        return rows

    @staticmethod
    def _breakdown_figure(runs: list[ConfigRun], start: float, stop: float) -> FigureSpec:
        """Stacked bars per configuration: drawn as cumulative bars, largest first, so the
        visible slice of each bar is one timing component. Every bar is named on the
        categorical x axis by its configuration (fs, actual RBW / N, averages)."""
        x = [float(r.cfg.index + 1) for r in runs]
        per = [r.per_step_ms() for r in runs]
        cum = np.cumsum([[m[k] for k in TIMING_KEYS] for m in per], axis=1)      # (n_cfg, 4)
        series = [Series(x=x, y=cum[:, i].tolist(), label=TIMING_LABELS[k], style="bar")
                  for i, k in reversed(list(enumerate(TIMING_KEYS)))]
        markers = [(xi, float(c[-1]), f"{c[-1]:.2f} ms/adım, {r.rate_ghz:.3f} GHz/s")
                   for xi, c, r in zip(x, cum, runs)]
        top = float(np.max(cum[:, -1])) if cum.size else 1.0
        return FigureSpec(
            name="scan_speed_step_breakdown",
            title="Tarama hızı — adım başına süre dökümü (tune / settle / yakalama / işleme)",
            kind="xy", series=series, xlabel="Tarama konfigürasyonu", ylabel="Adım başına ortalama süre [ms]",
            markers=markers, xlim=(0.4, max(x, default=1.0) + 0.6), ylim=(0.0, 1.35 * top if top > 0 else 1.0),
            info={"Aralık": f"{fmt_hz(start, 4)} – {fmt_hz(stop, 4)}", "Gösterim": "yığılmış çubuk"},
            xticks=[(xi, r.label.replace(", ", "\n")) for xi, r in zip(x, runs)],
        )

    @staticmethod
    def _screenshot(res: SweepResult, start: float, stop: float, rate_ghz: float) -> FigureSpec:
        spec = res.to_spectrum()
        dets = sorted(res.detections, key=lambda d: d.snr_db, reverse=True)[:MAX_MARKERS]
        markers = [(d.f_peak_hz, d.peak_db, f"SNR {d.snr_db:.0f} dB") for d in dets]
        title = (f"Birleştirilmiş tarama spektrumu (max-hold) — {res.n_steps} adım × {fmt_hz(res.step_hz, 4)}, "
                 f"{res.duration_s:.2f} s, {rate_ghz:.3f} GHz/s, {len(res.detections)} tespit")
        # only keys the screenshot already has are overridden (the info bar has room for 6 per row)
        return spectrum_screenshot(spec, "scan_speed_sweep_spectrum", title, markers=markers,
                                   extra_info={"Center": fmt_hz(0.5 * (start + stop), 6),
                                               "Span": fmt_hz(stop - start, 4)})
