"""Absolute power calibration: ``dBm = dBFS + offset_db(f, G)``.

The AD936x receive chain is close to linear in dB: ``dBFS ≈ dBm + G + C(f)``, so the
offset ``dBm − dBFS ≈ −C(f) − G`` falls with slope −1 dB/dB of hardware gain ``G``. The
real slope deviates by a few tenths of a dB over the gain table and ``C(f)`` varies with
frequency (LNA, matching, filters), hence a measured table.

:class:`PowerCalTable` interpolation rules (all in dB):

* **frequency**: piecewise linear between the measured frequencies, constant beyond the
  first/last one;
* **gain** (at each measured frequency): piecewise linear between the gains measured at
  that frequency; outside them extrapolated with the ideal slope −1 dB/dB from the nearest
  measured gain (a frequency with a single gain point uses slope −1 everywhere).

Offsets refer to signals near the centre of the analog passband (the table is measured
``offset_hz`` away from the LO); band-edge roll-off of the analog filter is not included.
"""

from __future__ import annotations

import logging
import math
from collections.abc import Callable, Iterable, Mapping, Sequence
from typing import Any

import numpy as np

from ..core.errors import CalibrationError, ConfigError
from ..core.timeutil import now_ns
from ..core.units import fmt_hz
from ..dsp.levels import iq_levels
from ..dsp.psd import welch
from .ppm import (
    SEARCH_TOLERANCE_FACTOR,
    _finite,
    _positive_int,
    device_temperature_c,
    jsonable,
    noise_bin_mean,
    preserve_rx_settings,
)

log = logging.getLogger(__name__)

#: Ideal slope of the offset versus hardware gain (dB/dB).
GAIN_SLOPE: float = -1.0


