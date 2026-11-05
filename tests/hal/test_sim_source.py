"""SimSignalSource: the simulated generator used by the measurement suite."""

from __future__ import annotations

import math
import time

import numpy as np
import pytest

from eds.core.errors import ConfigError, DeviceError, NotSupported
from eds.core.units import C_LIGHT
from eds.hal.sim import SimSignalSource, SimWorld
from eds.hal.tx import SignalSource, open_source
from tests.hal.sim_fixtures import apparent_bb, make_device, make_world, tone_power_db, wrap_deg

F0 = 1e9


def test_class_contract():
    w = SimWorld()
    src = SimSignalSource(w)
    assert isinstance(src, SignalSource)
    assert src.absolute_power is True and src.max_tones == 4
    assert src.power_range_db == (-150.0, 10.0)
    e = w.get("sim_src")
    assert e is not None and e.freq_hz == 1e9 and e.power_dbm == -60.0
    assert e.cabled is True and e.enabled is False            # powers up with RF OFF
    d = src.describe()
    assert d["name"] == "sim_src" and d["absolute_power"] is True and d["enabled"] is False


def test_power_at_dut_is_setting_minus_path_loss():
    w = make_world()
    src = SimSignalSource(w, path_loss_db=12.5, freq_hz=F0 + 200e3, power_dbm=-40.0)
    d = make_device(w, gain=40)
    t0 = time.time_ns()
    t_on = src.set_enabled(True)
    assert t0 <= t_on <= time.time_ns()
    x = d.capture(65536).samples[0]
    fb = apparent_bb(d, F0 + 200e3)
    assert tone_power_db(x, d.fs, fb) == pytest.approx(-40.0 - 12.5 + 40.0 - 5.0, abs=0.2)
    assert src.set_power(-70.0) == -70.0
    x = d.capture(65536).samples[0]
    assert tone_power_db(x, d.fs, fb) == pytest.approx(-70.0 - 12.5 + 35.0, abs=0.2)
    assert w.get("sim_src").power_dbm == pytest.approx(-82.5)


def test_rf_off_and_frequency_change():
    w = make_world()
    src = SimSignalSource(w, freq_hz=F0 + 100e3, power_dbm=-50.0)
    d = make_device(w, gain=40)
    x = d.capture(32768).samples[0]
    assert tone_power_db(x, d.fs, apparent_bb(d, F0 + 100e3)) < -90.0     # RF off: only noise
    src.set_enabled(True)
    assert src.set_frequency(F0 - 300e3) == F0 - 300e3
    x = d.capture(32768).samples[0]
    assert tone_power_db(x, d.fs, apparent_bb(d, F0 - 300e3)) == pytest.approx(-15.0, abs=0.2)
    assert tone_power_db(x, d.fs, apparent_bb(d, F0 + 100e3)) < -90.0
    src.set_enabled(False)
    x = d.capture(32768).samples[0]
    assert tone_power_db(x, d.fs, apparent_bb(d, F0 - 300e3)) < -90.0


def test_set_tones_gives_two_tones_and_back_to_single():
    w = make_world()
    src = SimSignalSource(w, path_loss_db=3.0)
    src.set_enabled(True)
    f1, f2 = F0 + 150e3, F0 + 250e3
    src.set_tones([f1, f2], [-50.0, -56.0])
    assert sorted(e.name for e in w.emitters) == ["sim_src_0", "sim_src_1"]
    assert all(e.enabled for e in w.emitters)
    d = make_device(w, gain=40)
    x = d.capture(65536).samples[0]
    assert tone_power_db(x, d.fs, apparent_bb(d, f1)) == pytest.approx(-50.0 - 3.0 + 35.0, abs=0.2)
    assert tone_power_db(x, d.fs, apparent_bb(d, f2)) == pytest.approx(-56.0 - 3.0 + 35.0, abs=0.2)
    # a new tone set replaces the previous one
    src.set_tones([f1], [-60.0])
    assert [e.name for e in w.emitters] == ["sim_src_0"]
    # set_frequency returns to single-tone mode with the main emitter
    src.set_frequency(F0 + 50e3)
    assert [e.name for e in w.emitters] == ["sim_src"] and w.get("sim_src").enabled
    with pytest.raises(ConfigError):
        src.set_tones([f1] * 5, [-50.0] * 5)
    with pytest.raises(ConfigError):
        src.set_tones([f1, f2], [-50.0])


def test_set_aoa_changes_geometric_phase():
    w = make_world(seed=11)
    f = F0 + 100e3
    src = SimSignalSource(w, freq_hz=f, power_dbm=-50.0, cabled=False, aoa_deg=0.0)
    src.set_enabled(True)
    kw = {"shared_clock": "ref", "lo_phase_random": False}
    a = make_device(w, "a", **kw)
    b = make_device(w, "b", position_m=[0.15, 0.0], **kw)
    a.start_stream()
    b.start_stream()

    def rel() -> float:
        return math.degrees(np.angle(np.vdot(a.read(16384).samples[0], b.read(16384).samples[0])))

    assert wrap_deg(rel()) == pytest.approx(0.0, abs=1.0)         # broadside
    assert src.set_aoa(-40.0) == -40.0
    a.flush()
    b.flush()
    expected = math.degrees(2 * math.pi * 0.15 * math.sin(math.radians(-40.0)) / (C_LIGHT / f))
    assert wrap_deg(rel() - expected) == pytest.approx(0.0, abs=1.0)


def test_validation_and_close():
    w = SimWorld()
    with pytest.raises(ConfigError):
        SimSignalSource(w, power_dbm=20.0)                         # above +10 dBm
    with pytest.raises(ConfigError):
        SimSignalSource(w, freq_hz=-1.0)
    with pytest.raises(ConfigError):
        SimSignalSource("not a world")                             # type: ignore[arg-type]
    src = SimSignalSource(w, name="gen")
    with pytest.raises(ConfigError):
        src.set_power(-200.0)
    with pytest.raises(ConfigError):
        src.set_frequency(float("nan"))
    src.set_tones([F0, F0 + 1e3], [-60.0, -60.0])
    src.close()
    assert w.emitters == []
    src.close()                                                    # idempotent
    with pytest.raises(DeviceError):
        src.set_enabled(True)


def test_context_manager_and_factory():
    w = SimWorld()
    with open_source("sim", {"name": "s2", "freq_hz": 2e9, "power_dbm": -30.0}, world=w) as src:
        assert isinstance(src, SimSignalSource)
        src.set_enabled(True)
        assert w.get("s2").enabled
    assert w.get("s2") is None                                     # __exit__: RF off + close
    with pytest.raises(ConfigError):
        open_source("sim", {})


def test_multitone_not_supported_is_not_raised():
    """SimSignalSource overrides the optional multi-tone API (base raises NotSupported)."""
    src = SimSignalSource(SimWorld())
    try:
        src.set_tones([F0], [-60.0])
    except NotSupported:  # pragma: no cover - would be a regression
        pytest.fail("SimSignalSource must support set_tones")
