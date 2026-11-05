"""SimRxDevice: quantitative physics + streaming model.

Every assertion is numerical (levels in dB, frequencies in Hz, phases in degrees, sample
indices), computed with numpy using the ARCHITECTURE spectrum conventions in
``tests.hal.sim_fixtures``.
"""

from __future__ import annotations

import math
import threading
import time

import numpy as np
import pytest

from eds.core.config import AppConfig, DeviceConfig, RxConfig, SimConfig
from eds.core.errors import ConfigError, DeviceError, StreamError
from eds.core.units import C_LIGHT, KT_DBM_HZ
from eds.hal.factory import get_sim_world, open_device, open_devices
from eds.hal.sim import Emitter, SimRxDevice, SimWorld
from tests.hal.sim_fixtures import (
    PROFILE,
    apparent_bb,
    estimate_tone_freq,
    expected_irr_db,
    install_fake_clock,
    make_device,
    make_world,
    phase_slope,
    psd,
    tone_power_db,
    wrap_deg,
)

F0 = 1e9


def _analog_db(f: float, bw: float) -> float:
    """Documented AD936x analog chain (independent of the device code): 3rd-order Butterworth
    with corner 1.4 x bw/2 times the TIA pole at 2.5 x bw/2."""
    return (-10 * math.log10(1 + (abs(f) / (0.7 * bw)) ** 6)
            - 10 * math.log10(1 + (abs(f) / (1.25 * bw)) ** 2))


# =========================================================================== levels
@pytest.mark.parametrize("p_dbm,gain", [(-40.0, 10.0), (-70.0, 40.0), (-100.0, 70.0), (-80.0, 60.0), (-60.0, 20.0)])
def test_tone_level_follows_level_equation(p_dbm, gain):
    w = make_world(Emitter("t", F0 + 100e3, p_dbm))
    d = make_device(w, gain=gain)
    x = d.capture(65536).samples[0]
    lvl = tone_power_db(x, d.fs, apparent_bb(d, F0 + 100e3))
    assert lvl == pytest.approx(p_dbm + gain - 5.0, abs=0.2)
    assert d.level_dbfs(p_dbm) == pytest.approx(p_dbm + gain - 5.0)


def test_tone_at_bin_centre_reads_power_in_single_bin():
    """ARCHITECTURE power-spectrum scaling: a bin-centred tone reads its power in one bin."""
    fs, nfft = 1e6, 4096
    w = make_world(Emitter("t", F0, -60.0))
    d = make_device(w, fs=fs, gain=40, ppm=0.0)
    f_bb = 37 * fs / nfft
    d.set_center_freq(F0 - f_bb)
    x = d.capture(nfft * 16).samples[0]
    s = psd(x, fs, nfft)
    k = int(np.argmin(np.abs(s.f - f_bb)))
    assert 10 * np.log10(s.p[k]) == pytest.approx(-25.0, abs=0.1)


def test_analog_lpf_attenuation_and_aliasing():
    """Analog chain alone (digital decimation chain off; the combined response is covered by
    tests/hal/test_sim_digital_filter.py): Butterworth at 1.4 x bw/2 x TIA pole at 2.5 x bw/2
    (RF-04: the AD936x BB LPF is calibrated to 1.4 x BBBW, so bw/2 itself is only ~-1.2 dB)."""
    fs = 2e6
    half = fs / 2.0                                      # bw = fs -> BBBW = 1 MHz
    w = make_world(Emitter("edge", F0 + half, -60.0), Emitter("alias", F0 + 1.6e6, -60.0))
    d = make_device(w, fs=fs, gain=40, ppm=0.0, digital_filter=False)
    for f in (half, 1.6e6, 1.4 * half, 3e6):
        assert d.response_db(f) == pytest.approx(_analog_db(f, fs), abs=1e-9)
    assert d.response_db(half) == pytest.approx(-1.19, abs=0.01)
    x = d.capture(65536).samples[0]
    # at fs/2 the tone sits on the Nyquist edge (+-fs/2 alias): measure both edges together
    edge = 10 * np.log10(10 ** (tone_power_db(x, fs, half, half_bw=3e3) / 10)
                         + 10 ** (tone_power_db(x, fs, -half, half_bw=3e3) / 10))
    assert edge == pytest.approx(-25.0 + _analog_db(half, fs), abs=0.25)
    # +1.6 MHz is outside +-fs/2: attenuated by |H(1.6 MHz)| then aliased to -0.4 MHz
    assert tone_power_db(x, fs, 1.6e6 - fs) == pytest.approx(-25.0 + _analog_db(1.6e6, fs), abs=0.2)
    # the corner and the TIA pole are per-device parameters
    old = make_device(make_world(), fs=fs, ppm=0.0, digital_filter=False, analog_corner_factor=1.0,
                      tia_pole_factor=0.0)
    assert old.response_db(half) == pytest.approx(-10 * math.log10(2.0), abs=1e-9)


# =========================================================================== RF compression (RF-01)
def test_input_p1db_depends_on_gain_and_band():
    d = make_device(make_world(), gain=40.0)                    # 1 GHz: full gain table -1..73 dB
    assert d.input_p1db_dbm(-1.0) == pytest.approx(0.0)
    assert d.input_p1db_dbm(73.0) == pytest.approx(-20.0)
    assert d.input_p1db_dbm(36.0) == pytest.approx(-10.0)
    assert d.input_p1db_dbm() == pytest.approx(-20.0 * 41.0 / 74.0)  # current gain
    assert d.input_p1db_dbm(-30.0) == pytest.approx(0.0)              # clamped outside the table
    p1 = d.input_p1db_dbm(0.0)
    assert d.compression_db(p1, 0.0) == pytest.approx(1.0, abs=1e-12)  # definition of P1dB
    assert d.compression_db(p1 - 10.0, 0.0) == pytest.approx(0.0127, abs=5e-4)   # Rapp p = 2
    assert d.compression_db(p1 + 10.0, 0.0) > 8.5                     # saturating
    d.set_center_freq(2.4e9)                                     # 1.3-4 GHz band: -3..71 dB
    assert d.input_p1db_dbm(-3.0) == pytest.approx(0.0) and d.input_p1db_dbm(71.0) == pytest.approx(-20.0)
    off = make_device(make_world(), rf_compression=False)
    assert off.input_p1db_dbm(0.0) == math.inf and off.compression_db(10.0, 0.0) == 0.0
    custom = make_device(make_world(), p1db_min_gain_dbm=3.0, p1db_max_gain_dbm=-25.0)
    assert custom.input_p1db_dbm(-1.0) == pytest.approx(3.0) and custom.input_p1db_dbm(73.0) == pytest.approx(-25.0)


