"""SimWorld / Emitter: validation, scene timeline, device registry, thread safety."""

from __future__ import annotations

import math
import threading
import time

import pytest

from eds.core.config import SimConfig
from eds.core.errors import ConfigError
from eds.hal.sim import Emitter, SimWorld
from eds.hal.sim.signals import (
    GSM_DEFAULT_BW_HZ,
    bessel_jn,
    gate_intervals,
    image_rejection_db,
    interval_indices,
)
from eds.hal.sim.world import FIRST_DEVICE_PPM, HASHED_PPM_SPAN
from tests.hal.sim_fixtures import expected_irr_db, install_fake_clock


# --------------------------------------------------------------------------- Emitter
def test_emitter_defaults_and_param_merge():
    e = Emitter("p", 1e9, -50.0, kind="pulse", params={"pw_s": 2e-5})
    assert e.params == {"pri_s": 1e-3, "pw_s": 2e-5, "t0_s": 0.0}
    fm = Emitter("f", 1e9, -50.0, kind="fm")
    assert fm.params["dev_hz"] == 5e3 and fm.params["mod_hz"] == 1e3
    g = Emitter("g", 935e6, -60.0, kind="gsm_fcch")
    assert g.bw_hz == GSM_DEFAULT_BW_HZ
    c = Emitter("c", 1e9, -50.0, params={"phase_deg": 30.0})
    assert c.params == {"phase_deg": 30.0}


@pytest.mark.parametrize("kw", [
    {"kind": "am"},
    {"kind": "noise"},                                   # needs bw_hz > 0
    {"freq_hz": -1.0},
    {"power_dbm": float("nan")},
    {"kind": "pulse", "params": {"pri_s": 1e-3, "pw_s": 2e-3}},
    {"kind": "fm", "params": {"mod_hz": 0.0}},
    {"params": {"dev_hz": 1.0}},                         # not a cw parameter
    {"kind": "gsm_fcch", "params": {"period_frames": 2.5}},
    {"bw_hz": -5.0},
    {"enabled": 1},
])
def test_emitter_validation(kw):
    base = {"name": "x", "freq_hz": 1e9, "power_dbm": -50.0}
    base.update(kw)
    with pytest.raises(ConfigError):
        Emitter(**base)


def test_emitter_empty_name_rejected():
    with pytest.raises(ConfigError):
        Emitter("", 1e9, -50.0)


# --------------------------------------------------------------------------- SimWorld basics
def test_from_config_builds_emitters():
    cfg = SimConfig(emitters=[
        {"name": "a", "freq_hz": 915e6, "power_dbm": -70.0},
        {"name": "b", "freq_hz": 2.4e9, "power_dbm": -60.0, "kind": "noise", "bw_hz": 1e6, "aoa_deg": 20.0},
    ], temperature_c=41.5, seed=99)
    w = SimWorld.from_config(cfg)
    assert [e.name for e in w.emitters] == ["a", "b"]
    assert w.get("b").kind == "noise" and w.get("b").aoa_deg == 20.0
    assert w.temperature_c == 41.5 and w.seed == 99


@pytest.mark.parametrize("bad", [
    [{"name": "a", "freq_hz": 1e9, "power_dbm": -70.0, "colour": "red"}],
    [{"name": "a", "freq_hz": 1e9}],
    [{"name": "a", "freq_hz": 1e9, "power_dbm": -70.0}, {"name": "a", "freq_hz": 2e9, "power_dbm": -70.0}],
    ["not-a-table"],
])
def test_from_config_rejects_bad_tables(bad):
    with pytest.raises(ConfigError):
        SimWorld.from_config(SimConfig(emitters=bad))


