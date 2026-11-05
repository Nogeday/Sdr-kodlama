"""Software AGC for the 12-bit AD9363 ADC (``rx.gain_mode = "soft_agc"``).

Why a software AGC
------------------
A 12-bit ADC gives ~74 dB between full scale and the quantization floor (I+Q, whole band).
A fixed gain is either too high (a strong emitter clips, and hard clipping spreads
harmonics/IMD over the whole spectrum) or too low (weak emitters sink into the ADC floor).
The AD936x hardware AGC solves this inside the chip, but it reports no gain value that is
synchronous with the samples, so absolute power (dBm calibration) becomes impossible.
:class:`GainController` keeps the hardware in **manual** gain mode and moves the gain itself,
so every :class:`~eds.core.types.IQBlock` carries the gain that produced it
(``IQBlock.gain_db``) and the HAL flags the blocks after each change as ``settling``.

Control law (``[agc]`` section, :class:`~eds.core.config.AgcConfig`)
------------------------------------------------------------------
Levels come from :func:`eds.dsp.levels.iq_levels` (per-rail peak ``max(|I|, |Q|)``, which is
what the ADC clips, and the fraction of clipped samples).

* **Fast attack** (immediately, one block): if ``clip_fraction > max_clip_fraction`` or
  ``peak > high_peak_dbfs`` the gain drops by ``max(attack_step_db, peak - target_peak_dbfs)``.
  A clipped block reads ``peak ~ 0 dBFS`` whatever the true input, so at least
  ``attack_step_db`` is taken and repeated attacks follow until the clipping stops.
* **Slow decay** (continuous mode, :meth:`GainController.update`): only after ``hold_blocks``
  *consecutive* blocks with ``peak < low_peak_dbfs`` the gain rises by
  ``min(decay_step_db, target - peak)`` — slow, so that bursty emitters (radar pulses, TDMA)
  that were absent from a few blocks do not pump the gain.
* **One-shot raise** (:meth:`GainController.settle`, sweep auto gain): a single capture is a
  complete measurement, so the gain rises straight to ``target - peak``. This never
  overshoots: the signal part of the peak scales with the gain while the gain-independent
  ADC noise does not, so the new peak is at most the target.

Front-end compression (``too_strong``)
-------------------------------------
Clipping is not the only overload: the AD936x LNA/mixer compress near 0 dBm input at low
gain, where their output saturates *below* ADC full scale — a too-strong input then reads a
normal peak (e.g. -4 dBFS at minimum gain in the simulator) and would pass as a good
settle. Compression is detected from a gain change between two unclipped captures of a
strong signal (higher-gain capture peak >= ``target_peak_dbfs``, lower-gain one above
-50 dBFS RMS, change >= :data:`LINEARITY_MIN_STEP_DB`): a linear receiver moves the level
by the gain change, a compressed one by clearly less. The *slope* ``ΔL / ΔG`` (the larger
of the RMS and the peak slope) below :data:`COMPRESSION_SLOPE_MAX` flags compression. In
the simulator's model the slope tends to 0.73 in deep compression (the input P1dB rises
with falling gain, so part of a gain step acts before the compressing stage) and is
~0.88 at 1 dB compression; 0.85 separates ≳ 2 dB of compression from a linear receiver
with margin for gain-step and level-estimate errors. :meth:`GainController.settle` checks
every gain change it makes and, when an attack ends in the window without such a check,
spends one *probe* capture :data:`PROBE_STEP_DB` lower (or at least
:data:`LINEARITY_MIN_STEP_DB` higher when the band minimum is too close) and restores the
gain; compressed → minimum gain and
``last_settle_status = "too_strong"``. :meth:`GainController.update` evaluates the natural
pairs of its own attacks only (no probing of a running stream) and sets
:attr:`GainController.compressed`.

The new gain is clamped/quantized to the **band-dependent** range of the device
(:meth:`~eds.hal.base.RxDevice.gain_range`, e.g. AD9363 -3..71 dB above 1.3 GHz) and applied
with :meth:`~eds.hal.base.RxDevice.set_gain`, which marks the following
``(kernel_buffers + 1) x buffer_size`` samples as settling (stale libiio buffers still hold
samples taken at the old gain). :meth:`GainController.update` ignores settling blocks and
blocks whose ``gain_db`` differs from the current device gain (blocks that were already
queued when the gain changed), so a single overload is never attacked twice.

Thread safety: :meth:`GainController.update` may run in the :class:`~eds.rx.stream.RxStreamer`
reader thread (``on_block``) while another thread reads :attr:`last_levels` /
:attr:`history`; the controller state is protected by a lock.
"""

