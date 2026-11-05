"""SoapySDR backend (eds.hal.soapy_backend) against the python3-soapysdr / pylibiio fakes."""

from __future__ import annotations

import gc
import time

import numpy as np
import pytest

from eds.core.config import DeviceConfig, RxConfig
from eds.core.errors import (
    BackendUnavailable,
    ConfigError,
    DeviceNotFound,
    ProfileViolation,
    StreamError,
    StreamTimeout,
)
from eds.core.profile import load_profile
from eds.hal.factory import open_device
from eds.hal.soapy_backend import SoapyRxDevice, enumerate_soapy, kwargs_to_dict
from tests.hal.fakes import fake_iio, fake_soapy

PLUTO = load_profile("ad9363_pluto")
RX = fake_soapy.SOAPY_SDR_RX
IP = "ip:192.168.2.1"
P = "ad9361-phy"
FS = 2.4e6
TONE = 100e3


def rxcfg(**kw) -> RxConfig:
    base = dict(center_freq_hz=915e6, sample_rate_hz=FS, bandwidth_hz=2e6, gain_mode="manual",
                gain_db=40.0, buffer_size=1024)
    base.update(kw)
    return RxConfig(**base)


def make_dev(args=None, control_uri="none", profile=PLUTO, channels=None, **kw) -> SoapyRxDevice:
    cfg = DeviceConfig(name="rx0", backend="soapy", profile=profile.name,
                       args=dict(args if args is not None else {"driver": "plutosdr", "uri": "usb:1.5.5"}),
                       control_uri=control_uri, channels=list(channels or [0]))
    return SoapyRxDevice(cfg, profile, **kw)


@pytest.fixture
def no_iio(monkeypatch):
    fake_iio.uninstall(monkeypatch)


@pytest.fixture
def sdr(monkeypatch, no_iio):
    return fake_soapy.install(monkeypatch)


@pytest.fixture
def board(monkeypatch):
    b = fake_iio.make_pluto_board(IP)
    fake_iio.install(monkeypatch, b)
    return b


def power_dbfs(x: np.ndarray) -> float:
    return float(10 * np.log10(np.mean(np.abs(x) ** 2)))


def phase_steps(x: np.ndarray) -> np.ndarray:
    return np.angle(x[1:] * np.conj(x[:-1]))


# ----------------------------------------------------------------------------- module / open
def test_backend_unavailable_when_soapysdr_missing(monkeypatch, no_iio):
    fake_soapy.uninstall(monkeypatch)
    with pytest.raises(BackendUnavailable, match="python3-soapysdr") as ei:
        make_dev()
    assert "SoapyPlutoSDR" in str(ei.value) and "install_ubuntu.sh" in str(ei.value)
    with pytest.raises(BackendUnavailable):
        enumerate_soapy({})


def test_enumerate_returns_plain_dicts(sdr):
    found = enumerate_soapy({"driver": "plutosdr"})
    assert found == [fake_soapy.PLUTO_ENTRY]
    assert type(found[0]) is dict and all(isinstance(v, str) for v in found[0].values())
    assert enumerate_soapy({"driver": "uhd"}) == []
    assert kwargs_to_dict(fake_soapy.SoapySDRKwargs({"a": 1})) == {"a": "1"}


def test_open_not_found_lists_args_and_found_devices(sdr):
    with pytest.raises(DeviceNotFound) as ei:
        make_dev(args={"driver": "plutosdr", "uri": "usb:9.9.9"})
    msg = str(ei.value)
    assert "usb:9.9.9" in msg and "PlutoSDR #0 usb:1.5.5" in msg and "SoapySDRUtil" in msg


def test_open_fills_info_and_defaults_driver_from_profile(sdr):
    dev = make_dev(args={"uri": "usb:1.5.5", "usb_direct": "1", "stream:foo": "bar"})
    fk = dev._dev
    assert fk.args == {"uri": "usb:1.5.5", "usb_direct": "1", "driver": "plutosdr"}   # stream:* stripped
    assert dev.info.backend == "soapy" and dev.info.name == "rx0" and dev.info.profile == "ad9363_pluto"
    assert dev.info.driver == "PlutoSDR" and dev.info.hardware == "ADALM-PLUTO"
    assert dev.info.serial == fake_soapy.PLUTO_ENTRY["serial"]
    assert dev.info.firmware == "v0.38" and dev.info.uri == "usb:1.5.5"
    assert dev.info.extra["stream_format"] == "CS16"
    assert dev.is_pluto and dev.num_channels == 1 and dev.coherent
    with pytest.raises(ConfigError):
        make_dev(stream_format="CS8")


