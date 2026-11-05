"""Unit tests of ``eds.core``: config (strict loading, overrides, enums), device profiles
(validation, USB2 warnings, band-dependent gain), units, core types, errors, time and
paths helpers.

Numerical expectations follow docs/ARCHITECTURE.md §2 (Hz, s, dB; ppm sign; 0 dBFS).
Tests marked ``xfail`` document confirmed defects in core modules owned by the lead; the
fixes are listed in the agent report (requested_changes). They XPASS once fixed.
"""

from __future__ import annotations

import copy
import dataclasses
import json
import math
import re
import time

import numpy as np
import pytest

from eds.core import config as cfgmod
from eds.core import paths, timeutil
from eds.core.config import (
    AppConfig,
    DeviceConfig,
    apply_overrides,
    config_from_dict,
    load_config,
)
from eds.core.errors import (
    BackendUnavailable,
    CalibrationError,
    ConfigError,
    DeviceError,
    DeviceNotFound,
    EDSError,
    MeasurementError,
    NotSupported,
    ProfileViolation,
    StreamError,
    StreamTimeout,
)
from eds.core.profile import DeviceProfile, GainBand, list_profiles, load_profile, profile_from_dict
from eds.core.types import DeviceInfo, GainRange, IQBlock, StreamStats
from eds.core.units import (
    C_LIGHT,
    KT_DBM_HZ,
    db10,
    db20,
    dbm_to_watt,
    fmt_hz,
    lin10,
    lin20,
    watt_to_dbm,
    wavelength,
    wrap_deg,
    wrap_rad,
)

PLUTO = load_profile("ad9363_pluto")

# Section name -> dataclass, derived from AppConfig itself (stays in sync with the code).
SECTIONS = {f.name: f.type for f in dataclasses.fields(AppConfig) if f.name not in ("devices", "source_path")}


def _section_cls(name: str) -> type:
    return type(getattr(AppConfig(), name))


# =============================================================================== config
class TestConfigStrictness:
    def test_defaults_without_file(self):
        app = load_config()
        assert app.source_path == "<defaults>"
        assert len(app.devices) == 1 and app.devices[0].backend == "sim"
        assert app.rx.sample_rate_hz == 4e6 and app.rx.gain_mode == "manual"
        assert app.measure.targets_file == "ktr_targets.toml"
        assert math.isnan(app.cal.ppm_override)

    def test_unknown_top_level_section_rejected(self):
        with pytest.raises(ConfigError, match="rxx"):
            config_from_dict({"rxx": {"gain_db": 10.0}})

    @pytest.mark.parametrize("section", sorted(SECTIONS))
    def test_unknown_key_in_every_section_rejected(self, section):
        with pytest.raises(ConfigError) as ei:
            config_from_dict({section: {"definitely_not_a_key": 1}})
        msg = str(ei.value)
        assert f"[{section}]" in msg and "definitely_not_a_key" in msg
        # the error lists the valid keys so the operator can fix the typo
        some_valid = dataclasses.fields(_section_cls(section))[0].name
        assert some_valid in msg

    def test_unknown_device_key_rejected(self):
        with pytest.raises(ConfigError) as ei:
            config_from_dict({"device": [{"name": "rx0"}, {"name": "rx1", "uri": "usb:1.5.5"}]})
        assert "device[1]" in str(ei.value) and "uri" in str(ei.value)

    def test_unknown_key_in_toml_file_rejected(self, tmp_path):
        p = tmp_path / "typo.toml"
        p.write_text('[rx]\ncenter_freq_hz = 935e6\ngain_bd = 40.0\n', encoding="utf-8")
        with pytest.raises(ConfigError, match="gain_bd"):
            load_config(p)

    def test_missing_file_is_config_error(self, tmp_path):
        with pytest.raises(ConfigError, match="yok"):
            load_config(tmp_path / "nope.toml")
        with pytest.raises(ConfigError):
            load_config(tmp_path)          # a directory is not a config file

    def test_invalid_toml_is_config_error(self, tmp_path):
        p = tmp_path / "broken.toml"
        p.write_text("[rx\ngain_db = 1\n", encoding="utf-8")
        with pytest.raises(ConfigError):
            load_config(p)

    def test_single_device_table_accepted(self):
        app = config_from_dict({"device": {"name": "solo", "backend": "sim"}})
        assert [d.name for d in app.devices] == ["solo"]

    def test_duplicate_device_names_rejected(self):
        with pytest.raises(ConfigError, match="benzersiz"):
            config_from_dict({"device": [{"name": "rx0"}, {"name": "rx0"}]})

    def test_backend_validated(self):
        with pytest.raises(ConfigError, match="backend"):
            config_from_dict({"device": {"name": "rx0", "backend": "uhd"}})

    def test_empty_device_list_rejected(self):
        with pytest.raises(ConfigError):
            config_from_dict({"device": []})

    def test_device_lookup(self):
        app = config_from_dict({"device": [{"name": "rx0"}, {"name": "rx1"}]})
        assert app.device().name == "rx0"
        assert app.device("rx1").name == "rx1"
        with pytest.raises(ConfigError, match="rx9"):
            app.device("rx9")

    def test_to_dict_is_json_serializable(self):
        app = load_config()
        d = app.to_dict()
        assert set(SECTIONS) <= set(d) and "devices" in d
        json.dumps(d)                       # NaN allowed by json (ppm_override)


