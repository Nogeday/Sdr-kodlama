"""Tests of eds.rx.sweep against the physics-based simulator (non-realtime unless marked).

Physical checks use the simulator's level equation ``dBFS = dBm + G + gain_offset_db``
(``SimRxDevice.level_dbfs``) and the ARCHITECTURE §2 band-power convention.
"""

from __future__ import annotations

import math

import numpy as np
import pytest

from eds.core.config import AppConfig, DetectConfig, DeviceConfig, DspConfig, SimConfig, SweepConfig
from eds.core.errors import ConfigError, MeasurementError, ProfileViolation
from eds.hal import open_device
from eds.rx import Sweeper, SweepResult, gain_referred_unit, plan_coverage, plan_offsets
from eds.rx.sweep import BUFFER_GRANULE, MIN_BUFFER_SIZE

# --------------------------------------------------------------------------- helpers
# eds.hal.factory caches one SimWorld per id(AppConfig); keeping every AppConfig alive for
# the session guarantees that a new config never reuses the id (and world) of a dead one.
_APPS: list[AppConfig] = []


def make_device(emitters=(), name: str = "rx0", seed: int = 1234, **sim):
    sim.setdefault("realtime", False)
    sim.setdefault("ppm", 0.0)
    app = AppConfig(devices=[DeviceConfig(name=name, backend="sim", sim=sim)],
                    sim=SimConfig(emitters=[dict(e) for e in emitters], seed=seed))
    _APPS.append(app)
    dev = open_device(app.devices[0], app)
    assert sorted(e.name for e in dev.world.current_scene().emitters) == sorted(e["name"] for e in emitters)
    return dev


def cw(name: str, f: float, p: float) -> dict:
    return {"name": name, "kind": "cw", "freq_hz": f, "power_dbm": p}


def tone_power(r: SweepResult, f: float, half_bins: int = 6) -> float:
    return r.band_power_db(f - half_bins * r.df_hz, f + half_bins * r.df_hz)


def detection_near(r: SweepResult, f: float, tol: float):
    near = [d for d in r.detections if abs(d.f_peak_hz - f) <= tol]
    return max(near, key=lambda d: d.peak_db) if near else None


def local_excess_db(r: SweepResult, f: float, half_bins: int = 3) -> float:
    """Max of p_db within +-half_bins of f, relative to the sweep's noise floor."""
    m = np.abs(r.f_hz - f) <= half_bins * r.df_hz
    return float(np.max(r.p_db[m]) - r.noise_floor_db)


# --------------------------------------------------------------------------- plan
def test_full_band_plan_is_gap_free_and_inside_official_range():
    dev = make_device()
    cfg = SweepConfig()                        # 325 MHz - 3.8 GHz, 20 MS/s, dc_avoid
    sw = Sweeper(dev, cfg, DspConfig(), DetectConfig())
    centers = np.array(sw.plan())
    assert np.all(np.diff(centers) > 0)
    assert centers[0] >= 325e6 and centers[-1] <= 3.8e9
    df = 20e6 / 2048
    assert sw.df_hz == df
    # step = U/2 in whole bins: floor(0.8 * 2048 / 2) = 819 bins
    assert sw.step_hz == 819 * df
    # every centre on the global grid start + m*df
    m = (centers - cfg.start_hz) / df
    assert np.allclose(m, np.rint(m), atol=1e-6)
    m = np.rint(m).astype(np.int64)
    n_grid = sw.n_bins
    assert n_grid == int(round((cfg.stop_hz - cfg.start_hz) / df)) + 1
    cov = plan_coverage(m, n_grid, sw.usable_half_bins, sw.dc_notch_bins, allow_notch=False)
    assert cov.all()                           # no gaps, DC notch bins never needed
    spacing = np.diff(m)
    assert spacing.max() <= 819 and spacing.min() >= 0.95 * 819
    assert len(centers) == math.ceil((n_grid - 1) / 819) + 1
    # every grid bin lies >= U/4 (minus rounding) from the DC of at least one covering step
    j = np.arange(n_grid)
    i = np.clip(np.searchsorted(m, j, side="right") - 1, 0, len(m) - 2)
    best = np.maximum(np.abs(j - m[i]), np.abs(m[i + 1] - j))
    assert best.min() >= math.floor(spacing.min() / 2)
    assert best.min() * df >= 0.24 * 0.8 * 20e6