def test_factory_opens_soapy_backend(sdr):
    dev = open_device(DeviceConfig(name="rxF", backend="soapy", args={"driver": "plutosdr"}))
    assert isinstance(dev, SoapyRxDevice) and dev.name == "rxF"
    dev.close()


def test_runtime_limits_and_old_binding_fallbacks(monkeypatch, no_iio):
    fake_soapy.install(monkeypatch)
    lim = make_dev().runtime_limits()
    assert lim == {"freq_hz": (70e6, 6e9), "sample_rate_hz": (65105.0, 61.44e6),
                   "bandwidth_hz": (200e3, 56e6), "gain_db": (0.0, 73.0)}
    fake_soapy.install(monkeypatch, missing_methods=("getSampleRateRange", "getBandwidthRange"))
    lim = make_dev().runtime_limits()
    assert lim["sample_rate_hz"] == (1e6, 20e6) and lim["bandwidth_hz"] == (200e3, 56e6)
    fake_soapy.install(monkeypatch, missing_methods=("getSampleRateRange", "listSampleRates", "getGainRange"))
    dev = make_dev()
    lim = dev.runtime_limits()
    assert "sample_rate_hz" not in lim and "gain_db" not in lim
    assert dev.gain_range(2.4e9).max_db == 71.0          # profile alone


# ----------------------------------------------------------------------------- stream format
def test_cs16_scaling_tone_level_and_stream_setup(sdr):
    dev = make_dev()
    dev.configure(rxcfg())
    fk = dev._dev
    dev.start_stream()
    (args, _), = fk.called("setupStream")
    assert args == (RX, "CS16", [0], {"bufflen": "1024"})
    blk = dev.read(4096, timeout_s=0.5)
    x = blk.samples[0]
    assert blk.samples.dtype == np.complex64 and blk.samples.shape == (1, 4096)
    assert x[0] == np.complex64(0.5 + 0j)                 # count 1024 -> 0.5 exactly
    np.testing.assert_allclose(np.abs(x), 0.5, atol=1e-3)
    assert power_dbfs(x) == pytest.approx(-6.0206, abs=0.01)
    np.testing.assert_allclose(phase_steps(x), 2 * np.pi * TONE / FS, atol=2e-3)
    reads = fk.called("readStream")
    assert all(a[0] <= 1024 and k["timeoutUs"] == 500_000 for a, k in reads)
    assert len(reads) == 4                                # one readStream per buffer
    assert blk.fs == FS and blk.fc == 915e6 and blk.gain_db == (40.0,)


def test_minus_20_dbfs_tone_reads_minus_20(monkeypatch, no_iio):
    fake_soapy.install(monkeypatch, tone_counts=2048.0 * 10 ** (-20 / 20))
    dev = make_dev()
    dev.configure(rxcfg())
    blk = dev.capture(8192)
    assert power_dbfs(blk.samples[0]) == pytest.approx(-20.0, abs=0.1)


def test_cf32_stream(monkeypatch, no_iio):
    sdr = fake_soapy.install(monkeypatch)
    dev = make_dev(stream_format="CF32")
    dev.configure(rxcfg())
    dev.start_stream()
    assert sdr.state.last().called("setupStream")[0][0][1] == "CF32"
    x = dev.read(2048).samples[0]
    assert x[0] == np.complex64(0.5 + 0j)
    np.testing.assert_allclose(np.abs(x), 0.5, atol=1e-3)
    assert x.base is None or not np.shares_memory(x, dev._bufs[0])   # not a view of the reused buffer


def test_cf32_block_does_not_alias_driver_buffer(monkeypatch, no_iio):
    fake_soapy.install(monkeypatch, max_per_read=None)
    dev = make_dev(stream_format="CF32")
    dev.configure(rxcfg())
    dev.start_stream()
    b1 = dev.read(1024).samples.copy()
    b1_live = dev.read(1024)
    dev.read(1024)
    assert not np.array_equal(b1, b1_live.samples)        # sanity: data advances
    first = dev.read(1024)
    snapshot = first.samples.copy()
    dev.read(1024)
    np.testing.assert_array_equal(first.samples, snapshot)  # later reads must not overwrite it


