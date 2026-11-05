"""eds.calib.apply: spectrum frequency/power calibration (no double correction), device PPM
application paths, temperature warning and the cal.result bus payload (schema-validated)."""

from __future__ import annotations

import numpy as np
import pytest

from eds.calib.apply import apply_calibration, apply_ppm_to_device, check_temperature, make_cal_payload
from eds.calib.power import PowerCalTable
from eds.calib.ppm import PpmEstimate, compose_ppm
from eds.calib.store import DeviceCalibration
from eds.core.errors import CalibrationError, ConfigError
from eds.core.types import IQBlock
from eds.dsp.psd import spectrum_from_block
from eds.hal.sim import Emitter
from tests.hal.sim_fixtures import make_device, make_world

FS, FC, N = 1e6, 1.0e9, 1 << 15
F_TONE = FC + 125e3


def _ppm(value: float, temp: float | None = 35.0) -> PpmEstimate:
    return PpmEstimate(ppm=value, std_ppm=0.01, f_measured_hz=1e9, f_ref_hz=1e9, snr_db=50.0, n=N, method="cw",
                       temperature_c=temp, t_ns=1_700_000_000_000_000_000)


def _block(ppm_true: float, ppm_hw: float = 0.0, gain: float = 40.0, gain_mode: str = "manual",
           p_dbfs: float = -20.0) -> IQBlock:
    """Tone at true RF F_TONE seen by a receiver with total error ppm_true, hw-corrected ppm_hw."""
    e_res = (1 + ppm_true * 1e-6) / (1 + ppm_hw * 1e-6) - 1
    f_bb = F_TONE / (1 + e_res) - FC
    k = np.arange(N)
    x = (10 ** (p_dbfs / 20) * np.exp(2j * np.pi * f_bb * k / FS)).astype(np.complex64)
    return IQBlock(samples=x[None, :], fs=FS, fc=FC, t_ns=1, index=0, seq=0, gain_db=(gain,),
                   meta={"ppm_hw": ppm_hw, "ppm_hw_via": "hardware" if ppm_hw else "none", "gain_mode": gain_mode})


def _peak_hz(spec) -> float:
    return float(spec.f_hz[int(np.argmax(spec.p_lin))])


# =========================================================================== frequency
def test_software_correction_moves_tone_to_true_frequency():
    spec = spectrum_from_block(_block(20.0), 8192)
    df = spec.df_hz
    assert abs(_peak_hz(spec) - F_TONE) > 100 * df                      # raw: off by 20 kHz
    cal = DeviceCalibration(serial="S", ppm=_ppm(20.0))
    out = apply_calibration(spec, cal, "software")
    assert abs(_peak_hz(out) - F_TONE) <= 0.6 * out.df_hz
    assert out.meta["ppm_corrected"] is True and out.meta["ppm_residual_applied"] == pytest.approx(20.0)
    assert out.fc == pytest.approx(FC * (1 + 20e-6)) and out.fs == pytest.approx(FS * (1 + 20e-6))
    assert spec.meta.get("ppm_corrected") is None                       # input untouched
    # idempotent: a second pass does not correct again
    again = apply_calibration(out, cal, "software")
    assert np.array_equal(again.f_hz, out.f_hz)
    # off: nothing happens
    off = apply_calibration(spec, cal, "off")
    assert np.array_equal(off.f_hz, spec.f_hz) and off.meta["ppm_corrected"] is False


def test_hardware_correction_in_block_meta_is_not_applied_twice():
    spec = spectrum_from_block(_block(20.0, ppm_hw=19.0), 8192)
    cal = DeviceCalibration(serial="S", ppm=_ppm(20.0))
    out = apply_calibration(spec, cal, "auto")                         # ppm_hw taken from the block meta
    assert out.meta["ppm_residual_applied"] == pytest.approx((1 + 20e-6) / (1 + 19e-6) * 1e6 - 1e6, abs=1e-6)
    assert abs(_peak_hz(out) - F_TONE) <= 0.6 * out.df_hz
    # an explicit ppm_hw overrides the meta
    forced = apply_calibration(spec, cal, "auto", ppm_hw=0.0)
    assert forced.meta["ppm_residual_applied"] == pytest.approx(20.0)
    # multi-device block: the analysed channel's member correction wins
    blk = _block(20.0, ppm_hw=19.0)
    blk.meta["ppm_hw"] = 3.0
    blk.meta["ppm_hw_members"] = [19.0]
    out = apply_calibration(spectrum_from_block(blk, 8192), cal, "auto")
    assert out.meta["ppm_hw"] == 19.0


