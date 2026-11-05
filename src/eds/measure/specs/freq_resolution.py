"""Frekans çözünürlüğü [Hz] — RBW, measured two-tone resolution and frequency accuracy.

(a) Resolution bandwidth (calculated) ``RBW = ENBW_bins · fs / N`` and the measured −3 dB
    width of a single tone's spectral line (interpolated on a ``zero_pad``-times zero-padded
    Welch spectrum, i.e. independent of where the tone falls between bins).

(b) Two-tone resolvability (source with ``max_tones >= 2``): two equal tones at the apparent
    frequencies ``fc + center_frac·fs ± Δf/2`` (in-situ ppm compensated). The averaged
    zero-padded spectrum of ``captures`` independent settled captures (different relative tone
    phases → incoherent average) is searched for two local maxima, one within ``±RBW`` of
    each true frequency, with a dip of at least ``dip_db`` (3 dB) between them. ``Δf`` runs over
    a ``sep_points``-point geometric series ``sep_min_rbw … sep_max_rbw × RBW``; the smallest
    separation above which every separation is resolved is bracketed and refined by
    ``bisect_steps`` geometric bisections. Value = smallest verified resolved separation;
    uncertainty = the final bracket width. Without a multi-tone source the value is the RBW
    (method says so).

(c) Frequency accuracy: a CW tone at a known frequency (the source is the truth — on hardware
    it MUST be locked to a GPSDO/10 MHz reference) at ``fc + accuracy_frac[1]·fs``, measured
    with :func:`eds.dsp.peaks.tone_frequency` on ``accuracy_n`` samples: the raw error and the
    error after the stored calibration ppm (``ctx.cal.ppm``, if any — an independently
    obtained correction, so this is the operational accuracy). The in-situ ppm estimate
    (:func:`eds.calib.ppm.estimate_ppm_from_samples`, on a separate capture of a tone at
    ``fc + accuracy_frac[0]·fs`` from the SAME source) shares the source/receiver reference
    ratio with the validation tone, so the error after it is a *self-consistency* figure
    (estimator noise only), reported as such and never as accuracy. ``reference_locked``
    states whether the source is locked to a reference (auto: simulator yes, ``pluto_tx`` no —
    its TCXO is ±25 ppm itself, so its "raw error" is only the TX-vs-RX reference difference;
    other sources: unknown); without a locked source no absolute accuracy is claimed.
"""

from __future__ import annotations

import logging
import math
from dataclasses import dataclass, field
from typing import Any

import numpy as np
from numpy.lib.stride_tricks import sliding_window_view

from ...calib.ppm import correct_freq, estimate_ppm_from_samples, residual_ppm
from ...core.errors import ConfigError, MeasurementError
from ...core.units import db10, fmt_hz
from ...dsp.peaks import interp_peak, tone_frequency
from ...dsp.psd import Spectrum, spectrum_from_block
from ...dsp.windows import canonical_window_name, get_window, window_info
from ..base import FigureSpec, Measurement, MeasurementResult, Series, register
from ..common import (
    base_conditions,
    setup_rx,
    source_conditions,
    spectrum_screenshot,
    stimulus_changed,
    tone,
    welch_len,
)
from ..stimulus import (
    adjust_tone,
    as_float,
    as_float_list,
    as_int,
    qparam,
    settled_capture,
)

log = logging.getLogger(__name__)

_CHUNK = 64            # segments per FFT batch of the zero-padded Welch
_LINE_ZP = 64          # zero padding of the analytic window line


# =============================================================================== DSP helpers
def zp_welch_sum(x: np.ndarray, nfft: int, window: str, overlap: float, zero_pad: int) -> tuple[np.ndarray, int]:
    """Sum over Welch segments of ``|FFT(w·x, N·Z)|²/(Σw)²`` (not shifted) and the segment count."""
    w = get_window(window, nfft)
    scale = 1.0 / float(np.sum(w)) ** 2
    hop = max(1, int(round(nfft * (1.0 - overlap))))
    x = np.asarray(x)
    if x.size < nfft:
        raise MeasurementError(f"Kayıt ({x.size}) FFT boyundan ({nfft}) kısa")
    n_seg = 1 + (x.size - nfft) // hop
    segs = sliding_window_view(x, nfft)[::hop][:n_seg]
    m = nfft * zero_pad
    acc = np.zeros(m)
    for i in range(0, n_seg, _CHUNK):
        f = np.fft.fft(segs[i:i + _CHUNK] * w, n=m, axis=1)
        acc += np.sum(f.real ** 2 + f.imag ** 2, axis=0)
    return acc * scale, n_seg