@pytest.mark.parametrize("p_dbm", [-20.0, -6.0, 0.0, 5.0])
def test_cw_gain_compression_at_low_gain(p_dbm):
    """At low gain the front end compresses before the ADC clips (real AD936x): the tone reads
    level_dbfs - compression_db and never reaches full scale."""
    w = make_world(Emitter("t", F0 + 100e3, p_dbm))
    d = make_device(w, gain=0.0)
    x = d.capture(65536).samples[0]
    lvl = tone_power_db(x, d.fs, apparent_bb(d, F0 + 100e3))
    assert lvl == pytest.approx(d.level_dbfs(p_dbm) - d.compression_db(p_dbm), abs=0.1)
    assert np.max(np.abs(x.view(np.float32))) < 2047 / 2048          # compression, not the ADC
    if p_dbm >= 0.0:
        assert d.compression_db(p_dbm) > 1.0
    # without the compression model the same input is linear (5 dBm: 0 dBFS, on the clip edge)
    if p_dbm < 5.0:
        lin = make_device(make_world(Emitter("t", F0 + 100e3, p_dbm)), gain=0.0, rf_compression=False)
        y = lin.capture(65536).samples[0]
        assert tone_power_db(y, d.fs, apparent_bb(lin, F0 + 100e3)) == pytest.approx(lin.level_dbfs(p_dbm), abs=0.1)


def test_two_tones_near_p1db_produce_intermodulation():
    tones = (Emitter("a", F0 + 100e3, -6.0), Emitter("b", F0 + 150e3, -6.0))
    im3 = {}
    for comp in (True, False):
        d = make_device(make_world(*tones), gain=0.0, ppm=0.0, rf_compression=comp)
        x = d.capture(1 << 17).samples[0]
        im3[comp] = tone_power_db(x, d.fs, 50e3) - tone_power_db(x, d.fs, 100e3)    # 2 f1 - f2
    assert im3[True] > -50.0
    assert im3[False] < -75.0                                        # only the noise floor


def test_compression_leaves_small_signals_bit_identical():
    def run(comp: bool) -> np.ndarray:
        w = make_world(Emitter("t", F0 + 100e3, -60.0), Emitter("n", F0 - 300e3, -65.0, kind="noise", bw_hz=200e3))
        return make_device(w, gain=40.0, rf_compression=comp).capture(65536).samples[0]

    assert np.array_equal(run(True), run(False))


# =========================================================================== noise
def test_thermal_noise_density_at_high_gain():
    fs, gain = 4e6, 70.0
    d = make_device(make_world(), fs=fs, gain=gain)
    x = d.capture(1 << 17).samples[0]
    s = psd(x, fs, 4096)
    meas = 10 * math.log10(10 ** (s.density_db(-300e3, -10e3) / 10) / 2 + 10 ** (s.density_db(10e3, 300e3) / 10) / 2)
    expected = KT_DBM_HZ + 5.0 + gain - 5.0
    assert meas == pytest.approx(expected, abs=0.5)
    assert d.noise_density_dbfs_hz() == pytest.approx(expected)


def test_noise_is_shaped_by_the_analog_chain():
    fs, bw = 4e6, 2e6
    d = make_device(make_world(seed=7), fs=fs, bw=bw, gain=70.0, digital_filter=False)
    x = d.capture(1 << 19).samples[0]
    s = psd(x, fs, 4096)
    centre = s.density_db(-200e3, 200e3)
    at_bbbw = 10 * math.log10((10 ** (s.density_db(-1.05e6, -0.95e6) / 10) + 10 ** (s.density_db(0.95e6, 1.05e6) / 10)) / 2)
    assert at_bbbw - centre == pytest.approx(_analog_db(1e6, bw), abs=0.4)       # -1.2 dB
    at_corner = 10 * math.log10((10 ** (s.density_db(-1.45e6, -1.35e6) / 10) + 10 ** (s.density_db(1.35e6, 1.45e6) / 10)) / 2)
    assert at_corner - centre == pytest.approx(_analog_db(1.4e6, bw), abs=0.5)   # -3 dB - 1.1 dB TIA
    far = s.density_db(1.7e6, 1.9e6) - centre
    assert far == pytest.approx(_analog_db(1.8e6, bw), abs=1.0)


def test_noise_floor_at_minimum_gain_is_adc_limited():
    d = make_device(make_world(), fs=4e6, gain=-1.0)
    x = d.capture(1 << 16).samples[0].astype(np.complex128)
    p = 10 * np.log10(np.mean(np.abs(x - x.mean()) ** 2))
    quant = 10 * np.log10(2 * (1 / 2048) ** 2 / 12)            # ideal 12-bit quantization, complex
    expected = 10 * np.log10(10 ** (d.adc_noise_dbfs / 10) + 10 ** (quant / 10))
    assert p == pytest.approx(expected, abs=0.3)


# =========================================================================== ADC
def test_hard_clipping_and_12bit_grid():
    w = make_world(Emitter("big", F0 + 100e3, -20.0))
    d = make_device(w, gain=60.0)                          # +35 dBFS -> heavy clipping
    x = d.capture(16384).samples[0]
    iq = x.view(np.float32)
    assert iq.max() == pytest.approx(2047 / 2048) and iq.min() == -1.0
    assert np.all(np.abs(iq * 2048 - np.rint(iq * 2048)) < 1e-6)
    clip_frac = np.mean(np.maximum(np.abs(x.real), np.abs(x.imag)) >= 2047 / 2048)
    assert clip_frac > 0.5
    d.set_gain(20.0)                                       # -5 dBFS: no clipping, still on the grid
    y = d.capture(16384).samples[0].view(np.float32)
    assert np.abs(y).max() < 2047 / 2048
    assert np.all(np.abs(y * 2048 - np.rint(y * 2048)) < 1e-6)


# =========================================================================== IQ / DC / spurs
@pytest.mark.parametrize("eps,phi", [(0.4, 2.5), (1.0, 5.0), (0.2, 1.0)])
def test_image_rejection_matches_formula(eps, phi):
    f_rf = F0 + 300e3
    w = make_world(Emitter("t", f_rf, -45.0))
    d = make_device(w, gain=40, quad_tracking=False, iq_gain_db=eps, iq_phase_deg=phi)
    x = d.capture(65536).samples[0]
    fb = apparent_bb(d, f_rf)
    irr = tone_power_db(x, d.fs, fb) - tone_power_db(x, d.fs, -fb)
    assert irr == pytest.approx(expected_irr_db(eps, phi), abs=0.5)
    assert d.image_rejection_db() == pytest.approx(expected_irr_db(eps, phi), abs=1e-9)


