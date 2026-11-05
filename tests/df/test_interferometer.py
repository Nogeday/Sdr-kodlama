"""eds.df.interferometer: geometry/sign convention, grating-lobe listing, synthetic and simulator
bearings after calibration (RMS < 1°), delay compensation and the df.bearing payload."""

from __future__ import annotations

import math

import numpy as np
import pytest

from eds.core.errors import ConfigError, MeasurementError
from eds.core.units import C_LIGHT, wrap_deg
from eds.df.interferometer import Bearing, Interferometer2
from eds.df.metrics import rms_angle_error, summarize
from eds.hal.group import DeviceGroup
from eds.hal.sim import Emitter
from tests.hal.sim_fixtures import make_device, make_world

F = 1.0e9
LAM = C_LIGHT / F


def _pair(rng, az_deg, d, f=F, n=8192, snr_db=20.0, cal_rad=0.7, f_bb=0.05):
    """Tone common to both elements: x1 = x0 · e^{j(2π d sin(az)/λ + cal)} (plus noise)."""
    k = np.arange(n)
    s = np.exp(2j * np.pi * f_bb * k)
    dphi = 2 * np.pi * d * math.sin(math.radians(az_deg)) * f / C_LIGHT + cal_rad
    nv = 10 ** (-snr_db / 10)

    def noise():
        return math.sqrt(nv / 2) * (rng.standard_normal(n) + 1j * rng.standard_normal(n))

    return s + noise(), s * np.exp(1j * dphi) + noise()


# =========================================================================== geometry
def test_geometry_and_sign_convention():
    ifm = Interferometer2(0.15)
    assert ifm.d_over_lambda(F) == pytest.approx(0.15 / LAM)
    assert ifm.max_unambiguous_hz == pytest.approx(C_LIGHT / 0.3)
    # source toward element 1 (+x) -> element 1 leads -> positive phase -> positive azimuth
    assert ifm.geometric_phase_rad(30.0, F) == pytest.approx(2 * math.pi * 0.15 * 0.5 / LAM)
    assert ifm.geometric_phase_rad(-30.0, F) < 0
    assert not ifm.calibrated
    assert Interferometer2(0.1, phase_cal_rad=0.2).calibrated
    for bad in (0.0, -1.0, float("nan")):
        with pytest.raises(ConfigError):
            Interferometer2(bad)


@pytest.mark.parametrize("az", [-75.0, -60.0, -30.0, -5.0, 0.0, 12.5, 45.0, 60.0, 80.0])
def test_noise_free_bearing_is_exact_for_half_wavelength(az):
    rng = np.random.default_rng(0)
    d = 0.49 * LAM
    x0, x1 = _pair(rng, az, d, snr_db=200.0, cal_rad=0.0)
    br = Interferometer2(d).bearing(x0, x1, F)
    assert br.az_deg == pytest.approx(az, abs=1e-6)
    assert br.ambiguous_deg == [] and br.coherence == pytest.approx(1.0)
    assert br.phase_deg == pytest.approx(math.degrees(2 * math.pi * 0.49 * math.sin(math.radians(az))), abs=1e-6)


def test_saturated_sine_is_clipped_and_penalized():
    ifm = Interferometer2(0.3 * LAM)                          # d < λ/2: |s| can exceed 1
    k = np.arange(1024)
    x0 = np.exp(0.1j * k)
    x1 = x0 * np.exp(1j * math.radians(150.0))                  # needs sin = 150/108 > 1
    br = ifm.bearing(x0, x1, F)
    assert br.az_deg == pytest.approx(90.0) and br.sin_az > 1.0
    assert br.quality < 0.1 and br.ambiguous_deg == []


