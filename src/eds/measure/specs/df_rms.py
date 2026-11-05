"""DF RMS hatası [°] — two-element interferometer bearing error over known angles.

Definition (docs/MEASUREMENTS.md section 6), after the coherence check and the phase
calibration::

    RMS = sqrt( (1/M) · Σ wrap(θ̂_i − θ_i)² )        wrap: (−180°, 180°]
    Δφ  = 2π · d · sin(θ) / λ + φ_cal  ⇒  θ̂ = asin( λ · (Δφ − φ_cal) / (2π · d) )

Array: :class:`~eds.hal.group.DeviceGroup` of the first two devices of the session (2×
Pluto) or, with one device, the DUT itself when it has ≥ 2 coherent channels (KrakenSDR).
Both channels run at the same fc / fs / manual gain. The group stream is started once and
kept running until the end, so the sample alignment of the members (lock-step reads)
survives from the calibration to the last angle; while the operator works (hardware
prompts) the stream is drained in the background. Any overflow (alignment lost) triggers a
new phase calibration before the next capture is used.

Steps
-----
1. **Coherence.** A common signal at ``fc + pilot_offset_frac·fs`` reaches both channels
   (simulator: a ``cabled`` emitter added to the scene; hardware: the operator connects the
   splitter-fed calibration source). :func:`eds.df.coherence.check_coherence` measures the
   phase drift, coherence and phase stability. Not coherent → the measurement continues but
   the summary is flagged ``KOHERENS YOK - ortak 40 MHz referans gerekli``.
2. **Phase calibration.** The cabled signal is moved to the target frequency
   ``fc + target_offset_frac·fs`` and :meth:`Interferometer2.calibrate` sets ``φ_cal``
   (true azimuth 0°). Calibrating at the frequency of the measurement removes the phase
   ``2π · f_bb · d / fs`` of an unknown stream delay ``d``, which a CW pilot cannot resolve
   (a pilot at another baseband frequency would leave ``2π · Δf_bb · d / fs`` in every
   bearing). The difference between the pilot phase (step 1) and ``φ_cal`` is reported as a
   delay indicator. The cabled signal is then switched off.
3. **Angles.** For every true angle (default −60…+60° in 10° steps, ±90° is avoided
   because the error grows as 1/cos θ) an over-the-air tone at the target frequency arrives
   from that azimuth (simulator: emitter with ``cabled = False`` and ``aoa_deg``; hardware:
   the operator rotates the array on the turntable). ``repeats`` captures of ``n`` samples →
   :meth:`Interferometer2.bearing` with an FFT band mask around the tone (its baseband
   frequency is located in each capture, so a TCXO offset never pushes it out of the mask).
4. :func:`eds.df.metrics.summarize` → value = RMS; bias, std, max |error| as extras. The
   uncertainty of the RMS treats the M angles as the independent units (the K repeats at one
   angle share that angle's bias): ``u = std_m(mean_k err²) / (2·RMS·√M)``.

SNR. Every SNR is reported in a STATED bandwidth ``snr_bw_hz`` (default 25 kHz, a narrow-band
channel) besides the per-capture FFT-bin value. On the simulator the over-the-air tone is set
to ``sim_snr_db`` in that bandwidth (default 10 dB → about −9 dB per sample at 2 MS/s; the
level is found from a reference capture), the cabled calibration signal stays strong
(``sim_cal_power_dbm``) as on a real bench, and — outside ``--quick`` — the angle scan is
repeated for every ``snr_sweep_db`` value (RMS–SNR curve). The simulator models ONLY thermal
noise: no multipath, no antenna mutual coupling / element patterns, no antenna/cable phase
mismatch after calibration and no LO phase drift. Its RMS is therefore a lower bound of a
real array's error; the result says so and carries a blocker (no target verdict). Channels
that are not coherent also block the verdict.
"""

from __future__ import annotations

import logging
import math
import threading
import uuid
from abc import ABC, abstractmethod
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field
from typing import Any

import numpy as np

from ...core.errors import ConfigError, EDSError, MeasurementError
from ...core.units import fmt_hz, wavelength, wrap_deg
from ...df.coherence import CoherenceReport, check_coherence
from ...df.interferometer import DEFAULT_MASK_BINS, Bearing, Interferometer2
from ...df.metrics import angle_errors, summarize
from ...dsp.psd import welch
from ...dsp.windows import get_window, window_info
from ..base import FigureSpec, Measurement, MeasurementResult, Series, register
from ..common import base_conditions, spectrum_screenshot
from ..targets import REASON_NO_COHERENCE

log = logging.getLogger(__name__)

NO_COHERENCE_FLAG = "KOHERENS YOK - ortak 40 MHz referans gerekli"
SIM_FLAG = ("SİMÜLASYON: yalnız termal gürültü modellendi — çok yollu yayılım, anten karşılıklı kuplajı/eleman "
            "örüntüleri, anten/kablo faz uyumsuzluğu ve LO faz kayması yok; değer gerçek bir dizinin hatasının alt "
            "sınırıdır")
BLOCKER_SIM = "simülasyon: yalnız termal gürültü (çok yollu yayılım, kuplaj, kablo/LO faz kayması modellenmedi)"
#: The tone search half-width never exceeds this fraction of fs (pilot, target, DC and
#: images stay separated for offsets that differ by ≥ 0.1·fs).
MAX_SEARCH_FRACTION = 0.05
#: Extra search margin around the TCXO tolerance, in FFT bins.
SEARCH_MARGIN_BINS = 8
#: Largest |angle| accepted (the interferometer error grows as 1/cos θ toward endfire).
MAX_ABS_ANGLE_DEG = 85.0
_KEEPALIVE_JOIN_MARGIN_S = 5.0