def test_quadrature_tracking_on_gives_residual_irr():
    f_rf = F0 + 300e3
    w = make_world(Emitter("t", f_rf, -45.0))
    d = make_device(w, gain=40)
    x = d.capture(65536).samples[0]
    fb = apparent_bb(d, f_rf)
    irr = tone_power_db(x, d.fs, fb) - tone_power_db(x, d.fs, -fb)
    assert irr > 50.0
    assert irr == pytest.approx(expected_irr_db(0.02, 0.15), abs=1.5)
    # switching tracking off at run time takes effect after settling
    assert d.set_tracking(quadrature=False) == {"quadrature": "sim"}
    x = d.capture(65536).samples[0]
    irr_off = tone_power_db(x, d.fs, fb) - tone_power_db(x, d.fs, -fb)
    assert irr_off == pytest.approx(expected_irr_db(0.4, 2.5), abs=0.5)


def test_dc_offset_levels():
    d = make_device(make_world(), fs=1e6, gain=50.0, dc_tracking=False)
    x = d.capture(1 << 17).samples[0].astype(np.complex128)
    assert 20 * np.log10(abs(x.mean())) == pytest.approx(-40.0, abs=0.2)
    d.set_tracking(dc=True)
    x = d.capture(1 << 17).samples[0].astype(np.complex128)
    assert 20 * np.log10(abs(x.mean())) == pytest.approx(-80.0, abs=1.0)
    d2 = make_device(make_world(), fs=1e6, gain=50.0, dc_tracking=False, dc_offset_dbfs=-30.0)
    x2 = d2.capture(1 << 15).samples[0].astype(np.complex128)
    assert 20 * np.log10(abs(x2.mean())) == pytest.approx(-30.0, abs=0.2)


def test_spur_level_is_gain_independent_and_dropped_outside_band():
    levels = []
    for g in (20.0, 35.0):
        d = make_device(make_world(), fs=4e6, gain=g)
        x = d.capture(1 << 18).samples[0]
        levels.append(tone_power_db(x, 4e6, 1.234e6, nfft=65536))
    assert levels[0] == pytest.approx(-85.0, abs=0.5)
    assert levels[1] == pytest.approx(-85.0, abs=0.5)
    # fs = 2 MHz: the 1.234 MHz spur is outside +-fs/2 and must not appear (nor alias)
    d = make_device(make_world(), fs=2e6, gain=20.0)
    x = d.capture(1 << 18).samples[0]
    assert tone_power_db(x, 2e6, 1.234e6 - 2e6, nfft=65536) < -100.0
    # custom spurs
    d = make_device(make_world(), fs=2e6, gain=20.0, spurs=[{"offset_hz": -250e3, "dbfs": -70.0}])
    x = d.capture(1 << 17).samples[0]
    assert tone_power_db(x, 2e6, -250e3, nfft=16384) == pytest.approx(-70.0, abs=0.3)


# =========================================================================== TCXO / ppm
def test_apparent_frequency_follows_ppm():
    f_true = F0 + 123456.789
    w = make_world(Emitter("t", f_true, -60.0))
    d = make_device(w, fs=1e6, gain=40)
    assert d.true_ppm == pytest.approx(11.7)                 # first device of the world
    x = d.capture(1 << 18).samples[0]
    f_app = F0 + estimate_tone_freq(x, d.fs)
    assert f_app == pytest.approx(f_true / (1 + 11.7e-6), abs=1.0)
    assert f_app - f_true == pytest.approx(-11.7e-6 * f_true, rel=1e-3)


def test_hardware_ppm_correction_removes_offset():
    f_true = F0 + 50e3
    w = make_world(Emitter("t", f_true, -60.0))
    d = make_device(w, fs=1e6, gain=40, ppm=-8.3)
    idx_before = d._index
    assert d.set_ppm_correction(-8.3) == "hardware"                # 40 MHz x -8.3 ppm = -332 Hz exactly
    assert d.ppm_hw == pytest.approx(-8.3, abs=1e-9) and d.ppm_hw_via == "hardware"
    assert d._settle_until >= idx_before + d.settle_samples   # marked settling
    x = d.capture(1 << 18).samples[0]
    assert F0 + estimate_tone_freq(x, d.fs) == pytest.approx(f_true, abs=1.0)
    # partial correction leaves (1+e)/(1+e_hw)-1
    d.set_ppm_correction(-3.3)
    x = d.capture(1 << 18).samples[0]
    e_res = (1 - 8.3e-6) / (1 - 3.3e-6) - 1
    assert F0 + estimate_tone_freq(x, d.fs) == pytest.approx(f_true / (1 + e_res), abs=1.0)
    # devices without xo_correction report "none" and keep the offset
    d2 = make_device(make_world(Emitter("t", f_true, -60.0)), fs=1e6, gain=40, ppm=-8.3, hw_xo_correction=False)
    assert d2.set_ppm_correction(-8.3) == "none"
    assert d2.ppm_hw == 0.0 and d2.ppm_hw_via == "none"
    x = d2.capture(1 << 18).samples[0]
    assert F0 + estimate_tone_freq(x, d2.fs) == pytest.approx(f_true / (1 - 8.3e-6), abs=1.0)