def test_ambiguity_listing_for_one_wavelength_baseline():
    rng = np.random.default_rng(1)
    d = LAM                                                     # d = λ: grating lobes
    ifm = Interferometer2(d)
    # 20°: Δφ = 2π sin20 = 123.1°  -> principal 20°, other: asin(sin20 - 1) = -41.15°
    br = ifm.bearing(*_pair(rng, 20.0, d, snr_db=60.0, cal_rad=0.0), F)
    assert br.az_deg == pytest.approx(20.0, abs=0.05)
    assert br.ambiguous_deg == pytest.approx([math.degrees(math.asin(math.sin(math.radians(20)) - 1))], abs=0.05)
    assert br.quality == pytest.approx(br.coherence / 2)
    # 45°: Δφ wraps (254.6° -> -105.4°): the principal solution is the WRONG lobe, the truth is listed
    br = ifm.bearing(*_pair(rng, 45.0, d, snr_db=60.0, cal_rad=0.0), F)
    assert br.az_deg == pytest.approx(math.degrees(math.asin(math.sin(math.radians(45)) - 1)), abs=0.05)
    assert 45.0 == pytest.approx(br.ambiguous_deg[0], abs=0.05)
    assert br.solutions_deg == sorted([br.az_deg, *br.ambiguous_deg]) and br.ambiguous == br.ambiguous_deg
    # 2λ: every solution of sin = (Δφ/2π + k)/2 listed
    sols = Interferometer2(2 * LAM).solutions_deg(math.radians(30.0), F)
    expected = sorted(math.degrees(math.asin((30.0 / 360.0 + k) / 2.0)) for k in (-2, -1, 0, 1))
    assert sols == pytest.approx(expected)


def test_synthetic_calibrated_sweep_rms_below_1_deg():
    rng = np.random.default_rng(2)
    d = 0.15
    ifm = Interferometer2(d)
    x0, x1 = _pair(rng, 0.0, d, snr_db=20.0, cal_rad=1.9)       # cabled reference: no geometry
    cal = ifm.calibrate(x0, x1, F, estimate_delay=False)
    assert cal["phase_cal_deg"] == pytest.approx(math.degrees(1.9), abs=0.2)
    assert ifm.calibrated and cal["coherence"] > 0.95
    true = np.arange(-60.0, 61.0, 5.0)
    est = [ifm.bearing(*_pair(rng, a, d, snr_db=20.0, cal_rad=1.9), F).az_deg for a in true]
    assert rms_angle_error(est, true) < 1.0
    s = summarize(est, true)
    assert s["n"] == true.size and abs(s["mean"]) < 0.5


def test_calibration_with_known_over_the_air_azimuth():
    rng = np.random.default_rng(3)
    d = 0.1
    ifm = Interferometer2(d)
    ifm.calibrate(*_pair(rng, 25.0, d, snr_db=40.0, cal_rad=-2.5), F, true_az_deg=25.0, estimate_delay=False)
    assert ifm.phase_cal_deg == pytest.approx(math.degrees(-2.5), abs=0.1)
    assert ifm.bearing(*_pair(rng, -40.0, d, snr_db=40.0, cal_rad=-2.5), F).az_deg == pytest.approx(-40.0, abs=0.2)


def test_band_mask_isolates_signal_and_needs_fs():
    rng = np.random.default_rng(4)
    n, fs = 1 << 14, 2e6
    k = np.arange(n)
    d = 0.15
    tgt = np.exp(2j * np.pi * 300e3 * k / fs)
    jam = 3.0 * np.exp(2j * np.pi * -450e3 * k / fs)            # strong cabled signal at another frequency
    ph = 2 * np.pi * d * math.sin(math.radians(35.0)) * F / C_LIGHT
    x0 = tgt + jam + 0.01 * (rng.standard_normal(n) + 1j * rng.standard_normal(n))
    x1 = tgt * np.exp(1j * ph) + jam + 0.01 * (rng.standard_normal(n) + 1j * rng.standard_normal(n))
    ifm = Interferometer2(d)
    assert abs(ifm.bearing(x0, x1, F).az_deg - 35.0) > 10.0     # the jammer dominates without a mask
    br = ifm.bearing(x0, x1, F, f_bb_hz=300e3, bw_hz=20e3, fs=fs)
    assert br.az_deg == pytest.approx(35.0, abs=0.05)
    with pytest.raises(ConfigError, match="fs"):
        ifm.bearing(x0, x1, F, f_bb_hz=300e3)
    with pytest.raises(ConfigError):
        ifm.bearing(x0, x1, F, bw_hz=20e3)
    with pytest.raises(MeasurementError):
        ifm.bearing(np.zeros(n), x1, F)


# =========================================================================== simulator
def _sim_array(*emitters, offset=0, fs=2e6):
    w = make_world(*emitters)
    fc = F - 100e3
    a = make_device(w, "rx0", fc=fc, fs=fs, gain=40, shared_clock="ref40", position_m=[0.0, 0.0])
    b = make_device(w, "rx1", fc=fc, fs=fs, gain=40, shared_clock="ref40", position_m=[0.15, 0.0],
                    time_offset_samples=offset)
    return w, DeviceGroup([a, b])


