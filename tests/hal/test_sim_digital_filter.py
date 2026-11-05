"""SimRxDevice RX filter model: analog Butterworth x AD9363 digital decimation chain.

Ground truth = :meth:`SimRxDevice.response_db` (pre-alias baseband offset); every
measurement is compared with it numerically. Alias ghosts are measured *exactly* as the
difference between a device that sees the scene and an otherwise identical device in an
empty world (same seed -> bit-identical thermal noise, LO phases, DC and spurs), so a ghost
far below the thermal floor still has a measurable level.
"""

from __future__ import annotations

import math
import time
from pathlib import Path

import numpy as np
import pytest

from eds.core.config import load_config
from eds.core.units import KT_DBM_HZ
from eds.hal import open_device
from eds.hal.sim import Emitter, SimWorld
from eds.hal.sim.signals import butterworth_mag, digital_filter_db
from eds.rx import Sweeper
from tests.hal.sim_fixtures import make_device, make_world, psd, tone_power_db

F0 = 1e9
REPO = Path(__file__).resolve().parents[2]
RX_SIM_TOML = REPO / "config" / "rx_sim.toml"


def _scene_emitters() -> list[Emitter]:
    app = load_config(RX_SIM_TOML)
    return [Emitter(**d) for d in app.sim.emitters]


def _analytic_db(f: float, fs: float, bw: float, digital: bool = True) -> float:
    """Independent re-statement of the documented model (not the device code path)."""
    a = -10.0 * math.log10(1.0 + (abs(f) / (bw / 2.0)) ** 6)
    if not digital:
        return a
    x = min(max((abs(f) / fs - 0.40) / 0.20, 0.0), 1.0)
    return a - 80.0 * 0.5 * (1.0 - math.cos(math.pi * x))


# =========================================================================== ground truth helper
def test_response_db_matches_documented_model():
    d = make_device(make_world(), fs=20e6, bw=20e6, ppm=0.0)
    for f in (0.0, 3e6, -8e6, 9e6, -10e6, 11e6, 12e6, -20e6, 41.3e6):
        assert d.response_db(f) == pytest.approx(_analytic_db(f, 20e6, 20e6), abs=1e-9)
    assert isinstance(d.response_db(1e6), float)
    arr = d.response_db(np.array([0.0, 8e6, 10e6, 12e6]))
    assert isinstance(arr, np.ndarray) and arr.shape == (4,)
    # flat to 0.4 fs (analog only), -40 dB digital at fs/2, -80 dB digital from 0.6 fs on
    assert arr[0] == 0.0
    assert arr[1] == pytest.approx(-10 * math.log10(1 + 0.8 ** 6), abs=1e-12)
    assert arr[2] == pytest.approx(-10 * math.log10(2.0) - 40.0, abs=1e-12)
    assert arr[3] == pytest.approx(-10 * math.log10(1 + 1.2 ** 6) - 80.0, abs=1e-12)
    # response follows the commanded state (fs, bw)
    d.set_sample_rate(4e6)
    d.set_bandwidth(2e6)
    assert d.response_db(1.7e6) == pytest.approx(_analytic_db(1.7e6, 4e6, 2e6), abs=1e-9)
    off = make_device(make_world(), fs=4e6, bw=2e6, ppm=0.0, digital_filter=False)
    assert off.response_db(1.7e6) == pytest.approx(_analytic_db(1.7e6, 4e6, 2e6, digital=False), abs=1e-9)


def test_digital_filter_kernel_shape_and_parameters():
    f = np.linspace(-30e6, 30e6, 6001)
    g = digital_filter_db(f, 20e6)
    assert np.all(g <= 0.0) and np.all(g >= -80.0)
    assert np.all(g[np.abs(f) <= 8e6] == 0.0) and np.all(g[np.abs(f) >= 12e6] == -80.0)
    assert np.array_equal(g, g[::-1])                                  # even in f
    pos = g[f >= 0]
    assert np.all(np.diff(pos) <= 1e-12)                               # monotone stop-band approach
    assert digital_filter_db(10e6, 20e6) == pytest.approx(-40.0, abs=1e-12)   # taper midpoint
    assert digital_filter_db(9e6, 20e6, 0.3, 0.5, -60.0) == pytest.approx(-60.0 * 0.5 * (1 - math.cos(0.75 * math.pi)))
    assert butterworth_mag(1e6, 1e6) == pytest.approx(1 / math.sqrt(2.0))
    assert isinstance(butterworth_mag(1e6, 1e6), float)
    # custom per-device parameters reach the response
    d = make_device(make_world(), fs=10e6, bw=10e6, ppm=0.0, dig_pass_frac=0.3, dig_stop_frac=0.45,
                    dig_stop_db=-60.0)
    a = -10 * math.log10(1 + (4e6 / 5e6) ** 6)
    assert d.response_db(4e6) == pytest.approx(a - 60.0 * 0.5 * (1 - math.cos(math.pi * (0.4 - 0.3) / 0.15)))