def test_two_channel_layout_uses_native_full_scale(monkeypatch, no_iio):
    fake_soapy.install(
        monkeypatch, num_channels=2, driver_key="UHD", hardware_key="B210",
        devices=[{"driver": "uhd", "label": "B210 3141", "serial": "3141"}], hardware_info={"serial": "3141"},
        native_full_scale=32768.0, cs16_full_scale=32768.0, tone_counts=16384.0,
    )
    b210 = load_profile("usrp_b210")
    dev = make_dev(args={"driver": "uhd"}, profile=b210, channels=[0, 1], control_uri="ip:192.168.2.1")
    dev.configure(rxcfg(sample_rate_hz=4e6, bandwidth_hz=4e6))
    fk = dev._dev
    assert [a for a, _ in fk.called("setFrequency")] == [(RX, 0, 915e6), (RX, 1, 915e6)]
    blk = dev.capture(512)
    assert blk.samples.shape == (2, 512) and dev.info.serial == "3141"
    np.testing.assert_allclose(np.abs(blk.samples), 0.5, atol=1e-4)
    dphi = np.angle(blk.samples[1] * np.conj(blk.samples[0]))
    np.testing.assert_allclose(dphi, 0.5, atol=1e-3)       # channel 1 = channel 0 + 0.5 rad
    assert dev.thin_layer() is None                         # thin layer only for plutosdr


def test_non_cs16_native_warns_and_assumes_2048(monkeypatch, no_iio):
    fake_soapy.install(monkeypatch, native_format="CF32", native_full_scale=1.0)
    dev = make_dev()
    dev.configure(rxcfg())
    dev.start_stream()
    assert any("CF32" in w and "tam ölçe" in w for w in dev.warnings)
    assert dev.read(256).samples[0][0] == np.complex64(0.5)


def test_stream_args_passthrough_and_mtu(sdr):
    dev = make_dev(args={"driver": "plutosdr", "uri": "usb:1.5.5", "stream:bufflen": "4096", "stream:foo": "bar"})
    dev.configure(rxcfg())
    dev.start_stream()
    fk = dev._dev
    assert fk.called("setupStream")[0][0][3] == {"bufflen": "4096", "foo": "bar"}
    assert dev.stream_mtu == 4096 and dev._cap == 4096
    dev.read(4096)
    assert fk.called("readStream")[-1][0][0] == 4096


# ----------------------------------------------------------------------------- read semantics
def test_overflow_mid_read_restarts_block(monkeypatch, no_iio):
    sdr = fake_soapy.install(monkeypatch, max_per_read=256, overflow_drop=1000)
    dev = make_dev()
    dev.configure(rxcfg())
    dev.start_stream()
    fk = sdr.state.last()
    fk.rx_script.extend([("short", 256), ("short", 256), "overflow"])
    blk = dev.read(1024)
    assert blk.overflow and blk.samples.shape == (1, 1024)
    assert dev.stats.overflows == 1 and dev.stats.discarded_samples == 512
    x = blk.samples[0]
    np.testing.assert_allclose(phase_steps(x), 2 * np.pi * TONE / FS, atol=2e-3)   # gap-free
    # first sample follows the gap: 512 delivered + 1000 lost
    expected0 = 0.5 * np.exp(1j * 2 * np.pi * TONE * 1512 / FS)
    assert abs(x[0] - expected0) < 1e-3
    assert blk.index == 512 and dev._index == 512 + 1024
    nxt = dev.read(256)
    assert not nxt.overflow


def test_timeout_raises_stream_timeout(sdr):
    dev = make_dev()
    dev.configure(rxcfg())
    dev.start_stream()
    fk = dev._dev
    fk.rx_script.append("timeout")
    with pytest.raises(StreamTimeout):
        dev.read(512, timeout_s=0.25)
    assert dev.stats.timeouts == 1
    assert fk.called("readStream")[-1][1]["timeoutUs"] == 250_000
    assert dev.read(512).samples.shape == (1, 512)           # stream keeps working


@pytest.mark.parametrize("code,name", [(-2, "STREAM_ERROR"), (-3, "CORRUPTION"), (-5, "NOT_SUPPORTED"),
                                       (-6, "TIME_ERROR")])
def test_other_error_codes_raise_stream_error(sdr, code, name):
    dev = make_dev()
    dev.configure(rxcfg())
    dev.start_stream()
    dev._dev.rx_script.append(code)
    with pytest.raises(StreamError, match=name) as ei:
        dev.read(512)
    assert not isinstance(ei.value, StreamTimeout)


@pytest.mark.realtime
def test_zero_returns_end_in_timeout(sdr):
    dev = make_dev()
    dev.configure(rxcfg())
    dev.start_stream()
    dev._dev.rx_zero_mode = True
    t0 = time.perf_counter()
    with pytest.raises(StreamTimeout):
        dev.read(512, timeout_s=0.05)
    assert time.perf_counter() - t0 < 1.0


def test_read_requires_stream(sdr):
    dev = make_dev()
    with pytest.raises(StreamError):
        dev.read(16)


