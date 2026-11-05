"""Dinamik alan [dB] — instantaneous DR, SFDR and gain-managed total DR.

(a) Instantaneous DR at a fixed gain (headline value)::

        DR_inst = P_max [dBFS] − N_RBW [dBFS]          (same gain, same RBW)

    ``N_RBW`` = noise power in one RBW with the input terminated (Gamma-median-corrected
    median of the flat central band). ``P_max`` = the strongest *non-clipping* CW tone: the
    tone is raised in ``lin_step_db`` steps until the capture clips (``clip_fraction > 0``) or
    its per-rail peak reaches ``clip_peak_dbfs`` (:func:`eds.dsp.levels.iq_levels`), then the
    last-good / first-clipping bracket is bisected down to ``bisect_db``. The stepped points
    form the input→output linearity curve. Without a source ``DR_inst = 0 dBFS − N_RBW``
    (full-scale CW assumed, stated in the method).

(b) SFDR: a CW tone at ``fc + (tone_offset_frac + tone_detune_frac)·fs`` (fs/8 plus a small
    detuning so harmonics/images do not coincide) at ``sfdr_level_dbfs``; the strongest
    component in the usable band outside the carrier main lobe (``±main_lobe_bins``) and the
    DC notch — the IQ image and digital spurs INCLUDED — is the dominant spur::

        SFDR [dBc]  = P_carrier − P_spur,      SFDR [dBFS] = 0 dBFS − P_spur

    The value excluding the IQ image is reported separately. Spur power = noise-corrected
    band power (``±5`` bins); a spur less than 10 dB above the noise makes SFDR a lower bound.

(c) Total (gain-managed) DR::

        DR_total = P_in,max(G_min) [dBm] − MDS_RBW(G_max) [dBm]

    ``P_in,max(G_min)`` from the same clip search at the minimum gain (in-situ / table level
    reference), ``MDS = N_RBW(G_max) + SNR_min`` with ``SNR_min`` from the detector model
    (:func:`eds.measure.specs.sensitivity.required_snr_db`, Pd = ``pd_target``). Without an
    absolute reference: level-model estimate (``dBFS ≈ dBm + G − 5``), flagged in the result.

A noise-floor-vs-gain sweep gives ``DR_inst(G) = P_max − N_RBW(G)`` over the gain range.
"""

from __future__ import annotations

import logging
import math
from dataclasses import dataclass
from typing import Any

import numpy as np

