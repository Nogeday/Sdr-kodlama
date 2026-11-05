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
convergence on hardware). Tracking is restored to ON at the end, whatever happens. The path
that :meth:`set_tracking` reports is checked: when the OFF state cannot really be applied
(Pluto without the thin libiio layer: ``"unsupported"``) the OFF points are skipped instead of
being mislabelled, and when even the ON state cannot be applied/verified the headline gets a
blocker (no target verdict).

Stimulus artifacts: a source with its own LO (Pluto TX) emits LO leakage at ``LO`` and its TX
IQ image at ``2·LO − f``. For every point the source's LO offset is chosen (LO beyond the
tone, away from ``fc``, by default) so that neither artifact — folded into the RX band — lies
within ``halfwidth_bins + 2`` bins of the tone or of its RX image ``2·fc − f``; the chosen
offset and the clearance are recorded per point.

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
from ..common import base_conditions, setup_rx, source_conditions, spectrum_screenshot, tone, welch_len
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
    source_artifacts,
    source_lo_offset_hz,
    source_power_range,
    tone_band_db,
    tone_band_lin,
    try_lo_offset,
    valid_bins,
)

log = logging.getLogger(__name__)

#: ``set_tracking`` paths that really apply the requested state.
CONTROLLED_PATHS: frozenset[str] = frozenset({"soapy", "iio", "sim"})
#: Magnitude multipliers of the source LO offset tried in turn (sign: LO beyond the tone first).
_LO_OFFSET_STEPS: tuple[float, ...] = (1.0, 1.5, 0.75, 2.0, 0.5)


def fold_to_band(f_hz: float, fc: float, fs: float) -> float:
    """Apparent frequency of ``f_hz`` after sampling at ``fs`` around ``fc`` (in ``[fc − fs/2, fc + fs/2)``)."""
    return fc + ((float(f_hz) - fc + 0.5 * fs) % fs) - 0.5 * fs


def artifacts_clear(f_tone: float, fc: float, fs: float, offset_hz: float, guard_hz: float) -> bool:
    """True when a source LO at ``f_tone − offset_hz`` puts neither its leakage (``LO``) nor its
    TX image (``2·LO − f_tone``), folded into the RX band, within ``guard_hz`` of the tone or of
    the tone's RX image ``2·fc − f_tone``."""
    lo = f_tone - offset_hz
    protect = (f_tone, 2.0 * fc - f_tone)
    for a in (lo, 2.0 * lo - f_tone):
        fa = fold_to_band(a, fc, fs)
        if any(abs(fa - q) < guard_hz for q in protect):
            return False
    return True


