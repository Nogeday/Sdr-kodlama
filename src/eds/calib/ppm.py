"""Reference-clock (TCXO) error estimation and frequency correction.

Sign convention (docs/ARCHITECTURE.md section 2 — mandatory)::

    ppm    = (f_ref_true - f_ref_nominal) / f_ref_nominal * 1e6     (positive = TCXO fast)
    e      = ppm * 1e-6
    f_app  = f_true / (1 + e)          apparent RF frequency (LO and fs share the reference)
    f_true = f_app * (1 + e)           correction            -> :func:`correct_freq`
    fc_cmd = f / (1 + e)               tune exactly to f     -> :func:`tune_freq`
    xo_correction = ref_clock_hz * (1 + e)                    (hardware correction)

Hardware correction
-------------------
When the device already compensates ``e_hw`` (``RxDevice.set_ppm_correction`` →
``IQBlock.meta['ppm_hw']``), a measurement only sees the *residual*
``(1 + e_res) = (1 + e) / (1 + e_hw)``. :func:`estimate_ppm_cw` therefore records the
residual **and** returns the *total* error ``(1 + e) = (1 + e_hw)(1 + e_res)``
(:func:`compose_ppm`), always relative to the nominal reference. Storing that value and
re-applying it in hardware is idempotent: a second calibration with the correction
active reads ``residual ≈ 0`` and the same total.

The tone frequency is measured with :func:`eds.dsp.peaks.tone_frequency` (window +
zero-padded FFT + golden-section refinement of the DTFT magnitude), searching only
``±1.5 × ref_clock_ppm`` around the nominal reference so that the zero-IF DC spike, the IQ
image and other signals cannot be mistaken for the reference.
"""

from __future__ import annotations

import logging
import math
from collections.abc import Iterator, Mapping
from contextlib import contextmanager
from dataclasses import dataclass, field, fields
from typing import Any

import numpy as np

from ..core.errors import CalibrationError, ConfigError, EDSError, MeasurementError
from ..core.timeutil import now_ns
from ..core.units import fmt_hz
from ..dsp.levels import iq_levels
from ..dsp.peaks import tone_frequency
from ..dsp.psd import Spectrum, welch
from ..dsp.stats import effective_averages, gamma_quantile

log = logging.getLogger(__name__)

#: Methods recorded in :attr:`PpmEstimate.method`.
PPM_METHODS: tuple[str, ...] = ("cw", "gsm_fcch", "manual")

#: Search half-width factor applied to the profile's reference tolerance.
SEARCH_TOLERANCE_FACTOR: float = 1.5

#: Largest fraction of fs/2 a reference tone search may reach (analog filter roll-off).
MAX_SEARCH_FRACTION: float = 0.9

#: FFT length of the spectrum used for SNR estimation (RBW = ENBW * fs / N).
SNR_NFFT: int = 4096

#: Clipped-sample fraction above which a capture is rejected for calibration.
MAX_CLIP_FRACTION: float = 1e-4


# ============================================================================ helpers
def _finite(x: Any, what: str) -> float:
    if isinstance(x, bool) or not isinstance(x, (int, float, np.integer, np.floating)):
        raise ConfigError(f"{what}: sayı bekleniyordu, {x!r} geldi")
    v = float(x)
    if not math.isfinite(v):
        raise ConfigError(f"{what}: sonlu bir sayı olmalı, {x!r} geldi")
    return v


def _positive_int(x: Any, what: str, minimum: int = 1) -> int:
    if isinstance(x, bool) or not isinstance(x, (int, np.integer)) or int(x) < minimum:
        raise ConfigError(f"{what}: >= {minimum} tamsayı olmalı, {x!r} geldi")
    return int(x)