class TestConfigTypes:
    def test_int_promoted_to_float(self):
        app = config_from_dict({"rx": {"center_freq_hz": 935_000_000, "gain_db": 50}})
        assert isinstance(app.rx.center_freq_hz, float) and app.rx.center_freq_hz == 935e6
        assert isinstance(app.rx.gain_db, float)

    @pytest.mark.parametrize("bad", ["50", True, None, [50.0]])
    def test_float_field_rejects_non_numbers(self, bad):
        with pytest.raises(ConfigError, match="rx.gain_db"):
            config_from_dict({"rx": {"gain_db": bad}})

    def test_int_field(self):
        assert config_from_dict({"rx": {"buffer_size": 32768.0}}).rx.buffer_size == 32768
        assert isinstance(config_from_dict({"rx": {"buffer_size": 32768.0}}).rx.buffer_size, int)
        with pytest.raises(ConfigError, match="tamsayı"):
            config_from_dict({"rx": {"buffer_size": 1.5}})
        with pytest.raises(ConfigError):
            config_from_dict({"dsp": {"averages": True}})

    def test_bool_field_rejects_ints(self):
        with pytest.raises(ConfigError, match="true/false"):
            config_from_dict({"rx": {"dc_tracking": 1}})

    def test_list_and_table_fields(self):
        with pytest.raises(ConfigError, match="liste"):
            config_from_dict({"device": {"name": "rx0", "channels": 0}})
        with pytest.raises(ConfigError, match="tablo"):
            config_from_dict({"measure": {"params": {"df_rms": 0.15}}})
        with pytest.raises(ConfigError, match="tablo"):
            config_from_dict({"rx": 5})

    def test_soapy_args_are_stringified(self):
        app = config_from_dict({"device": {"name": "rx0", "backend": "soapy",
                                           "args": {"driver": "plutosdr", "usb_direct": 1}}})
        assert app.devices[0].args == {"driver": "plutosdr", "usb_direct": "1"}

    def test_nested_free_form_tables_preserved(self):
        app = config_from_dict({
            "measure": {"source_args": {"args": {"driver": "plutosdr"}, "fs": 1e6},
                        "params": {"df_rms": {"baseline_m": 0.15}}},
            "sim": {"emitters": [{"name": "a", "freq_hz": 1e9, "power_dbm": -50.0, "params": {"x": 1}}]},
        })
        assert app.measure.source_args["args"] == {"driver": "plutosdr"}
        assert app.measure.params["df_rms"]["baseline_m"] == 0.15
        assert app.sim.emitters[0]["params"] == {"x": 1}


ENUMS = {
    "rx.gain_mode": ("manual", "soft_agc", "hw_slow", "hw_fast", "hw_hybrid"),
    "dsp.window": ("rect", "hann", "hamming", "blackman", "blackmanharris", "flattop"),
    "sweep.window": ("rect", "hann", "hamming", "blackman", "blackmanharris", "flattop"),
    "dsp.avg_mode": ("mean", "max", "min", "median"),
    "detect.method": ("os_cfar", "ca_cfar", "fixed"),
    "sweep.gain_mode": ("fixed", "auto"),
    "bus.mode": ("direct", "proxy"),
    "cal.ppm_mode": ("auto", "hardware", "software", "off"),
    "measure.source": ("sim", "pluto_tx", "scpi", "manual", "none"),
    "rx.stream_format": ("CS16", "CF32"),
}


def _dotted(key: str, value) -> dict:
    sec, k = key.split(".")
    return {sec: {k: value}}


