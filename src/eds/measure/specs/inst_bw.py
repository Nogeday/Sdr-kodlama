"""Anlık bant genişliği [MHz] — −3 dB (and −1 dB) width of the receiver's amplitude response.

For every ``(fs, bw)`` configuration (``configs``; analog BW ``bw``, sample rate ``fs``):

With a stimulus source (tone stepping)
    A constant-power CW tone is stepped across ``±span_frac·fs`` (``points`` uniform steps,
    ``|f_bb| < skip_dc_bins`` bins skipped). Each point is a settled capture; the tone power is
    the noise-corrected, scalloping-free band power (``±5`` bins) around the tone's apparent
    frequency (the in-situ reference error from the level probe predicts it to a fraction of a
    bin). ``H(f) = P_meas(f) − setting`` is normalized to the median of the central
    ``|f| ≤ ref_frac·fs`` points. The −1 / −3 dB edges are the first crossings walking outward
    from the centre on each side, linearly interpolated in dB; each crossing bracket is then
    bisected ``refine_steps`` times with extra tone points (the response falls steeply at the
    band edge — the doc's "kenarlarda sıklaştırılmış" sampling). Flatness = peak-to-peak of
    ``H`` within ``±flat_frac·fs``. Every tone point waits the source's settle time
    (:func:`eds.measure.common.stimulus_changed`): at 20 MS/s the RX stale-buffer window is
    far shorter than a Pluto TX's queue latency.

Without a source (noise shape — approximate)
    Terminated input at the band's maximum gain, long-average spectrum, linear moving average
    over ``smooth_frac·nfft`` bins (DC notch interpolated), normalized to the central median;
    edges as above on the bin grid. The white ADC floor masks the shape far down the skirts,
    so this is a low-confidence fallback (the method text says so).

Headline value: the largest −3 dB width over the configurations (MHz), capped at the
profile's maximum analog bandwidth (``profile.bw_max_hz``) with a note.
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
    TONE_HALFWIDTH_BINS,
    adjust_tone,
    as_float,
    as_int,
    known_search_hz,
    locate_peak,
    noise_mean_lin,
    opt_float,
    qparam,
    settled_capture,
    tone_band_db,
    valid_bins,
)

log = logging.getLogger(__name__)

LEVELS_DB: tuple[float, float] = (-1.0, -3.0)


@dataclass
class _Point:
    f_bb_hz: float            # apparent baseband frequency of the tone
    f_src_hz: float
    tone_dbfs: float
    phase: str                # "adım" | "kenar"
    spec: Spectrum | None = field(default=None, repr=False)


@dataclass
class _Edges:
    lo_hz: float
    hi_hz: float
    limited: bool             # a crossing was not found inside the measured span

    @property
    def width_hz(self) -> float:
        return self.hi_hz - self.lo_hz


@dataclass
class _ConfigResult:
    fs: float
    bw: float
    method: str               # "ton" | "gürültü şekli"
    f_hz: np.ndarray          # baseband frequencies of the response samples
    resp_db: np.ndarray       # normalized response (dB)
    edges: dict[float, _Edges]
    flatness_db: float
    points: list[_Point]
    shot: Spectrum | None
    shot_f_hz: float | None
    rbw_hz: float
    setting: float | None = None
    in_situ_ppm: float | None = None


def crossings(f: np.ndarray, r: np.ndarray, level: float, *, f0: float = 0.0) -> _Edges:
    """First ``level`` crossings of the response ``r(f)`` walking outward from ``f0`` on each
    side, linearly interpolated (``f`` ascending). ``limited`` when a side never drops below."""
    f = np.asarray(f, float)
    r = np.asarray(r, float)
    order = np.argsort(f)
    f, r = f[order], r[order]
    i0 = int(np.argmin(np.abs(f - f0)))
    limited = False

    def side(step: int) -> float:
        nonlocal limited
        i = i0
        while 0 <= i + step < f.size:
            j = i + step
            if r[j] < level <= r[i]:
                return float(f[i] + (level - r[i]) / (r[j] - r[i]) * (f[j] - f[i]))
            i = j
        limited = True
        return float(f[0] if step < 0 else f[-1])

    lo, hi = side(-1), side(+1)
    return _Edges(lo_hz=lo, hi_hz=hi, limited=limited)


def bracket(f: np.ndarray, r: np.ndarray, level: float, upper: bool, *, f0: float = 0.0) -> tuple[float, float] | None:
    """``(f_in, f_out)`` of the first ``level`` crossing on one side (inner point ≥ level)."""
    order = np.argsort(f)
    fs_, rs = np.asarray(f, float)[order], np.asarray(r, float)[order]
    i = int(np.argmin(np.abs(fs_ - f0)))
    step = 1 if upper else -1
    while 0 <= i + step < fs_.size:
        j = i + step
        if rs[j] < level <= rs[i]:
            return float(fs_[i]), float(fs_[j])
        i = j
    return None


@register
class InstBwMeasurement(Measurement):
    spec_id = "inst_bw"
    title = "Anlık bant genişliği"
    unit = "MHz"
    default_params = {
        "fc": None,                     # None → rx.center_freq_hz
        "configs": [[20e6, 20e6], [10e6, 10e6], [5e6, 5e6]],   # [(fs, bw)]
        "quick_configs": None,          # None → first config only in quick mode
        "points": 41,
        "quick_points": 21,
        "refine_steps": 3,
        "quick_refine_steps": 2,
        "gain": 40.0,
        "nfft": None,                   # None → dsp.nfft
        "averages": 4,
        "window": "blackmanharris",
        "overlap": 0.5,
        "span_frac": 0.49,
        "ref_frac": 0.1,
        "flat_frac": 0.4,
        "skip_dc_bins": 2,
        "tone_dbfs": -20.0,
        "noise_gain": None,             # None → band maximum (noise-shape fallback)
        "noise_averages": 64,
        "quick_noise_averages": 32,
        "smooth_frac": 0.01,
    }

    # ------------------------------------------------------------------ parameters
    def _params(self) -> dict[str, Any]:
        p, app, ctx = self.p, self.ctx.app, self.ctx
        raw = p["configs"]
        if ctx.quick:
            raw = p["quick_configs"] if p.get("quick_configs") is not None else (raw[:1] if isinstance(raw, list) else raw)
        if not isinstance(raw, (list, tuple)) or not raw:
            raise ConfigError(f"inst_bw.configs boş olmayan [[fs, bw], ...] listesi olmalı, {raw!r} geldi")
        configs: list[tuple[float, float]] = []
        for i, c in enumerate(raw):
            if not isinstance(c, (list, tuple)) or len(c) != 2:
                raise ConfigError(f"inst_bw.configs[{i}]: [fs, bw] çifti bekleniyordu, {c!r} geldi")
            configs.append((as_float(c[0], f"inst_bw.configs[{i}].fs", 1.0), as_float(c[1], f"inst_bw.configs[{i}].bw", 1.0)))
        prm = {
            "fc": as_float(p["fc"] if p["fc"] is not None else app.rx.center_freq_hz, "inst_bw.fc", 1.0),
            "configs": configs,
            "points": as_int(qparam(self, "points"), "inst_bw.points", 5),
            "refine_steps": as_int(qparam(self, "refine_steps"), "inst_bw.refine_steps", 0),
            "gain": as_float(p["gain"], "inst_bw.gain"),
            "nfft": as_int(p["nfft"] if p["nfft"] is not None else app.dsp.nfft, "inst_bw.nfft", 64),
            "averages": as_int(p["averages"], "inst_bw.averages"),
            "window": str(p["window"]),
            "overlap": as_float(p["overlap"], "inst_bw.overlap", 0.0, 0.95),
            "span_frac": as_float(p["span_frac"], "inst_bw.span_frac", 0.05, 0.499),
            "ref_frac": as_float(p["ref_frac"], "inst_bw.ref_frac", 0.005, 0.3),
            "flat_frac": as_float(p["flat_frac"], "inst_bw.flat_frac", 0.01, 0.499),
            "skip_dc_bins": as_int(p["skip_dc_bins"], "inst_bw.skip_dc_bins", 0),
            "tone_dbfs": as_float(p["tone_dbfs"], "inst_bw.tone_dbfs", -80.0, -6.0),
            "noise_gain": opt_float(p["noise_gain"], "inst_bw.noise_gain"),
            "noise_averages": as_int(qparam(self, "noise_averages"), "inst_bw.noise_averages"),
            "smooth_frac": as_float(p["smooth_frac"], "inst_bw.smooth_frac", 0.0, 0.1),
        }
        if prm["ref_frac"] >= prm["span_frac"]:
            raise ConfigError("inst_bw.ref_frac, span_frac'tan küçük olmalı")
        return prm

    # ------------------------------------------------------------------ run
    def run(self) -> MeasurementResult:
        ctx = self.ctx
        dev = ctx.dut
        prm = self._params()
        results: list[_ConfigResult] = []
        skipped: list[str] = []
        for fs, bw in prm["configs"]:
            gain = prm["gain"] if ctx.source is not None else (
                prm["noise_gain"] if prm["noise_gain"] is not None else float(dev.gain_range(prm["fc"]).max_db))
            try:
                setup_rx(dev, fc=prm["fc"], fs=fs, bw=bw, gain=gain,
                         tracking={"dc": True, "rf_dc": True, "quadrature": True})
            except ProfileViolation as e:
                msg = f"fs {fs / 1e6:g} MS/s / BW {fmt_hz(bw)} atlandı (profil ihlali: {e})"
                log.warning(msg)
                skipped.append(msg)
                continue
            if ctx.source is not None:
                try:
                    with ctx.isolated_source():
                        results.append(self._tone_method(dev, prm, fs, bw))
                finally:
                    ctx.source.set_enabled(False)
            else:
                results.append(self._noise_method(dev, prm, fs, bw))
        if not results:
            raise MeasurementError("Hiçbir (fs, bw) konfigürasyonu ölçülemedi: " + "; ".join(skipped))
        return self._result(dev, prm, results, skipped)

    # ------------------------------------------------------------------ tone method
    def _measure(self, dev: Any, prm: dict[str, Any], f_src: float, e: float, setting: float, phase: str) -> _Point:
        nfft, avg, win, ov = prm["nfft"], prm["averages"], prm["window"], prm["overlap"]
        with tone(self.ctx, f_src, setting):
            blk = settled_capture(dev, welch_len(nfft, avg, ov))
        spec = spectrum_from_block(blk, nfft, win, ov)
        f_app = f_src / (1.0 + e)
        nz = noise_mean_lin(spec, valid_bins(self.ctx.app, nfft))
        k = locate_peak(spec, f_app, known_search_hz(spec.df_hz))
        return _Point(f_bb_hz=f_app - float(dev.fc), f_src_hz=f_src, tone_dbfs=tone_band_db(spec, k, TONE_HALFWIDTH_BINS, nz),
                      phase=phase, spec=spec)

    def _tone_method(self, dev: Any, prm: dict[str, Any], fs: float, bw: float) -> _ConfigResult:
        ctx = self.ctx
        nfft, avg, win, ov = prm["nfft"], prm["averages"], prm["window"], prm["overlap"]
        fc = float(dev.fc)
        df = fs / nfft
        probe = adjust_tone(ctx, dev, fc + 0.05 * fs, prm["tone_dbfs"], nfft=nfft, averages=avg, window=win,
                            overlap=ov, tol_db=1.0)
        setting, e = probe.setting, probe.e
        grid = np.linspace(-prm["span_frac"] * fs, prm["span_frac"] * fs, prm["points"])
        grid = grid[np.abs(grid) >= max(prm["skip_dc_bins"], 1) * df]
        pts = [self._measure(dev, prm, fc + float(fb), e, setting, "adım") for fb in grid]

        def response() -> tuple[np.ndarray, np.ndarray]:
            f = np.array([p.f_bb_hz for p in pts])
            h = np.array([p.tone_dbfs for p in pts]) - setting
            central = np.abs(f) <= prm["ref_frac"] * fs
            if not np.any(central):
                raise MeasurementError("inst_bw: merkez referans bölgesinde (±ref_frac·fs) ölçüm noktası yok")
            return f, h - float(np.median(h[central]))

        for level in LEVELS_DB:
            for upper in (False, True):
                for _ in range(prm["refine_steps"]):
                    f, r = response()
                    br = bracket(f, r, level, upper)
                    if br is None:
                        break
                    f_mid_app = 0.5 * (br[0] + br[1])
                    if abs(br[1] - br[0]) < 2.0 * df:
                        break
                    f_src = (fc + f_mid_app) * (1.0 + e)
                    pts.append(self._measure(dev, prm, f_src, e, setting, "kenar"))
        f, r = response()
        edges = {lv: crossings(f, r, lv) for lv in LEVELS_DB}
        flat = np.abs(f) <= prm["flat_frac"] * fs
        flatness = float(np.max(r[flat]) - np.min(r[flat])) if np.any(flat) else math.nan
        # screenshot: the point closest to the upper −3 dB edge
        e3 = edges[-3.0].hi_hz
        best = min(pts, key=lambda p: abs(p.f_bb_hz - e3))
        order = np.argsort(f)
        rbw = pts[0].spec.rbw_hz if pts[0].spec is not None else math.nan
        res = _ConfigResult(fs=fs, bw=bw, method="ton", f_hz=f[order], resp_db=r[order], edges=edges, flatness_db=flatness,
                            points=sorted(pts, key=lambda p: p.f_bb_hz), shot=best.spec, shot_f_hz=best.f_bb_hz + fc,
                            rbw_hz=rbw, setting=setting, in_situ_ppm=e * 1e6)
        for p in res.points:
            p.spec = None                       # keep memory bounded; the screenshot spectrum is kept above
        return res

    # ------------------------------------------------------------------ noise method
    def _noise_method(self, dev: Any, prm: dict[str, Any], fs: float, bw: float) -> _ConfigResult:
        app = self.ctx.app
        nfft, win, ov = prm["nfft"], prm["window"], prm["overlap"]
        with self.ctx.terminated_input():
            blk = settled_capture(dev, welch_len(nfft, prm["noise_averages"], ov))
        spec = spectrum_from_block(blk, nfft, win, ov)
        p = spec.p_lin.copy()
        k = spec.dc_offset_bins
        notch = int(app.dsp.dc_notch_bins)
        bad = np.abs(k) <= max(notch, 0)
        if np.any(bad) and np.any(~bad):
            idx = np.arange(p.size)
            p[bad] = np.interp(idx[bad], idx[~bad], p[~bad])
        m = max(1, int(round(prm["smooth_frac"] * nfft)))
        if m % 2 == 0:
            m += 1
        if m > 1:
            pad = m // 2
            ext = np.concatenate((np.full(pad, p[0]), p, np.full(pad, p[-1])))
            p = np.convolve(ext, np.ones(m) / m, mode="valid")
        f = spec.f_rel_hz
        r = db10(p)
        central = np.abs(f) <= prm["ref_frac"] * fs
        r = r - float(np.median(r[central]))
        edges = {lv: crossings(f, r, lv) for lv in LEVELS_DB}
        flat = np.abs(f) <= prm["flat_frac"] * fs
        flatness = float(np.max(r[flat]) - np.min(r[flat]))
        return _ConfigResult(fs=fs, bw=bw, method="gürültü şekli", f_hz=f, resp_db=r, edges=edges, flatness_db=flatness,
                             points=[], shot=spec, shot_f_hz=None, rbw_hz=spec.rbw_hz)

    # ------------------------------------------------------------------ result
    def _result(self, dev: Any, prm: dict[str, Any], results: list[_ConfigResult], skipped: list[str]) -> MeasurementResult:
        ctx = self.ctx
        bw_max = float(dev.profile.bw_max_hz)
        best = max(results, key=lambda c: c.edges[-3.0].width_hz)
        w3 = best.edges[-3.0].width_hz
        notes: list[str] = list(skipped)
        value_hz = w3
        if w3 > bw_max:
            value_hz = bw_max
            notes.append(f"ölçülen −3 dB genişliği {w3 / 1e6:.2f} MHz, profil analog BW sınırı {bw_max / 1e6:g} MHz ile "
                         "sınırlandı")
        noise_method = any(c.method != "ton" for c in results)
        lower = best.edges[-3.0].limited
        if lower:
            notes.append("−3 dB geçişi ölçülen açıklıkta bulunamadı — değer alt sınırdır (≥)")
        per_cfg = []
        table: list[dict[str, Any]] = []
        for i, c in enumerate(results):
            d = {"fs_hz": c.fs, "bw_hz": c.bw, "method": c.method, "bw_3db_hz": c.edges[-3.0].width_hz,
                 "bw_1db_hz": c.edges[-1.0].width_hz, "f_lo_3db_hz": c.edges[-3.0].lo_hz,
                 "f_hi_3db_hz": c.edges[-3.0].hi_hz, "f_lo_1db_hz": c.edges[-1.0].lo_hz,
                 "f_hi_1db_hz": c.edges[-1.0].hi_hz, "flatness_db": c.flatness_db,
                 "bw_3db_lower_bound": c.edges[-3.0].limited, "points": len(c.points) if c.points else int(c.f_hz.size),
                 "rbw_hz": c.rbw_hz}
            if c.in_situ_ppm is not None:
                d["in_situ_ppm"] = c.in_situ_ppm
            per_cfg.append(d)
            table.append({"bölüm": "özet", "config": i, **{k: v for k, v in d.items() if k != "points"}})
            for p in c.points:
                fb = p.f_bb_hz
                r = float(np.interp(fb, c.f_hz, c.resp_db))
                table.append({"bölüm": "nokta", "config": i, "fs_hz": c.fs, "bw_hz": c.bw, "f_bb_hz": fb,
                              "f_src_hz": p.f_src_hz, "tone_dbfs": p.tone_dbfs, "response_db": r, "phase": p.phase})
        extra: dict[str, Any] = {
            "bw_3db_hz": w3, "bw_1db_hz": best.edges[-1.0].width_hz, "flatness_db": best.flatness_db,
            "best_config": {"fs_hz": best.fs, "bw_hz": best.bw}, "configs": per_cfg,
            "method": "gürültü şekli (yaklaşık)" if noise_method else "ton adımlama",
            "capped_at_profile": w3 > bw_max, "profile_bw_max_hz": bw_max, "lower_bound": lower,
            "value_bound": "lower" if lower else "",
        }
        conditions = base_conditions(
            dev, None, nfft=prm["nfft"], averages=prm["averages"] if not noise_method else prm["noise_averages"],
            window=prm["window"], rbw_hz=best.rbw_hz, configs=[[c.fs, c.bw] for c in results],
            points=prm["points"], refine_steps=prm["refine_steps"], span_frac=prm["span_frac"],
            ref_frac=prm["ref_frac"], flat_frac=prm["flat_frac"], gain_used_db=float(dev.gains[0]),
            **source_conditions(ctx),
        )
        conditions.update({"fs_hz": best.fs, "bw_hz": best.bw})
        cfg_txt = ", ".join(f"fs {c.fs / 1e6:g} MS/s/BW {c.bw / 1e6:g} MHz: {c.edges[-3.0].width_hz / 1e6:.2f} MHz"
                            for c in results)
        lb = "≥ " if lower else ""
        summary = (f"Anlık BW {lb}{value_hz / 1e6:.2f} MHz (−3 dB), −1 dB {best.edges[-1.0].width_hz / 1e6:.2f} MHz, "
                   f"düzlük {best.flatness_db:.2f} dB p-p (±{prm['flat_frac']:g}·fs) — fs {best.fs / 1e6:g} MS/s, "
                   f"BW {best.bw / 1e6:g} MHz, {fmt_hz(prm['fc'])}, "
                   + ("gürültü şekli (yaklaşık, kaynaksız)" if noise_method else f"ton adımlama ({len(best.points)} nokta)")
                   + f"; konfigürasyonlar: {cfg_txt}")
        if notes:
            summary += "; " + "; ".join(notes)
        if noise_method:
            method = (
                "Kaynaksız yedek yöntem — gürültü şekli (yaklaşık, düşük güven): giriş sonlandırılıp en yüksek "
                "kazançta uzun ortalamalı spektrum alınır, DC çentiği enterpolasyonla doldurulur, "
                f"{prm['smooth_frac']:g}·N binlik kayan ortalama ile düzleştirilir ve merkez (±{prm['ref_frac']:g}·fs) "
                "medyanına normalize edilir; −1/−3 dB kenarları merkezden dışa ilk geçişlerdir. ADC'nin düz gürültü "
                "tabanı yanıtın derin eteklerini maskeleyebilir.")
        else:
            method = (
                f"Sabit güçte CW ton ±{prm['span_frac']:g}·fs aralığında {prm['points']} eşit adımla gezdirilir (DC "
                "çevresi atlanır); her noktada settled yakalama ve tonun görünen frekansında ±5 binlik, gürültüsü "
                "düşülmüş bant gücü (scalloping'den bağımsız) ölçülür. Yanıt merkez (±"
                f"{prm['ref_frac']:g}·fs) noktalarının medyanına normalize edilir; −1/−3 dB kenarları merkezden dışa "
                f"ilk geçişlerin dB'de doğrusal enterpolasyonudur ve her kenar {prm['refine_steps']} ek ton noktasıyla "
                f"ikiye bölünerek sıklaştırılır. Düzlük = ±{prm['flat_frac']:g}·fs içindeki tepe-tepe dalgalanma.")
        res = self.new_result(float(value_hz / 1e6), summary=summary, method=method, conditions=conditions,
                              extra=extra)
        res.table = table
        res.figures = self._figures(prm, results)
        return res

    def _figures(self, prm: dict[str, Any], results: list[_ConfigResult]) -> list[FigureSpec]:
        series = []
        for c in results:
            f, r = c.f_hz, c.resp_db
            if f.size > 2000:
                step = int(math.ceil(f.size / 2000))
                f, r = f[::step], r[::step]
            series.append(Series(x=(f / 1e6).tolist(), y=r.tolist(),
                                 label=f"fs {c.fs / 1e6:g} MS/s, BW {c.bw / 1e6:g} MHz: −3 dB {c.edges[-3.0].width_hz / 1e6:.2f} MHz",
                                 style="line" if c.method != "ton" else "marker"))
        lo = min(float(np.min(c.resp_db)) for c in results)
        figs = [FigureSpec(
            name="inst_bw_response", title="Alıcı genlik yanıtı (normalize)", kind="xy", series=series,
            xlabel="Taban bant frekansı [MHz]", ylabel="Yanıt [dB]",
            hlines=[(-1.0, "−1 dB"), (-3.0, "−3 dB")], ylim=(max(lo - 2.0, -45.0), 2.0),
            info={"Yöntem": results[0].method, "fc": fmt_hz(prm["fc"])})]
        for i, c in enumerate(results):
            if c.shot is None:
                continue
            fc_shot = float(c.shot.fc)
            mk = []
            if c.shot_f_hz is not None:
                # the captured tone point nearest to the upper edge — NOT the edge itself: its own
                # normalized response is stated; the interpolated edges are the vertical lines
                kk = int(np.argmin(np.abs(c.shot.f_hz - c.shot_f_hz)))
                r_pt = float(np.interp(c.shot_f_hz - fc_shot, c.f_hz, c.resp_db))
                mk.append((c.shot_f_hz, float(c.shot.p_db[kk]),
                           f"en yakın ton noktası ({r_pt:.1f} dB)".replace("-", "−")))
            title = (f"Bant kenarı (fs {c.fs / 1e6:g} MS/s, BW {c.bw / 1e6:g} MHz)" if c.method == "ton"
                     else f"Gürültü şekli (fs {c.fs / 1e6:g} MS/s, BW {c.bw / 1e6:g} MHz)")
            fig = spectrum_screenshot(c.shot, f"inst_bw_screenshot_{i}", title, markers=mk, max_points=c.shot.n_bins)
            e3 = c.edges[-3.0]
            edge_label = "−3 dB kenarı" if not e3.limited else "−3 dB kenarı (açıklıkta bulunamadı, ≥)"
            fig.vlines = [((fc_shot + f) / 1e6, edge_label) for f in (e3.lo_hz, e3.hi_hz)]
            fig.info["−3 dB BW"] = f"{'≥ ' if e3.limited else ''}{e3.width_hz / 1e6:.2f} MHz"
            figs.append(fig)
        return figs
