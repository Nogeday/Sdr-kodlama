"""Tests of the shipped configuration files (config/*.toml, config/profiles/*.toml,
config/ktr_targets.toml).

The configs are part of the measurement instrument: a value that silently violates the
hardware profile, a source argument the stimulus class does not accept, or a scene whose
geometry contradicts the DF sign convention would only surface on the bench. These tests
load every file strictly, check it against the device profile and the consuming classes,
and verify the physics of the simulator configs numerically (levels in dBFS, interferometer
phase) per docs/ARCHITECTURE.md §2.
"""

from __future__ import annotations

import inspect
import math
import re
import shutil
import subprocess
import sys
from pathlib import Path

import numpy as np
import pytest

from eds.core.config import AppConfig, load_config
from eds.core.paths import repo_root
from eds.core.profile import DeviceProfile, load_profile
from eds.core.units import C_LIGHT, db10, wavelength, wrap_deg
from eds.measure.base import SPEC_IDS

if sys.version_info >= (3, 11):
    import tomllib
else:  # pragma: no cover
    import tomli as tomllib

CONFIG_DIR = repo_root() / "config"
PROFILES_DIR = CONFIG_DIR / "profiles"
TARGETS_FILE = CONFIG_DIR / "ktr_targets.toml"
APP_CONFIGS = sorted(p for p in CONFIG_DIR.glob("*.toml") if p.name != TARGETS_FILE.name)


def _is_sim_config(path: Path) -> bool:
    with path.open("rb") as f:
        devs = tomllib.load(f).get("device", [])
    devs = [devs] if isinstance(devs, dict) else devs
    return bool(devs) and all(d.get("backend") == "sim" for d in devs)


SIM_CONFIGS = [p for p in APP_CONFIGS if _is_sim_config(p)]
PROFILE_FILES = sorted(PROFILES_DIR.glob("*.toml"))
EXPECTED_APP_CONFIGS = {"rx_sim.toml", "rx_pluto.toml", "dual_pluto.toml", "dual_sim.toml"}
SIM_GAIN_OFFSET_DB = -5.0          # simulator level model: dBFS = dBm + G + gain_offset_db (default)
FCCH_OFFSET_HZ = 1625e3 / 24       # GSM FCCH tone offset (+67.708 kHz)


def _ids(paths: list[Path]) -> list[str]:
    return [p.name for p in paths]


def _load(name: str, **overrides) -> AppConfig:
    return load_config(CONFIG_DIR / name, overrides=overrides or None)


def _open_sim(app: AppConfig):
    """World + configured-but-not-streaming devices for an all-sim config.

    Mirrors ``eds.hal.factory.open_device`` but builds a fresh world explicitly, so a test
    never depends on the factory's per-AppConfig world cache.
    """
    from eds.hal.sim import SimRxDevice, SimWorld

    world = SimWorld.from_config(app.sim)
    devs = []
    try:
        for d in app.devices:
            devs.append(SimRxDevice(d, load_profile(d.profile), world))
    except Exception:
        for o in devs:
            o.close()
        raise
    return world, devs


def _bh4(n: int) -> np.ndarray:
    """4-term Blackman-Harris (periodic), numpy only (independent of eds.dsp)."""
    k = np.arange(n) * 2.0 * np.pi / n
    return 0.35875 - 0.48829 * np.cos(k) + 0.14128 * np.cos(2 * k) - 0.01168 * np.cos(3 * k)