def test_sim_df_sweep_after_cabled_calibration_rms_below_1_deg():
    # cabled pilot (-500 kHz baseband) and over-the-air target (+100 kHz) on at the same time,
    # calibration in the pilot band, DF in the target band (config/dual_sim.toml scenario)
    w, g = _sim_array(Emitter("pilot", F - 600e3, -55.0, cabled=True), Emitter("tgt", F, -60.0, aoa_deg=0.0))
    with g:
        ifm = Interferometer2(0.15)
        blk = g.capture(1 << 15)
        cal = ifm.calibrate(blk.samples[0], blk.samples[1], F - 600e3, fs=blk.fs, f_bb_hz=-500e3, bw_hz=60e3)
        assert cal["coherence"] > 0.99 and not cal["delay_resolvable"]
        true, est = [], []
        for az in np.arange(-60.0, 61.0, 10.0):
            w.update("tgt", aoa_deg=float(az))
            blk = g.read(16384)
            br = ifm.bearing(blk.samples[0], blk.samples[1], F, f_bb_hz=100e3, bw_hz=60e3, fs=blk.fs)
            assert br.coherence > 0.99 and br.calibrated and br.ambiguous_deg == []
            true.append(az)
            est.append(br.az_deg)
    assert rms_angle_error(est, true) < 1.0
    assert summarize(est, true)["max_abs"] < 0.5


def test_sim_delay_compensation_with_noise_pilot():
    # streams 5 samples apart: a narrowband target 100 kHz away from where a CW calibration
    # would have been taken needs the delay; the wideband noise pilot provides it
    w, g = _sim_array(Emitter("noise_src", F - 100e3, -50.0, kind="noise", bw_hz=1.2e6, cabled=True),
                      Emitter("tgt", F + 250e3, -60.0, aoa_deg=20.0, enabled=False), offset=5)
    with g:
        blk = g.capture(1 << 15)
        ifm = Interferometer2(0.15)
        cal = ifm.calibrate(blk.samples[0], blk.samples[1], F)
        assert cal["delay_resolvable"] and cal["delay_cal_samples"] == pytest.approx(-5.0, abs=0.05)
        w.set_enabled("noise_src", False)
        w.set_enabled("tgt", True)
        blk = g.read(1 << 14)
        br = ifm.bearing(blk.samples[0], blk.samples[1], F + 250e3, f_bb_hz=350e3, bw_hz=60e3, fs=blk.fs)
        assert br.az_deg == pytest.approx(20.0, abs=0.5)
        # without the delay the phase is off by 2π·f_bb·5/fs (≈ 315° → wrong bearing)
        naive = Interferometer2(0.15, phase_cal_rad=ifm.phase_cal_rad)
        assert abs(naive.bearing(blk.samples[0], blk.samples[1], F + 250e3, f_bb_hz=350e3, bw_hz=60e3,
                                 fs=blk.fs).az_deg - 20.0) > 5.0


# =========================================================================== bus payload
def test_bearing_payload_validates_against_schema():
    pytest.importorskip("jsonschema")
    from eds.bus import topics as T
    from eds.bus.validate import validate_data

    rng = np.random.default_rng(5)
    br = Interferometer2(LAM).bearing(*_pair(rng, 20.0, LAM, snr_db=30.0), 1e9)
    p = br.to_bus_payload("pluto2x_d0.30m", t_acq_ns=1_700_000_000_000_000_000)
    validate_data(T.DF_BEARING, p)
    assert p["method"] == "interferometer2" and p["ambiguous_deg"] and p["calibrated"] is False
    assert set(p) == {"array", "method", "f_hz", "az_deg", "ambiguous_deg", "phase_deg", "coherence", "quality",
                      "calibrated", "t_acq_ns"}
    edge = Bearing(az_deg=-180.0, ambiguous_deg=[540.0], phase_deg=-180.0, coherence=1.0000001, quality=float("nan"),
                   f_hz=433.92e6, calibrated=True)
    p2 = edge.to_bus_payload("a", method="music")
    validate_data(T.DF_BEARING, p2)
    assert p2["az_deg"] == 180.0 and p2["ambiguous_deg"] == [180.0] and p2["coherence"] == 1.0 and p2["quality"] == 0.0
    assert "t_acq_ns" not in p2
    for bad in ({"array_name": ""}, {"array_name": "a", "t_acq_ns": -1}):
        with pytest.raises(ConfigError):
            edge.to_bus_payload(**bad)
    assert wrap_deg(p["phase_deg"]) == pytest.approx(p["phase_deg"])
