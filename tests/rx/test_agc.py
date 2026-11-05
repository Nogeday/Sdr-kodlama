"""Tests of eds.rx.agc (control law on synthetic IQ + closed loop on the simulator)."""

from __future__ import annotations

import dataclasses
import time

import numpy as np
import pytest

from eds.core.config import AgcConfig, AppConfig, DeviceConfig, RxConfig, SimConfig
from eds.core.errors import ConfigError
from eds.core.types import IQBlock
from eds.dsp.levels import iq_levels
from eds.hal import open_device
from eds.rx import GainController, RxStreamer, classify_levels, gain_delta_db, validate_agc_config

# eds.hal.factory caches one SimWorld per id(AppConfig): keep configs alive so ids are unique.
_APPS: list[AppConfig] = []

FS = 2e6
BUF = 4096
F_TONE = 915.3e6            # 300 kHz above the 915 MHz centre (away from DC)


def sim_device(emitters=(), fs: float = FS, fc: float = 915e6, gain: float = 40.0, **sim):
    sim.setdefault("realtime", False)
    sim.setdefault("ppm", 0.0)
    app = AppConfig(devices=[DeviceConfig(name="rx0", backend="sim", sim=sim)],
                    sim=SimConfig(emitters=[dict(e) for e in emitters]))
    _APPS.append(app)
    dev = open_device(app.devices[0], app)
    dev.configure(RxConfig(center_freq_hz=fc, sample_rate_hz=fs, bandwidth_hz=fs, gain_db=gain,
                           buffer_size=BUF, block_size=BUF))
    return dev


def strong(p_dbm: float = -30.0) -> dict:
    return {"name": "big", "kind": "cw", "freq_hz": F_TONE, "power_dbm": p_dbm}


def tone(level_dbfs: float, n: int = 4096) -> np.ndarray:
    a = 10.0 ** (level_dbfs / 20.0)
    x = a * np.exp(2j * np.pi * 0.0371 * np.arange(n))
    lim = 2047.0 / 2048.0
    return (np.clip(x.real, -1.0, lim) + 1j * np.clip(x.imag, -1.0, lim)).astype(np.complex64)


def synthetic_block(dev, level_dbfs: float, seq: int, settling: bool = False) -> IQBlock:
    return IQBlock(samples=tone(level_dbfs)[None, :], fs=FS, fc=dev.fc, t_ns=0, index=seq * BUF, seq=seq,
                   gain_db=tuple(dev.gains), settling=settling)


# --------------------------------------------------------------------------- control law
def test_levels_classification_and_requested_steps():
    cfg = AgcConfig()                      # target -10, high -3, low -35, attack 10, decay 3
    ok = iq_levels(tone(-20.0))
    assert ok.peak_dbfs == pytest.approx(-20.0, abs=1e-3)
    assert classify_levels(ok, cfg) == "ok" and gain_delta_db(ok, cfg) == 0.0
    hot = iq_levels(tone(-1.0))
    assert classify_levels(hot, cfg) == "attack"
    assert gain_delta_db(hot, cfg) == pytest.approx(-10.0)          # max(10, -1 - (-10) = 9)
    clipped = iq_levels(tone(+6.0))
    assert clipped.clip_fraction > 0.1
    assert gain_delta_db(clipped, cfg) == pytest.approx(-10.0)
    deep = AgcConfig(target_peak_dbfs=-20.0, low_peak_dbfs=-45.0)
    assert gain_delta_db(hot, deep) == pytest.approx(-19.0)          # peak - target dominates
    low = iq_levels(tone(-50.0))
    assert classify_levels(low, cfg) == "low"
    assert gain_delta_db(low, cfg) == pytest.approx(3.0)             # slow decay step
    assert gain_delta_db(low, cfg, one_shot=True) == pytest.approx(40.0)
    big_decay = AgcConfig(decay_step_db=30.0)
    assert gain_delta_db(iq_levels(tone(-36.0)), big_decay) == pytest.approx(26.0)   # min(30, -10 + 36)
    # a clip fraction just above the limit attacks even with a moderate peak
    x = tone(-20.0, 100_000)
    x[:3] = 1.0 + 0j
    lv = iq_levels(x)
    assert lv.clip_fraction == pytest.approx(3e-5) and classify_levels(lv, cfg) == "attack"