def test_xo_correction_step_quantization():
    """RF-02: Pluto's xo_correction is integer Hz on 40 MHz (the default): residual <= 0.0125
    ppm, and ppm_hw reports the correction IN EFFECT (read-back), not the request, so
    ppm_hw (+) residual composes back to the true TCXO error."""
    f_true = F0 + 50e3
    d = make_device(make_world(Emitter("t", f_true, -60.0)), fs=1e6, gain=40, ppm=7.3456)
    assert d.sim_params["xo_correction_step_hz"] == 1.0
    d.set_ppm_correction(7.3456)
    xo = round(40e6 * (1 + 7.3456e-6))
    assert d.xo_correction_hz == xo
    assert d.ppm_hw == pytest.approx((xo / 40e6 - 1) * 1e6, abs=1e-12)
    assert d.ppm_hw != pytest.approx(7.3456, abs=1e-4)                  # quantised: 7.35 ppm, not 7.3456
    assert 0 < abs(d.residual_ppm) <= 0.0125 + 1e-9
    total = ((1 + d.ppm_hw * 1e-6) * (1 + d.residual_ppm * 1e-6) - 1) * 1e6
    assert total == pytest.approx(7.3456, abs=1e-9)
    blk = d.capture(1 << 18)
    assert blk.meta["ppm_hw"] == pytest.approx(d.ppm_hw, abs=1e-12)
    e_res = d.residual_ppm * 1e-6                                       # what the spectrum still shows
    assert F0 + estimate_tone_freq(blk.samples[0], d.fs) == pytest.approx(f_true / (1 + e_res), abs=1.0)
    assert abs(f_true * e_res) > 3.0                                    # visible: 0.176 Hz of 40 MHz -> 4.4 Hz
    # continuous correction (step 0) applies exactly what was asked
    c = make_device(make_world(), fs=1e6, ppm=7.3456, xo_correction_step_hz=0.0)
    c.set_ppm_correction(7.3456)
    assert c.ppm_hw == 7.3456 and abs(c.residual_ppm) < 1e-9


# =========================================================================== stale buffers
def test_stale_buffers_after_retune_non_realtime():
    f_rf = F0 + 100e3
    w = make_world(Emitter("t", f_rf, -50.0))
    d = make_device(w, fs=2e6, gain=40, buffer_size=4096, ppm=0.0)
    d.capture(4096)
    d.set_center_freq(F0 + 400e3)                        # tone moves from +100 kHz to -300 kHz
    raw = d.read(32768)
    assert raw.settling
    x = raw.samples[0]
    stale = d.profile.kernel_buffers * d.buffer_size     # 16384 samples generated with OLD fc
    f_old = estimate_tone_freq(x[:stale - 1024], d.fs)
    f_new = estimate_tone_freq(x[stale + 1024:], d.fs)
    assert f_old == pytest.approx(100e3, abs=5.0)
    assert f_new == pytest.approx(-300e3, abs=5.0)
    # exact switch index: per-sample demodulation against the old tone
    ph = np.angle(x[stale - 64:stale + 64] * np.exp(-2j * np.pi * 100e3 * np.arange(128) / d.fs))
    jump = np.nonzero(np.abs(np.diff(np.unwrap(ph))) > 0.5)[0]
    assert jump.size and jump[0] + 1 == 64
    blk = d.capture(16384)                               # capture() never returns stale data
    assert not blk.settling
    assert estimate_tone_freq(blk.samples[0], d.fs) == pytest.approx(-300e3, abs=5.0)


def test_stale_fifo_samples_after_retune_realtime(monkeypatch):
    clk = install_fake_clock(monkeypatch)
    f_rf = F0 + 100e3
    w = make_world(Emitter("t", f_rf, -50.0))
    d = make_device(w, fs=1e6, gain=40, buffer_size=4096, ppm=0.0, realtime=True)
    d.start_stream()
    clk.advance(0.010)                                    # 10000 samples wait in the FIFO
    d.set_center_freq(F0 + 400e3)
    x = d.read(16384).samples[0]
    old = x[:10000 - 512]
    new = x[10000 + 512:]
    assert estimate_tone_freq(old, d.fs) == pytest.approx(100e3, abs=10.0)
    assert estimate_tone_freq(new, d.fs) == pytest.approx(-300e3, abs=10.0)
    blk = d.capture(8192)
    assert not blk.settling and estimate_tone_freq(blk.samples[0], d.fs) == pytest.approx(-300e3, abs=10.0)


# =========================================================================== continuity
def test_waveform_independent_of_read_pattern_and_deterministic():
    def run(chunks):
        w = make_world(Emitter("t", F0 + 70e3, -50.0), Emitter("n", F0 - 300e3, -55.0, kind="noise", bw_hz=200e3),
                       Emitter("p", F0 + 500e3, -50.0, kind="pulse", params={"pri_s": 2e-4, "pw_s": 3e-5}), seed=5)
        d = make_device(w, fs=2e6, gain=40)
        d.start_stream()
        return np.concatenate([d.read(c).samples[0] for c in chunks])

    a = run([30000])
    assert np.array_equal(a, run([30000]))                  # same reads -> bit-identical
    # other read patterns: identical up to float32 rounding before the 12-bit quantizer
    # (a value landing on a rounding boundary may flip by exactly 1 LSB)
    for other in (run([10000, 10000, 10000]), run([4321] * 6 + [4074])):
        diff = np.abs(other - a).view(np.float32) * 2048
        assert diff.max() <= 1.0 + 1e-6
        assert np.count_nonzero(diff) < 1e-3 * diff.size
    # different world seed -> different emitter phases / noise jammer realization
    w = make_world(Emitter("t", F0 + 70e3, -50.0), seed=6)
    d = make_device(w, fs=2e6, gain=40)
    d.start_stream()
    assert not np.array_equal(a[:1000], d.read(1000).samples[0])


def test_carrier_phase_continuous_across_reads_and_gain_change():
    f_rf = F0 + 37e3
    w = make_world(Emitter("t", f_rf, -50.0))
    d = make_device(w, fs=1e6, gain=30, ppm=0.0)
    b1 = d.capture(8192)
    d.set_gain(40.0)                                     # gain change: same LO phase, continuous carrier
    b2 = d.capture(8192)
    def carrier_phase(blk):
        n = np.arange(blk.n) + blk.index
        z = blk.samples[0] * np.exp(-2j * np.pi * 37e3 * n / blk.fs)
        return math.degrees(np.angle(z.sum()))
    assert wrap_deg(carrier_phase(b1) - carrier_phase(b2)) == pytest.approx(0.0, abs=0.5)
    d.set_center_freq(F0 + 1e3)                          # retune: new random LO phase
    d.set_center_freq(F0)
    b3 = d.capture(8192)
    assert abs(wrap_deg(carrier_phase(b1) - carrier_phase(b3))) > 1.0