from __future__ import annotations

import logging
import math
import threading
from collections import deque

import numpy as np

from ..core.config import AgcConfig
from ..core.errors import ConfigError
from ..core.types import IQBlock
from ..dsp.levels import IQLevels, iq_levels
from ..hal.base import RxDevice

log = logging.getLogger(__name__)

#: Level classes returned by :func:`classify_levels`.
LEVEL_CLASSES: tuple[str, ...] = ("attack", "low", "ok")

_GAIN_EQ_TOL_DB = 1e-6

#: Level change / gain change below this between two valid captures = front-end compression.
COMPRESSION_SLOPE_MAX: float = 0.85
#: Smallest gain change (dB) evaluated for compression (gain-step errors dominate below).
LINEARITY_MIN_STEP_DB: float = 3.0
#: Gain step (dB) of the verification probe in :meth:`GainController.settle`.
PROBE_STEP_DB: float = 6.0
_PROBE_HEADROOM_DB = 1.0          # upward probe: linear peak kept this far below FS when the step allows
_LINEARITY_FLOOR_DBFS = -50.0     # lower-gain capture RMS must exceed this (ADC floor negligible)


def validate_agc_config(cfg: AgcConfig) -> None:
    """Raise :class:`ConfigError` (Turkish) if ``cfg`` is not a usable AGC configuration."""
    vals = {
        "target_peak_dbfs": cfg.target_peak_dbfs, "high_peak_dbfs": cfg.high_peak_dbfs,
        "low_peak_dbfs": cfg.low_peak_dbfs, "max_clip_fraction": cfg.max_clip_fraction,
        "attack_step_db": cfg.attack_step_db, "decay_step_db": cfg.decay_step_db,
    }
    for k, v in vals.items():
        if isinstance(v, bool) or not isinstance(v, (int, float)) or not math.isfinite(float(v)):
            raise ConfigError(f"agc.{k} sonlu bir sayı olmalı, {v!r} geldi")
    if not (cfg.low_peak_dbfs < cfg.target_peak_dbfs < cfg.high_peak_dbfs <= 0.0):
        raise ConfigError(
            "agc: low_peak_dbfs < target_peak_dbfs < high_peak_dbfs <= 0 olmalı "
            f"(geldi: {cfg.low_peak_dbfs:g} / {cfg.target_peak_dbfs:g} / {cfg.high_peak_dbfs:g} dBFS)")
    if not (0.0 <= cfg.max_clip_fraction < 1.0):
        raise ConfigError(f"agc.max_clip_fraction [0, 1) aralığında olmalı, {cfg.max_clip_fraction!r} geldi")
    if cfg.attack_step_db <= 0.0 or cfg.decay_step_db <= 0.0:
        raise ConfigError("agc.attack_step_db ve agc.decay_step_db pozitif olmalı")
    if isinstance(cfg.hold_blocks, bool) or int(cfg.hold_blocks) != cfg.hold_blocks or int(cfg.hold_blocks) < 1:
        raise ConfigError(f"agc.hold_blocks pozitif tamsayı olmalı, {cfg.hold_blocks!r} geldi")


