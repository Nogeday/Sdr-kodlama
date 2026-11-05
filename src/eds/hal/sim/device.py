"""Physics-based AD9363 / ADALM-Pluto receiver model (``backend = "sim"``).

CI, demos and the validation of every KTR measurement procedure run against this model,
so each impairment is reproduced *quantitatively* (numbers below are the defaults of the
``[[device]].sim`` table; every one can be overridden per device).

Signal chain per sample (in this order)
---------------------------------------
1. **Level.** An emitter with input power ``P`` dBm appears at complex baseband with power
   ``P + G + gain_offset_db`` dBFS (``G`` = hardware gain in dB, ``gain_offset_db`` = -5 ⇒
   full scale ≈ -68 dBm at G = 73). 0 dBFS == ``mean(|x|^2) == 1``.
2. **Thermal noise.** Density ``kT + NF`` = ``-173.975 + noise_figure_db`` dBm/Hz referred
   to the input, scaled by the same level equation (per-sample variance = density·fs).
   Generated in the frequency domain in frames of :data:`~eds.hal.sim.signals.FRAME`
   samples aligned to absolute sample indices (independent of read sizes).
3. **RX filters (analog LPF x digital decimation chain).** Both are applied to every
   emitter contribution at its *pre-alias* baseband offset ``f = f_RF - f_LO`` (per
   frequency bin for noise-like emitters, per Jacobi-Anger sideband for FM), i.e. BEFORE
   the contribution folds into ``±fs/2`` (baseband frequency mod fs), and to the thermal
   noise within ``±fs/2`` (per-frame frequency-domain shaping, which gives the band-edge
   roll-off of real Pluto spectra):

   * analog: 3rd-order Butterworth magnitude, -3 dB corner at ``bw/2``;
   * digital (``digital_filter`` = true): the AD9363 half-band decimators + programmable
     FIR, flat to ``±dig_pass_frac·fs`` (0.40) and ``dig_stop_db`` (-80 dB) beyond
     ``±dig_stop_frac·fs`` (0.60) with a cosine taper in dB in between
     (:func:`~eds.hal.sim.signals.digital_filter_db`). An emitter at ``k·fs + δ``
     (``k != 0``) therefore folds onto ``δ`` at least 80 dB down (plus the analog
     attenuation), as on the real chip, where alias images are rejected by > 50 dB.

   Ground truth: :meth:`SimRxDevice.response_db` ``(f_bb_hz)`` = ``20 log10(|H_analog(f)|
   |H_digital(f)|)`` for the commanded ``fs``/``bw``; a tone at pre-alias offset ``f`` reads
   ``level_dbfs(P) + response_db(f)`` at its aliased frequency. Emitter contributions whose
   level after both filters is more than 60 dB below the ADC noise floor
   (:attr:`adc_noise_dbfs`; the 12-bit quantization noise when ``adc_noise_lsb_rms`` = 0;
   :attr:`skip_level_dbfs`) are not synthesized at all — emitters GHz away from the LO cost
   nothing, which keeps a full-band sweep fast.
4. **TCXO.** ``e = ppm·1e-6``; with hardware correction ``e_hw`` (``set_ppm_correction``,
   Pluto ``xo_correction``) the residual is ``e_res = (1+e)/(1+e_hw) - 1``. Actual LO =
   ``fc·(1+e_res)``, actual sample clock = ``fs·(1+e_res)``; an emitter at ``f_true`` is
   seen at nominal-Hz baseband frequency ``(f_true - fc(1+e_res))/(1+e_res)``, i.e. apparent
   RF ``f_true/(1+e_res)`` (ARCHITECTURE ppm sign convention).
5. **Geometry / LO phase.** Received carrier phase at world time ``t``:
   ``2π f t + φ_e + 2π f/c·dot(p, u) − (2π f_LO t + θ_LO)`` with ``u = (sin aoa, cos aoa)``
   (``cabled`` emitters: no geometric term). ``θ_LO`` is redrawn uniformly at every retune
   and hardware ppm change (``lo_phase_random``; else 0). Device sample ``n`` is taken at
   world time ``t(n) = (n + time_offset_samples)/fs_act`` (piecewise across settings
   changes), so emitter phases are continuous across reads via the absolute index.
   ``shared_clock`` group: all members use the first member's ppm, hence identical
   ``f_LO``/``fs_act`` and a relative phase ``θ_a − θ_b`` (+ geometry) that only changes when
   either device retunes. Independent clocks: ``angle(x_a·conj(x_b))`` of a tone at ``f``
   advances at ``2π f (1/(1+e_a) − 1/(1+e_b)) ≈ 2π f (e_b − e_a)`` rad/s. Noise-like emitter
   envelopes are drawn on the world sample grid ``m = n + time_offset_samples`` so two
   devices see the same realization (cross-correlation peak at the offset difference).
6. **IQ imbalance** on the composite analog signal: ``y = x + r·conj(x)`` with
   ``r = K2/K1`` of the ``I' = I, Q' = g(Q cos φ − I sin φ)`` model ⇒ image rejection
   ``IRR = 10log10((1+2g cos φ+g²)/(1−2g cos φ+g²))``, ``g = 10^(ε/20)``. ``ε, φ`` =
   ``iq_gain_db, iq_phase_deg`` (0.4 dB, 2.5° ⇒ 30 dB) with quadrature tracking off, the
   residuals ``iq_residual_gain_db, iq_residual_phase_deg`` (0.02 dB, 0.15° ⇒ 55 dB) on.
7. **DC offset**: complex constant (fixed random phase per device) at ``dc_offset_dbfs``
   (-40) with BB DC tracking off, ``dc_residual_dbfs`` (-80) on. RF DC tracking is
   accepted/reported; its effect is folded into the BB DC model.
8. **Spurs**: post-ADC digital tones at ``offset_hz`` / ``dbfs`` (gain independent, on the
   digital clock, phase continuous); spurs outside ``±fs/2`` are dropped.
9. **ADC**: white, gain-independent transition noise of ``adc_noise_lsb_rms`` (0.5 LSB rms
   per I/Q component ≈ -69.2 dBFS; it dithers the quantizer so sub-LSB DC/spurs survive
   and the output floor never collapses to exact zeros at low gain), then 12-bit
   quantization ``rint(x·2048)`` with HARD clipping of I and Q separately to
   ``[-2048, 2047]`` (values ``counts/2048``; ``profile.full_scale_counts``). The
   quantization noise itself (−74 dBFS for 12 bits) arises from the rounding. Output floor
   at minimum gain ≈ −68 dBFS; at high gain the thermal term dominates.

Emitter synthesis (see :mod:`eds.hal.sim.world` for the kinds): CW tones (and the
Jacobi–Anger sideband series of sine FM, ``J_n(β)`` at ``f + n·f_mod``, each filtered by the
LPF at its own frequency) are summed in one separable complex64 matrix product; pulse
trains and FCCH bursts are gated analytically per period on the world-time axis (carrier
phase continuous); noise-like envelopes are frequency-domain frames on the world grid.
Phases are evaluated exactly (``fractions.Fraction``) at every piece start, so they do not
drift with stream length. Reading the same index range in different chunk sizes yields the
same waveform up to float32 rounding (rare ±1 LSB flips after quantization).

Settings changes and stale buffers
----------------------------------
Every settings change (fc, fs, bw, gain, gain mode, tracking, ppm correction) is recorded
in a per-device log keyed by absolute sample index. While streaming, samples that were
already in the device FIFO keep the OLD settings: in realtime mode the samples produced
before the call; in non-realtime mode the next ``kernel_buffers × buffer_size`` samples.
:class:`~eds.hal.base.RxDevice` flags ``(kernel_buffers + 1) × buffer_size`` samples as
settling, so :meth:`capture` never returns stale data while a raw :meth:`read` right after
a retune does.

Hardware AGC (``hw_slow | hw_fast | hw_hybrid``): an AD9361-like slow-attack loop that, once
per ``buffer_size`` block (aligned to absolute indices), moves the gain toward a block
peak of -12 dBFS (±2 dB hysteresis, at most +3/-6 dB per block, -10 dB on clipping);
``gains[0]`` follows it (not sample-synchronous — like the real chip).

Streaming / USB model
---------------------
* ``realtime = False``: ``_read_raw`` returns the requested samples immediately; time is
  virtual and deterministic, no overflow. Sample production times (for emitter switching)
  are ``max(read time, previous production time + 1/fs)``.
* ``realtime = True``: the device produces ``fs_act`` samples per second of *virtual* time
  = host monotonic clock minus the time spent inside this simulator's own synthesis
  (production is frozen while synthesizing, so the model's CPU cost never counts against
  the consumer). FIFO capacity = ``kernel_buffers × buffer_size``; reads move at most
  ``usb_bytes_per_s / bytes_per_sample`` samples/s (sleeping for the transfer time) and
  block until ``min(n, buffer_size)`` samples are available. If the FIFO would exceed its
  capacity (slow consumer, or ``fs·bytes_per_sample > usb_bytes_per_s``) the OLDEST samples
  are dropped, the next read reports ``overflow_before=True`` and the waveform resumes at
  the correct absolute sample index after the gap.
* Emitter on/off switches and other scene changes are applied per sample: a sample uses the
  :class:`~eds.hal.sim.world.Scene` active at its production time (host
  ``time.perf_counter``), so a switch at host time ``T`` shows up exactly at the first
  sample produced at or after ``T``.
"""

