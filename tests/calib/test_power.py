"""eds.calib.power: PowerCalTable interpolation/extrapolation, serialization and the measured
calibration on the simulator (a -60 dBm tone must read -60 +- 0.5 dBm afterwards)."""

from __future__ import annotations

import json

import numpy as np
import pytest

from eds.calib.apply import apply_calibration
from eds.calib.power import PowerCalTable, measure_power_cal, measure_tone_dbfs
from eds.calib.store import DeviceCalibration
from eds.core.errors import CalibrationError, ConfigError
from eds.dsp.psd import spectrum_from_block
from eds.hal.sim import SimSignalSource
from eds.hal.tx import SignalSource
from tests.hal.sim_fixtures import make_device, make_world

F1, F2, F3 = 400e6, 1.0e9, 2.4e9


def _table() -> PowerCalTable:
    return PowerCalTable([
        (F1, 20.0, -20.0), (F1, 40.0, -39.0), (F1, 60.0, -58.0),     # slope -0.95 dB/dB
        (F2, 30.0, -31.0), (F2, 50.0, -50.0),                        # slope -0.95
        (F3, 40.0, -38.0),                                           # single gain point
    ], meta={"source": "unit"})


# =========================================================================== interpolation
def test_exact_points_are_reproduced():
    t = _table()
    for f, g, o in t.points:
        assert t.offset_db(f, g) == pytest.approx(o, abs=1e-12)
    assert len(t) == 6 and t.gains_at(F1) == [20.0, 40.0, 60.0]


def test_gain_interpolation_and_minus_one_extrapolation():
    t = _table()
    assert t.offset_db(F1, 30.0) == pytest.approx(-29.5)              # between measured gains
    assert t.offset_db(F1, 10.0) == pytest.approx(-20.0 + 10.0)       # below: slope -1 from G=20
    assert t.offset_db(F1, 70.0) == pytest.approx(-58.0 - 10.0)       # above: slope -1 from G=60
    assert t.offset_db(F3, 0.0) == pytest.approx(-38.0 + 40.0)        # single gain point: slope -1
    assert t.offset_db(F3, 73.0) == pytest.approx(-38.0 - 33.0)


def test_frequency_interpolation_and_constant_beyond_ends():
    t = _table()
    fm = 0.5 * (F1 + F2)
    o1, o2 = t.offset_db(F1, 40.0), t.offset_db(F2, 40.0)
    assert o2 == pytest.approx(-40.5)
    assert t.offset_db(fm, 40.0) == pytest.approx(0.5 * (o1 + o2))
    assert t.offset_db(100e6, 40.0) == pytest.approx(o1)              # below first frequency
    assert t.offset_db(3.8e9, 40.0) == pytest.approx(t.offset_db(F3, 40.0))
    f = F2 + 0.25 * (F3 - F2)
    assert t.offset_db(f, 10.0) == pytest.approx(0.75 * t.offset_db(F2, 10.0) + 0.25 * t.offset_db(F3, 10.0))


def test_vectorized_and_scalar_types():
    t = _table()
    f = np.array([F1, F2, 3e9])
    out = t.offset_db(f, 40.0)
    assert isinstance(out, np.ndarray) and out.shape == (3,)
    assert out == pytest.approx([t.offset_db(x, 40.0) for x in f])
    grid = t.offset_db(f[:, None], np.array([20.0, 50.0])[None, :])
    assert grid.shape == (3, 2)
    assert isinstance(t.offset_db(F2, 40), float)
    assert t.to_dbm(-30.0, F2, 40.0) == pytest.approx(-70.5)


def test_duplicates_are_averaged_and_single_node_table():
    t = PowerCalTable([(1e9, 40.0, -35.0), (1e9, 40.0, -35.4)])
    assert t.points == [(1e9, 40.0, pytest.approx(-35.2))]
    assert t.offset_db(2e9, 50.0) == pytest.approx(-45.2)             # one node: f constant, slope -1


def test_from_model_and_fit_from_measurements():
    m = PowerCalTable.from_model(5.0)                                  # sim: dBFS = dBm + G - 5
    assert m.offset_db(2.4e9, 40.0) == pytest.approx(-35.0)
    m2 = PowerCalTable.from_model([4.0, 6.0], freqs=[1e9, 2e9])
    assert m2.offset_db(1.5e9, 10.0) == pytest.approx(-5.0)
    with pytest.raises(ConfigError):
        PowerCalTable.from_model([1.0], freqs=[1e9, 2e9])
    fit = PowerCalTable.fit_from_measurements([
        {"f_hz": 1e9, "gain_db": 30.0, "p_in_dbm": -60.0, "p_meas_dbfs": -35.2},
        {"f_hz": 1e9, "gain_db": 50.0, "p_in_dbm": -80.0, "p_meas_dbfs": -35.0},
    ])
    assert fit.offset_db(1e9, 30.0) == pytest.approx(-24.8)
    assert fit.offset_db(1e9, 40.0) == pytest.approx(-34.9)
    assert fit.meta["method"] == "measured" and len(fit.meta["rows"]) == 2
    with pytest.raises(ConfigError, match="eksik"):
        PowerCalTable.fit_from_measurements([{"f_hz": 1e9, "gain_db": 30.0}])