class TestConfigEnums:
    @pytest.mark.parametrize("key", sorted(ENUMS))
    def test_valid_values_accepted(self, key):
        sec, k = key.split(".")
        for v in ENUMS[key]:
            assert getattr(getattr(config_from_dict(_dotted(key, v)), sec), k) == v

    @pytest.mark.parametrize("key", sorted(ENUMS))
    def test_invalid_value_rejected_with_options(self, key):
        with pytest.raises(ConfigError) as ei:
            config_from_dict(_dotted(key, "bogus_value"))
        msg = str(ei.value)
        assert key in msg and "bogus_value" in msg and ENUMS[key][0] in msg

    def test_enum_is_case_sensitive(self):
        with pytest.raises(ConfigError):
            config_from_dict({"rx": {"stream_format": "cs16"}})

    @pytest.mark.parametrize("value, ok", [(0.0, True), (0.5, True), (0.99, True), (1.0, False), (-0.1, False)])
    def test_overlap_range(self, value, ok):
        if ok:
            assert config_from_dict({"dsp": {"overlap": value}}).dsp.overlap == value
        else:
            with pytest.raises(ConfigError, match="overlap"):
                config_from_dict({"dsp": {"overlap": value}})

    @pytest.mark.parametrize("section", ["dsp", "sweep"])
    @pytest.mark.parametrize("value, ok", [(0.1, True), (0.8, True), (1.0, True), (0.05, False), (1.01, False)])
    def test_usable_fraction_range(self, section, value, ok):
        raw = {section: {"usable_fraction": value}}
        if ok:
            assert getattr(config_from_dict(raw), section).usable_fraction == value
        else:
            with pytest.raises(ConfigError, match="usable_fraction"):
                config_from_dict(raw)


class TestOverrides:
    TOML = """
[[device]]
name = "rx0"
backend = "sim"
sim = { ppm = 2.0, realtime = false }

[[device]]
name = "rx1"
backend = "sim"
sim = { realtime = false }

[rx]
center_freq_hz = 935e6
gain_db = 30.0

[measure.params.df_rms]
baseline_m = 0.15
"""

    @pytest.fixture
    def cfg_file(self, tmp_path):
        p = tmp_path / "base.toml"
        p.write_text(self.TOML, encoding="utf-8")
        return p

    def test_precedence_defaults_file_overrides(self, cfg_file):
        app = load_config(cfg_file, overrides={
            "rx.gain_db": 45.0,
            "device.sim.ppm": 3.5,
            "device.args.uri": "usb:1.2.3",
            "measure.params.df_rms.baseline_m": 0.3,
            "dsp.nfft": 8192,
        })
        assert app.rx.gain_db == 45.0                      # override beats file
        assert app.rx.center_freq_hz == 935e6              # file beats default
        assert app.rx.sample_rate_hz == 4e6                # default where nobody set it
        assert app.dsp.nfft == 8192                        # override creates the missing section
        assert app.devices[0].sim == {"ppm": 3.5, "realtime": False}   # merged, not replaced
        assert app.devices[0].args == {"uri": "usb:1.2.3"}
        assert app.devices[1].sim == {"realtime": False}   # device.* targets device[0] only
        assert app.measure.params["df_rms"]["baseline_m"] == 0.3

    def test_without_overrides_file_values_apply(self, cfg_file):
        app = load_config(cfg_file)
        assert app.rx.gain_db == 30.0 and app.devices[0].sim["ppm"] == 2.0
        assert app.source_path == str(cfg_file)

    def test_device_override_without_device_section(self):
        app = load_config(None, overrides={"device.name": "solo", "device.backend": "sim"})
        assert [d.name for d in app.devices] == ["solo"]

    def test_override_with_unknown_key_rejected(self, cfg_file):
        with pytest.raises(ConfigError, match="gian_db"):
            load_config(cfg_file, overrides={"rx.gian_db": 3.0})
        with pytest.raises(ConfigError, match="bogus"):
            load_config(cfg_file, overrides={"device.bogus": 1})

    def test_override_values_are_type_checked(self, cfg_file):
        with pytest.raises(ConfigError, match="rx.gain_db"):
            load_config(cfg_file, overrides={"rx.gain_db": "high"})
        with pytest.raises(ConfigError, match="rx.gain_mode"):
            load_config(cfg_file, overrides={"rx.gain_mode": "auto"})

    def test_apply_overrides_none_is_identity(self):
        raw = {"rx": {"gain_db": 1.0}}
        assert apply_overrides(raw, None) is raw
        assert apply_overrides(raw, {}) is raw

    def test_apply_overrides_does_not_mutate_top_level_sections(self):
        raw = {"rx": {"gain_db": 1.0}}
        before = copy.deepcopy(raw)
        out = apply_overrides(raw, {"rx.gain_db": 9.0})
        assert out["rx"]["gain_db"] == 9.0 and raw == before

    def test_apply_overrides_does_not_mutate_nested_input(self):
        raw = {"measure": {"params": {"df_rms": {"baseline_m": 0.15}}},
               "device": [{"name": "rx0", "sim": {"ppm": 1.0}}]}
        before = copy.deepcopy(raw)
        apply_overrides(raw, {"measure.params.df_rms.baseline_m": 0.3, "device.sim.ppm": 5.0})
        assert raw == before