def test_emitters_are_copies_and_crud():
    w = SimWorld([Emitter("a", 1e9, -50.0)])
    lst = w.emitters
    lst[0].power_dbm = 0.0
    assert w.get("a").power_dbm == -50.0             # mutating a copy has no effect
    got = w.get("a")
    got.freq_hz = 5.0
    assert w.get("a").freq_hz == 1e9
    stored = w.add_emitter(Emitter("a", 2e9, -40.0))  # replace by name
    assert stored.freq_hz == 2e9 and len(w.emitters) == 1
    upd = w.update("a", power_dbm=-30.0, aoa_deg=15.0)
    assert upd.power_dbm == -30.0 and w.get("a").aoa_deg == 15.0
    with pytest.raises(ConfigError):
        w.update("a", colour="red")
    with pytest.raises(ConfigError):
        w.update("a", kind="radar")
    with pytest.raises(ConfigError):
        w.update("nope", power_dbm=1.0)
    w.remove_emitter("a")
    assert w.get("a") is None
    with pytest.raises(ConfigError):
        w.remove_emitter("a")
    w.remove_emitter("a", missing_ok=True)


def test_update_merges_params_and_resets_on_kind_change():
    w = SimWorld([Emitter("p", 1e9, -50.0, kind="pulse", params={"pri_s": 2e-3, "pw_s": 1e-4})])
    assert w.update("p", params={"pw_s": 5e-5}).params == {"pri_s": 2e-3, "pw_s": 5e-5, "t0_s": 0.0}
    fm = w.update("p", kind="fm")
    assert fm.params == {"dev_hz": 5e3, "mod_hz": 1e3}
    assert w.update("p", kind="pulse", params={"pri_s": 1e-2}).params["pri_s"] == 1e-2
    with pytest.raises(ConfigError):
        w.update("p", params={"dev_hz": 1.0})                # not a pulse parameter
    with pytest.raises(ConfigError):
        w.update("p", params=[1, 2])


def test_set_enabled_returns_time_ns_and_logs():
    w = SimWorld([Emitter("a", 1e9, -50.0, enabled=False)])
    t0 = time.time_ns()
    t = w.set_enabled("a", True)
    t1 = time.time_ns()
    assert t0 <= t <= t1
    assert w.get("a").enabled is True
    log = w.switch_log()
    assert log[-1]["name"] == "a" and log[-1]["on"] is True and log[-1]["t_ns"] == t
    with pytest.raises(ConfigError):
        w.set_enabled("missing", True)


def test_temperature_setter_validates():
    w = SimWorld()
    w.temperature_c = 50.0
    assert w.temperature_c == 50.0
    with pytest.raises(ConfigError):
        w.temperature_c = float("inf")
    with pytest.raises(ConfigError):
        SimWorld(seed=-1)


# --------------------------------------------------------------------------- timeline
def test_scene_timeline_maps_switch_times(monkeypatch):
    clk = install_fake_clock(monkeypatch)
    w = SimWorld([Emitter("a", 1e9, -50.0, enabled=False)])
    clk.advance(1.0)
    t_on = clk.t
    w.set_enabled("a", True)
    clk.advance(1.0)
    t_off = clk.t
    w.set_enabled("a", False)
    sc = w.scenes_between(t_on - 0.5, t_on - 0.1)
    assert len(sc) == 1 and not sc[0].emitters[0].enabled
    sc = w.scenes_between(t_on - 0.5, t_on + 0.5)
    assert [s.emitters[0].enabled for s in sc] == [False, True]
    assert sc[1].t_mono == pytest.approx(t_on)
    sc = w.scenes_between(t_on + 0.1, t_off + 5.0)
    assert [s.emitters[0].enabled for s in sc] == [True, False]
    assert w.scenes_between(t_off + 1, t_off + 2)[0] is w.current_scene()


def test_history_is_pruned(monkeypatch):
    clk = install_fake_clock(monkeypatch)
    w = SimWorld([Emitter("a", 1e9, -50.0)])
    for _ in range(10):
        clk.advance(SimWorld.HISTORY_S / 4)
        w.update("a", power_dbm=-40.0)
    sc = w.scenes_between(-math.inf, clk.t)
    assert len(sc) <= 6                                  # only ~HISTORY_S worth kept
    assert sc[-1] is w.current_scene()