def jsonable(o: Any) -> Any:
    """JSON-safe deep copy: numpy scalars/arrays → Python, non-finite floats → ``None``."""
    if o is None or isinstance(o, (str, bool)):
        return o
    if isinstance(o, Mapping):
        return {str(k): jsonable(v) for k, v in o.items()}
    if isinstance(o, (list, tuple)):
        return [jsonable(v) for v in o]
    if isinstance(o, np.ndarray):
        return [jsonable(v) for v in o.tolist()]
    if isinstance(o, np.bool_):
        return bool(o)
    if isinstance(o, (int, np.integer)):
        return int(o)
    if isinstance(o, (float, np.floating)):
        f = float(o)
        return f if math.isfinite(f) else None
    if isinstance(o, np.str_):
        return str(o)
    raise ConfigError(f"JSON'a çevrilemeyen değer türü: {type(o).__name__}")


def _opt_float(v: Any, what: str) -> float:
    """``None`` → NaN (JSON null round trip), else a finite-or-not float."""
    if v is None:
        return math.nan
    if isinstance(v, bool) or not isinstance(v, (int, float)):
        raise CalibrationError(f"{what}: sayı bekleniyordu, {v!r} geldi")
    return float(v)


def compose_ppm(ppm_a: float, ppm_b: float) -> float:
    """Total error of two cascaded reference errors: ``(1+e) = (1+e_a)(1+e_b)`` (ppm)."""
    a, b = _finite(ppm_a, "ppm_a"), _finite(ppm_b, "ppm_b")
    return a + b + a * b * 1e-6                      # == ((1+e_a)(1+e_b) - 1) 1e6, no cancellation


def residual_ppm(ppm_total: float, ppm_hw: float) -> float:
    """Error left after a hardware correction ``e_hw``: ``(1+e_res) = (1+e)/(1+e_hw)`` (ppm)."""
    t, h = _finite(ppm_total, "ppm_total"), _finite(ppm_hw, "ppm_hw")
    return (t - h) / (1.0 + h * 1e-6)               # == ((1+e)/(1+e_hw) - 1) 1e6, no cancellation


def ppm_from_apparent(f_true_hz: float, f_app_hz: float) -> float:
    """PPM from a reference's true and apparent frequency: ``(f_true / f_app − 1) · 1e6``."""
    t, a = _finite(f_true_hz, "f_true_hz"), _finite(f_app_hz, "f_app_hz")
    if t <= 0 or a <= 0:
        raise ConfigError(f"Frekanslar pozitif olmalı (gerçek {t!r}, görünen {a!r})")
    return (t - a) / a * 1e6


def correct_freq(f_app_hz, ppm: float):
    """Apparent → true RF frequency: ``f_true = f_app (1 + e)``. Scalars or arrays."""
    e = _finite(ppm, "ppm") * 1e-6
    if np.ndim(f_app_hz) == 0:
        return float(f_app_hz) * (1.0 + e)
    return np.asarray(f_app_hz, dtype=np.float64) * (1.0 + e)


def tune_freq(f_target_hz, ppm: float):
    """Command frequency that makes the LO land exactly on ``f_target``: ``f / (1 + e)``."""
    e = _finite(ppm, "ppm") * 1e-6
    if np.ndim(f_target_hz) == 0:
        return float(f_target_hz) / (1.0 + e)
    return np.asarray(f_target_hz, dtype=np.float64) / (1.0 + e)


def xo_correction_hz(ppm: float, ref_clock_hz: float = 40e6) -> float:
    """Hardware reference value for AD936x ``xo_correction``: ``ref (1 + e)`` (Hz)."""
    return _finite(ref_clock_hz, "ref_clock_hz") * (1.0 + _finite(ppm, "ppm") * 1e-6)


def _member_for_channel(device: Any, channel: int) -> Any | None:
    """The member device that owns ``channel`` of a :class:`~eds.hal.group.DeviceGroup`."""
    members = getattr(device, "members", None)
    if not members:
        return None
    k = int(channel)
    for m in members:
        nch = int(getattr(m, "num_channels", 1))
        if k < nch:
            return m
        k -= nch
    return None