def classify_levels(levels: IQLevels, cfg: AgcConfig) -> str:
    """``"attack"`` (clipping or peak above ``high_peak_dbfs``), ``"low"`` (peak below
    ``low_peak_dbfs``) or ``"ok"``."""
    if levels.clip_fraction > cfg.max_clip_fraction or levels.peak_dbfs > cfg.high_peak_dbfs:
        return "attack"
    if levels.peak_dbfs < cfg.low_peak_dbfs:
        return "low"
    return "ok"


def gain_delta_db(levels: IQLevels, cfg: AgcConfig, *, one_shot: bool = False) -> float:
    """Requested gain change (dB) for ``levels`` before clamping; ``0.0`` = keep the gain.

    Attack: ``-max(attack_step_db, peak - target)``. Low: ``+min(decay_step_db, target -
    peak)`` (``one_shot=False``, continuous slow decay — the caller applies the
    ``hold_blocks`` rule) or ``+(target - peak)`` (``one_shot=True``)."""
    kind = classify_levels(levels, cfg)
    if kind == "attack":
        return -max(float(cfg.attack_step_db), levels.peak_dbfs - float(cfg.target_peak_dbfs))
    if kind == "low":
        want = float(cfg.target_peak_dbfs) - levels.peak_dbfs
        return want if one_shot else min(float(cfg.decay_step_db), want)
    return 0.0


def gain_slope(levels_a: IQLevels, gain_a_db: float, levels_b: IQLevels, gain_b_db: float,
               cfg: AgcConfig) -> float | None:
    """Level change per dB of gain change between two captures of the same scene, or
    ``None`` when the pair cannot tell compression: gain change below
    :data:`LINEARITY_MIN_STEP_DB`, a capture clipping (``clip_fraction >
    max_clip_fraction``), the higher-gain capture weaker than ``target_peak_dbfs`` (peak) or
    the lower-gain one near the ADC floor (RMS < -50 dBFS). The larger of the RMS-based and
    peak-based slopes is returned (both must drop for a compression verdict, which keeps a
    noise-like signal's random peak from faking one); 1.0 = linear."""
    (g_hi, hi), (g_lo, lo) = sorted(((float(gain_a_db), levels_a), (float(gain_b_db), levels_b)),
                                    key=lambda t: t[0], reverse=True)
    dg = g_hi - g_lo
    if dg < LINEARITY_MIN_STEP_DB - _GAIN_EQ_TOL_DB:
        return None
    lim = float(cfg.max_clip_fraction)
    if hi.clip_fraction > lim or lo.clip_fraction > lim:
        return None
    if hi.peak_dbfs < float(cfg.target_peak_dbfs) or lo.rms_dbfs < _LINEARITY_FLOOR_DBFS:
        return None
    vals = [(hi.rms_dbfs - lo.rms_dbfs) / dg, (hi.peak_dbfs - lo.peak_dbfs) / dg]
    if not all(math.isfinite(v) for v in vals):
        return None
    return float(max(vals))