def test_replace_emitters_is_one_scene_change():
    w = SimWorld([Emitter("a", 1e9, -50.0), Emitter("b", 1e9, -50.0)])
    v0 = w.version
    w.replace_emitters(remove=["a", "b", "zz"], add=[Emitter("c", 1e9, -40.0)])
    assert w.version == v0 + 1
    assert [e.name for e in w.emitters] == ["c"]


# --------------------------------------------------------------------------- registry
def test_device_registry_ppm_rules():
    w = SimWorld()
    assert w.register_device("rxA") == FIRST_DEVICE_PPM
    p1 = w.register_device("rx1")
    assert abs(p1) <= HASHED_PPM_SPAN and p1 != FIRST_DEVICE_PPM
    assert SimWorld().register_device("zzz") == FIRST_DEVICE_PPM       # first in its own world
    w2 = SimWorld()
    w2.register_device("first")
    assert w2.register_device("rx1") == p1                             # hash is process independent
    assert w.register_device("rxA") == FIRST_DEVICE_PPM               # re-registration is stable
    # shared clock: later members inherit the first member's ppm
    w3 = SimWorld()
    assert w3.register_device("a", "ref", 3.25) == 3.25
    assert w3.register_device("b", "ref") == 3.25
    assert w3.register_device("c", "ref", 3.25) == 3.25
    with pytest.raises(ConfigError):
        w3.register_device("d", "ref", -1.0)
    assert w3.clock_ppm("ref") == 3.25
    assert w3.device_names == ["a", "b", "c", "d"]


# --------------------------------------------------------------------------- kernels
def test_irr_formula_kernel():
    assert image_rejection_db(0.4, 2.5) == pytest.approx(expected_irr_db(0.4, 2.5), abs=1e-9)
    assert image_rejection_db(0.0, 0.0) == math.inf


def test_bessel_matches_series():
    # J_n(x) power series for a cross-check (scipy not needed)
    def jn(n, x):
        return sum((-1) ** m / (math.factorial(m) * math.factorial(m + n)) * (x / 2) ** (2 * m + n) for m in range(60))
    j = bessel_jn(2.4, 10)
    for n in range(0, 8):
        assert j[10 + n] == pytest.approx(jn(n, 2.4), abs=1e-12)
        assert j[10 - n] == pytest.approx((-1) ** n * jn(n, 2.4), abs=1e-12)


def test_gate_intervals_match_definition():
    import numpy as np

    u0, du, n, duty = 0.73, 1.0 / 997.3, 20000, 0.137
    s, e = gate_intervals(u0, du, n, duty)
    idx = interval_indices(s, e)
    ref = np.nonzero(((u0 + du * np.arange(n)) % 1.0) < duty)[0]
    assert np.array_equal(idx, ref)


# --------------------------------------------------------------------------- threads
def test_world_concurrent_mutation_is_consistent():
    w = SimWorld([Emitter(f"e{i}", 1e9 + i * 1e3, -60.0) for i in range(4)])
    errors: list[BaseException] = []

    def worker(k: int) -> None:
        try:
            for j in range(300):
                name = f"e{(k + j) % 4}"
                w.set_enabled(name, bool(j % 2))
                w.update(name, power_dbm=-60.0 + (j % 5))
                w.scenes_between(-math.inf, math.inf)
        except BaseException as ex:  # noqa: BLE001 - collected and re-raised in the test thread
            errors.append(ex)

    th = [threading.Thread(target=worker, args=(k,)) for k in range(4)]
    for t in th:
        t.start()
    for t in th:
        t.join()
    assert not errors
    assert w.version == 4 * 300 * 2
    assert len(w.emitters) == 4