def channel_ppm_hw(device: Any, channel: int = 0, block: Any = None) -> float:
    """Hardware ppm correction in effect for ``channel`` (block meta first, then device).

    Groups carry member 0's value in ``meta['ppm_hw']``; other channels are resolved through
    the owning member device.
    """
    if channel != 0:
        member = _member_for_channel(device, channel)
        if member is not None:
            return float(getattr(member, "ppm_hw", 0.0))
    if block is not None and "ppm_hw" in getattr(block, "meta", {}):
        return float(block.meta["ppm_hw"])
    return float(getattr(device, "ppm_hw", 0.0))


def device_temperature_c(device: Any, channel: int = 0) -> float | None:
    """Device temperature (°C) from ``read_sensors()`` or ``None`` if unavailable.

    Groups report member sensors as ``"<member>.temperature_c"``; the member owning
    ``channel`` is used. Sensor failures are logged (calibration still proceeds).
    """
    try:
        sensors = device.read_sensors()
    except EDSError as e:
        log.warning("%s: sıcaklık sensörü okunamadı: %s", getattr(device, "name", "?"), e)
        return None
    t = sensors.get("temperature_c")
    if t is None:
        member = _member_for_channel(device, channel)
        if member is not None:
            t = sensors.get(f"{member.name}.temperature_c")
    if t is None or isinstance(t, (str, bool)):
        return None
    try:
        v = float(t)
    except (TypeError, ValueError):
        log.warning("%s: geçersiz sıcaklık değeri %r yok sayıldı", getattr(device, "name", "?"), t)
        return None
    return v if math.isfinite(v) else None


@contextmanager
def preserve_rx_settings(device: Any) -> Iterator[None]:
    """Restore fs, BW, fc, gain mode, gains and streaming state after a calibration step.

    Only settings that actually changed are re-applied (each change re-arms settling). A
    restore failure is raised as :class:`CalibrationError` unless another exception is
    already propagating (then it is logged and the original error wins).
    """
    saved = {
        "fs": float(device.fs), "bw": float(device.bw), "fc": float(device.fc),
        "gain_mode": str(device.gain_mode), "gains": [float(g) for g in device.gains],
        "streaming": bool(device.streaming),
    }
    failed = False
    try:
        yield
    except BaseException:
        failed = True
        raise
    finally:
        try:
            _restore(device, saved)
        except EDSError as e:
            if failed:
                log.error("%s: ayarlar geri yüklenemedi: %s", getattr(device, "name", "?"), e)
            else:
                raise CalibrationError(
                    f"{getattr(device, 'name', '?')}: kalibrasyon sonrası RX ayarları geri yüklenemedi: {e}") from e


def _restore(device: Any, s: dict[str, Any]) -> None:
    if s["fs"] > 0 and device.fs != s["fs"]:
        device.set_sample_rate(s["fs"])
    if s["bw"] > 0 and device.bw != s["bw"]:
        device.set_bandwidth(s["bw"])
    if s["fc"] > 0 and device.fc != s["fc"]:
        device.set_center_freq(s["fc"])
    if device.gain_mode != s["gain_mode"]:
        device.set_gain_mode(s["gain_mode"])
    if s["gain_mode"] == "manual":
        for ch, g in enumerate(s["gains"]):
            if ch < len(device.gains) and device.gains[ch] != g:
                device.set_gain(g, channel=ch)
    if not s["streaming"] and device.streaming:
        device.stop_stream()


def noise_bin_mean(spec: Spectrum) -> float:
    """Unbiased mean noise power per bin (linear) from the median of the usable bins.

    The median of averaged (Gamma-distributed) noise lies below its mean; the bias is
    removed with the median of a Gamma(n_eff) variable, ``n_eff`` = effective number of
    independent Welch averages.
    """
    mask = spec.usable_bins()
    p = spec.p_lin[mask] if np.any(mask) else spec.p_lin
    med = float(np.median(p))
    overlap = float(spec.meta.get("overlap", 0.0))
    n_eff = effective_averages(max(int(spec.averages), 1), overlap, spec.window, spec.nfft)
    ratio = gamma_quantile(0.5, n_eff) / n_eff
    return med / ratio


