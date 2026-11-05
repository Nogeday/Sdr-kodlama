"""Duyarlılık [dBm] — the smallest input power the CFAR detector finds with ``Pd = pd_target``.

Part A — noise floor (always)
    1. DUT at ``fc``, ``fs``, ``bw = fs``, the band's maximum gain (manual), tracking on.
    2. Level reference (:func:`eds.measure.common.level_reference`: calibration table → in-situ
       absolute source → uncalibrated dBFS) at that gain.
    3. Terminated input: a long-average noise spectrum (``noise_averages``) → noise power per
       RBW ``N_RBW`` = Gamma-median-corrected median over the usable band (DC notch excluded;
       the median of ``Gamma(n_eff)`` noise bins lies ``gamma_quantile(0.5, n_eff)/n_eff`` below
       the mean, the correction makes it unbiased) → ``DANL = N_RBW − 10·log10(RBW)``,
       ``NF = DANL[dBm/Hz] − kT`` (only when calibrated) and ``MDS(B) = DANL + 10·log10(B) +
       SNR_min`` for every ``B`` in ``mds_bandwidths``. ``fa_trials`` detector runs on the
       terminated input give the empirical false-alarm rate.
    4. ``SNR_min`` (= ``snr_req_db`` when given): the per-bin SNR at which a bin-centred tone
       crosses the detector's threshold ``α·N`` with probability ``pd_target``. ``α`` is the
       real detector multiplier (OS/CA-CFAR finite-M correction at the per-pass Bonferroni
       share of ``detect.pfa``, floored by ``min_snr_db``), the tone bin is a non-central χ²
       with ``2·n_eff`` degrees of freedom (``n_eff`` = Welch effective averages), evaluated
       with the Patnaik (moment-matched Gamma) approximation (:func:`required_snr_db`).

Part B — detection probability curve (stimulus source present)
    The tone sits on a bin centre at ``fc + offset_frac·fs`` (apparent frequency: a high-SNR
    probe measures the in-situ reference error ``e`` and the source is set to ``f·(1 + e)``).
    Starting at ``start_snr_db`` per bin above ``N_RBW`` the level steps down by
    ``coarse_step_db`` until ``Pd < pd_stop``, then a ``fine_step_db`` grid fills the
    transition. At each level ``trials`` independent settled captures (one flush after the
    level change, then non-overlapping records) run through :func:`eds.dsp.detect.detect`
    (``[detect]`` settings, usable-band mask); a hit is a detection whose peak lies within
    ``±hit_tolerance_bins`` of the tone. The Pd points are made monotone (weighted isotonic
    regression) and the ``pd_target`` crossing is interpolated linearly in dB. Uncertainty
    (≈95 %): the binomial half-width at ``pd_target`` divided by the local Pd slope, RSS with
    the level-reference / generator uncertainty.

Headline value: Part B when it ran, else ``MDS(RBW)`` (the summary says which). Without any
absolute reference the result stays in dBFS (``unit = "dBFS"``, ``calibrated = False``).

Shared tone helpers
-------------------
The second half of this module holds the stimulus helpers used by the other receiver specs
(``dynamic_range``, ``inst_bw``, ``image_rejection``, ``freq_resolution``): parameter
validation, settled captures, tone location / scalloping-free tone power with noise
correction, adaptive tone level (:func:`adjust_tone`) and the in-situ reference error
(:class:`ToneProbe`).
"""

from __future__ import annotations

import logging
import math
from dataclasses import dataclass
from typing import Any

import numpy as np

from ...core.config import DetectConfig
from ...core.errors import ConfigError, MeasurementError
from ...core.units import KT_DBM_HZ, db10, fmt_hz, lin10
from ...dsp.detect import detect, estimate_noise, resolution_passes, usable_mask
from ...dsp.psd import Spectrum, spectrum_from_block
from ...dsp.stats import regularized_gamma_q
from ..base import FigureSpec, Measurement, MeasurementResult, Series, register
from ..common import (
    LevelRef,
    base_conditions,
    setup_rx,
    spectrum_screenshot,
    tone,
    welch_len,
)
from ..stimulus import (  # noqa: F401
    TONE_HALFWIDTH_BINS,
    Z95,
    ToneProbe,
    adjust_tone,
    as_float,
    as_int,
    level_uncertainty_db,
    nearest_bin,
    noise_mean_db,
    noise_mean_lin,
    opt_float,
    qparam,
    reference_level,
    settled_capture,
    source_power_range,
    tone_band_db,
    valid_bins,
)