class GainController:
    """Software AGC acting on ``device`` (manual gain mode) — see the module docstring.

    ``history`` keeps the last ``history_len`` gain changes as ``(block_seq, gain_db)``.
    """

    def __init__(self, device: RxDevice, cfg: AgcConfig, history_len: int = 1024):
        validate_agc_config(cfg)
        if isinstance(history_len, bool) or int(history_len) != history_len or int(history_len) < 1:
            raise ConfigError(f"history_len pozitif tamsayı olmalı, {history_len!r} geldi")
        self._dev = device
        self._cfg = cfg
        self._lock = threading.Lock()
        self._low_count = 0
        self._last_levels: IQLevels | None = None
        self._history: deque[tuple[int, float]] = deque(maxlen=int(history_len))
        self._mode_warned = False
        self._compressed = False
        self._last_slope: float | None = None
        self._pending: tuple[IQLevels, float] | None = None    # (levels, gain) before the last change
        self.last_settle_status: str = ""

    # ------------------------------------------------------------------ properties
    @property
    def device(self) -> RxDevice:
        return self._dev

    @property
    def cfg(self) -> AgcConfig:
        return self._cfg

    @property
    def last_levels(self) -> IQLevels | None:
        """Levels of the most recent block that was evaluated (settling blocks excluded)."""
        with self._lock:
            return self._last_levels

    @property
    def history(self) -> list[tuple[int, float]]:
        """Gain changes as ``(seq of the block that triggered it, new gain dB)``, oldest first."""
        with self._lock:
            return list(self._history)

    @property
    def low_count(self) -> int:
        """Consecutive evaluated blocks below ``low_peak_dbfs`` (slow-decay hold counter)."""
        with self._lock:
            return self._low_count

    @property
    def gain_db(self) -> float:
        """Current device gain (channel 0; the controller sets all channels together)."""
        return float(self._dev.gains[0])

    @property
    def compressed(self) -> bool:
        """The last evaluated gain change showed front-end compression (input too strong:
        an external attenuator is needed); cleared by a later linear one or :meth:`reset`."""
        with self._lock:
            return self._compressed

    @property
    def last_gain_slope(self) -> float | None:
        """Slope ``ΔL/ΔG`` of the last evaluated gain change (:func:`gain_slope`), or None."""
        with self._lock:
            return self._last_slope

    def reset(self) -> None:
        """Forget the hold counter, last levels, history and the compression state (e.g.
        after a retune)."""
        with self._lock:
            self._low_count = 0
            self._last_levels = None
            self._history.clear()
            self._compressed = False
            self._last_slope = None
            self._pending = None

    def _record_slope(self, slope: float | None) -> bool:
        """Store a pair's slope; True if it shows compression. Caller holds the lock."""
        if slope is None:
            return False
        was = self._compressed
        self._last_slope = slope
        self._compressed = slope < COMPRESSION_SLOPE_MAX
        if self._compressed and not was:
            log.warning("%s: RF ön uç sıkışması: seviyenin kazanca eğimi %.2f dB/dB (doğrusal alıcıda 1.00, "
                        "eşik %.2f) — giriş çok güçlü, harici zayıflatıcı gerekli",
                        self._dev.name, slope, COMPRESSION_SLOPE_MAX)
        return self._compressed

    # ------------------------------------------------------------------ decision
    def propose(self, levels: IQLevels, gain_db: float, *, one_shot: bool = False) -> float | None:
        """Gain the control law asks for, clamped to the device's band gain range at its
        current frequency, or ``None`` if no change results (levels fine, or the gain is
        already at the range limit in the requested direction)."""
        delta = gain_delta_db(levels, self._cfg, one_shot=one_shot)
        if delta == 0.0:
            return None
        gr = self._dev.gain_range()
        new = gr.clamp(float(gain_db) + delta)
        if abs(new - float(gain_db)) <= _GAIN_EQ_TOL_DB:
            return None
        return float(new)

    def _check_manual(self) -> bool:
        if self._dev.gain_mode == "manual":
            return True
        if not self._mode_warned:
            self._mode_warned = True
            log.warning("%s: yazılım AGC'si devre dışı — kazanç modu %r (donanım AGC'si kazancı yönetiyor)",
                        self._dev.name, self._dev.gain_mode)
        return False

    # ------------------------------------------------------------------ continuous mode
    def update(self, block: IQBlock) -> float | None:
        """Evaluate one streamed block; apply and return the new gain, or ``None``.

        Ignored (``None``, counters untouched): settling blocks, blocks whose ``gain_db``
        differs from the current device gain (queued before the last change), and every
        block while the device is not in manual gain mode.
        """
        if block.settling or not self._check_manual():
            return None
        gains = np.asarray(self._dev.gains, dtype=np.float64)
        bg = np.asarray(block.gain_db, dtype=np.float64)
        if bg.shape != gains.shape or not np.allclose(bg, gains, rtol=0.0, atol=1e-6):
            return None
        lv = iq_levels(block.samples, block.clip_level)
        with self._lock:
            self._last_levels = lv
            if self._pending is not None:              # first evaluated block after our change
                self._record_slope(gain_slope(self._pending[0], self._pending[1], lv, float(gains[0]), self._cfg))
                self._pending = None
            kind = classify_levels(lv, self._cfg)
            if kind == "ok":
                self._low_count = 0
                return None
            if kind == "low":
                self._low_count += 1
                if self._low_count < int(self._cfg.hold_blocks):
                    return None
            self._low_count = 0
            new = self.propose(lv, float(gains[0]), one_shot=False)
            if new is None:
                return None
            applied = float(self._dev.set_gain(new))
            self._history.append((int(block.seq), applied))
            self._pending = (lv, float(gains[0]))
        log.debug("%s: AGC %s → %.1f dB (tepe %.1f dBFS, kırpma %.2e)", self._dev.name, kind, applied,
                  lv.peak_dbfs, lv.clip_fraction)
        return applied

    # ------------------------------------------------------------------ one-shot mode
    def settle(self, max_iter: int = 8, n: int = 16384, timeout_s: float = 2.0) -> float:
        """Capture-and-adjust until the peak is in ``[low_peak_dbfs, high_peak_dbfs]`` without
        clipping (one-shot raise, fast attack). Returns the final gain.

        ``last_settle_status`` tells how it ended: ``"ok"`` (in window), ``"at_max_gain"``
        (still below the window at the band's maximum gain — best possible sensitivity),
        ``"at_min_gain"`` (still clipping / too high at the minimum gain: an attenuator is
        needed), ``"too_strong"`` (the front end is compressed — see the module docstring;
        the gain is set to the band minimum, an attenuator is needed) or ``"max_iter"`` (not
        converged within ``max_iter`` captures). All but the first two are logged as
        warnings. An attack that ends in the window without a compression check costs one
        extra probe capture (gain restored afterwards; :attr:`last_levels` stay those of the
        settled capture; probe gain changes are not recorded in :attr:`history`) and the
        ``too_strong`` path one capture at the minimum gain.
        Raises :class:`ConfigError` if the device is not in manual gain mode.
        """
        if isinstance(max_iter, bool) or int(max_iter) != max_iter or int(max_iter) < 1:
            raise ConfigError(f"max_iter pozitif tamsayı olmalı, {max_iter!r} geldi")
        if isinstance(n, bool) or int(n) != n or int(n) < 1:
            raise ConfigError(f"n pozitif tamsayı olmalı, {n!r} geldi")
        if self._dev.gain_mode != "manual":
            raise ConfigError(
                f"{self._dev.name}: yazılım AGC'si yalnız manuel kazanç modunda çalışır (şu an: {self._dev.gain_mode!r})")
        with self._lock:
            self._compressed = False
            self._last_slope = None
            self._pending = None
        prev: tuple[IQLevels, float] | None = None
        attacked = verified = False
        for _ in range(int(max_iter)):
            blk = self._dev.capture(int(n), timeout_s)
            lv = iq_levels(blk.samples, blk.clip_level)
            g = float(blk.gain_db[0])
            kind = classify_levels(lv, self._cfg)
            with self._lock:
                self._last_levels = lv
                self._low_count = 0
                slope = gain_slope(prev[0], prev[1], lv, g, self._cfg) if prev is not None else None
                compressed = self._record_slope(slope)
            if compressed:
                return self._too_strong(g, int(n), timeout_s, int(blk.seq))
            verified = verified or slope is not None
            if kind == "ok":
                if attacked and not verified and lv.peak_dbfs >= float(self._cfg.target_peak_dbfs) \
                        and self._probe_compressed(g, lv, int(n), timeout_s):
                    return self._too_strong(g, int(n), timeout_s, int(blk.seq))
                self.last_settle_status = "ok"
                return g
            new = self.propose(lv, g, one_shot=True)
            if new is None:
                if kind == "low":
                    self.last_settle_status = "at_max_gain"
                    log.info("%s: AGC en yüksek kazançta (%.1f dB), tepe %.1f dBFS", self._dev.name, g, lv.peak_dbfs)
                else:
                    self.last_settle_status = "at_min_gain"
                    log.warning("%s: en düşük kazançta (%.1f dB) bile tepe %.1f dBFS / kırpma %.2e — "
                                "harici zayıflatıcı gerekli", self._dev.name, g, lv.peak_dbfs, lv.clip_fraction)
                return g
            attacked = attacked or new < g
            applied = float(self._dev.set_gain(new))
            with self._lock:
                self._history.append((int(blk.seq), applied))
            prev = (lv, g)
        self.last_settle_status = "max_iter"
        log.warning("%s: AGC %d denemede yakınsamadı; kazanç %.1f dB", self._dev.name, int(max_iter), self.gain_db)
        return self.gain_db

    def _probe_compressed(self, g: float, lv: IQLevels, n: int, timeout_s: float) -> bool:
        """One capture :data:`PROBE_STEP_DB` below ``g`` — or, when the band minimum is
        closer than :data:`LINEARITY_MIN_STEP_DB`, above it (as far as 1 dB headroom of a
        linear signal allows, but at least that minimum step: a linear signal that then clips
        only makes the probe inconclusive, a compressed one cannot reach full scale) —
        compared with the settled capture ``lv``; the gain is restored to ``g``. True if the
        pair shows compression; inconclusive pairs → False."""
        gr = self._dev.gain_range()
        pg = gr.clamp(g - PROBE_STEP_DB)
        if g - pg < LINEARITY_MIN_STEP_DB - _GAIN_EQ_TOL_DB:
            step = gr.step_db if gr.step_db > 0 else 1.0
            room = max(LINEARITY_MIN_STEP_DB, min(PROBE_STEP_DB, -lv.peak_dbfs - _PROBE_HEADROOM_DB))
            pg = gr.clamp(g + math.ceil(room / step - 1e-9) * step)
            if pg - g < LINEARITY_MIN_STEP_DB - _GAIN_EQ_TOL_DB:
                log.debug("%s: AGC sıkışma kontrolü yapılamadı (kazanç %.1f dB, tepe %.1f dBFS)",
                          self._dev.name, g, lv.peak_dbfs)
                return False
        try:                                    # probe and restore: not recorded in the history
            self._dev.set_gain(pg)
            blk = self._dev.capture(n, timeout_s)
            plv = iq_levels(blk.samples, blk.clip_level)
            slope = gain_slope(lv, g, plv, float(blk.gain_db[0]), self._cfg)
        finally:
            self._dev.set_gain(g)
            with self._lock:
                self._last_levels = lv
        with self._lock:
            return self._record_slope(slope)

    def _too_strong(self, g: float, n: int, timeout_s: float, seq: int) -> float:
        """End a settle on detected compression: band-minimum gain (least compression; one
        capture there for :attr:`last_levels`), status ``"too_strong"``."""
        gmin = self._dev.gain_range().min_db
        if g > gmin + _GAIN_EQ_TOL_DB:
            applied = float(self._dev.set_gain(gmin))
            with self._lock:
                self._history.append((seq, applied))
            blk = self._dev.capture(n, timeout_s)
            with self._lock:
                self._last_levels = iq_levels(blk.samples, blk.clip_level)
            g = float(blk.gain_db[0])
        self.last_settle_status = "too_strong"
        slope = self.last_gain_slope
        log.warning("%s: giriş çok güçlü (RF ön uç sıkışmada, eğim %.2f dB/dB): kazanç en düşükte (%.1f dB), "
                    "ölçüm seviyesi doğrusal değil — harici zayıflatıcı gerekli", self._dev.name,
                    slope if slope is not None else math.nan, g)
        return g