def test_activate_failure_closes_stream(monkeypatch, no_iio):
    sdr = fake_soapy.install(monkeypatch, activate_ret=-2)
    dev = make_dev()
    dev.configure(rxcfg())
    with pytest.raises(StreamError, match="STREAM_ERROR"):
        dev.start_stream()
    assert not dev.streaming and sdr.state.last().called("closeStream")


def test_setup_stream_failure(monkeypatch, no_iio):
    fake_soapy.install(monkeypatch, stream_formats=("CS8", "CF32"))
    dev = make_dev()
    dev.configure(rxcfg())
    with pytest.raises(StreamError, match="setupStream"):
        dev.start_stream()


def test_hardware_timestamp_gap_flags_overflow(monkeypatch, no_iio):
    sdr = fake_soapy.install(monkeypatch, has_time=True)
    dev = make_dev()
    dev.configure(rxcfg())
    dev.start_stream()
    fk = sdr.state.last()
    b0 = dev.read(1024)
    assert not b0.overflow and b0.meta["hw_time_ns"] == 1_000_000_000
    b1 = dev.read(1024)
    assert not b1.overflow
    assert b1.meta["hw_time_ns"] == 1_000_000_000 + round(1024 * 1e9 / FS)
    fk.rx_script.append(("gap", 500))                        # samples silently lost
    b2 = dev.read(1024)
    assert b2.overflow and dev.ts_gap_events == 1 and dev.stats.overflows == 1
    assert not dev.read(1024).overflow


def test_timestamp_gap_detection_can_be_disabled(monkeypatch, no_iio):
    sdr = fake_soapy.install(monkeypatch, has_time=True)
    dev = make_dev(detect_ts_gaps=False)
    dev.configure(rxcfg())
    dev.start_stream()
    dev.read(1024)
    sdr.state.last().rx_script.append(("gap", 500))
    assert not dev.read(1024).overflow


def test_retune_marks_settling_and_capture_discards_stale(sdr):
    dev = make_dev()
    dev.configure(rxcfg())                                   # buffer 1024 → settle 5×1024
    first = dev.capture(2048)
    assert not first.settling
    np.testing.assert_allclose(np.abs(first.samples[0]), 0.5, atol=1e-3)
    dev.set_center_freq(2.4e9)
    stale = dev.read(1024)
    assert stale.settling and stale.fc == 2.4e9
    np.testing.assert_allclose(np.abs(stale.samples[0]), 300 / 2048, atol=1e-3)   # old-frequency data
    blk = dev.capture(2048)
    assert not blk.settling
    np.testing.assert_allclose(np.abs(blk.samples[0]), 0.5, atol=1e-3)           # all fresh
    assert dev.stats.settling_blocks >= 5


# ----------------------------------------------------------------------------- profile / gain
def test_profile_violation_before_any_soapy_call(sdr):
    dev = make_dev()
    fk = dev._dev
    fk.calls.clear()
    with pytest.raises(ProfileViolation) as ei:
        dev.set_center_freq(4.0e9)
    assert "AD9363" in str(ei.value)
    with pytest.raises(ProfileViolation) as ei:
        dev.configure(rxcfg(center_freq_hz=100e6, bandwidth_hz=30e6, sample_rate_hz=80e6))
    assert len(ei.value.violations) == 3
    with pytest.raises(ProfileViolation):
        dev.set_sample_rate(70e6)
    with pytest.raises(ProfileViolation):
        dev.set_bandwidth(25e6)
    assert [n for n, _, _ in fk.calls if n.startswith("set")] == []


def test_band_dependent_gain_clamp(sdr):
    dev = make_dev()
    dev.configure(rxcfg(center_freq_hz=915e6, gain_db=73.0))
    fk = dev._dev
    assert dev.gains == [73.0]
    dev.set_center_freq(2.4e9)                               # 1.3–4 GHz band: max 71 dB
    assert dev.gains == [71.0]
    assert fk.called("setGain")[-1][0] == (RX, 0, 71.0)
    assert dev.set_gain(73.0) == 71.0
    assert dev.set_gain(-3.0) == 0.0                         # profile −3 ∩ runtime 0..73
    assert fk.called("setGain")[-1][0] == (RX, 0, 0.0)


def test_gain_modes_without_thin_layer(sdr):
    dev = make_dev()
    fk = dev._dev
    dev.set_gain_mode("hw_fast")
    assert fk.called("setGainMode")[-1][0] == (RX, 0, True)
    assert dev.agc_mode_actual == "slow_attack"
    assert any("slow_attack" in w and "control_uri" in w for w in dev.warnings)
    dev.set_gain_mode("manual")
    assert fk.called("setGainMode")[-1][0] == (RX, 0, False)