log = logging.getLogger(__name__)

# =============================================================================== detection model
def patnaik_pd(snr_db: float, alpha: float, n_eff: float) -> float:
    """Detection probability of a bin-centred tone (per-bin SNR ``snr_db``) against the
    threshold ``alpha · N`` in an ``n_eff``-average periodogram.

    ``X · 2n/N`` is non-central χ² with ``2n`` degrees of freedom and non-centrality
    ``λ = 2n·S/N``; it is approximated by the moment-matched Gamma (Patnaik):
    mean ``2n + λ``, variance ``4n + 4λ``."""
    n = float(n_eff)
    lam = 2.0 * n * 10.0 ** (snr_db / 10.0)
    mu = 2.0 * n + lam
    var = 4.0 * n + 4.0 * lam
    k, theta = mu * mu / var, var / mu
    return float(regularized_gamma_q(k, 2.0 * n * float(alpha) / theta))


def required_snr_db(pd: float, alpha: float, n_eff: float) -> float:
    """Per-bin SNR (dB; tone power / noise mean in one RBW) for detection probability ``pd``
    (inverse of :func:`patnaik_pd`, bisection to 1e-4 dB)."""
    pd = as_float(pd, "pd", 1e-6, 1.0 - 1e-6)
    if not (alpha > 0 and n_eff > 0):
        raise ConfigError(f"alpha ve n_eff pozitif olmalı (alpha={alpha!r}, n_eff={n_eff!r})")
    lo, hi = -40.0, 60.0
    if patnaik_pd(lo, alpha, n_eff) >= pd:
        return lo
    while hi - lo > 1e-4:
        mid = 0.5 * (lo + hi)
        if patnaik_pd(mid, alpha, n_eff) >= pd:
            hi = mid
        else:
            lo = mid
    return 0.5 * (lo + hi)


@dataclass(frozen=True)
class DetectorModel:
    """Threshold model of the configured detector for one spectrum configuration."""

    alpha: float              # effective multiplier on the noise mean (CFAR α floored by min_snr)
    n_eff: float              # effective Welch averages
    pfa_pass: float           # per-pass Bonferroni share of detect.pfa
    n_passes: int


def detector_model(spec: Spectrum, cfg: DetectConfig, valid: np.ndarray) -> DetectorModel:
    """Multiplier the native CFAR pass of :func:`eds.dsp.detect.detect` uses on ``spec``."""
    n_passes = 1 + len(resolution_passes(spec, cfg, valid_mask=valid))
    pfa_pass = float(cfg.pfa) / n_passes
    est = estimate_noise(spec, cfg, pfa=pfa_pass)
    if cfg.method == "fixed":
        alpha = float(lin10(float(cfg.fixed_threshold))) / noise_mean_lin(spec, valid)
    else:
        alpha = float(est.alpha)
    alpha = max(alpha, float(lin10(float(cfg.min_snr_db))))
    return DetectorModel(alpha=alpha, n_eff=float(est.n_eff), pfa_pass=pfa_pass, n_passes=n_passes)


def isotonic_increasing(y: np.ndarray, w: np.ndarray) -> np.ndarray:
    """Weighted least-squares non-decreasing fit (pool-adjacent-violators)."""
    blocks: list[list[float]] = []          # [value, weight, count]
    for yi, wi in zip(np.asarray(y, float), np.asarray(w, float)):
        blocks.append([float(yi), float(wi), 1.0])
        while len(blocks) > 1 and blocks[-2][0] > blocks[-1][0]:
            v2, w2, c2 = blocks.pop()
            v1, w1, c1 = blocks.pop()
            blocks.append([(v1 * w1 + v2 * w2) / (w1 + w2), w1 + w2, c1 + c2])
    out: list[float] = []
    for v, _, c in blocks:
        out.extend([v] * int(c))
    return np.asarray(out, dtype=float)


def wilson_halfwidth(p: float, n: int, z: float = Z95) -> float:
    """Half-width of the Wilson score interval of a binomial proportion."""
    if n <= 0:
        return math.nan
    return z * math.sqrt(p * (1.0 - p) / n + z * z / (4.0 * n * n)) / (1.0 + z * z / n)