# ============================================================================ helpers
@contextmanager
def keep_streaming(dev: Any, timeout_s: float) -> Iterator[None]:
    """Read and discard ``dev`` blocks in a background thread while the caller waits.

    Used around operator prompts: an idle stream would overflow the device FIFOs and lose
    the members' sample alignment, whereas a :class:`~eds.hal.group.DeviceGroup` reads its
    members in lock-step, so discarding whole group blocks keeps it. No-op when the device
    is not streaming. A stream error in the background is raised after the wait.
    """
    if not dev.streaming:
        yield
        return
    stop = threading.Event()
    errors: list[EDSError] = []

    def drain() -> None:
        while not stop.is_set():
            try:
                dev.read(dev.buffer_size, timeout_s)
            except EDSError as e:          # handed to the caller below
                errors.append(e)
                return

    th = threading.Thread(target=drain, name=f"eds-df-keepalive-{dev.name}", daemon=True)
    th.start()
    try:
        yield
    finally:
        stop.set()
        th.join(timeout_s + _KEEPALIVE_JOIN_MARGIN_S)
    if th.is_alive():
        raise MeasurementError(f"{dev.name}: arka plan okuması {timeout_s + _KEEPALIVE_JOIN_MARGIN_S:.0f} s içinde "
                               "durmadı (sürücü takıldı?)")
    if errors:
        raise MeasurementError(f"{dev.name}: operatör beklenirken akış hatası: {errors[0]}") from errors[0]


def locate_tone(x: np.ndarray, fs: float, f_bb_nominal: float, search_hz: float) -> tuple[float, float]:
    """Baseband frequency (FFT bin centre, Hz) of the strongest component within
    ``f_bb_nominal ± search_hz`` and its SNR (dB) in the RBW ``ENBW · fs / n``.

    Blackman-Harris window; the noise per bin is the spectrum median / ln 2 (mean of the
    exponentially distributed noise bins)."""
    n = int(x.size)
    w = get_window("blackmanharris", n)
    p = np.abs(np.fft.fft(np.asarray(x) * w)) ** 2
    f = np.fft.fftfreq(n, d=1.0 / fs)
    sel = np.flatnonzero(np.abs(f - f_bb_nominal) <= search_hz)
    if sel.size == 0:
        raise MeasurementError(f"Ton araması ({f_bb_nominal:.6g} ± {search_hz:.6g} Hz) hiçbir FFT binini içermiyor")
    k = int(sel[np.argmax(p[sel])])
    noise = float(np.median(p)) / math.log(2.0)
    snr = 10.0 * math.log10(max(float(p[k]), 1e-300) / max(noise, 1e-300))
    return float(f[k]), snr


class Bench(ABC):
    """Stimulus and geometry of the DF bench (cabled calibration signal / over-the-air tone)."""

    kind: str = "abstract"

    @abstractmethod
    def cabled_on(self, f_hz: float) -> None:
        """Common (splitter-fed) signal at ``f_hz`` on both channels."""

    @abstractmethod
    def cabled_off(self) -> None: ...

    @abstractmethod
    def ota_on(self, f_hz: float, az_deg: float) -> None:
        """Over-the-air tone at ``f_hz`` arriving from azimuth ``az_deg``."""

    @abstractmethod
    def ota_off(self) -> None: ...

    def set_ota_power(self, dbm: float) -> None:
        """Level of the over-the-air tone (simulator only)."""
        raise MeasurementError(f"{self.kind}: havadan ton seviyesi bu tezgahta ayarlanamaz")

    @abstractmethod
    def close(self) -> None:
        """Switch everything off / restore the scene."""

    def describe(self) -> str:
        return self.kind


class SimBench(Bench):
    """Simulator bench: a quiet scene (every existing emitter disabled and restored on
    :meth:`close`) plus two private emitters, a ``cabled`` pilot and an over-the-air tone."""

    kind = "sim"

    def __init__(self, world: Any, power_dbm: float, ota_power_dbm: float | None = None):
        from ...hal.sim.world import Emitter

        self._world = world
        self._saved = {e.name: e.enabled for e in world.emitters}
        tag = uuid.uuid4().hex[:8]
        self.pilot = f"df_rms_pilot_{tag}"
        self.ota = f"df_rms_ota_{tag}"
        self.ota_power_dbm = float(power_dbm if ota_power_dbm is None else ota_power_dbm)
        if self._saved:
            world.set_enabled_many(list(self._saved), False)
        world.replace_emitters(add=[
            Emitter(name=self.pilot, freq_hz=1e9, power_dbm=power_dbm, kind="cw", cabled=True, enabled=False),
            Emitter(name=self.ota, freq_hz=1e9, power_dbm=self.ota_power_dbm, kind="cw", cabled=False,
                    enabled=False),
        ])

    def set_ota_power(self, dbm: float) -> None:
        self.ota_power_dbm = float(dbm)
        self._world.update(self.ota, power_dbm=self.ota_power_dbm)

    def cabled_on(self, f_hz: float) -> None:
        self._world.update(self.pilot, freq_hz=float(f_hz), enabled=True)

    def cabled_off(self) -> None:
        self._world.update(self.pilot, enabled=False)

    def ota_on(self, f_hz: float, az_deg: float) -> None:
        self._world.update(self.ota, freq_hz=float(f_hz), aoa_deg=float(az_deg), enabled=True)

    def ota_off(self) -> None:
        self._world.update(self.ota, enabled=False)

    def close(self) -> None:
        self._world.replace_emitters(remove=[self.pilot, self.ota])
        for name, on in self._saved.items():
            if self._world.get(name) is not None:
                self._world.set_enabled(name, on)

    def describe(self) -> str:
        return "sim (kablolu pilot + havadan ton, sessiz sahne)"