# =========================================================================== tones
def test_tone_levels_follow_response_db_including_aliased_ones():
    fs = 2e6
    offsets = (0.30 * fs, -0.45 * fs, 0.53 * fs, -0.42 * fs)      # pre-alias; 0.53 fs folds to -0.47 fs
    w = make_world(*(Emitter(f"t{i}", F0 + f, -50.0) for i, f in enumerate(offsets)))
    d = make_device(w, fs=fs, gain=40.0, ppm=0.0)
    x = d.capture(1 << 18).samples[0]
    for f in offsets:
        alias = (f + fs / 2) % fs - fs / 2
        expected = d.level_dbfs(-50.0) + d.response_db(f)
        meas = tone_power_db(x, fs, alias, nfft=65536)
        assert meas == pytest.approx(expected, abs=0.3), f"{f / 1e3:.0f} kHz"
    assert d.response_db(offsets[2]) < -60.0                        # alias image: > 60 dB down
    assert d.response_db(offsets[0]) > -0.25                         # usable band: flat


def _difference(dev_scene, dev_empty, n: int) -> np.ndarray:
    xs = dev_scene.capture(n).samples[0].astype(np.complex128)
    xe = dev_empty.capture(n).samples[0].astype(np.complex128)
    return xs - xe


@pytest.mark.parametrize("k,delta", [(1, 1.3e6), (-1, 4.4e6), (1, -6.2e6), (-2, -2.1e6), (3, 3.7e6)])
def test_strong_emitter_at_k_fs_creates_no_alias_ghost_above_minus_60_dbc(k, delta):
    """Emitter at f_LO + k fs + delta folds onto delta (inside the usable +-0.4 fs band)."""
    fs, gain, p_dbm = 20e6, 50.0, -55.0                 # in-band it would read -10 dBFS
    f_pre = k * fs + delta

    def pair(digital: bool):
        kw = dict(fc=F0, fs=fs, gain=gain, ppm=0.0, digital_filter=digital)
        scene = make_device(make_world(Emitter("blk", F0 + f_pre, p_dbm, cabled=True)), **kw)
        empty = make_device(make_world(), **kw)
        return scene, empty

    scene, empty = pair(True)
    ref = scene.level_dbfs(p_dbm)
    expected = ref + scene.response_db(f_pre)            # ghost level (dBFS), ground truth
    diff = _difference(scene, empty, 1 << 17)
    if expected < scene.skip_level_dbfs:
        # below the skip level the emitter is not synthesized at all: bit-identical output
        assert not np.any(diff)
        assert expected - ref < -60.0
    else:
        ghost = tone_power_db(diff, fs, delta, nfft=16384)
        assert ghost - ref < -60.0
        # exact ground truth: the ghost IS the filtered emitter (difference signal; thermal noise
        # cancels, what remains are rare +-1 LSB quantizer flips = the measurement floor)
        floor = tone_power_db(diff, fs, delta + 300e3, nfft=16384)
        assert ghost == pytest.approx(10 * math.log10(10 ** (expected / 10) + 10 ** (floor / 10)), abs=1.5)
    # control: the analog filter alone lets the image through far above -60 dBc
    scene_a, empty_a = pair(False)
    ghost_a = tone_power_db(_difference(scene_a, empty_a, 1 << 17), fs, delta, nfft=16384)
    assert ghost_a - ref == pytest.approx(_analytic_db(f_pre, fs, fs, digital=False), abs=0.3)
    if abs(k) == 1:
        assert ghost_a - ref > -40.0


def test_wideband_emitter_one_fs_away_leaves_no_ghost_in_usable_band():
    fs, gain = 20e6, 50.0
    lte = Emitter("lte", F0 + fs + 3e6, -55.0, kind="noise", bw_hz=5e6, cabled=True)   # [fs+0.5, fs+5.5] MHz
    scene = make_device(make_world(lte), fs=fs, gain=gain, ppm=0.0)
    empty = make_device(make_world(), fs=fs, gain=gain, ppm=0.0)
    n = 1 << 17
    diff = scene.capture(n).samples[0].astype(np.complex128) - empty.capture(n).samples[0].astype(np.complex128)
    s = psd(diff, fs, 4096)
    ghost = s.band_power_db(0.5e6, 5.5e6)
    ref = scene.level_dbfs(-55.0)                        # its in-band power
    assert ghost - ref < -60.0
    # without the digital chain the folded band is only ~18-21 dB down
    scene_a = make_device(make_world(lte), fs=fs, gain=gain, ppm=0.0, digital_filter=False)
    empty_a = make_device(make_world(), fs=fs, gain=gain, ppm=0.0, digital_filter=False)
    diff_a = scene_a.capture(n).samples[0].astype(np.complex128) - empty_a.capture(n).samples[0].astype(np.complex128)
    assert psd(diff_a, fs, 4096).band_power_db(0.5e6, 5.5e6) - ref > -25.0