def zp_spectrum(acc: np.ndarray, n_seg: int, fc: float, fs: float, nfft: int, window: str, overlap: float,
                zero_pad: int) -> Spectrum:
    """Zero-padded power spectrum (ARCHITECTURE scaling; ``nfft = N·Z``, ``enbw_bins = ENBW·Z`` so
    ``rbw_hz`` and band powers stay those of the N-point analysis)."""
    m = nfft * zero_pad
    p = np.fft.fftshift(acc / max(n_seg, 1))
    f = fc + (np.arange(m) - m // 2) * (fs / m)
    info = window_info(window, nfft)
    return Spectrum(f_hz=f, p_lin=p, fc=fc, fs=fs, nfft=m, window=canonical_window_name(window),
                    enbw_bins=info.enbw_bins * zero_pad, averages=n_seg,
                    meta={"avg": "mean", "overlap": overlap, "zero_pad": zero_pad, "segment_len": nfft})


def line_width_hz(f: np.ndarray, p: np.ndarray, k_peak: int, level_db: float = -3.0) -> float:
    """Width of a spectral line at ``level_db`` below its (parabolically interpolated) peak,
    edges interpolated linearly in dB between samples."""
    pdb = db10(p)
    _, pk_db = interp_peak(pdb, k_peak)
    t_db = float(pk_db) + level_db
    thr = float(10.0 ** (t_db / 10.0))

    def edge(step: int) -> float:
        i = k_peak
        while 0 <= i + step < p.size:
            j = i + step
            if p[j] < thr:
                return float(f[i] + (t_db - pdb[i]) / (pdb[j] - pdb[i]) * (f[j] - f[i]))
            i = j
        raise MeasurementError("Çizgi genişliği: −3 dB kenarı spektrum içinde bulunamadı")

    return edge(+1) - edge(-1)


def dip_analysis(f: np.ndarray, p: np.ndarray, fa: float, fb: float, tol: float) -> dict[str, Any]:
    """Two-peak test: a local maximum within ``±tol`` of each of ``fa < fb`` (distinct) and the
    dip ``min(peak_a, peak_b) / min(p between)`` in dB. ``dip_db = 0`` when not two peaks."""
    f = np.asarray(f, float)
    p = np.asarray(p, float)
    idx = np.flatnonzero((f >= fa - tol) & (f <= fb + tol))
    idx = idx[(idx > 0) & (idx < p.size - 1)]
    loc = idx[(p[idx] > p[idx - 1]) & (p[idx] >= p[idx + 1])]
    near_a = [int(k) for k in loc if abs(f[k] - fa) <= tol]
    near_b = [int(k) for k in loc if abs(f[k] - fb) <= tol]
    out: dict[str, Any] = {"dip_db": 0.0, "two_peaks": False, "f_peak_a_hz": None, "f_peak_b_hz": None,
                           "f_dip_hz": None, "k_a": None, "k_b": None, "k_dip": None}
    if not near_a or not near_b:
        return out
    ka = max(near_a, key=lambda k: p[k])
    kb = max(near_b, key=lambda k: p[k])
    if ka >= kb:
        return out
    kd = ka + int(np.argmin(p[ka:kb + 1]))
    dip = float(db10(min(p[ka], p[kb]) / max(p[kd], 1e-300)))
    out.update({"dip_db": dip, "two_peaks": True, "f_peak_a_hz": float(f[ka]), "f_peak_b_hz": float(f[kb]),
                "f_dip_hz": float(f[kd]), "k_a": ka, "k_b": kb, "k_dip": kd})
    return out


def analytic_dip_db(window: str, nfft: int, sep_bins: np.ndarray) -> np.ndarray:
    """Dip (dB) of two equal, *incoherent* tones ``sep_bins`` apart for an N-point ``window``
    (calculated from the window's line shape; 0 when not two peaks)."""
    w = get_window(window, nfft)
    m = nfft * _LINE_ZP
    line = np.abs(np.fft.fft(w, m)) ** 2
    line = np.fft.fftshift(line / line[0])
    xb = (np.arange(m) - m // 2) / _LINE_ZP
    out = []
    for s in np.asarray(sep_bins, float):
        half = s / 2.0
        x = np.arange(-half - 2.0, half + 2.0 + 1e-9, 1.0 / _LINE_ZP)
        pp = np.interp(x - half, xb, line) + np.interp(x + half, xb, line)
        out.append(dip_analysis(x, pp, -half, half, max(1.0, 0.5 * s))["dip_db"])
    return np.asarray(out)


# =============================================================================== measurement
@dataclass
class _SepPoint:
    sep_hz: float
    dip_db: float
    resolved: bool
    phase: str                 # "seri" | "ikiye bölme"
    info: dict[str, Any]
    spec: Spectrum | None = field(default=None, repr=False)


@register
class FreqResolutionMeasurement(Measurement):
    spec_id = "freq_resolution"
    title = "Frekans çözünürlüğü"
    unit = "Hz"
    default_params = {
        "fc": None,                     # None → rx.center_freq_hz
        "fs": 2e6,
        "bw": None,                     # None → fs
        "gain": 40.0,
        "nfft": None,                   # None → dsp.nfft
        "window": None,                 # None → dsp.window
        "averages": None,               # None → dsp.averages
        "overlap": None,                # None → dsp.overlap
        "zero_pad": 8,
        "sep_min_rbw": 0.5,
        "sep_max_rbw": 6.0,
        "sep_points": 12,
        "bisect_steps": 3,
        "captures": 4,
        "quick_captures": 2,
        "dip_db": 3.0,
        "tone_dbfs": -20.0,             # per tone
        "center_frac": 0.2,
        "accuracy_n": 2 ** 18,
        "accuracy_frac": [0.15, -0.1],  # [in-situ ppm estimate tone, validation tone] (× fs)
        "reference_locked": None,       # source locked to a GPSDO/10 MHz reference? None → auto (see module doc)
    }

    # ------------------------------------------------------------------ parameters
    def _params(self) -> dict[str, Any]:
        p, app = self.p, self.ctx.app
        fs = as_float(p["fs"], "freq_resolution.fs", 1.0)
        acc = as_float_list(p["accuracy_frac"], "freq_resolution.accuracy_frac", -0.45, 0.45)
        if len(acc) != 2 or abs(acc[0] - acc[1]) < 0.02:
            raise ConfigError("freq_resolution.accuracy_frac iki farklı ofset olmalı: [ppm kestirimi, doğrulama]")
        prm = {
            "fc": as_float(p["fc"] if p["fc"] is not None else app.rx.center_freq_hz, "freq_resolution.fc", 1.0),
            "fs": fs,
            "bw": as_float(p["bw"], "freq_resolution.bw", 1.0) if p["bw"] is not None else fs,
            "gain": as_float(p["gain"], "freq_resolution.gain"),
            "nfft": as_int(p["nfft"] if p["nfft"] is not None else app.dsp.nfft, "freq_resolution.nfft", 64),
            "window": canonical_window_name(str(p["window"] if p["window"] is not None else app.dsp.window)),
            "averages": as_int(p["averages"] if p["averages"] is not None else app.dsp.averages,
                               "freq_resolution.averages"),
            "overlap": as_float(p["overlap"] if p["overlap"] is not None else app.dsp.overlap,
                                "freq_resolution.overlap", 0.0, 0.95),
            "zero_pad": as_int(p["zero_pad"], "freq_resolution.zero_pad", 1),
            "sep_min_rbw": as_float(p["sep_min_rbw"], "freq_resolution.sep_min_rbw", 0.05),
            "sep_max_rbw": as_float(p["sep_max_rbw"], "freq_resolution.sep_max_rbw", 0.1),
            "sep_points": as_int(p["sep_points"], "freq_resolution.sep_points", 2),
            "bisect_steps": as_int(p["bisect_steps"], "freq_resolution.bisect_steps", 0),
            "captures": as_int(qparam(self, "captures"), "freq_resolution.captures"),
            "dip_db": as_float(p["dip_db"], "freq_resolution.dip_db", 0.1, 40.0),
            "tone_dbfs": as_float(p["tone_dbfs"], "freq_resolution.tone_dbfs", -80.0, -9.0),
            "center_frac": as_float(p["center_frac"], "freq_resolution.center_frac", -0.4, 0.4),
            "accuracy_n": as_int(p["accuracy_n"], "freq_resolution.accuracy_n", 1024),
            "accuracy_frac": acc,
        }
        locked = p["reference_locked"]
        if locked is not None and not isinstance(locked, bool):
            raise ConfigError(f"freq_resolution.reference_locked true/false olmalı, {locked!r} geldi")
        prm["reference_locked"] = locked
        if prm["sep_max_rbw"] <= prm["sep_min_rbw"]:
            raise ConfigError("freq_resolution.sep_max_rbw, sep_min_rbw'den büyük olmalı")
        if prm["accuracy_n"] < prm["nfft"]:
            raise ConfigError("freq_resolution.accuracy_n en az nfft olmalı")
        return prm

    # ------------------------------------------------------------------ run
    def run(self) -> MeasurementResult:
        ctx = self.ctx
        dev = ctx.dut
        prm = self._params()
        setup_rx(dev, fc=prm["fc"], fs=prm["fs"], bw=prm["bw"], gain=prm["gain"],
                 tracking={"dc": True, "rf_dc": True, "quadrature": True})
        info = window_info(prm["window"], prm["nfft"])
        rbw = info.enbw_bins * float(dev.fs) / prm["nfft"]
        out: dict[str, Any] = {"rbw": rbw, "enbw_bins": info.enbw_bins, "fs": float(dev.fs)}
        src = ctx.source
        if src is None:
            with ctx.terminated_input():
                blk = settled_capture(dev, welch_len(prm["nfft"], prm["averages"], prm["overlap"]))
            out["noise_spec"] = spectrum_from_block(blk, prm["nfft"], prm["window"], prm["overlap"])
        else:
            try:
                with ctx.isolated_source():
                    self._accuracy(dev, prm, out)
                    if int(getattr(src, "max_tones", 1)) >= 2:
                        self._two_tone(dev, prm, out)
            finally:
                src.set_enabled(False)
                if out.get("two_tone_used"):
                    # leave the generator in single-tone mode (other measurements use tone())
                    src.set_frequency(out["f1_hz"])
                    src.set_enabled(False)
        return self._result(dev, prm, out)

    # ------------------------------------------------------------------ (a) + (c)
    def _reference_locked(self, prm: dict[str, Any]) -> bool | None:
        """Is the source locked to a reference (its frequency = the truth)? Parameter first;
        auto: simulator source yes, Pluto TX (own ±25 ppm TCXO) no, anything else unknown."""
        if prm["reference_locked"] is not None:
            return bool(prm["reference_locked"])
        from ...hal.sim.source import SimSignalSource

        src = self.ctx.source
        if isinstance(src, SimSignalSource):
            return True
        if str(getattr(src, "name", "")) == "pluto_tx":
            return False
        return None

    def _record(self, dev: Any, prm: dict[str, Any], f_src: float, setting: float) -> np.ndarray:
        with tone(self.ctx, f_src, setting):
            blk = settled_capture(dev, prm["accuracy_n"])
        return np.asarray(blk.samples[0])

    def _accuracy(self, dev: Any, prm: dict[str, Any], out: dict[str, Any]) -> None:
        ctx = self.ctx
        nfft, avg, win, ov = prm["nfft"], prm["averages"], prm["window"], prm["overlap"]
        fc, fs = float(dev.fc), float(dev.fs)
        f1 = fc + prm["accuracy_frac"][0] * fs
        f2 = fc + prm["accuracy_frac"][1] * fs
        probe = adjust_tone(ctx, dev, f1, prm["tone_dbfs"], nfft=nfft, averages=avg, window=win, overlap=ov,
                            tol_db=1.0)
        setting = probe.setting
        ref_ppm = float(getattr(dev.profile, "ref_clock_ppm", 25.0))
        # (a) + in-situ ppm on record 1
        x1 = self._record(dev, prm, f1, setting)
        ppm_est = estimate_ppm_from_samples(x1, fs, fc, f1, ref_clock_ppm=ref_ppm)
        e = ppm_est * 1e-6
        acc1, nseg1 = zp_welch_sum(x1, nfft, win, ov, prm["zero_pad"])
        line_spec = zp_spectrum(acc1, nseg1, fc, fs, nfft, win, ov, prm["zero_pad"])
        f_app1 = f1 / (1.0 + e)
        tol = 2.0 * fs / nfft
        sel = np.flatnonzero(np.abs(line_spec.f_hz - f_app1) <= tol)
        kp = int(sel[np.argmax(line_spec.p_lin[sel])])
        width = line_width_hz(line_spec.f_hz, line_spec.p_lin, kp)
        # (c) validation record at f2 (independent capture, different frequency)
        x2 = self._record(dev, prm, f2, setting)
        f_meas = tone_frequency(x2, fs, fc=fc, f_hint=f2 / (1.0 + e), search_hz=10.0 * fs / nfft)
        err_raw = f_meas - f2
        f_corr = float(correct_freq(f_meas, ppm_est))
        err_corr = f_corr - f2
        locked = self._reference_locked(prm)
        acc: dict[str, Any] = {
            "f_ref_hz": f2, "f_measured_hz": f_meas, "freq_error_raw_hz": err_raw,
            "freq_error_raw_ppm": err_raw / f2 * 1e6, "ppm_estimate": ppm_est, "ppm_estimate_tone_hz": f1,
            # same source reference for the ppm tone and the validation tone: self-consistency, NOT accuracy
            "freq_error_corrected_hz": err_corr, "freq_error_self_consistency_hz": err_corr,
            "freq_error_corrected_is_self_consistency": True,
            "residual_ppm": err_corr / f2 * 1e6, "ppm_hw": float(dev.ppm_hw),
            "reference_locked": locked, "accuracy_verified": locked is True,
        }
        cal_ppm = getattr(getattr(ctx.cal, "ppm", None), "ppm", None) if ctx.cal is not None else None
        if cal_ppm is not None and math.isfinite(float(cal_ppm)):
            sw = residual_ppm(float(cal_ppm), float(dev.ppm_hw))
            f_cal = float(correct_freq(f_meas, sw))
            acc.update({"ppm_cal": float(cal_ppm), "ppm_cal_software": sw, "freq_error_cal_hz": f_cal - f2,
                        "freq_error_cal_ppm": (f_cal - f2) / f2 * 1e6})
        out.update({"accuracy": acc, "line_width_hz": width, "line_spec": line_spec, "line_peak_hz": float(line_spec.f_hz[kp]),
                    "setting": setting, "e": e, "f1_hz": f1, "probe_offset_db": probe.offset_db})

    # ------------------------------------------------------------------ (b)
    def _two_tone_point(self, dev: Any, prm: dict[str, Any], sep: float, center: float, e: float, setting: float,
                        rbw: float, phase: str) -> _SepPoint:
        src = self.ctx.source
        nfft, avg, win, ov = prm["nfft"], prm["averages"], prm["window"], prm["overlap"]
        fa, fb = center - 0.5 * sep, center + 0.5 * sep
        src.set_tones([fa * (1.0 + e), fb * (1.0 + e)], [setting, setting])
        src.set_enabled(True)
        try:
            stimulus_changed(self.ctx)
            acc = None
            n_tot = 0
            for _ in range(prm["captures"]):
                blk = dev.capture(welch_len(nfft, avg, ov))
                a, n = zp_welch_sum(blk.samples[0], nfft, win, ov, prm["zero_pad"])
                acc = a if acc is None else acc + a
                n_tot += n
        finally:
            src.set_enabled(False)
        spec = zp_spectrum(acc, n_tot, float(dev.fc), float(dev.fs), nfft, win, ov, prm["zero_pad"])
        d = dip_analysis(spec.f_hz, spec.p_lin, fa, fb, rbw)
        d.update({"f_a_hz": fa, "f_b_hz": fb})
        return _SepPoint(sep_hz=sep, dip_db=d["dip_db"], resolved=bool(d["two_peaks"] and d["dip_db"] >= prm["dip_db"]),
                         phase=phase, info=d, spec=spec)

    def _two_tone(self, dev: Any, prm: dict[str, Any], out: dict[str, Any]) -> None:
        rbw = out["rbw"]
        e, setting = out["e"], out["setting"]
        center = float(dev.fc) + prm["center_frac"] * float(dev.fs)
        out["two_tone_used"] = True
        seps = np.geomspace(prm["sep_min_rbw"], prm["sep_max_rbw"], prm["sep_points"]) * rbw
        pts = [self._two_tone_point(dev, prm, float(s), center, e, setting, rbw, "seri") for s in seps]
        res = [p.resolved for p in pts]
        i_star = len(res)
        while i_star > 0 and res[i_star - 1]:
            i_star -= 1
        if i_star == len(res):
            raise MeasurementError(
                f"En büyük ayrımda ({seps[-1]:.0f} Hz = {prm['sep_max_rbw']:g}×RBW) bile iki ton ayrışmadı — "
                "kaynak/çoklu ton ayarını kontrol edin")
        hi = pts[i_star]
        lo = pts[i_star - 1] if i_star > 0 else None
        if lo is not None:
            for _ in range(prm["bisect_steps"]):
                mid = math.sqrt(lo.sep_hz * hi.sep_hz)
                pt = self._two_tone_point(dev, prm, mid, center, e, setting, rbw, "ikiye bölme")
                pts.append(pt)
                if pt.resolved:
                    hi = pt
                else:
                    lo = pt
        out.update({"seps": sorted(pts, key=lambda q: q.sep_hz), "hi": hi, "lo": lo, "center_hz": center,
                    "lower_end": lo is None})
        for q in out["seps"]:
            if q is not hi and q is not lo:
                q.spec = None

    # ------------------------------------------------------------------ result
    def _result(self, dev: Any, prm: dict[str, Any], out: dict[str, Any]) -> MeasurementResult:
        rbw = out["rbw"]
        acc = out.get("accuracy")
        hi: _SepPoint | None = out.get("hi")
        lo: _SepPoint | None = out.get("lo")
        src = self.ctx.source
        notes: list[str] = []
        uncertainty: float | None = None
        if hi is not None:
            value = hi.sep_hz
            value_source = "iki-ton ölçümü"
            if lo is not None:
                uncertainty = hi.sep_hz - lo.sep_hz
            else:
                notes.append(f"en küçük denenen ayrım ({prm['sep_min_rbw']:g}×RBW) bile ayrıştı — değer üst sınırdır (≤)")
        else:
            value = rbw
            value_source = "RBW (hesap)"
            notes.append("çoklu ton kaynağı yok — değer hesaplanan RBW'dir" if src is not None
                         else "kaynak yok — değer hesaplanan RBW'dir")
        extra: dict[str, Any] = {
            "rbw_hz": rbw, "enbw_bins": out["enbw_bins"], "bin_hz": float(dev.fs) / prm["nfft"],
            "value_source": value_source, "zero_pad": prm["zero_pad"],
            "value_bound": "upper" if (hi is not None and lo is None) else "",
        }
        if "line_width_hz" in out:
            extra.update({"line_3db_hz": out["line_width_hz"], "line_3db_bins": out["line_width_hz"] * prm["nfft"] / float(dev.fs)})
        if hi is not None:
            extra.update({"min_separation_hz": hi.sep_hz, "min_separation_rbw": hi.sep_hz / rbw,
                          "dip_at_min_db": hi.dip_db, "captures_per_separation": prm["captures"]})
            if lo is not None:
                extra.update({"max_unresolved_hz": lo.sep_hz, "dip_at_max_unresolved_db": lo.dip_db})
        if acc is not None:
            extra.update(acc)
        conditions = base_conditions(
            dev, None, nfft=prm["nfft"], rbw_hz=rbw, averages=prm["averages"], window=prm["window"],
            overlap=prm["overlap"], zero_pad=prm["zero_pad"], dip_db=prm["dip_db"], captures=prm["captures"],
            tone_dbfs=prm["tone_dbfs"], source_max_tones=int(getattr(src, "max_tones", 0)) if src is not None else 0,
            reference_note="donanımda kaynak GPSDO/10 MHz referansa kilitli olmalı (frekans doğruluğu için gerçek değer)",
            **source_conditions(self.ctx),
        )
        if acc is not None:
            conditions["source_reference_locked"] = acc["reference_locked"]
        parts = [f"Frekans çözünürlüğü {value:.0f} Hz ({value_source}; RBW {rbw:.0f} Hz = {out['enbw_bins']:.3f} bin × "
                 f"{fmt_hz(float(dev.fs))}/{prm['nfft']}, {prm['window']}, {fmt_hz(prm['fc'])})"]
        if hi is not None:
            parts.append(f"≥ {prm['dip_db']:g} dB çukurla en küçük ayrım = {hi.sep_hz / rbw:.2f}×RBW")
        if "line_width_hz" in out:
            parts.append(f"tek ton −3 dB çizgi genişliği {out['line_width_hz']:.0f} Hz")
        if acc is not None:
            locked = acc["reference_locked"]
            if locked is True:
                s = (f"{fmt_hz(acc['f_ref_hz'], 10)} referansında frekans hatası ham {acc['freq_error_raw_hz']:+.1f} Hz "
                     f"({acc['freq_error_raw_ppm']:+.3f} ppm)")
                if "freq_error_cal_hz" in acc:
                    s += (f", kalibrasyon ppm'i ({acc['ppm_cal']:+.4f}) sonrası {acc['freq_error_cal_hz']:+.1f} Hz "
                          "(doğruluk)")
                else:
                    s += "; kayıtlı kalibrasyon ppm'i yok — düzeltme sonrası doğruluk ölçülmedi"
            else:
                why = ("kaynak referansa kilitli değil (örn. pluto_tx TCXO ±25 ppm)" if locked is False
                       else "kaynağın referans kilidi bildirilmedi (freq_resolution.reference_locked)")
                s = (f"{why}: {fmt_hz(acc['f_ref_hz'], 10)} tonunda ham fark {acc['freq_error_raw_hz']:+.1f} Hz "
                     f"({acc['freq_error_raw_ppm']:+.3f} ppm) yalnızca kaynak–alıcı bağıl referans farkıdır, mutlak "
                     "frekans doğruluğu iddia edilmez")
            s += (f"; yerinde ppm ({acc['ppm_estimate']:+.4f}) ile öz-tutarlılık {acc['freq_error_corrected_hz']:+.3f} Hz "
                  "(aynı kaynak referansı — doğruluk değildir)")
            parts.append(s)
        summary = "; ".join(parts + notes)
        method = (
            "RBW = ENBW·fs/N hesaplanır; tek tonun çizgi genişliği sıfır dolgulu (×"
            f"{prm['zero_pad']}) Welch spektrumunda −3 dB noktaları arasında enterpolasyonla ölçülür. İki eşit ton "
            f"(fc + {prm['center_frac']:g}·fs ± Δf/2, yerinde ppm düzeltmeli) {prm['sep_min_rbw']:g}…"
            f"{prm['sep_max_rbw']:g}×RBW geometrik serisinde verilir; {prm['captures']} bağımsız yakalamanın "
            "ortalama spektrumunda her tonun ±RBW'si içinde iki tepe ve aralarında ≥ "
            f"{prm['dip_db']:g} dB çukur aranır, sınır {prm['bisect_steps']} geometrik ikiye bölmeyle daraltılır. "
            "Frekans doğruluğu: bilinen frekanstaki ton (kaynak = gerçek değer; donanımda GPSDO kilitli olmalı) "
            "tone_frequency ile ölçülür; ham hata ve kayıtlı kalibrasyon ppm'i (bağımsız referans) sonrası hata "
            "doğruluk olarak raporlanır. Aynı kaynağın başka bir tonundan yerinde ppm kestirimi (estimate_ppm_from_"
            "samples) sonrası kalan hata, kaynak ile alıcının ortak referans oranını paylaştığından yalnızca "
            "öz-tutarlılıktır (doğruluk değildir). Kaynak referansa kilitli değilse (pluto_tx) mutlak doğruluk iddia "
            "edilmez; ham fark kaynak–alıcı bağıl hatasıdır.")
        if hi is None:
            method += " İki-ton ölçümü yapılamadı (çoklu ton kaynağı yok): değer hesaplanan RBW'dir."
        res = self.new_result(float(value), summary=summary, method=method, conditions=conditions, extra=extra,
                              uncertainty=uncertainty)
        res.table = self._table(prm, out)
        res.figures = self._figures(dev, prm, out, value)
        return res

    def _table(self, prm: dict[str, Any], out: dict[str, Any]) -> list[dict[str, Any]]:
        rbw = out["rbw"]
        rows: list[dict[str, Any]] = [{"bölüm": "RBW", "rbw_hz": rbw, "enbw_bins": out["enbw_bins"], "fs_hz": out["fs"],
                                       "nfft": prm["nfft"], "window": prm["window"]}]
        for q in out.get("seps", []):
            rows.append({"bölüm": "iki-ton", "sep_hz": q.sep_hz, "sep_rbw": q.sep_hz / rbw, "dip_db": q.dip_db,
                         "resolved": q.resolved, "two_peaks": q.info["two_peaks"], "f_a_hz": q.info["f_a_hz"],
                         "f_b_hz": q.info["f_b_hz"], "f_peak_a_hz": q.info["f_peak_a_hz"],
                         "f_peak_b_hz": q.info["f_peak_b_hz"], "phase": q.phase})
        acc = out.get("accuracy")
        if acc is not None:
            rows.append({"bölüm": "doğruluk", "f_ref_hz": acc["f_ref_hz"], "f_measured_hz": acc["f_measured_hz"],
                         "freq_error_raw_hz": acc["freq_error_raw_hz"],
                         "freq_error_corrected_hz": acc["freq_error_corrected_hz"],
                         "freq_error_cal_hz": acc.get("freq_error_cal_hz"), "ppm_estimate": acc["ppm_estimate"]})
        if "line_width_hz" in out:
            rows.append({"bölüm": "çizgi", "line_3db_hz": out["line_width_hz"], "rbw_hz": rbw})
        return rows

    def _zoom_shot(self, q: _SepPoint, name: str, title: str, rbw: float, center: float) -> FigureSpec:
        span = max(3.0 * q.sep_hz, 14.0 * rbw)
        sl = q.spec.slice(center - span / 2.0, center + span / 2.0)
        top = float(np.max(sl.p_db))
        mk = []
        if q.info["two_peaks"]:
            for key in ("f_peak_a_hz", "f_peak_b_hz", "f_dip_hz"):
                fk = q.info[key]
                k = int(np.argmin(np.abs(sl.f_hz - fk)))
                mk.append((fk, float(sl.p_db[k]), "çukur" if key == "f_dip_hz" else "tepe"))
        else:
            k = int(np.argmax(sl.p_lin))
            mk.append((float(sl.f_hz[k]), float(sl.p_db[k]), "tek tepe"))
        fig = spectrum_screenshot(sl, name, f"{title}, çukur {q.dip_db:.1f} dB", markers=mk, max_points=sl.n_bins,
                                  ref_level=top, extra_info={"Center": fmt_hz(center, 9), "Span": fmt_hz(span, 4)})
        if fig.ylim is not None:
            fig.ylim = (fig.ylim[1] - 60.0, fig.ylim[1])
        return fig

    def _figures(self, dev: Any, prm: dict[str, Any], out: dict[str, Any], value: float) -> list[FigureSpec]:
        rbw = out["rbw"]
        nfft = prm["nfft"]
        bin_hz = float(dev.fs) / nfft
        figs: list[FigureSpec] = []
        grid = np.linspace(prm["sep_min_rbw"], prm["sep_max_rbw"], 80)
        theo = analytic_dip_db(prm["window"], nfft, grid * rbw / bin_hz)
        series = [Series(x=grid.tolist(), y=theo.tolist(), label=f"Hesap ({prm['window']}, eş güçlü, eşevresiz)",
                         style="line")]
        seps: list[_SepPoint] = out.get("seps", [])
        if seps:
            series.insert(0, Series(x=[q.sep_hz / rbw for q in seps], y=[q.dip_db for q in seps],
                                    label="Ölçülen çukur", style="marker"))
        # y range from the data: every measured dip stays in view (the theory may leave the frame)
        top = max([prm["dip_db"]] + [q.dip_db for q in seps if math.isfinite(q.dip_db)]) if seps else 40.0
        y_hi = 10.0 * math.ceil((max(top, prm["dip_db"]) + 5.0) / 10.0)
        figs.append(FigureSpec(
            name="freq_resolution_dip_vs_separation", title="İki-ton çukur derinliği – ayrım", kind="xy", series=series,
            xlabel="Ayrım Δf / RBW", ylabel="Çukur [dB]", ylim=(-2.0, y_hi),
            xlim=(0.0, prm["sep_max_rbw"] * 1.03),
            hlines=[(prm["dip_db"], f"{prm['dip_db']:g} dB ölçütü")],
            vlines=[(value / rbw, f"Δf_min = {value:.0f} Hz")],
            info={"RBW": fmt_hz(rbw, 4), "N": str(nfft), "Pencere": prm["window"]}))
        hi: _SepPoint | None = out.get("hi")
        lo: _SepPoint | None = out.get("lo")
        if hi is not None and hi.spec is not None:
            c = out["center_hz"]
            figs.append(self._zoom_shot(hi, "freq_resolution_resolved",
                                        f"Ayrışan iki ton: Δf = {hi.sep_hz:.0f} Hz ({hi.sep_hz / rbw:.2f}×RBW)", rbw, c))
            if lo is not None and lo.spec is not None:
                figs.append(self._zoom_shot(lo, "freq_resolution_unresolved",
                                            f"Ayrışmayan iki ton: Δf = {lo.sep_hz:.0f} Hz ({lo.sep_hz / rbw:.2f}×RBW)",
                                            rbw, c))
        if "line_spec" in out:
            ls: Spectrum = out["line_spec"]
            fpk = out["line_peak_hz"]
            span = 20.0 * rbw
            sl = ls.slice(fpk - span / 2.0, fpk + span / 2.0)
            fig = spectrum_screenshot(sl, "freq_resolution_single_tone",
                                      f"Tek ton çizgisi (−3 dB genişlik {out['line_width_hz']:.0f} Hz, RBW {rbw:.0f} Hz)",
                                      max_points=sl.n_bins, ref_level=float(np.max(sl.p_db)),
                                      extra_info={"Center": fmt_hz(fpk, 10), "Span": fmt_hz(span, 4)})
            if fig.ylim is not None:
                fig.ylim = (fig.ylim[1] - 100.0, fig.ylim[1])
            figs.append(fig)
        if "noise_spec" in out:
            figs.append(spectrum_screenshot(out["noise_spec"], "freq_resolution_noise",
                                            f"Analiz ayarlarıyla spektrum (RBW {rbw:.0f} Hz, kaynak yok)",
                                            max_points=out["noise_spec"].n_bins))
        return figs