def test_round_trip_and_validation():
    t = _table()
    back = PowerCalTable.from_dict(json.loads(json.dumps(t.to_dict(), allow_nan=False)))
    assert back == t and back.meta == {"source": "unit"}
    with pytest.raises(CalibrationError):
        PowerCalTable.from_dict({"points": []})
    with pytest.raises(CalibrationError):
        PowerCalTable.from_dict({"points": [[1e9, 40.0]]})
    with pytest.raises(CalibrationError, match="bilinmeyen"):
        PowerCalTable.from_dict({"points": [[1e9, 40.0, -35.0]], "x": 1})
    for bad in ([], [(0.0, 1.0, 2.0)], [(1e9, float("nan"), 0.0)], [(1e9, 1.0)]):
        with pytest.raises(ConfigError):
            PowerCalTable(bad)
    with pytest.raises(ConfigError):
        t.offset_db(float("inf"), 40.0)


# =========================================================================== measurement (simulator)
def test_measure_power_cal_then_minus60_dbm_tone_reads_minus60():
    w = make_world()
    dev = make_device(w, fc=915e6, fs=2e6, gain=20, ppm=-4.0)
    src = SimSignalSource(w)
    table = measure_power_cal(dev, src, [0.9e9, 1.1e9], [30.0, 50.0], -60.0)
    # simulator level model: dBm = dBFS - G + 5
    for _f, g, o in table.points:
        assert o == pytest.approx(5.0 - g, abs=0.05)
    assert table.meta["source"] == "sim_src" and len(table.meta["rows"]) == 4
    assert (dev.fc, dev.gains) == (915e6, [20.0])                      # restored
    assert w.get("sim_src").enabled is False                           # source left off

    # an independent capture at an interpolated (f, G) point
    dev.set_center_freq(1.0e9)
    dev.set_gain(40.0)
    src.set_frequency(1.0e9 + 300e3)
    src.set_power(-60.0)
    src.set_enabled(True)
    dev.mark_settling()
    spec = spectrum_from_block(dev.capture(1 << 16), 4096)
    cal = DeviceCalibration(serial=dev.info.serial, power=table)
    out = apply_calibration(spec, cal, "software")
    assert out.unit == "dBm" and out.meta["calibrated"] is True
    f_app = (1.0e9 + 300e3) / (1 - 4.0e-6)
    assert out.band_power_db(f_app - 6 * out.df_hz, f_app + 6 * out.df_hz) == pytest.approx(-60.0, abs=0.5)
    assert out.band_power_db(f_app - 6 * out.df_hz, f_app + 6 * out.df_hz) == pytest.approx(-60.0, abs=0.05)


def test_p_in_per_gain_and_callable():
    w = make_world()
    dev = make_device(w, fc=1e9, fs=2e6, gain=20, ppm=0.0)
    src = SimSignalSource(w)
    t1 = measure_power_cal(dev, src, [1e9], [10.0, 60.0], [-40.0, -90.0], n=1 << 14)
    assert [r["p_in_dbm"] for r in t1.meta["rows"]] == [-40.0, -90.0]
    t2 = measure_power_cal(dev, src, [1e9], [10.0, 60.0], lambda f, g: -30.0 - g, n=1 << 14)
    assert [r["p_in_dbm"] for r in t2.meta["rows"]] == [-40.0, -90.0]
    assert t2.offset_db(1e9, 35.0) == pytest.approx(-30.0, abs=0.05)
    with pytest.raises(ConfigError):
        measure_power_cal(dev, src, [1e9], [10.0, 60.0], [-40.0], n=1 << 14)


def test_clipping_and_low_snr_raise():
    w = make_world()
    dev = make_device(w, fc=1e9, fs=2e6, gain=20)
    src = SimSignalSource(w)
    with pytest.raises(CalibrationError, match="yüksek"):
        measure_power_cal(dev, src, [1e9], [60.0], -10.0, n=1 << 14)
    assert w.get("sim_src").enabled is False
    with pytest.raises(CalibrationError, match="SNR"):
        measure_power_cal(dev, src, [1e9], [0.0], -140.0, n=1 << 14)


class _RelativeSource(SignalSource):
    name = "rel"
    absolute_power = False

    def set_frequency(self, hz):
        return hz

    def set_power(self, dbm):
        return dbm

    def set_enabled(self, on):
        return 0


def test_requires_absolute_power_source():
    dev = make_device(make_world(), fc=1e9, fs=2e6)
    with pytest.raises(CalibrationError, match="mutlak"):
        measure_power_cal(dev, _RelativeSource(), [1e9], [40.0], -60.0)
    with pytest.raises(CalibrationError):
        measure_power_cal(dev, None, [1e9], [40.0], -60.0)


def test_measure_tone_dbfs_synthetic_noise_corrected():
    rng = np.random.default_rng(11)
    n, fs = 1 << 15, 1e6
    k = np.arange(n)
    x = 10 ** (-40 / 20) * np.exp(2j * np.pi * 123.4e3 * k / fs)
    x += np.sqrt(10 ** (-60 / 10) / 2) * (rng.standard_normal(n) + 1j * rng.standard_normal(n))
    p, snr, f_pk = measure_tone_dbfs(x, fs, 0.0, 123e3, 5e3)
    assert p == pytest.approx(-40.0, abs=0.05)
    assert f_pk == pytest.approx(123.4e3, abs=fs / 4096)
    assert snr == pytest.approx(-40.0 - (-60.0 + 10 * np.log10(2.004 / 4096)), abs=0.5)