def test_gain_modes_with_thin_layer(sdr, board):
    dev = make_dev(control_uri=IP)
    fk = dev._dev
    for mode, iio_mode in (("hw_fast", "fast_attack"), ("hw_hybrid", "hybrid"), ("hw_slow", "slow_attack"),
                           ("manual", "manual")):
        dev.set_gain_mode(mode)
        assert fk.called("setGainMode")[-1][0] == (RX, 0, mode != "manual")
        assert board.value(f"{P}/in/voltage0/gain_control_mode") == iio_mode
        assert dev.agc_mode_actual == iio_mode
    assert dev.warnings == []


# ----------------------------------------------------------------------------- PPM correction
def test_ppm_hardware_xo_correction_exact_int(sdr, board):
    dev = make_dev(control_uri=IP)
    dev.configure(rxcfg())
    dev.start_stream()
    dev.read(8192)                                           # past the start-up settling window
    assert not dev.read(1024).settling
    fk = dev._dev
    fk.calls.clear()
    assert dev.set_ppm_correction(3.33) == "hardware"
    assert board.value(f"{P}/xo_correction") == "40000133"   # round(40e6·(1+3.33e-6))
    assert dev.ppm_hw == 3.33 and dev.ppm_hw_via == "hardware" and dev.xo_correction_hz == 40_000_133
    assert fk.called("setSampleRate")[-1][0] == (RX, 0, FS)          # PLLs re-programmed
    assert fk.called("setFrequency")[-1][0] == (RX, 0, 915e6)
    assert fk.called("setFrequencyCorrection") == []
    blk = dev.read(1024)
    assert blk.settling and blk.meta["ppm_hw"] == 3.33 and blk.meta["ppm_hw_via"] == "hardware"
    assert dev.set_ppm_correction(-12.5) == "hardware"
    assert board.value(f"{P}/xo_correction") == "39999500"
    assert dev.set_ppm_correction(0.0) == "hardware"
    assert board.value(f"{P}/xo_correction") == "40000000" and dev.ppm_hw == 0.0
    with pytest.raises(ConfigError):
        dev.set_ppm_correction(float("nan"))


def test_ppm_soapy_fallback_without_thin_layer(monkeypatch, no_iio):
    sdr = fake_soapy.install(monkeypatch, has_frequency_correction=True)
    dev = make_dev(control_uri="none")
    assert dev.set_ppm_correction(3.33) == "soapy"
    assert sdr.state.last().called("setFrequencyCorrection")[-1][0] == (RX, 0, 3.33)
    assert dev.ppm_hw == 3.33 and dev.ppm_hw_via == "soapy"


def test_ppm_old_firmware_without_xo_falls_back_to_soapy(monkeypatch):
    fake_iio.install(monkeypatch, fake_iio.make_pluto_board(IP, has_xo=False))
    sdr = fake_soapy.install(monkeypatch, has_frequency_correction=True)
    dev = make_dev(control_uri=IP)
    assert dev.set_ppm_correction(-7.0) == "soapy"
    assert sdr.state.last().called("setFrequencyCorrection")[-1][0] == (RX, 0, -7.0)
    assert any("xo_correction" in w for w in dev.warnings)


def test_ppm_none_when_nothing_available(sdr):
    dev = make_dev(control_uri="none")
    dev.configure(rxcfg())
    assert dev.set_ppm_correction(5.0) == "none"
    assert dev.ppm_hw == 0.0 and dev.ppm_hw_via == "none"


def test_thin_open_attempted_once_then_soapy(monkeypatch):
    b = fake_iio.make_pluto_board(IP, open_error=(110, "Connection timed out"))
    fake_iio.install(monkeypatch, b)
    fake_soapy.install(monkeypatch, has_frequency_correction=True)
    dev = make_dev(control_uri=IP)
    assert dev.set_ppm_correction(4.0) == "soapy"            # thin layer unreachable → soapy
    assert dev.set_ppm_correction(4.0) == "soapy"            # thin open is attempted only once
    assert sum("açılamadı" in w for w in dev.warnings) == 1


