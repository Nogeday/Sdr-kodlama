"""Two-element phase interferometer (phase 0: 2× Pluto on a shared reference).

Geometry and convention (docs/ARCHITECTURE.md section 2, contracts df.bearing)::

        broadside (+y), az = 0°
              ^
              |   / source at azimuth az (positive toward +x, i.e. toward element 1)
              |  /
     elem 0 ●-+---------● elem 1        +x
            x = 0        x = +d

    Δφ = arg(x1 · conj(x0)) − φ_cal = 2π · d · sin(az) / λ
    az = asin(Δφ · λ / (2π d))

* **Front/back ambiguity.** A linear two-element array cannot tell ``az`` from
  ``180° − az`` (both give the same ``sin``); bearings are reported in [−90°, 90°], i.e.
  the half-plane in front of the array (+y). Resolving it needs a third element or a
  directional antenna.
* **Grating lobes.** For ``d > λ/2`` several azimuths explain the same wrapped phase
  (``Δφ + 2πk``); the principal one (``k = 0``) is ``az_deg``, the others are listed in
  ``ambiguous_deg``. ``d ≤ λ/2`` (0.15 m ⇒ f ≤ 999 MHz) is unambiguous.
* The error grows as ``1/cos(az)`` toward endfire: KTR sweeps stay within ±60°.

Calibration (:meth:`Interferometer2.calibrate`): a common signal from a known direction
(cabled through a splitter = no geometric phase, ``true_az_deg = 0``; or an over-the-air
source at a known azimuth) gives ``φ_cal = arg(x1·conj(x0)) − 2π d sin(az_true)/λ``. With a
wideband calibration signal the sample delay between the two streams is estimated as well
and compensated before every phase measurement; with a narrowband (CW) pilot the delay
cannot be resolved and its phase effect ``2π · f_bb · delay / fs`` is absorbed into
``φ_cal`` — exact only at the pilot's baseband frequency (calibrate at the frequency of
interest, or use a noise source).
"""

from __future__ import annotations

import logging
import math
from dataclasses import dataclass, field
from typing import Any

import numpy as np

from ..core.errors import ConfigError, MeasurementError
from ..core.units import C_LIGHT, wrap_deg, wrap_rad
from ..dsp.windows import get_window
from .coherence import _pair, align, estimate_delay_ex, valid_slice

log = logging.getLogger(__name__)

#: Default band-mask width in FFT bins when only ``f_bb_hz`` is given.
DEFAULT_MASK_BINS: int = 32


def _clip01(v: float) -> float:
    return float(min(max(v, 0.0), 1.0)) if math.isfinite(v) else 0.0


def _is_finite(v: Any) -> bool:
    try:
        return math.isfinite(float(v))
    except (TypeError, ValueError):
        return False


def _checked_pair(x0: Any, x1: Any, what: str) -> tuple[np.ndarray, np.ndarray]:
    """:func:`~eds.df.coherence._pair` with the operation named in the error (non-finite
    samples — e.g. a corrupt block — would otherwise give a NaN bearing)."""
    try:
        return _pair(x0, x1)
    except MeasurementError as e:
        raise MeasurementError(f"{what} hesaplanamaz: {e} (bozuk blok: overflow / dönüşüm hatası?)") from e


