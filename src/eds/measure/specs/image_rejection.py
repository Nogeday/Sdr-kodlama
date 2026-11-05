"""İmaj bastırma [dB] — rejection of the IQ-imbalance image of a single tone.

Definition (docs/MEASUREMENTS.md §7)::

    IRR(fc, δ) = P(fc + δ) − P(fc − δ)        [dB]

For every centre frequency (``fcs``, default ``[fc]``), quadrature tracking state (ON, the
operational state, then OFF) and offset ``δ = ±offsets_frac·fs`` (both sides): a CW tone at
about ``tone_dbfs`` is placed at the *apparent* frequency ``fc + δ`` (the in-situ reference
error from the level probe is compensated), a settled capture is analysed and

* ``P(fc + δ)``: noise-corrected band power over ``±halfwidth_bins`` around the tone peak,
* ``P(fc − δ)``: the same at the exact mirror bin of the tone peak (the image of a tone at
  apparent baseband ``+f`` lies at ``−f``),
* an image whose raw band power is below the noise band power + ``bound_margin_db`` is
  noise-limited: the point is re-measured with the tone raised to ``bound_boost_dbfs`` (close
  to full scale, no clipping) and, if still noise-limited, ``IRR`` is the lower bound
  ``P(fc + δ) − P_raw(fc − δ)`` and flagged.

After every tracking change ``tracking_settle_s`` of samples are discarded (tracking loop
convergence on hardware). Tracking is restored to ON at the end, whatever happens.
Headline value: the worst (smallest) IRR with tracking ON within ``|δ| ≤ worst_frac·fs`` over
all centre frequencies (``≥`` when that worst point is a lower bound). Optional ``save``
stores the per-``fc`` worst ON value in the device calibration (``image_rejection_db``) and
writes it to the calibration store.
"""

from __future__ import annotations

import logging
import math
from dataclasses import dataclass, field
from typing import Any

import numpy as np

from ...core.errors import ConfigError, MeasurementError, ProfileViolation
from ...core.units import db10, fmt_hz
from ...dsp.psd import Spectrum, spectrum_from_block
from ..base import FigureSpec, Measurement, MeasurementResult, Series, register
from ..common import base_conditions, setup_rx, spectrum_screenshot, tone, welch_len
from ..stimulus import (
    adjust_tone,
    as_float,
    as_float_list,
    as_int,
    known_search_hz,
    locate_peak,
    nearest_bin,
    noise_mean_lin,
    opt_float,
    settled_capture,
    source_power_range,
    tone_band_db,
    tone_band_lin,
    valid_bins,
)

log = logging.getLogger(__name__)


@dataclass
class _IrrPoint:
    fc: float
    tracking: bool
    frac: float               # signed offset / fs
    f_tone_hz: float          # apparent tone frequency (peak bin)
    f_image_hz: float
    tone_dbfs: float
    image_dbfs: float         # noise-corrected (raw when noise-limited)
    noise_band_dbfs: float
    irr_db: float
    lower_bound: bool
    boosted: bool = False     # re-measured with the tone raised to bound_boost_dbfs
    spec: Spectrum | None = field(default=None, repr=False)