# =========================================================================== realtime / USB
def test_realtime_overflow_when_wire_rate_exceeds_usb_fake_clock(monkeypatch):
    clk = install_fake_clock(monkeypatch)
    w = make_world(Emitter("t", F0 + 100e3, -50.0), seed=3)
    d = make_device(w, fs=10e6, gain=40, buffer_size=65536, ppm=0.0, realtime=True)   # 40 MB/s > 24 MB/s
    d.start_stream()
    t0 = clk.t
    first = None
    for _ in range(20):
        blk = d.read(65536)
        if blk.overflow:
            first = blk
            break
    assert first is not None and clk.t - t0 < 0.3
    assert d.stats.overflows >= 1 and d.dropped_samples > 0
    # the waveform continues at the correct ABSOLUTE index after the gap
    start = d.abs_index - first.n
    ref = make_device(make_world(Emitter("t", F0 + 100e3, -50.0), seed=3), fs=10e6, gain=40,
                      buffer_size=65536, ppm=0.0)
    ref.start_stream()
    ref.read(start)
    assert np.array_equal(ref.read(first.n).samples[0], first.samples[0])


def test_realtime_no_overflow_below_usb_rate_fake_clock(monkeypatch):
    clk = install_fake_clock(monkeypatch)
    d = make_device(make_world(), fs=2e6, gain=40, buffer_size=65536, realtime=True)
    d.start_stream()
    t0 = clk.t
    while clk.t - t0 < 0.5:
        d.read(65536)
    assert d.stats.overflows == 0 and d.dropped_samples == 0
    # consumer pace = production pace (it waits for samples), USB transfer included
    assert d.stats.samples / (clk.t - t0) == pytest.approx(d.fs, rel=0.15)


def test_realtime_slow_consumer_overflows_fake_clock(monkeypatch):
    clk = install_fake_clock(monkeypatch)
    d = make_device(make_world(), fs=1e6, gain=40, buffer_size=4096, realtime=True)
    d.start_stream()
    d.read(4096)
    clk.advance(0.1)                                      # 100k samples > FIFO (4 x 4096)
    blk = d.read(4096)
    assert blk.overflow and d.dropped_samples == pytest.approx(100000 - 4 * 4096, abs=4200)
    assert not d.read(4096).overflow


@pytest.mark.realtime
def test_realtime_overflow_wallclock():
    d = make_device(make_world(), fs=10e6, gain=40, buffer_size=65536, realtime=True)
    d.start_stream()
    t0 = time.perf_counter()
    seen = False
    while time.perf_counter() - t0 < 2.0:
        if d.read(65536).overflow:
            seen = True
            break
    assert seen
    assert d.abs_index / d.fs < 0.3                      # within 0.3 s of (virtual) stream time
    d.stop_stream()


@pytest.mark.realtime
def test_realtime_no_overflow_wallclock():
    d = make_device(make_world(), fs=1e6, gain=40, buffer_size=65536, realtime=True)
    d.start_stream()
    t0 = time.perf_counter()
    while time.perf_counter() - t0 < 0.5:
        assert not d.read(65536).overflow
    assert d.stats.overflows == 0
    d.stop_stream()


def test_read_timeout_and_lifecycle_errors(monkeypatch):
    install_fake_clock(monkeypatch)
    d = make_device(make_world(), fs=1e6, gain=40, buffer_size=65536, realtime=True)
    with pytest.raises(StreamError):
        d.read(10)
    d.start_stream()
    from eds.core.errors import StreamTimeout
    with pytest.raises(StreamTimeout):
        d.read(65536, timeout_s=0.0)
    d.close()
    with pytest.raises(DeviceError):
        d.set_center_freq(2e9)


# =========================================================================== emitter switching
def test_emitter_switch_maps_to_exact_sample_fake_clock(monkeypatch):
    clk = install_fake_clock(monkeypatch)
    w = make_world(Emitter("t", F0 + 100e3, -40.0, enabled=False))
    d = make_device(w, fs=1e6, gain=40, buffer_size=4096, realtime=True, ppm=0.0)
    d.start_stream()
    t_start = clk.t
    n0 = d.abs_index
    clk.advance(0.0123456)
    w.set_enabled("t", True)
    x = d.read(16384).samples[0]
    on = np.abs(x) > 10 ** (-15 / 20)                    # tone at -5 dBFS, floor ~ -65 dBFS
    first_on = int(np.argmax(on))
    expected = math.ceil((0.0123456) * d.fs) + n0 - n0    # first sample produced at/after T
    assert first_on == expected
    assert on[first_on:].all() and not on[:first_on].any()
    assert t_start < clk.t


@pytest.mark.realtime
def test_emitter_switch_host_time_wallclock():
    fs, buf = 1e6, 32768
    w = make_world(Emitter("t", F0 + 100e3, -40.0, enabled=False))
    d = make_device(w, fs=fs, gain=40, buffer_size=buf, realtime=True, ppm=0.0)
    d.start_stream()
    t_switch: list[int] = []
    timer = threading.Timer(0.15, lambda: t_switch.append(w.set_enabled("t", True)))
    timer.start()
    blocks = []
    t0 = time.perf_counter()
    while time.perf_counter() - t0 < 0.35:
        blocks.append(d.read(buf))
    timer.join()
    assert t_switch and not any(b.overflow for b in blocks)
    t_on = None
    for b in blocks:
        on = np.abs(b.samples[0]) > 10 ** (-15 / 20)
        if on.any():
            t_on = b.t_ns + int(np.argmax(on) / fs * 1e9)
            break
    assert t_on is not None
    assert abs(t_on - t_switch[0]) <= buf / fs * 1e9      # +-1 buffer


# =========================================================================== multi-device coherence
def _pair(emitters, *, pos_b=(0.15, 0.0), shared="ref", lo_random=True, ppm_a=None, ppm_b=None,
          off_b=0, fs=1e6, seed=1234):
    w = make_world(*emitters, seed=seed)
    kw_a = {"shared_clock": shared, "lo_phase_random": lo_random}
    kw_b = {"shared_clock": shared, "lo_phase_random": lo_random, "position_m": list(pos_b),
            "time_offset_samples": off_b}
    if ppm_a is not None:
        kw_a["ppm"] = ppm_a
    if ppm_b is not None:
        kw_b["ppm"] = ppm_b
    a = make_device(w, "rxA", fs=fs, gain=40, **kw_a)
    b = make_device(w, "rxB", fs=fs, gain=40, **kw_b)
    a.start_stream()
    b.start_stream()
    return w, a, b


def _rel_phase_deg(xa, xb):
    return math.degrees(np.angle(np.vdot(xa, xb)))       # angle(sum xb * conj(xa))