# =============================================================================== measurement
@dataclass
class _Level:
    snr_db: float             # commanded tone power above N_RBW (dB, prediction)
    setting: float
    p_in: float               # input power in the result unit
    hits: int
    trials: int
    tone_dbfs: float          # measured (trial-averaged, noise-corrected) tone power
    phase: str                # "kaba" | "ince"

    @property
    def pd(self) -> float:
        return self.hits / self.trials if self.trials else math.nan


@dataclass
class _NoiseFloor:
    spec: Spectrum
    n_rbw_dbfs: float
    danl_dbfs_hz: float
    model: DetectorModel
    fa_trials: int
    fa_detections: int
    fa_bins: int
    fa_captures_hit: int
    valid_bins: int


@register
class SensitivityMeasurement(Measurement):
    spec_id = "sensitivity"
    title = "Duyarlılık (MDS)"
    unit = "dBm"
    default_params = {
        "fc": None,                   # None → rx.center_freq_hz
        "fs": 2e6,
        "bw": None,                   # None → fs
        "gain": None,                 # None → maximum gain of the band at fc
        "nfft": 4096,
        "averages": 8,
        "window": "blackmanharris",
        "overlap": 0.5,
        "pd_target": 0.9,
        "trials": 20,
        "quick_trials": 10,
        "snr_req_db": None,           # None → derived from the CFAR threshold (Pd at Pfa)
        "mds_bandwidths": ["rbw", 25e3, 200e3, 1e6],
        "offset_frac": 0.15,          # tone at fc + offset_frac·fs (bin centre)
        "start_snr_db": 20.0,         # first Pd level: tone power above N_RBW
        "coarse_step_db": 2.0,
        "fine_step_db": 0.5,
        "pd_stop": 0.2,               # coarse descent stops below this Pd
        "hit_tolerance_bins": 2,
        "danl_fraction": 0.5,         # noise floor from the flat central ±danl_fraction/2·fs (DC notch excluded)
        "noise_averages": 64,
        "quick_noise_averages": 32,
        "fa_trials": 40,
        "quick_fa_trials": 20,
        "max_levels": 40,
        "level_uncertainty_db": None,  # None → automatic (see level_uncertainty_db)
        "mds_warn_db": 3.0,            # |P_sens − MDS(RBW)| above this → warning in the result
    }

    # ------------------------------------------------------------------ parameters
    def _params(self, dev: Any) -> dict[str, Any]:
        p, app = self.p, self.ctx.app
        fc = as_float(p["fc"] if p["fc"] is not None else app.rx.center_freq_hz, "sensitivity.fc", 1.0)
        fs = as_float(p["fs"], "sensitivity.fs", 1.0)
        bw = as_float(p["bw"], "sensitivity.bw", 1.0) if p["bw"] is not None else fs
        gain = opt_float(p["gain"], "sensitivity.gain")
        if gain is None:
            gain = float(dev.gain_range(fc).max_db)
        bws = p["mds_bandwidths"]
        if not isinstance(bws, (list, tuple)) or not bws:
            raise ConfigError(f"sensitivity.mds_bandwidths boş olmayan liste olmalı, {bws!r} geldi")
        mds_bw: list[float | str] = []
        for i, b in enumerate(bws):
            if isinstance(b, str):
                if b.lower() != "rbw":
                    raise ConfigError(f"sensitivity.mds_bandwidths[{i}]: sayı ya da 'rbw' olmalı, {b!r} geldi")
                mds_bw.append("rbw")
            else:
                mds_bw.append(as_float(b, f"sensitivity.mds_bandwidths[{i}]", 1e-3))
        usable = float(app.dsp.usable_fraction)
        prm = {
            "fc": fc, "fs": fs, "bw": bw, "gain": gain,
            "nfft": as_int(p["nfft"], "sensitivity.nfft", 64),
            "averages": as_int(p["averages"], "sensitivity.averages"),
            "window": str(p["window"]),
            "overlap": as_float(p["overlap"], "sensitivity.overlap", 0.0, 0.95),
            "pd_target": as_float(p["pd_target"], "sensitivity.pd_target", 0.05, 0.995),
            "trials": as_int(qparam(self, "trials"), "sensitivity.trials", 2),
            "snr_req_db": opt_float(p["snr_req_db"], "sensitivity.snr_req_db", -20.0, 60.0),
            "mds_bandwidths": mds_bw,
            "offset_frac": as_float(p["offset_frac"], "sensitivity.offset_frac", 0.01, 0.5 * usable - 0.01),
            "start_snr_db": as_float(p["start_snr_db"], "sensitivity.start_snr_db", 0.0, 60.0),
            "coarse_step_db": as_float(p["coarse_step_db"], "sensitivity.coarse_step_db", 0.1, 20.0),
            "fine_step_db": as_float(p["fine_step_db"], "sensitivity.fine_step_db", 0.05, 10.0),
            "pd_stop": as_float(p["pd_stop"], "sensitivity.pd_stop", 0.0, 0.99),
            "hit_tolerance_bins": as_int(p["hit_tolerance_bins"], "sensitivity.hit_tolerance_bins", 0),
            "danl_fraction": as_float(p["danl_fraction"], "sensitivity.danl_fraction", 0.05, 1.0),
            "noise_averages": as_int(qparam(self, "noise_averages"), "sensitivity.noise_averages"),
            "fa_trials": as_int(qparam(self, "fa_trials"), "sensitivity.fa_trials", 1),
            "max_levels": as_int(p["max_levels"], "sensitivity.max_levels", 3),
            "level_uncertainty_db": opt_float(p["level_uncertainty_db"], "sensitivity.level_uncertainty_db", 0.0),
            "mds_warn_db": as_float(p["mds_warn_db"], "sensitivity.mds_warn_db", 0.0),
        }
        if prm["pd_stop"] >= prm["pd_target"]:
            raise ConfigError("sensitivity.pd_stop, pd_target'tan küçük olmalı")
        return prm

    # ------------------------------------------------------------------ run
    def run(self) -> MeasurementResult:
        ctx = self.ctx
        dev = ctx.dut
        prm = self._params(dev)
        setup_rx(dev, fc=prm["fc"], fs=prm["fs"], bw=prm["bw"], gain=prm["gain"],
                 tracking={"dc": True, "rf_dc": True, "quadrature": True})
        ref = reference_level(ctx, dev, prm["fc"], nfft=prm["nfft"], averages=prm["averages"], window=prm["window"],
                              overlap=prm["overlap"])
        u_ref = level_uncertainty_db(ctx, ref, prm["level_uncertainty_db"])
        nf = self._part_a(dev, prm)
        snr_req = prm["snr_req_db"] if prm["snr_req_db"] is not None else required_snr_db(
            prm["pd_target"], nf.model.alpha, nf.model.n_eff)
        part_b: dict[str, Any] | None = None
        part_b_error = ""
        if ctx.source is not None:
            try:
                with ctx.isolated_source():
                    part_b = self._part_b(dev, prm, nf, ref)
            except MeasurementError as e:
                part_b_error = str(e)
                log.warning("Duyarlılık Bölüm B tamamlanamadı: %s", e)
            finally:
                ctx.source.set_enabled(False)
        return self._result(dev, prm, ref, u_ref, nf, snr_req, part_b, part_b_error)

    # ------------------------------------------------------------------ part A
    def _part_a(self, dev: Any, prm: dict[str, Any]) -> _NoiseFloor:
        app = self.ctx.app
        nfft, avg, win, ov = prm["nfft"], prm["averages"], prm["window"], prm["overlap"]
        valid = valid_bins(app, nfft)
        central = usable_mask(nfft, prm["danl_fraction"], int(app.dsp.dc_notch_bins))
        with self.ctx.terminated_input():
            blk = settled_capture(dev, welch_len(nfft, prm["noise_averages"], ov))
            spec = spectrum_from_block(blk, nfft, win, ov)
            fa_det = fa_bins = fa_hit = 0
            model: DetectorModel | None = None
            for i in range(prm["fa_trials"]):
                b = dev.capture(welch_len(nfft, avg, ov))
                s = spectrum_from_block(b, nfft, win, ov)
                if i == 0:
                    model = detector_model(s, app.detect, valid)
                dets, _ = detect(s, app.detect, valid_mask=valid)
                fa_det += len(dets)
                fa_bins += sum(d.n_bins for d in dets)
                fa_hit += int(bool(dets))
        assert model is not None
        n_rbw = noise_mean_db(spec, central)
        return _NoiseFloor(spec=spec, n_rbw_dbfs=n_rbw, danl_dbfs_hz=n_rbw - 10.0 * math.log10(spec.rbw_hz),
                           model=model, fa_trials=prm["fa_trials"], fa_detections=fa_det, fa_bins=fa_bins,
                           fa_captures_hit=fa_hit, valid_bins=int(np.count_nonzero(valid)))

    # ------------------------------------------------------------------ part B
    def _input_power(self, setting: float, probe: ToneProbe, ref: LevelRef) -> float:
        """Input power of a source setting in the result unit (dBm or dBFS)."""
        if getattr(self.ctx.source, "absolute_power", False):
            return float(setting)
        dbfs = setting + probe.offset_db
        return dbfs + ref.offset_db if ref.calibrated else dbfs

    def _part_b(self, dev: Any, prm: dict[str, Any], nf: _NoiseFloor, ref: LevelRef) -> dict[str, Any]:
        ctx, app = self.ctx, self.ctx.app
        nfft, avg, win, ov = prm["nfft"], prm["averages"], prm["window"], prm["overlap"]
        fs, fc = float(dev.fs), float(dev.fc)
        df = fs / nfft
        k_off = int(round(prm["offset_frac"] * nfft))
        f_target = fc + k_off * df                         # apparent frequency, bin centre
        valid = valid_bins(app, nfft)
        lo_src, hi_src = source_power_range(ctx.source)
        # high-SNR probe: setting → dBFS offset and the in-situ reference error
        probe = adjust_tone(ctx, dev, f_target, nf.n_rbw_dbfs + 40.0, nfft=nfft, averages=avg, window=win,
                            overlap=ov, ref=ref, tol_db=1.0)
        f_src = f_target * (1.0 + probe.e)
        tol_hz = prm["hit_tolerance_bins"] * df
        trials = prm["trials"]
        n_rec = welch_len(nfft, avg, ov)
        k_t = nearest_bin(nf.spec, f_target)
        notes: list[str] = []
        levels: dict[float, _Level] = {}

        def setting_for(snr: float) -> float:
            return probe.setting + (nf.n_rbw_dbfs + snr - probe.level_dbfs)

        def run_level(snr: float, phase: str) -> _Level | None:
            key = round(snr, 6)
            if key in levels:
                return levels[key]
            s = setting_for(snr)
            if not lo_src <= s <= hi_src:
                notes.append(f"{snr:+.1f} dB SNR seviyesi kaynak aralığı dışında ({s:.1f}; [{lo_src:g}, {hi_src:g}]) "
                             "— atlandı")
                return None
            hits = 0
            acc = np.zeros(nfft)
            with tone(ctx, f_src, s):
                dev.mark_settling()
                for _ in range(trials):
                    blk = dev.capture(n_rec)
                    spec = spectrum_from_block(blk, nfft, win, ov)
                    dets, _ = detect(spec, app.detect, valid_mask=valid)
                    hits += int(any(abs(d.f_peak_hz - f_target) <= tol_hz for d in dets))
                    acc += spec.p_lin
            mean_spec = Spectrum(f_hz=spec.f_hz, p_lin=acc / trials, fc=spec.fc, fs=spec.fs, nfft=nfft,
                                 window=spec.window, enbw_bins=spec.enbw_bins, averages=spec.averages * trials,
                                 meta=dict(spec.meta))
            t_db = tone_band_db(mean_spec, k_t, TONE_HALFWIDTH_BINS, float(lin10(nf.n_rbw_dbfs)))
            lv = _Level(snr_db=snr, setting=s, p_in=self._input_power(s, probe, ref), hits=hits, trials=trials,
                        tone_dbfs=t_db, phase=phase)
            levels[key] = lv
            return lv

        # coarse descent (raised first when the start level does not reach pd_target)
        snr = prm["start_snr_db"]
        first = run_level(snr, "kaba")
        raises = 0
        while (first is None or first.pd < prm["pd_target"]) and raises < 3:
            snr += 6.0
            raises += 1
            first = run_level(snr, "kaba")
        if first is None or first.pd < prm["pd_target"]:
            raise MeasurementError(
                f"Ton {snr:.0f} dB SNR'de bile Pd ≥ {prm['pd_target']:g} vermedi — dedektör/kaynak/kablo kontrol edin")
        cur = snr
        lowest: _Level | None = first
        while len(levels) < prm["max_levels"]:
            cur -= prm["coarse_step_db"]
            lv = run_level(cur, "kaba")
            if lv is None:
                break
            lowest = lv
            if lv.pd < prm["pd_stop"]:
                break
        if lowest is None or lowest.pd >= prm["pd_target"]:
            raise MeasurementError("Pd eşiğinin altına inilemedi (kaynak alt güç sınırı ya da max_levels) — "
                                   "zayıflatıcı ekleyin")
        hi_snr = min(lv.snr_db for lv in levels.values() if lv.pd >= prm["pd_target"])
        x = lowest.snr_db + prm["fine_step_db"]            # fine grid across the whole transition
        while x < hi_snr - 1e-9 and len(levels) < prm["max_levels"]:
            run_level(x, "ince")
            x += prm["fine_step_db"]

        pts = sorted(levels.values(), key=lambda v: v.snr_db)
        snrs = np.array([v.snr_db for v in pts])
        pd = np.array([v.pd for v in pts])
        w = np.array([v.trials for v in pts], dtype=float)
        fit = isotonic_increasing(pd, w)
        target = prm["pd_target"]
        idx = np.flatnonzero(fit >= target)
        if idx.size == 0 or idx[0] == 0:
            raise MeasurementError("Pd eğrisinde pd_target geçişi ölçülen aralıkta değil")
        i = int(idx[0])
        slope = (fit[i] - fit[i - 1]) / (snrs[i] - snrs[i - 1])
        snr_sens = snrs[i - 1] + (target - fit[i - 1]) / slope
        stat_u = wilson_halfwidth(target, trials) / slope
        p_sens = self._input_power(setting_for(snr_sens), probe, ref)
        # screenshot capture at the sensitivity level (+ detector threshold)
        with tone(ctx, f_src, setting_for(snr_sens)):
            blk = settled_capture(dev, n_rec)
        shot = spectrum_from_block(blk, nfft, win, ov)
        dets, thr_db = detect(shot, app.detect, valid_mask=valid)
        return {"levels": pts, "fit": fit, "snr_sens": float(snr_sens), "p_sens": float(p_sens),
                "slope_per_db": float(slope), "stat_u_db": float(stat_u), "probe": probe, "f_src": f_src,
                "f_target": f_target, "tol_hz": tol_hz, "notes": notes, "shot": shot, "shot_thr_db": thr_db,
                "shot_hit": any(abs(d.f_peak_hz - f_target) <= tol_hz for d in dets)}

    # ------------------------------------------------------------------ result
    def _result(self, dev: Any, prm: dict[str, Any], ref: LevelRef, u_ref: float | None, nf: _NoiseFloor,
                snr_req: float, b: dict[str, Any] | None, b_error: str) -> MeasurementResult:
        app = self.ctx.app
        src = self.ctx.source
        absolute_src = bool(getattr(src, "absolute_power", False))
        calibrated = bool(ref.calibrated)
        unit = "dBm" if calibrated else "dBFS"
        off = ref.offset_db if calibrated else 0.0
        rbw = nf.spec.rbw_hz
        danl = nf.danl_dbfs_hz + off
        nf_db = danl - KT_DBM_HZ if calibrated else None
        mds_rows = []
        mds_rbw = danl + 10.0 * math.log10(rbw) + snr_req
        for bwv in prm["mds_bandwidths"]:
            b_hz = rbw if bwv == "rbw" else float(bwv)
            mds_rows.append({"bölüm": "MDS", "bandwidth_hz": b_hz,
                             "mds": danl + 10.0 * math.log10(b_hz) + snr_req, "unit": unit,
                             "snr_req_db": snr_req, "danl": danl})
        fa_total_bins = nf.fa_trials * nf.valid_bins
        pfa_emp = nf.fa_bins / fa_total_bins if fa_total_bins else math.nan
        extra: dict[str, Any] = {
            "calibrated": calibrated, "level_ref": ref.method, "level_offset_db": ref.offset_db,
            f"danl_{unit.lower()}_hz": danl, "danl_dbfs_hz": nf.danl_dbfs_hz, "n_rbw_dbfs": nf.n_rbw_dbfs,
            f"n_rbw_{unit.lower()}": nf.n_rbw_dbfs + off, f"mds_rbw_{unit.lower()}": mds_rbw,
            "snr_req_db": snr_req, "snr_req_source": "parametre" if prm["snr_req_db"] is not None else "CFAR modeli",
            "cfar_alpha_db": float(db10(nf.model.alpha)), "n_eff": nf.model.n_eff, "pfa_pass": nf.model.pfa_pass,
            "detector_passes": nf.model.n_passes,
            "fa_trials": nf.fa_trials, "fa_detections": nf.fa_detections, "fa_captures_with_detection": nf.fa_captures_hit,
            "pfa_empirical": pfa_emp,
            "pfa_empirical_upper95": (3.0 / fa_total_bins if nf.fa_bins == 0 and fa_total_bins else None),
            "mds": {f"{r['bandwidth_hz']:.6g}": r["mds"] for r in mds_rows},
        }
        if nf_db is not None:
            extra["nf_db"] = nf_db
        if u_ref is not None:
            extra["level_uncertainty_db"] = u_ref
        table: list[dict[str, Any]] = []
        figures: list[FigureSpec] = []
        uncertainty: float | None = None
        warnings: list[str] = []
        if b is not None:
            value = b["p_sens"]
            value_source = "Pd eğrisi"
            stat = b["stat_u_db"]
            uncertainty = math.hypot(stat, u_ref) if u_ref is not None else stat
            delta = value - mds_rbw
            extra.update({
                "value_source": "pd_curve", "sensitivity": value, "snr_at_sensitivity_db": b["snr_sens"],
                "pd_slope_per_db": b["slope_per_db"], "stat_uncertainty_db": stat, "trials_per_level": prm["trials"],
                "levels": len(b["levels"]), "tone_hz": b["f_target"], "tone_src_hz": b["f_src"],
                "in_situ_ppm": b["probe"].e * 1e6, "hit_tolerance_hz": b["tol_hz"],
                "sens_minus_mds_db": delta, "screenshot_detected": bool(b["shot_hit"]),
            })
            if b["notes"]:
                extra["part_b_notes"] = list(b["notes"])
            if abs(delta) > prm["mds_warn_db"]:
                warnings.append(f"UYARI: Pd eğrisi ile MDS formülü {delta:+.1f} dB farklı (> {prm['mds_warn_db']:g} dB)")
            for lv, f in zip(b["levels"], b["fit"]):
                table.append({"bölüm": "Pd", "p_in": lv.p_in, "unit": unit, "snr_bin_db": lv.snr_db,
                              "tone_meas_dbfs": lv.tone_dbfs, "setting": lv.setting, "hits": lv.hits,
                              "trials": lv.trials, "pd": lv.pd, "pd_fit": float(f), "phase": lv.phase})
        else:
            value = mds_rbw
            value_source = "MDS formülü (kaynak yok)" if src is None else "MDS formülü (Pd eğrisi başarısız)"
            extra["value_source"] = "mds_formula"
            if b_error:
                extra["part_b_error"] = b_error
                warnings.append(f"Pd eğrisi ölçülemedi: {b_error}")
        table.extend(mds_rows)
        if not calibrated:
            warnings.append("kalibrasyonsuz — değer dBFS'tir, jüriye dBm diye sunulamaz")
        cfg = app.detect
        conditions = base_conditions(
            dev, nf.spec, averages=prm["averages"], noise_averages=prm["noise_averages"],
            detector=cfg.method, pfa=cfg.pfa, guard_bins=cfg.guard_bins, train_bins=cfg.train_bins,
            os_rank=cfg.os_rank, min_snr_db=cfg.min_snr_db, resolutions=list(cfg.resolutions),
            pd_target=prm["pd_target"], trials=prm["trials"], level_ref=ref.method, calibrated=calibrated,
            source=getattr(src, "name", "yok") if src is not None else "yok", absolute_source=absolute_src,
        )
        conditions["averages"] = prm["averages"]
        g = float(dev.gains[0])
        summary = (f"Duyarlılık {value:.1f} {unit} ({value_source}; Pd = {prm['pd_target']:g}, Pfa = {cfg.pfa:g}, "
                   f"RBW {fmt_hz(rbw, 4)}, {prm['averages']} ort., G = {g:g} dB, {fmt_hz(prm['fc'])}"
                   + (f", {prm['trials']} deneme/seviye" if b is not None else "") + f"); DANL {danl:.1f} {unit}/Hz"
                   + (f", NF {nf_db:.1f} dB" if nf_db is not None else "") + f", MDS(RBW) {mds_rbw:.1f} {unit}")
        if warnings:
            summary += "; " + "; ".join(warnings)
        method = (
            "Giriş sonlandırılarak uzun ortalamalı gürültü spektrumunun düz merkez bölgesinde "
            f"(±{prm['danl_fraction'] / 2:g}·fs, DC çentiği hariç) Gamma-medyan düzeltmeli gürültü tabanı N_RBW, "
            "DANL = N_RBW − 10·log10(RBW) ve NF = DANL + 174 hesaplanır; MDS(B) = DANL + 10·log10(B) + SNR_min, "
            "SNR_min dedektörün gerçek CFAR eşiğinden (α, Pfa payı, n_eff ortalama) Pd = "
            f"{prm['pd_target']:g} için merkezi olmayan χ² (Patnaik) modeliyle bulunur. Kaynak varsa bin merkezine "
            f"oturtulmuş ton ~{prm['start_snr_db']:g} dB SNR'den {prm['coarse_step_db']:g} dB adımlarla "
            f"Pd < {prm['pd_stop']:g} olana kadar, geçiş bölgesinde {prm['fine_step_db']:g} dB adımlarla indirilir; "
            "her seviyede bağımsız settled yakalamalar aynı "
            "CFAR dedektöründen (eds.dsp.detect, [detect] ayarları) geçirilir, Pd = isabet/deneme. Monoton "
            "(izotonik) Pd eğrisinin hedef geçişi dB ekseninde enterpolasyonla bulunur; belirsizlik = binom "
            "güven aralığı / Pd eğimi ⊕ seviye referansı belirsizliği (≈%95). Seviye referansı: "
            f"{ref.method}.")
        res = self.new_result(float(value), summary=summary, method=method, conditions=conditions, extra=extra,
                              uncertainty=uncertainty)
        res.unit = unit
        res.table = table
        figures.extend(self._figures(dev, prm, nf, b, unit, off, value, snr_req))
        res.figures = figures
        return res

    def _figures(self, dev: Any, prm: dict[str, Any], nf: _NoiseFloor, b: dict[str, Any] | None, unit: str,
                 off: float, value: float, snr_req: float) -> list[FigureSpec]:
        figs: list[FigureSpec] = []
        cfg = self.ctx.app.detect
        if b is not None:
            pts = b["levels"]
            x = [lv.p_in for lv in pts]
            figs.append(FigureSpec(
                name="sensitivity_pd_curve", title=f"Tespit olasılığı – giriş gücü (Pfa = {cfg.pfa:g})", kind="xy",
                series=[Series(x=x, y=[lv.pd for lv in pts], label="Ölçülen Pd (%95 Wilson)", style="marker",
                               yerr=[wilson_halfwidth(lv.pd, lv.trials) for lv in pts]),
                        Series(x=x, y=[float(v) for v in b["fit"]], label="Monoton (izotonik) uyum", style="line")],
                xlabel=f"Giriş gücü [{unit}]", ylabel="Pd", ylim=(-0.05, 1.05),
                hlines=[(prm["pd_target"], f"Pd = {prm['pd_target']:g}")],
                vlines=[(value, f"P_sens = {value:.1f} {unit}")],
                info={"RBW": fmt_hz(nf.spec.rbw_hz, 4), "Deneme": str(prm["trials"]), "Pfa": f"{cfg.pfa:g}"}))
            shot = b["shot"]
            fig = spectrum_screenshot(
                shot, "sensitivity_at_level",
                f"Duyarlılık seviyesinde spektrum ({value:.1f} {unit}, Pfa = {cfg.pfa:g})",
                offset_db=off, unit=unit, max_points=shot.n_bins,
                markers=[(b["f_target"], float(shot.p_db[nearest_bin(shot, b["f_target"])]) + off,
                          f"ton {value:.1f} {unit}")])
            fig.series.append(Series(x=(shot.f_hz / 1e6).tolist(), y=(np.asarray(b["shot_thr_db"]) + off).tolist(),
                                     label="CFAR eşiği"))
            fig.series[0].label = "Spektrum"
            figs.append(fig)
        figs.append(spectrum_screenshot(
            nf.spec, "sensitivity_noise_floor",
            f"Gürültü tabanı (giriş sonlandırılmış): N_RBW {nf.n_rbw_dbfs + off:.1f} {unit}, "
            f"DANL {nf.danl_dbfs_hz + off:.1f} {unit}/Hz", offset_db=off, unit=unit,
            max_points=nf.spec.n_bins, ref_level=nf.n_rbw_dbfs + off + 40.0))
        return figs