class PowerCalTable:
    """dBFS → dBm offset table ``offset_db(f, G)`` (see module docstring).

    ``points``: iterable of ``(f_hz, gain_db, offset_db)``. Repeated ``(f, G)`` pairs are
    averaged (repeated measurements). ``meta``: free-form JSON-safe provenance.
    """

    def __init__(self, points: Iterable[Sequence[float]], meta: Mapping[str, Any] | None = None):
        acc: dict[float, dict[float, list[float]]] = {}
        for i, p in enumerate(points):
            if isinstance(p, (str, bytes)) or not isinstance(p, Sequence) or len(p) != 3:
                raise ConfigError(f"Güç kalibrasyon noktası {i}: (f_hz, gain_db, offset_db) bekleniyordu, {p!r} geldi")
            f = _finite(p[0], f"nokta {i} f_hz")
            g = _finite(p[1], f"nokta {i} gain_db")
            o = _finite(p[2], f"nokta {i} offset_db")
            if f <= 0:
                raise ConfigError(f"Güç kalibrasyon noktası {i}: frekans pozitif olmalı ({f!r})")
            acc.setdefault(f, {}).setdefault(g, []).append(o)
        if not acc:
            raise ConfigError("Güç kalibrasyon tablosu en az bir nokta içermeli")
        self._freqs = np.array(sorted(acc), dtype=np.float64)
        self._gains: list[np.ndarray] = []
        self._offs: list[np.ndarray] = []
        for f in self._freqs.tolist():
            gs = sorted(acc[f])
            self._gains.append(np.array(gs, dtype=np.float64))
            self._offs.append(np.array([float(np.mean(acc[f][g])) for g in gs], dtype=np.float64))
        self.meta: dict[str, Any] = dict(meta or {})

    # ------------------------------------------------------------------ inspection
    @property
    def points(self) -> list[tuple[float, float, float]]:
        """Sorted ``(f_hz, gain_db, offset_db)`` (duplicates already averaged)."""
        return [(float(f), float(g), float(o))
                for f, gs, os in zip(self._freqs, self._gains, self._offs) for g, o in zip(gs, os)]

    @property
    def freqs_hz(self) -> np.ndarray:
        return self._freqs.copy()

    def gains_at(self, f_hz: float) -> list[float]:
        """Measured gains at the table frequency nearest to ``f_hz``."""
        i = int(np.argmin(np.abs(self._freqs - float(f_hz))))
        return self._gains[i].tolist()

    def __len__(self) -> int:
        return sum(g.size for g in self._gains)

    def __repr__(self) -> str:
        return (f"PowerCalTable({len(self)} nokta, {self._freqs.size} frekans "
                f"{fmt_hz(self._freqs[0])} … {fmt_hz(self._freqs[-1])})")

    def __eq__(self, other: object) -> bool:
        return isinstance(other, PowerCalTable) and self.points == other.points

    __hash__ = None  # type: ignore[assignment]

    # ------------------------------------------------------------------ evaluation
    def _node(self, i: int, g: np.ndarray) -> np.ndarray:
        gs, os = self._gains[i], self._offs[i]
        out = np.interp(g, gs, os)
        lo, hi = g < gs[0], g > gs[-1]
        out[lo] = os[0] + GAIN_SLOPE * (g[lo] - gs[0])
        out[hi] = os[-1] + GAIN_SLOPE * (g[hi] - gs[-1])
        return out

    def offset_db(self, f_hz, gain_db):
        """Offset (dB) such that ``dBm = dBFS + offset``. Broadcasts arrays; scalars → float."""
        f = np.asarray(f_hz, dtype=np.float64)
        g = np.asarray(gain_db, dtype=np.float64)
        if not (np.all(np.isfinite(f)) and np.all(np.isfinite(g))):
            raise ConfigError("offset_db: frekans ve kazanç sonlu olmalı")
        fb, gb = np.broadcast_arrays(f, g)
        ff, gg = fb.ravel(), gb.ravel().copy()
        fr = self._freqs
        if fr.size == 1:
            out = self._node(0, gg)
        else:
            fcl = np.clip(ff, fr[0], fr[-1])
            i = np.clip(np.searchsorted(fr, fcl, side="right") - 1, 0, fr.size - 2)
            t = (fcl - fr[i]) / (fr[i + 1] - fr[i])
            out = np.empty_like(gg)
            for k in np.unique(i).tolist():
                m = i == k
                lo = self._node(k, gg[m])
                hi = self._node(k + 1, gg[m])
                out[m] = lo + t[m] * (hi - lo)
        if np.ndim(f_hz) == 0 and np.ndim(gain_db) == 0:
            return float(out[0])
        return out.reshape(fb.shape)

    def to_dbm(self, p_dbfs, f_hz, gain_db):
        """Convenience: ``p_dbfs + offset_db(f, G)``."""
        off = self.offset_db(f_hz, gain_db)
        if np.ndim(p_dbfs) == 0 and np.ndim(off) == 0:
            return float(p_dbfs) + float(off)
        return np.asarray(p_dbfs, dtype=np.float64) + off

    # ------------------------------------------------------------------ (de)serialization
    def to_dict(self) -> dict[str, Any]:
        return {"points": [list(p) for p in self.points], "meta": jsonable(self.meta)}

    @classmethod
    def from_dict(cls, d: Mapping[str, Any]) -> PowerCalTable:
        if not isinstance(d, Mapping):
            raise CalibrationError(f"Güç kalibrasyon kaydı bir tablo olmalı, {type(d).__name__} geldi")
        unknown = set(d) - {"points", "meta"}
        if unknown:
            raise CalibrationError(f"Güç kalibrasyon kaydında bilinmeyen alan(lar): {sorted(unknown)}")
        pts = d.get("points")
        if not isinstance(pts, list) or not pts:
            raise CalibrationError("Güç kalibrasyon kaydında 'points' listesi eksik ya da boş")
        meta = d.get("meta") or {}
        if not isinstance(meta, Mapping):
            raise CalibrationError("Güç kalibrasyon kaydında 'meta' bir tablo olmalı")
        try:
            return cls([tuple(p) for p in pts], meta=meta)
        except (ConfigError, TypeError) as e:
            raise CalibrationError(f"Güç kalibrasyon kaydı geçersiz: {e}") from e

    # ------------------------------------------------------------------ constructors
    @classmethod
    def from_model(cls, c0_db: float | Sequence[float], freqs: Sequence[float] | None = None) -> PowerCalTable:
        """Nominal table ``offset = c0 − G`` (one gain point per frequency, slope −1).

        ``c0_db`` is the offset at 0 dB gain (scalar, or one value per frequency). Without
        ``freqs`` the table has a single node (frequency independent). For the AD9363
        simulator model (``dBFS = dBm + G − 5``) ``c0 = +5``.
        """
        fl = [1e9] if freqs is None else [float(f) for f in freqs]
        if not fl:
            raise ConfigError("from_model: en az bir frekans gerekli")
        if np.ndim(c0_db) == 0:
            cs = [float(c0_db)] * len(fl)
        else:
            cs = [float(c) for c in c0_db]  # type: ignore[union-attr]
            if len(cs) != len(fl):
                raise ConfigError(f"from_model: {len(fl)} frekans için {len(cs)} c0 değeri verildi")
        return cls([(f, 0.0, c) for f, c in zip(fl, cs)], meta={"method": "model", "c0_db": cs})

    @classmethod
    def fit_from_measurements(cls, rows: Iterable[Mapping[str, Any]],
                              meta: Mapping[str, Any] | None = None) -> PowerCalTable:
        """Table from measurement rows ``{f_hz, gain_db, p_in_dbm, p_meas_dbfs}``.

        ``offset = p_in_dbm − p_meas_dbfs`` per row; repeated ``(f, G)`` are averaged.
        """
        pts = []
        rl = list(rows)
        for i, r in enumerate(rl):
            if not isinstance(r, Mapping):
                raise ConfigError(f"Ölçüm satırı {i} bir tablo olmalı, {r!r} geldi")
            missing = {"f_hz", "gain_db", "p_in_dbm", "p_meas_dbfs"} - set(r)
            if missing:
                raise ConfigError(f"Ölçüm satırı {i}: eksik alan(lar) {sorted(missing)}")
            off = _finite(r["p_in_dbm"], f"satır {i} p_in_dbm") - _finite(r["p_meas_dbfs"], f"satır {i} p_meas_dbfs")
            pts.append((r["f_hz"], r["gain_db"], off))
        if not pts:
            raise ConfigError("fit_from_measurements: ölçüm satırı yok")
        m = {"method": "measured", "rows": jsonable([dict(r) for r in rl])}
        m.update(meta or {})
        return cls(pts, meta=m)