def test_geometric_phase_difference_aoa30():
    f = F0 + 100e3
    _, a, b = _pair([Emitter("t", f, -50.0, aoa_deg=30.0)], lo_random=False)
    xa, xb = a.read(32768).samples[0], b.read(32768).samples[0]
    expected = math.degrees(2 * math.pi * 0.15 * math.sin(math.radians(30.0)) / (C_LIGHT / f))
    assert wrap_deg(_rel_phase_deg(xa, xb) - expected) == pytest.approx(0.0, abs=1.0)
    _, a, b = _pair([Emitter("t", f, -50.0, aoa_deg=30.0, cabled=True)], lo_random=False)
    assert wrap_deg(_rel_phase_deg(a.read(32768).samples[0], b.read(32768).samples[0])) == pytest.approx(0.0, abs=1.0)


def test_shared_clock_relative_phase_constant_until_retune():
    f = F0 + 150e3
    _, a, b = _pair([Emitter("t", f, -50.0, aoa_deg=30.0)])
    assert a.true_ppm == b.true_ppm
    geo = math.degrees(2 * math.pi * 0.15 * 0.5 / (C_LIGHT / f))
    ph = [wrap_deg(_rel_phase_deg(a.read(8192).samples[0], b.read(8192).samples[0]) - geo) for _ in range(6)]
    assert np.ptp(np.unwrap(np.radians(ph))) < math.radians(0.3)
    b.set_center_freq(F0 + 1e6)                         # either device retunes -> new constant
    b.set_center_freq(F0)
    a.flush()
    b.flush()
    skew = b.abs_index - a.abs_index                    # B flushed extra samples: index offset
    if skew > 0:
        a.read(skew)
    ph2 = [wrap_deg(_rel_phase_deg(a.read(8192).samples[0], b.read(8192).samples[0]) - geo) for _ in range(6)]
    assert np.ptp(np.unwrap(np.radians(ph2))) < math.radians(0.3)
    assert abs(wrap_deg(ph2[0] - ph[0])) > 1.0


def test_independent_clocks_phase_drift_rate():
    f = F0 + 50e3
    _, a, b = _pair([Emitter("t", f, -50.0)], shared="", ppm_a=0.5, ppm_b=0.4)
    xa, xb = a.read(1 << 17).samples[0], b.read(1 << 17).samples[0]
    rate = phase_slope(xa * np.conj(xb), 1e6)            # rad/s of angle(xa * conj(xb))
    ea, eb = 0.5e-6, 0.4e-6
    exact = 2 * math.pi * f * (1 / (1 + ea) - 1 / (1 + eb))
    assert rate == pytest.approx(exact, rel=1e-3)
    assert rate == pytest.approx(2 * math.pi * f * (eb - ea), rel=1e-3)   # 2 pi f (e_b - e_a)


def test_time_offset_samples_shows_in_cross_correlation():
    _, a, b = _pair([Emitter("n", F0, -50.0, kind="noise", bw_hz=400e3)], off_b=7, lo_random=False, pos_b=(0, 0))
    xa = a.read(1 << 15).samples[0].astype(np.complex128)
    xb = b.read(1 << 15).samples[0].astype(np.complex128)
    n = xa.size
    r = np.fft.ifft(np.fft.fft(xa, 2 * n) * np.conj(np.fft.fft(xb, 2 * n)))   # r[l] = sum xa[k+l] conj(xb[k])
    lag = int(np.argmax(np.abs(r)))
    lag = lag - 2 * n if lag > n else lag
    assert lag == 7
    rho = np.abs(r[7]) / math.sqrt(np.vdot(xa, xa).real * np.vdot(xb, xb).real)
    assert rho > 0.95                                    # same jammer realization at both devices


# =========================================================================== emitter kinds
def test_pulse_train_duty_power_and_coherence():
    f_rf = F0 + 200e3
    w = make_world(Emitter("p", f_rf, -50.0, kind="pulse", params={"pri_s": 1e-3, "pw_s": 1e-4}))
    d = make_device(w, fs=2e6, gain=40, ppm=0.0)
    x = d.capture(40000).samples[0]
    on = np.abs(x) > 10 ** (-25 / 20)                    # pulse at -15 dBFS, floor ~ -60 dBFS
    assert on.mean() == pytest.approx(0.1, abs=0.002)
    assert 10 * np.log10(np.mean(np.abs(x[on].astype(np.complex128)) ** 2)) == pytest.approx(-15.0, abs=0.15)
    edges = np.nonzero(np.diff(on.astype(int)) == 1)[0]
    assert np.diff(edges) == pytest.approx(2000, abs=1)
    n = np.arange(x.size)
    z = x * np.exp(-2j * np.pi * 200e3 * n / d.fs)     # coherent: one carrier across pulses
    phs = [np.angle(z[e + 20:e + 180].sum()) for e in edges]
    assert np.ptp(np.unwrap(phs)) < math.radians(1.0)


def test_fm_deviation_and_power():
    f_rf = F0 + 100e3
    w = make_world(Emitter("f", f_rf, -50.0, kind="fm", params={"dev_hz": 10e3, "mod_hz": 1e3}))
    d = make_device(w, fs=1e6, gain=40, ppm=0.0)
    x = d.capture(1 << 16).samples[0].astype(np.complex128)
    raw = np.angle(x[1:] * np.conj(x[:-1])) * d.fs / (2 * np.pi)
    inst = np.convolve(raw, np.ones(25) / 25, mode="valid")    # ~360 Hz rms per-sample noise -> ~70 Hz
    assert inst.max() == pytest.approx(110e3, abs=300) and inst.min() == pytest.approx(90e3, abs=300)
    assert inst.mean() == pytest.approx(100e3, abs=50)
    assert 10 * np.log10(np.mean(np.abs(x) ** 2)) == pytest.approx(-15.0, abs=0.1)
    sp = np.abs(np.fft.rfft(inst - inst.mean()))
    k = int(np.argmax(sp))
    assert k * d.fs / inst.size == pytest.approx(1e3, abs=d.fs / inst.size)


def test_noise_emitter_band_power_and_shape():
    w = make_world(Emitter("n", F0 + 200e3, -60.0, kind="noise", bw_hz=400e3))
    d = make_device(w, fs=2e6, gain=40, ppm=0.0)
    x = d.capture(1 << 17).samples[0]
    s = psd(x, d.fs, 4096)
    assert s.band_power_db(0.0, 400e3) == pytest.approx(-25.0, abs=0.3)
    inband = s.density_db(50e3, 350e3)
    assert inband == pytest.approx(-25.0 - 10 * math.log10(400e3), abs=0.3)
    assert s.density_db(-700e3, -300e3) < inband - 30.0


