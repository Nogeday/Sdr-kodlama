"""Hardware signal sources (eds.hal.tx_hw): tone buffer math, Soapy TX with the fake TX stream,
SCPI against an in-test TCP server, and the operator-prompt source."""

from __future__ import annotations

import gc
import socket
import socketserver
import threading
import time

import numpy as np
import pytest

from eds.core.errors import (
    BackendUnavailable,
    ConfigError,
    DeviceError,
    DeviceNotFound,
    MeasurementError,
    NotSupported,
    StreamError,
)
from eds.core.timeutil import now_ns
from eds.hal.tx import open_source
from eds.hal.tx_hw import (
    DIGITAL_FULL_AMPLITUDE,
    ManualSource,
    ScpiSignalGenerator,
    SoapyTxSource,
    build_tone_buffer,
    quantize_tone_freq,
)
from tests.hal.fakes import fake_soapy

TX = fake_soapy.SOAPY_SDR_TX
FS = 2.4e6
N = 1024
F_Q = 85 * FS / N          # round(200 kHz · 1024 / 2.4 MHz) = 85 cycles → 199 218.75 Hz


def wait_until(cond, timeout_s: float = 2.0) -> None:
    t_end = time.perf_counter() + timeout_s
    while not cond():
        if time.perf_counter() > t_end:
            raise AssertionError("koşul zaman aşımına uğradı")
        time.sleep(0.002)


# ----------------------------------------------------------------------------- tone buffer
def test_tone_buffer_integer_cycles_and_phase_continuity():
    buf, freqs = build_tone_buffer([200e3], [0.5], FS, N)
    assert buf.dtype == np.complex64 and buf.shape == (N,)
    assert freqs == [F_Q] and quantize_tone_freq(200e3, FS, N) == F_Q
    np.testing.assert_allclose(np.abs(buf), 0.5, atol=1e-6)
    # looping the buffer equals one continuous tone (no phase jump at the wrap)
    k = np.arange(3 * N)
    ideal = 0.5 * np.exp(2j * np.pi * F_Q * k / FS)
    np.testing.assert_allclose(np.tile(buf, 3), ideal, atol=2e-6)
    step = np.angle(buf[0] * np.conj(buf[-1]))
    assert step == pytest.approx(2 * np.pi * F_Q / FS, abs=1e-5)
    # all energy in exactly one FFT bin (no leakage = integer cycles)
    spec = np.abs(np.fft.fft(buf) / N) ** 2
    assert np.argmax(spec) == 85 and spec[85] == pytest.approx(0.25, rel=1e-5)
    assert spec.sum() - spec[85] < 1e-10


def test_tone_buffer_two_tones_and_negative_frequency():
    buf, freqs = build_tone_buffer([-50e3, 50e3], [0.25, 0.25], FS, N)
    assert freqs == [-21 * FS / N, 21 * FS / N]
    assert np.max(np.abs(buf)) <= 0.5 + 1e-6
    spec = np.abs(np.fft.fft(buf) / N) ** 2
    assert spec[21] == pytest.approx(0.0625, rel=1e-5) and spec[N - 21] == pytest.approx(0.0625, rel=1e-5)


def test_tone_buffer_validation():
    with pytest.raises(ConfigError):
        build_tone_buffer([1.3e6], [0.5], FS, N)               # beyond Nyquist
    with pytest.raises(ConfigError):
        build_tone_buffer([1e3, 2e3], [0.5], FS, N)
    with pytest.raises(ConfigError):
        build_tone_buffer([1e3], [-0.1], FS, N)
    with pytest.raises(ConfigError):
        build_tone_buffer([1e3], [0.1], FS, 1)


# ----------------------------------------------------------------------------- Soapy TX
@pytest.fixture
def sdr(monkeypatch):
    return fake_soapy.install(monkeypatch)


@pytest.fixture
def src(sdr):
    s = SoapyTxSource(fs=FS, tone_offset_hz=200e3, buffer_len=N)
    yield s
    s.close()


def test_soapy_tx_backend_unavailable(monkeypatch):
    fake_soapy.uninstall(monkeypatch)
    with pytest.raises(BackendUnavailable):
        SoapyTxSource()


