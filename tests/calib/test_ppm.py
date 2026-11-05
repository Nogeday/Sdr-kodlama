"""eds.calib.ppm: sign convention, synthetic estimator accuracy, CW calibration on the simulator
(including the hardware-correction path: total ppm unchanged, residual ~ 0)."""

from __future__ import annotations

import json
import math

import numpy as np
import pytest

from eds.calib.ppm import (
    PpmEstimate,
    compose_ppm,
    correct_freq,
    estimate_ppm_cw,
    estimate_ppm_from_samples,
    residual_ppm,
    tune_freq,
    xo_correction_hz,
)
from eds.core.errors import CalibrationError, ConfigError
from eds.hal.sim import Emitter
from tests.hal.sim_fixtures import make_device, make_world

F_REF = 1.0e9


# =========================================================================== conventions
def test_correct_and_tune_follow_architecture_sign_convention():
    ppm = 11.7                                   # TCXO fast
    e = ppm * 1e-6
    f_app = F_REF / (1 + e)                      # a fast TCXO sees the tone LOW
    assert f_app == pytest.approx(999_988_300.137, abs=1e-3)
    assert correct_freq(f_app, ppm) == pytest.approx(F_REF, abs=1e-6)
    assert tune_freq(F_REF, ppm) == pytest.approx(F_REF / (1 + e), abs=1e-6)
    arr = correct_freq(np.array([f_app, 2 * f_app]), ppm)
    assert isinstance(arr, np.ndarray) and arr == pytest.approx([F_REF, 2 * F_REF], abs=1e-6)
    assert xo_correction_hz(ppm) == pytest.approx(40_000_468.0, abs=1e-6)     # HARDWARE_PLUTO example


def test_compose_and_residual_are_inverse():
    tot = compose_ppm(11.0, 0.7)
    assert tot == pytest.approx(11.0 + 0.7 + 11.0 * 0.7 * 1e-6, abs=1e-12)
    assert tot == pytest.approx(((1 + 11e-6) * (1 + 0.7e-6) - 1) * 1e6, abs=1e-8)
    assert residual_ppm(tot, 11.0) == pytest.approx(0.7, abs=1e-9)
    assert residual_ppm(-20.0, -20.0) == pytest.approx(0.0, abs=1e-12)
    with pytest.raises(ConfigError):
        compose_ppm(float("nan"), 1.0)


# =========================================================================== synthetic samples
def _tone(n, f_bb, fs, power_dbfs, rng, noise_dbfs=-70.0, dc_dbfs=-20.0, image_dbc=-30.0):
    k = np.arange(n)
    a = 10 ** (power_dbfs / 20)
    x = a * np.exp(2j * np.pi * f_bb * k / fs + 0.3j)
    x += a * 10 ** (image_dbc / 20) * np.exp(-2j * np.pi * f_bb * k / fs)      # IQ image
    x += 10 ** (dc_dbfs / 20) * np.exp(0.7j)                                    # strong zero-IF DC
    s = math.sqrt(10 ** (noise_dbfs / 10) / 2)
    x += s * (rng.standard_normal(n) + 1j * rng.standard_normal(n))
    return x.astype(np.complex64)


@pytest.mark.parametrize("ppm_true", [-23.4, -3.1, 0.0, 11.7, 24.9])
def test_estimate_from_samples_recovers_ppm(ppm_true):
    rng = np.random.default_rng(7)
    fs, fc = 1e6, F_REF - 100e3
    f_app = F_REF / (1 + ppm_true * 1e-6)
    x = _tone(1 << 17, f_app - fc, fs, -20.0, rng)
    assert estimate_ppm_from_samples(x, fs, fc, F_REF) == pytest.approx(ppm_true, abs=2e-4)


def test_estimate_from_samples_ignores_dc_spike_inside_window_at_high_frequency():
    # 3.8 GHz, offset 100 kHz: the +-1.5*25 ppm window (+-142.5 kHz) contains DC
    rng = np.random.default_rng(3)
    f_ref, fs = 3.8e9, 2e6
    fc = f_ref - 100e3
    ppm_true = 22.0                                      # tone only 16.4 kHz from DC
    f_app = f_ref / (1 + ppm_true * 1e-6)
    x = _tone(1 << 17, f_app - fc, fs, -30.0, rng, dc_dbfs=-10.0)
    assert estimate_ppm_from_samples(x, fs, fc, f_ref) == pytest.approx(ppm_true, abs=1e-3)


def test_estimate_from_samples_window_outside_band_is_config_error():
    x = np.ones(4096, np.complex64)
    with pytest.raises(ConfigError, match="örnekleme hızını"):
        estimate_ppm_from_samples(x, 200e3, F_REF - 100e3, F_REF)


# =========================================================================== simulator CW
def _ref_world(power=-60.0):
    return make_world(Emitter("ref", F_REF, power, cabled=True))