# ============================================================================ measurement
def _p_in_for(p_in_dbm: Any, f: float, g: float, gi: int, n_gains: int) -> float:
    if callable(p_in_dbm):
        return _finite(p_in_dbm(f, g), "p_in_dbm(f, G)")
    if np.ndim(p_in_dbm) == 0:
        return _finite(p_in_dbm, "p_in_dbm")
    seq = list(p_in_dbm)
    if len(seq) != n_gains:
        raise ConfigError(f"p_in_dbm listesi kazanç sayısı kadar ({n_gains}) olmalı, {len(seq)} geldi")
    return _finite(seq[gi], f"p_in_dbm[{gi}]")


def measure_tone_dbfs(x: np.ndarray, fs: float, fc: float, f_tone_hz: float, half_search_hz: float,
                      nfft: int = 4096) -> tuple[float, float, float]:
    """``(power_dbfs, snr_db, f_peak_hz)`` of the tone nearest ``f_tone_hz`` (absolute RF).

    The strongest bin within ``±half_search_hz`` of ``f_tone_hz`` is taken as the tone;
    its power is the band power over ±6 bins (scalloping-free) minus the noise in those
    bins; SNR is relative to the noise in one RBW. Blackman-Harris Welch spectrum.
    """
    spec = welch(x, fs, nfft, "blackmanharris", 0.5, "mean", fc=fc)
    m = np.abs(spec.f_hz - f_tone_hz) <= half_search_hz
    if not np.any(m):
        raise CalibrationError(f"Ton arama penceresi ({fmt_hz(f_tone_hz)} ± {fmt_hz(half_search_hz)}) spektrum dışında")
    idx = np.flatnonzero(m)
    k = int(idx[np.argmax(spec.p_lin[idx])])
    f_pk = float(spec.f_hz[k])
    hw = 6.0 * spec.df_hz
    band = (spec.f_hz >= f_pk - hw) & (spec.f_hz <= f_pk + hw)
    noise = noise_bin_mean(spec)
    p = float(np.sum(spec.p_lin[band]) / spec.enbw_bins) - noise * int(np.count_nonzero(band)) / spec.enbw_bins
    p = max(p, 1e-30)
    return 10.0 * math.log10(p), 10.0 * math.log10(p / max(noise, 1e-30)), f_pk