def test_soapy_tx_setup(sdr, src):
    fk = sdr.state.last()
    assert fk.args == {"driver": "plutosdr"}
    assert fk.called("setSampleRate")[0][0] == (TX, 0, FS)
    assert fk.called("setupStream")[0][0] == (TX, "CF32", [0], {"bufflen": str(N)})
    assert fk.getGain(TX, 0) == 0.0                          # safe start: minimum TX gain
    assert src.absolute_power is False and src.power_range_db == (-149.0, 0.0)
    assert src.freq_range_hz == (46.875e6, 6e9)
    wait_until(lambda: fk.tx_recorded >= 4 * N)
    assert not np.any(fk.tx_samples())                       # disabled → zeros only
    with pytest.raises(ConfigError):
        src.set_enabled(True)                                # no frequency yet


def test_soapy_tx_tone_on_off_phase_continuous(sdr, src):
    fk = sdr.state.last()
    assert src.set_frequency(915e6) == pytest.approx(915e6, abs=1e-6)
    assert fk.getFrequency(TX, 0) == pytest.approx(915e6 - F_Q, abs=1e-6)   # LO away from the tone
    assert src.set_power(-10.0) == pytest.approx(-10.0, abs=1e-9)
    assert fk.getGain(TX, 0) == 79.0 and src.digital_amps == [pytest.approx(0.5)]
    t_before = now_ns()
    t_on = src.set_enabled(True)
    assert t_before <= t_on <= now_ns()
    n0 = fk.tx_recorded
    wait_until(lambda: fk.tx_recorded >= n0 + 5 * N)
    x = fk.tx_samples()
    i0 = int(np.flatnonzero(np.abs(x) > 0)[0])
    seg = x[i0:i0 + 4 * N]
    assert len(seg) == 4 * N                                 # spans several buffer wraps
    np.testing.assert_allclose(np.abs(seg), 0.5, atol=1e-6)
    steps = np.angle(seg[1:] * np.conj(seg[:-1]))
    np.testing.assert_allclose(steps, 2 * np.pi * F_Q / FS, atol=1e-5)
    src.set_enabled(False)
    n1 = fk.tx_recorded
    wait_until(lambda: fk.tx_recorded >= n1 + 3 * N)
    tail = fk.tx_samples()[n1 + N:]
    assert len(tail) >= N and not np.any(tail)               # zeros after switching off
    assert src.samples_written >= fk.tx_recorded - N


def test_soapy_tx_power_split_gain_then_digital(sdr, src):
    fk = sdr.state.last()
    src.set_frequency(915e6)
    assert src.set_power(-100.0) == pytest.approx(-100.0, abs=1e-9)
    assert fk.getGain(TX, 0) == 0.0                          # TX gain exhausted (89 dB) ...
    assert src.digital_amps[0] == pytest.approx(0.5 * 10 ** (-11 / 20))   # ... remainder digital
    assert src.set_power(-10.4) == pytest.approx(-10.4, abs=1e-9)
    assert fk.getGain(TX, 0) == 79.0                         # driver truncates → one step up
    assert src.digital_amps[0] == pytest.approx(0.5 * 10 ** (-0.4 / 20))
    assert src.set_power(5.0) == pytest.approx(0.0)          # cannot exceed relative 0 dB
    assert fk.getGain(TX, 0) == 89.0 and src.digital_amps[0] == pytest.approx(DIGITAL_FULL_AMPLITUDE)
    assert src.set_power(-500.0) == pytest.approx(-149.0)    # digital floor (−60 dB) below min gain
    with pytest.raises(ConfigError):
        src.set_power(float("nan"))


def test_soapy_tx_absolute_power_with_anchor(sdr):
    with SoapyTxSource(fs=FS, buffer_len=N, anchor_dbm=-20.0, path_loss_db=3.0) as s:
        fk = sdr.state.last()
        assert s.absolute_power is True
        assert s.power_range_db == (-172.0, -23.0)
        s.set_frequency(2.4e9)
        assert s.set_power(-50.0) == pytest.approx(-50.0, abs=1e-9)
        assert fk.getGain(TX, 0) == 62.0                     # −50 − (−20 − 3) = −27 dB relative