# =========================================================================== power
def test_power_offset_to_dbm_and_uncalibrated_dbfs():
    spec = spectrum_from_block(_block(0.0, gain=40.0, p_dbfs=-20.0), 4096)
    table = PowerCalTable.from_model(5.0)                               # offset = 5 - G = -35 dB
    out = apply_calibration(spec, DeviceCalibration(serial="S", power=table), "off")
    assert out.unit == "dBm" and out.meta["calibrated"] is True and out.meta["power_offset_db"] == pytest.approx(-35.0)
    f = F_TONE
    assert out.band_power_db(f - 5e3, f + 5e3) == pytest.approx(-55.0, abs=0.01)
    assert apply_calibration(out, DeviceCalibration(serial="S", power=table), "off").unit == "dBm"   # idempotent
    raw = apply_calibration(spec, DeviceCalibration(serial="S"), "auto")
    assert raw.unit == "dBFS" and raw.meta["calibrated"] is False and raw.meta["ppm_corrected"] is False
    none = apply_calibration(spec, None, "auto")
    assert none.unit == "dBFS" and none.meta["calibrated"] is False


def test_hw_agc_capture_is_not_power_calibrated():
    spec = spectrum_from_block(_block(0.0, gain_mode="hw_slow"), 4096)
    out = apply_calibration(spec, DeviceCalibration(serial="S", power=PowerCalTable.from_model(5.0)), "off")
    assert out.unit == "dBFS" and out.meta["calibrated"] is False and "AGC" in out.meta["cal_note"]


def test_invalid_mode_and_missing_gain():
    spec = spectrum_from_block(_block(0.0), 4096)
    with pytest.raises(ConfigError):
        apply_calibration(spec, None, "always")
    spec.gain_db = ()
    with pytest.raises(ConfigError, match="kazanç"):
        apply_calibration(spec, DeviceCalibration(serial="S", power=PowerCalTable.from_model(5.0)), "off")


# =========================================================================== device
def test_apply_ppm_to_device_paths():
    w = make_world(Emitter("t", 1e9, -60.0))
    dev = make_device(w, fs=1e6)
    cal = DeviceCalibration(serial=dev.info.serial, ppm=_ppm(dev.true_ppm))
    assert apply_ppm_to_device(dev, cal, "off") == "off" and dev.ppm_hw == 0.0
    assert apply_ppm_to_device(dev, cal, "software") == "software" and dev.ppm_hw == 0.0
    assert apply_ppm_to_device(dev, DeviceCalibration(serial="x"), "auto") == "off"
    assert apply_ppm_to_device(dev, cal, "auto") == "hardware"
    assert dev.ppm_hw == pytest.approx(11.7) and dev.residual_ppm == pytest.approx(0.0, abs=1e-9)
    assert apply_ppm_to_device(dev, cal, "hardware") == "hardware"
    with pytest.raises(ConfigError):
        apply_ppm_to_device(dev, cal, "bogus")


def test_missing_ppm_warning_is_logged_once_per_serial(monkeypatch, caplog):
    import logging

    import eds.calib.apply as apply_mod

    monkeypatch.setattr(apply_mod, "_NO_PPM_WARNED", set())
    w = make_world()
    devs = [make_device(w, "rxA", fs=1e6, serial="SER-1"), make_device(w, "rxA2", fs=1e6, serial="SER-1"),
            make_device(w, "rxB", fs=1e6, serial="SER-2")]
    with caplog.at_level(logging.DEBUG, logger="eds.calib.apply"):
        for _ in range(3):
            for d in devs:                                  # e.g. one RxNode per measurement
                assert apply_ppm_to_device(d, None, "auto") == "off"
                assert apply_ppm_to_device(d, DeviceCalibration(serial=d.info.serial), "auto") == "off"
    msgs = [r for r in caplog.records if "PPM kalibrasyonu yok" in r.getMessage()]
    warned = [r.getMessage() for r in msgs if r.levelno == logging.WARNING]
    assert len(warned) == 2 and warned[0].startswith("rxA:") and warned[1].startswith("rxB:")
    assert len(msgs) == 18 and sum(r.levelno == logging.DEBUG for r in msgs) == 16
    assert apply_mod._NO_PPM_WARNED == {"serial:SER-1", "serial:SER-2"}