from ...core.errors import ConfigError, MeasurementError
from ...core.units import db10, fmt_hz
from ...dsp.detect import usable_mask
from ...dsp.levels import iq_levels
from ...dsp.psd import Spectrum, spectrum_from_block
from ..base import FigureSpec, Measurement, MeasurementResult, Series, register
from ..common import LevelRef, base_conditions, setup_rx, spectrum_screenshot, tone, welch_len
from ..stimulus import (
    LEVEL_MODEL_OFFSET_DB,
    TONE_HALFWIDTH_BINS,
    VISIBLE_SNR_DB,
    ToneProbe,
    adjust_tone,
    as_float,
    as_int,
    known_search_hz,
    level_uncertainty_db,
    locate_peak,
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
from .sensitivity import (
    detector_model,
    required_snr_db,
)

log = logging.getLogger(__name__)


@dataclass
class _LinPoint:
    setting: float
    p_in: float               # input power (result unit: dBm or dBFS-equivalent)
    tone_dbfs: float          # measured tone power at the ADC (noise corrected)
    peak_dbfs: float          # per-rail time-domain peak
    clip_fraction: float
    phase: str                # "adım" | "ikiye bölme"

    @property
    def clipped(self) -> bool:
        return self.clip_fraction > 0.0


@dataclass
class _ClipSearch:
    points: list[_LinPoint]
    ok: _LinPoint | None      # strongest non-clipping point
    clip: _LinPoint | None    # weakest clipping point (None: source limit reached)
    limited: bool             # True: source maximum reached before clipping (P_max is a lower bound)


@register
class DynamicRangeMeasurement(Measurement):
    spec_id = "dynamic_range"
    title = "Dinamik alan"
    unit = "dB"
    default_params = {
        "fc": None,                    # None → rx.center_freq_hz
        "fs": 4e6,
        "bw": None,                    # None → fs
        "gain": 40.0,
        "nfft": 8192,
        "averages": 8,
        "window": "blackmanharris",
        "overlap": 0.5,
        "noise_averages": 32,
        "quick_noise_averages": 16,
        "danl_fraction": 0.5,          # noise floor from the flat central ±danl_fraction/2·fs
        "tone_offset_frac": 0.125,     # fs/8 ...
        "tone_detune_frac": 0.0107,    # ... plus a small detuning
        "sfdr_level_dbfs": -3.0,
        "main_lobe_bins": 6,
        "clip_peak_dbfs": -0.5,
        "bisect_db": 0.5,
        "lin_start_dbfs": -70.0,
        "lin_step_db": 5.0,
        "quick_lin_step_db": 10.0,
        "gain_curve_step_db": 10.0,
        "quick_gain_curve_step_db": 20.0,
        "total": True,                 # measure the gain-managed total DR
        "pd_target": 0.9,              # Pd of the MDS used in DR_total
        "level_uncertainty_db": None,
    }

    # ------------------------------------------------------------------ parameters
    def _params(self) -> dict[str, Any]:
        p, app = self.p, self.ctx.app
        fs = as_float(p["fs"], "dynamic_range.fs", 1.0)
        usable = float(app.dsp.usable_fraction)
        prm = {
            "fc": as_float(p["fc"] if p["fc"] is not None else app.rx.center_freq_hz, "dynamic_range.fc", 1.0),
            "fs": fs,
            "bw": as_float(p["bw"], "dynamic_range.bw", 1.0) if p["bw"] is not None else fs,
            "gain": as_float(p["gain"], "dynamic_range.gain"),
            "nfft": as_int(p["nfft"], "dynamic_range.nfft", 64),
            "averages": as_int(p["averages"], "dynamic_range.averages"),
            "window": str(p["window"]),
            "overlap": as_float(p["overlap"], "dynamic_range.overlap", 0.0, 0.95),
            "noise_averages": as_int(qparam(self, "noise_averages"), "dynamic_range.noise_averages"),
            "danl_fraction": as_float(p["danl_fraction"], "dynamic_range.danl_fraction", 0.05, 1.0),
            "tone_frac": as_float(p["tone_offset_frac"], "dynamic_range.tone_offset_frac", 0.01, 0.45)
            + as_float(p["tone_detune_frac"], "dynamic_range.tone_detune_frac", -0.1, 0.1),
            "sfdr_level_dbfs": as_float(p["sfdr_level_dbfs"], "dynamic_range.sfdr_level_dbfs", -60.0, -0.5),
            "main_lobe_bins": as_int(p["main_lobe_bins"], "dynamic_range.main_lobe_bins", 1),
            "clip_peak_dbfs": as_float(p["clip_peak_dbfs"], "dynamic_range.clip_peak_dbfs", -20.0, 0.0),
            "bisect_db": as_float(p["bisect_db"], "dynamic_range.bisect_db", 0.01, 10.0),
            "lin_start_dbfs": as_float(p["lin_start_dbfs"], "dynamic_range.lin_start_dbfs", -150.0, -1.0),
            "lin_step_db": as_float(qparam(self, "lin_step_db"), "dynamic_range.lin_step_db", 0.5, 30.0),
            "gain_curve_step_db": as_float(qparam(self, "gain_curve_step_db"), "dynamic_range.gain_curve_step_db",
                                           1.0, 100.0),
            "total": bool(p["total"]),
            "pd_target": as_float(p["pd_target"], "dynamic_range.pd_target", 0.05, 0.995),
            "level_uncertainty_db": opt_float(p["level_uncertainty_db"], "dynamic_range.level_uncertainty_db", 0.0),
        }
        if not abs(prm["tone_frac"]) < 0.5 * usable:
            raise ConfigError(f"dynamic_range: ton ofseti {prm['tone_frac']:.3f}·fs kullanılabilir bant dışında")
        return prm

    # ------------------------------------------------------------------ run
    def run(self) -> MeasurementResult:
        ctx = self.ctx
        dev = ctx.dut
        prm = self._params()
        setup_rx(dev, fc=prm["fc"], fs=prm["fs"], bw=prm["bw"], gain=prm["gain"],
                 tracking={"dc": True, "rf_dc": True, "quadrature": True})
        g_fix = float(dev.gains[0])
        ref = reference_level(ctx, dev, prm["fc"], nfft=prm["nfft"], averages=prm["averages"],
                              window=prm["window"], overlap=prm["overlap"])
        gr = dev.gain_range(prm["fc"])
        g_min, g_max = float(gr.min_db), float(gr.max_db)
        noise = self._noise_vs_gain(dev, prm, g_fix, g_min, g_max)
        out: dict[str, Any] = {"ref": ref, "noise": noise, "g_fix": g_fix, "g_min": g_min, "g_max": g_max}
        if ctx.source is not None:
            try:
                with ctx.isolated_source():
                    self._with_source(dev, prm, out)
            finally:
                ctx.source.set_enabled(False)
                dev.set_gain(g_fix)
        if prm["total"]:
            self._total(dev, prm, out)
            dev.set_gain(g_fix)
        return self._result(dev, prm, out)

    # ------------------------------------------------------------------ noise
    def _noise_vs_gain(self, dev: Any, prm: dict[str, Any], g_fix: float, g_min: float,
                       g_max: float) -> dict[str, Any]:
        """Terminated-input noise per RBW at the fixed gain and over the gain range."""
        app = self.ctx.app
        nfft, ov, win = prm["nfft"], prm["overlap"], prm["window"]
        central = usable_mask(nfft, prm["danl_fraction"], int(app.dsp.dc_notch_bins))
        step = prm["gain_curve_step_db"]
        gains = sorted({g_min, g_max, g_fix, *np.arange(g_min, g_max, step).tolist()})
        per_gain: dict[float, float] = {}
        spec_fix: Spectrum | None = None
        model_spec: Spectrum | None = None
        with self.ctx.terminated_input():
            for g in gains:
                g_act = float(dev.set_gain(g))
                blk = settled_capture(dev, welch_len(nfft, prm["noise_averages"], ov))
                spec = spectrum_from_block(blk, nfft, win, ov)
                per_gain[g_act] = noise_mean_db(spec, central)
                if abs(g_act - g_fix) < 1e-9:
                    spec_fix = spec
                    model_spec = spectrum_from_block(dev.capture(welch_len(nfft, prm["averages"], ov)), nfft, win, ov)
            dev.set_gain(g_fix)
        assert spec_fix is not None and model_spec is not None
        model = detector_model(model_spec, app.detect, valid_bins(app, nfft))
        return {"per_gain": per_gain, "spec": spec_fix, "n_rbw_dbfs": per_gain[g_fix], "model": model}

    # ------------------------------------------------------------------ source part
    def _input_power(self, setting: float, dbfs: float, ref: LevelRef) -> float:
        if getattr(self.ctx.source, "absolute_power", False):
            return float(setting)
        return dbfs + ref.offset_db if ref.calibrated else dbfs

    def _measure(self, dev: Any, prm: dict[str, Any], f_src: float, f_app: float, setting: float, ref: LevelRef,
                 phase: str) -> _LinPoint:
        nfft, avg, win, ov = prm["nfft"], prm["averages"], prm["window"], prm["overlap"]
        with tone(self.ctx, f_src, setting):
            blk = settled_capture(dev, welch_len(nfft, avg, ov))
        lv = iq_levels(blk.samples[0], blk.clip_level)
        spec = spectrum_from_block(blk, nfft, win, ov)
        nz = noise_mean_lin(spec, valid_bins(self.ctx.app, nfft))
        k = locate_peak(spec, f_app, known_search_hz(spec.df_hz))
        t_db = tone_band_db(spec, k, TONE_HALFWIDTH_BINS, nz)
        return _LinPoint(setting=float(setting), p_in=self._input_power(setting, t_db, ref), tone_dbfs=t_db,
                         peak_dbfs=lv.peak_dbfs, clip_fraction=lv.clip_fraction, phase=phase)

    def _is_clip(self, pt: _LinPoint, prm: dict[str, Any]) -> bool:
        return pt.clipped or pt.peak_dbfs >= prm["clip_peak_dbfs"]

    def _clip_search(self, dev: Any, prm: dict[str, Any], probe: ToneProbe, f_src: float, ref: LevelRef,
                     start_dbfs: float, step: float) -> _ClipSearch:
        """Raise the tone from ``start_dbfs`` in ``step`` dB until it clips, then bisect."""
        lo_src, hi_src = source_power_range(self.ctx.source)
        f_app = probe.f_app_hz
        s = float(np.clip(probe.setting + (start_dbfs - probe.level_dbfs), lo_src, hi_src))
        pts: list[_LinPoint] = []
        ok: _LinPoint | None = None
        clip: _LinPoint | None = None
        while True:
            pt = self._measure(dev, prm, f_src, f_app, s, ref, "adım")
            pts.append(pt)
            if self._is_clip(pt, prm):
                clip = pt
                break
            ok = pt
            if s >= hi_src:
                break
            s = min(hi_src, s + step)
        if clip is not None and ok is None:
            raise MeasurementError(f"Ton başlangıç seviyesinde ({start_dbfs:.0f} dBFS hedef) bile kırpıyor")
        while clip is not None and ok is not None and clip.setting - ok.setting > prm["bisect_db"]:
            mid = 0.5 * (ok.setting + clip.setting)
            pt = self._measure(dev, prm, f_src, f_app, mid, ref, "ikiye bölme")
            pts.append(pt)
            if self._is_clip(pt, prm):
                clip = pt
            else:
                ok = pt
        pts.sort(key=lambda q: q.setting)
        return _ClipSearch(points=pts, ok=ok, clip=clip, limited=clip is None)

    def _with_source(self, dev: Any, prm: dict[str, Any], out: dict[str, Any]) -> None:
        ctx = self.ctx
        nfft, avg, win, ov = prm["nfft"], prm["averages"], prm["window"], prm["overlap"]
        fs, fc = float(dev.fs), float(dev.fc)
        ref: LevelRef = out["ref"]
        n_rbw = out["noise"]["n_rbw_dbfs"]
        f_src = fc + prm["tone_frac"] * fs
        probe = adjust_tone(ctx, dev, f_src, -30.0, nfft=nfft, averages=avg, window=win, overlap=ov, ref=ref,
                            tol_db=2.0)
        start = max(prm["lin_start_dbfs"], n_rbw + 15.0)
        cs = self._clip_search(dev, prm, probe, f_src, ref, start, prm["lin_step_db"])
        out["probe"], out["clip_search"] = probe, cs
        # SFDR at sfdr_level_dbfs (re-uses the probe spectrum of the adjusted tone)
        sp = adjust_tone(ctx, dev, f_src, prm["sfdr_level_dbfs"], nfft=nfft, averages=avg, window=win, overlap=ov,
                         setting=probe.setting + (prm["sfdr_level_dbfs"] - probe.level_dbfs), tol_db=0.5,
                         search_hz=known_search_hz(fs / nfft), f_expected=probe.f_app_hz)
        out["sfdr"] = self._sfdr(sp, prm)
        out["sfdr_probe"] = sp

    def _sfdr(self, pr: ToneProbe, prm: dict[str, Any]) -> dict[str, Any]:
        spec = pr.spec
        app = self.ctx.app
        n = spec.n_bins
        k = np.arange(n)
        valid = valid_bins(app, n)
        nz = noise_mean_lin(spec, valid)
        nz_db = float(db10(nz))
        kc = pr.k
        carrier = tone_band_db(spec, kc, TONE_HALFWIDTH_BINS, nz)
        f_img = 2.0 * spec.fc - float(spec.f_hz[kc])
        k_img = nearest_bin(spec, f_img)
        ml = prm["main_lobe_bins"]

        def strongest(mask: np.ndarray) -> dict[str, Any]:
            idx = np.flatnonzero(mask)
            if idx.size == 0:
                raise MeasurementError("SFDR arama bölgesi boş (bant/ana lob ayarlarını kontrol edin)")
            ks = int(idx[np.argmax(spec.p_lin[idx])])
            peak_db = float(spec.p_db[ks])
            limited = peak_db - nz_db < VISIBLE_SNR_DB
            level = peak_db if limited else tone_band_db(spec, ks, TONE_HALFWIDTH_BINS, nz)
            return {"k": ks, "f_hz": float(spec.f_hz[ks]), "level_dbfs": level, "noise_limited": bool(limited),
                    "is_image": abs(ks - k_img) <= 2}

        search = valid & (np.abs(k - kc) > ml)
        s_all = strongest(search)
        s_ex = strongest(search & (np.abs(k - k_img) > ml))
        img_level = tone_band_db(spec, k_img, TONE_HALFWIDTH_BINS, nz)
        return {"carrier_dbfs": carrier, "f_carrier_hz": float(spec.f_hz[kc]), "f_image_hz": f_img,
                "image_dbfs": img_level, "spur": s_all, "spur_ex_image": s_ex, "noise_bin_dbfs": nz_db,
                "sfdr_dbc": carrier - s_all["level_dbfs"], "sfdr_dbfs": -s_all["level_dbfs"],
                "sfdr_ex_image_dbc": carrier - s_ex["level_dbfs"], "spec": spec}

    # ------------------------------------------------------------------ total DR
    def _total(self, dev: Any, prm: dict[str, Any], out: dict[str, Any]) -> None:
        ctx = self.ctx
        nfft, avg, win, ov = prm["nfft"], prm["averages"], prm["window"], prm["overlap"]
        noise = out["noise"]
        g_min, g_max = out["g_min"], out["g_max"]
        model = noise["model"]
        snr_req = required_snr_db(prm["pd_target"], model.alpha, model.n_eff)
        n_max = noise["per_gain"][g_max]
        tot: dict[str, Any] = {"snr_req_db": snr_req, "g_min": g_min, "g_max": g_max, "n_rbw_gmax_dbfs": n_max}
        table = getattr(ctx.cal, "power", None) if ctx.cal is not None else None
        absolute = ctx.source is not None and getattr(ctx.source, "absolute_power", False)
        if ctx.source is not None and (absolute or table is not None):
            try:
                with ctx.isolated_source():
                    dev.set_gain(g_max)
                    ref_max = reference_level(ctx, dev, prm["fc"], nfft=nfft, averages=avg, window=win, overlap=ov)
                    dev.set_gain(g_min)
                    ref_min = reference_level(ctx, dev, prm["fc"], nfft=nfft, averages=avg, window=win, overlap=ov)
                    f_src = float(dev.fc) + prm["tone_frac"] * float(dev.fs)
                    probe = adjust_tone(ctx, dev, f_src, -20.0, nfft=nfft, averages=avg, window=win, overlap=ov,
                                        ref=ref_min, tol_db=2.0)
                    cs = self._clip_search(dev, prm, probe, f_src, ref_min, -20.0, prm["lin_step_db"])
            finally:
                ctx.source.set_enabled(False)
            if cs.ok is None:
                raise MeasurementError("En düşük kazançta kırpmasız ton ölçülemedi")
            p_max_in = cs.ok.p_in
            mds = n_max + ref_max.offset_db + snr_req
            tot.update({"method": "ölçüm", "p_max_in_gmin_dbm": p_max_in, "mds_gmax_dbm": mds,
                        "dr_total_db": p_max_in - mds, "lower_bound": cs.limited,
                        "ref_gmin": ref_min.method, "ref_gmax": ref_max.method})
        elif table is not None:
            off_min = float(table.offset_db(prm["fc"], g_min))
            off_max = float(table.offset_db(prm["fc"], g_max))
            p_max_in = 0.0 + off_min
            mds = n_max + off_max + snr_req
            tot.update({"method": "kalibrasyon tablosu (P_max = 0 dBFS varsayımı)", "p_max_in_gmin_dbm": p_max_in,
                        "mds_gmax_dbm": mds, "dr_total_db": p_max_in - mds, "lower_bound": False})
        else:
            off_min = -(g_min + LEVEL_MODEL_OFFSET_DB)
            off_max = -(g_max + LEVEL_MODEL_OFFSET_DB)
            p_max_in = 0.0 + off_min
            mds = n_max + off_max + snr_req
            tot.update({"method": "seviye modeli tahmini (dBFS ≈ dBm + G − 5; mutlak referans yok)",
                        "p_max_in_gmin_dbm": p_max_in, "mds_gmax_dbm": mds, "dr_total_db": p_max_in - mds,
                        "lower_bound": False, "estimate": True})
        out["total"] = tot

    # ------------------------------------------------------------------ result
    def _result(self, dev: Any, prm: dict[str, Any], out: dict[str, Any]) -> MeasurementResult:
        ctx = self.ctx
        ref: LevelRef = out["ref"]
        noise = out["noise"]
        n_rbw = noise["n_rbw_dbfs"]
        spec_n: Spectrum = noise["spec"]
        cs: _ClipSearch | None = out.get("clip_search")
        sf = out.get("sfdr")
        tot = out.get("total")
        u_ref = level_uncertainty_db(ctx, ref, prm["level_uncertainty_db"])
        unit_in = "dBm" if (ref.calibrated or getattr(ctx.source, "absolute_power", False)) else "dBFS"
        notes: list[str] = []
        if cs is not None and cs.ok is not None:
            p_max = cs.ok.tone_dbfs
            dr_inst = p_max - n_rbw
            pmax_src = "ölçüldü"
            if cs.limited:
                notes.append("kaynak en yüksek gücünde kırpma görülmedi — DR_anlık alt sınırdır (≥)")
            uncertainty = prm["bisect_db"]
        else:
            p_max = 0.0
            dr_inst = -n_rbw
            pmax_src = "varsayım (tam ölçekli CW, kaynak yok)"
            notes.append("kaynak yok — P_max = 0 dBFS varsayıldı")
            uncertainty = None
        extra: dict[str, Any] = {
            "dr_inst_db": dr_inst, "p_max_dbfs": p_max, "p_max_source": pmax_src, "noise_rbw_dbfs": n_rbw,
            "noise_rbw_by_gain_dbfs": {f"{g:g}": v for g, v in sorted(noise["per_gain"].items())},
            "level_ref": ref.method, "calibrated": ref.calibrated,
        }
        if ref.calibrated:
            extra["noise_rbw_dbm"] = n_rbw + ref.offset_db
        if cs is not None and cs.ok is not None:
            extra.update({"clip_input": cs.ok.p_in, "clip_input_unit": unit_in, "clip_peak_dbfs": cs.ok.peak_dbfs,
                          "p_max_lower_bound": cs.limited})
            if cs.clip is not None:
                extra["first_clip_input"] = cs.clip.p_in
        if sf is not None:
            spur, spur_ex = sf["spur"], sf["spur_ex_image"]
            extra.update({
                "sfdr_dbc": sf["sfdr_dbc"], "sfdr_dbfs": sf["sfdr_dbfs"], "sfdr_ex_image_dbc": sf["sfdr_ex_image_dbc"],
                "carrier_dbfs": sf["carrier_dbfs"], "f_carrier_hz": sf["f_carrier_hz"],
                "dominant_spur_hz": spur["f_hz"], "dominant_spur_dbfs": spur["level_dbfs"],
                "dominant_spur_is_image": spur["is_image"], "sfdr_noise_limited": spur["noise_limited"],
                "image_dbfs": sf["image_dbfs"], "f_image_hz": sf["f_image_hz"],
                "spur_ex_image_hz": spur_ex["f_hz"], "spur_ex_image_dbfs": spur_ex["level_dbfs"],
            })
        else:
            notes.append("kaynak yok — SFDR ölçülemedi")
        if tot is not None:
            extra.update({"dr_total_db": tot["dr_total_db"], "dr_total_method": tot["method"],
                          "dr_total_lower_bound": tot["lower_bound"], "p_max_in_gmin_dbm": tot["p_max_in_gmin_dbm"],
                          "mds_gmax_dbm": tot["mds_gmax_dbm"], "snr_req_db": tot["snr_req_db"],
                          "gain_min_db": tot["g_min"], "gain_max_db": tot["g_max"]})
            if tot.get("estimate"):
                notes.append("DR_toplam seviye modeli tahminidir (mutlak referans yok)")
        if u_ref is not None:
            extra["level_uncertainty_db"] = u_ref
        src = ctx.source
        conditions = base_conditions(
            dev, spec_n, averages=prm["averages"], noise_averages=prm["noise_averages"], gain_fixed_db=out["g_fix"],
            source=getattr(src, "name", "yok") if src is not None else "yok", level_ref=ref.method,
            calibrated=ref.calibrated, clip_criterion=f"clip_fraction > 0 veya tepe ≥ {prm['clip_peak_dbfs']:g} dBFS",
            sfdr_level_dbfs=prm["sfdr_level_dbfs"], main_lobe_bins=prm["main_lobe_bins"],
        )
        conditions["averages"] = prm["averages"]
        rbw = spec_n.rbw_hz
        parts = [f"DR_anlık {dr_inst:.1f} dB (G = {out['g_fix']:g} dB, RBW {fmt_hz(rbw, 4)}, {fmt_hz(prm['fc'])}, "
                 f"fs {fmt_hz(prm['fs'])}; P_max {p_max:.1f} dBFS, N_RBW {n_rbw:.1f} dBFS)"]
        if sf is not None:
            what = "imaj" if sf["spur"]["is_image"] else "spur"
            bound = "≥ " if sf["spur"]["noise_limited"] else ""
            parts.append(f"SFDR {bound}{sf['sfdr_dbc']:.1f} dBc / {bound}{sf['sfdr_dbfs']:.1f} dBFS "
                         f"(baskın {what} {fmt_hz(sf['spur']['f_hz'] - prm['fc'])} ofsette; imaj hariç "
                         f"{sf['sfdr_ex_image_dbc']:.1f} dBc)")
        if tot is not None:
            lb = "≥ " if tot["lower_bound"] else ""
            parts.append(f"DR_toplam {lb}{tot['dr_total_db']:.1f} dB (G {tot['g_min']:g}…{tot['g_max']:g} dB)")
        summary = "; ".join(parts + notes)
        method = (
            "Sabit kazançta giriş sonlandırılarak RBW'deki gürültü N_RBW (merkez bant, Gamma-medyan düzeltmeli) "
            "ölçülür; ton adım adım yükseltilip IQ örneklerinde kırpma (clip_fraction > 0) ya da tepe ≥ "
            f"{prm['clip_peak_dbfs']:g} dBFS görülene kadar artırılır, son kırpmasız/ilk kırpan aralığı "
            f"{prm['bisect_db']:g} dB'ye ikiye bölünür: DR_anlık = P_max − N_RBW. SFDR, fs/8 + küçük ofsetteki "
            f"{prm['sfdr_level_dbfs']:g} dBFS tonla kullanılabilir bantta ana lob (±{prm['main_lobe_bins']} bin) ve "
            "DC çentiği dışındaki en güçlü bileşene (IQ imajı ve spur'lar dahil) göre verilir. DR_toplam = en düşük "
            "kazançta kırpmasız giriş (dBm) − en yüksek kazançta MDS(RBW) (CFAR modelinden SNR_min).")
        if cs is None:
            method += " Kaynak yok: DR_anlık = 0 dBFS (tam ölçekli CW varsayımı) − N_RBW."
        res = self.new_result(float(dr_inst), summary=summary, method=method, conditions=conditions, extra=extra,
                              uncertainty=uncertainty)
        res.table = self._table(prm, out, unit_in)
        res.figures = self._figures(dev, prm, out, unit_in, p_max, ref)
        return res

    def _table(self, prm: dict[str, Any], out: dict[str, Any], unit_in: str) -> list[dict[str, Any]]:
        rows: list[dict[str, Any]] = []
        cs: _ClipSearch | None = out.get("clip_search")
        if cs is not None:
            for pt in cs.points:
                rows.append({"bölüm": "doğrusallık", "p_in": pt.p_in, "unit": unit_in, "setting": pt.setting,
                             "tone_dbfs": pt.tone_dbfs, "peak_dbfs": pt.peak_dbfs, "clip_fraction": pt.clip_fraction,
                             "clipped": self._is_clip(pt, prm), "phase": pt.phase})
        p_max = cs.ok.tone_dbfs if cs is not None and cs.ok is not None else 0.0
        for g, nv in sorted(out["noise"]["per_gain"].items()):
            rows.append({"bölüm": "gürültü-kazanç", "gain_db": g, "noise_rbw_dbfs": nv, "dr_inst_db": p_max - nv})
        return rows

    def _figures(self, dev: Any, prm: dict[str, Any], out: dict[str, Any], unit_in: str, p_max: float,
                 ref: LevelRef) -> list[FigureSpec]:
        figs: list[FigureSpec] = []
        cs: _ClipSearch | None = out.get("clip_search")
        sf = out.get("sfdr")
        noise = out["noise"]
        if cs is not None and cs.points:
            pts = [p for p in cs.points if math.isfinite(p.tone_dbfs) and p.tone_dbfs > -250.0]
            x = [p.p_in for p in pts]
            y = [p.tone_dbfs for p in pts]
            lin = [p.tone_dbfs - p.p_in for p in pts if not self._is_clip(p, prm) and p.tone_dbfs < -10.0]
            gain_ref = float(np.median(lin)) if lin else float(np.median([b - a for a, b in zip(x, y)]))
            xs = [min(x), max(x)]
            series = [Series(x=x, y=y, label="Ölçülen ton gücü", style="marker"),
                      Series(x=xs, y=[v + gain_ref for v in xs], label="İdeal 1:1", style="line")]
            clipped = [p for p in pts if self._is_clip(p, prm)]
            if clipped:
                series.append(Series(x=[p.p_in for p in clipped], y=[p.tone_dbfs for p in clipped],
                                     label="Kırpma", style="marker"))
            markers = []
            if cs.ok is not None:
                markers.append((cs.ok.p_in, cs.ok.tone_dbfs, f"P_max {cs.ok.tone_dbfs:.1f} dBFS"))
            figs.append(FigureSpec(
                name="dynamic_range_linearity", title=f"Giriş–çıkış doğrusallığı (G = {out['g_fix']:g} dB)", kind="xy",
                series=series, xlabel=f"Giriş gücü [{unit_in}]", ylabel="Çıkış (ton gücü) [dBFS]",
                hlines=[(0.0, "0 dBFS"), (noise["n_rbw_dbfs"], "N_RBW")], markers=markers,
                xlim=(min(x) - 5.0, max(x) + 0.25 * (max(x) - min(x)) + 5.0),
                info={"fc": fmt_hz(prm["fc"]), "RBW": fmt_hz(noise["spec"].rbw_hz, 4)}))
        if sf is not None:
            spec = sf["spec"]
            spur = sf["spur"]
            mk = [(sf["f_carrier_hz"], sf["carrier_dbfs"], "taşıyıcı"),
                  (spur["f_hz"], spur["level_dbfs"], "baskın imaj" if spur["is_image"] else "baskın spur")]
            if not spur["is_image"]:
                mk.append((sf["f_image_hz"], sf["image_dbfs"], "imaj"))
            shot = spectrum_screenshot(
                spec, "dynamic_range_sfdr", f"SFDR {sf['sfdr_dbc']:.1f} dBc (taşıyıcı {sf['carrier_dbfs']:.1f} dBFS)",
                markers=mk, max_points=spec.n_bins, ref_level=0.0)
            if shot.ylim is not None:            # carrier at full scale AND the noise floor on one screen
                top = float(shot.ylim[1])
                shot.ylim = (min(top - 100.0, 10.0 * math.floor((sf["noise_bin_dbfs"] - 10.0) / 10.0)), top)
            figs.append(shot)
        per_gain = sorted(noise["per_gain"].items())
        g = [a for a, _ in per_gain]
        figs.append(FigureSpec(
            name="dynamic_range_vs_gain", title="Kazanca göre gürültü tabanı ve anlık dinamik alan", kind="xy",
            series=[Series(x=g, y=[v for _, v in per_gain], label="N_RBW [dBFS]", style="line"),
                    Series(x=g, y=[p_max - v for _, v in per_gain], label=f"DR_anlık = {p_max:.1f} dBFS − N_RBW [dB]",
                           style="line")],
            xlabel="Kazanç [dB]", ylabel="Seviye [dBFS] / DR [dB]",
            vlines=[(out["g_fix"], f"G = {out['g_fix']:g} dB")], info={"RBW": fmt_hz(noise["spec"].rbw_hz, 4)}))
        off = ref.offset_db if ref.calibrated else 0.0
        unit = "dBm" if ref.calibrated else "dBFS"
        figs.append(spectrum_screenshot(
            noise["spec"], "dynamic_range_noise_floor", f"Gürültü tabanı (giriş sonlandırılmış, G = {out['g_fix']:g} dB)",
            offset_db=off, unit=unit, max_points=noise["spec"].n_bins,
            ref_level=noise["n_rbw_dbfs"] + off + 40.0))
        return figs