def test_soapy_tx_two_tones(sdr, src):
    fk = sdr.state.last()
    src.set_tones([915.0e6, 915.1e6], [-6.0206, -6.0206])
    half = 21 * FS / N
    assert fk.getFrequency(TX, 0) == pytest.approx(915.05e6)          # LO at the midpoint
    assert src.tones_hz == [pytest.approx(915.05e6 - half), pytest.approx(915.05e6 + half)]
    assert fk.getGain(TX, 0) == 89.0
    assert src.digital_amps == [pytest.approx(0.25, rel=1e-4), pytest.approx(0.25, rel=1e-4)]
    assert np.max(np.abs(src._tone)) <= DIGITAL_FULL_AMPLITUDE + 1e-6
    spec = np.abs(np.fft.fft(src._tone) / N) ** 2
    assert spec[21] == pytest.approx(0.0625, rel=1e-3) and spec[N - 21] == pytest.approx(0.0625, rel=1e-3)
    assert src.tone_levels_rel_db == [pytest.approx(-6.0206, abs=1e-3)] * 2
    with pytest.raises(NotSupported):
        src.set_tones([1e9, 1.1e9, 1.2e9], [0, 0, 0])
    with pytest.raises(ConfigError):
        src.set_tones([1e9, 1.002e9], [0, 0])                       # 2 MHz spacing > 0.4·fs·2


def test_soapy_tx_lo_out_of_range(sdr, src):
    with pytest.raises(ConfigError):
        src.set_frequency(30e6)


def test_soapy_tx_underflow_counted_and_stream_continues(sdr, src):
    fk = sdr.state.last()
    fk.tx_script.extend(["underflow", "timeout", "underflow"])
    wait_until(lambda: src.underflows == 2 and src.write_timeouts >= 1)
    n = fk.tx_recorded
    wait_until(lambda: fk.tx_recorded >= n + 2 * N)
    assert src.describe()["underflows"] == 2


def test_soapy_tx_fatal_write_error_surfaces(sdr, src):
    fk = sdr.state.last()
    src.set_frequency(915e6)
    fk.tx_script.append(fake_soapy.SOAPY_SDR_STREAM_ERROR)
    wait_until(lambda: src._error is not None)
    with pytest.raises(StreamError, match="STREAM_ERROR"):
        src.set_power(-10.0)
    with pytest.raises(StreamError):
        src.set_enabled(True)


def test_soapy_tx_close_releases(sdr):
    s = SoapyTxSource(fs=FS, buffer_len=N)
    fk = sdr.state.last()
    s.set_frequency(915e6)
    s.set_enabled(True)
    s.close()
    assert s._thread is None and fk.called("deactivateStream") and fk.called("closeStream")
    assert not any(t.name == "eds-tx-writer" and t.is_alive() for t in threading.enumerate())
    assert s.set_enabled(False) > 0                          # after close: no-op
    with pytest.raises(StreamError):
        s.set_enabled(True)
    del fk
    gc.collect()
    assert len(sdr.state.unmade) == 1 and sdr.state.double_unmake == 0
    s.close()


def test_soapy_tx_open_failure(monkeypatch):
    fake_soapy.install(monkeypatch)
    with pytest.raises(DeviceNotFound):
        SoapyTxSource(args={"driver": "hackrf"})
    with pytest.raises(ConfigError):
        SoapyTxSource(tone_offset_hz=0.0)


def test_open_source_factory(sdr):
    s = open_source("pluto_tx", {"fs": FS, "buffer_len": N})
    assert isinstance(s, SoapyTxSource)
    s.close()


# ----------------------------------------------------------------------------- SCPI
class _ScpiHandler(socketserver.StreamRequestHandler):
    def handle(self):
        srv = self.server
        for raw in self.rfile:
            cmd = raw.decode("ascii").strip()
            srv.log.append(cmd)
            reply = None
            if cmd == "*IDN?":
                reply = None if srv.mute else "FakeGen,SG-1000,0001,1.0"
            elif cmd == "*OPC?":
                reply = "1"
            elif cmd == ":FREQ?":
                reply = f"{srv.freq:.3f}"
            elif cmd == ":POW?":
                reply = f"{srv.pow:.2f}"
            elif cmd.startswith(":FREQ "):
                srv.freq = float(cmd.split()[1])
            elif cmd.startswith(":POW "):
                srv.pow = min(float(cmd.split()[1]), srv.max_pow)
            elif cmd.startswith(":OUTP "):
                srv.out = cmd.split()[1] == "ON"
            if reply is not None:
                self.wfile.write((reply + "\n").encode("ascii"))
                self.wfile.flush()