def measure_power_cal(device: Any, source: Any, freqs_hz: Sequence[float], gains_db: Sequence[float],
                      p_in_dbm: float | Sequence[float] | Callable[[float, float], float], n: int = 2 ** 16,
                      offset_hz: float = 200e3, *, channel: int = 0, nfft: int = 4096, min_snr_db: float = 20.0,
                      max_peak_dbfs: float = -1.0, timeout_s: float = 2.0) -> PowerCalTable:
    """Measure a :class:`PowerCalTable` with an absolute-power stimulus.

    For every frequency ``f`` and gain ``G`` the device is tuned to ``fc = f − offset_hz``,
    set to manual gain ``G`` (actual, quantized gain recorded), the source emits ``p_in``
    dBm at ``f`` (``p_in_dbm``: scalar, one value per gain, or ``callable(f, G)`` — choose
    levels that neither clip at high gain nor sink into the noise at low gain), and the
    tone's band power is measured on a settled capture of ``n`` samples. The source is
    switched off and the device's settings are restored afterwards.

    Raises :class:`CalibrationError` if the source is not absolute (``absolute_power``),
    if a capture clips / exceeds ``max_peak_dbfs`` or if the tone SNR (in RBW) is below
    ``min_snr_db``.
    """
    if source is None or not getattr(source, "absolute_power", False):
        raise CalibrationError(
            "Güç kalibrasyonu mutlak güçlü bir kaynak gerektirir (absolute_power = True): kalibre sinyal "
            "üreteci (SCPI), simülatör kaynağı ya da anchor_dbm ile referanslanmış TX kullanın.")
    freqs = [_finite(f, "freqs_hz") for f in freqs_hz]
    gains = [_finite(g, "gains_db") for g in gains_db]
    if not freqs or not gains:
        raise ConfigError("measure_power_cal: en az bir frekans ve bir kazanç gerekli")
    n = _positive_int(n, "n", nfft)
    offset_hz = _finite(offset_hz, "offset_hz")
    channel = int(channel)
    if not 0 <= channel < int(device.num_channels):
        raise ConfigError(f"{device.name}: kanal {channel} yok ({device.num_channels} kanal)")
    fs = float(device.fs)
    ref_ppm = float(device.profile.ref_clock_ppm)
    rows: list[dict[str, Any]] = []
    t0 = now_ns()
    with preserve_rx_settings(device):
        temperature = device_temperature_c(device, channel)
        device.set_gain_mode("manual")
        try:
            for f in freqs:
                device.set_center_freq(f - offset_hz)
                f_act = float(source.set_frequency(f))
                half = SEARCH_TOLERANCE_FACTOR * ref_ppm * 1e-6 * f_act + 8.0 * fs / nfft
                if abs(offset_hz) - half < 8.0 * fs / nfft:
                    raise ConfigError(
                        f"offset_hz = {fmt_hz(offset_hz)} {fmt_hz(f)} için ppm arama penceresinden (±{fmt_hz(half)}) "
                        "küçük: ton DC'ye karışabilir, ofseti büyütün.")
                for gi, g in enumerate(gains):
                    device.set_gain(g)
                    g_act = float(device.gains[channel])
                    p_set = _p_in_for(p_in_dbm, f, g_act, gi, len(gains))
                    p_act = float(source.set_power(p_set))
                    source.set_enabled(True)
                    device.mark_settling()          # samples already in the FIFO predate the source
                    blk = device.capture(n, timeout_s)
                    x = blk.samples[channel]
                    lv = iq_levels(x, blk.clip_level)
                    if lv.clip_fraction > 0.0 or lv.peak_dbfs > max_peak_dbfs:
                        raise CalibrationError(
                            f"{device.name}: {fmt_hz(f)}, G = {g_act:g} dB, giriş {p_act:.1f} dBm'de seviye çok yüksek "
                            f"(tepe {lv.peak_dbfs:.1f} dBFS, kırpma %{100 * lv.clip_fraction:.4f}): bu kazanç için "
                            "p_in_dbm'i düşürün.")
                    p_meas, snr, f_pk = measure_tone_dbfs(x, blk.fs, blk.fc, f_act, half, nfft)
                    if snr < min_snr_db:
                        raise CalibrationError(
                            f"{device.name}: {fmt_hz(f)}, G = {g_act:g} dB'de ton SNR {snr:.1f} dB < {min_snr_db:.0f} dB: "
                            "p_in_dbm'i artırın, kazancı yükseltin ya da kablo bağlantısını kontrol edin.")
                    rows.append({"f_hz": f, "gain_db": g_act, "p_in_dbm": p_act, "p_meas_dbfs": p_meas,
                                 "snr_db": snr, "f_peak_hz": f_pk})
                    log.debug("%s: güç kal. f=%s G=%g p_in=%.2f dBm → %.2f dBFS (SNR %.1f dB)", device.name,
                              fmt_hz(f), g_act, p_act, p_meas, snr)
                    source.set_enabled(False)
        finally:
            source.set_enabled(False)
    meta = {"source": getattr(source, "name", type(source).__name__), "n": n, "offset_hz": offset_hz,
            "nfft": nfft, "t_ns": t0, "temperature_c": temperature, "device": device.name,
            "serial": getattr(device.info, "serial", ""), "channel": channel}
    table = PowerCalTable.fit_from_measurements(rows, meta=meta)
    log.info("%s: güç kalibrasyonu: %s", device.name, table)
    return table