# =============================================================================== profiles
class TestProfileGain:
    @pytest.mark.parametrize("f_hz, lo, hi", [
        (500e6, -1.0, 73.0),            # AD9361 full gain table, < 1.3 GHz
        (935e6, -1.0, 73.0),
        (1299.999e6, -1.0, 73.0),
        (1300e6, -3.0, 71.0),           # bands are half-open [f_min, f_max)
        (2.4e9, -3.0, 71.0),
        (3.8e9, -3.0, 71.0),
        (5.0e9, -10.0, 62.0),
        (50e6, -1.0, 73.0),             # below every band -> closest band
        (7e9, -10.0, 62.0),             # above every band -> closest band
    ])
    def test_band_dependent_gain_range(self, f_hz, lo, hi):
        gr = PLUTO.gain_range(f_hz)
        assert (gr.min_db, gr.max_db, gr.step_db) == (lo, hi, 1.0)

    def test_gain_range_default_band_and_no_bands(self):
        assert PLUTO.gain_range(None).max_db == 73.0
        bare = DeviceProfile(name="x", chip="y")
        assert (bare.gain_range(1e9).min_db, bare.gain_range(1e9).max_db) == (0.0, 0.0)

    def test_gain_validation_follows_band(self):
        assert PLUTO.check_rx(center_freq_hz=500e6, gain_db=72.0) == ([], [])
        err, _ = PLUTO.check_rx(center_freq_hz=2.4e9, gain_db=72.0)
        assert len(err) == 1 and "72" in err[0] and "71" in err[0]
        err, _ = PLUTO.check_rx(center_freq_hz=2.4e9, gain_db=-3.0)
        assert err == []
        err, _ = PLUTO.check_rx(center_freq_hz=500e6, gain_db=-2.0)
        assert len(err) == 1


class TestProfileValidation:
    def test_limits_are_inclusive(self):
        assert PLUTO.check_rx(center_freq_hz=325e6)[0] == []
        assert PLUTO.check_rx(center_freq_hz=3.8e9)[0] == []
        assert len(PLUTO.check_rx(center_freq_hz=324.999e6)[0]) == 1
        assert len(PLUTO.check_rx(center_freq_hz=3.800001e9)[0]) == 1
        assert PLUTO.check_rx(bandwidth_hz=20e6)[0] == []
        assert PLUTO.check_rx(bandwidth_hz=200e3)[0] == []
        assert len(PLUTO.check_rx(bandwidth_hz=20.001e6)[0]) == 1
        assert len(PLUTO.check_rx(bandwidth_hz=199e3)[0]) == 1
        assert PLUTO.check_rx(sample_rate_hz=61.44e6)[0] == []
        assert len(PLUTO.check_rx(sample_rate_hz=61.45e6)[0]) == 1

    def test_profile_violation_lists_all_violations(self):
        kw = dict(center_freq_hz=6e9, sample_rate_hz=100e6, bandwidth_hz=30e6, gain_db=80.0, n_channels=2)
        err, warn = PLUTO.check_rx(**kw)
        assert len(err) == 5
        joined = "\n".join(err)
        for needle in ("Merkez frekans", "Analog kanal BW", "Örnekleme hızı", "Kazanç", "RX kanalı"):
            assert needle in joined
        assert warn, "the USB2 overload must be reported as a warning even when errors exist"
        with pytest.raises(ProfileViolation) as ei:
            PLUTO.validate_rx(**kw)
        e = ei.value
        assert isinstance(e, ConfigError) and isinstance(e, EDSError)
        assert e.violations == err
        text = str(e)
        assert text.startswith("Cihaz profili ihlali:")
        assert all(f"  - {v}" in text for v in err)

    def test_validate_rx_returns_warnings_when_valid(self):
        warns = PLUTO.validate_rx(center_freq_hz=935e6, sample_rate_hz=20e6, bandwidth_hz=20e6, gain_db=50.0)
        assert len(warns) == 1 and "teorik" in warns[0]

    def test_profile_violation_copies_list(self):
        src = ["a", "b"]
        e = ProfileViolation(src)
        src.append("c")
        assert e.violations == ["a", "b"]
        assert str(e) == "Cihaz profili ihlali:\n  - a\n  - b"