def _band_power_dbfs(x: np.ndarray, fs: float, f_lo: float, f_hi: float) -> float:
    """Power in [f_lo, f_hi] (baseband Hz): sum P[k] / ENBW_bins with power-spectrum scaling."""
    n = x.size
    w = _bh4(n)
    p = np.abs(np.fft.fftshift(np.fft.fft(x * w))) ** 2 / np.sum(w) ** 2
    f = (np.arange(n) - n // 2) * fs / n
    enbw_bins = n * np.sum(w**2) / np.sum(w) ** 2
    sel = (f >= f_lo) & (f <= f_hi)
    return float(db10(np.sum(p[sel]) / enbw_bins))


@pytest.fixture(scope="module")
def apps() -> dict[str, AppConfig]:
    """Every app config loaded once per module (read-only in the tests)."""
    return {p.name: load_config(p) for p in APP_CONFIGS}


# =============================================================================== inventory
def test_expected_config_files_exist():
    assert EXPECTED_APP_CONFIGS <= {p.name for p in APP_CONFIGS}
    assert TARGETS_FILE.is_file()


# =============================================================================== every app config
@pytest.mark.parametrize("path", APP_CONFIGS, ids=_ids(APP_CONFIGS))
class TestEveryAppConfig:
    def test_loads_strictly(self, path):
        app = load_config(path)
        assert app.source_path == str(path)
        names = [d.name for d in app.devices]
        assert names and len(set(names)) == len(names)
        assert names[0] == "rx0"

    def test_devices_reference_existing_profiles(self, path):
        for d in load_config(path).devices:
            prof = load_profile(d.profile)
            assert prof.status == "supported", f"{d.name}: planned profile {d.profile} in a runnable config"
            assert len(d.channels) <= prof.rx_channels
            if d.backend == "soapy":
                assert d.args.get("driver") == prof.soapy_driver
                assert all(isinstance(k, str) and isinstance(v, str) for k, v in d.args.items())

    def test_rx_settings_within_profile_and_usb2(self, path):
        app = load_config(path)
        rx = app.rx
        for d in app.devices:
            prof = load_profile(d.profile)
            gain = rx.gain_db if rx.gain_mode in ("manual", "soft_agc") else None
            err, warn = prof.check_rx(center_freq_hz=rx.center_freq_hz, sample_rate_hz=rx.sample_rate_hz,
                                      bandwidth_hz=rx.bandwidth_hz, gain_db=gain, n_channels=len(d.channels))
            assert err == [], err
            assert warn == [], f"continuous RX must stay below the {prof.interface} practical limit: {warn}"
            # per device link: continuous streaming must be sustainable with margin
            assert prof.wire_rate_bytes_per_s(rx.sample_rate_hz) <= prof.interface_practical_bytes_per_s
        assert rx.block_size > 0 and rx.buffer_size > 0
        assert rx.gain_mode == "manual", "measurement-grade configs keep HW AGC off"

    def test_sweep_settings_within_profile(self, path):
        app = load_config(path)
        sw = app.sweep
        assert sw.start_hz < sw.stop_hz
        for d in app.devices:
            prof = load_profile(d.profile)
            assert prof.freq_min_hz <= sw.start_hz and sw.stop_hz <= prof.freq_max_hz
            # the sweep covers the WHOLE official band
            assert (sw.start_hz, sw.stop_hz) == (prof.freq_min_hz, prof.freq_max_hz)
            for f in (sw.start_hz, sw.stop_hz):
                err, _ = prof.check_rx(center_freq_hz=f, sample_rate_hz=sw.sample_rate_hz,
                                       bandwidth_hz=sw.bandwidth_hz, gain_db=sw.gain_db)
                assert err == [], err
        assert sw.usable_fraction * sw.sample_rate_hz > 0

    def test_targets_file_resolves(self, path):
        app = load_config(path)
        assert (CONFIG_DIR / app.measure.targets_file).resolve() == TARGETS_FILE.resolve()

    def test_source_args_match_source_constructor(self, path):
        app = load_config(path)
        kind = app.measure.source
        if kind == "none":
            assert app.measure.source_args == {}
            return
        if kind == "sim":
            from eds.hal.sim.source import SimSignalSource as cls
            implicit = {"world"}
        elif kind == "pluto_tx":
            from eds.hal.tx_hw import SoapyTxSource as cls
            implicit = set()
        elif kind == "scpi":
            from eds.hal.tx_hw import ScpiSignalGenerator as cls
            implicit = set()
        else:
            from eds.hal.tx_hw import ManualSource as cls
            implicit = {"prompt"}
        params = inspect.signature(cls.__init__).parameters
        accepted = set(params) - {"self"}
        assert set(app.measure.source_args) <= accepted - implicit, (
            f"{path.name}: unknown source_args {set(app.measure.source_args) - accepted}")
        required = {n for n, p in params.items() if n != "self" and p.default is inspect.Parameter.empty
                    and p.kind not in (p.VAR_KEYWORD, p.VAR_POSITIONAL)}
        assert required - implicit <= set(app.measure.source_args)

    def test_sim_scene_inside_official_band(self, path):
        app = load_config(path)
        prof = load_profile(app.devices[0].profile)
        names = [e["name"] for e in app.sim.emitters]
        assert len(set(names)) == len(names)
        for e in app.sim.emitters:
            half = e.get("bw_hz", 0.0) / 2
            assert prof.freq_min_hz <= e["freq_hz"] - half and e["freq_hz"] + half <= prof.freq_max_hz, e["name"]


@pytest.mark.parametrize("path", SIM_CONFIGS, ids=_ids(SIM_CONFIGS))
def test_sim_configs_open_configure_and_build_source(path):
    """Every [[device]].sim table and [[sim.emitters]] entry is accepted by the simulator,
    the [rx] section configures without profile warnings, and the stimulus can be built."""
    from eds.hal.tx import open_source

    app = load_config(path)
    world, devs = _open_sim(app)
    try:
        assert [e.name for e in world.emitters] == [e["name"] for e in app.sim.emitters]
        for d in devs:
            assert d.configure(app.rx) == []
            assert d.fc == app.rx.center_freq_hz and d.fs == app.rx.sample_rate_hz
            assert d.gains == [app.rx.gain_db]
        src = open_source(app.measure.source, app.measure.source_args, world=world)
        try:
            assert src is not None and src.absolute_power
        finally:
            src.close()
    finally:
        for d in devs:
            d.close()


def test_sim_config_inventory():
    assert {p.name for p in SIM_CONFIGS} >= {"rx_sim.toml", "dual_sim.toml"}
    assert not {p.name for p in SIM_CONFIGS} & {"rx_pluto.toml", "dual_pluto.toml"}


# =============================================================================== rx_sim.toml
class TestRxSim:
    @pytest.fixture
    def app(self, apps: dict[str, AppConfig]) -> AppConfig:
        return apps["rx_sim.toml"]

    def test_device(self, app):
        assert len(app.devices) == 1
        d = app.devices[0]
        assert (d.name, d.backend, d.profile) == ("rx0", "sim", "ad9363_pluto")
        assert d.sim["ppm"] == pytest.approx(11.7) and d.sim["realtime"] is True
        assert app.measure.source == "sim"
        rx = app.rx
        assert (rx.sample_rate_hz, rx.bandwidth_hz, rx.gain_db, rx.gain_mode) == (4e6, 4e6, 50.0, "manual")

    def test_scene_composition(self, app):
        em = {e["name"]: e for e in app.sim.emitters}
        assert 6 <= len(em) <= 8
        kinds = {e.get("kind", "cw") for e in em.values()}
        assert {"cw", "noise", "fm", "pulse", "gsm_fcch"} <= kinds

        def one(kind, freq):
            hits = [e for e in em.values() if e.get("kind", "cw") == kind and e["freq_hz"] == pytest.approx(freq)]
            assert len(hits) == 1, (kind, freq)
            return hits[0]

        gsm = one("gsm_fcch", 935.2e6)
        assert gsm["power_dbm"] == -60.0
        assert one("noise", 1842.5e6)["bw_hz"] == 10e6
        assert one("noise", 2.437e9)["bw_hz"] == 20e6
        one("fm", 446.00625e6)
        one("fm", 433.92e6)
        radar = one("pulse", 2.8e9)
        assert radar["params"]["pri_s"] == pytest.approx(1e-3)
        assert 10e-6 <= radar["params"]["pw_s"] <= radar["params"]["pri_s"]
        beacon = one("cw", 1.2e9)
        assert beacon["power_dbm"] == -110.0

    def test_ppm_reference_inside_default_band(self, app):
        """The GSM FCCH tone (PPM reference) is visible at the default tuning, away from DC."""
        rx, dsp = app.rx, app.dsp
        f_fcch = 935.2e6 + FCCH_OFFSET_HZ
        f_bb_apparent = f_fcch / (1 + app.devices[0].sim["ppm"] * 1e-6) - rx.center_freq_hz
        assert abs(f_bb_apparent) < dsp.usable_fraction * rx.sample_rate_hz / 2
        rbw = 2.0043529 * rx.sample_rate_hz / dsp.nfft
        assert abs(f_bb_apparent) > 10 * dsp.dc_notch_bins * rbw

    def test_no_in_band_emitter_clips(self, app):
        rx = app.rx
        for e in app.sim.emitters:
            if abs(e["freq_hz"] - rx.center_freq_hz) < rx.sample_rate_hz / 2 + e.get("bw_hz", 0.0) / 2:
                assert e["power_dbm"] + rx.gain_db + SIM_GAIN_OFFSET_DB <= -10.0, e["name"]

    def test_gsm_level_reads_minus_15_dbfs(self):
        """-60 dBm BCCH at G = 50 dB -> -60 + 50 - 5 = -15 dBFS (power-spectrum band power)."""
        app = _load("rx_sim.toml", **{"device.sim.realtime": False})
        _, (dev,) = _open_sim(app)
        try:
            dev.configure(app.rx)
            blk = dev.capture(1 << 17)
            x = blk.channel(0).astype(np.complex128)
            assert not blk.settling
            peak = float(np.max(np.maximum(np.abs(x.real), np.abs(x.imag))))
            assert peak < blk.clip_level
            ppm = app.devices[0].sim["ppm"]
            f_bb = 935.2e6 / (1 + ppm * 1e-6) - blk.fc           # apparent baseband carrier
            p = _band_power_dbfs(x, blk.fs, f_bb - 150e3, f_bb + 150e3)
            assert p == pytest.approx(-15.0, abs=0.3)
        finally:
            dev.close()


# =============================================================================== rx_pluto.toml
class TestRxPluto:
    @pytest.fixture
    def app(self, apps: dict[str, AppConfig]) -> AppConfig:
        return apps["rx_pluto.toml"]

    def test_device_selection(self, app):
        assert len(app.devices) == 1
        d = app.devices[0]
        assert (d.name, d.backend, d.profile) == ("rx0", "soapy", "ad9363_pluto")
        assert d.args["driver"] == "plutosdr" and d.args["uri"].startswith("usb:")
        assert d.control_uri.startswith("ip:")

    def test_rates(self, app):
        prof = load_profile("ad9363_pluto")
        assert app.rx.sample_rate_hz == 4e6
        assert app.sweep.sample_rate_hz == 20e6 and app.sweep.bandwidth_hz == 20e6
        # 20 MS/s = 80 MB/s exceeds USB2: valid only as settled bursts -> the profile warns
        _, warn = prof.check_rx(sample_rate_hz=app.sweep.sample_rate_hz)
        assert warn and "teorik" in warn[0]

    def test_tx_source(self, app):
        assert app.measure.source == "pluto_tx"
        sa = app.measure.source_args
        tx_args = sa["args"]
        assert tx_args["driver"] == "plutosdr"
        assert tx_args["uri"] != app.devices[0].args["uri"], "TX must be the OTHER Pluto"
        assert tx_args["uri"].startswith("ip:") and tx_args["uri"] != app.devices[0].control_uri
        fs = sa.get("fs", 2.4e6)
        assert 0 < abs(sa.get("tone_offset_hz", 200e3)) <= 0.4 * fs
        # TX streamed over the network gadget: keep its wire rate modest
        assert fs * 4 <= 8e6


# =============================================================================== dual_pluto.toml
class TestDualPluto:
    @pytest.fixture
    def app(self, apps: dict[str, AppConfig]) -> AppConfig:
        return apps["dual_pluto.toml"]

    def test_two_distinct_plutos(self, app):
        assert [d.name for d in app.devices] == ["rx0", "rx1"]
        assert all(d.backend == "soapy" and d.profile == "ad9363_pluto" for d in app.devices)
        uris = [d.args["uri"] for d in app.devices]
        assert len(set(uris)) == 2
        # with two Plutos on USB libiio refuses the ambiguous "usb:" -> explicit addresses
        assert all(u != "usb:" for u in uris)
        ctl = [d.control_uri for d in app.devices]
        assert len(set(ctl)) == 2 and all(c.startswith("ip:") for c in ctl)

    def test_df_baseline_is_half_wavelength(self, app):
        d = app.measure.params["df_rms"]["baseline_m"]
        assert d == pytest.approx(wavelength(1e9) / 2, rel=0.01)
        # unambiguous (|θ| < 90°) up to f = c / (2 d) ≈ 1 GHz
        assert app.rx.center_freq_hz <= C_LIGHT / (2 * d) * 1.001
        assert app.rx.gain_mode == "manual"

    def test_aggregate_usb_load(self, app):
        prof = load_profile("ad9363_pluto")
        total = prof.wire_rate_bytes_per_s(app.rx.sample_rate_hz, len(app.devices))
        assert total < prof.interface_max_bytes_per_s


# =============================================================================== dual_sim.toml
class TestDualSim:
    @pytest.fixture
    def app(self, apps: dict[str, AppConfig]) -> AppConfig:
        return apps["dual_sim.toml"]

    def test_devices_geometry_and_clock(self, app):
        assert [d.name for d in app.devices] == ["rx0", "rx1"]
        pos = [np.asarray(d.sim["position_m"], dtype=float) for d in app.devices]
        np.testing.assert_allclose(pos[0], [0.0, 0.0])
        np.testing.assert_allclose(pos[1], [0.15, 0.0])
        assert all(d.sim["shared_clock"] == "ref40" for d in app.devices)
        baseline = app.measure.params["df_rms"]["baseline_m"]
        assert baseline == pytest.approx(0.15)
        assert baseline == pytest.approx(float(np.linalg.norm(pos[1] - pos[0])))
        assert baseline == pytest.approx(wavelength(1e9) / 2, rel=0.01)

    def test_scene_has_cabled_cal_and_ota_emitter(self, app):
        cabled = [e for e in app.sim.emitters if e.get("cabled", False)]
        ota = [e for e in app.sim.emitters if not e.get("cabled", False)]
        assert len(cabled) >= 1 and len(ota) >= 1
        rx = app.rx
        for e in cabled + ota:      # both inside the usable band, clear of the DC notch
            f_bb = e["freq_hz"] - rx.center_freq_hz
            assert 50e3 < abs(f_bb) < app.dsp.usable_fraction * rx.sample_rate_hz / 2

    def test_group_shares_clock(self, app):
        from eds.hal.group import DeviceGroup

        world, devs = _open_sim(app)
        grp = DeviceGroup(devs)
        try:
            assert grp.num_channels == 2 and grp.coherent is False
            assert world.clock_ppm("ref40") == pytest.approx(app.devices[0].sim["ppm"])
            assert world.device_names == ["rx0", "rx1"]
        finally:
            grp.close()

    def test_interferometer_phase_matches_geometry(self, app):
        """Phase(target) - phase(cabled pilot) between rx1 and rx0 = 2π d sin(θ)/λ.

        The cabled pilot removes the random per-retune LO phase difference; the remaining
        phase is pure geometry, with +θ toward rx1 (ARCHITECTURE §2 DF convention).
        """
        from eds.hal.group import DeviceGroup

        _, devs = _open_sim(app)
        grp = DeviceGroup(devs)
        try:
            grp.configure(app.rx)
            blk = grp.capture(1 << 16)
            x = blk.samples.astype(np.complex128)
            n = blk.n
            w = _bh4(n)
            e = app.devices[0].sim["ppm"] * 1e-6

            def cross_phase(f_rf: float) -> float:
                f_bb = f_rf / (1 + e) - blk.fc
                k = w * np.exp(-2j * np.pi * f_bb * np.arange(n) / blk.fs)
                return float(np.angle(np.sum(x[1] * k) * np.conj(np.sum(x[0] * k))))

            pilot = next(em for em in app.sim.emitters if em.get("cabled", False))
            target = next(em for em in app.sim.emitters if not em.get("cabled", False))
            d = app.measure.params["df_rms"]["baseline_m"]
            lam = wavelength(target["freq_hz"])
            meas = float(wrap_deg(np.degrees(cross_phase(target["freq_hz"]) - cross_phase(pilot["freq_hz"]))))
            expected = math.degrees(2 * math.pi * d * math.sin(math.radians(target["aoa_deg"])) / lam)
            assert meas == pytest.approx(expected, abs=0.5)
            aoa_est = math.degrees(math.asin(math.radians(meas) * lam / (2 * math.pi * d)))
            assert aoa_est == pytest.approx(target["aoa_deg"], abs=0.5)
        finally:
            grp.close()


# =============================================================================== profiles
@pytest.mark.parametrize("path", PROFILE_FILES, ids=_ids(PROFILE_FILES))
def test_every_profile_loads_and_is_consistent(path):
    p = load_profile(path)
    assert isinstance(p, DeviceProfile)
    assert p.name == path.stem and p.chip
    assert p.status in ("supported", "planned")
    assert 0 < p.freq_min_hz < p.freq_max_hz
    assert 0 < p.bw_min_hz < p.bw_max_hz
    assert 0 < p.sample_rate_min_hz < p.sample_rate_max_hz
    assert p.rx_channels >= 1 and p.adc_bits in (8, 12, 14, 16)
    assert p.full_scale_counts == 2.0 ** (p.adc_bits - 1)
    assert p.bytes_per_sample in (2, 4) and p.default_buffer_size > 0 and p.kernel_buffers >= 0
    assert p.ref_clock_hz > 0 and p.ref_clock_ppm > 0
    assert 0 < p.interface_practical_bytes_per_s <= p.interface_max_bytes_per_s
    rates = list(p.candidate_sample_rates_hz)
    assert rates == sorted(rates) and all(p.sample_rate_min_hz <= r <= p.sample_rate_max_hz for r in rates)
    bands = p.gain_bands
    assert bands, "every profile declares its gain range"
    for b in bands:
        assert b.f_min_hz < b.f_max_hz and b.gain_min_db < b.gain_max_db and b.step_db > 0
    for a, b in zip(bands, bands[1:]):
        assert a.f_max_hz == b.f_min_hz, "gain bands must be sorted and contiguous"
    assert bands[0].f_min_hz <= p.freq_min_hz and p.freq_max_hz <= bands[-1].f_max_hz
    if p.coherent_rx:
        assert p.rx_channels > 1


def test_official_ad9363_limits():
    """The default profile carries the official AD9363 / ADALM-Pluto figures."""
    p = load_profile("ad9363_pluto")
    assert (p.freq_min_hz, p.freq_max_hz) == (325e6, 3.8e9)
    assert p.bw_max_hz == 20e6
    assert (p.rx_channels, p.tx_channels, p.coherent_rx) == (1, 1, False)
    assert (p.adc_bits, p.full_scale_counts) == (12, 2048.0)
    assert (p.ref_clock_hz, p.ref_clock_ppm) == (40e6, 25.0)
    assert p.interface == "usb2" and p.interface_max_bytes_per_s == pytest.approx(13 * 512 / 125e-6)
    assert p.soapy_driver == "plutosdr" and p.status == "supported"
    ext = load_profile("ad9364_pluto_ext")
    assert ext.freq_min_hz < p.freq_min_hz and ext.freq_max_hz > p.freq_max_hz   # out-of-spec, opt-in


@pytest.mark.xfail(reason="profile data: sample_rate_min_hz = 65104.17 is above 25e6/384 = 65104.1667, so the "
                          "documented SoapyPlutoSDR minimum is rejected (see requested_changes)")
def test_ad9363_accepts_documented_minimum_rate():
    assert load_profile("ad9363_pluto").check_rx(sample_rate_hz=25e6 / 384)[0] == []


# =============================================================================== KTR targets
EXPECTED_OPS = {
    "sensitivity": "<=", "dynamic_range": ">=", "inst_bw": ">=", "scan_speed": ">=",
    "freq_resolution": "<=", "df_rms": "<=", "image_rejection": ">=", "max_rate": ">=", "latency": "<=",
}


@pytest.fixture(scope="module")
def targets() -> dict:
    with TARGETS_FILE.open("rb") as f:
        return tomllib.load(f)


def test_targets_cover_exactly_the_spec_ids(targets):
    assert list(targets) == list(SPEC_IDS)
    assert set(EXPECTED_OPS) == set(SPEC_IDS)


@pytest.mark.parametrize("spec_id", SPEC_IDS)
def test_target_entry_well_formed(targets, spec_id):
    t = targets[spec_id]
    assert set(t) == {"target", "op", "note"}
    assert isinstance(t["target"], (int, float)) and not isinstance(t["target"], bool)
    assert math.isfinite(t["target"])
    assert t["op"] in ("<=", ">=")
    assert t["op"] == EXPECTED_OPS[spec_id], "direction of 'better' for this spec"
    assert isinstance(t["note"], str) and "ÖRNEK" in t["note"]


def test_targets_physically_plausible_for_ad9363(targets):
    p = load_profile("ad9363_pluto")
    usb2_max_msps = p.interface_max_bytes_per_s / p.bytes_per_sample / 1e6
    assert -170.0 < targets["sensitivity"]["target"] < -50.0                  # dBm
    assert 0.0 < targets["dynamic_range"]["target"] < 6.02 * p.adc_bits + 60  # dB (12-bit + gain management)
    assert 0.0 < targets["inst_bw"]["target"] <= p.bw_max_hz / 1e6           # MHz
    assert targets["scan_speed"]["target"] > 0.0                              # GHz/s
    assert targets["freq_resolution"]["target"] > 0.0                         # Hz
    assert 0.0 < targets["df_rms"]["target"] < 90.0                           # deg
    assert 0.0 < targets["image_rejection"]["target"] < 100.0                 # dB
    assert 0.0 < targets["max_rate"]["target"] <= usb2_max_msps               # MS/s
    assert targets["latency"]["target"] > 0.0                                 # ms


def test_measurement_units_match_target_comments():
    """Target values are in each measurement's unit; the file comments name that unit."""
    from eds.measure import specs  # noqa: F401  (registers measurements)
    from eds.measure.base import MEASUREMENTS

    text = TARGETS_FILE.read_text(encoding="utf-8")
    for sid in SPEC_IDS:
        unit = MEASUREMENTS[sid].unit
        header = next(line for line in text.splitlines() if line.startswith(f"[{sid}]"))
        assert f"birim: {unit}" in header, (sid, unit, header)


# =============================================================================== setup script, udev, docs
SCRIPTS_DIR = repo_root() / "scripts"
DOCS_DIR = repo_root() / "docs"
REQUIRED_APT = ("python3-venv", "python3-pip", "libiio-dev", "libiio-utils", "python3-libiio", "libad9361-dev",
                "soapysdr-tools", "libsoapysdr-dev", "python3-soapysdr", "libzmq3-dev", "cmake", "g++", "git",
                "pkg-config")


def test_udev_rule_covers_pluto_usb_ids():
    text = (SCRIPTS_DIR / "53-adi-plutosdr-usb.rules").read_text(encoding="utf-8")
    rules = [ln for ln in text.splitlines() if ln.strip() and not ln.lstrip().startswith("#")]
    for pid in ("b673", "b674"):     # normal mode, DFU mode
        assert any('ATTRS{idVendor}=="0456"' in r and f'ATTRS{{idProduct}}=="{pid}"' in r and 'GROUP="plugdev"' in r
                   for r in rules), pid
    assert any("ID_MM_DEVICE_IGNORE" in r for r in rules)


def test_install_script_declares_required_packages_and_steps():
    text = (SCRIPTS_DIR / "install_ubuntu.sh").read_text(encoding="utf-8")
    assert text.startswith("#!/usr/bin/env bash")
    assert "set -Eeuo pipefail" in text
    apt_block = text[text.index("APT_PACKAGES=("): text.index(")", text.index("APT_PACKAGES=("))]
    for pkg in REQUIRED_APT:
        assert re.search(rf"(?<![\w-]){re.escape(pkg)}(?![\w-])", apt_block), pkg
    for needle in ("https://github.com/pothosware/SoapyPlutoSDR", "--system-site-packages", '[dev]',
                   'SoapySDRUtil --find="driver=plutosdr"', "iio_info -s", "import SoapySDR, iio",
                   "udevadm control --reload-rules", "usermod -aG plugdev"):
        assert needle in text, needle


@pytest.mark.skipif(shutil.which("bash") is None, reason="bash yok")
def test_install_script_syntax_and_cli():
    script = str(SCRIPTS_DIR / "install_ubuntu.sh")
    assert subprocess.run(["bash", "-n", script], capture_output=True, timeout=10).returncode == 0
    # argument parsing happens before any system change: --help / bad flag are side-effect free
    r = subprocess.run(["bash", script, "--help"], capture_output=True, text=True, timeout=10)
    assert r.returncode == 0 and "--no-build" in r.stdout and "--no-venv" in r.stdout
    r = subprocess.run(["bash", script, "--definitely-not-a-flag"], capture_output=True, text=True, timeout=10)
    assert r.returncode != 0 and "Bilinmeyen seçenek" in r.stderr


def test_measurements_doc_covers_every_spec():
    text = (DOCS_DIR / "MEASUREMENTS.md").read_text(encoding="utf-8")
    headings = [ln for ln in text.splitlines() if ln.startswith("## ")]
    for sid in SPEC_IDS:
        assert any(f"`{sid}`" in h for h in headings), sid
        assert f"eds measure {sid}" in text, sid


def test_hardware_doc_references_existing_files():
    text = (DOCS_DIR / "HARDWARE_PLUTO.md").read_text(encoding="utf-8")
    for ref in re.findall(r"`((?:config|scripts)/[\w./-]+\.(?:toml|sh|rules))`", text):
        assert (repo_root() / ref).is_file(), ref
    for prof in re.findall(r"`([a-z0-9_]+\.toml)`", text):
        assert (PROFILES_DIR / prof).is_file() or (CONFIG_DIR / prof).is_file(), prof
