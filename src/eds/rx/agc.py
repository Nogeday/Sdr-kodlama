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

    def reset(self) -> None:
        """Forget the hold counter, last levels and history (e.g. after a retune)."""
        with self._lock:
            self._low_count = 0
            self._last_levels = None
            self._history.clear()

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
        log.debug("%s: AGC %s → %.1f dB (tepe %.1f dBFS, kırpma %.2e)", self._dev.name, kind, applied,
                  lv.peak_dbfs, lv.clip_fraction)
        return applied

    # ------------------------------------------------------------------ one-shot mode
    def settle(self, max_iter: int = 8, n: int = 16384, timeout_s: float = 2.0) -> float:
        """Capture-and-adjust until the peak is in ``[low_peak_dbfs, high_peak_dbfs]`` without
        clipping (one-shot raise, fast attack). Returns the final gain.

        ``last_settle_status`` tells how it ended: ``"ok"`` (in window), ``"at_max_gain"``
        (still below the window at the band's maximum gain — best possible sensitivity),
        ``"at_min_gain"`` (still too strong at the minimum gain: an attenuator is needed) or
        ``"max_iter"`` (not converged within ``max_iter`` captures). The last two are logged
        as warnings. Raises :class:`ConfigError` if the device is not in manual gain mode.
        """
        if isinstance(max_iter, bool) or int(max_iter) != max_iter or int(max_iter) < 1:
            raise ConfigError(f"max_iter pozitif tamsayı olmalı, {max_iter!r} geldi")
        if isinstance(n, bool) or int(n) != n or int(n) < 1:
            raise ConfigError(f"n pozitif tamsayı olmalı, {n!r} geldi")
        if self._dev.gain_mode != "manual":
            raise ConfigError(
                f"{self._dev.name}: yazılım AGC'si yalnız manuel kazanç modunda çalışır (şu an: {self._dev.gain_mode!r})")
        for _ in range(int(max_iter)):
            blk = self._dev.capture(int(n), timeout_s)
            lv = iq_levels(blk.samples, blk.clip_level)
            g = float(blk.gain_db[0])
            kind = classify_levels(lv, self._cfg)
            with self._lock:
                self._last_levels = lv
                self._low_count = 0
            if kind == "ok":
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
            applied = float(self._dev.set_gain(new))
            with self._lock:
                self._history.append((int(blk.seq), applied))
        self.last_settle_status = "max_iter"
        log.warning("%s: AGC %d denemede yakınsamadı; kazanç %.1f dB", self._dev.name, int(max_iter), self.gain_db)
        return self.gain_db
