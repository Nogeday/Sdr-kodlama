"""Tests of eds.rx.node (RX node service) against the physics-based simulator.

Physical checks use the simulator's level equation (``SimRxDevice.level_dbfs`` /
``noise_density_dbfs_hz``), the ARCHITECTURE §2 conventions (power-spectrum scaling,
RBW = ENBW·fs/N, ppm sign) and the bus JSON schemas (every received message is validated).
"""

from __future__ import annotations

import math
import socket
import threading
import time

import numpy as np
import pytest

from eds.bus import topics as T
from eds.bus.pubsub import Publisher, Subscriber
from eds.bus.validate import validate_message
from eds.core.config import (
    AgcConfig,
    AppConfig,
    BusConfig,
    CalConfig,
    DeviceConfig,
    DspConfig,
    RxConfig,
    SimConfig,
)
from eds.core.errors import DeviceError, EDSError, ProfileViolation
from eds.core.types import IQBlock
from eds.dsp.windows import window_info
from eds.hal import open_device
from eds.rx.node import RxNode, WindowAccumulator, decimate_max

FC = 915e6
FS = 2e6
F_CW = 915.3e6
P_CW = -80.0
GAIN = 50.0
PPM = 11.7


# --------------------------------------------------------------------------- helpers
def free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return int(s.getsockname()[1])


def make_app(realtime: bool, emitters=None, *, gain_mode: str = "manual", gain: float = GAIN,
             ppm_override: float = PPM, block: int = 32768, bus: BusConfig | None = None,
             tmp_cal: str = "") -> AppConfig:
    ems = emitters if emitters is not None else [
        {"name": "cw1", "kind": "cw", "freq_hz": F_CW, "power_dbm": P_CW}]
    return AppConfig(
        devices=[DeviceConfig(name="rx0", backend="sim", sim={"realtime": realtime, "ppm": PPM})],
        rx=RxConfig(center_freq_hz=FC, sample_rate_hz=FS, bandwidth_hz=FS, gain_db=gain, gain_mode=gain_mode,
                    block_size=block, buffer_size=block),
        dsp=DspConfig(nfft=4096, averages=8),
        bus=bus or BusConfig(status_period_s=0.25, spectrum_rate_hz=20.0, spectrum_max_bins=1000),
        cal=CalConfig(dir=tmp_cal, ppm_override=ppm_override),
        sim=SimConfig(emitters=ems, seed=7),
    )


class Bus:
    """Validating publisher on a private TCP endpoint + a subscriber collecting everything."""

    def __init__(self, topics=(T.RX_STATUS, T.RX_SPECTRUM, T.RX_DETECTIONS, T.RX_SWEEP, T.CTL_ACK)):
        self.pub = Publisher("tcp://127.0.0.1:*", bind=True, src="rx0", validate=True)
        self.sub = Subscriber(self.pub.endpoint, list(topics), rcvhwm=100_000)

    def collect(self, duration_s: float, until=None) -> list:
        out = []
        end = time.monotonic() + duration_s
        while (rem := end - time.monotonic()) > 0:
            m = self.sub.recv(rem)
            if m is None:
                continue
            out.append(m)
            if until is not None and until(m):
                break
        return out

    def wait(self, pred, timeout_s: float = 10.0):
        end = time.monotonic() + timeout_s
        while (rem := end - time.monotonic()) > 0:
            m = self.sub.recv(rem)
            if m is not None and pred(m):
                return m
        return None

    def close(self) -> None:
        self.sub.close()
        self.pub.close()


@pytest.fixture
def bus():
    b = Bus()
    yield b
    b.close()


def by_topic(msgs, topic):
    return [m for m in msgs if m.topic == topic]


# --------------------------------------------------------------------------- pure helpers
def _blk(index: int, n: int, *, t0_ns: int = 1_000_000_000, fs: float = 1e6, fc: float = 1e9, gain=(40.0,),
         overflow: bool = False, seq: int = 0) -> IQBlock:
    x = (np.arange(index, index + n) % 1000).astype(np.complex64)[None, :]
    return IQBlock(samples=x, fs=fs, fc=fc, t_ns=t0_ns + int(round(index / fs * 1e9)), index=index, seq=seq,
                   gain_db=tuple(gain), overflow=overflow, meta={"ppm_hw": 0.0})


