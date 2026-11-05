"""eds.calib.fcch: ARFCN table and FCCH-burst PPM calibration on the simulator's GSM BCCH."""

from __future__ import annotations

import pytest

from eds.calib.fcch import GSM_FCCH_OFFSET_HZ, arfcn_to_freq, estimate_ppm_gsm_fcch
from eds.calib.ppm import residual_ppm
from eds.core.errors import CalibrationError, ConfigError
from eds.hal.sim import Emitter
from tests.hal.sim_fixtures import make_device, make_world


@pytest.mark.parametrize(("arfcn", "band", "f_mhz"), [
    (1, "GSM900", 935.2), (62, "GSM900", 947.4), (124, "GSM900", 959.8),
    (0, "GSM900", 935.0), (975, "GSM900", 925.2), (1023, "gsm900", 934.8),     # E-GSM
    (512, "DCS1800", 1805.2), (700, "dcs1800", 1842.8), (885, "DCS-1800", 1879.8),
])
def test_arfcn_to_freq(arfcn, band, f_mhz):
    assert arfcn_to_freq(arfcn, band) == pytest.approx(f_mhz * 1e6, abs=1e-3)


@pytest.mark.parametrize(("arfcn", "band"), [(125, "GSM900"), (511, "DCS1800"), (886, "DCS1800"),
                                             (1, "PCS1900"), (1.0, "GSM900")])
def test_arfcn_invalid(arfcn, band):
    with pytest.raises(ConfigError):
        arfcn_to_freq(arfcn, band)


def _bcch_device(carrier: float, ppm: float, *, t0_s: float = 0.0, power: float = -50.0, **kw):
    w = make_world(Emitter("bcch", carrier, power, kind="gsm_fcch", params={"t0_s": t0_s}))
    return make_device(w, fc=carrier, fs=2e6, gain=40, ppm=ppm, **kw)


def test_fcch_estimate_within_0p1_ppm():
    carrier = arfcn_to_freq(1)
    dev = _bcch_device(carrier, -7.3)
    est = estimate_ppm_gsm_fcch(dev, carrier, n_bursts=10)
    assert est.ppm == pytest.approx(-7.3, abs=0.1)
    assert est.method == "gsm_fcch"
    assert est.repeats >= 9 and len(est.extra["bursts"]) == 10
    assert est.f_ref_hz == pytest.approx(carrier + GSM_FCCH_OFFSET_HZ)
    assert est.f_measured_hz == pytest.approx(est.f_ref_hz / (1 - 7.3e-6), abs=10.0)
    assert est.std_ppm < 0.05
    assert est.snr_db > 30.0
    assert est.fs_hz == 1e6 and est.fc_nominal_hz == pytest.approx(carrier - 200e3)
    assert est.temperature_c == pytest.approx(35.0)
    # bursts are one TDMA period (10 frames = 46.15 ms) apart
    starts = [b["start"] for b in est.extra["bursts"]]
    gaps = [b - a for a, b in zip(starts, starts[1:])]
    assert all(abs(g - 46154) < 400 for g in gaps)
    assert all(450 <= b["length"] <= 560 for b in est.extra["bursts"])
    # device settings restored (fs, bw, fc)
    assert (dev.fs, dev.bw, dev.fc) == (2e6, 2e6, carrier)


def test_fcch_dcs1800_arfcn_with_hardware_correction():
    carrier = arfcn_to_freq(700, "DCS1800")
    dev = _bcch_device(carrier, 21.5, t0_s=0.0123)
    dev.set_ppm_correction(20.0)
    est = estimate_ppm_gsm_fcch(dev, 700, n_bursts=5)          # integer ARFCN, band chosen automatically
    assert est.extra["carrier_hz"] == pytest.approx(carrier)
    assert est.ppm_hw == pytest.approx(20.0)
    assert est.ppm_residual == pytest.approx(residual_ppm(21.5, 20.0), abs=0.1)
    assert est.ppm == pytest.approx(21.5, abs=0.1)


def test_steady_cw_in_fcch_window_is_not_a_burst():
    carrier = arfcn_to_freq(10)
    w = make_world(Emitter("cw", carrier + GSM_FCCH_OFFSET_HZ, -50.0))       # a carrier, not a BCCH
    dev = make_device(w, fc=carrier, fs=1e6, gain=40, ppm=3.0)
    with pytest.raises(CalibrationError, match="FCCH"):
        estimate_ppm_gsm_fcch(dev, carrier, max_capture_s=0.3)
    assert dev.fs == 1e6


def test_no_signal_raises_with_hints():
    carrier = arfcn_to_freq(20)
    dev = make_device(make_world(), fc=carrier, fs=1e6, gain=40)
    with pytest.raises(CalibrationError, match="BCCH"):
        estimate_ppm_gsm_fcch(dev, carrier, max_capture_s=0.2)


def test_invalid_arguments():
    dev = make_device(make_world(), fc=935.2e6, fs=1e6)
    with pytest.raises(ConfigError):
        estimate_ppm_gsm_fcch(dev, 935.2e6, n_bursts=2)
    with pytest.raises(ConfigError, match="örnekleme hızını"):
        estimate_ppm_gsm_fcch(dev, 935.2e6, fs=600e3)
    with pytest.raises(ConfigError):
        estimate_ppm_gsm_fcch(dev, 935.2e6, frame_s=1e-3)