@register
class ImageRejectionMeasurement(Measurement):
    spec_id = "image_rejection"
    title = "İmaj bastırma"
    unit = "dB"
    requires = frozenset({"source"})
    default_params = {
        "fc": None,                     # None → rx.center_freq_hz
        "fcs": None,                    # optional list of centre frequencies (overrides fc)
        "fs": 4e6,
        "bw": None,                     # None → fs
        "gain": 30.0,
        "nfft": None,                   # None → dsp.nfft
        "averages": None,               # None → dsp.averages
        "window": "blackmanharris",
        "overlap": 0.5,
        "offsets_frac": [0.05, 0.1, 0.2, 0.3, 0.4],
        "tone_dbfs": -10.0,
        "halfwidth_bins": 5,
        "bound_margin_db": 6.0,
        "bound_boost_dbfs": -3.0,       # noise-limited image → tone raised to this level (None: off)
        "tracking_settle_s": 0.5,
        "worst_frac": 0.4,
        "save": False,
    }

    # ------------------------------------------------------------------ parameters
    def _params(self) -> dict[str, Any]:
        p, app = self.p, self.ctx.app
        fs = as_float(p["fs"], "image_rejection.fs", 1.0)
        if p["fcs"] is not None:
            fcs = as_float_list(p["fcs"], "image_rejection.fcs", 1.0)
        else:
            fcs = [as_float(p["fc"] if p["fc"] is not None else app.rx.center_freq_hz, "image_rejection.fc", 1.0)]
        prm = {
            "fcs": fcs,
            "fs": fs,
            "bw": as_float(p["bw"], "image_rejection.bw", 1.0) if p["bw"] is not None else fs,
            "gain": as_float(p["gain"], "image_rejection.gain"),
            "nfft": as_int(p["nfft"] if p["nfft"] is not None else app.dsp.nfft, "image_rejection.nfft", 64),
            "averages": as_int(p["averages"] if p["averages"] is not None else app.dsp.averages,
                               "image_rejection.averages"),
            "window": str(p["window"]),
            "overlap": as_float(p["overlap"], "image_rejection.overlap", 0.0, 0.95),
            "offsets_frac": as_float_list(p["offsets_frac"], "image_rejection.offsets_frac", 0.001, 0.49),
            "tone_dbfs": as_float(p["tone_dbfs"], "image_rejection.tone_dbfs", -60.0, -3.0),
            "halfwidth_bins": as_int(p["halfwidth_bins"], "image_rejection.halfwidth_bins", 1),
            "bound_margin_db": as_float(p["bound_margin_db"], "image_rejection.bound_margin_db", 0.0, 30.0),
            "bound_boost_dbfs": opt_float(p["bound_boost_dbfs"], "image_rejection.bound_boost_dbfs", -60.0, -1.0),
            "tracking_settle_s": as_float(p["tracking_settle_s"], "image_rejection.tracking_settle_s", 0.0, 60.0),
            "worst_frac": as_float(p["worst_frac"], "image_rejection.worst_frac", 0.001, 0.5),
            "save": p["save"],
        }
        if not isinstance(prm["save"], bool):
            raise ConfigError(f"image_rejection.save true/false olmalı, {prm['save']!r} geldi")
        if not any(f <= prm["worst_frac"] for f in prm["offsets_frac"]):
            raise ConfigError("image_rejection: worst_frac içinde hiç ofset yok")
        return prm

    # ------------------------------------------------------------------ run
    def run(self) -> MeasurementResult:
        ctx = self.ctx
        dev = ctx.dut
        prm = self._params()
        points: list[_IrrPoint] = []
        skipped: list[str] = []
        probes: dict[float, dict[str, float]] = {}
        try:
            for fc in prm["fcs"]:
                try:
                    setup_rx(dev, fc=fc, fs=prm["fs"], bw=prm["bw"], gain=prm["gain"],
                             tracking={"dc": True, "rf_dc": True, "quadrature": True})
                except ProfileViolation as e:
                    msg = f"{fmt_hz(fc)} atlandı (profil ihlali: {e})"
                    log.warning(msg)
                    skipped.append(msg)
                    continue
                with ctx.isolated_source():
                    pts, pinfo = self._one_fc(dev, prm)
                points.extend(pts)
                probes[fc] = pinfo
        finally:
            ctx.source.set_enabled(False)
            dev.set_tracking(quadrature=True)
        if not points:
            raise MeasurementError("Hiçbir merkez frekansında ölçüm yapılamadı: " + "; ".join(skipped))
        return self._result(dev, prm, points, skipped, probes)

    def _settle_tracking(self, dev: Any, seconds: float) -> None:
        """Discard ``seconds`` of samples (at least the stale-buffer window) after a tracking change."""
        if not dev.streaming:
            dev.start_stream()
        dev.mark_settling(max(int(dev.settle_samples), int(round(seconds * float(dev.fs)))))
        dev.flush()

    def _one_fc(self, dev: Any, prm: dict[str, Any]) -> tuple[list[_IrrPoint], dict[str, float]]:
        ctx = self.ctx
        nfft, avg, win, ov = prm["nfft"], prm["averages"], prm["window"], prm["overlap"]
        fc, fs = float(dev.fc), float(dev.fs)
        probe = adjust_tone(ctx, dev, fc + 0.1 * fs, prm["tone_dbfs"], nfft=nfft, averages=avg, window=win, overlap=ov,
                            tol_db=1.0)
        setting, e = probe.setting, probe.e
        pts: list[_IrrPoint] = []
        try:
            for state in (True, False):
                dev.set_tracking(quadrature=state)
                self._settle_tracking(dev, prm["tracking_settle_s"])
                for frac in prm["offsets_frac"]:
                    for sign in (1.0, -1.0):
                        f_app = fc + sign * frac * fs
                        pt = self._point(dev, prm, f_app * (1.0 + e), f_app, setting, state, sign * frac)
                        boost = prm["bound_boost_dbfs"]
                        if pt.lower_bound and boost is not None and boost > prm["tone_dbfs"]:
                            # image in the noise: raise the tone toward full scale to tighten the bound
                            s_b = min(setting + (boost - prm["tone_dbfs"]), source_power_range(ctx.source)[1])
                            if s_b > setting:
                                pt = self._point(dev, prm, f_app * (1.0 + e), f_app, s_b, state, sign * frac)
                                pt.boosted = True
                        pts.append(pt)
        finally:
            dev.set_tracking(quadrature=True)
        return pts, {"setting": setting, "in_situ_ppm": e * 1e6, "probe_dbfs": probe.level_dbfs}

    def _point(self, dev: Any, prm: dict[str, Any], f_src: float, f_app: float, setting: float, tracking: bool,
               frac: float) -> _IrrPoint:
        nfft, avg, win, ov = prm["nfft"], prm["averages"], prm["window"], prm["overlap"]
        hw = prm["halfwidth_bins"]
        with tone(self.ctx, f_src, setting):
            blk = settled_capture(dev, welch_len(nfft, avg, ov))
        spec = spectrum_from_block(blk, nfft, win, ov)
        nz = noise_mean_lin(spec, valid_bins(self.ctx.app, nfft))
        kt = locate_peak(spec, f_app, known_search_hz(spec.df_hz))
        f_t = float(spec.f_hz[kt])
        f_i = 2.0 * spec.fc - f_t
        ki = nearest_bin(spec, f_i)
        p_t = tone_band_db(spec, kt, hw, nz)
        raw_i = tone_band_lin(spec, ki, hw, 0.0)
        cor_i = tone_band_lin(spec, ki, hw, nz)
        lo, hi = max(0, ki - hw), min(spec.n_bins - 1, ki + hw)
        noise_band = nz * (hi - lo + 1) / spec.enbw_bins
        bound = cor_i <= 0.0 or raw_i < noise_band * 10.0 ** (prm["bound_margin_db"] / 10.0)
        img_db = float(db10(raw_i if bound else cor_i))
        return _IrrPoint(fc=float(spec.fc), tracking=tracking, frac=frac, f_tone_hz=f_t, f_image_hz=f_i, tone_dbfs=p_t,
                         image_dbfs=img_db, noise_band_dbfs=float(db10(noise_band)), irr_db=p_t - img_db,
                         lower_bound=bool(bound), spec=spec)

    # ------------------------------------------------------------------ result
    @staticmethod
    def _worst(pts: list[_IrrPoint]) -> _IrrPoint:
        return min(pts, key=lambda q: q.irr_db)

    def _save(self, dev: Any, per_fc: dict[float, float]) -> str:
        from ...calib.store import CalibrationStore, DeviceCalibration

        ctx = self.ctx
        cal = ctx.cal
        if cal is None:
            cal = DeviceCalibration(serial=str(dev.info.serial), profile=str(dev.profile.name))
            ctx.cal = cal
        for fc, v in per_fc.items():
            cal.image_rejection_db[str(int(round(fc)))] = float(v)
        cal.add_note("image_rejection ölçümü: tracking açık en kötü IRR kaydedildi "
                     + ", ".join(f"{fmt_hz(f)}: {v:.1f} dB" for f, v in per_fc.items()))
        path = CalibrationStore(ctx.app.cal.dir or None).save(cal)
        return str(path)

    def _result(self, dev: Any, prm: dict[str, Any], pts: list[_IrrPoint], skipped: list[str],
                probes: dict[float, dict[str, float]]) -> MeasurementResult:
        lim = prm["worst_frac"] + 1e-12
        on = [q for q in pts if q.tracking and abs(q.frac) <= lim]
        off = [q for q in pts if not q.tracking and abs(q.frac) <= lim]
        w_on, w_off = self._worst(on), self._worst(off)
        mean_on = float(np.mean([q.irr_db for q in on]))
        mean_off = float(np.mean([q.irr_db for q in off]))
        per_fc: dict[float, float] = {}
        per_fc_off: dict[float, float] = {}
        for fc in sorted({q.fc for q in pts}):
            per_fc[fc] = self._worst([q for q in on if q.fc == fc]).irr_db
            per_fc_off[fc] = self._worst([q for q in off if q.fc == fc]).irr_db
        n_bounds_on = sum(q.lower_bound for q in on)
        extra: dict[str, Any] = {
            "irr_on_worst_db": w_on.irr_db, "irr_on_worst_is_bound": w_on.lower_bound,
            "irr_on_worst_offset_hz": w_on.f_tone_hz - w_on.fc, "irr_on_mean_db": mean_on,
            "irr_off_worst_db": w_off.irr_db, "irr_off_worst_is_bound": w_off.lower_bound,
            "irr_off_worst_offset_hz": w_off.f_tone_hz - w_off.fc, "irr_off_mean_db": mean_off,
            "improvement_db": w_on.irr_db - w_off.irr_db, "improvement_mean_db": mean_on - mean_off,
            "lower_bounds_on": n_bounds_on, "lower_bounds_off": sum(q.lower_bound for q in off),
            "per_fc_on_worst_db": {f"{fc:.0f}": v for fc, v in per_fc.items()},
            "per_fc_off_worst_db": {f"{fc:.0f}": v for fc, v in per_fc_off.items()},
            "probes": {f"{fc:.0f}": v for fc, v in probes.items()},
        }
        notes = list(skipped)
        if prm["save"]:
            extra["saved_to"] = self._save(dev, per_fc)
            notes.append(f"tracking açık en kötü IRR kalibrasyona kaydedildi ({extra['saved_to']})")
        if n_bounds_on:
            notes.append(f"tracking açıkken {n_bounds_on} noktada imaj gürültüde — alt sınır (≥)")
        src = self.ctx.source
        conditions = base_conditions(
            dev, w_on.spec, fcs_hz=[float(f) for f in sorted(per_fc)], offsets_frac=list(prm["offsets_frac"]),
            tone_dbfs=prm["tone_dbfs"], halfwidth_bins=prm["halfwidth_bins"], bound_margin_db=prm["bound_margin_db"],
            tracking_settle_s=prm["tracking_settle_s"], worst_frac=prm["worst_frac"],
            source=getattr(src, "name", "yok"),
        )
        conditions["fc_hz"] = w_on.fc
        lb_on = "≥ " if w_on.lower_bound else ""
        lb_off = "≥ " if w_off.lower_bound else ""
        fcs_txt = ", ".join(fmt_hz(f) for f in sorted(per_fc))
        summary = (f"İmaj bastırma (tracking açık, en kötü) {lb_on}{w_on.irr_db:.1f} dB "
                   f"({fmt_hz(w_on.f_tone_hz - w_on.fc)} ofsette); tracking kapalı en kötü {lb_off}{w_off.irr_db:.1f} dB, "
                   f"iyileşme {w_on.irr_db - w_off.irr_db:.1f} dB — fc {fcs_txt}, fs {fmt_hz(prm['fs'])}, "
                   f"±{min(prm['offsets_frac']):g}…{max(prm['offsets_frac']):g}·fs, G = {prm['gain']:g} dB, "
                   f"ton ≈ {prm['tone_dbfs']:g} dBFS")
        if notes:
            summary += "; " + "; ".join(notes)
        method = (
            "Tek CW ton fc ± δ (δ = ofset·fs, iki yan) görünen frekansına konur (yerinde ppm düzeltmesi); settled "
            f"yakalamada tonun ±{prm['halfwidth_bins']} binlik gürültüsü düşülmüş bant gücü P(fc+δ) ile tam ayna "
            "bindeki imaj gücü P(fc−δ) ölçülür: IRR = P(fc+δ) − P(fc−δ). İmaj gürültü + "
            f"{prm['bound_margin_db']:g} dB altındaysa ton kırpmasız seviyeye yükseltilerek yeniden ölçülür; yine "
            "gürültüdeyse IRR alt sınır (≥) olarak işaretlenir. Önce quadrature tracking "
            f"açık (operasyonel durum), sonra kapalı ölçülür; her değişiklikten sonra {prm['tracking_settle_s']:g} s "
            "örnek atılır ve sonunda tracking yeniden açılır. Başlık değeri tracking açıkken en kötü IRR'dir.")
        res = self.new_result(float(w_on.irr_db), summary=summary, method=method, conditions=conditions, extra=extra)
        res.table = [{"fc_hz": q.fc, "tracking": "açık" if q.tracking else "kapalı", "offset_frac": q.frac,
                      "f_tone_hz": q.f_tone_hz, "f_image_hz": q.f_image_hz, "tone_dbfs": q.tone_dbfs,
                      "image_dbfs": q.image_dbfs, "noise_band_dbfs": q.noise_band_dbfs, "irr_db": q.irr_db,
                      "lower_bound": q.lower_bound, "boosted": q.boosted} for q in pts]
        res.figures = self._figures(prm, pts, w_on, w_off)
        return res

    def _figures(self, prm: dict[str, Any], pts: list[_IrrPoint], w_on: _IrrPoint, w_off: _IrrPoint) -> list[FigureSpec]:
        fs = prm["fs"]
        series: list[Series] = []
        multi = len({q.fc for q in pts}) > 1
        for fc in sorted({q.fc for q in pts}):
            for state, name in ((True, "açık"), (False, "kapalı")):
                sel = sorted((q for q in pts if q.fc == fc and q.tracking == state), key=lambda q: q.frac)
                lab = f"tracking {name}" + (f" @ {fmt_hz(fc)}" if multi else "")
                series.append(Series(x=[q.frac * fs / 1e6 for q in sel], y=[q.irr_db for q in sel], label=lab,
                                     style="marker"))
        bounds = [q for q in pts if q.lower_bound]
        if bounds:
            series.append(Series(x=[q.frac * fs / 1e6 for q in bounds], y=[q.irr_db for q in bounds],
                                 label="alt sınır (imaj gürültüde)", style="marker"))
        figs = [FigureSpec(
            name="image_rejection_vs_offset", title="İmaj bastırma – ton ofseti (tracking açık/kapalı)", kind="xy",
            series=series, xlabel="Ton ofseti δ [MHz]", ylabel="IRR [dB]",
            hlines=[(w_on.irr_db, f"en kötü (açık) {w_on.irr_db:.1f} dB"),
                    (w_off.irr_db, f"en kötü (kapalı) {w_off.irr_db:.1f} dB")],
            info={"fs": fmt_hz(fs), "Ton": f"{prm['tone_dbfs']:g} dBFS"})]
        for q, tag in ((w_on, "açık"), (w_off, "kapalı")):
            if q.spec is None:
                continue
            k_t = nearest_bin(q.spec, q.f_tone_hz)
            k_i = nearest_bin(q.spec, q.f_image_hz)
            shot = spectrum_screenshot(
                q.spec, f"image_rejection_screenshot_{'on' if q.tracking else 'off'}",
                f"Ton ve imaj — tracking {tag} (IRR {q.irr_db:.1f} dB)", max_points=q.spec.n_bins, ref_level=0.0,
                markers=[(q.f_tone_hz, float(q.spec.p_db[k_t]), "ton"),
                         (q.f_image_hz, float(q.spec.p_db[k_i]), "imaj")])
            if shot.ylim is not None:
                top = float(shot.ylim[1])
                floor = float(db10(noise_mean_lin(q.spec)))
                shot.ylim = (min(top - 100.0, 10.0 * math.floor((floor - 10.0) / 10.0)), top)
            figs.append(shot)
        return figs