def test_hardware_ppm_resets_previous_soapy_correction(monkeypatch, board):
    sdr = fake_soapy.install(monkeypatch, has_frequency_correction=True)
    dev = make_dev(control_uri=IP)
    fk = sdr.state.last()
    # +300 ppm is outside the driver's xo_correction range (±200 ppm) → Soapy correction
    assert dev.set_ppm_correction(300.0) == "soapy"
    assert fk.getFrequencyCorrection(RX, 0) == 300.0
    assert board.value(f"{P}/xo_correction") == "40000000"
    # a valid value goes to hardware and the Soapy correction is removed (no double correction)
    assert dev.set_ppm_correction(4.0) == "hardware"
    assert board.value(f"{P}/xo_correction") == "40000160"
    assert fk.called("setFrequencyCorrection")[-1][0] == (RX, 0, 0.0)
    assert fk.getFrequencyCorrection(RX, 0) == 0.0


# ----------------------------------------------------------------------------- tracking
def test_tracking_paths_soapy_and_iio(monkeypatch, board):
    sdr = fake_soapy.install(monkeypatch, has_dc_offset_mode=True, has_iq_balance_mode=True)
    dev = make_dev(control_uri=IP)
    res = dev.set_tracking(dc=True, rf_dc=False, quadrature=True)
    assert res == {"dc": "soapy", "rf_dc": "iio", "quadrature": "soapy"}
    fk = sdr.state.last()
    assert fk.called("setDCOffsetMode")[-1][0] == (RX, 0, True)
    assert fk.called("setIQBalanceMode")[-1][0] == (RX, 0, True)
    assert board.writes == [(f"{P}/in/voltage0/rf_dc_offset_tracking_en", "0")]


def test_tracking_old_binding_goes_through_iio(monkeypatch, board):
    fake_soapy.install(monkeypatch, has_dc_offset_mode=False,
                       missing_methods=("hasIQBalanceMode", "setIQBalanceMode", "getIQBalanceMode"))
    dev = make_dev(control_uri=IP)
    assert dev.set_tracking(dc=False, rf_dc=True, quadrature=False) == {
        "dc": "iio", "rf_dc": "iio", "quadrature": "iio"}
    assert board.value(f"{P}/in/voltage0/bb_dc_offset_tracking_en") == "0"
    assert board.value(f"{P}/in/voltage0/quadrature_tracking_en") == "0"


def test_tracking_unsupported_warns(monkeypatch, no_iio):
    fake_soapy.install(monkeypatch, has_dc_offset_mode=False,
                       missing_methods=("hasIQBalanceMode", "setIQBalanceMode", "getIQBalanceMode"))
    dev = make_dev(control_uri="none")
    dev.configure(rxcfg())                                   # configure() requests all three
    assert dev.set_tracking(dc=True, rf_dc=True, quadrature=True) == {
        "dc": "unsupported", "rf_dc": "unsupported", "quadrature": "unsupported"}
    tw = [w for w in dev.warnings if "tracking" in w]
    assert len(tw) == 3 and all("ayarlanamadı" in w for w in tw)


# ----------------------------------------------------------------------------- thin-layer URI
def test_thin_uri_derivation(monkeypatch, no_iio):
    fake_soapy.install(monkeypatch, devices=[
        {"driver": "plutosdr", "label": "PlutoSDR #0 ip:192.168.2.1", "uri": "ip:192.168.2.1"},
        dict(fake_soapy.PLUTO_ENTRY)])
    assert make_dev(args={"driver": "plutosdr", "uri": "usb:1.5.5"}, control_uri="").thin_uri() == "usb:1.5.5"
    assert make_dev(args={"driver": "plutosdr", "hostname": "192.168.2.1"}, control_uri="").thin_uri() == IP
    assert make_dev(args={"driver": "plutosdr", "uri": "usb:1.5.5"}, control_uri=IP).thin_uri() == IP
    assert make_dev(args={"driver": "plutosdr", "uri": "usb:1.5.5"}, control_uri="none").thin_uri() == ""
    # neither given: libiio context URI reported by the driver (hardware info) of the device it
    # actually opened (SoapyPlutoSDR takes the first context found: here the ip one)
    dev = make_dev(args={"driver": "plutosdr"}, control_uri="")
    assert dev.thin_uri() == dev._dev.entry["uri"] == IP


def test_thin_layer_usb_conflict_falls_back(monkeypatch, sdr):
    b = fake_iio.make_pluto_board("usb:1.5.5", open_error=(16, "Device or resource busy"))
    fake_iio.install(monkeypatch, b)
    dev = make_dev(control_uri="")
    dev.configure(rxcfg())                                   # must not crash
    assert dev.thin_layer() is None
    assert dev.set_ppm_correction(2.0) == "none"
    msgs = [w for w in dev.warnings if "usb:1.5.5" in w]
    assert len(msgs) == 1 and 'control_uri = "ip:192.168.2.1"' in msgs[0]