def tone_snr_db(x: np.ndarray, fs: float, f_bb_hz: float, nfft: int = SNR_NFFT) -> tuple[float, float, float]:
    """``(snr_db, tone_dbfs, rbw_hz)`` of a tone at baseband ``f_bb_hz`` in ``x``.

    SNR = tone band power (±5 bins, scalloping-free) over the noise power in one RBW of a
    Blackman-Harris Welch spectrum (spectrum-analyzer definition; RBW = ENBW·fs/N).
    """
    n = int(np.asarray(x).shape[-1])
    nf = int(min(nfft, 1 << max(6, int(math.floor(math.log2(max(n, 64)))))))
    spec = welch(x, fs, nf, "blackmanharris", 0.5, "mean", fc=0.0)
    hw = 5.0 * spec.df_hz
    p_tone = spec.band_power(f_bb_hz - hw, f_bb_hz + hw)
    noise = noise_bin_mean(spec)
    k = int(np.count_nonzero((spec.f_hz >= f_bb_hz - hw) & (spec.f_hz <= f_bb_hz + hw)))
    p_tone = max(p_tone - noise * k / spec.enbw_bins, 1e-30)
    return 10.0 * math.log10(p_tone / max(noise, 1e-30)), 10.0 * math.log10(p_tone), spec.rbw_hz


def search_halfwidth_hz(f_ref_hz: float, ref_clock_ppm: float, n: int, fs: float, ppm_hw: float = 0.0) -> float:
    """Half-width of the reference-tone search window (Hz).

    ``1.5 × ref_clock_ppm`` of ``f_ref`` (plus any hardware correction already applied, whose
    own error is unknown) and a margin of 8 frequency-resolution cells ``fs / n``.
    """
    tol = (SEARCH_TOLERANCE_FACTOR * abs(ref_clock_ppm) + abs(ppm_hw)) * 1e-6 * abs(f_ref_hz)
    return tol + 8.0 * fs / max(int(n), 1)


def _locate_reference(x: np.ndarray, fs: float, fc: float, f_ref_hz: float, half_hz: float) -> float:
    """Apparent RF frequency of the reference tone (DC removed first). Raises CalibrationError."""
    center = f_ref_hz - fc
    if abs(center) + half_hz > MAX_SEARCH_FRACTION * fs / 2.0:
        raise ConfigError(
            f"Referans arama penceresi ({fmt_hz(center - half_hz)} … {fmt_hz(center + half_hz)} taban bant) "
            f"fs/2 = {fmt_hz(fs / 2)} sınırına çok yakın: örnekleme hızını artırın ya da ofseti küçültün.")
    xs = np.asarray(x)
    if xs.ndim == 2 and xs.shape[0] == 1:
        xs = xs[0]
    xs = xs.astype(np.complex128) - np.mean(xs, dtype=np.complex128)   # zero-IF DC spike out
    try:
        return float(tone_frequency(xs, fs, fc=fc, f_hint=f_ref_hz, search_hz=half_hz))
    except MeasurementError as e:
        raise CalibrationError(
            f"Referans ton {fmt_hz(f_ref_hz)} ±{fmt_hz(half_hz)} aralığında bulunamadı ({e}). "
            "Kontrol: referans kaynağı açık ve doğru frekansta mı, kablo/zayıflatıcı bağlı mı, "
            "kazanç yeterli mi?") from e