@pytest.mark.parametrize("kw", [
    {"target_peak_dbfs": -40.0}, {"high_peak_dbfs": 1.0}, {"low_peak_dbfs": -5.0},
    {"max_clip_fraction": 1.0}, {"attack_step_db": 0.0}, {"decay_step_db": -1.0}, {"hold_blocks": 0},
    {"target_peak_dbfs": float("nan")},
])
def test_invalid_agc_configs_are_rejected(kw):
    with pytest.raises(ConfigError):
        validate_agc_config(AgcConfig(**kw))
    with pytest.raises(ConfigError):
        GainController(sim_device(), AgcConfig(**kw))


def test_hold_blocks_require_consecutive_low_blocks():
    dev = sim_device(gain=40.0)
    agc = GainController(dev, AgcConfig(hold_blocks=4))
    seq = 0
    for level in (-50.0, -50.0, -20.0, -50.0, -50.0, -50.0):     # the 'ok' block resets the count
        assert agc.update(synthetic_block(dev, level, seq)) is None
        seq += 1
    assert agc.low_count == 3
    assert agc.update(synthetic_block(dev, -50.0, seq, settling=True)) is None   # ignored
    assert agc.low_count == 3
    assert agc.update(synthetic_block(dev, -50.0, seq)) == 43.0                  # 4th consecutive
    assert dev.gains[0] == 43.0 and agc.history == [(seq, 43.0)] and agc.low_count == 0


def test_blocks_from_before_the_last_gain_change_are_ignored():
    dev = sim_device(gain=40.0)
    agc = GainController(dev, AgcConfig())
    blk = synthetic_block(dev, -1.0, 0)
    assert agc.update(blk) == 30.0                       # fast attack
    # the same overload still queued (recorded at 40 dB) must not trigger a second attack
    assert agc.update(dataclasses.replace(blk, seq=1)) is None
    assert dev.gains[0] == 30.0 and len(agc.history) == 1


# --------------------------------------------------------------------------- closed loop (sim)
def test_strong_emitter_fast_attack_until_no_clipping():
    dev = sim_device([strong(-30.0)], gain=60.0)         # -30 dBm + 60 - 5 = +25 dBFS: clips hard
    agc = GainController(dev, AgcConfig())
    dev.start_stream()
    evaluated = 0
    for _ in range(40):
        b = dev.read(BUF)
        if not b.settling and b.gain_db == tuple(dev.gains):
            evaluated += 1
        agc.update(b)
    gains = [g for _, g in agc.history]
    assert gains == [50.0, 40.0, 30.0]                   # 10 dB per attack until the clipping stops
    seqs = [s for s, _ in agc.history]
    # the HAL flags settle_buffers x buffer_size samples after every change; the AGC waits them out
    assert np.diff(seqs).tolist() == [dev.settle_buffers + 1] * 2
    lv = agc.last_levels
    assert lv.clip_fraction == 0.0 and lv.peak_dbfs <= AgcConfig().high_peak_dbfs
    # physics: at 30 dB the tone reads -30 + 30 - 5 = -5 dBFS
    final = dev.read(BUF)
    assert not final.settling and final.gain_db == (30.0,)
    assert iq_levels(final.samples).rms_dbfs == pytest.approx(dev.level_dbfs(-30.0, 30.0), abs=0.2)
    dev.close()


def test_weak_scene_raises_the_gain_slowly():
    dev = sim_device(gain=10.0)                           # only noise; ADC floor dominates at 10 dB
    cfg = AgcConfig()
    agc = GainController(dev, cfg)
    dev.start_stream()
    for _ in range(260):
        agc.update(dev.read(BUF))
    hist = agc.history
    assert len(hist) >= 15
    gains = [10.0] + [g for _, g in hist]
    assert all(b - a == pytest.approx(cfg.decay_step_db) for a, b in zip(gains, gains[1:]))
    seqs = [s for s, _ in hist]
    assert seqs[0] == dev.settle_buffers + cfg.hold_blocks - 1
    spacing = np.diff(seqs)
    # far below the window every hold period is exactly settle + hold blocks; near the window a
    # noise peak above low_peak_dbfs resets the hold counter, so a period can only get longer
    assert np.all(spacing[:10] == dev.settle_buffers + cfg.hold_blocks)
    assert np.all(spacing >= dev.settle_buffers + cfg.hold_blocks)
    assert agc.last_levels.peak_dbfs >= cfg.low_peak_dbfs          # reached the window ...
    assert gains[-1] <= dev.gain_range().max_db
    n_changes = len(hist)
    for _ in range(30):                                            # ... and stays there
        agc.update(dev.read(BUF))
    assert len(agc.history) == n_changes
    dev.close()