# =========================================================================== thermal noise roll-off
def test_thermal_noise_band_edge_rolloff_follows_response():
    fs, bw, gain = 4e6, 4e6, 70.0
    d = make_device(make_world(seed=11), fs=fs, bw=bw, gain=gain, ppm=0.0)
    x = d.capture(1 << 19).samples[0]
    s = psd(x, fs, 4096)
    n0 = 10 ** (d.noise_density_dbfs_hz() / 10)
    assert d.noise_density_dbfs_hz() == pytest.approx(KT_DBM_HZ + 5.0 + gain - 5.0)
    quant = 2.0 / (12.0 * 2048.0 ** 2)
    floor = (10 ** (d.adc_noise_dbfs / 10) + quant) / fs           # white ADC + quantization density

    def expected(lo: float, hi: float) -> float:
        m = (s.f >= lo) & (s.f <= hi)
        h2 = 10 ** (d.response_db(s.f[m]) / 10)
        return 10 * math.log10(float(np.mean(n0 * h2 + floor)))

    for lo, hi in ((-200e3, 200e3), (1.45e6, 1.55e6), (1.70e6, 1.75e6), (-1.80e6, -1.75e6),
                   (1.85e6, 1.90e6), (-1.95e6, -1.92e6)):
        assert s.density_db(lo, hi) == pytest.approx(expected(lo, hi), abs=0.5), f"{lo / 1e6:.2f} MHz"
    # visible roll-off: edge of the band > 20 dB below the centre (real Pluto spectra)
    assert s.density_db(-200e3, 200e3) - s.density_db(1.92e6, 1.95e6) > 20.0


# =========================================================================== skipping far emitters
def test_contributions_below_skip_level_are_not_synthesized():
    fs, gain = 2e6, 40.0
    f_pre = 0.75 * fs                                            # deep in the digital stop band
    probe = make_device(make_world(), fs=fs, gain=gain, ppm=0.0)
    assert probe.skip_level_dbfs == pytest.approx(probe.adc_noise_dbfs - 60.0)
    r = probe.response_db(f_pre)
    p_at_skip = probe.skip_level_dbfs - (gain - 5.0) - r         # dBm that lands exactly at the skip level

    def run(p_dbm: float | None) -> np.ndarray:
        w = make_world(Emitter("e", F0 + f_pre, p_dbm, cabled=True)) if p_dbm is not None else make_world()
        return make_device(w, fs=fs, gain=gain, ppm=0.0).capture(1 << 16).samples[0]

    empty = run(None)
    assert np.array_equal(run(p_at_skip - 3.0), empty)          # skipped: bit-identical output
    above = run(p_at_skip + 20.0)
    assert not np.array_equal(above, empty)                      # synthesized


def test_far_away_scene_is_skipped_entirely():
    """config/rx_sim.toml scene, LO at 3.5 GHz: nothing within reach -> exactly the empty world."""
    scene = make_device(make_world(*_scene_emitters()), fc=3.5e9, fs=20e6, gain=50.0, ppm=0.0)
    empty = make_device(make_world(), fc=3.5e9, fs=20e6, gain=50.0, ppm=0.0)
    xs = scene.capture(1 << 16).samples[0]
    assert np.array_equal(xs, empty.capture(1 << 16).samples[0])
    assert all(key[0] == "th" for key in scene._frame_cache)     # no emitter envelope was generated
    # the same scene near an emitter is synthesized (LTE B3 at 1842.5 MHz)
    near = make_device(make_world(*_scene_emitters()), fc=1840e6, fs=20e6, gain=50.0, ppm=0.0)
    near.capture(1 << 14)
    assert any(key[0] == "env" for key in near._frame_cache)


@pytest.mark.realtime
def test_per_read_cost_with_far_away_scene_equals_empty_world():
    def per_read(world: SimWorld) -> float:
        d = make_device(world, fc=3.5e9, fs=20e6, gain=50.0, buffer_size=65536)
        d.start_stream()
        d.read(1 << 16)                                          # warm-up
        best = math.inf
        for _ in range(7):
            t0 = time.perf_counter()
            d.read(1 << 16)
            best = min(best, time.perf_counter() - t0)
        return best

    t_empty = per_read(make_world())
    t_far = per_read(make_world(*_scene_emitters()))
    assert t_far < 1.3 * t_empty + 1e-3
    assert t_far < 0.020                                         # 65536 samples (3.3 ms of 20 MS/s)


@pytest.mark.realtime
def test_full_band_sweep_of_rx_sim_scene_is_fast():
    app = load_config(RX_SIM_TOML)
    app.devices[0].sim = dict(app.devices[0].sim, realtime=False)
    dev = open_device(app.devices[0], app)
    try:
        sw = Sweeper(dev, app.sweep, app.dsp, app.detect)
        assert (app.sweep.start_hz, app.sweep.stop_hz, app.sweep.sample_rate_hz) == (325e6, 3.8e9, 20e6)
        assert (app.sweep.nfft, app.sweep.averages, app.sweep.dc_avoid) == (2048, 4, True)
        t0 = time.perf_counter()
        r = sw.run()
        elapsed = time.perf_counter() - t0
    finally:
        dev.close()
    assert r.n_steps == len(sw.plan()) > 400
    assert elapsed < 8.0