class TestUsb2Warnings:
    """AD9363 profile: 4 B/sample, practical 24 MB/s, theoretical 53.248 MB/s (USB2 HS bulk)."""

    def test_wire_rate(self):
        assert PLUTO.wire_rate_bytes_per_s(4e6) == pytest.approx(16e6)
        assert PLUTO.wire_rate_bytes_per_s(4e6, n_channels=2) == pytest.approx(32e6)
        assert load_profile("hackrf_one").wire_rate_bytes_per_s(10e6) == pytest.approx(20e6)   # 8-bit IQ

    @pytest.mark.parametrize("fs, kind", [
        (4e6, None),                   # 16 MB/s
        (6e6, None),                   # 24 MB/s == practical threshold (strictly greater warns)
        (6.5e6, "pratik"),             # 26 MB/s
        (13.312e6, "pratik"),          # 53.248 MB/s == theoretical (strictly greater warns)
        (13.4e6, "teorik"),
        (20e6, "teorik"),              # 80 MB/s: sweep bursts only
        (61.44e6, "teorik"),
    ])
    def test_thresholds(self, fs, kind):
        err, warn = PLUTO.check_rx(sample_rate_hz=fs)
        assert err == []
        if kind is None:
            assert warn == []
        else:
            assert len(warn) == 1 and kind in warn[0]
            mbps = fs * 4 / 1e6
            assert f"{mbps:.1f} MB/s" in warn[0]
            if kind == "pratik":
                assert "max_rate" in warn[0]

    def test_warning_scales_with_channels(self):
        two = dataclasses.replace(PLUTO, rx_channels=2)
        assert two.check_rx(sample_rate_hz=4e6, n_channels=1) == ([], [])
        err, warn = two.check_rx(sample_rate_hz=4e6, n_channels=2)    # 32 MB/s > 24 MB/s
        assert err == [] and len(warn) == 1 and "32.0 MB/s" in warn[0] and "pratik" in warn[0]
        err, warn = two.check_rx(sample_rate_hz=8e6, n_channels=2)    # 64 MB/s > 53.248 MB/s
        assert err == [] and len(warn) == 1 and "teorik" in warn[0] and "2 kanal" in warn[0]

    def test_bw_wider_than_fs_warns_aliasing(self):
        err, warn = PLUTO.check_rx(sample_rate_hz=4e6, bandwidth_hz=10e6)
        assert err == [] and len(warn) == 1 and "aliasing" in warn[0]
        assert PLUTO.check_rx(sample_rate_hz=4e6, bandwidth_hz=4e6) == ([], [])


class TestProfileLoading:
    def test_list_and_load_by_name_and_path(self):
        names = list_profiles()
        for n in ("ad9363_pluto", "ad9364_pluto_ext", "krakensdr", "hackrf_one", "bladerf2_micro", "usrp_b210"):
            assert n in names
        by_path = load_profile(paths.profiles_dir() / "ad9363_pluto.toml")
        assert by_path == PLUTO

    def test_unknown_profile_lists_available(self):
        with pytest.raises(ConfigError) as ei:
            load_profile("ad9999_nope")
        assert "ad9363_pluto" in str(ei.value)

    def test_profile_from_dict_strictness(self):
        with pytest.raises(ConfigError, match="bilinmeyen"):
            profile_from_dict({"name": "x", "chip": "y", "freq_mn_hz": 1.0})
        with pytest.raises(ConfigError, match="name"):
            profile_from_dict({"chip": "y"})

    def test_profile_from_dict_coerces_and_defaults(self):
        p = profile_from_dict({"name": "x", "chip": "y", "freq_min_hz": 325_000_000, "ref_clock_ppm": 25,
                               "gain_bands": [{"f_min_hz": 0, "f_max_hz": 1e9, "gain_min_db": 0, "gain_max_db": 10}],
                               "candidate_sample_rates_hz": [1_000_000, 2_000_000], "notes": ["n"]})
        assert isinstance(p.freq_min_hz, float) and isinstance(p.ref_clock_ppm, float)
        assert p.gain_bands == (GainBand(0, 1e9, 0, 10, 1.0),)
        assert p.candidate_sample_rates_hz == (1e6, 2e6) and p.notes == ("n",)

    def test_profile_from_dict_bad_band_is_config_error(self):
        with pytest.raises(ConfigError):
            profile_from_dict({"name": "x", "chip": "y",
                               "gain_bands": [{"f_min_hz": 0, "f_max_hz": 1, "gain_min_db": 0, "gain_max_db": 1,
                                               "step": 1}]})

    def test_profiles_dir_honours_env(self, tmp_path, monkeypatch):
        (tmp_path / "profiles").mkdir()
        (tmp_path / "profiles" / "custom_rx.toml").write_text(
            'name = "custom_rx"\nchip = "TEST"\nfreq_min_hz = 1e9\nfreq_max_hz = 2e9\n', encoding="utf-8")
        monkeypatch.setenv("EDS_CONFIG_DIR", str(tmp_path))
        assert paths.config_dir() == tmp_path.resolve()
        assert paths.profiles_dir() == tmp_path.resolve() / "profiles"
        p = load_profile("custom_rx")
        assert (p.chip, p.freq_min_hz, p.freq_max_hz) == ("TEST", 1e9, 2e9)