# ----------------------------------------------------------------------------- sensors
def test_read_sensors_with_thin_layer(monkeypatch, board):
    fake_soapy.install(monkeypatch, failing_sensors=("temp0",))
    s = make_dev(control_uri=IP).read_sensors()
    assert s["xadc_temp0"] == 41.5 and s["temp0"] is None
    assert s["temperature_c"] == pytest.approx(36.25) and s["rssi_db"] == pytest.approx(105.25)


def test_read_sensors_soapy_only_and_never_raises(monkeypatch, no_iio):
    fake_soapy.install(monkeypatch)
    s = make_dev().read_sensors()
    assert s["temperature_c"] == 35.25                       # RF chip sensor preferred over XADC
    fake_soapy.install(monkeypatch, failing_sensors=("temp0",))
    assert make_dev().read_sensors()["temperature_c"] == 41.5
    fake_soapy.install(monkeypatch, missing_methods=("listSensors",))
    assert make_dev().read_sensors() == {}
    dev = make_dev()
    dev.close()
    assert dev.read_sensors() == {}


# ----------------------------------------------------------------------------- lifecycle
@pytest.mark.parametrize("define_del", [True, False])
def test_close_stops_stream_and_releases_device_once(monkeypatch, no_iio, define_del):
    sdr = fake_soapy.install(monkeypatch, define_del=define_del)
    dev = make_dev()
    dev.configure(rxcfg())
    dev.start_stream()
    dev.read(512)
    fk = dev._dev
    dev.close()
    assert [n for n, _, _ in fk.calls][-2:] == ["deactivateStream", "closeStream"]
    del fk
    gc.collect()
    assert len(sdr.state.unmade) == 1 and sdr.state.double_unmake == 0
    dev.close()                                              # idempotent
    gc.collect()
    assert len(sdr.state.unmade) == 1 and sdr.state.double_unmake == 0


def test_describe(sdr):
    dev = make_dev()
    dev.configure(rxcfg())
    d = dev.describe()
    assert d["stream_format"] == "CS16" and d["info"]["driver"] == "PlutoSDR"
    assert d["fc_hz"] == 915e6 and d["fs_hz"] == FS and d["num_channels"] == 1


# ----------------------------------------------------------------------------- serial selection
SER_A = "104473dc5993000917000e00c83b2e5e35"
SER_B = "1044734c9605000f12003300af7c5b6a8e"
TWO_PLUTOS = [
    {"device": "PlutoSDR", "driver": "plutosdr", "label": "PlutoSDR #0 usb:1.5.5", "uri": "usb:1.5.5",
     "serial": SER_A},
    {"device": "PlutoSDR", "driver": "plutosdr", "label": "PlutoSDR #1 usb:1.6.5", "uri": "usb:1.6.5",
     "serial": SER_B},
]


def _count_enumerate(sdr) -> list[dict]:
    calls: list[dict] = []
    orig = sdr.Device.enumerate

    def spy(args=None):
        calls.append(dict(fake_soapy._parse_args(args)))
        return orig(args)

    sdr.Device.enumerate = staticmethod(spy)
    return calls


def _live_devices() -> int:
    """Devices currently made (not unmade) in the installed fake."""
    import sys
    return len(sys.modules["SoapySDR"].state.live)


def test_soapy_pluto_ignores_serial_so_the_backend_resolves_it(monkeypatch, no_iio):
    sdr = fake_soapy.install(monkeypatch, devices=[dict(e) for e in TWO_PLUTOS])
    # the driver alone opens the FIRST Pluto whatever serial is asked for (SoapyPlutoSDR)
    raw = sdr.Device({"driver": "plutosdr", "serial": SER_B})
    assert raw.entry["uri"] == "usb:1.5.5"
    del raw
    calls = _count_enumerate(sdr)
    dev = make_dev(args={"driver": "plutosdr", "serial": SER_B})
    assert calls == [{"driver": "plutosdr"}]                  # driver filter only
    fk = dev._dev
    assert fk.entry["uri"] == "usb:1.6.5" and fk.args["uri"] == "usb:1.6.5"
    assert dev.device_args == {"driver": "plutosdr", "serial": SER_B, "uri": "usb:1.6.5"}
    assert dev.info.serial == SER_B and dev.info.uri == "usb:1.6.5"
    dev.close()