from __future__ import annotations

import bisect
import dataclasses
import math
import threading
import time
from collections import OrderedDict
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

import numpy as np

from ...core.config import DeviceConfig, RxConfig
from ...core.errors import ConfigError, DeviceError, StreamError
from ...core.profile import DeviceProfile
from ...core.units import C_LIGHT, KT_DBM_HZ
from ..base import RawRead, RxDevice
from . import signals as sg
from .world import Emitter, Scene, SimWorld

# Indirections so tests can drive realtime behaviour with a fake clock.
_perf = time.perf_counter
_sleep = time.sleep
_time_ns = time.time_ns

_TWO_PI = 2.0 * math.pi
_SKIP_MARGIN_DB = 60.0             # emitter contributions this far below the ADC floor are skipped
_SHAPE_FLOOR = 1e-7                # thermal-noise bins with |H| below this (-140 dB) are not drawn
_FRAME_CACHE_SIZE = 24
_AGC_TARGET_DBFS = -12.0
_AGC_HYST_DB = 2.0
_AGC_MAX_UP_DB = 3.0
_AGC_MAX_DOWN_DB = 6.0
_AGC_CLIP_STEP_DB = -10.0
_MAX_PPM_CORRECTION = 1000.0
_FM_MAX_SIDEBANDS = 401            # beyond this (beta > ~170) FM is synthesized per sample
_MAX_PIECE = 1 << 20               # synthesis chunk (bounds temporary memory for huge reads)

_DEFAULT_SPURS: list[dict[str, float]] = [{"offset_hz": 1.234e6, "dbfs": -85.0}]

#: Accepted ``[[device]].sim`` keys and their defaults (``None`` = derived, see docs).
SIM_PARAM_DEFAULTS: dict[str, Any] = {
    "serial": None,                 # "SIM-" + name
    "ppm": None,                    # first device of the world +11.7, else name-hashed ±15
    "noise_figure_db": 5.0,
    "gain_offset_db": -5.0,
    "dc_offset_dbfs": -40.0,
    "dc_residual_dbfs": -80.0,
    "iq_gain_db": 0.4,
    "iq_phase_deg": 2.5,
    "iq_residual_gain_db": 0.02,
    "iq_residual_phase_deg": 0.15,
    "spurs": _DEFAULT_SPURS,
    "adc_noise_lsb_rms": 0.5,       # ADC transition noise per I/Q component (dithers the quantizer)
    "usb_bytes_per_s": 24e6,
    "realtime": True,
    "position_m": [0.0, 0.0],
    "shared_clock": "",
    "lo_phase_random": True,
    "time_offset_samples": 0,
    "hw_xo_correction": True,
    "xo_correction_step_hz": 0.0,   # 1.0 emulates Pluto's integer-Hz xo_correction
    "seed": None,                   # world seed + stable hash(name)
    "digital_filter": True,         # AD9363 RX digital decimation chain (half-bands + FIR)
    "dig_pass_frac": sg.DIG_PASS_FRAC,   # flat up to +-0.40 fs
    "dig_stop_frac": sg.DIG_STOP_FRAC,   # dig_stop_db from +-0.60 fs on
    "dig_stop_db": sg.DIG_STOP_DB,       # stop-band gain (dB)
}

_FLOAT_KEYS = ("ppm", "noise_figure_db", "gain_offset_db", "dc_offset_dbfs", "dc_residual_dbfs",
               "iq_gain_db", "iq_phase_deg", "iq_residual_gain_db", "iq_residual_phase_deg",
               "usb_bytes_per_s", "xo_correction_step_hz", "adc_noise_lsb_rms", "dig_pass_frac",
               "dig_stop_frac", "dig_stop_db")
_BOOL_KEYS = ("realtime", "lo_phase_random", "hw_xo_correction", "digital_filter")