@dataclass
class Bearing:
    """One interferometer bearing (degrees; see module docstring for conventions).

    az_deg          principal azimuth in [−90, 90] (front half-plane)
    ambiguous_deg   other azimuths consistent with the phase (grating lobes, d > λ/2)
    phase_deg       calibrated phase difference ``arg(x1 x0*) − φ_cal`` (wrapped)
    coherence       normalized coherence of the two channels in the analysed band
    quality         0..1 = coherence × (sin saturation penalty) / number of solutions
    f_hz            RF frequency used for λ
    calibrated      phase calibration applied
    sin_az          unclipped ``Δφ λ / (2π d)`` of the principal solution (|·| > 1 = noise
                    or calibration error)
    d_over_lambda   baseline in wavelengths
    """

    az_deg: float
    ambiguous_deg: list[float] = field(default_factory=list)
    phase_deg: float = 0.0
    coherence: float = 0.0
    quality: float = 0.0
    f_hz: float = 0.0
    calibrated: bool = False
    sin_az: float = 0.0
    d_over_lambda: float = 0.0

    @property
    def ambiguous(self) -> list[float]:
        """Alias of :attr:`ambiguous_deg` (ARCHITECTURE §3.6 name)."""
        return self.ambiguous_deg

    @property
    def solutions_deg(self) -> list[float]:
        """Every consistent azimuth (principal + ambiguous), ascending."""
        return sorted([self.az_deg, *self.ambiguous_deg])

    def to_dict(self) -> dict[str, Any]:
        return {"az_deg": self.az_deg, "ambiguous_deg": list(self.ambiguous_deg), "phase_deg": self.phase_deg,
                "coherence": self.coherence, "quality": self.quality, "f_hz": self.f_hz,
                "calibrated": self.calibrated, "sin_az": self.sin_az, "d_over_lambda": self.d_over_lambda}

    def to_bus_payload(self, array_name: str, t_acq_ns: int | None = None,
                       method: str = "interferometer2") -> dict[str, Any]:
        """``df.bearing`` payload (contracts/schemas/df.bearing.schema.json).

        Raises :class:`MeasurementError` when ``az_deg``, ``phase_deg`` or an
        ``ambiguous_deg`` entry is not finite: the schema requires numbers there and the bus
        would turn NaN into ``null`` (non-finite ``coherence`` / ``quality`` map to 0).
        """
        bad = [k for k, v in (("az_deg", self.az_deg), ("phase_deg", self.phase_deg)) if not _is_finite(v)]
        if not all(_is_finite(a) for a in self.ambiguous_deg):
            bad.append("ambiguous_deg")
        if bad:
            raise MeasurementError(f"Kerteriz yayınlanamaz: {', '.join(bad)} sonlu bir sayı değil (NaN/sonsuz); "
                                   "df.bearing şeması sayı gerektirir")
        if not isinstance(array_name, str) or not array_name:
            raise ConfigError("Dizi adı (array_name) boş olmayan bir metin olmalı")
        if not isinstance(method, str) or not method:
            raise ConfigError("Yöntem (method) boş olmayan bir metin olmalı")
        if not (math.isfinite(self.f_hz) and self.f_hz > 0):
            raise ConfigError(f"Kerteriz frekansı pozitif olmalı, {self.f_hz!r}")
        data: dict[str, Any] = {
            "array": array_name, "method": method, "f_hz": float(self.f_hz),
            "az_deg": float(wrap_deg(self.az_deg)),
            "ambiguous_deg": [float(wrap_deg(a)) for a in self.ambiguous_deg],
            "phase_deg": float(wrap_deg(self.phase_deg)),
            "coherence": _clip01(self.coherence), "quality": _clip01(self.quality),
            "calibrated": bool(self.calibrated),
        }
        if t_acq_ns is not None:
            if isinstance(t_acq_ns, bool) or int(t_acq_ns) < 0:
                raise ConfigError(f"t_acq_ns negatif olmayan bir tamsayı olmalı, {t_acq_ns!r}")
            data["t_acq_ns"] = int(t_acq_ns)
        return data