def test_apply_ppm_without_hardware_support():
    dev = make_device(make_world(), fs=1e6, hw_xo_correction=False)
    cal = DeviceCalibration(serial=dev.info.serial, ppm=_ppm(5.0))
    assert apply_ppm_to_device(dev, cal, "auto") == "software"
    with pytest.raises(CalibrationError, match="donanımda uygulanamadı"):
        apply_ppm_to_device(dev, cal, "hardware")


def test_end_to_end_hardware_ppm_then_spectrum_has_true_frequency():
    w = make_world(Emitter("t", F_TONE, -60.0))
    dev = make_device(w, fc=FC, fs=FS, gain=40, ppm=-14.2)
    cal = DeviceCalibration(serial=dev.info.serial, ppm=_ppm(-14.2))
    assert apply_ppm_to_device(dev, cal, "auto") == "hardware"
    spec = apply_calibration(spectrum_from_block(dev.capture(N), 8192), cal, "auto")
    assert spec.meta["ppm_residual_applied"] == pytest.approx(0.0, abs=1e-9)
    assert abs(_peak_hz(spec) - F_TONE) <= 0.6 * spec.df_hz


# =========================================================================== temperature
def test_check_temperature_warning_in_turkish():
    w = make_world(temperature_c=35.0)
    dev = make_device(w, fs=1e6)
    cal = DeviceCalibration(serial="S", ppm=_ppm(1.0, temp=33.0))
    assert check_temperature(cal, dev, 5.0) is None
    w.temperature_c = 44.0
    msg = check_temperature(cal, dev, 5.0)
    assert msg is not None and "+11.0 °C" in msg and "kalibrasyon" in msg
    assert check_temperature(DeviceCalibration(serial="S", ppm=_ppm(1.0, temp=None)), dev, 5.0) is None
    assert check_temperature(None, dev, 5.0) is None


# =========================================================================== bus payload
def test_cal_payloads_validate_against_schema():
    pytest.importorskip("jsonschema")
    from eds.bus import topics as T
    from eds.bus.validate import validate_data
    from eds.df.coherence import CoherenceReport

    dev = make_device(make_world(), fs=1e6)
    est = _ppm(compose_ppm(11.0, 0.7))
    est.extra = {"ppm_values": [11.7, 11.71], "nan_here": float("nan")}
    p = make_cal_payload("ppm", dev, est, "hardware")
    validate_data(T.CAL_RESULT, p)
    assert p["values"]["ppm"] == pytest.approx(11.7000077) and p["values"]["extra"]["nan_here"] is None
    assert p["t_ns"] == est.t_ns and p["temperature_c"] == 35.0 and p["serial"] == dev.info.serial

    p = make_cal_payload("power", dev, PowerCalTable.from_model(5.0), "store")
    validate_data(T.CAL_RESULT, p)
    assert p["temperature_c"] == 35.0                                   # from the device sensor

    rep = CoherenceReport(delay_samples=0.37, phase_deg=-47.2, coherence=0.993, phase_drift_deg_per_s=1.8,
                          phase_std_deg=0.4, n_blocks=8, coherent=False, note="x")
    p = make_cal_payload("coherence", "df0", rep, "software", t_ns=5)
    validate_data(T.CAL_RESULT, p)
    assert p["values"]["delay_std_samples"] is None and "serial" not in p and p["temperature_c"] is None

    p = make_cal_payload("image_rejection", dev, {"freqs_hz": np.array([915e6]), "irr_db": [41.2]}, "store")
    validate_data(T.CAL_RESULT, p)
    with pytest.raises(ConfigError):
        make_cal_payload("gain", dev, {}, "store")
    with pytest.raises(ConfigError):
        make_cal_payload("ppm", dev, 3.0, "store")
    with pytest.raises(ConfigError):
        make_cal_payload("ppm", dev, est, "")