def test_plan_without_dc_avoid_halves_the_steps_and_still_covers():
    dev = make_device()
    cfg_a = SweepConfig(start_hz=400e6, stop_hz=1400e6)
    cfg_b = SweepConfig(start_hz=400e6, stop_hz=1400e6, dc_avoid=False)
    a = Sweeper(dev, cfg_a, DspConfig(), DetectConfig())
    b = Sweeper(dev, cfg_b, DspConfig(), DetectConfig())
    assert b.step_hz == 1638 * b.df_hz          # floor(0.8 * 2048) bins = U
    assert len(b.plan()) <= len(a.plan()) // 2 + 1
    m = np.rint((np.array(b.plan()) - 400e6) / b.df_hz).astype(np.int64)
    assert plan_coverage(m, b.n_bins, b.usable_half_bins, b.dc_notch_bins, allow_notch=True).all()


def test_narrow_range_uses_one_step_with_band_clear_of_dc():
    dev = make_device()
    sw = Sweeper(dev, SweepConfig(start_hz=1000e6, stop_hz=1003e6), DspConfig(), DetectConfig())
    (c,) = sw.plan()
    off = (np.array([1000e6, 1003e6]) - c) / sw.df_hz
    assert np.all(np.abs(off) > sw.dc_notch_bins) and np.all(np.abs(off) <= sw.usable_half_bins)
    assert np.sign(off[0]) == np.sign(off[1])   # whole band on one side of DC


def test_plan_clamps_centres_at_the_profile_edges():
    dev = make_device()
    # start exactly at the profile minimum: the first centre may not go below 325 MHz
    sw = Sweeper(dev, SweepConfig(start_hz=325e6, stop_hz=380e6), DspConfig(), DetectConfig())
    c = sw.plan()
    assert min(c) >= 325e6
    m = np.rint((np.array(c) - 325e6) / sw.df_hz).astype(np.int64)
    assert plan_coverage(m, sw.n_bins, sw.usable_half_bins, sw.dc_notch_bins, allow_notch=False).all()
    sw2 = Sweeper(dev, SweepConfig(start_hz=3.75e9, stop_hz=3.8e9), DspConfig(), DetectConfig())
    c2 = sw2.plan()
    assert max(c2) <= 3.8e9
    m2 = np.rint((np.array(c2) - 3.75e9) / sw2.df_hz).astype(np.int64)
    assert plan_coverage(m2, sw2.n_bins, sw2.usable_half_bins, sw2.dc_notch_bins, allow_notch=False).all()