def test_settle_converges_on_a_strong_emitter():
    dev = sim_device([strong(-30.0)], gain=60.0)
    agc = GainController(dev, AgcConfig())
    g = agc.settle()
    assert g == 30.0 and agc.last_settle_status == "ok"
    lv = agc.last_levels
    assert lv.clip_fraction == 0.0 and -35.0 <= lv.peak_dbfs <= -3.0
    blk = dev.capture(16384)
    assert iq_levels(blk.samples).rms_dbfs == pytest.approx(dev.level_dbfs(-30.0, g), abs=0.2)


def test_settle_raises_the_gain_on_a_weak_scene():
    dev = sim_device(gain=10.0)
    agc = GainController(dev, AgcConfig())
    g = agc.settle()
    assert g > 10.0 and agc.last_settle_status in ("ok", "at_max_gain")
    assert g <= dev.gain_range().max_db == 73.0
    assert agc.last_levels.peak_dbfs <= AgcConfig().high_peak_dbfs and agc.last_levels.clip_fraction == 0.0
    assert len(agc.history) <= 3                          # one-shot raise, not 3 dB steps


def test_gain_never_exceeds_the_band_range_at_2_4_ghz():
    cfg = AgcConfig(low_peak_dbfs=-30.0)                  # noise peak at 71 dB stays "low"
    dev = sim_device(fs=0.6e6, fc=2.4e9, gain=40.0)
    assert dev.gain_range().max_db == 71.0
    agc = GainController(dev, cfg)
    assert agc.settle() == 71.0 and agc.last_settle_status == "at_max_gain"
    assert max(g for _, g in agc.history) == 71.0
    # continuous mode from 60 dB: +3 dB steps, clamped at 71, never above
    dev2 = sim_device(fs=0.6e6, fc=2.4e9, gain=60.0)
    agc2 = GainController(dev2, cfg)
    dev2.start_stream()
    for _ in range(60):
        agc2.update(dev2.read(BUF))
        assert dev2.gains[0] <= 71.0
    assert [g for _, g in agc2.history] == [63.0, 66.0, 69.0, 71.0]
    dev2.close()


def test_settle_reports_too_strong_input_at_minimum_gain():
    dev = sim_device([strong(+10.0)], gain=40.0)          # +10 dBm: clips even at -1 dB gain
    agc = GainController(dev, AgcConfig())
    g = agc.settle(max_iter=8)
    assert g == dev.gain_range().min_db == -1.0
    assert agc.last_settle_status == "at_min_gain"
    assert agc.last_levels.clip_fraction > 0.0


def test_settle_max_iter():
    dev = sim_device([strong(-30.0)], gain=60.0)
    agc = GainController(dev, AgcConfig())
    assert agc.settle(max_iter=1) == 50.0 and agc.last_settle_status == "max_iter"
    with pytest.raises(ConfigError):
        agc.settle(max_iter=0)


def test_hardware_agc_mode_disables_the_software_agc():
    dev = sim_device([strong(-30.0)], gain=60.0)
    dev.set_gain_mode("hw_slow")
    agc = GainController(dev, AgcConfig())
    dev.start_stream()
    for _ in range(12):
        assert agc.update(dev.read(BUF)) is None
    assert agc.history == []
    with pytest.raises(ConfigError):
        agc.settle()
    dev.close()


def test_agc_as_streamer_callback():
    dev = sim_device([strong(-30.0)], gain=60.0)
    agc = GainController(dev, AgcConfig())
    with RxStreamer(dev, block_size=BUF, queue_blocks=64, on_block=agc.update) as s:
        t_end = time.monotonic() + 5.0
        while len(agc.history) < 3 and time.monotonic() < t_end:
            time.sleep(0.002)
        n0 = s.blocks_read
        while s.blocks_read < n0 + 2 * (dev.settle_buffers + 1) and time.monotonic() < t_end:
            time.sleep(0.002)
    assert s.error is None
    assert [g for _, g in agc.history] == [50.0, 40.0, 30.0]
    late = [b for b in s.drain() if not b.settling and b.gain_db == (30.0,)]
    assert late and all(iq_levels(b.samples).clip_fraction == 0.0 for b in late)