class HardwareBench(Bench):
    """Real bench: the operator re-cables (splitter ↔ antennas) and turns the array; the
    tone comes from ``ctx.source`` when present, otherwise the operator sets a generator.
    The stream is drained while the operator works (:func:`keep_streaming`)."""

    kind = "hardware"

    def __init__(self, ctx: Any, dev: Any, power_dbm: float, baseline_m: float, timeout_s: float):
        self._ctx = ctx
        self._dev = dev
        self._src = ctx.source
        self._power = float(power_dbm)
        self._baseline = float(baseline_m)
        self._timeout = float(timeout_s)
        self._setup: str | None = None
        self._az: float | None = None

    def _prompt(self, msg: str) -> None:
        with keep_streaming(self._dev, self._timeout):
            self._ctx.prompt(msg)

    def _tone(self, f_hz: float | None) -> None:
        if self._src is not None:
            with keep_streaming(self._dev, self._timeout):
                if f_hz is None:
                    self._src.set_enabled(False)
                else:
                    self._src.set_frequency(f_hz)
                    self._src.set_power(self._power)
                    self._src.set_enabled(True)
        elif f_hz is None:
            self._prompt("Sinyal üretecinin RF çıkışını kapatın, ardından Enter.")
        else:
            self._prompt(f"Sinyal üretecini {fmt_hz(f_hz, 10)} CW olarak açın (seviye: kırpmasız, yüksek SNR), "
                         "ardından Enter.")

    def cabled_on(self, f_hz: float) -> None:
        if self._setup != "cabled":
            self._prompt("DF kalibrasyonu: iki RX girişini EŞİT BOYLU, aynı tip kablolarla 2'li güç bölücünün "
                         "çıkışlarına bağlayın; bölücü girişine kalibrasyon kaynağını bağlayın (ortak 40 MHz "
                         "referans takılı olmalı). Hazır olunca Enter.")
            self._setup, self._az = "cabled", None
        self._tone(f_hz)

    def cabled_off(self) -> None:
        self._tone(None)

    def ota_on(self, f_hz: float, az_deg: float) -> None:
        if self._setup != "ota":
            self._prompt(f"Havadan ölçüm: bölücüyü çıkarıp antenleri (eleman aralığı d = {self._baseline:.3f} m) "
                         "bağlayın; verici antenini uzak alana, dizinin broadside (0°) yönüne yerleştirin. Hazır olunca "
                         "Enter.")
            self._setup, self._az = "ota", None
        if self._az is None or abs(self._az - az_deg) > 1e-9:
            self._prompt(f"Diziyi (döner tabla) {az_deg:+.1f}° konumuna getirin (broadside = 0°, rx1 yönü pozitif). "
                         "Hazır olunca Enter.")
            self._az = float(az_deg)
        self._tone(f_hz)

    def ota_off(self) -> None:
        self._tone(None)

    def close(self) -> None:
        if self._src is not None:
            self._src.set_enabled(False)

    def describe(self) -> str:
        return f"donanım ({getattr(self._src, 'name', 'üreteç elle')})"


@dataclass
class AngleResult:
    true_deg: float
    bearings: list[Bearing] = field(default_factory=list)
    snr_db: list[float] = field(default_factory=list)

    @property
    def est_deg(self) -> np.ndarray:
        return np.array([b.az_deg for b in self.bearings], dtype=np.float64)

    def row(self) -> dict[str, Any]:
        est = self.est_deg
        err = angle_errors(est, self.true_deg)
        return {
            "true_deg": self.true_deg, "mean_deg": float(np.mean(est)), "std_deg": float(np.std(est)),
            "error_deg": float(wrap_deg(float(np.mean(est)) - self.true_deg)),
            "rms_deg": float(math.sqrt(float(np.mean(err * err)))), "max_abs_deg": float(np.max(np.abs(err))),
            "n": int(est.size), "phase_deg": float(np.mean([b.phase_deg for b in self.bearings])),
            "coherence": float(np.mean([b.coherence for b in self.bearings])),
            "quality": float(np.mean([b.quality for b in self.bearings])),
            "snr_db": float(np.mean(self.snr_db)), "estimates_deg": [float(v) for v in est],
        }


def fmt_deg(v: float) -> str:
    """Degrees with 3 decimals below 0.1° (simulator-grade errors), else 2."""
    return f"{v:.3f}" if abs(v) < 0.1 else f"{v:.2f}"


def _num(v: Any, what: str, lo: float | None = None, hi: float | None = None) -> float:
    if isinstance(v, bool) or not isinstance(v, (int, float)) or not math.isfinite(float(v)):
        raise ConfigError(f"df_rms.{what} sonlu bir sayı olmalı, {v!r} geldi")
    x = float(v)
    if (lo is not None and x < lo) or (hi is not None and x > hi):
        raise ConfigError(f"df_rms.{what} = {x:g} izinli aralık dışında [{lo}, {hi}]")
    return x


def _int(v: Any, what: str, lo: int = 1) -> int:
    if isinstance(v, bool) or not isinstance(v, (int, float)) or int(v) != v or int(v) < lo:
        raise ConfigError(f"df_rms.{what} en az {lo} olan bir tamsayı olmalı, {v!r} geldi")
    return int(v)