def _parse_sim_params(name: str, raw: dict[str, Any]) -> dict[str, Any]:
    """Validate ``cfg.sim`` (unknown keys / wrong types -> :class:`ConfigError`, Turkish)."""
    where = f"device '{name}': sim"
    if not isinstance(raw, dict):
        raise ConfigError(f"{where} bir tablo olmalı")
    unknown = set(raw) - set(SIM_PARAM_DEFAULTS)
    if unknown:
        raise ConfigError(f"{where}: bilinmeyen anahtar(lar) {sorted(unknown)} — geçerli: {sorted(SIM_PARAM_DEFAULTS)}")
    p: dict[str, Any] = {k: (list(v) if isinstance(v, list) else v) for k, v in SIM_PARAM_DEFAULTS.items()}
    p["spurs"] = [dict(s) for s in _DEFAULT_SPURS]
    for k, v in raw.items():
        key = f"{where}.{k}"
        if k in _FLOAT_KEYS:
            if isinstance(v, bool) or not isinstance(v, (int, float)) or not math.isfinite(float(v)):
                raise ConfigError(f"{key}: sonlu sayı bekleniyordu, {v!r} geldi")
            p[k] = float(v)
        elif k in _BOOL_KEYS:
            if not isinstance(v, bool):
                raise ConfigError(f"{key}: true/false bekleniyordu, {v!r} geldi")
            p[k] = v
        elif k in ("serial", "shared_clock"):
            if not isinstance(v, str):
                raise ConfigError(f"{key}: metin bekleniyordu, {v!r} geldi")
            p[k] = v
        elif k in ("time_offset_samples", "seed"):
            if isinstance(v, bool) or not isinstance(v, (int, float)) or int(v) != v:
                raise ConfigError(f"{key}: tamsayı bekleniyordu, {v!r} geldi")
            p[k] = int(v)
            if k == "seed" and p[k] < 0:
                raise ConfigError(f"{key}: negatif olamaz")
        elif k == "position_m":
            if (not isinstance(v, (list, tuple)) or len(v) != 2
                    or any(isinstance(c, bool) or not isinstance(c, (int, float)) or not math.isfinite(c) for c in v)):
                raise ConfigError(f"{key}: [x, y] (metre) bekleniyordu, {v!r} geldi")
            p[k] = [float(v[0]), float(v[1])]
        elif k == "spurs":
            if not isinstance(v, list):
                raise ConfigError(f"{key}: liste bekleniyordu, {v!r} geldi")
            spurs = []
            for i, s in enumerate(v):
                if not isinstance(s, dict) or set(s) != {"offset_hz", "dbfs"}:
                    raise ConfigError(f"{key}[{i}]: {{offset_hz, dbfs}} tablosu bekleniyordu, {s!r} geldi")
                vals = [s["offset_hz"], s["dbfs"]]
                if any(isinstance(x, bool) or not isinstance(x, (int, float)) or not math.isfinite(x) for x in vals):
                    raise ConfigError(f"{key}[{i}]: sayısal offset_hz/dbfs bekleniyordu, {s!r} geldi")
                spurs.append({"offset_hz": float(s["offset_hz"]), "dbfs": float(s["dbfs"])})
            p[k] = spurs
    if p["usb_bytes_per_s"] <= 0:
        raise ConfigError(f"{where}.usb_bytes_per_s pozitif olmalı")
    if p["noise_figure_db"] < 0:
        raise ConfigError(f"{where}.noise_figure_db negatif olamaz")
    if p["adc_noise_lsb_rms"] < 0:
        raise ConfigError(f"{where}.adc_noise_lsb_rms negatif olamaz")
    if p["xo_correction_step_hz"] < 0:
        raise ConfigError(f"{where}.xo_correction_step_hz negatif olamaz")
    if not (0.0 < p["dig_pass_frac"] < p["dig_stop_frac"]) or p["dig_pass_frac"] > 0.5:
        raise ConfigError(
            f"{where}: 0 < dig_pass_frac < dig_stop_frac ve dig_pass_frac <= 0.5 olmalı "
            f"(geldi: {p['dig_pass_frac']}, {p['dig_stop_frac']})")
    if p["dig_stop_db"] > 0.0:
        raise ConfigError(f"{where}.dig_stop_db pozitif olamaz (zayıflatma, dB ≤ 0)")
    return p


@dataclass(frozen=True, slots=True)
class _RxState:
    """Effective radio settings for a run of samples (immutable)."""

    fc: float
    fs: float
    bw: float
    gain_db: float
    gain_mode: str
    lo_phase: float
    e_res: float
    dc_tracking: bool
    rf_dc_tracking: bool
    quad_tracking: bool

    @property
    def fs_act(self) -> float:
        return self.fs * (1.0 + self.e_res)

    @property
    def f_lo(self) -> float:
        return self.fc * (1.0 + self.e_res)


@dataclass(frozen=True, slots=True)
class _Segment:
    """State log entry: ``state`` is effective from absolute sample index ``start``.

    ``t_start`` = world time (s) of sample ``start``; ``m_offset`` maps device index to the
    world sample grid used for shared noise envelopes (``m = n + m_offset``).
    """

    start: int
    t_start: float
    state: _RxState
    m_offset: int

    def t_of(self, n: int) -> float:
        return self.t_start + (n - self.start) / self.state.fs_act