def test_noise_emitter_has_no_frame_splice_skirts():
    """RF-05: consecutive noise frames are crossfaded, so a 10 MHz LTE-like emitter leaves no
    spectral skirt outside its band (independent frames spliced end to end gave skirts only
    ~30 dB below the in-band density). Thermal noise cancels in the scene - empty difference."""
    fs = 20e6
    kw = dict(fc=1.8e9, fs=fs, bw=fs, gain=20.0, ppm=0.0, spurs=[])
    scene = make_device(make_world(Emitter("lte", 1.8e9, -30.0, kind="noise", bw_hz=10e6)), **kw)
    empty = make_device(make_world(), **kw)
    n = 1 << 20
    diff = scene.capture(n).samples[0].astype(np.complex128) - empty.capture(n).samples[0].astype(np.complex128)
    s = psd(diff, fs, 2048)
    inband = s.density_db(-4e6, 4e6)
    assert inband == pytest.approx(-30.0 + 20.0 - 5.0 - 70.0, abs=0.3)          # -85 dBFS/Hz
    for lo, hi in ((5.3e6, 6e6), (7e6, 8e6), (-8e6, -5.3e6)):
        assert s.density_db(lo, hi) < inband - 55.0, (lo, hi)


def test_noise_frame_crossfade_keeps_the_variance_stationary():
    from eds.hal.sim.signals import FRAME_HOP, FRAME_XFADE

    d = make_device(make_world(Emitter("n", F0, -40.0, kind="noise", bw_hz=1.6e6), seed=3), fs=2e6, gain=40.0,
                    ppm=0.0, spurs=[])
    x = d.capture(FRAME_HOP * 64).samples[0].astype(np.complex128)
    pos = (d.abs_index - x.size + np.arange(x.size)) % FRAME_HOP               # world index = device index
    p = np.abs(x) ** 2
    fade = np.mean(p[pos < FRAME_XFADE])
    mid = np.mean(p[(pos >= FRAME_XFADE // 4) & (pos < 3 * FRAME_XFADE // 4)])   # deepest part of the fade
    rest = np.mean(p[pos >= FRAME_XFADE])
    assert 10 * np.log10(fade / rest) == pytest.approx(0.0, abs=0.1)
    assert 10 * np.log10(mid / rest) == pytest.approx(0.0, abs=0.15)
    assert 10 * np.log10(rest) == pytest.approx(-5.0, abs=0.4)                # -40 dBm + 40 - 5 (band-edge droop)


def test_gsm_fcch_bursts():
    f_rf = 935.2e6
    w = make_world(Emitter("bcch", f_rf, -50.0, kind="gsm_fcch"))
    d = make_device(w, fc=f_rf - 100e3, fs=1e6, gain=40, ppm=0.0)
    x = d.capture(100000).samples[0].astype(np.complex128)           # 100 ms -> 2 bursts
    blk_start = d.abs_index - x.size                                  # world t(n) = n / fs (ppm 0, offset 0)
    p_lin = 10 ** (-15 / 10)
    assert 10 * np.log10(np.mean(np.abs(x) ** 2)) == pytest.approx(-15.0, abs=0.3)
    s = psd(x, d.fs, 4096)                                            # GMSK-like part: ~200 kHz wide
    assert s.band_power_db(0.0, 200e3) == pytest.approx(-15.0, abs=0.3)
    f_tone = 100e3 + 1625e3 / 24
    z = x * np.exp(-2j * np.pi * f_tone * np.arange(x.size) / d.fs)
    period = 10 * 120e-3 / 26 * d.fs                                  # 10 TDMA frames (46.15 ms)
    burst = 148 * 48e-6 / 13 * d.fs                                   # 546.46 us
    k0 = math.ceil(blk_start / period)
    found = 0
    for k in (k0, k0 + 1):
        b0 = math.ceil(k * period) - blk_start                        # first sample with t >= k*period
        b1 = math.ceil(k * period + burst) - blk_start
        if b1 + 50 > x.size:
            continue
        core = z[b0 + 5:b1 - 5]
        c = core.mean()
        assert 10 * np.log10(abs(c) ** 2) == pytest.approx(-15.0, abs=0.2)        # same power as the noise
        assert np.std(core) / abs(c) < 0.01                                        # pure tone at fc + 67.708 kHz
        inside = np.abs(z[b0 - 50:b1 + 50] - c) ** 2 < p_lin / 30
        # burst start = first run of 8 samples on the tone (a single noise sample may touch it)
        first = int(np.argmax(np.convolve(inside, np.ones(8, dtype=int), "valid") == 8))
        length = int(np.argmin(inside[first:]))                                    # contiguous run
        assert first - 50 == pytest.approx(0, abs=1)
        assert length == pytest.approx(b1 - b0, abs=1)
        found += 1
    assert found == 2
    seg = x[math.ceil(k0 * period) - blk_start + 20:math.ceil(k0 * period + burst) - blk_start - 20]
    assert estimate_tone_freq(seg, d.fs, block=16) == pytest.approx(f_tone, abs=30)


# =========================================================================== AGC / gain
def test_hw_agc_converges_to_minus12_dbfs_peak():
    w = make_world(Emitter("t", F0 + 100e3, -40.0))
    d = make_device(w, fs=1e6, gain=70.0, buffer_size=4096)
    d.set_gain_mode("hw_slow")
    d.start_stream()
    for _ in range(40):
        blk = d.read(4096)
    peak = 20 * np.log10(np.max(np.abs(blk.samples[0].view(np.float32))))
    assert peak == pytest.approx(-12.0, abs=2.5)
    assert d.gains[0] == pytest.approx(33.0, abs=2.5)     # -40 + G - 5 = -12
    g_agc = d.gains[0]
    d.set_gain_mode("manual")                            # manual keeps the AGC gain
    assert d.gains[0] == g_agc


def test_gain_quantized_and_band_limits():
    d = make_device(make_world(), fc=2.4e9, gain=40)
    assert d.set_gain(40.4) == 40.0
    assert d.set_gain(99.0) == 71.0                      # 1.3-4 GHz band max
    assert d.runtime_limits()["gain_db"] == (-3.0, 71.0)
    d.set_center_freq(900e6)
    assert d.runtime_limits()["gain_db"] == (-1.0, 73.0)


# =========================================================================== config / identity
def test_identity_sensors_and_tracking():
    w = make_world(Emitter("t", F0 + 100e3, -50.0), temperature_c=42.0)
    d = make_device(w, gain=40)
    assert d.info.driver == "sim" and d.info.hardware == "EDS-SIM AD9363 model"
    assert d.info.serial == "SIM-rx0" and d.info.firmware == "sim-1" and d.info.backend == "sim"
    blk = d.capture(16384)
    s = d.read_sensors()
    assert s["temperature_c"] == 42.0
    p = 10 * np.log10(np.mean(np.abs(blk.samples[0].astype(np.complex128)) ** 2))
    assert s["rssi_db"] == pytest.approx(-p, abs=0.5)
    assert d.set_tracking(dc=False, rf_dc=True, quadrature=True) == {"dc": "sim", "rf_dc": "sim", "quadrature": "sim"}
    assert d.get_tracking() == {"dc": False, "rf_dc": True, "quadrature": True}
    desc = d.describe()
    assert desc["sim"]["true_ppm"] == pytest.approx(11.7) and desc["info"]["serial"] == "SIM-rx0"


@pytest.mark.parametrize("sim", [
    {"bogus": 1},
    {"ppm": "12"},
    {"realtime": 1},
    {"spurs": [{"offset_hz": 1e6}]},
    {"spurs": {"offset_hz": 1e6, "dbfs": -80}},
    {"position_m": [0.0, 0.0, 1.0]},
    {"time_offset_samples": 1.5},
    {"usb_bytes_per_s": 0.0},
    {"seed": -3},
    {"adc_noise_lsb_rms": -1.0},
    {"digital_filter": 1},
    {"dig_pass_frac": 0.6},                              # pass >= stop (0.60)
    {"dig_pass_frac": 0.0},
    {"dig_pass_frac": 0.55, "dig_stop_frac": 0.9},       # pass edge beyond Nyquist
    {"dig_stop_db": 10.0},
    {"dig_stop_frac": "0.6"},
    {"analog_corner_factor": -1.4},
    {"tia_pole_factor": -2.5},
    {"rf_compression": 1},
    {"rapp_smoothness": 0.0},
    {"p1db_min_gain_dbm": "0"},
    {"xo_correction_step_hz": -1.0},
])
def test_sim_params_validated(sim):
    with pytest.raises(ConfigError):
        SimRxDevice(DeviceConfig(name="rx0", sim=sim), PROFILE, SimWorld())


def test_multichannel_and_shared_clock_conflicts_rejected():
    with pytest.raises(ConfigError):
        SimRxDevice(DeviceConfig(name="rx0", channels=[0, 1]), PROFILE, SimWorld())
    w = SimWorld()
    SimRxDevice(DeviceConfig(name="a", sim={"shared_clock": "ref", "ppm": 2.0}), PROFILE, w)
    with pytest.raises(ConfigError):
        SimRxDevice(DeviceConfig(name="b", sim={"shared_clock": "ref", "ppm": 5.0}), PROFILE, w)
    b = SimRxDevice(DeviceConfig(name="b", sim={"shared_clock": "ref"}), PROFILE, w)
    assert b.true_ppm == 2.0
    c = SimRxDevice(DeviceConfig(name="c"), PROFILE, w)
    assert abs(c.true_ppm) <= 15.0 and c.true_ppm != 2.0


def test_factory_opens_sim_devices_on_one_world():
    app = AppConfig(devices=[DeviceConfig(name="rx0", sim={"realtime": False}),
                             DeviceConfig(name="rx1", sim={"realtime": False})],
                    sim=SimConfig(emitters=[{"name": "t", "freq_hz": 915.1e6, "power_dbm": -60.0}]))
    devs = open_devices(app)
    try:
        assert all(isinstance(x, SimRxDevice) for x in devs)
        assert devs[0].world is devs[1].world is get_sim_world(app)
        assert devs[0].true_ppm == pytest.approx(11.7)
        devs[0].configure(RxConfig(center_freq_hz=915e6, sample_rate_hz=1e6, bandwidth_hz=1e6, gain_db=40))
        x = devs[0].capture(16384).samples[0]
        assert tone_power_db(x, 1e6, apparent_bb(devs[0], 915.1e6)) == pytest.approx(-25.0, abs=0.2)
    finally:
        for x in devs:
            x.close()
    single = open_device(DeviceConfig(name="solo", sim={"realtime": False}))
    assert isinstance(single, SimRxDevice)
    single.close()


# =========================================================================== threads / perf
def test_thread_safety_toggle_while_reading():
    w = make_world(Emitter("a", F0 + 100e3, -50.0), Emitter("b", F0 - 200e3, -55.0, kind="noise", bw_hz=50e3))
    d = make_device(w, fs=2e6, gain=40, buffer_size=8192)
    d.start_stream()
    stop = threading.Event()
    errors: list[BaseException] = []

    def mutate() -> None:
        try:
            i = 0
            while not stop.is_set():
                w.set_enabled("a", bool(i % 2))
                w.update("b", power_dbm=-55.0 + (i % 7))
                if i % 10 == 0:
                    w.add_emitter(Emitter("c", F0 + 300e3 + i, -60.0, kind="fm"))
                if i % 10 == 5:
                    w.remove_emitter("c", missing_ok=True)
                i += 1
                time.sleep(2e-4)                         # a control thread, not a busy loop
        except BaseException as ex:  # noqa: BLE001 - re-raised in the main thread
            errors.append(ex)

    th = threading.Thread(target=mutate)
    th.start()
    try:
        blocks = [d.read(8192) for _ in range(40)]
    finally:
        stop.set()
        th.join()
    assert not errors
    for b in blocks:
        assert b.samples.shape == (1, 8192) and b.samples.dtype == np.complex64
        assert np.all(np.isfinite(b.samples))


@pytest.mark.realtime
def test_synthesis_performance_1m_samples():
    w = make_world(
        Emitter("cw1", F0 + 100e3, -50.0), Emitter("cw2", F0 - 300e3, -60.0),
        Emitter("nz", F0 + 500e3, -55.0, kind="noise", bw_hz=300e3), Emitter("fm", F0 - 600e3, -50.0, kind="fm"),
        Emitter("pl", F0 + 800e3, -45.0, kind="pulse"), Emitter("gsm", F0 - 900e3, -50.0, kind="gsm_fcch"),
    )
    d = make_device(w, fs=4e6, gain=40, buffer_size=65536)
    d.start_stream()
    d.read(1 << 20)                                      # warm-up (FFT plans, caches)
    best = math.inf
    for _ in range(5):
        t0 = time.perf_counter()
        d.read(1 << 20)
        best = min(best, time.perf_counter() - t0)
    assert best < 0.150