def test_plan_offsets_single_step_falls_back_to_other_side_band():
    # start at the official minimum (m_min = 0): the upper side band (centre below start)
    # is not allowed, the lower side band (centre above stop) is used instead
    offs = plan_offsets(n_grid=100, k_usable=819, notch=3, step_bins=819, dc_avoid=True, m_min=0, m_max=10_000)
    assert offs.tolist() == [99 + 4 + (816 - 100) // 2]


def test_rbw_overrides_nfft_and_sets_capture_and_buffer():
    dev = make_device()
    sw = Sweeper(dev, SweepConfig(start_hz=400e6, stop_hz=500e6, rbw_hz=5e3, averages=3), DspConfig(overlap=0.5),
                 DetectConfig())
    assert sw.nfft == 8192                      # 2.004 * 20e6 / 4096 = 9.8 kHz > 5 kHz >= 4.9 kHz
    assert sw.rbw_hz <= 5e3 < 2 * sw.rbw_hz
    assert sw.n_capture == 8192 + 2 * 4096
    assert sw.buffer_size % BUFFER_GRANULE == 0 and sw.buffer_size >= max(sw.n_capture, MIN_BUFFER_SIZE)


@pytest.mark.parametrize("kw, exc", [
    ({"start_hz": 300e6, "stop_hz": 400e6}, ProfileViolation),        # below 325 MHz
    ({"start_hz": 3.7e9, "stop_hz": 3.9e9}, ProfileViolation),        # above 3.8 GHz
    ({"bandwidth_hz": 30e6}, ProfileViolation),                       # > 20 MHz analog BW
    ({"gain_db": 80.0}, ProfileViolation),
    ({"start_hz": 500e6, "stop_hz": 400e6}, ConfigError),
    ({"start_hz": 500e6, "stop_hz": 500.05e6}, ConfigError),          # narrower than the CFAR window
    ({"gain_mode": "agc"}, ConfigError),
    ({"averages": 0}, ConfigError),
    ({"nfft": 32}, ConfigError),
])
def test_invalid_sweep_configs_are_rejected(kw, exc):
    dev = make_device()
    base = {"start_hz": 400e6, "stop_hz": 500e6}
    base.update(kw)
    with pytest.raises(exc):
        Sweeper(dev, SweepConfig(**base), DspConfig(), DetectConfig())


# --------------------------------------------------------------------------- spectrum content
def _offset_freqs(sw: Sweeper, k: int, offs_hz: list[float]) -> list[float]:
    c = sw.plan()[k]
    return [c + o for o in offs_hz]


def test_stitched_spectrum_shows_emitters_with_correct_frequency_and_power():
    probe = Sweeper(make_device(), SweepConfig(start_hz=700e6, stop_hz=800e6), DspConfig(), DetectConfig())
    # tones away from DC notches, at different offsets of different steps
    f1, = _offset_freqs(probe, 2, [2.3456e6])
    f2, = _offset_freqs(probe, 6, [-3.1e6])
    f3, = _offset_freqs(probe, 9, [1.07e6])
    f_noise = probe.plan()[4] + 0.5e6
    f_gsm = probe.plan()[7] - 2.6e6
    scene = [cw("strong", f1, -70.0), cw("weak", f2, -95.0), cw("mid", f3, -82.0),
             {"name": "ofdm", "kind": "noise", "freq_hz": f_noise, "power_dbm": -75.0, "bw_hz": 2e6},
             {"name": "gsm", "kind": "noise", "freq_hz": f_gsm, "power_dbm": -80.0, "bw_hz": 150e3}]
    dev = make_device(scene)
    r = Sweeper(dev, SweepConfig(start_hz=700e6, stop_hz=800e6), DspConfig(), DetectConfig()).run()
    assert r.unit == "dBFS@50dB" and r.unit == gain_referred_unit(50.0) and not r.calibrated
    assert r.f_hz[0] == 700e6 and r.f_hz[-1] == 800e6 and r.n_bins == r.p_db.size
    assert np.allclose(np.diff(r.f_hz), r.df_hz)
    assert r.valid.all() and (r.source_step >= 0).all()
    assert abs(r.rbw_hz - 2.004 * 20e6 / 2048) < 50.0
    for f, p in ((f1, -70.0), (f2, -95.0), (f3, -82.0)):
        d = detection_near(r, f, r.rbw_hz)
        assert d is not None, f"{f / 1e6:.4f} MHz tespit edilmedi"
        assert abs(d.f_peak_hz - f) <= r.rbw_hz
        expected = dev.level_dbfs(p, 50.0)
        assert abs(tone_power(r, f) - expected) <= 1.0
        assert abs(tone_power(r, f) - expected) <= 0.3         # actual accuracy of the chain
    # noise-like emitters: channel power over their bandwidth
    p_ch = r.band_power_db(f_noise - 1e6, f_noise + 1e6)
    assert abs(p_ch - dev.level_dbfs(-75.0, 50.0)) <= 0.5
    assert abs(r.band_power_db(f_gsm - 75e3, f_gsm + 75e3) - dev.level_dbfs(-80.0, 50.0)) <= 0.5
    gsm = [d for d in r.detections if d.f_lo_hz <= f_gsm <= d.f_hi_hz]
    assert len(gsm) == 1 and 0.6 * 150e3 <= gsm[0].bw_hz <= 1.5 * 150e3
    assert abs(gsm[0].band_power_db - dev.level_dbfs(-80.0, 50.0)) <= 1.0
    # noise floor matches kT + NF in the RBW, referred to 50 dB gain (median of 4-average
    # Welch noise is ~0.4 dB below the mean)
    floor_expected = dev.noise_density_dbfs_hz(50.0) + 10 * math.log10(r.rbw_hz)
    assert -1.2 <= r.noise_floor_db - floor_expected <= 0.3


def test_fixed_gain_is_clamped_per_band_and_normalized_to_reference():
    probe = Sweeper(make_device(), SweepConfig(start_hz=1260e6, stop_hz=1340e6, gain_db=73.0), DspConfig(),
                    DetectConfig())
    c = probe.plan()
    f_lo = c[1] + 2.2e6            # < 1.3 GHz: 73 dB allowed
    f_hi = c[-2] + 2.2e6           # > 1.3 GHz: max 71 dB
    assert f_lo < 1.3e9 < f_hi
    dev = make_device([cw("lo", f_lo, -95.0), cw("hi", f_hi, -95.0)])
    r = Sweeper(dev, SweepConfig(start_hz=1260e6, stop_hz=1340e6, gain_db=73.0), DspConfig(), DetectConfig()).run()
    gains = {round(s["fc"]): s["gain_db"] for s in r.steps}
    assert all(g == 73.0 for fc, g in gains.items() if fc < 1.3e9)
    assert all(g == 71.0 for fc, g in gains.items() if fc >= 1.3e9)
    assert r.unit == "dBFS@73dB"
    for f in (f_lo, f_hi):
        assert abs(tone_power(r, f) - dev.level_dbfs(-95.0, 73.0)) <= 0.3


def test_ppm_error_is_corrected_through_calibrate_callable():
    f0 = 2.4321e9
    dev = make_device([cw("ref", f0, -75.0)], ppm=11.7)
    cfg = SweepConfig(start_hz=2.40e9, stop_hz=2.46e9)
    raw = Sweeper(dev, cfg, DspConfig(), DetectConfig()).run()
    d_raw = detection_near(raw, f0, 100e3)
    assert d_raw is not None
    shift = f0 / (1 + 11.7e-6) - f0                       # apparent - true = -28.5 kHz
    assert abs(shift) > raw.rbw_hz                        # uncorrected error exceeds the RBW
    assert abs((d_raw.f_peak_hz - f0) - shift) <= raw.df_hz
    fixed = Sweeper(dev, cfg, DspConfig(), DetectConfig(),
                    calibrate=lambda s: s.with_freq_correction(dev.true_ppm)).run()
    d = detection_near(fixed, f0, fixed.rbw_hz)
    assert d is not None and abs(d.f_peak_hz - f0) <= 0.25 * fixed.df_hz
    assert fixed.unit == "dBFS@50dB"
    assert (fixed.source_step >= 0).sum() >= fixed.n_bins - 2   # global grid stays covered


def test_calibrated_sweep_reports_dbm_without_gain_normalization():
    f0 = 1.5123e9
    dev = make_device([cw("a", f0, -40.0)])
    # simulator calibration: dBm = dBFS - G - gain_offset_db (gain_offset_db = -5)
    cal = lambda s: s.with_offset_db(5.0 - s.gain_db[0], unit="dBm")  # noqa: E731
    r = Sweeper(dev, SweepConfig(start_hz=1.49e9, stop_hz=1.53e9, gain_mode="auto"), DspConfig(), DetectConfig(),
                calibrate=cal).run()
    assert r.calibrated and r.unit == "dBm"
    assert len({s["gain_db"] for s in r.steps}) > 1         # auto gain really varied
    assert abs(tone_power(r, f0) - (-40.0)) <= 0.5
    d = detection_near(r, f0, r.rbw_hz)
    assert d is not None and abs(d.band_power_db - (-40.0)) <= 1.0


def test_dc_avoid_removes_the_zero_if_dc_spike():
    dev = make_device(dc_offset_dbfs=-20.0)
    dev.set_tracking(dc=False)                 # BB DC tracking off -> -20 dBFS LO leakage
    cfg_on = SweepConfig(start_hz=600e6, stop_hz=680e6)
    cfg_off = SweepConfig(start_hz=600e6, stop_hz=680e6, dc_avoid=False)
    on = Sweeper(dev, cfg_on, DspConfig(), DetectConfig())
    r_on = on.run()
    centers = [c for c in on.plan() if 600e6 <= c <= 680e6]
    assert len(centers) >= 8
    for c in centers:
        assert local_excess_db(r_on, c) < 10.0
    assert r_on.valid.all()
    assert not any(abs(d.f_peak_hz - c) <= 5 * r_on.df_hz for d in r_on.detections for c in centers)
    # control: without dc_avoid the -20 dBFS spike lands in the result, but is marked
    # invalid and never reported as a detection
    off = Sweeper(dev, cfg_off, DspConfig(), DetectConfig())
    r_off = off.run()
    centers_off = [c for c in off.plan() if 600e6 <= c <= 680e6]
    for c in centers_off:
        assert local_excess_db(r_off, c, half_bins=0) > 40.0
        assert not r_off.valid[np.argmin(np.abs(r_off.f_hz - c))]
    assert not any(abs(d.f_peak_hz - c) <= 5 * r_off.df_hz for d in r_off.detections for c in centers_off)


def test_stale_buffers_never_leak_into_the_next_step():
    probe = Sweeper(make_device(), SweepConfig(start_hz=690e6, stop_hz=740e6), DspConfig(), DetectConfig())
    k = 2
    f_e = probe.plan()[k] + 2.0e6
    step = probe.step_hz
    ghost_freqs = [f_e + n * step for n in (1, 2) if f_e + n * step < 740e6]

    dev = make_device([cw("strong", f_e, -55.0)])
    r = Sweeper(dev, SweepConfig(start_hz=690e6, stop_hz=740e6), DspConfig(), DetectConfig()).run()
    assert abs(tone_power(r, f_e) - dev.level_dbfs(-55.0, 50.0)) <= 0.5
    for g in ghost_freqs:
        assert local_excess_db(r, g) < 10.0
        assert detection_near(r, g, 5 * r.df_hz) is None
    # control: one settle buffer short of the 4 stale kernel buffers -> step k+1 still holds
    # samples tuned to step k and the emitter shows up one step too high
    dev2 = make_device([cw("strong", f_e, -55.0)])
    short = dev2.profile.kernel_buffers - 1
    r2 = Sweeper(dev2, SweepConfig(start_hz=690e6, stop_hz=740e6, settle_buffers=short), DspConfig(),
                 DetectConfig()).run()
    assert local_excess_db(r2, ghost_freqs[0]) > 40.0


def test_auto_gain_handles_a_strong_emitter_without_clipping():
    probe = Sweeper(make_device(), SweepConfig(start_hz=880e6, stop_hz=940e6), DspConfig(), DetectConfig())
    f_big = probe.plan()[3] + 2.1e6
    f_weak = probe.plan()[5] + 3.3e6
    scene = [cw("big", f_big, -35.0), cw("weak", f_weak, -100.0)]
    dev = make_device(scene)
    fixed = Sweeper(dev, SweepConfig(start_hz=880e6, stop_hz=940e6), DspConfig(), DetectConfig()).run()
    assert fixed.clipped_steps                   # +10 dBFS at 50 dB gain: the ADC clips

    dev = make_device(scene)
    sw = Sweeper(dev, SweepConfig(start_hz=880e6, stop_hz=940e6, gain_mode="auto"), DspConfig(), DetectConfig())
    r = sw.run()
    assert r.clipped_steps == []
    assert all(s["gain_iters"] <= 2 for s in r.steps)
    assert all(s["peak_dbfs"] <= -3.0 for s in r.steps)
    assert any(s["gain_db"] < 50.0 for s in r.steps)
    gr = dev.profile.gain_range(900e6)
    assert all(gr.min_db <= s["gain_db"] <= gr.max_db for s in r.steps)
    assert r.unit == "dBFS@50dB"
    # gain-referred levels: what a linear receiver would read at 50 dB
    assert abs(tone_power(r, f_big) - dev.level_dbfs(-35.0, 50.0)) <= 1.0
    assert abs(tone_power(r, f_weak) - dev.level_dbfs(-100.0, 50.0)) <= 1.0
    assert detection_near(r, f_big, r.rbw_hz) is not None
    assert detection_near(r, f_weak, r.rbw_hz) is not None


# --------------------------------------------------------------------------- timing / bookkeeping
def test_per_step_timing_is_recorded_and_rate_computed():
    dev = make_device([cw("a", 452.1e6, -70.0)])
    sw = Sweeper(dev, SweepConfig(start_hz=420e6, stop_hz=480e6), DspConfig(), DetectConfig())
    r = sw.run()
    assert r.n_steps == len(sw.plan()) == len(r.steps)
    keys = ("fc", "gain_db", "t_tune", "t_settle", "t_capture", "t_dsp", "overflow")
    for s, fc in zip(r.steps, sw.plan()):
        assert all(k in s for k in keys)
        assert s["fc"] == pytest.approx(fc)
        assert all(s[k] >= 0.0 for k in ("t_tune", "t_settle", "t_capture", "t_dsp"))
        assert s["overflow"] is False            # non-realtime sim never overflows
    assert sum(s["bins_used"] for s in r.steps) == r.n_bins
    for k in ("tune", "settle", "capture"):
        assert r.timing[f"{k}_s"] == pytest.approx(sum(s[f"t_{k}"] for s in r.steps))
    assert r.timing["dsp_s"] >= sum(s["t_dsp"] for s in r.steps)       # + stitching/detection
    four = sum(r.timing[k] for k in ("tune_s", "settle_s", "capture_s", "dsp_s"))
    assert four <= r.duration_s and r.duration_s == pytest.approx(four + r.timing["other_s"], rel=1e-9)
    assert r.rate_hz_per_s == pytest.approx((480e6 - 420e6) / r.duration_s)
    assert r.t_end_ns >= r.t_start_ns
    assert r.step_hz == sw.step_hz
    r2 = sw.run()
    assert (r.sweep_id, r2.sweep_id) == (0, 1)


def test_device_state_is_restored_after_the_sweep():
    dev = make_device()
    dev.start_stream(65536)
    dev.set_gain_mode("hw_slow")
    settle_before = dev.settle_buffers
    sw = Sweeper(dev, SweepConfig(start_hz=420e6, stop_hz=450e6, settle_buffers=6), DspConfig(), DetectConfig())
    seen = []
    sw.run(on_step=lambda i, step, spec: seen.append((i, dev.buffer_size, dev.settle_buffers, dev.gain_mode)))
    assert seen and all(b == sw.buffer_size and sb == 6 and gm == "manual" for _, b, sb, gm in seen)
    assert dev.streaming and dev.buffer_size == 65536
    assert dev.settle_buffers == settle_before and dev.gain_mode == "hw_slow"
    assert dev.fs == 20e6 and dev.bw == 20e6
    dev.close()


def test_on_step_can_abort_and_state_is_restored():
    dev = make_device()
    sw = Sweeper(dev, SweepConfig(start_hz=420e6, stop_hz=480e6), DspConfig(), DetectConfig())
    calls = []

    def on_step(i, step, spec):
        calls.append(i)
        assert spec.fc == pytest.approx(step["fc"]) and spec.nfft == sw.nfft
        return i < 1

    with pytest.raises(MeasurementError):
        sw.run(on_step=on_step)
    assert calls == [0, 1]
    assert not dev.streaming and dev.buffer_size == dev.profile.default_buffer_size
    # the sweeper is usable again after an abort
    assert sw.run().n_steps == len(sw.plan())


def test_bad_calibrate_results_are_rejected():
    dev = make_device()
    with pytest.raises(ConfigError):
        Sweeper(dev, SweepConfig(start_hz=420e6, stop_hz=450e6), DspConfig(), DetectConfig(), calibrate=42)
    sw = Sweeper(dev, SweepConfig(start_hz=420e6, stop_hz=450e6), DspConfig(), DetectConfig(),
                 calibrate=lambda s: s.with_offset_db(0.0, unit="W"))
    with pytest.raises(ConfigError):
        sw.run()
    assert not dev.streaming


# --------------------------------------------------------------------------- result helpers / bus
def _small_result() -> tuple[SweepResult, float]:
    probe = Sweeper(make_device(), SweepConfig(start_hz=500e6, stop_hz=560e6), DspConfig(), DetectConfig())
    f = probe.plan()[2] + 1.9e6
    dev = make_device([cw("a", f, -72.0)])
    return Sweeper(dev, SweepConfig(start_hz=500e6, stop_hz=560e6), DspConfig(), DetectConfig()).run(), f


def test_decimate_is_peak_preserving_and_uniform():
    r, f = _small_result()
    fd, pd = r.decimate(500, "max")
    d = r.decimation_for(500)
    assert d == math.ceil(r.n_bins / 500) and fd.size == pd.size == math.ceil(r.n_bins / d) <= 500
    assert np.allclose(np.diff(fd), d * r.df_hz)
    assert pd.max() == pytest.approx(r.p_db.max())
    k = int(np.argmax(pd))
    assert abs(fd[k] - f) <= d * r.df_hz
    _, pmean = r.decimate(500, "mean")
    _, pmin = r.decimate(500, "min")
    assert np.all(pmin <= pmean + 1e-9) and np.all(pmean <= pd + 1e-9)
    f1, p1 = r.decimate(10 * r.n_bins)
    assert np.array_equal(p1, r.p_db) and np.array_equal(f1, r.f_hz)
    with pytest.raises(ConfigError):
        r.decimate(0)
    with pytest.raises(ConfigError):
        r.decimate(100, "median")


def test_to_spectrum_round_trip():
    r, f = _small_result()
    s = r.to_spectrum()
    assert s.unit == r.unit and s.n_bins == r.n_bins and s.rbw_hz == pytest.approx(r.rbw_hz)
    assert s.band_power_db(f - 6 * r.df_hz, f + 6 * r.df_hz) == pytest.approx(tone_power(r, f), abs=1e-9)


def test_bus_payload_validates_against_rx_sweep_schema():
    pytest.importorskip("jsonschema")
    from eds.bus.validate import validate_data

    r, f = _small_result()
    payload = r.to_bus_payload("rx0", max_points=1000)
    validate_data("rx.sweep", payload)
    d = payload["decimation"]
    assert d == math.ceil(r.n_bins / 1000)
    assert len(payload["values"]) == math.ceil(r.n_bins / d)
    assert payload["f_start_hz"] == 500e6 and payload["f_stop_hz"] == 560e6
    assert payload["f_point_step_hz"] == pytest.approx(d * r.df_hz)
    assert payload["unit"] == "dBFS@50dB" and payload["calibrated"] is False
    assert max(payload["values"]) == pytest.approx(r.p_db.max(), abs=0.006)
    assert set(payload["timing"]) == {"tune_s", "settle_s", "capture_s", "dsp_s"}
    assert any(abs(det["f_peak_hz"] - f) <= r.rbw_hz for det in payload["detections"])
    # full resolution also fits the contract
    validate_data("rx.sweep", r.to_bus_payload("rx0", max_points=r.n_bins))
    with pytest.raises(ConfigError):
        r.to_bus_payload("")


# --------------------------------------------------------------------------- realtime
@pytest.mark.realtime
def test_realtime_burst_sweep_325_500_mhz_has_plausible_timing():
    f0 = 433.92e6
    dev = make_device([cw("uav", f0, -70.0)], realtime=True)
    cfg = SweepConfig(start_hz=325e6, stop_hz=500e6)
    sw = Sweeper(dev, cfg, DspConfig(), DetectConfig())
    r = sw.run()
    assert r.n_steps == len(sw.plan())
    usb = dev.sim_params["usb_bytes_per_s"]
    bps = dev.profile.bytes_per_sample
    settle_bytes = (dev.profile.kernel_buffers + 1) * sw.buffer_size * bps
    for s in r.steps:
        # USB transfer of the flushed buffers / the burst is a hard lower bound
        assert s["t_settle"] >= 0.95 * settle_bytes / usb
        assert s["t_capture"] >= 0.95 * sw.buffer_size * bps / usb
    assert 0.0 < r.duration_s < 3.0
    assert (r.t_end_ns - r.t_start_ns) / 1e9 == pytest.approx(r.duration_s, abs=0.05)
    assert r.rate_hz_per_s == pytest.approx(175e6 / r.duration_s)
    # 20 MS/s x 4 B = 80 MB/s > 24 MB/s: the FIFO overflows between steps ...
    assert any(s["overflow"] for s in r.steps)
    # ... yet every burst is contiguous, so frequency and power are exact
    d = detection_near(r, f0, r.rbw_hz)
    assert d is not None
    assert abs(tone_power(r, f0) - dev.level_dbfs(-70.0, 50.0)) <= 1.0
    assert not dev.streaming