class SimRxDevice(RxDevice):
    """Simulated single-channel AD9363 receiver attached to a shared :class:`SimWorld`."""

    backend_name = "sim"

    def __init__(self, cfg: DeviceConfig, profile: DeviceProfile, world: SimWorld):
        super().__init__(cfg, profile)
        if not isinstance(world, SimWorld):
            raise ConfigError(f"device '{cfg.name}': SimWorld bekleniyordu, {type(world).__name__} geldi")
        if len(cfg.channels) != 1:
            raise ConfigError(
                f"device '{cfg.name}': simülatör cihaz başına tek RX kanalı modeller (AD9363 1R1T); "
                f"channels={cfg.channels}. Çok kanal için birden çok [[device]] + DeviceGroup kullanın."
            )
        self.world = world
        p = _parse_sim_params(cfg.name, cfg.sim)
        self._p = p
        self._seed = int(p["seed"]) if p["seed"] is not None else (world.seed + sg.stable_hash(cfg.name)) % (1 << 62)
        self._p["seed"] = self._seed
        self._ppm = float(world.register_device(cfg.name, p["shared_clock"], p["ppm"]))
        self._p["ppm"] = self._ppm
        serial = p["serial"] if p["serial"] is not None else f"SIM-{cfg.name}"
        self._p["serial"] = serial
        self._pos = np.array(p["position_m"], dtype=np.float64)
        self._fs_counts = float(profile.full_scale_counts)
        # Emitter contributions whose level after the analog + digital filters is more than
        # _SKIP_MARGIN_DB below the output noise floor at minimum gain (ADC transition noise;
        # the 12-bit quantization noise bounds it when adc_noise_lsb_rms = 0) are not synthesized.
        quant_dbfs = 10.0 * math.log10(2.0 / (12.0 * self._fs_counts ** 2))
        self._skip_dbfs = max(self.adc_noise_dbfs, quant_dbfs) - _SKIP_MARGIN_DB
        self._ctl_rng = np.random.default_rng(np.random.SeedSequence([self._seed, 7]))
        self._dc_phase = float(self._ctl_rng.uniform(0.0, _TWO_PI))
        self._spur_phase = [float(self._ctl_rng.uniform(0.0, 1.0)) for _ in p["spurs"]]

        self.info.driver = "sim"
        self.info.hardware = f"EDS-SIM {profile.chip} model"
        self.info.serial = serial
        self.info.firmware = "sim-1"
        self.info.uri = f"sim://{cfg.name}"
        self.info.extra = {"realtime": str(p["realtime"]).lower(), "shared_clock": p["shared_clock"] or "-",
                           "true_ppm": f"{self._ppm:+.4f}"}

        self._sim_lock = threading.RLock()
        self._closed = False
        self._sim_streaming = False
        self._n_next = 0                                   # absolute index of next sample to deliver
        self._anchors: list[tuple[float, float, float]] = []   # production clock (n0, t0, rate)
        self._pending_overflow = False
        self.overflow_events = 0
        self.dropped_samples = 0
        self._frame_cache: OrderedDict[tuple, np.ndarray] = OrderedDict()
        self._shape_cache: OrderedDict[tuple, tuple[list[tuple[int, int]], np.ndarray]] = OrderedDict()
        self._last_power_dbfs: float | None = None
        self.xo_correction_hz = float(profile.ref_clock_hz)

        # Power-on state = application defaults (RxConfig), manual gain, all tracking on.
        rx = RxConfig()
        gain = profile.gain_range(rx.center_freq_hz).clamp(rx.gain_db)
        self.fc, self.fs, self.bw = rx.center_freq_hz, rx.sample_rate_hz, rx.bandwidth_hz
        self.gains = [gain]
        self.gain_mode = "manual"
        st = _RxState(fc=rx.center_freq_hz, fs=rx.sample_rate_hz, bw=rx.bandwidth_hz, gain_db=gain,
                      gain_mode="manual", lo_phase=self._new_lo_phase(), e_res=self._e_res(0.0),
                      dc_tracking=True, rf_dc_tracking=True, quad_tracking=True)
        off = int(p["time_offset_samples"])
        self._segments: list[_Segment] = [_Segment(0, off / st.fs_act, st, off)]
        self._agc_gain = gain
        self._agc_peak = 0.0

    # ================================================================== ground truth
    @property
    def true_ppm(self) -> float:
        """Actual TCXO error of this simulated device (ground truth for calibration tests)."""
        return self._ppm

    @property
    def residual_ppm(self) -> float:
        """Reference error left after hardware correction (commanded state)."""
        return self._segments[-1].state.e_res * 1e6

    @property
    def sim_params(self) -> dict[str, Any]:
        """Effective simulator parameters (copy)."""
        return {k: ([dict(s) for s in v] if k == "spurs" else (list(v) if isinstance(v, list) else v))
                for k, v in self._p.items()}

    @property
    def position_m(self) -> tuple[float, float]:
        return float(self._pos[0]), float(self._pos[1])

    @property
    def abs_index(self) -> int:
        """Absolute device sample index of the next sample to be delivered (gaps included)."""
        return self._n_next

    def level_dbfs(self, power_dbm: float, gain_db: float | None = None) -> float:
        """Expected baseband level of an in-band input of ``power_dbm`` (level equation)."""
        g = self.gains[0] if gain_db is None else gain_db
        return float(power_dbm) + float(g) + self._p["gain_offset_db"]

    def noise_density_dbfs_hz(self, gain_db: float | None = None) -> float:
        """Expected thermal noise density in dBFS/Hz (flat part of the passband)."""
        return self.level_dbfs(KT_DBM_HZ + self._p["noise_figure_db"], gain_db)

    @property
    def adc_noise_dbfs(self) -> float:
        """Total power (dBFS, complex) of the white ADC transition noise."""
        s = self._p["adc_noise_lsb_rms"] / self._fs_counts
        return 10.0 * math.log10(max(2.0 * s * s, 1e-30))

    @property
    def skip_level_dbfs(self) -> float:
        """Emitter contributions below this level (after the filters) are not synthesized."""
        return self._skip_dbfs

    def response_db(self, f_bb_hz: Any) -> Any:
        """Ground-truth RX amplitude response (dB) at PRE-ALIAS baseband offset ``f_bb_hz``.

        ``f_bb_hz = f_RF - f_LO`` (Hz) before folding into ``+-fs/2``; the result is
        ``20 log10(|H_analog(f)| |H_digital(f)|)`` for the commanded ``fs`` / ``bw`` (analog
        3rd-order Butterworth with corner ``bw/2`` times the digital decimation chain, see
        the module docstring). A tone at ``f`` then reads ``level_dbfs(P) + response_db(f)``
        at its aliased frequency. Scalar in -> ``float``; array in -> ``float64`` array.
        """
        h = self._hmag(f_bb_hz, self._segments[-1].state)
        if np.ndim(h) == 0:
            return 20.0 * math.log10(max(float(h), 1e-300))
        return 20.0 * np.log10(np.maximum(h, 1e-300))

    def _hmag(self, f: Any, st: _RxState) -> Any:
        """Combined ``|H|`` (analog LPF x digital chain) at pre-alias baseband ``f`` (Hz)."""
        h = sg.butterworth_mag(f, st.bw / 2.0)
        p = self._p
        if not p["digital_filter"]:
            return h
        g_db = sg.digital_filter_db(f, st.fs, p["dig_pass_frac"], p["dig_stop_frac"], p["dig_stop_db"])
        if np.ndim(g_db) == 0:
            return h * 10.0 ** (g_db / 20.0)
        return h * np.power(10.0, g_db / 20.0)

    def image_rejection_db(self) -> float:
        """Image rejection of the commanded quadrature-tracking state."""
        eps, phi = self._iq_params(self._segments[-1].state)
        return sg.image_rejection_db(eps, phi)

    # ================================================================== backend primitives
    def _apply_sample_rate(self, hz: float) -> float:
        if not (math.isfinite(hz) and hz > 0):
            raise ConfigError(f"{self.name}: örnekleme hızı pozitif olmalı (geldi: {hz})")
        self._push_state(fs=float(hz))
        return float(hz)

    def _apply_bandwidth(self, hz: float) -> float:
        if not (math.isfinite(hz) and hz > 0):
            raise ConfigError(f"{self.name}: analog BW pozitif olmalı (geldi: {hz})")
        self._push_state(bw=float(hz))
        return float(hz)

    def _apply_center_freq(self, hz: float) -> float:
        if not (math.isfinite(hz) and hz > 0):
            raise ConfigError(f"{self.name}: merkez frekans pozitif olmalı (geldi: {hz})")
        with self._sim_lock:
            self._push_state(fc=float(hz), lo_phase=self._new_lo_phase())
        return float(hz)

    def _apply_gain(self, db: float, channel: int) -> float:
        if channel != 0:
            raise ConfigError(f"{self.name}: simülatörde yalnız kanal 0 var (istenen: {channel})")
        with self._sim_lock:
            g = self.profile.gain_range(self._segments[-1].state.fc).clamp(db)
            self._push_state(gain_db=g)
            self._agc_gain = g
        return g

    def _apply_gain_mode(self, mode: str) -> None:
        with self._sim_lock:
            cur = self._segments[-1].state
            if mode == "manual":
                # AD936x keeps the gain the AGC last chose when switched to manual.
                g = self._agc_gain if cur.gain_mode != "manual" else cur.gain_db
                self._push_state(gain_mode="manual", gain_db=g)
                self.gains[0] = g
            else:
                self._agc_gain = cur.gain_db
                self._push_state(gain_mode=mode)

    def _start(self, buffer_size: int) -> None:
        with self._sim_lock:
            self._check_open()
            if buffer_size <= 0:
                raise ConfigError(f"{self.name}: buffer_size pozitif olmalı")
            self._collapse_pending()
            st = self._segments[-1].state
            self._anchors = [(float(self._n_next), _perf(), st.fs_act)]
            self._pending_overflow = False
            self._agc_peak = 0.0
            self._sim_streaming = True

    def _stop(self) -> None:
        with self._sim_lock:
            if self._sim_streaming and self._p["realtime"]:
                produced = int(math.floor(self._produced_float(_perf())))
                if produced > self._n_next:          # FIFO content is discarded with the buffers
                    self._n_next = produced
            self._sim_streaming = False
            self._collapse_pending()
            self._anchors = []

    def _close(self) -> None:
        with self._sim_lock:
            self._closed = True
            self._sim_streaming = False
            self._frame_cache.clear()
            self._shape_cache.clear()

    def _read_raw(self, n: int, timeout_s: float) -> RawRead:
        if n <= 0:
            return RawRead(samples=np.zeros((1, 0), np.complex64), t_ns=_time_ns())
        if not self._p["realtime"]:
            with self._sim_lock:
                self._check_streaming()
                now = _perf()
                if self._prod_time(self._n_next) < now:
                    self._anchors.append((float(self._n_next), now, self._segments[-1].state.fs_act))
                a = self._n_next
                x = self._synthesize(a, n)
                self._n_next = a + n
                self._prune()
            return RawRead(samples=x.reshape(1, -1), t_ns=_time_ns())
        return self._read_realtime(n, timeout_s)

    # ------------------------------------------------------------------ optional features
    def set_tracking(self, dc: bool | None = None, rf_dc: bool | None = None,
                     quadrature: bool | None = None) -> dict[str, str]:
        changes: dict[str, bool] = {}
        if dc is not None:
            changes["dc_tracking"] = bool(dc)
        if rf_dc is not None:
            changes["rf_dc_tracking"] = bool(rf_dc)
        if quadrature is not None:
            changes["quad_tracking"] = bool(quadrature)
        if changes:
            with self._lock, self._sim_lock:
                self._push_state(**changes)
                self.mark_settling()
        return {k: "sim" for k, v in (("dc", dc), ("rf_dc", rf_dc), ("quadrature", quadrature)) if v is not None}

    def get_tracking(self) -> dict[str, bool]:
        st = self._segments[-1].state
        return {"dc": st.dc_tracking, "rf_dc": st.rf_dc_tracking, "quadrature": st.quad_tracking}

    def set_ppm_correction(self, ppm: float) -> str:
        if isinstance(ppm, bool) or not isinstance(ppm, (int, float)) or not math.isfinite(float(ppm)):
            raise ConfigError(f"{self.name}: ppm düzeltmesi sonlu bir sayı olmalı (geldi: {ppm!r})")
        ppm = float(ppm)
        if abs(ppm) > _MAX_PPM_CORRECTION:
            raise ConfigError(f"{self.name}: ppm düzeltmesi ±{_MAX_PPM_CORRECTION:g} aralığı dışında ({ppm:+.3f})")
        with self._lock, self._sim_lock:
            if not self._p["hw_xo_correction"]:
                self.ppm_hw, self.ppm_hw_via = 0.0, "none"
                return "none"
            self.ppm_hw, self.ppm_hw_via = ppm, "hardware"
            self._push_state(e_res=self._e_res(ppm), lo_phase=self._new_lo_phase())
            self.mark_settling()
            return "hardware"

    def read_sensors(self) -> dict[str, float | str | None]:
        """``temperature_c`` = world temperature; ``rssi_db`` = AD936x-style attenuation-like
        value: minus the mean power (dBFS) of the most recently synthesized samples."""
        p = self._last_power_dbfs
        return {"temperature_c": self.world.temperature_c, "rssi_db": None if p is None else -p}

    def runtime_limits(self) -> dict[str, Any]:
        gr = self.profile.gain_range(self._segments[-1].state.fc)
        return {"gain_db": (gr.min_db, gr.max_db),
                "sample_rate_hz": (self.profile.sample_rate_min_hz, self.profile.sample_rate_max_hz),
                "bandwidth_hz": (self.profile.bw_min_hz, self.profile.bw_max_hz)}

    def describe(self) -> dict[str, Any]:
        d = super().describe()
        d["sim"] = {
            "true_ppm": self._ppm, "residual_ppm": self.residual_ppm, "realtime": self._p["realtime"],
            "noise_figure_db": self._p["noise_figure_db"], "gain_offset_db": self._p["gain_offset_db"],
            "shared_clock": self._p["shared_clock"], "position_m": list(self.position_m),
            "tracking": self.get_tracking(), "overflow_events": self.overflow_events,
            "dropped_samples": self.dropped_samples, "seed": self._seed,
        }
        return d

    # ================================================================== state log
    def _check_open(self) -> None:
        if self._closed:
            raise DeviceError(f"{self.name}: cihaz kapatıldı")

    def _check_streaming(self) -> None:
        self._check_open()
        if not self._sim_streaming:
            raise StreamError(f"{self.name}: akış başlatılmadı")

    def _new_lo_phase(self) -> float:
        return float(self._ctl_rng.uniform(0.0, _TWO_PI)) if self._p["lo_phase_random"] else 0.0

    def _e_res(self, ppm_hw: float) -> float:
        e = self._ppm * 1e-6
        e_hw = ppm_hw * 1e-6
        step = self._p["xo_correction_step_hz"]
        ref = float(self.profile.ref_clock_hz)
        if step > 0.0 and ppm_hw != 0.0:
            xo = round(ref * (1.0 + e_hw) / step) * step
            e_hw = xo / ref - 1.0
        self.xo_correction_hz = ref * (1.0 + e_hw)
        return (1.0 + e) / (1.0 + e_hw) - 1.0

    def _stale_boundary(self) -> int:
        """First absolute index that a settings change made *now* can affect."""
        if not self._sim_streaming:
            return self._n_next
        if self._p["realtime"]:
            return self._rt_advance(_perf())
        return self._n_next + int(self.profile.kernel_buffers) * int(self.buffer_size)

    def _push_state(self, **changes: Any) -> None:
        with self._sim_lock:
            self._check_open()
            old = self._segments[-1].state
            new = dataclasses.replace(old, **changes)
            if new == old:
                return
            b = self._stale_boundary()
            self._insert_state(b, new)
            if self._sim_streaming and self._p["realtime"] and new.fs_act != old.fs_act:
                now = _perf()
                self._anchors.append((self._produced_float(now), now, new.fs_act))

    def _insert_state(self, b: int, new: _RxState) -> None:
        segs = self._segments
        while len(segs) > 1 and segs[-1].start >= b:
            segs.pop()                       # superseded pending states (new one is cumulative)
        last = segs[-1]
        if last.start >= b:
            if self._n_next == 0 and last.start == 0:
                off = int(self._p["time_offset_samples"])
                segs[-1] = _Segment(0, off / new.fs_act, new, off)
            else:
                m_off = last.m_offset if new.fs_act == last.state.fs_act else \
                    int(round(last.t_start * new.fs_act)) - last.start
                segs[-1] = _Segment(last.start, last.t_start, new, m_off)
            return
        t_b = last.t_of(b)
        m_off = last.m_offset if new.fs_act == last.state.fs_act else int(round(t_b * new.fs_act)) - b
        segs.append(_Segment(b, t_b, new, m_off))

    def _collapse_pending(self) -> None:
        """Without a FIFO (stream stopped) pending settings take effect immediately."""
        segs = self._segments
        if segs[-1].start > self._n_next:
            latest = segs[-1].state
            self._insert_state(self._n_next, latest)

    def _prune(self) -> None:
        segs = self._segments
        i = bisect.bisect_right(segs, self._n_next, key=lambda s: s.start) - 1
        if i > 0:
            del segs[:i]
        an = self._anchors
        j = bisect.bisect_right(an, float(self._n_next), key=lambda a: a[0]) - 1
        if j > 0:
            del an[:j]

    # ================================================================== production clock
    def _produced_float(self, now: float) -> float:
        n0, t0, rate = self._anchors[-1]
        return n0 + max(now - t0, 0.0) * rate

    def _rt_advance(self, now: float) -> int:
        """Account for production up to ``now``; drop the oldest samples on FIFO overflow."""
        produced = int(math.floor(self._produced_float(now)))
        cap = max(int(self.profile.kernel_buffers), 1) * int(self.buffer_size)
        level = produced - self._n_next
        if level > cap:
            drop = level - cap
            self._n_next += drop
            self.dropped_samples += drop
            if not self._pending_overflow:
                self.overflow_events += 1
            self._pending_overflow = True
        return produced

    def _prod_time(self, n: float) -> float:
        """Host perf_counter time at which absolute sample ``n`` was (or will be) produced."""
        an = self._anchors
        if not an:
            return _perf()
        i = max(bisect.bisect_right(an, n, key=lambda a: a[0]) - 1, 0)
        n0, t0, rate = an[i]
        return t0 + (n - n0) / rate

    def _first_index_at(self, t: float, lo: int, hi: int) -> int:
        """Smallest integer ``n`` in ``[lo, hi]`` with production time >= ``t`` (``hi`` if none)."""
        if self._prod_time(lo) >= t:
            return lo
        a, b = lo, hi                         # invariant: prod(a) < t, answer in (a, b]
        while b - a > 1:
            mid = (a + b) // 2
            if self._prod_time(mid) >= t:
                b = mid
            else:
                a = mid
        return b

    def _read_realtime(self, n: int, timeout_s: float) -> RawRead:
        want = min(int(n), int(self.buffer_size))
        deadline = _perf() + max(float(timeout_s), 0.0)
        while True:
            with self._sim_lock:
                self._check_streaming()
                now = _perf()
                produced = self._rt_advance(now)
                avail = produced - self._n_next
                if avail >= want:
                    break
                rate = self._anchors[-1][2]
                wait = (want - avail) / rate
            if now >= deadline:
                if avail > 0:
                    want = avail
                    break
                return RawRead(samples=np.zeros((1, 0), np.complex64), timeout=True, t_ns=_time_ns())
            _sleep(max(min(wait, deadline - now), 0.0) + 1e-6)
        with self._sim_lock:
            self._check_streaming()
            produced = self._rt_advance(_perf())
            k = min(want, produced - self._n_next)
            if k <= 0:
                return RawRead(samples=np.zeros((1, 0), np.complex64), timeout=True, t_ns=_time_ns())
            overflow = self._pending_overflow
            self._pending_overflow = False
            a = self._n_next
            self._n_next = a + k
            t_syn = _perf()
            x = self._synthesize(a, k)
            spent = _perf() - t_syn
            if spent > 0.0:                    # freeze production while we were synthesizing
                n_at = self._produced_float(t_syn)
                self._anchors.append((n_at, t_syn + spent, self._anchors[-1][2]))
            self._prune()
        _sleep(k * float(self.profile.bytes_per_sample) / self._p["usb_bytes_per_s"])
        return RawRead(samples=x.reshape(1, -1), overflow_before=overflow, t_ns=_time_ns())

    # ================================================================== synthesis
    def _synthesize(self, a: int, k: int) -> np.ndarray:
        """Complex64 samples for absolute indices ``[a, a + k)`` (caller holds the lock)."""
        out = np.empty(k, dtype=np.complex64)
        segs = self._segments
        end = a + k
        for i, seg in enumerate(segs):
            s_lo = max(a, seg.start)
            s_hi = min(end, segs[i + 1].start) if i + 1 < len(segs) else end
            if s_hi <= s_lo:
                continue
            if seg.state.gain_mode == "manual":
                pos = s_lo
                while pos < s_hi:                     # bounded temporaries for huge reads
                    stop = min(s_hi, pos + _MAX_PIECE)
                    self._synth_piece(out, a, pos, stop, seg, seg.state.gain_db)
                    pos = stop
            else:
                blk = int(self.buffer_size)
                pos = s_lo
                while pos < s_hi:
                    stop = min(s_hi, (pos // blk + 1) * blk)
                    self._synth_piece(out, a, pos, stop, seg, self._agc_gain)
                    self._agc_update(out[pos - a:stop - a], stop, blk, seg.state)
                    pos = stop
        tail = out[max(0, k - 65536):]
        self._last_power_dbfs = 10.0 * math.log10(max(float(np.vdot(tail, tail).real) / max(tail.size, 1), 1e-30))
        return out

    def _agc_update(self, chunk: np.ndarray, stop: int, blk: int, st: _RxState) -> None:
        if chunk.size:
            self._agc_peak = max(self._agc_peak, float(np.max(np.abs(chunk.view(np.float32)))))
        if stop % blk:
            return
        peak = self._agc_peak
        self._agc_peak = 0.0
        if peak >= (self._fs_counts - 1.0) / self._fs_counts:
            step = _AGC_CLIP_STEP_DB
        else:
            err = _AGC_TARGET_DBFS - 20.0 * math.log10(max(peak, 1e-12))
            step = 0.0 if abs(err) <= _AGC_HYST_DB else min(max(err, -_AGC_MAX_DOWN_DB), _AGC_MAX_UP_DB)
        if step:
            g = self.profile.gain_range(st.fc).clamp(self._agc_gain + step)
            self._agc_gain = g
            self.gains[0] = g

    def _synth_piece(self, out: np.ndarray, a: int, p_a: int, p_b: int, seg: _Segment, gain: float) -> None:
        st = seg.state
        lvl = gain + self._p["gain_offset_db"]
        buf = out[p_a - a:p_b - a]
        buf[:] = self._thermal(p_a, p_b, st, lvl)
        for q_a, q_b, scene in self._scene_pieces(p_a, p_b):
            self._add_emitters(buf[q_a - p_a:q_b - p_a], q_a, seg, lvl, scene)
        # IQ imbalance on the composite analog signal
        eps, phi = self._iq_params(st)
        r = sg.iq_image_ratio(eps, phi)
        if r != 0:
            img = np.conj(buf)
            img *= np.complex64(r)
            buf += img
        # DC offset (LO leakage / mixer) and post-ADC digital spurs
        dc_db = self._p["dc_residual_dbfs"] if st.dc_tracking else self._p["dc_offset_dbfs"]
        buf += np.complex64(10.0 ** (dc_db / 20.0) * complex(math.cos(self._dc_phase), math.sin(self._dc_phase)))
        amps, c0s, nus = [], [], []
        for s, ph in zip(self._p["spurs"], self._spur_phase):
            if abs(s["offset_hz"]) > st.fs / 2.0:
                continue
            nu = s["offset_hz"] / st.fs
            amps.append(10.0 ** (s["dbfs"] / 20.0))
            c0s.append(sg.exact_frac_product(nu, p_a) + ph)
            nus.append(nu)
        if amps:
            buf += sg.tone_bank(p_b - p_a, np.array(amps), np.array(c0s), np.array(nus))
        sg.quantize_inplace(buf, self._fs_counts)

    def _iq_params(self, st: _RxState) -> tuple[float, float]:
        if st.quad_tracking:
            return self._p["iq_residual_gain_db"], self._p["iq_residual_phase_deg"]
        return self._p["iq_gain_db"], self._p["iq_phase_deg"]

    def _scene_pieces(self, p_a: int, p_b: int) -> list[tuple[int, int, Scene]]:
        t_a = self._prod_time(p_a)
        t_b = self._prod_time(p_b - 1)
        scenes = self.world.scenes_between(t_a, t_b)
        pieces: list[tuple[int, int, Scene]] = []
        pos = p_a
        for i, sc in enumerate(scenes):
            stop = p_b if i == len(scenes) - 1 else self._first_index_at(scenes[i + 1].t_mono, pos, p_b)
            if stop > pos:
                pieces.append((pos, stop, sc))
            pos = stop
        return pieces

    # ------------------------------------------------------------------ noise frames
    def _cache_get(self, key: tuple) -> np.ndarray | None:
        v = self._frame_cache.get(key)
        if v is not None:
            self._frame_cache.move_to_end(key)
        return v

    def _cache_put(self, key: tuple, frame: np.ndarray) -> None:
        self._frame_cache[key] = frame
        self._frame_cache.move_to_end(key)
        while len(self._frame_cache) > _FRAME_CACHE_SIZE:
            self._frame_cache.popitem(last=False)

    def _frames(self, key: tuple, i_a: int, i_b: int,
                fill: Callable[[int, np.ndarray], None]) -> np.ndarray:
        """Samples ``[i_a, i_b)`` of a frame-structured process (frames aligned to multiples
        of FRAME). ``fill(frame_index, spectrum_row)`` writes one frame's spectrum; missing
        frames are inverse-transformed in one batched FFT."""
        fr_a, fr_b = i_a // sg.FRAME, (i_b - 1) // sg.FRAME
        nf = fr_b - fr_a + 1
        rows: list[np.ndarray | None] = [self._cache_get(key + (fr,)) for fr in range(fr_a, fr_b + 1)]
        missing = [j for j, r in enumerate(rows) if r is None]
        if missing:
            spec = np.zeros((len(missing), sg.FRAME), dtype=np.complex64)
            for row, j in enumerate(missing):
                fill(fr_a + j, spec[row])
            td = np.asarray(np.fft.ifft(spec, axis=1), dtype=np.complex64)
            for row, j in enumerate(missing):
                rows[j] = td[row]
            # Reads rarely end on a frame boundary: keep the (partially used) edge frames.
            for j in sorted({0, nf - 1} & set(missing)):
                self._cache_put(key + (fr_a + j,), np.array(rows[j]))
            if len(missing) == nf:                # all generated in one batch: no concatenation
                return td.reshape(-1)[i_a - fr_a * sg.FRAME:i_b - fr_a * sg.FRAME]
        if nf == 1:
            r0 = rows[0]
            assert r0 is not None
            return r0[i_a - fr_a * sg.FRAME:i_b - fr_a * sg.FRAME]
        flat = np.concatenate(rows)          # type: ignore[arg-type]
        return flat[i_a - fr_a * sg.FRAME:i_b - fr_a * sg.FRAME]

    def _shape(self, key: tuple, make: Callable[[], tuple[list[tuple[int, int]], np.ndarray]]
               ) -> tuple[list[tuple[int, int]], np.ndarray]:
        v = self._shape_cache.get(key)
        if v is None:
            v = make()
            self._shape_cache[key] = v
            while len(self._shape_cache) > 32:
                self._shape_cache.popitem(last=False)
        return v

    def _thermal(self, p_a: int, p_b: int, st: _RxState, lvl: float) -> np.ndarray:
        """Receiver noise for ``[p_a, p_b)``: input-referred thermal noise (``kT + NF``, level
        equation, shaped by the analog LPF and the digital decimation chain within
        ``+-fs/2``) plus white, gain-independent ADC transition noise (``adc_noise_lsb_rms``
        per I/Q component). Both are independent Gaussians, so they are drawn together with
        the summed per-bin variance."""
        n0_dbfs_hz = KT_DBM_HZ + self._p["noise_figure_db"] + lvl
        var_th = 10.0 ** (n0_dbfs_hz / 10.0) * st.fs                 # complex variance (unfiltered)
        sig_adc = self._p["adc_noise_lsb_rms"] / self._fs_counts      # per-component std (full scale = 1)
        var_adc = 2.0 * sig_adc * sig_adc                             # complex variance

        def make() -> tuple[list[tuple[int, int]], np.ndarray]:
            f = np.fft.fftfreq(sg.FRAME, d=1.0 / st.fs)
            h2 = self._hmag(f, st) ** 2
            per_bin = sg.FRAME / 2.0 * (var_th * h2 + var_adc)        # E|X_k|^2 / 2 per component
            idx = np.nonzero(per_bin >= (_SHAPE_FLOOR ** 2) * sg.FRAME / 2.0 * var_th)[0]
            return sg.bins_to_slices(idx), np.sqrt(per_bin[idx]).astype(np.float32)

        slices, hs = self._shape(("th", st.fs, st.bw, round(lvl, 9)), make)
        seed = self._seed

        def fill(fr: int, row: np.ndarray) -> None:
            sg.fill_gaussian_bins(sg.frame_rng(seed, 1, fr), row, slices, hs)

        return self._frames(("th", st.fs, st.bw, round(lvl, 9)), p_a, p_b, fill)

    def _envelope(self, e: Emitter, seg: _Segment, q_a: int, q_b: int, df: float, p_lin: float) -> np.ndarray:
        """Band-limited Gaussian envelope (unit carrier) of a noise-like emitter for device
        samples ``[q_a, q_b)``, drawn on the WORLD grid so devices share the realization;
        includes the analog LPF and the digital chain at the pre-alias frequency
        ``f_bin + df`` and total power ``p_lin``."""
        st = seg.state
        bw = e.bw_hz

        def make() -> tuple[list[tuple[int, int]], np.ndarray]:
            idx, m_eff = sg.noise_bins(st.fs, sg.FRAME, bw)
            f = np.fft.fftfreq(sg.FRAME, d=1.0 / st.fs)[idx]
            h = self._hmag(f + df, st)
            return sg.bins_to_slices(idx), (h * math.sqrt(sg.FRAME * sg.FRAME / m_eff / 2.0)).astype(np.float32)

        slices, hs = self._shape(("env", e.name, bw, st.fs, st.bw, df), make)
        hs = hs * np.float32(math.sqrt(p_lin))
        key_name = sg.stable_hash(e.name)
        wseed = self.world.seed
        bias = 1 << 40                        # keep frame keys non-negative for negative offsets

        def fill(fr: int, row: np.ndarray) -> None:
            sg.fill_gaussian_bins(sg.frame_rng(wseed, key_name, 2, fr + bias), row, slices, hs)

        m_a = q_a + seg.m_offset
        return self._frames(("env", e.name, bw, st.fs, st.bw, df, p_lin), m_a, m_a + (q_b - q_a), fill)

    # ------------------------------------------------------------------ emitters
    def _add_emitters(self, buf: np.ndarray, q_a: int, seg: _Segment, lvl: float, scene: Scene) -> None:
        st = seg.state
        k = buf.size
        t_a = seg.t_of(q_a)
        dn = q_a - seg.start
        skip_amp = 10.0 ** (self._skip_dbfs / 20.0)
        cw_amp: list[np.ndarray] = []
        cw_c0: list[np.ndarray] = []
        cw_nu: list[np.ndarray] = []
        wseed = self.world.seed
        for e in scene.emitters:
            if not e.enabled:
                continue
            df = e.freq_hz - st.f_lo                          # actual-Hz baseband offset (unaliased)
            nu = df / st.fs_act
            amp = 10.0 ** ((e.power_dbm + lvl) / 20.0)
            # Strongest filtered contribution of this emitter: analog LPF x digital chain at the
            # pre-alias frequency closest to DC (both responses fall monotonically with |f|).
            if e.kind in ("noise", "gsm_fcch"):
                h_max = self._hmag(max(abs(df) - e.bw_hz / 2.0, 0.0), st)
            elif e.kind == "fm":
                n_sb, j_sb = sg.fm_sidebands(e.params["dev_hz"] / e.params["mod_hz"])
                if n_sb.size <= _FM_MAX_SIDEBANDS:
                    h_sb = self._hmag(df + n_sb * e.params["mod_hz"], st)
                    sb_amp = amp * np.abs(j_sb) * h_sb
                    h_max = float(np.max(sb_amp)) / amp
                else:
                    reach = e.params["dev_hz"] + e.params["mod_hz"]
                    h_max = self._hmag(max(abs(df) - reach, 0.0), st)
            else:
                h_max = self._hmag(df, st)
            if amp * h_max < skip_amp:
                continue
            phi0 = math.radians(e.params["phase_deg"]) if "phase_deg" in e.params else \
                _TWO_PI * sg.unit_from_hash(f"{wseed}/phase/{e.name}")
            if not e.cabled:
                a = math.radians(e.aoa_deg)
                phi0 += _TWO_PI * e.freq_hz / C_LIGHT * (self._pos[0] * math.sin(a) + self._pos[1] * math.cos(a))
            phi0 -= st.lo_phase
            rot = complex(math.cos(phi0), math.sin(phi0))
            c0 = sg.exact_frac_cycles(df, seg.t_start, dn, st.fs_act)
            if e.kind == "cw":
                cw_amp.append(np.array([amp * self._hmag(df, st) * rot]))
                cw_c0.append(np.array([c0]))
                cw_nu.append(np.array([nu]))
            elif e.kind == "fm":
                dev, fmod = e.params["dev_hz"], e.params["mod_hz"]
                u0 = sg.exact_frac_cycles(fmod, t_a, 0, st.fs_act)
                beta = dev / fmod
                if n_sb.size <= _FM_MAX_SIDEBANDS:
                    # Jacobi-Anger: sine FM == sum of tones J_n(beta) at df + n*fmod, each
                    # attenuated by the analog LPF and the digital chain at its own frequency;
                    # sidebands below the skip level are dropped.
                    keep = sb_amp >= skip_amp
                    n_k = n_sb[keep]
                    ph_n = _TWO_PI * np.mod(n_k * u0, 1.0)
                    cw_amp.append(amp * j_sb[keep] * h_sb[keep] * rot * np.exp(1j * ph_n))
                    cw_c0.append(np.full(n_k.size, c0))
                    cw_nu.append((df + n_k * fmod) / st.fs_act)
                else:                         # very wide FM: direct per-sample phase
                    ph = sg.tone_bank(k, np.array([1.0]), np.array([u0]), np.array([fmod / st.fs_act])).imag
                    ph *= np.float32(beta)
                    mod = np.empty(k, dtype=np.complex64)
                    np.cos(ph, out=mod.real)
                    np.sin(ph, out=mod.imag)
                    a_c = amp * self._hmag(df, st) * rot
                    mod *= sg.tone_bank(k, np.array([a_c]), np.array([c0]), np.array([nu]))
                    buf += mod
            elif e.kind == "pulse":
                pri, pw, t0 = e.params["pri_s"], e.params["pw_s"], e.params["t0_s"]
                u0 = sg.exact_frac_cycles(1.0 / pri, t_a - t0, 0, st.fs_act)
                starts, stops = sg.gate_intervals(u0, 1.0 / (pri * st.fs_act), k, pw / pri)
                a_c = amp * self._hmag(df, st) * rot
                if pw / pri < 0.25:
                    on = sg.interval_indices(starts, stops)
                    buf[on] += sg.tone_at(on, a_c, c0, nu)
                else:
                    car = sg.tone_bank(k, np.array([a_c]), np.array([c0]), np.array([nu]))
                    car *= sg.gate_mask(starts, stops, k)
                    buf += car
            elif e.kind == "noise":
                car = sg.tone_bank(k, np.array([rot]), np.array([c0]), np.array([nu]))
                car *= self._envelope(e, seg, q_a, q_a + k, df, amp * amp)
                buf += car
            elif e.kind == "gsm_fcch":
                env = np.array(self._envelope(e, seg, q_a, q_a + k, df, amp * amp))
                period = e.params["period_frames"] * sg.GSM_FRAME_S
                u0 = sg.exact_frac_cycles(1.0 / period, t_a - e.params["t0_s"], 0, st.fs_act)
                starts, stops = sg.gate_intervals(u0, 1.0 / (period * st.fs_act), k, sg.GSM_FCCH_BURST_S / period)
                if starts.size:
                    # FCCH: the burst REPLACES the GMSK-like noise by a pure tone (same power)
                    f_off = sg.GSM_FCCH_OFFSET_HZ
                    nb = sg.interval_indices(starts, stops)
                    cf = sg.exact_frac_cycles(f_off, t_a, 0, st.fs_act)
                    a_t = amp * self._hmag(df + f_off, st)
                    env[nb] = sg.tone_at(nb, a_t, cf, f_off / st.fs_act)
                car = sg.tone_bank(k, np.array([rot]), np.array([c0]), np.array([nu]))
                car *= env
                buf += car
            else:  # pragma: no cover - Emitter validates kind
                raise ConfigError(f"{self.name}: desteklenmeyen emitter türü {e.kind!r}")
        if cw_amp:
            buf += sg.tone_bank(k, np.concatenate(cw_amp), np.concatenate(cw_c0), np.concatenate(cw_nu))