# ============================================================================ estimate
@dataclass
class PpmEstimate:
    """Result of a reference-clock calibration (ARCHITECTURE sign convention).

    ppm             TOTAL reference error relative to the nominal reference (store/apply this)
    std_ppm         spread (standard deviation) of the individual estimates
    f_measured_hz   apparent RF frequency of the reference as received (mean)
    f_ref_hz        true frequency of the reference signal
    snr_db          reference SNR (CW: in one RBW of a 4096-point BH spectrum; FCCH: in-burst
                    per-sample SNR over fs) — see ``extra['snr_definition']``
    n               samples per estimate (CW: per repeat; FCCH: total captured)
    method          ``"cw"`` | ``"gsm_fcch"`` | ``"manual"``
    temperature_c   device temperature during the calibration (None if unknown)
    t_ns            wall-clock time of the calibration
    fc_nominal_hz   commanded centre frequency during the measurement
    fs_hz           sample rate during the measurement
    ppm_residual    error measured on top of the hardware correction in effect
    ppm_hw          hardware correction in effect (``IQBlock.meta['ppm_hw']``)
    repeats         number of individual estimates (CW repeats / FCCH bursts used)
    extra           provenance (per-repeat values, RBW, gain, burst list, ...)
    """

    ppm: float
    std_ppm: float
    f_measured_hz: float
    f_ref_hz: float
    snr_db: float
    n: int
    method: str
    temperature_c: float | None = None
    t_ns: int = 0
    fc_nominal_hz: float = 0.0
    fs_hz: float = 0.0
    ppm_residual: float = 0.0
    ppm_hw: float = 0.0
    repeats: int = 1
    extra: dict[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        self.ppm = _finite(self.ppm, "PpmEstimate.ppm")
        self.std_ppm = float(self.std_ppm)
        self.f_measured_hz = float(self.f_measured_hz)
        self.f_ref_hz = float(self.f_ref_hz)
        self.snr_db = float(self.snr_db)
        self.n = int(self.n)
        self.method = str(self.method)
        if self.temperature_c is not None:
            t = float(self.temperature_c)
            self.temperature_c = t if math.isfinite(t) else None
        self.t_ns = int(self.t_ns)
        self.fc_nominal_hz = float(self.fc_nominal_hz)
        self.fs_hz = float(self.fs_hz)
        self.ppm_residual = float(self.ppm_residual)
        self.ppm_hw = float(self.ppm_hw)
        self.repeats = int(self.repeats)
        if not isinstance(self.extra, dict):
            raise ConfigError("PpmEstimate.extra bir sözlük olmalı")

    @property
    def e(self) -> float:
        """Fractional error ``ppm · 1e-6``."""
        return self.ppm * 1e-6

    def to_dict(self) -> dict[str, Any]:
        """JSON-safe dict (NaN → null)."""
        return jsonable({f.name: getattr(self, f.name) for f in fields(self)})

    @classmethod
    def from_dict(cls, d: Mapping[str, Any]) -> PpmEstimate:
        """Inverse of :meth:`to_dict`. Unknown keys are an error (corrupt/foreign file)."""
        if not isinstance(d, Mapping):
            raise CalibrationError(f"PPM kaydı bir tablo olmalı, {type(d).__name__} geldi")
        names = {f.name for f in fields(cls)}
        unknown = set(d) - names
        if unknown:
            raise CalibrationError(f"PPM kaydında bilinmeyen alan(lar): {sorted(unknown)}")
        if d.get("ppm") is None:
            raise CalibrationError("PPM kaydında 'ppm' alanı eksik ya da boş")
        kw: dict[str, Any] = dict(d)
        for k in ("std_ppm", "f_measured_hz", "f_ref_hz", "snr_db", "fc_nominal_hz", "fs_hz",
                  "ppm_residual", "ppm_hw"):
            if k in kw:
                kw[k] = _opt_float(kw[k], f"ppm.{k}")
        for k in ("std_ppm", "f_measured_hz", "f_ref_hz", "snr_db"):
            kw.setdefault(k, math.nan)
        kw.setdefault("n", 0)
        kw.setdefault("method", "manual")
        if kw.get("extra") is None:
            kw["extra"] = {}
        try:
            return cls(**kw)
        except (TypeError, ValueError, ConfigError) as e:
            raise CalibrationError(f"PPM kaydı geçersiz: {e}") from e

    def summary_tr(self) -> str:
        """One-line Turkish summary for logs/reports."""
        t = f", {self.temperature_c:.1f} °C" if self.temperature_c is not None else ""
        return (f"PPM = {self.ppm:+.4f} ± {self.std_ppm:.4f} ({self.method}, ref {fmt_hz(self.f_ref_hz)}, "
                f"SNR {self.snr_db:.1f} dB{t})")


def estimate_ppm_from_samples(x: np.ndarray, fs: float, fc_nominal: float, f_ref_hz: float,
                              ref_clock_ppm: float = 25.0) -> float:
    """PPM of the reference relative to the clock that produced ``x``.

    ``x`` was captured at nominal centre ``fc_nominal`` / sample rate ``fs`` and contains a
    reference tone whose true frequency is ``f_ref_hz``. The apparent frequency ``f_app`` is
    searched within ``±1.5 · ref_clock_ppm`` of ``f_ref`` (plus 8 resolution cells) and
    ``ppm = (f_ref / f_app − 1) · 1e6``. If the samples were taken with a hardware
    correction active, the result is the *residual*; combine with :func:`compose_ppm`.

    Raises :class:`CalibrationError` if no tone is found and :class:`ConfigError` if the
    search window does not fit in the band.
    """
    fs = _finite(fs, "fs")
    if fs <= 0:
        raise ConfigError(f"fs pozitif olmalı, {fs!r} geldi")
    fc_nominal = _finite(fc_nominal, "fc_nominal")
    f_ref_hz = _finite(f_ref_hz, "f_ref_hz")
    if f_ref_hz <= 0:
        raise ConfigError(f"f_ref_hz pozitif olmalı, {f_ref_hz!r} geldi")
    ref_clock_ppm = _finite(ref_clock_ppm, "ref_clock_ppm")
    n = int(np.asarray(x).shape[-1])
    half = search_halfwidth_hz(f_ref_hz, ref_clock_ppm, n, fs)
    f_app = _locate_reference(x, fs, fc_nominal, f_ref_hz, half)
    return ppm_from_apparent(f_ref_hz, f_app)


def _crb_std_ppm(snr_rbw_db: float, rbw_hz: float, fs: float, n: int, f_hz: float) -> float:
    """Cramér-Rao bound of a tone-frequency estimate, in ppm of ``f_hz``."""
    noise_total = 10.0 ** (-snr_rbw_db / 10.0) * fs / rbw_hz        # noise over fs, tone power = 1
    snr_ps = 1.0 / noise_total
    sigma_hz = math.sqrt(6.0 / (snr_ps * n * (float(n) ** 2 - 1.0))) * fs / (2.0 * math.pi)
    return sigma_hz / abs(f_hz) * 1e6


def estimate_ppm_cw(device: Any, f_ref_hz: float, offset_hz: float = 100e3, n: int = 2 ** 18, repeats: int = 5,
                    gain_db: float | None = None, *, channel: int = 0, min_snr_db: float = 15.0,
                    ref_clock_ppm: float | None = None, timeout_s: float = 2.0) -> PpmEstimate:
    """Calibrate the reference clock against a CW tone of known frequency ``f_ref_hz``.

    The device is tuned to ``fc = f_ref − offset_hz`` (tone away from the zero-IF DC
    spike), optionally set to manual ``gain_db``, and ``repeats`` settled captures of ``n``
    samples are analysed (:func:`estimate_ppm_from_samples` per capture, SNR from a
    4096-point Welch spectrum). The hardware correction in effect is taken into account
    (``meta['ppm_hw']``): the returned :attr:`PpmEstimate.ppm` is the TOTAL error relative to
    the nominal reference, :attr:`PpmEstimate.ppm_residual` what was measured on top of it.
    The device's fc / gain / gain mode / stream state are restored afterwards.

    The reference must be far more accurate than the TCXO (GPSDO/10 MHz-locked generator);
    a second Pluto's TX is *not* a reference (its TCXO is also ±25 ppm).

    Raises :class:`CalibrationError` (Turkish, with hints) if the tone is not found, the
    capture clips or the SNR is below ``min_snr_db``.
    """
    f_ref_hz = _finite(f_ref_hz, "f_ref_hz")
    offset_hz = _finite(offset_hz, "offset_hz")
    n = _positive_int(n, "n", 64)
    repeats = _positive_int(repeats, "repeats")
    channel = int(channel)
    if not 0 <= channel < int(device.num_channels):
        raise ConfigError(f"{device.name}: kanal {channel} yok ({device.num_channels} kanal)")
    min_snr_db = _finite(min_snr_db, "min_snr_db")
    ref_ppm = float(device.profile.ref_clock_ppm) if ref_clock_ppm is None else _finite(ref_clock_ppm, "ref_clock_ppm")
    fs = float(device.fs)
    if not fs > 0:
        raise ConfigError(f"{device.name}: örnekleme hızı ayarlanmamış")
    fc = f_ref_hz - offset_hz
    t0 = now_ns()
    tot: list[float] = []
    res: list[float] = []
    f_apps: list[float] = []
    snrs: list[float] = []
    rbw = math.nan
    with preserve_rx_settings(device):
        device.set_center_freq(fc)
        if gain_db is not None:
            device.set_gain_mode("manual")
            device.set_gain(_finite(gain_db, "gain_db"))
        temperature = device_temperature_c(device, channel)
        ppm_hw = channel_ppm_hw(device, channel)
        half = search_halfwidth_hz(f_ref_hz, ref_ppm, n, fs, ppm_hw)
        for i in range(repeats):
            blk = device.capture(n, timeout_s)
            x = blk.samples[channel]
            lv = iq_levels(x, blk.clip_level)
            if lv.clip_fraction > MAX_CLIP_FRACTION:
                raise CalibrationError(
                    f"{device.name}: yakalamada kırpma (%{100 * lv.clip_fraction:.3f} örnek, tepe "
                    f"{lv.peak_dbfs:.1f} dBFS): kazancı düşürün ya da referans seviyesini azaltın.")
            hw = channel_ppm_hw(device, channel, blk)
            f_app = _locate_reference(x, blk.fs, blk.fc, f_ref_hz, half)
            snr, _, rbw = tone_snr_db(x, blk.fs, f_app - blk.fc)
            if snr < min_snr_db:
                raise CalibrationError(
                    f"{device.name}: referans ton SNR {snr:.1f} dB < {min_snr_db:.0f} dB (RBW {fmt_hz(rbw)}). "
                    "Kazancı artırın, referans seviyesini yükseltin ya da kablo/anten bağlantısını kontrol edin.")
            r = ppm_from_apparent(f_ref_hz, f_app)
            res.append(r)
            tot.append(compose_ppm(hw, r))
            f_apps.append(f_app)
            snrs.append(snr)
            log.debug("%s: ppm tekrar %d: f_app=%.3f Hz artık=%+.5f toplam=%+.5f SNR=%.1f dB",
                      device.name, i, f_app, r, tot[-1], snr)
            ppm_hw = hw
        gains = list(device.gains)
    arr = np.asarray(tot)
    if repeats >= 2:
        std = float(np.std(arr, ddof=1))
    else:
        std = _crb_std_ppm(snrs[0], rbw, fs, n, f_ref_hz)
    est = PpmEstimate(
        ppm=float(np.mean(arr)), std_ppm=std, f_measured_hz=float(np.mean(f_apps)), f_ref_hz=f_ref_hz,
        snr_db=float(np.min(snrs)), n=n, method="cw", temperature_c=temperature, t_ns=t0,
        fc_nominal_hz=fc, fs_hz=fs, ppm_residual=float(np.mean(res)), ppm_hw=ppm_hw, repeats=repeats,
        extra={"ppm_values": tot, "residual_values": res, "rbw_hz": rbw, "offset_hz": offset_hz,
               "gain_db": gains, "channel": channel, "device": device.name,
               "serial": getattr(device.info, "serial", ""),
               "snr_definition": "RBW (4096 nokta Blackman-Harris Welch)",
               "std_definition": "tekrarlar arası örnek std" if repeats >= 2 else "Cramér-Rao alt sınırı"},
    )
    log.info("%s: %s", device.name, est.summary_tr())
    return est