@pytest.fixture
def scpi_server():
    srv = socketserver.ThreadingTCPServer(("127.0.0.1", 0), _ScpiHandler)
    srv.daemon_threads = True
    srv.log, srv.freq, srv.pow, srv.out, srv.max_pow, srv.mute = [], 0.0, -130.0, False, 13.0, False
    th = threading.Thread(target=srv.serve_forever, kwargs={"poll_interval": 0.05}, daemon=True)
    th.start()
    yield srv
    srv.shutdown()
    srv.server_close()


def test_scpi_command_sequence_and_path_loss(scpi_server):
    port = scpi_server.server_address[1]
    gen = ScpiSignalGenerator("127.0.0.1", port, timeout_s=1.0, path_loss_db=10.0)
    assert gen.idn == "FakeGen,SG-1000,0001,1.0" and gen.absolute_power is True
    assert gen.set_frequency(915e6) == 915e6
    assert gen.set_power(-60.0) == pytest.approx(-60.0)      # generator at −50 dBm
    t0 = now_ns()
    t = gen.set_enabled(True)
    assert t >= t0 and scpi_server.out is True
    assert scpi_server.log == ["*IDN?", ":FREQ 915000000.000", "*OPC?", ":FREQ?", ":POW -50.00", "*OPC?",
                               ":POW?", ":OUTP ON", "*OPC?"]
    d = gen.describe()
    assert d["idn"] == gen.idn and d["path_loss_db"] == 10.0
    scpi_server.max_pow = -5.0
    assert gen.set_power(0.0) == pytest.approx(-15.0)        # generator clamped → actual reported
    with gen:
        pass
    assert scpi_server.out is False and scpi_server.log[-2:] == [":OUTP OFF", "*OPC?"]
    with pytest.raises(DeviceError):
        gen.set_frequency(1e9)                                # closed


def test_scpi_connect_refused():
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    with pytest.raises(DeviceNotFound):
        ScpiSignalGenerator("127.0.0.1", port, timeout_s=0.5)


@pytest.mark.realtime
def test_scpi_no_answer_times_out(scpi_server):
    scpi_server.mute = True
    with pytest.raises(DeviceError, match="SCPI"):
        ScpiSignalGenerator("127.0.0.1", scpi_server.server_address[1], timeout_s=0.2)


# ----------------------------------------------------------------------------- manual
class PromptStub:
    def __init__(self, replies):
        self.replies = list(replies)
        self.prompts: list[str] = []

    def __call__(self, msg: str) -> str:
        self.prompts.append(msg)
        if not self.replies:
            raise EOFError
        return self.replies.pop(0)


def test_manual_source_prompts_and_operator_values():
    p = PromptStub(["", "915,0005e6", "", "-49,5", ""])
    m = ManualSource(prompt=p, path_loss_db=10.0)
    assert m.absolute_power is True
    assert m.set_frequency(915e6) == 915e6
    assert m.set_frequency(915.0005e6) == pytest.approx(915.0005e6)   # operator typed the actual value
    assert m.set_power(-60.0) == -60.0
    assert m.set_power(-60.0) == pytest.approx(-59.5)                  # typed generator level −49.5 dBm
    t0 = now_ns()
    assert m.set_enabled(True) >= t0
    assert "915 MHz" in p.prompts[0] and "Enter" in p.prompts[0]
    assert "-50.00 dBm" in p.prompts[2] and "yol kaybı 10.00 dB" in p.prompts[2]
    assert "AÇIN" in p.prompts[4]


def test_manual_source_invalid_then_valid_and_eof():
    p = PromptStub(["abc", "-30"])
    m = ManualSource(prompt=p, absolute_power=False)
    assert m.set_power(-30.0) == -30.0
    assert p.prompts[1].startswith("Geçersiz sayı") and "bağıl" in p.prompts[1]
    with pytest.raises(MeasurementError):
        ManualSource(prompt=PromptStub([])).set_frequency(1e9)
    with pytest.raises(MeasurementError):
        ManualSource(prompt=PromptStub(["x"] * 5)).set_frequency(1e9)
    m2 = ManualSource(prompt=PromptStub(["", ""]))
    with m2:                                                  # __exit__ asks to switch RF off
        pass
    assert m2.enabled is False