# =============================================================================== GainRange
class TestGainRangeClamp:
    @pytest.mark.parametrize("g, expected", [
        (10.4, 10.0), (10.6, 11.0), (-50.0, -3.0), (100.0, 71.0), (71.0, 71.0), (-3.0, -3.0), (0.49, 0.0),
    ])
    def test_unit_step(self, g, expected):
        r = GainRange(-3.0, 71.0, 1.0).clamp(g)
        assert r == expected and isinstance(r, float)

    @pytest.mark.parametrize("g, expected", [(0.2, 1.0), (-0.2, -1.0), (2.9, 3.0), (100.0, 71.0)])
    def test_grid_is_anchored_at_min(self, g, expected):
        # grid {-3, -1, 1, 3, ..., 71}: quantization is relative to min_db, not to 0 dB
        assert GainRange(-3.0, 71.0, 2.0).clamp(g) == expected

    def test_fine_step(self):
        kraken = GainRange(0.0, 49.6, 0.1)
        assert kraken.clamp(25.04) == pytest.approx(25.0, abs=1e-9)
        assert kraken.clamp(33.37) == pytest.approx(33.4, abs=1e-9)
        assert kraken.clamp(60.0) == pytest.approx(49.6, abs=1e-12)

    def test_zero_step_means_continuous(self):
        assert GainRange(0.0, 10.0, 0.0).clamp(3.3) == 3.3
        assert GainRange(0.0, 10.0, 0.0).clamp(11.0) == 10.0

    @pytest.mark.parametrize("lo, hi, step", [(-3.0, 71.0, 1.0), (-1.0, 73.0, 1.0), (0.0, 102.0, 2.0),
                                              (0.0, 49.6, 0.1), (-10.0, 62.0, 0.5)])
    def test_properties_on_grid_in_range_nearest(self, lo, hi, step):
        rng = np.random.default_rng(20261105)
        gr = GainRange(lo, hi, step)
        for g in rng.uniform(lo - 20.0, hi + 20.0, 400):
            r = gr.clamp(float(g))
            assert lo <= r <= hi
            k = (r - lo) / step
            assert abs(k - round(k)) < 1e-6, "result must lie on the step grid"
            assert abs(r - min(max(g, lo), hi)) <= step / 2 + 1e-9, "must be the nearest grid point"

    def test_off_grid_max_is_floored_to_grid(self):
        assert GainRange(0.0, 5.5, 2.0).clamp(5.5) == 4.0


# =============================================================================== units
class TestUnits:
    def test_db10_lin10_inverse(self):
        x = np.logspace(-25, 5, 61)
        np.testing.assert_allclose(lin10(db10(x)), x, rtol=1e-12)
        d = np.linspace(-250.0, 50.0, 31)
        np.testing.assert_allclose(db10(lin10(d)), d, atol=1e-10)

    def test_db_known_values(self):
        assert float(db10(1.0)) == 0.0
        assert float(db10(10.0)) == pytest.approx(10.0)
        assert float(db10(2.0)) == pytest.approx(3.0103, abs=1e-4)
        assert float(db20(10.0)) == pytest.approx(20.0)
        assert float(db20(-10.0)) == pytest.approx(20.0)        # amplitude: magnitude only
        np.testing.assert_allclose(lin20(db20(np.array([0.5, 2.0, 1e-3]))), [0.5, 2.0, 1e-3], rtol=1e-12)

    def test_db_never_minus_inf(self):
        v = db10(np.array([0.0, -1.0]))
        assert np.all(np.isfinite(v)) and np.all(v == pytest.approx(-300.0))
        assert np.isfinite(float(db20(0.0)))

    def test_full_scale_complex_exponential_is_0_dbfs(self):
        n = np.arange(4096)
        x = np.exp(2j * np.pi * 0.0625 * n)                     # |x| = 1.0 = full scale
        assert float(db10(np.mean(np.abs(x) ** 2))) == pytest.approx(0.0, abs=1e-12)
        x20 = 10 ** (-20 / 20) * x
        assert float(db10(np.mean(np.abs(x20) ** 2))) == pytest.approx(-20.0, abs=1e-9)

    def test_power_units(self):
        assert float(dbm_to_watt(30.0)) == pytest.approx(1.0)
        assert float(dbm_to_watt(0.0)) == pytest.approx(1e-3)
        assert float(watt_to_dbm(1e-3)) == pytest.approx(0.0, abs=1e-12)
        p = np.array([-120.0, -30.0, 0.0, 20.0])
        np.testing.assert_allclose(watt_to_dbm(dbm_to_watt(p)), p, atol=1e-10)

    def test_physical_constants(self):
        k_b = 1.380649e-23                                       # J/K (SI exact)
        assert KT_DBM_HZ == pytest.approx(10 * math.log10(k_b * 290.0 * 1e3), abs=1e-3)
        assert C_LIGHT == 299_792_458.0
        assert wavelength(1e9) == pytest.approx(0.299792458)
        assert wavelength(2.4e9) / 2 == pytest.approx(0.0624568, abs=1e-6)

    @pytest.mark.parametrize("a, w", [
        (-180.0, 180.0), (180.0, 180.0), (540.0, 180.0), (-540.0, 180.0), (360.0, 0.0), (0.0, 0.0),
        (-0.0, 0.0), (720.5, 0.5), (-190.0, 170.0), (190.0, -170.0), (179.999, 179.999), (-179.999, -179.999),
    ])
    def test_wrap_deg_edges(self, a, w):
        assert float(wrap_deg(a)) == pytest.approx(w, abs=1e-9)

    @pytest.mark.parametrize("a, w", [
        (-math.pi, math.pi), (math.pi, math.pi), (3 * math.pi, math.pi), (-3 * math.pi, math.pi),
        (2 * math.pi, 0.0), (0.0, 0.0), (math.pi + 0.1, -math.pi + 0.1), (-math.pi - 0.1, math.pi - 0.1),
    ])
    def test_wrap_rad_edges(self, a, w):
        assert float(wrap_rad(a)) == pytest.approx(w, abs=1e-12)

    def test_wrap_range_and_equivalence(self):
        rng = np.random.default_rng(7)
        a = rng.uniform(-1e4, 1e4, 5000)
        wd = wrap_deg(a)
        assert wd.shape == a.shape
        assert np.all(wd > -180.0) and np.all(wd <= 180.0)
        np.testing.assert_allclose(np.cos(np.radians(wd)), np.cos(np.radians(a)), atol=1e-9)
        np.testing.assert_allclose(np.sin(np.radians(wd)), np.sin(np.radians(a)), atol=1e-9)
        r = rng.uniform(-100.0, 100.0, 5000)
        wr = wrap_rad(r)
        assert np.all(wr > -np.pi) and np.all(wr <= np.pi)
        np.testing.assert_allclose(np.exp(1j * wr), np.exp(1j * r), atol=1e-12)

    def test_wrap_rms_error_example(self):
        # DF RMS must use wrapped differences: 179° vs -179° is a 2° error, not 358°
        est, true = np.array([179.0, -179.0, 10.0]), np.array([-179.0, 179.0, 12.0])
        rms = float(np.sqrt(np.mean(wrap_deg(est - true) ** 2)))
        assert rms == pytest.approx(2.0)

    @pytest.mark.parametrize("hz, kw, text", [
        (915e6, {}, "915 MHz"), (2.4e9, {}, "2.4 GHz"), (433.92e6, {}, "433.92 MHz"),
        (446.00625e6, {}, "446.006 MHz"), (446.00625e6, {"digits": 9}, "446.00625 MHz"),
        (1e3, {}, "1 kHz"), (1.957e3, {}, "1.957 kHz"), (999.0, {}, "999 Hz"), (0.0, {}, "0 Hz"),
        (-5e6, {}, "-5 MHz"), (3.8e9, {}, "3.8 GHz"), (325e6, {}, "325 MHz"),
    ])
    def test_fmt_hz(self, hz, kw, text):
        assert fmt_hz(hz, **kw) == text