# ============================================================================ measurement
@register
class DfRmsMeasurement(Measurement):
    spec_id = "df_rms"
    title = "DF RMS hatası"
    unit = "°"
    requires = frozenset({"two_channels"})
    default_params = {
        "baseline_m": None,            # None = λ/2 at fc
        "fc": None,                    # None = [rx] center_freq_hz
        "fs": 2e6,
        "gain_db": None,               # None = [rx] gain_db (same on every channel)
        "angles_deg": None,            # explicit list; None = grid below
        "angle_min_deg": -60.0,
        "angle_max_deg": 60.0,
        "angle_step_deg": 10.0,
        "quick_angle_step_deg": 20.0,
        "repeats": 5,
        "quick_repeats": 2,
        "n": 65536,
        "pilot_offset_frac": 0.1,      # coherence pilot at fc + 0.1·fs
        "target_offset_frac": 0.2,     # calibration + over-the-air tone at fc + 0.2·fs
        "coherence_blocks": 8,
        "snr_bw_hz": 25e3,             # bandwidth every reported SNR refers to (narrow-band channel)
        "sim_snr_db": 10.0,            # simulator: over-the-air tone SNR in snr_bw_hz (headline)
        "snr_sweep_db": [-10.0, 0.0, 10.0, 20.0, 30.0],   # simulator: RMS–SNR curve (in snr_bw_hz)
        "quick_snr_sweep_db": [],
        "sim_power_dbm": None,         # simulator: fixed over-the-air tone power (overrides sim_snr_db)
        "sim_cal_power_dbm": -60.0,    # simulator: cabled pilot / calibration power (strong, as on a bench)
        "source_power_dbm": -40.0,     # hardware: ctx.source level setting
        "max_recalibrations": 3,       # per angle, after alignment losses (overflow)
    }

    # ------------------------------------------------------------------ parameters
    def _params(self) -> dict[str, Any]:
        p, app = self.p, self.ctx.app
        quick = bool(self.ctx.quick)
        fc = _num(app.rx.center_freq_hz if p["fc"] is None else p["fc"], "fc", 1.0)
        fs = _num(p["fs"], "fs", 1.0)
        pilot_frac = _num(p["pilot_offset_frac"], "pilot_offset_frac", -0.45, 0.45)
        target_frac = _num(p["target_offset_frac"], "target_offset_frac", -0.45, 0.45)
        if abs(pilot_frac - target_frac) < 2 * MAX_SEARCH_FRACTION or min(abs(pilot_frac), abs(target_frac)) < \
                MAX_SEARCH_FRACTION or abs(pilot_frac + target_frac) < 2 * MAX_SEARCH_FRACTION:
            raise ConfigError("df_rms: pilot ve hedef ofsetleri birbirinden, DC'den ve birbirinin imajından en az "
                              f"{2 * MAX_SEARCH_FRACTION:g}·fs uzak olmalı")
        f_target = fc + target_frac * fs
        baseline = 0.5 * wavelength(fc) if p["baseline_m"] is None else _num(p["baseline_m"], "baseline_m", 1e-4)
        if p["angles_deg"] is not None:
            raw = p["angles_deg"]
            if not isinstance(raw, (list, tuple)) or not raw:
                raise ConfigError("df_rms.angles_deg boş olmayan bir liste olmalı")
            angles = [_num(a, "angles_deg[]", -MAX_ABS_ANGLE_DEG, MAX_ABS_ANGLE_DEG) for a in raw]
        else:
            a0 = _num(p["angle_min_deg"], "angle_min_deg", -MAX_ABS_ANGLE_DEG, MAX_ABS_ANGLE_DEG)
            a1 = _num(p["angle_max_deg"], "angle_max_deg", a0, MAX_ABS_ANGLE_DEG)
            step = _num(p["quick_angle_step_deg"] if quick else p["angle_step_deg"], "angle_step_deg", 1e-3)
            k = int(math.floor((a1 - a0) / step + 1e-9))
            angles = [a0 + i * step for i in range(k + 1)]
        repeats = _int(p["quick_repeats"] if quick else p["repeats"], "repeats")
        n = _int(p["n"], "n", 1024)
        gain = _num(app.rx.gain_db if p["gain_db"] is None else p["gain_db"], "gain_db")
        return {
            "fc": fc, "fs": fs, "f_pilot": fc + pilot_frac * fs, "f_target": f_target, "baseline_m": baseline,
            "angles": angles, "repeats": repeats, "n": n, "gain_db": gain,
            "coherence_blocks": _int(p["coherence_blocks"], "coherence_blocks", 3),
            "snr_bw_hz": _num(p["snr_bw_hz"], "snr_bw_hz", 1.0),
            "sim_snr_db": _num(p["sim_snr_db"], "sim_snr_db", -40.0, 80.0),
            "snr_sweep_db": self._sweep(p["quick_snr_sweep_db"] if quick else p["snr_sweep_db"]),
            "sim_power_dbm": None if p["sim_power_dbm"] is None else _num(p["sim_power_dbm"], "sim_power_dbm"),
            "sim_cal_power_dbm": _num(p["sim_cal_power_dbm"], "sim_cal_power_dbm"),
            "source_power_dbm": _num(p["source_power_dbm"], "source_power_dbm"),
            "max_recal": _int(p["max_recalibrations"], "max_recalibrations", 0),
            "timeout_s": max(float(app.rx.read_timeout_s), 4.0 * n / fs),
        }

    @staticmethod
    def _sweep(raw: Any) -> list[float]:
        if not isinstance(raw, (list, tuple)):
            raise ConfigError(f"df_rms.snr_sweep_db bir liste olmalı, {raw!r} geldi")
        return sorted({_num(v, "snr_sweep_db[]", -40.0, 80.0) for v in raw})

    # ------------------------------------------------------------------ array
    def _array(self) -> tuple[Any, bool, int]:
        """``(device, owns_group, second_channel)``."""
        devs = self.ctx.devices
        if len(devs) >= 2:
            from ...hal.group import DeviceGroup

            members = devs[:2]
            for m in members:
                m.stop_stream()
            return DeviceGroup(members), True, int(members[0].num_channels)
        dev = self.ctx.dut
        if dev.num_channels >= 2:
            return dev, False, 1
        raise MeasurementError("En az 2 RX kanalı gerekli (2× Pluto veya koherent çok kanallı cihaz)")

    @staticmethod
    def _setup(dev: Any, prm: dict[str, Any]) -> None:
        prof = dev.profile
        dev.stop_stream()
        dev.set_sample_rate(prm["fs"])
        dev.set_bandwidth(min(max(prm["fs"], prof.bw_min_hz), prof.bw_max_hz))
        dev.set_center_freq(prm["fc"])
        dev.set_gain_mode("manual")
        dev.set_gain(prm["gain_db"])

    def _geometry_warnings(self, prm: dict[str, Any]) -> list[str]:
        """Simulator ground truth: the baseline parameter must match the element positions."""
        if not self.ctx.is_sim or len(self.ctx.devices) < 2:
            return []
        pos = [getattr(d, "position_m", None) for d in self.ctx.devices[:2]]
        if any(p is None for p in pos):
            return []
        dx, dy = pos[1][0] - pos[0][0], pos[1][1] - pos[0][1]
        warns = []
        d_true = math.hypot(dx, dy)
        if abs(d_true - prm["baseline_m"]) > 1e-3:
            warns.append(f"baseline_m = {prm['baseline_m']:.4f} m, simülatör eleman konumlarından {d_true:.4f} m: "
                         "parametreyi konumlarla aynı yapın")
        if d_true > 0 and (abs(dy) > 1e-3 * d_true or dx < 0):
            warns.append("Simülatörde dizi ekseni rx0 → rx1 = +x olmalı (DF açı konvansiyonu)")
        return warns

    # ------------------------------------------------------------------ steps
    def _search_hz(self, dev: Any, prm: dict[str, Any]) -> float:
        tol = 1.2 * float(dev.profile.ref_clock_ppm) * 1e-6 * prm["f_target"]
        return min(tol + SEARCH_MARGIN_BINS * prm["fs"] / prm["n"], MAX_SEARCH_FRACTION * prm["fs"])

    def _calibrate(self, dev: Any, bench: Bench, interf: Interferometer2, prm: dict[str, Any],
                   chans: tuple[int, int], search: float) -> dict[str, Any]:
        """Phase calibration with the cabled signal AT the target frequency (true az 0°)."""
        a, b = chans
        bench.cabled_on(prm["f_target"])
        try:
            dev.mark_settling()
            blk = dev.capture(prm["n"], prm["timeout_s"])
            f_bb, snr = locate_tone(blk.samples[a], prm["fs"], prm["f_target"] - prm["fc"], search)
            info = interf.calibrate(blk.samples[a], blk.samples[b], prm["f_target"], 0.0, fs=prm["fs"], f_bb_hz=f_bb)
        finally:
            bench.cabled_off()
        info.update({"snr_db": snr, "f_bb_hz": f_bb, "overflows_ref": int(dev.stats.overflows)})
        log.info("df_rms: faz kalibrasyonu φ_cal = %+.2f° (koherens %.4f, SNR %.1f dB)", info["phase_cal_deg"],
                 info["coherence"], snr)
        return info

    def _measure_angle(self, dev: Any, bench: Bench, interf: Interferometer2, prm: dict[str, Any],
                       chans: tuple[int, int], search: float, angle: float, cal: dict[str, Any],
                       cal_log: list[dict[str, Any]]) -> tuple[AngleResult, np.ndarray, float, dict[str, Any]]:
        a, b = chans
        res = AngleResult(true_deg=float(angle))
        recal = 0
        last_x = np.zeros(0, dtype=np.complex64)
        last_f = 0.0
        bench.ota_on(prm["f_target"], angle)
        dev.mark_settling()
        while len(res.bearings) < prm["repeats"]:
            blk = dev.capture(prm["n"], prm["timeout_s"])
            if blk.overflow or int(dev.stats.overflows) != cal["overflows_ref"]:
                recal += 1
                if recal > prm["max_recal"]:
                    raise MeasurementError(f"{angle:+.1f}°: {prm['max_recal']} yeniden kalibrasyondan sonra da kanal "
                                           "hizalaması kayboluyor (overflow) — örnekleme hızını düşürün/USB'yi kontrol edin")
                log.warning("df_rms %+.1f°: overflow — kanal hizalaması kayboldu, faz yeniden kalibre ediliyor", angle)
                bench.ota_off()
                cal = self._calibrate(dev, bench, interf, prm, chans, search)
                cal_log.append({"angle_deg": float(angle), **cal})
                bench.ota_on(prm["f_target"], angle)
                dev.mark_settling()
                continue
            x0, x1 = blk.samples[a], blk.samples[b]
            f_bb, snr = locate_tone(x0, prm["fs"], prm["f_target"] - prm["fc"], search)
            res.bearings.append(interf.bearing(x0, x1, prm["f_target"], f_bb_hz=f_bb, fs=prm["fs"]))
            res.snr_db.append(snr)
            last_x, last_f = x0, f_bb
        bench.ota_off()
        return res, last_x, last_f, cal

    # ------------------------------------------------------------------ run
    def run(self) -> MeasurementResult:
        ctx = self.ctx
        prm = self._params()
        interf = Interferometer2(prm["baseline_m"])
        geo_warn = self._geometry_warnings(prm)
        for w in geo_warn:
            log.warning("df_rms: %s", w)
        dev, owned, ch_b = self._array()
        chans = (0, ch_b)
        cal_log: list[dict[str, Any]] = []
        angles: list[AngleResult] = []
        shot: tuple[float, np.ndarray, float] | None = None
        level: dict[str, Any] = {}
        sweep: list[dict[str, Any]] = []
        try:
            self._setup(dev, prm)
            search = self._search_hz(dev, prm)
            cap = float(dev.profile.rx_max_input_dbm)
            bench: Bench = (SimBench(ctx.world, min(prm["sim_cal_power_dbm"], cap)) if ctx.is_sim else
                            HardwareBench(ctx, dev, min(prm["source_power_dbm"], cap), prm["baseline_m"],
                                          prm["timeout_s"]))
            try:
                dev.start_stream()
                bench.cabled_on(prm["f_pilot"])
                try:
                    dev.mark_settling()
                    coh = check_coherence(dev, n=prm["n"], blocks=prm["coherence_blocks"], ref_channel=chans[0],
                                          channels=[chans[1]], timeout_s=prm["timeout_s"])
                finally:
                    bench.cabled_off()
                cal = self._calibrate(dev, bench, interf, prm, chans, search)
                first_cal = dict(cal)
                if ctx.is_sim:
                    level = self._sim_level(dev, bench, prm, chans, search, prm["sim_snr_db"], cap)
                for ang in prm["angles"]:
                    r, x, f_bb, cal = self._measure_angle(dev, bench, interf, prm, chans, search, ang, cal, cal_log)
                    angles.append(r)
                    if shot is None or abs(ang) < abs(shot[0]):
                        shot = (float(ang), x, f_bb)
                    log.info("df_rms %+6.1f°: kestirim %+.2f° ± %.2f°", ang, float(np.mean(r.est_deg)),
                             float(np.std(r.est_deg)))
                if ctx.is_sim and prm["sim_power_dbm"] is None:
                    for snr in prm["snr_sweep_db"]:
                        lv = self._sim_level(dev, bench, prm, chans, search, snr, cap, ref=level)
                        rs = []
                        for ang in prm["angles"]:
                            r, _x, _f, cal = self._measure_angle(dev, bench, interf, prm, chans, search, ang, cal,
                                                                 cal_log)
                            rs.append(r)
                        sweep.append({"snr_target_db": snr, **self._stats(rs, prm), "ota_power_dbm": lv["power_dbm"]})
                        log.info("df_rms SNR %+.0f dB / %s: RMS %.3f°", snr, fmt_hz(prm["snr_bw_hz"]), sweep[-1]["rms_deg"])
            finally:
                bench.close()
        finally:
            release_errors = self._release(dev, owned)
        res = self._result(dev, prm, coh, first_cal, cal_log, angles, shot, geo_warn, bench.describe(), chans,
                           level, sweep)
        if release_errors:
            res.extra["release_errors"] = release_errors
        return res

    def _rbw(self, prm: dict[str, Any]) -> float:
        return window_info("blackmanharris", prm["n"]).enbw_bins * prm["fs"] / prm["n"]

    def _to_bw(self, snr_rbw_db: float, prm: dict[str, Any]) -> float:
        """SNR measured in the FFT RBW → SNR in the stated bandwidth ``snr_bw_hz``."""
        return snr_rbw_db + 10.0 * math.log10(self._rbw(prm) / prm["snr_bw_hz"])

    def _sim_level(self, dev: Any, bench: Bench, prm: dict[str, Any], chans: tuple[int, int], search: float,
                   snr_db: float, cap_dbm: float, ref: dict[str, Any] | None = None) -> dict[str, Any]:
        """Simulator: over-the-air tone level for ``snr_db`` in ``snr_bw_hz`` (fixed
        ``sim_power_dbm`` when given). One reference capture at broadside measures the SNR
        of the reference level; the receiver is linear far below compression, so the level
        follows by difference. ``ref`` reuses an earlier reference capture."""
        if prm["sim_power_dbm"] is not None:
            p = min(prm["sim_power_dbm"], cap_dbm)
            bench.set_ota_power(p)
            return {"power_dbm": p, "snr_target_db": None}
        if ref is None or "ref_power_dbm" not in ref:
            p_ref = min(prm["sim_cal_power_dbm"], cap_dbm)
            bench.set_ota_power(p_ref)
            bench.ota_on(prm["f_target"], 0.0)
            try:
                dev.mark_settling()
                blk = dev.capture(prm["n"], prm["timeout_s"])
                _f, snr_ref = locate_tone(blk.samples[chans[0]], prm["fs"], prm["f_target"] - prm["fc"], search)
            finally:
                bench.ota_off()
            ref = {"ref_power_dbm": p_ref, "ref_snr_bw_db": self._to_bw(snr_ref, prm)}
        p = min(ref["ref_power_dbm"] + snr_db - ref["ref_snr_bw_db"], cap_dbm)
        bench.set_ota_power(p)
        return {"power_dbm": p, "snr_target_db": snr_db, "ref_power_dbm": ref["ref_power_dbm"],
                "ref_snr_bw_db": ref["ref_snr_bw_db"]}

    def _stats(self, angles: list[AngleResult], prm: dict[str, Any]) -> dict[str, Any]:
        """RMS / bias / max of a scan; the uncertainty of the RMS from the M per-angle mean
        squared errors (the K repeats at one angle are not independent of each other)."""
        est = np.concatenate([r.est_deg for r in angles])
        truth = np.concatenate([np.full(len(r.bearings), r.true_deg) for r in angles])
        s = summarize(est, truth)
        rms = s["rms"]
        e2 = np.array([float(np.mean(angle_errors(r.est_deg, r.true_deg) ** 2)) for r in angles])
        unc = (float(np.std(e2, ddof=1)) / (2.0 * rms * math.sqrt(e2.size)) if e2.size >= 2 and rms > 0 else None)
        snr = [v for r in angles for v in r.snr_db]
        snr_rbw = float(np.mean(snr)) if snr else math.nan
        return {"rms_deg": rms, "bias_deg": s["mean"], "std_deg": s["std"], "max_abs_deg": s["max_abs"],
                "n": s["n"], "rms_unc_deg": unc, "snr_rbw_db": snr_rbw,
                "snr_bw_db": self._to_bw(snr_rbw, prm) if snr else math.nan}

    @staticmethod
    def _release(dev: Any, owned: bool) -> list[str]:
        """Stop the array stream. A group built here is detached: its worker threads stop
        and its members stay open (the session owns and closes them)."""
        errors: list[str] = []
        try:
            if owned and hasattr(dev, "detach"):
                dev.detach()          # stop stream + worker threads; members stay open
            else:
                dev.stop_stream()
        except EDSError as e:
            errors.append(f"{dev.name}: akış durdurulamadı: {e}")
            log.error("df_rms: %s", errors[-1])
        return errors

    # ------------------------------------------------------------------ result
    def _result(self, dev: Any, prm: dict[str, Any], coh: CoherenceReport, cal: dict[str, Any],
                cal_log: list[dict[str, Any]], angles: list[AngleResult], shot: tuple[float, np.ndarray, float] | None,
                geo_warn: list[str], bench: str, chans: tuple[int, int], level: dict[str, Any],
                sweep: list[dict[str, Any]]) -> MeasurementResult:
        st = self._stats(angles, prm)
        rms, unc = st["rms_deg"], st["rms_unc_deg"]
        lam = wavelength(prm["f_target"])
        d = prm["baseline_m"]
        rbw = self._rbw(prm)
        bw_txt = fmt_hz(prm["snr_bw_hz"], 4)
        is_sim = bool(self.ctx.is_sim)
        a_min, a_max = min(prm["angles"]), max(prm["angles"])
        # grating lobes (d > λ/2) appear for |sin θ| >= λ/d − 1
        unamb = 90.0 if d / lam <= 0.5 else math.degrees(math.asin(min(1.0, lam / d - 1.0)))
        steps = np.diff(sorted(prm["angles"]))
        step_txt = f" {float(steps[0]):g}° adımla" if steps.size and np.allclose(steps, steps[0]) else ""
        extra: dict[str, Any] = {
            "rms_deg": rms, "bias_deg": st["bias_deg"], "std_deg": st["std_deg"], "max_abs_deg": st["max_abs_deg"],
            "n": st["n"], "n_angles": len(angles),
            "coherent": bool(coh.coherent), "coherence": coh.coherence,
            "phase_drift_deg_per_s": coh.phase_drift_deg_per_s, "phase_std_deg": coh.phase_std_deg,
            "delay_samples": coh.delay_samples, "delay_resolvable": coh.delay_resolvable,
            "coherence_note": coh.note,
            "phase_cal_deg": cal["phase_cal_deg"], "cal_coherence": cal["coherence"], "cal_snr_db": cal["snr_db"],
            "pilot_phase_deg": coh.phase_deg,
            "pilot_to_target_phase_deg": float(wrap_deg(cal["phase_cal_deg"] - coh.phase_deg)),
            "recalibrations": len(cal_log), "baseline_m": d, "wavelength_m": lam, "d_over_lambda": d / lam,
            "unambiguous_max_deg": unamb, "ambiguous": max(abs(a_min), abs(a_max)) >= unamb,
            "snr_db_mean": st["snr_rbw_db"], "snr_rbw_hz": rbw, "snr_bw_db": st["snr_bw_db"],
            "snr_bw_hz": prm["snr_bw_hz"], "snr_per_sample_db": st["snr_bw_db"] + 10.0 * math.log10(
                prm["snr_bw_hz"] / prm["fs"]) if math.isfinite(st["snr_bw_db"]) else math.nan,
            "f_target_hz": prm["f_target"], "f_pilot_hz": prm["f_pilot"], "angle_range_deg": [a_min, a_max],
            "mask_bw_hz": DEFAULT_MASK_BINS * prm["fs"] / prm["n"], "simulated": is_sim,
        }
        if level:
            extra["sim_level"] = level
        if sweep:
            extra["snr_sweep"] = sweep
        if cal_log:
            extra["recalibration_log"] = cal_log
        if geo_warn:
            extra["geometry_warnings"] = geo_warn
        if is_sim:
            extra["model_limitations"] = SIM_FLAG

        conditions = base_conditions(dev)
        conditions.update({
            "fc_hz": prm["fc"], "fs_hz": prm["fs"], "f_target_hz": prm["f_target"], "f_pilot_hz": prm["f_pilot"],
            "baseline_m": d, "wavelength_m": lam, "d_over_lambda": d / lam, "angles_deg": prm["angles"],
            "repeats": prm["repeats"], "n": prm["n"], "mask_bw_hz": extra["mask_bw_hz"], "rbw_hz": rbw,
            "window": "blackmanharris", "channels": [dev.name, chans[0], chans[1]], "source": bench,
            "coherent": bool(coh.coherent), "calibration": "kablolu referans, hedef frekansında (φ_cal, gerçek açı 0°)",
            "snr_db": st["snr_bw_db"], "snr_bw_hz": prm["snr_bw_hz"],
        })
        if is_sim and level.get("snr_target_db") is not None:
            conditions["sim_snr_target_db"] = level["snr_target_db"]
        snr_txt = (f"SNR {st['snr_bw_db']:.1f} dB / {bw_txt} (FFT bininde {st['snr_rbw_db']:.1f} dB / "
                   f"{fmt_hz(rbw, 3)}, örnek başına {extra['snr_per_sample_db']:.1f} dB / {fmt_hz(prm['fs'], 3)})")
        summary = (
            f"{prm['f_target'] / 1e9:.4f} GHz'te d = {d:.3f} m ({d / lam:.2f} λ) iki elemanlı interferometre ile "
            f"{a_min:+.0f}°…{a_max:+.0f}° aralığında{step_txt} (açı başına {prm['repeats']} ölçüm, {snr_txt}) "
            f"ölçülen yön bulma RMS hatası {fmt_deg(rms)}°"
            + (f" ± {fmt_deg(unc)}°" if unc is not None else "")
            + f", sapma {'+' if st['bias_deg'] >= 0 else '−'}{fmt_deg(abs(st['bias_deg']))}°, en büyük |hata| "
            f"{fmt_deg(st['max_abs_deg'])}° (faz kalibrasyonu kablolu referansla; koherens {coh.coherence:.3f}, "
            f"faz kayması {coh.phase_drift_deg_per_s:+.3g} °/s)."
        )
        if sweep:
            summary += (f" RMS–SNR ({bw_txt}): " + ", ".join(f"{w['snr_target_db']:+.0f} dB → {fmt_deg(w['rms_deg'])}°"
                                                             for w in sweep) + ".")
        if not coh.coherent:
            summary = f"{NO_COHERENCE_FLAG}: sonuç koşulsuz kullanılamaz — {summary}"
        if is_sim:
            summary = f"{SIM_FLAG}. {summary}"
        if geo_warn:
            summary += " UYARI: " + "; ".join(geo_warn) + "."
        method = (
            ("SİMÜLASYON (yalnız termal gürültü; çok yollu yayılım, anten kuplajı, anten/kablo faz uyumsuzluğu, LO "
             f"faz kayması modellenmedi): havadan ton {bw_txt} bantta {prm['sim_snr_db']:g} dB SNR'a ayarlanır "
             "(broadside referans yakalamasından), kablolu kalibrasyon sinyali güçlü tutulur. " if is_sim and
             prm["sim_power_dbm"] is None else ("SİMÜLASYON (yalnız termal gürültü). " if is_sim else ""))
            + "İki kanal aynı fc/fs/manuel kazançla çalıştırılır ve akış ölçüm boyunca kesintisiz tutulur; bölücüden "
            "beslenen ortak kablolu sinyalle koherens (faz kayması, koherens, faz kararlılığı) ölçülür. Faz "
            "kalibrasyonu (φ_cal, gerçek açı 0°) kablolu sinyal hedefle aynı frekansta verilerek yapılır — CW pilotla "
            "örnek gecikmesi çözülemediğinden başka frekansta kalibrasyon 2π·Δf·gecikme/fs faz hatası bırakırdı. Her "
            f"gerçek açıda havadan gelen ton için {prm['repeats']} yakalamada Interferometer2 kerterizi FFT bant "
            f"maskesiyle (±{DEFAULT_MASK_BINS // 2} bin) hesaplanır; hata wrap(θ̂ − θ), RMS = √(ortalama hata²). "
            f"SNR {bw_txt} bantta verilir (FFT bininde ölçülen SNR + 10·log10(RBW/{bw_txt})). Belirsizlik açı başına "
            "ortalama kare hatalardan (M açı bağımsız birim; aynı açıdaki tekrarlar aynı sapmayı paylaşır). "
            "Overflow (hizalama kaybı) görülürse faz kalibrasyonu yinelenir."
        )
        res = self.new_result(rms, summary=summary, method=method, conditions=conditions, extra=extra, uncertainty=unc)
        res.device = dev.describe()                  # the array (group: both members), not only devices[0]
        if not coh.coherent:
            res.blockers.append(REASON_NO_COHERENCE)
        if is_sim:
            res.blockers.append(BLOCKER_SIM)
        res.table = [r.row() for r in angles]
        res.figures = self._figures(prm, angles, rms, shot, d / lam)
        if sweep:
            res.figures.append(self._sweep_figure(sweep, prm, level))
        return res

    def _sweep_figure(self, sweep: list[dict[str, Any]], prm: dict[str, Any], level: dict[str, Any]) -> FigureSpec:
        bw = fmt_hz(prm["snr_bw_hz"], 4)
        x = [w["snr_bw_db"] for w in sweep]
        return FigureSpec(
            name="df_rms_vs_snr", title=f"Yön bulma RMS hatası – SNR ({bw} bantta; simülasyon, yalnız termal gürültü)",
            kind="xy", series=[Series(x=x, y=[w["rms_deg"] for w in sweep],
                                      yerr=[w["rms_unc_deg"] or 0.0 for w in sweep], label="RMS hata", style="marker")],
            xlabel=f"SNR [dB] ({bw} bantta, ölçülen)", ylabel="RMS hata [°]", logy=True,
            vlines=[(prm["sim_snr_db"], f"başlık SNR {prm['sim_snr_db']:g} dB")] if level.get("snr_target_db")
            is not None else [],
            info={"f": fmt_hz(prm["f_target"], 7), "K": str(prm["repeats"]), "M": str(len(prm["angles"]))})

    def _figures(self, prm: dict[str, Any], angles: list[AngleResult], rms: float,
                 shot: tuple[float, np.ndarray, float] | None, d_lam: float) -> list[FigureSpec]:
        rows = [r.row() for r in angles]
        truth = [r["true_deg"] for r in rows]
        lo, hi = min(truth) - 10.0, max(truth) + 10.0
        info = {"f": fmt_hz(prm["f_target"], 7), "d/λ": f"{d_lam:.3f}", "K": str(prm["repeats"])}
        figs = [
            FigureSpec(
                name="df_rms_estimated_vs_true", title="Yön bulma — kestirilen açı / gerçek açı", kind="xy",
                series=[Series(x=[lo, hi], y=[lo, hi], label="ideal (θ̂ = θ)", style="line"),
                        Series(x=truth, y=[r["mean_deg"] for r in rows], yerr=[r["std_deg"] for r in rows],
                               label="kestirim (ortalama ± σ)", style="marker")],
                xlabel="Gerçek açı [°]", ylabel="Kestirilen açı [°]", xlim=(lo, hi), ylim=(lo, hi), info=info,
            ),
            FigureSpec(
                name="df_rms_error_vs_angle", title=f"Yön bulma hatası – açı (RMS = {fmt_deg(rms)}°)", kind="xy",
                series=[Series(x=truth, y=[r["error_deg"] for r in rows], yerr=[r["std_deg"] for r in rows],
                               label="hata (ortalama ± σ)", style="bar")],
                xlabel="Gerçek açı [°]", ylabel="Hata θ̂ − θ [°]",
                hlines=[(rms, f"+RMS {fmt_deg(rms)}°"), (-rms, f"−RMS {fmt_deg(rms)}°")], info=info,
            ),
            FigureSpec(
                name="df_rms_polar", title="Yön bulma — kestirimler (polar, broadside = 0°)", kind="polar",
                series=[Series(x=truth, y=[0.8] * len(truth), label="gerçek açı", style="marker"),
                        Series(x=[float(v) for r in angles for v in r.est_deg],
                               y=[1.0] * sum(len(r.bearings) for r in angles), label="kestirim", style="marker")],
                ylim=(0.0, 1.1), info={"theta_zero": "N", "theta_direction": "cw", **info},
            ),
        ]
        if shot is not None and shot[1].size:
            ang, x, f_bb = shot
            nfft = int(min(self.ctx.app.dsp.nfft, x.size))
            spec = welch(x, prm["fs"], nfft, self.ctx.app.dsp.window, float(self.ctx.app.dsp.overlap), "mean",
                         fc=prm["fc"])
            f_mark = prm["fc"] + f_bb
            k = int(np.argmin(np.abs(spec.f_hz - f_mark)))
            lo_k, hi_k = max(k - 3, 0), min(k + 4, spec.f_hz.size)
            level = float(np.max(spec.p_db[lo_k:hi_k]))
            figs.append(spectrum_screenshot(spec, "df_rms_spectrum",
                                            f"DF kanal 0 spektrumu — havadan ton, gerçek açı {ang:+.0f}°",
                                            markers=[(f_mark, level, f"hedef {ang:+.0f}°")]))
        return figs