def test_estimate_ppm_cw_sim_1ghz_within_0p05_ppm():
    w = _ref_world()
    dev = make_device(w, fc=915e6, fs=1e6, gain=40)
    assert dev.true_ppm == pytest.approx(11.7)          # first device of the world
    est = estimate_ppm_cw(dev, F_REF, repeats=3)
    assert est.ppm == pytest.approx(dev.true_ppm, abs=0.05)
    assert est.ppm_residual == pytest.approx(est.ppm, abs=1e-12) and est.ppm_hw == 0.0
    assert est.f_measured_hz == pytest.approx(F_REF / (1 + dev.true_ppm * 1e-6), abs=0.05)
    assert est.std_ppm < 0.01 and est.repeats == 3 and len(est.extra["ppm_values"]) == 3
    assert est.snr_db > 40.0
    assert est.method == "cw" and est.n == 2 ** 18
    assert est.temperature_c == pytest.approx(35.0)
    assert est.fc_nominal_hz == pytest.approx(F_REF - 100e3) and est.fs_hz == 1e6
    assert est.t_ns > 0
    assert dev.fc == 915e6                               # settings restored
    assert not dev.streaming


def test_estimate_ppm_cw_negative_ppm_and_gain_argument():
    w = _ref_world(-80.0)
    dev = make_device(w, fc=F_REF, fs=2e6, gain=10, ppm=-18.25)
    est = estimate_ppm_cw(dev, F_REF, offset_hz=250e3, n=2 ** 17, repeats=2, gain_db=50)
    assert est.ppm == pytest.approx(-18.25, abs=0.05)
    assert est.extra["gain_db"] == [50.0]
    assert dev.gains == [10.0]                           # gain restored


def test_hardware_correction_path_total_unchanged_residual_zero():
    w = _ref_world()
    dev = make_device(w, fc=F_REF, fs=1e6, gain=40)
    assert dev.set_ppm_correction(11.0) == "hardware"
    est = estimate_ppm_cw(dev, F_REF, repeats=2)
    assert est.ppm_hw == pytest.approx(11.0)
    assert est.ppm_residual == pytest.approx(residual_ppm(dev.true_ppm, 11.0), abs=0.05)
    assert est.ppm == pytest.approx(dev.true_ppm, abs=0.05)
    # storing the TOTAL and re-applying it in hardware is idempotent
    dev.set_ppm_correction(est.ppm)
    est2 = estimate_ppm_cw(dev, F_REF, repeats=2)
    assert est2.ppm_residual == pytest.approx(0.0, abs=0.01)
    assert est2.ppm == pytest.approx(est.ppm, abs=0.01)
    assert est2.f_measured_hz == pytest.approx(F_REF, abs=0.05)


def test_single_repeat_reports_cramer_rao_std():
    w = _ref_world()
    dev = make_device(w, fc=F_REF, fs=1e6, gain=40)
    est = estimate_ppm_cw(dev, F_REF, n=2 ** 16, repeats=1)
    assert 0.0 < est.std_ppm < 1e-4
    assert "Cramér-Rao" in est.extra["std_definition"]


def test_low_snr_reference_raises_turkish_calibration_error():
    w = _ref_world(-125.0)
    dev = make_device(w, fc=F_REF, fs=1e6, gain=10)
    with pytest.raises(CalibrationError, match="SNR"):
        estimate_ppm_cw(dev, F_REF, n=2 ** 16, repeats=1)
    assert dev.fc == F_REF                               # restored after the failure


def test_clipping_reference_raises():
    w = _ref_world(-20.0)
    dev = make_device(w, fc=F_REF, fs=1e6, gain=60)
    with pytest.raises(CalibrationError, match="kırpma"):
        estimate_ppm_cw(dev, F_REF, n=2 ** 16, repeats=1)


def test_invalid_arguments():
    dev = make_device(_ref_world(), fc=F_REF, fs=1e6)
    with pytest.raises(ConfigError):
        estimate_ppm_cw(dev, F_REF, repeats=0)
    with pytest.raises(ConfigError):
        estimate_ppm_cw(dev, F_REF, channel=1)
    with pytest.raises(ConfigError, match="fs/2"):
        estimate_ppm_cw(dev, F_REF, offset_hz=480e3, n=2 ** 16, repeats=1)


# =========================================================================== serialization
def test_ppm_estimate_round_trip_and_json_safety():
    est = PpmEstimate(ppm=11.7, std_ppm=float("nan"), f_measured_hz=999_988_300.1, f_ref_hz=F_REF, snr_db=48.7,
                      n=262144, method="cw", temperature_c=None, t_ns=123, fc_nominal_hz=999.9e6, fs_hz=1e6,
                      ppm_residual=0.7, ppm_hw=11.0, repeats=5, extra={"ppm_values": [np.float64(11.7)]})
    d = est.to_dict()
    text = json.dumps(d, allow_nan=False)                # NaN became null
    back = PpmEstimate.from_dict(json.loads(text))
    assert math.isnan(back.std_ppm)
    assert back.ppm == 11.7 and back.ppm_hw == 11.0 and back.extra == {"ppm_values": [11.7]}
    assert back.temperature_c is None and back.t_ns == 123
    assert est.e == pytest.approx(11.7e-6)
    assert "PPM = +11.7000" in est.summary_tr()
    with pytest.raises(CalibrationError, match="bilinmeyen"):
        PpmEstimate.from_dict({**d, "foo": 1})
    with pytest.raises(CalibrationError):
        PpmEstimate.from_dict({"std_ppm": 0.1})
    minimal = PpmEstimate.from_dict({"ppm": -3.0})
    assert minimal.ppm == -3.0 and minimal.method == "manual"