@pytest.mark.parametrize("asked,uri", [
    (SER_A[:12], "usb:1.5.5"),                                # unique prefix
    (SER_B[:10].upper(), "usb:1.6.5"),                        # case-insensitive prefix
    (SER_A, "usb:1.5.5"),                                     # exact
])
def test_serial_prefix_and_case_insensitive_match(monkeypatch, no_iio, asked, uri):
    fake_soapy.install(monkeypatch, devices=[dict(e) for e in TWO_PLUTOS])
    dev = make_dev(args={"driver": "plutosdr", "serial": asked})
    assert dev._dev.entry["uri"] == uri
    assert dev.device_args["serial"] == (SER_A if uri == "usb:1.5.5" else SER_B)   # full serial recorded
    dev.close()


def test_serial_from_libiio_label_when_entry_has_no_serial_key(monkeypatch, no_iio):
    devices = [
        {"device": "PlutoSDR", "driver": "plutosdr", "uri": "usb:3.4.5",
         "label": f"0456:b673 (Analog Devices Inc. PlutoSDR (ADALM-PLUTO)), serial={SER_A}"},
        {"device": "PlutoSDR", "driver": "plutosdr", "uri": "usb:3.7.5",
         "label": f"0456:b673 (Analog Devices Inc. PlutoSDR (ADALM-PLUTO)), serial={SER_B}"},
    ]
    fake_soapy.install(monkeypatch, devices=devices)
    dev = make_dev(args={"driver": "plutosdr", "serial": SER_B[:8]})
    assert dev._dev.entry["uri"] == "usb:3.7.5" and dev.info.serial == SER_B
    dev.close()


def test_ambiguous_serial_prefix_lists_candidates(monkeypatch, no_iio):
    fake_soapy.install(monkeypatch, devices=[dict(e) for e in TWO_PLUTOS])
    with pytest.raises(DeviceNotFound) as ei:
        make_dev(args={"driver": "plutosdr", "serial": "10447"})        # prefix of both
    msg = str(ei.value)
    assert "birden çok" in msg and SER_A in msg and SER_B in msg and "usb:1.5.5" in msg and "usb:1.6.5" in msg
    assert _live_devices() == 0                                       # nothing was opened


def test_unknown_serial_lists_candidates(monkeypatch, no_iio):
    fake_soapy.install(monkeypatch, devices=[dict(e) for e in TWO_PLUTOS])
    with pytest.raises(DeviceNotFound) as ei:
        make_dev(args={"driver": "plutosdr", "serial": "deadbeef"})
    msg = str(ei.value)
    assert "deadbeef" in msg and "eşleşen" in msg and SER_A in msg and SER_B in msg and "SoapySDRUtil" in msg
    assert _live_devices() == 0
    with pytest.raises(ConfigError):
        make_dev(args={"driver": "plutosdr", "serial": "  "})


def test_uri_or_hostname_given_skips_serial_resolution(monkeypatch, no_iio):
    sdr = fake_soapy.install(monkeypatch, devices=[dict(e) for e in TWO_PLUTOS])
    calls = _count_enumerate(sdr)
    dev = make_dev(args={"driver": "plutosdr", "uri": "usb:1.6.5", "serial": SER_B})
    assert calls == [] and dev._dev.entry["uri"] == "usb:1.6.5"
    assert dev.device_args == {"driver": "plutosdr", "uri": "usb:1.6.5", "serial": SER_B}
    dev.close()


def test_same_device_listed_twice_is_not_ambiguous(monkeypatch, no_iio):
    dup = [dict(TWO_PLUTOS[0]), dict(TWO_PLUTOS[0]), dict(TWO_PLUTOS[1])]
    fake_soapy.install(monkeypatch, devices=dup)
    dev = make_dev(args={"driver": "plutosdr", "serial": SER_A[:9]})
    assert dev._dev.entry["uri"] == "usb:1.5.5"
    dev.close()


def test_serial_for_driver_without_uri_keeps_native_filtering(monkeypatch, no_iio):
    """UHD-like drivers filter by serial themselves and enumerate no 'uri': the prefix is
    expanded to the full serial and passed on."""
    fake_soapy.install(
        monkeypatch, filters_serial=True, num_channels=2, driver_key="UHD", hardware_key="B210",
        devices=[{"driver": "uhd", "label": "B210 31415A", "serial": "31415A"},
                 {"driver": "uhd", "label": "B210 27182B", "serial": "27182B"}],
        hardware_info={"serial": "x"}, native_full_scale=32768.0, cs16_full_scale=32768.0,
    )
    dev = make_dev(args={"driver": "uhd", "serial": "2718"}, profile=load_profile("usrp_b210"), channels=[0, 1])
    assert dev.device_args == {"driver": "uhd", "serial": "27182B"} and "uri" not in dev._dev.args
    assert dev._dev.entry["serial"] == "27182B"
    dev.close()