# =============================================================================== types
class TestIQBlock:
    def _block(self, n=1000, ch=2, fs=1e6, t_ns=1_000_000_000):
        rng = np.random.default_rng(3)
        s = (rng.standard_normal((ch, n)) + 1j * rng.standard_normal((ch, n))).astype(np.complex64)
        return IQBlock(samples=s, fs=fs, fc=935e6, t_ns=t_ns, index=0, seq=0, gain_db=(50.0,) * ch)

    def test_properties(self):
        b = self._block()
        assert (b.n, b.n_channels) == (1000, 2)
        assert b.duration_s == pytest.approx(1e-3)
        assert b.t_end_ns == 1_000_000_000 + 1_000_000
        assert np.shares_memory(b.channel(1), b.samples) and np.array_equal(b.channel(1), b.samples[1])
        assert b.channel() is not None and np.array_equal(b.channel(), b.samples[0])

    def test_t_end_rounding(self):
        b = self._block(n=1, ch=1, fs=3.0, t_ns=0)
        assert b.t_end_ns == 333_333_333

    def test_defaults(self):
        a, b = self._block(), self._block()
        assert a.overflow is False and a.settling is False
        assert a.clip_level == pytest.approx(2047.0 / 2048.0)
        a.meta["x"] = 1
        assert b.meta == {}                                     # default_factory, not shared
        with pytest.raises(AttributeError):
            a.unexpected_attribute = 1                          # slots: no silent typos

    def test_clip_level_matches_12bit_full_scale(self):
        # counts/2048 normalization: the largest positive 12-bit code is exactly the clip level
        assert PLUTO.full_scale_counts == 2.0 ** (PLUTO.adc_bits - 1)
        assert (2047.0 / PLUTO.full_scale_counts) == IQBlock.__dataclass_fields__["clip_level"].default