class Interferometer2:
    """Two-element interferometer, element 0 at x = 0, element 1 at x = +``baseline_m``.

    ``phase_cal_rad`` is subtracted from every measured phase; ``delay_cal_samples`` (the
    stream delay ``x1[n] ≈ x0[n − d]``) is compensated before the phase is measured.
    ``calibrated`` defaults to True when either calibration value is non-zero.
    """

    def __init__(self, baseline_m: float, phase_cal_rad: float = 0.0, delay_cal_samples: float = 0.0, *,
                 calibrated: bool | None = None):
        d = float(baseline_m)
        if not (math.isfinite(d) and d > 0):
            raise ConfigError(f"Taban uzunluğu (baseline_m) pozitif olmalı, {baseline_m!r} geldi")
        self.baseline_m = d
        self.phase_cal_rad = float(wrap_rad(float(phase_cal_rad)))
        self.delay_cal_samples = float(delay_cal_samples)
        if not (math.isfinite(self.phase_cal_rad) and math.isfinite(self.delay_cal_samples)):
            raise ConfigError("phase_cal_rad ve delay_cal_samples sonlu olmalı")
        self.calibrated = bool(calibrated) if calibrated is not None else \
            (self.phase_cal_rad != 0.0 or self.delay_cal_samples != 0.0)
        self.last_cal: dict[str, Any] = {}

    # ------------------------------------------------------------------ geometry
    @property
    def phase_cal_deg(self) -> float:
        return math.degrees(self.phase_cal_rad)

    def d_over_lambda(self, f_hz: float) -> float:
        f = float(f_hz)
        if not (math.isfinite(f) and f > 0):
            raise ConfigError(f"Frekans pozitif olmalı, {f_hz!r} geldi")
        return self.baseline_m * f / C_LIGHT

    @property
    def max_unambiguous_hz(self) -> float:
        """Highest frequency with ``d ≤ λ/2`` (no grating lobes)."""
        return C_LIGHT / (2.0 * self.baseline_m)

    def geometric_phase_rad(self, az_deg: float, f_hz: float) -> float:
        """``2π d sin(az) / λ`` (unwrapped)."""
        return 2.0 * math.pi * self.d_over_lambda(f_hz) * math.sin(math.radians(float(az_deg)))

    def solutions_deg(self, dphi_rad: float, f_hz: float) -> list[float]:
        """Every azimuth in [−90°, 90°] with ``2π d sin(az)/λ ≡ dphi (mod 2π)``, ascending."""
        dl = self.d_over_lambda(f_hz)
        sols = []
        kmax = int(math.ceil(dl)) + 1
        for k in range(-kmax, kmax + 1):
            s = (float(dphi_rad) + 2.0 * math.pi * k) / (2.0 * math.pi * dl)
            if -1.0 <= s <= 1.0:
                sols.append(math.degrees(math.asin(s)))
        return sorted(sols)

    # ------------------------------------------------------------------ measurement
    def _phase(self, x0: Any, x1: Any, fs: float | None, f_bb_hz: float | None,
               bw_hz: float | None, delay: float) -> tuple[float, float]:
        """``(arg(x1·conj(x0)), coherence)`` after delay compensation and band masking."""
        a, b = _pair(x0, x1)
        if delay != 0.0:
            b = align(b, delay)
            sl = valid_slice(a.size, delay)
            a, b = a[sl], b[sl]
        if f_bb_hz is None:
            if bw_hz is not None:
                raise ConfigError("bw_hz verildiğinde f_bb_hz (bant merkezi) de verilmeli")
            s = complex(np.vdot(a, b))
            e = float(np.vdot(a, a).real) * float(np.vdot(b, b).real)
        else:
            if fs is None:
                raise ConfigError("Bant maskesi (f_bb_hz) için fs gerekli")
            fs = float(fs)
            n = a.size
            bw = DEFAULT_MASK_BINS * fs / n if bw_hz is None else float(bw_hz)
            if not (math.isfinite(bw) and bw > 0):
                raise ConfigError(f"bw_hz pozitif olmalı, {bw_hz!r} geldi")
            w = np.asarray(get_window("blackmanharris", n), dtype=np.float64)
            fa = np.fft.fft(a * w)
            fb = np.fft.fft(b * w)
            mask = np.abs(np.fft.fftfreq(n, d=1.0 / fs) - float(f_bb_hz)) <= bw / 2.0
            if not np.any(mask):
                raise MeasurementError(f"Bant maskesi ({f_bb_hz:.6g} ± {bw / 2:.6g} Hz) hiçbir FFT binini içermiyor")
            fa, fb = fa[mask], fb[mask]
            s = complex(np.vdot(fa, fb))
            e = float(np.vdot(fa, fa).real) * float(np.vdot(fb, fb).real)
        if e <= 0.0:
            raise MeasurementError("Kerteriz: kanallardan birinde (bantta) sinyal yok")
        return float(np.angle(s)), float(min(abs(s) / math.sqrt(e), 1.0))

    def measure_phase(self, x0: Any, x1: Any, *, fs: float | None = None, f_bb_hz: float | None = None,
                      bw_hz: float | None = None) -> tuple[float, float]:
        """Raw (uncalibrated) ``(phase_rad, coherence)`` with the stored delay compensated."""
        return self._phase(x0, x1, fs, f_bb_hz, bw_hz, self.delay_cal_samples)

    def calibrate(self, x0: Any, x1: Any, f_hz: float, true_az_deg: float = 0.0, *, fs: float | None = None,
                  f_bb_hz: float | None = None, bw_hz: float | None = None,
                  estimate_delay: bool = True) -> dict[str, Any]:
        """Set ``phase_cal`` (and ``delay_cal`` when resolvable) from a reference capture.

        ``true_az_deg = 0`` for a cabled/splitter reference (no geometric phase). With
        ``estimate_delay`` the stream delay is estimated on the (band-masked) reference and
        adopted only if the reference is wideband enough (:class:`DelayEstimate.resolvable`);
        otherwise the previous ``delay_cal_samples`` is kept. Returns a dict for reports /
        the ``cal.result`` coherence payload.
        """
        f_hz = float(f_hz)
        a, b = _checked_pair(x0, x1, "Interferometre kalibrasyonu")
        resolvable = False
        peak = math.nan
        if estimate_delay:
            da, db = a, b
            if f_bb_hz is not None:
                if fs is None:
                    raise ConfigError("Bant maskesi (f_bb_hz) için fs gerekli")
                n = a.size
                bw = DEFAULT_MASK_BINS * float(fs) / n if bw_hz is None else float(bw_hz)
                mask = np.abs(np.fft.fftfreq(n, d=1.0 / float(fs)) - float(f_bb_hz)) <= bw / 2.0
                da = np.fft.ifft(np.fft.fft(a) * mask)
                db = np.fft.ifft(np.fft.fft(b) * mask)
            de = estimate_delay_ex(da, db, fs)
            resolvable, peak = de.resolvable, de.peak_corr
            if de.resolvable:
                self.delay_cal_samples = float(de.delay_samples)
            else:
                log.info("Interferometre kalibrasyonu: gecikme dar bantlı referansla çözülemedi; "
                         "delay_cal = %.3f örnek korunuyor", self.delay_cal_samples)
        phi, coh = self._phase(a, b, fs, f_bb_hz, bw_hz, self.delay_cal_samples)
        self.phase_cal_rad = float(wrap_rad(phi - self.geometric_phase_rad(true_az_deg, f_hz)))
        self.calibrated = True
        self.last_cal = {
            "phase_cal_deg": self.phase_cal_deg, "delay_cal_samples": self.delay_cal_samples,
            "delay_resolvable": bool(resolvable), "delay_peak_corr": peak, "coherence": coh, "f_hz": f_hz,
            "true_az_deg": float(true_az_deg), "f_bb_hz": f_bb_hz, "baseline_m": self.baseline_m,
        }
        if coh < 0.9:
            log.warning("Interferometre kalibrasyonu düşük koherensle yapıldı (%.3f): referans sinyali zayıf/ortak değil",
                        coh)
        return dict(self.last_cal)

    def bearing(self, x0: Any, x1: Any, f_hz: float, f_bb_hz: float | None = None, bw_hz: float | None = None,
                *, fs: float | None = None) -> Bearing:
        """Bearing of the signal common to ``x0`` / ``x1`` (optionally isolated by an FFT band
        mask ``f_bb_hz ± bw_hz/2``, baseband Hz, which needs ``fs``; default width
        :data:`DEFAULT_MASK_BINS` bins). Raises :class:`MeasurementError` for non-finite
        input samples or a non-finite phase (e.g. overflowing sample values)."""
        dl = self.d_over_lambda(f_hz)
        a, b = _checked_pair(x0, x1, "Kerteriz")
        phi, coh = self._phase(a, b, fs, f_bb_hz, bw_hz, self.delay_cal_samples)
        if not (math.isfinite(phi) and math.isfinite(coh)):
            raise MeasurementError("Kerteriz: faz farkı/koherens sonlu değil (örnek değerleri taşıyor mu?)")
        dphi = float(wrap_rad(phi - self.phase_cal_rad))
        s0 = dphi / (2.0 * math.pi * dl)
        # principal solution (k = 0); |s0| > 1 is only possible for d <= λ/2 (noise / cal error)
        az = math.degrees(math.asin(min(max(s0, -1.0), 1.0)))
        others = [] if dl <= 0.5 else [s for s in self.solutions_deg(dphi, f_hz) if abs(s - az) > 1e-9]
        sat = 1.0 if abs(s0) <= 1.0 else max(0.0, 1.0 - 5.0 * (abs(s0) - 1.0))
        quality = coh * sat / max(1, 1 + len(others))
        return Bearing(az_deg=az, ambiguous_deg=others, phase_deg=math.degrees(dphi), coherence=coh,
                       quality=quality, f_hz=float(f_hz), calibrated=self.calibrated, sin_az=s0, d_over_lambda=dl)