def lo_offset_candidates(f_tone: float, fc: float, fs: float, base_hz: float, guard_hz: float) -> list[float]:
    """Signed LO offsets (``LO = f − offset``) whose artifacts stay clear (:func:`artifacts_clear`),
    best first: LO beyond the tone (away from ``fc``), the source's own magnitude first."""
    far = -1.0 if f_tone >= fc else 1.0
    out: list[float] = []
    for sign in (far, -far):
        for m in _LO_OFFSET_STEPS:
            off = sign * abs(base_hz) * m
            if artifacts_clear(f_tone, fc, fs, off, guard_hz):
                out.append(off)
    return out


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
    source_lo_offset_hz: float | None = None    # source LO offset in effect (None: source without LO)
    artifacts_clear: bool | None = None         # source LO leakage / TX image away from tone and image bins


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
        "source_lo_offset_hz": None,    # LO offset magnitude of a source with its own LO (None: the source's)
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
            "source_lo_offset_hz": opt_float(p["source_lo_offset_hz"], "image_rejection.source_lo_offset_hz", 1.0),
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
        probes: dict[float, dict[str, Any]] = {}
        lo0 = source_lo_offset_hz(ctx.source)
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
                    pts, pinfo = self._one_fc(dev, prm, lo0)
                points.extend(pts)
                probes[fc] = pinfo
        finally:
            ctx.source.set_enabled(False)
            dev.set_tracking(quadrature=True)
            if lo0 is not None:
                try:
                    try_lo_offset(ctx.source, lo0)       # leave the source as it was configured
                except Exception as e:  # noqa: BLE001 - restoring is best effort
                    log.warning("Kaynak LO ofseti geri yüklenemedi: %s", e)
        if not points:
            raise MeasurementError("Hiçbir merkez frekansında ölçüm yapılamadı: " + "; ".join(skipped))
        return self._result(dev, prm, points, skipped, probes, lo0)

    def _settle_tracking(self, dev: Any, seconds: float) -> None:
        """Discard ``seconds`` of samples (at least the stale-buffer window) after a tracking change."""
        if not dev.streaming:
            dev.start_stream()
        dev.mark_settling(max(int(dev.settle_samples), int(round(seconds * float(dev.fs)))))
        dev.flush()

    def _place_source_lo(self, f_src: float, f_app: float, fc: float, fs: float, lo0: float | None,
                         prm: dict[str, Any]) -> tuple[float | None, bool | None]:
        """Set the source's LO offset so its leakage / TX image avoid the tone and image bins.
        Returns ``(offset in effect, clear)``; ``(None, None)`` for a source without an LO."""
        if lo0 is None:
            return None, None
        src = self.ctx.source
        base = prm["source_lo_offset_hz"] if prm["source_lo_offset_hz"] is not None else lo0
        guard = (prm["halfwidth_bins"] + 2) * fs / prm["nfft"]
        for off in lo_offset_candidates(f_app, fc, fs, base, guard):
            try:
                eff = try_lo_offset(src, off)
            except ConfigError as e:                 # outside the source's TX band: next candidate
                log.debug("LO ofseti %s uygun değil: %s", fmt_hz(off), e)
                continue
            if eff is None:
                return None, None
            return eff, artifacts_clear(f_app, fc, fs, eff, guard)
        log.warning("%s: kaynak LO kaçağı/TX imajı için temiz bir LO ofseti bulunamadı", fmt_hz(f_src))
        return source_lo_offset_hz(src), False

    def _one_fc(self, dev: Any, prm: dict[str, Any], lo0: float | None = None
                ) -> tuple[list[_IrrPoint], dict[str, Any]]:
        ctx = self.ctx
        nfft, avg, win, ov = prm["nfft"], prm["averages"], prm["window"], prm["overlap"]
        fc, fs = float(dev.fc), float(dev.fs)
        probe = adjust_tone(ctx, dev, fc + 0.1 * fs, prm["tone_dbfs"], nfft=nfft, averages=avg, window=win, overlap=ov,
                            tol_db=1.0)
        setting, e = probe.setting, probe.e
        pts: list[_IrrPoint] = []
        paths: dict[str, str] = {}
        try:
            for state in (True, False):
                path = str(dev.set_tracking(quadrature=state).get("quadrature", "unsupported"))
                paths["on" if state else "off"] = path
                if not state and path not in CONTROLLED_PATHS:
                    log.warning("quadrature tracking kapatılamadı (yol: %s) — tracking kapalı noktaları atlandı", path)
                    break
                self._settle_tracking(dev, prm["tracking_settle_s"])
                for frac in prm["offsets_frac"]:
                    for sign in (1.0, -1.0):
                        f_app = fc + sign * frac * fs
                        f_src = f_app * (1.0 + e)
                        lo_off, clear = self._place_source_lo(f_src, f_app, fc, fs, lo0, prm)
                        pt = self._point(dev, prm, f_src, f_app, setting, state, sign * frac)
                        boost = prm["bound_boost_dbfs"]
                        if pt.lower_bound and boost is not None and boost > prm["tone_dbfs"]:
                            # image in the noise: raise the tone toward full scale to tighten the bound
                            s_b = min(setting + (boost - prm["tone_dbfs"]), source_power_range(ctx.source, dev)[1])
                            if s_b > setting:
                                pt = self._point(dev, prm, f_src, f_app, s_b, state, sign * frac)
                                pt.boosted = True
                        pt.source_lo_offset_hz, pt.artifacts_clear = lo_off, clear
                        if lo_off is not None:
                            arts = source_artifacts(ctx.source, e)
                            guard = (prm["halfwidth_bins"] + 2) * fs / nfft
                            protect = (pt.f_tone_hz, pt.f_image_hz)
                            pt.artifacts_clear = bool(clear) and not any(
                                abs(fold_to_band(f_a, fc, fs) - q) < guard for f_a, _ in arts for q in protect)
                        pts.append(pt)
        finally:
            dev.set_tracking(quadrature=True)
        return pts, {"setting": setting, "in_situ_ppm": e * 1e6, "probe_dbfs": probe.level_dbfs,
                     "tracking_path_on": paths.get("on", ""), "tracking_path_off": paths.get("off", "")}

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
                probes: dict[float, dict[str, Any]], lo0: float | None = None) -> MeasurementResult:
        lim = prm["worst_frac"] + 1e-12
        on = [q for q in pts if q.tracking and abs(q.frac) <= lim]
        off = [q for q in pts if not q.tracking and abs(q.frac) <= lim]
        w_on = self._worst(on)
        w_off = self._worst(off) if off else None
        mean_on = float(np.mean([q.irr_db for q in on]))
        mean_off = float(np.mean([q.irr_db for q in off])) if off else math.nan
        per_fc: dict[float, float] = {}
        per_fc_off: dict[float, float] = {}
        for fc in sorted({q.fc for q in pts}):
            per_fc[fc] = self._worst([q for q in on if q.fc == fc]).irr_db
            off_fc = [q for q in off if q.fc == fc]
            if off_fc:
                per_fc_off[fc] = self._worst(off_fc).irr_db
        n_bounds_on = sum(q.lower_bound for q in on)
        on_paths = sorted({str(v.get("tracking_path_on", "")) for v in probes.values()})
        off_paths = sorted({str(v.get("tracking_path_off", "")) for v in probes.values()})
        on_unverified = [pth for pth in on_paths if pth not in CONTROLLED_PATHS]
        off_skipped = [fc for fc, v in probes.items() if v.get("tracking_path_off", "") not in CONTROLLED_PATHS]
        extra: dict[str, Any] = {
            "irr_on_worst_db": w_on.irr_db, "irr_on_worst_is_bound": w_on.lower_bound,
            "value_bound": "lower" if w_on.lower_bound else "",
            "irr_on_worst_offset_hz": w_on.f_tone_hz - w_on.fc, "irr_on_mean_db": mean_on,
            "lower_bounds_on": n_bounds_on, "lower_bounds_off": sum(q.lower_bound for q in off),
            "per_fc_on_worst_db": {f"{fc:.0f}": v for fc, v in per_fc.items()},
            "per_fc_off_worst_db": {f"{fc:.0f}": v for fc, v in per_fc_off.items()},
            "probes": {f"{fc:.0f}": v for fc, v in probes.items()},
            "tracking_paths_on": on_paths, "tracking_paths_off": off_paths,
        }
        if w_off is not None:
            extra.update({
                "irr_off_worst_db": w_off.irr_db, "irr_off_worst_is_bound": w_off.lower_bound,
                "irr_off_worst_offset_hz": w_off.f_tone_hz - w_off.fc, "irr_off_mean_db": mean_off,
                "improvement_db": w_on.irr_db - w_off.irr_db, "improvement_mean_db": mean_on - mean_off,
            })
        notes = list(skipped)
        blockers: list[str] = []
        if on_unverified:
            blockers.append(f"quadrature tracking açık durumu ayarlanamadı/doğrulanamadı (yol: {', '.join(on_unverified)}; "
                            "Pluto'da ince libiio katmanı gerekir) — ölçülen IRR cihazın varsayılan durumundadır")
        if off_skipped:
            notes.append("quadrature tracking kapatılamadı (ince libiio katmanı yok) — tracking kapalı noktaları "
                         "ölçülmedi" + (f" ({', '.join(fmt_hz(f) for f in sorted(off_skipped))})" if len(probes) > 1
                                        else ""))
        with_lo = [q for q in pts if q.source_lo_offset_hz is not None]
        if with_lo:
            not_clear = [q for q in with_lo if q.artifacts_clear is False]
            extra.update({"source_lo_offsets_hz": sorted({float(q.source_lo_offset_hz) for q in with_lo}),
                          "source_artifact_conflicts": len(not_clear)})
            if not_clear:
                notes.append(f"{len(not_clear)} noktada kaynak LO kaçağı/TX imajı ton ya da imaj binine yakın — "
                             "bu noktalar kaynak kaynaklı olabilir")
        if prm["save"]:
            extra["saved_to"] = self._save(dev, per_fc)
            notes.append(f"tracking açık en kötü IRR kalibrasyona kaydedildi ({extra['saved_to']})")
        if n_bounds_on:
            notes.append(f"tracking açıkken {n_bounds_on} noktada imaj gürültüde — alt sınır (≥)")
        conditions = base_conditions(
            dev, w_on.spec, fcs_hz=[float(f) for f in sorted(per_fc)], offsets_frac=list(prm["offsets_frac"]),
            tone_dbfs=prm["tone_dbfs"], halfwidth_bins=prm["halfwidth_bins"], bound_margin_db=prm["bound_margin_db"],
            tracking_settle_s=prm["tracking_settle_s"], worst_frac=prm["worst_frac"],
            tracking_paths={"on": on_paths, "off": off_paths}, **source_conditions(self.ctx),
        )
        if lo0 is not None:
            conditions["source_lo_offset_default_hz"] = lo0
        conditions["fc_hz"] = w_on.fc
        lb_on = "≥ " if w_on.lower_bound else ""
        fcs_txt = ", ".join(fmt_hz(f) for f in sorted(per_fc))
        state_txt = "tracking açık" if not on_unverified else "tracking varsayılan (doğrulanamadı)"
        summary = f"İmaj bastırma ({state_txt}, en kötü) {lb_on}{w_on.irr_db:.1f} dB ({fmt_hz(w_on.f_tone_hz - w_on.fc)} ofsette); "
        if w_off is not None:
            lb_off = "≥ " if w_off.lower_bound else ""
            summary += (f"tracking kapalı en kötü {lb_off}{w_off.irr_db:.1f} dB, "
                        f"iyileşme {w_on.irr_db - w_off.irr_db:.1f} dB — ")
        else:
            summary += "tracking kapalı ölçülemedi — "
        summary += (f"fc {fcs_txt}, fs {fmt_hz(prm['fs'])}, ±{min(prm['offsets_frac']):g}…{max(prm['offsets_frac']):g}·fs, "
                    f"G = {prm['gain']:g} dB, ton ≈ {prm['tone_dbfs']:g} dBFS")
        if notes:
            summary += "; " + "; ".join(notes)
        method = (
            "Tek CW ton fc ± δ (δ = ofset·fs, iki yan) görünen frekansına konur (yerinde ppm düzeltmesi); settled "
            f"yakalamada tonun ±{prm['halfwidth_bins']} binlik gürültüsü düşülmüş bant gücü P(fc+δ) ile tam ayna "
            "bindeki imaj gücü P(fc−δ) ölçülür: IRR = P(fc+δ) − P(fc−δ). İmaj gürültü + "
            f"{prm['bound_margin_db']:g} dB altındaysa ton kırpmasız seviyeye yükseltilerek yeniden ölçülür; yine "
            "gürültüdeyse IRR alt sınır (≥) olarak işaretlenir. Önce quadrature tracking "
            f"açık (operasyonel durum), sonra kapalı ölçülür; her değişiklikten sonra {prm['tracking_settle_s']:g} s "
            "örnek atılır ve sonunda tracking yeniden açılır; sürücünün bildirdiği ayar yolu denetlenir (tracking "
            "gerçekten kapatılamıyorsa kapalı noktalar ölçülmez). Kendi LO'su olan kaynakta (Pluto TX) LO, kaynağın LO "
            "kaçağı ve TX IQ imajı tonun ve ölçülen imaj bininin dışında kalacak şekilde (tonun fc'den uzak yanına) "
            "her nokta için yeniden yerleştirilir (mümkünse referans kilitli laboratuvar üreteci önerilir). Başlık "
            "değeri tracking açıkken en kötü IRR'dir.")
        res = self.new_result(float(w_on.irr_db), summary=summary, method=method, conditions=conditions, extra=extra,
                              blockers=blockers)
        res.table = [{"fc_hz": q.fc, "tracking": "açık" if q.tracking else "kapalı", "offset_frac": q.frac,
                      "f_tone_hz": q.f_tone_hz, "f_image_hz": q.f_image_hz, "tone_dbfs": q.tone_dbfs,
                      "image_dbfs": q.image_dbfs, "noise_band_dbfs": q.noise_band_dbfs, "irr_db": q.irr_db,
                      "lower_bound": q.lower_bound, "boosted": q.boosted, "source_lo_offset_hz": q.source_lo_offset_hz,
                      "source_artifacts_clear": q.artifacts_clear} for q in pts]
        res.figures = self._figures(prm, pts, w_on, w_off)
        return res

    def _figures(self, prm: dict[str, Any], pts: list[_IrrPoint], w_on: _IrrPoint,
                 w_off: _IrrPoint | None) -> list[FigureSpec]:
        fs = prm["fs"]
        series: list[Series] = []
        multi = len({q.fc for q in pts}) > 1
        for fc in sorted({q.fc for q in pts}):
            for state, name in ((True, "açık"), (False, "kapalı")):
                sel = sorted((q for q in pts if q.fc == fc and q.tracking == state), key=lambda q: q.frac)
                if not sel:
                    continue
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
            hlines=[(w_on.irr_db, f"en kötü (açık) {w_on.irr_db:.1f} dB")]
            + ([(w_off.irr_db, f"en kötü (kapalı) {w_off.irr_db:.1f} dB")] if w_off is not None else []),
            info={"fs": fmt_hz(fs), "Ton": f"{prm['tone_dbfs']:g} dBFS"})]
        for q, tag in ((w_on, "açık"), (w_off, "kapalı")):
            if q is None or q.spec is None:
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