class TestStreamStats:
    def test_throughput(self):
        st = StreamStats(samples=4_000_000, start_mono=10.0, last_mono=11.0)
        assert st.elapsed_s == pytest.approx(1.0)
        assert st.throughput_sps == pytest.approx(4e6)
        st2 = StreamStats(samples=3_000_000, start_mono=5.0, last_mono=5.75)
        assert st2.throughput_sps == pytest.approx(4e6)

    def test_degenerate_elapsed(self):
        assert StreamStats().throughput_sps == 0.0
        st = StreamStats(samples=10, start_mono=2.0, last_mono=1.0)
        assert st.elapsed_s == 0.0 and st.throughput_sps == 0.0

    def test_to_dict(self):
        st = StreamStats(blocks=3, samples=196_608, overflows=1, host_drops=2, start_mono=0.0, last_mono=0.5)
        d = st.to_dict()
        assert d["overflows"] == 1 and d["host_drops"] == 2
        assert d["elapsed_s"] == pytest.approx(0.5)
        assert d["throughput_sps"] == pytest.approx(393_216.0)
        json.dumps(d)


class TestDeviceInfo:
    def test_to_dict_and_independent_extra(self):
        a = DeviceInfo(name="rx0", backend="soapy", driver="plutosdr", serial="1044")
        b = DeviceInfo(name="rx1", backend="sim")
        a.extra["fw"] = "v0.38"
        assert b.extra == {}
        d = a.to_dict()
        assert d["serial"] == "1044" and d["extra"] == {"fw": "v0.38"} and d["profile"] == ""


# =============================================================================== errors
def test_error_hierarchy():
    for cls in (ConfigError, ProfileViolation, DeviceError, DeviceNotFound, BackendUnavailable, StreamError,
                StreamTimeout, NotSupported, CalibrationError, MeasurementError):
        assert issubclass(cls, EDSError)
    assert issubclass(ProfileViolation, ConfigError)
    assert issubclass(StreamTimeout, StreamError) and issubclass(StreamError, DeviceError)
    assert issubclass(DeviceNotFound, DeviceError) and issubclass(BackendUnavailable, DeviceError)
    assert not issubclass(NotSupported, DeviceError)


def test_config_module_raises_only_eds_errors_for_bad_input():
    bad_inputs = [{"rx": {"gain_db": "x"}}, {"nope": {}}, {"device": [{"name": 1}]}, {"device": ["x"]}]
    for raw in bad_inputs:
        with pytest.raises(EDSError):
            cfgmod.config_from_dict(raw)


# =============================================================================== timeutil
class TestTimeutil:
    def test_now_ns_is_wall_clock(self):
        t = timeutil.now_ns()
        assert isinstance(t, int) and abs(t - time.time_ns()) < 5_000_000_000

    def test_iso_utc_and_stamp(self):
        assert timeutil.iso_utc(0) == "1970-01-01T00:00:00Z"
        assert timeutil.iso_utc(1_700_000_000 * 10**9) == "2023-11-14T22:13:20Z"
        assert re.fullmatch(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z", timeutil.iso_utc())
        assert re.fullmatch(r"\d{8}-\d{6}", timeutil.stamp())

    def test_stopwatch_accumulates(self, monkeypatch):
        clock = iter([0.0, 0.25, 1.0, 1.5, 2.0, 2.125])
        monkeypatch.setattr(timeutil, "mono", lambda: next(clock))
        sw = timeutil.Stopwatch()
        with sw.time("tune"):
            pass
        with sw.time("tune"):
            pass
        with pytest.raises(RuntimeError), sw.time("dsp"):
            raise RuntimeError("boom")                          # time is recorded even on error
        assert sw.totals["tune"] == pytest.approx(0.75) and sw.counts["tune"] == 2
        assert sw.mean("tune") == pytest.approx(0.375)
        assert sw.totals["dsp"] == pytest.approx(0.125) and sw.counts["dsp"] == 1
        assert sw.mean("settle") == 0.0


# =============================================================================== paths
class TestPaths:
    def test_repo_root_layout(self):
        root = paths.repo_root()
        assert (root / "pyproject.toml").is_file() and (root / "contracts" / "topics.json").is_file()
        assert paths.profiles_dir() == root / "config" / "profiles"

    @pytest.mark.parametrize("env, fn", [("EDS_CONFIG_DIR", paths.config_dir), ("EDS_CONTRACTS_DIR", paths.contracts_dir),
                                         ("EDS_CAL_DIR", paths.cal_dir), ("EDS_REPORTS_DIR", paths.reports_dir)])
    def test_env_overrides(self, env, fn, tmp_path, monkeypatch):
        default = fn()
        assert default.parent == paths.repo_root()
        monkeypatch.setenv(env, str(tmp_path))
        assert fn() == tmp_path.resolve()
        monkeypatch.delenv(env)
        assert fn() == default


def test_device_config_defaults_are_independent():
    a, b = DeviceConfig(), DeviceConfig()
    a.args["uri"] = "usb:"
    a.channels.append(1)
    assert b.args == {} and b.channels == [0]