def test_window_accumulator_contiguity_and_timestamps():
    acc = WindowAccumulator(1500)
    acc.push(_blk(0, 1000, seq=0))
    assert acc.pop() is None
    acc.push(_blk(1000, 1000, seq=1))
    w = acc.pop()
    assert w is not None and w.samples.shape == (1, 1500)
    np.testing.assert_array_equal(w.samples[0].real, np.arange(0, 1500) % 1000)
    assert w.t_acq_ns == 1_000_000_000 and w.t_acq_end_ns == 1_000_000_000 + 1_500_000      # 1500 µs at 1 MS/s
    assert (w.seq, w.index, w.gap_before) == (0, 0, False)
    assert acc.pop() is None and acc.available == 500
    acc.push(_blk(2000, 1000, seq=2))
    w2 = acc.pop()
    np.testing.assert_array_equal(w2.samples[0].real, np.arange(1500, 3000) % 1000)
    assert (w2.seq, w2.index) == (1, 1500) and w2.t_acq_ns == 1_000_000_000 + 1_500_000
    # host drop (index jump): the buffer restarts at the new block and the gap is flagged
    acc.push(_blk(5000, 1000, seq=5))
    acc.push(_blk(6000, 1000, seq=6))
    w3 = acc.pop()
    assert (w3.index, w3.gap_before) == (5000, True)
    # driver overflow before a block and a retune both restart the window
    acc.push(_blk(7000, 1000, overflow=True))
    assert acc.available == 1000
    acc.push(_blk(8000, 1000, fc=1.1e9))
    assert acc.available == 1000 and acc.pop() is None


def test_decimate_max_matches_brute_force():
    rng = np.random.default_rng(3)
    p = rng.normal(-90.0, 3.0, 4097)
    p[1234] = -20.0
    v, d = decimate_max(p, 1000)
    assert d == math.ceil(4097 / 1000) == 5 and v.size == math.ceil(4097 / d)
    ref = [p[i:i + d].max() for i in range(0, p.size, d)]
    np.testing.assert_array_equal(v, ref)
    assert v[1234 // d] == -20.0
    v1, d1 = decimate_max(p[:800], 1000)
    assert d1 == 1 and np.array_equal(v1, p[:800])


# --------------------------------------------------------------------------- pipeline
def _check_pipeline(msgs, node: RxNode, realtime: bool) -> None:
    dev = node.device
    for m in msgs:
        validate_message(m.topic, m.env)                    # every message conforms to its schema
    st, sp, de = by_topic(msgs, T.RX_STATUS), by_topic(msgs, T.RX_SPECTRUM), by_topic(msgs, T.RX_DETECTIONS)
    assert st and sp and len(de) >= 5
    assert node.stats["publish_errors"] == 0 and node.stats["frame_errors"] == 0
    s = st[-1].data
    assert s["state"] == "streaming" and s["device"]["name"] == "rx0" and s["device"]["serial"] == "SIM-rx0"
    assert s["fc_hz"] == FC and s["fs_hz"] == FS and s["gain_db"] == [GAIN]
    assert s["ppm"]["via"] == "hardware" and s["ppm"]["value"] == pytest.approx(PPM)
    assert s["stream"]["blocks"] > 0 and s["levels"]["peak_dbfs"] is not None
    assert s["sensors"]["temperature_c"] == pytest.approx(35.0)
    # the published spectrum: power-spectrum scaling, RBW, decimation, ppm-corrected axis
    d = sp[-1].data
    rbw = window_info("blackmanharris", 4096).enbw_bins * FS / 4096
    assert d["rbw_hz"] == pytest.approx(rbw, rel=1e-9)                 # hardware ppm: zero residual
    assert d["decimation"] == 5 and len(d["values"]) == math.ceil(4096 / 5) and d["n_bins"] == 4096
    assert d["ppm_corrected"] is True and d["unit"] == "dBFS" and d["calibrated"] is False
    f = d["f_start_hz"] + np.arange(len(d["values"])) * d["f_step_hz"]
    k = int(np.argmax(d["values"]))
    assert abs(f[k] - F_CW) <= d["f_step_hz"]
    # noise floor: thermal density of the sim model in one RBW (8 Welch averages, median bias < 1 dB)
    nf = dev.noise_density_dbfs_hz(GAIN) + 10.0 * math.log10(rbw)
    assert d["noise_floor"] == pytest.approx(nf, abs=1.5)
    # every frame published (empty lists allowed); the CW is detected at its true frequency
    hits = [x for m in de for x in m.data["detections"] if abs(x["f_peak_hz"] - F_CW) < 2 * rbw]
    assert len(hits) >= 0.9 * len(de)
    df = FS / 4096
    assert np.median([h["f_peak_hz"] for h in hits]) == pytest.approx(F_CW, abs=0.25 * df)
    # tone power = band power (scalloping-free), level equation of the simulator
    assert np.median([h["band_power_db"] for h in hits]) == pytest.approx(dev.level_dbfs(P_CW, GAIN), abs=0.5)
    for m in de:
        x = m.data
        assert x["t_acq_ns"] < x["t_acq_end_ns"] <= x["t_proc_ns"] <= m.t_ns <= m.t_recv_ns
        assert x["t_acq_end_ns"] - x["t_acq_ns"] == round(node.window_samples / FS * 1e9)
    seqs = [m.seq for m in de]
    assert seqs == sorted(seqs)
    if realtime:   # real-time pipeline keeps up: frames ≈ fs / window, nothing dropped
        assert node.stats["stream"]["host_drops"] == 0


@pytest.mark.parametrize("realtime", [False, pytest.param(True, marks=pytest.mark.realtime)])
def test_node_publishes_schema_valid_messages_and_detects_cw(bus, realtime, tmp_path):
    app = make_app(realtime, tmp_cal=str(tmp_path))
    node = RxNode(app, publisher=bus.pub)
    assert node.ppm_via == "hardware" and node.window_samples == 4096 + 2048 * 7
    node.start()
    try:
        msgs = bus.collect(1.5 if realtime else 1.0)
    finally:
        node.stop()
    _check_pipeline(msgs, node, realtime)


def test_status_counters_and_warnings_without_calibration(bus, tmp_path):
    app = make_app(False, tmp_cal=str(tmp_path), ppm_override=float("nan"))
    node = RxNode(app, publisher=bus.pub)
    assert node.ppm_via == "none"
    s = node.status_payload()
    validate_message(T.RX_STATUS, {"topic": T.RX_STATUS, "v": 1, "src": "rx0", "seq": 0, "t_ns": 1, "data": s})
    assert s["state"] == "idle" and s["ppm"] == {"value": None, "via": "none", "age_s": None}
    assert any("PPM kalibrasyonu yok" in w for w in s["warnings"])
    assert s["levels"] == {"peak_dbfs": None, "rms_dbfs": None, "clip_fraction": None}
    node.stop()


# --------------------------------------------------------------------------- control
def test_ctl_over_bus_set_center_freq_and_profile_violation(tmp_path):
    ctl_port = free_port()
    ctl_ep = f"tcp://127.0.0.1:{ctl_port}"
    pub_ep = f"tcp://127.0.0.1:{free_port()}"
    app = make_app(False, tmp_cal=str(tmp_path), bus=BusConfig(
        pub_endpoint=pub_ep, sub_endpoint=pub_ep, ctl_endpoints=[ctl_ep], validate=True, status_period_s=0.2,
        spectrum_rate_hz=5.0))
    ctl = Publisher(ctl_ep, bind=True, src="gui", validate=True, use_xpub=True)
    sub = Subscriber(pub_ep, [T.CTL_ACK, T.RX_STATUS])
    node = RxNode(app)                                     # opens the device, publisher and ctl subscriber
    assert node.control_enabled
    node.start()
    try:
        assert ctl.wait_for_subscribers(1, 5.0, topic=T.CTL_RX)
        time.sleep(0.2)                                    # ack SUB → node PUB subscription propagation

        def ack_for(req):
            end = time.monotonic() + 10.0
            while time.monotonic() < end:
                m = sub.recv(0.5)
                if m is not None and m.topic == T.CTL_ACK and m.data["req_id"] == req:
                    validate_message(m.topic, m.env)
                    return m.data
            raise AssertionError(f"ack {req} gelmedi")

        ctl.publish(T.CTL_RX, {"req_id": "f1", "target": "rx0", "cmd": "set_center_freq", "args": {"hz": 920e6}})
        a = ack_for("f1")
        assert a["ok"] is True and a["error"] is None and a["result"]["fc_hz"] == 920e6 and a["target"] == "rx0"
        st = None
        end = time.monotonic() + 5.0
        while time.monotonic() < end:
            m = sub.recv(0.5)
            if m is not None and m.topic == T.RX_STATUS and m.data["fc_hz"] == 920e6:
                st = m.data
                break
        assert st is not None and st["state"] == "streaming"
        ctl.publish(T.CTL_RX, {"req_id": "f2", "target": "*", "cmd": "set_center_freq", "args": {"hz": 5e9}})
        b = ack_for("f2")
        assert b["ok"] is False and b["result"] is None
        assert "aralığı dışında" in b["error"] and "5 GHz" in b["error"]       # Turkish profile message
        assert node.device.fc == 920e6                                          # unchanged
        ctl.publish(T.CTL_RX, {"req_id": "f3", "target": "rx9", "cmd": "ping"})  # other node: ignored
        ctl.publish(T.CTL_RX, {"req_id": "f4", "target": "rx0", "cmd": "ping"})
        c = ack_for("f4")
        assert c["result"]["state"] == "streaming" and c["result"]["fc_hz"] == 920e6
        assert node.stats["acks"] == 3                                          # f3 got no ack
    finally:
        node.stop()
        ctl.close()
        sub.close()
    with pytest.raises(DeviceError):
        node.device.start_stream()                         # the node opened the device → closed it


def test_handle_command_validation_and_gain_changes(bus, tmp_path):
    node = RxNode(make_app(False, tmp_cal=str(tmp_path)), publisher=bus.pub)
    try:
        assert node.handle_command({"req_id": "x", "target": "other", "cmd": "ping"}) is None
        a = node.handle_command({"req_id": "u", "target": "*", "cmd": "reboot"})
        assert a["ok"] is False and "Bilinmeyen komut" in a["error"]
        a = node.handle_command({"req_id": "m", "target": "*", "cmd": "set_gain"})
        assert a["ok"] is False and "'db' argümanı gerekli" in a["error"]
        a = node.handle_command({"req_id": "g", "target": "*", "cmd": "set_gain", "args": {"db": 30}})
        assert a["ok"] and a["result"]["gain_db"] == [30.0] and node.device.gains == [30.0]
        a = node.handle_command({"req_id": "g2", "target": "*", "cmd": "set_gain", "args": {"db": 99}})
        assert a["ok"] is False and "Kazanç" in a["error"] and node.device.gains == [30.0]
        a = node.handle_command({"req_id": "r", "target": "*", "cmd": "set_sample_rate", "args": {"hz": 4e6}})
        assert a["ok"] and node.device.fs == 4e6
        a = node.handle_command({"req_id": "b", "target": "*", "cmd": "set_bandwidth", "args": {"hz": 3e6}})
        assert a["ok"] and node.device.bw == 3e6
        a = node.handle_command({"req_id": "h", "target": "*", "cmd": "set_gain_mode", "args": {"mode": "hw_slow"}})
        assert a["ok"] and node.device.gain_mode == "hw_slow"
        a = node.handle_command({"req_id": "g3", "target": "*", "cmd": "set_gain", "args": {"db": 20}})
        assert a["ok"] is False and "AGC" in a["error"]
        acks = [m for m in bus.collect(1.0) if m.topic == T.CTL_ACK]
        assert [m.data["req_id"] for m in acks] == ["u", "m", "g", "g2", "r", "b", "h", "g3"]
        for m in acks:
            validate_message(m.topic, m.env)
    finally:
        node.stop()


@pytest.mark.realtime
def test_sweep_command_publishes_valid_rx_sweep_and_restores_pipeline(bus, tmp_path):
    node = RxNode(make_app(True, tmp_cal=str(tmp_path)), publisher=bus.pub)
    node.start()
    try:
        assert bus.wait(lambda m: m.topic == T.RX_DETECTIONS) is not None
        node.handle_command({"req_id": "c", "target": "*", "cmd": "set_center_freq", "args": {"hz": 916e6}})
        bad = node.handle_command({"req_id": "s0", "target": "*", "cmd": "sweep", "args": {"bogus": 1}})
        assert bad["ok"] is False and "bilinmeyen anahtar" in bad["error"]
        a = node.handle_command({"req_id": "s1", "target": "*", "cmd": "sweep",
                                 "args": {"start_hz": 900e6, "stop_hz": 930e6, "publish_max_points": 2000}})
        assert a["ok"], a
        r = a["result"]
        assert r["n_detections"] >= 1 and r["duration_s"] > 0
        assert r["rate_hz_per_s"] == pytest.approx(30e6 / r["duration_s"], rel=1e-6)
        sw = bus.wait(lambda m: m.topic == T.RX_SWEEP)
        assert sw is not None
        validate_message(sw.topic, sw.env)
        d = sw.data
        assert d["f_start_hz"] == 900e6 and d["f_stop_hz"] == 930e6 and len(d["values"]) <= 2000
        assert any(abs(x["f_peak_hz"] - F_CW) < 2 * d["rbw_hz"] for x in d["detections"])
        # RX settings restored: app.rx + the fc changed over ctl; pipeline streaming again
        dev = node.device
        assert (dev.fc, dev.fs, dev.bw, dev.gains) == (916e6, FS, FS, [GAIN])
        assert node.state == "streaming"
        n0 = node.stats["frames"]
        assert bus.wait(lambda m: m.topic == T.RX_DETECTIONS and m.data["fc_hz"] > 915.5e6) is not None
        assert node.stats["frames"] > n0 and node.stats["sweeps"] == 1
    finally:
        node.stop()


@pytest.mark.realtime
def test_soft_agc_reduces_gain_on_clipping_emitter(bus, tmp_path):
    strong = [{"name": "jam", "kind": "cw", "freq_hz": F_CW, "power_dbm": -25.0}]   # +20 dBFS at 50 dB: clips
    app = make_app(True, strong, gain_mode="soft_agc", tmp_cal=str(tmp_path))
    app.agc = AgcConfig()
    node = RxNode(app, publisher=bus.pub)
    assert node.gain_mode == "soft_agc" and node.device.gain_mode == "manual"
    node.start()
    try:
        st = bus.wait(lambda m: m.topic == T.RX_STATUS and m.data["gain_db"][0] < GAIN - 20
                      and m.data["levels"]["peak_dbfs"] is not None and -35.0 <= m.data["levels"]["peak_dbfs"] <= -3.0,
                      timeout_s=15.0)
        assert st is not None, node.status_payload()
        validate_message(st.topic, st.env)
        assert st.data["gain_mode"] == "soft_agc" and st.data["levels"]["clip_fraction"] == 0.0
        g = node.device.gains[0]
        # control law: peak lands near target −10 dBFS → gain ≈ G0 + (−10 − (P + G0 − 5)) with P = −25 dBm
        assert g == pytest.approx(-10.0 - (-25.0 - 5.0), abs=8.0)
        a = node.handle_command({"req_id": "m", "target": "*", "cmd": "set_gain_mode", "args": {"mode": "manual"}})
        assert a["ok"] and node.gain_mode == "manual" and a["result"]["gain_db"] == [g]
    finally:
        node.stop()


# --------------------------------------------------------------------------- lifecycle
def test_clean_stop_threads_and_passed_device_kept_open(bus, tmp_path):
    baseline = threading.active_count()
    app = make_app(False, tmp_cal=str(tmp_path), bus=BusConfig(
        ctl_endpoints=[f"tcp://127.0.0.1:{free_port()}"], status_period_s=0.2))
    dev = open_device(app.devices[0], app)
    try:
        node = RxNode(app, device=dev, publisher=bus.pub)
        node.start()
        assert bus.wait(lambda m: m.topic == T.RX_DETECTIONS) is not None
        assert threading.active_count() >= baseline + 4          # reader, proc, status, ctl
        node.stop()
        node.stop()                                               # idempotent
        assert threading.active_count() == baseline
        assert not node.running and node.state == "idle"
        with pytest.raises(EDSError):
            node.start()
        assert not dev.streaming
        blk = dev.capture(4096)                                   # passed-in device: still open
        assert blk.n == 4096
        assert not bus.pub.closed                                 # passed-in publisher: still open
    finally:
        dev.close()


def test_stop_command_stops_node_and_run_returns(bus, tmp_path):
    node = RxNode(make_app(False, tmp_cal=str(tmp_path)), publisher=bus.pub)
    t = threading.Thread(target=node.run, kwargs={"duration_s": 30.0})
    t.start()
    try:
        assert bus.wait(lambda m: m.topic == T.RX_DETECTIONS) is not None
        a = node.handle_command({"req_id": "q", "target": "rx0", "cmd": "stop"})
        assert a["ok"] and a["result"]["state"] == "stopping"
        t.join(10.0)
        assert not t.is_alive() and not node.running
    finally:
        node.stop()
        t.join(10.0)


def test_run_with_duration_and_profile_violation(tmp_path):
    app = make_app(False, tmp_cal=str(tmp_path))
    app.rx.center_freq_hz = 5e9
    pv_pub = Publisher("inproc://eds-node-test-pv", src="rx0")
    try:
        with pytest.raises(ProfileViolation):
            RxNode(app, publisher=pv_pub)
        assert not pv_pub.closed
    finally:
        pv_pub.close()
    b = Bus()
    try:
        node = RxNode(make_app(False, tmp_cal=str(tmp_path)), publisher=b.pub)
        t0 = time.monotonic()
        node.run(0.5)
        assert 0.4 <= time.monotonic() - t0 < 10.0 and not node.running
        assert node.stats["frames"] > 0 and node.stats["status_msgs"] >= 1
    finally:
        b.close()


def test_stream_failure_enters_error_state_and_recovers(bus, tmp_path, monkeypatch):
    import eds.rx.node as node_mod
    from eds.core.errors import StreamError

    monkeypatch.setattr(node_mod, "RESTART_BACKOFF_S", 0.3)
    node = RxNode(make_app(False, tmp_cal=str(tmp_path)), publisher=bus.pub)
    dev = node.device
    orig = dev.read
    fail = {"n": 0}

    def flaky(n=None, timeout_s=2.0):
        if fail["n"] > 0:
            fail["n"] -= 1
            raise StreamError("USB bağlantısı koptu (test)")
        return orig(n, timeout_s)

    monkeypatch.setattr(dev, "read", flaky)
    node.start()
    try:
        assert bus.wait(lambda m: m.topic == T.RX_DETECTIONS) is not None
        fail["n"] = 3                                    # three consecutive failed (re)starts
        err = bus.wait(lambda m: m.topic == T.RX_STATUS and m.data["state"] == "error")
        assert err is not None
        validate_message(err.topic, err.env)
        assert any("USB bağlantısı koptu" in w for w in err.data["warnings"])
        end = time.monotonic() + 15.0
        while fail["n"] > 0 and time.monotonic() < end:  # restarts with back-off use up the failures
            bus.collect(0.1)
        assert fail["n"] == 0
        t_fixed = time.time_ns()
        assert bus.wait(lambda m: m.topic == T.RX_DETECTIONS and m.data["t_acq_ns"] > t_fixed) is not None
        ok = bus.wait(lambda m: m.topic == T.RX_STATUS and m.data["state"] == "streaming")
        assert ok is not None and ok.data["stream"]["blocks"] > 0
        s = node.stats
        assert s["stream_errors"] == 3 and s["stream_restarts"] == 3 and s["state"] == "streaming"
    finally:
        node.stop()
