"""Process isolation (eds.hal.proc + factory placement): a device / signal source in a spawned
worker must behave exactly like the in-process one (samples, settling, overflow/timeout,
errors with their type), and the worker must never outlive its proxy."""

from __future__ import annotations

import dataclasses
import gc
import logging
import multiprocessing
import os
import pickle
import threading
import time

import numpy as np
import pytest

from eds.core.config import AppConfig, DeviceConfig, config_from_dict
from eds.core.errors import (
    ConfigError,
    DeviceError,
    DeviceNotFound,
    EDSError,
    NotSupported,
    ProfileViolation,
    StreamError,
    StreamTimeout,
)
from eds.hal import factory
from eds.hal.factory import get_sim_world, isolation_plan, open_device, open_devices, open_group
from eds.hal.group import DeviceGroup
from eds.hal.proc import (
    ProcessRxDevice,
    ProcessSignalSource,
    RemoteTraceback,
    app_from_plain,
    app_to_plain,
    pluto_tx_isolated,
)
from eds.hal.tx import open_source
from tests.hal.fakes.fake_proc import FakeSource, open_fake

FAKE = "tests.hal.fakes.fake_proc:open_fake"
SRC = "tests.hal.fakes.fake_proc:FakeSource"


# ----------------------------------------------------------------------------- helpers
def pid_gone(pid: int | None, timeout_s: float = 10.0) -> bool:
    """True once ``pid`` no longer exists at all (exited AND reaped: a zombie still answers)."""
    end = time.monotonic() + timeout_s
    while time.monotonic() < end:
        try:
            os.kill(int(pid), 0)
        except ProcessLookupError:
            return True
        time.sleep(0.02)
    return False


@pytest.fixture(autouse=True)
def no_leaked_workers():
    before = {p.pid for p in multiprocessing.active_children()}
    yield
    gc.collect()
    end = time.monotonic() + 10.0
    leaked = [p for p in multiprocessing.active_children() if p.pid not in before]
    while leaked and time.monotonic() < end:
        time.sleep(0.05)
        leaked = [p for p in multiprocessing.active_children() if p.pid not in before]
    assert not leaked, f"sızan alt süreçler: {leaked}"


@pytest.fixture
def opened():
    devs: list = []
    yield devs
    for d in devs:
        try:
            d.close()
        except EDSError:
            pass


def fake_cfg(name: str = "f0", **sim) -> DeviceConfig:
    return DeviceConfig(name=name, backend="sim", profile="ad9363_pluto", sim=sim, isolation="subprocess")


def sim_app(*, realtime: bool = False, emitters: list | None = None, ppm: float | None = None, **rx) -> AppConfig:
    rx_cfg = {"center_freq_hz": 915e6, "sample_rate_hz": 2e6, "bandwidth_hz": 2e6, "gain_db": 40.0,
              "buffer_size": 16384}
    rx_cfg.update(rx)
    return config_from_dict({
        "device": [{"name": "rx0", "backend": "sim",
                    "sim": {"realtime": realtime, **({} if ppm is None else {"ppm": ppm})}}],
        "rx": rx_cfg,
        "sim": {"emitters": emitters if emitters is not None else
                [{"name": "t", "freq_hz": 915.2e6, "power_dbm": -60.0}]},
    })


def sim_device(isolation: str, **kw):
    app = sim_app(**kw)
    dc = app.devices[0]
    dc.isolation = isolation
    return app, open_device(dc, app)


def tone_offset_hz(blk) -> float:
    x = blk.samples[0]
    spec = np.abs(np.fft.fftshift(np.fft.fft(x * np.hanning(x.size))))
    f = np.fft.fftshift(np.fft.fftfreq(x.size, 1.0 / blk.fs))
    return float(f[int(np.argmax(spec))])


def block_key(b, with_time: bool = True) -> tuple:
    return (b.index, b.seq, b.n, b.fc, b.fs, b.gain_db, b.overflow, b.settling, b.t_ns if with_time else 0,
            sorted(b.meta.items()))


def stats_key(st) -> tuple:
    d = dataclasses.asdict(st)
    d.pop("start_mono")
    d.pop("last_mono")
    return tuple(sorted(d.items()))


# ----------------------------------------------------------------------------- sim through the worker
def _sim_session(dev, app):
    out: dict = {"blocks": []}
    out["warn"] = dev.configure(app.rx)
    out["blocks"].append(dev.capture(32768))
    dev.set_gain(30.0)
    dev.set_center_freq(915.1e6)
    raw = dev.read(4096)                       # stale buffers right after the retune
    out["raw_settling"] = raw.settling
    out["blocks"].append(raw)
    out["blocks"].append(dev.capture(32768))
    out["tracking"] = dev.set_tracking(quadrature=False)
    out["blocks"].append(dev.capture(16384))
    out["ppm"] = dev.set_ppm_correction(5.0)
    out["blocks"].append(dev.capture(16384))
    out["sensors"] = dev.read_sensors()
    out["limits"] = dev.runtime_limits()
    out["state"] = (dev.fc, dev.fs, dev.bw, list(dev.gains), dev.gain_mode, dev.ppm_hw, dev.ppm_hw_via)
    out["stats"] = stats_key(dev.stats)
    info = dev.info.to_dict()
    info["extra"].pop("isolation", None)
    out["info"] = info
    return out


def test_sim_in_worker_is_bitwise_identical_to_inproc(opened):
    app_a, a = sim_device("inproc")
    app_b, b = sim_device("subprocess")
    opened += [a, b]
    assert isinstance(b, ProcessRxDevice) and not isinstance(a, ProcessRxDevice)
    assert b.backend_name == "proc" and b.info.backend == "sim" and b.info.extra["isolation"] == "subprocess"
    assert b.pid != os.getpid() and b.alive
    ra, rb = _sim_session(a, app_a), _sim_session(b, app_b)
    assert ra["raw_settling"] and rb["raw_settling"]
    for x, y in zip(ra["blocks"], rb["blocks"]):
        assert block_key(x, with_time=False) == block_key(y, with_time=False)   # t_ns = wall clock
        assert x.samples.dtype == y.samples.dtype == np.complex64 and x.samples.flags.writeable
        np.testing.assert_array_equal(x.samples, y.samples)
    for key in ("warn", "tracking", "ppm", "sensors", "limits", "state", "stats", "info"):
        assert ra[key] == rb[key], key
    da, db = a.describe(), b.describe()
    assert db["sim"] == da["sim"] and db["isolation"]["class"] == "SimRxDevice"
    pid = b.pid
    b.close()
    assert pid_gone(pid)


def test_settling_after_retune_in_worker(opened):
    _, dev = sim_device("subprocess", ppm=0.0)              # no TCXO error: tone exactly at +200 kHz
    opened.append(dev)
    dev.configure(sim_app().rx)
    first = dev.capture(16384)
    assert not first.settling and tone_offset_hz(first) == pytest.approx(200e3, abs=300)
    dev.set_center_freq(915.1e6)
    assert dev.read(16384).settling           # old-LO samples are flagged ...
    blk = dev.capture(16384)                  # ... and capture() skips them
    assert not blk.settling and blk.fc == 915.1e6
    assert tone_offset_hz(blk) == pytest.approx(100e3, abs=300)


@pytest.mark.realtime
def test_realtime_sim_overflow_crosses_the_worker(opened):
    _, dev = sim_device("subprocess", realtime=True, sample_rate_hz=4e6, bandwidth_hz=4e6)
    opened.append(dev)
    dev.configure(sim_app(sample_rate_hz=4e6, bandwidth_hz=4e6).rx)
    dev.start_stream()
    dev.read(16384)
    time.sleep(0.15)                          # FIFO = 4 x 16384 samples = 16 ms at 4 MS/s
    blk = dev.read(16384)
    assert blk.overflow and dev.stats.overflows >= 1
    assert dev.describe()["sim"]["overflow_events"] >= 1


# ----------------------------------------------------------------------------- read semantics (scripted)
SCRIPT = [
    {"k": 100, "hw_time_ns": 111, "t_ns": 1_000_000_000},
    {"k": 0, "overflow": True, "t_ns": 1_100_000_000},
    {"k": 300, "hw_time_ns": 222, "t_ns": 1_200_000_000},
    {"k": 100, "t_ns": 1_300_000_000},
    {"timeout": True, "t_ns": 1_400_000_000},
    {"k": 50, "overflow": True, "t_ns": 1_500_000_000},
    {"k": 14, "t_ns": 1_600_000_000},
    {"error": "StreamError"},
    {"k": 64, "t_ns": 1_700_000_000},
]


def _scripted(dev) -> list:
    dev.set_sample_rate(1e6)
    dev.start_stream(256)
    out: list = []
    for n in (400, 64, 64, 64, 64):
        try:
            b = dev.read(n, timeout_s=0.5)
            out.append((block_key(b), b.samples.copy()))
        except EDSError as e:
            out.append((type(e), str(e)))
    out.append(stats_key(dev.stats))
    return out


def test_overflow_timeout_and_errors_match_inproc(opened):
    cfg = fake_cfg(script=SCRIPT)
    a, b = open_fake(cfg), ProcessRxDevice(cfg, opener=FAKE)
    opened += [a, b]
    ra, rb = _scripted(a), _scripted(b)
    assert len(ra) == len(rb)
    for x, y in zip(ra, rb):
        if isinstance(x, tuple) and len(x) == 2 and isinstance(x[1], np.ndarray):
            assert x[0] == y[0]
            np.testing.assert_array_equal(x[1], y[1])
        else:
            assert x == y
    blk = rb[0][0]
    assert blk[6] is True                                   # overflow before the block
    assert dict(blk[9])["hw_time_ns"] == 222                # hw time of the first kept sample
    assert blk[8] == 1_300_000_000 - 400_000                # t_ns = last read time - n/fs
    assert rb[1] == (StreamTimeout, "f0: 0.50 s içinde örnek gelmedi")
    assert rb[3][0] is StreamError
    st = b.stats
    assert (st.overflows, st.timeouts, st.discarded_samples) == (2, 1, 100)
    assert b.describe()["fake"]["started_with"] == 256


def test_errors_keep_type_message_and_child_traceback(opened):
    # open failures: same class and message as in-process
    bad = {"device": [{"name": "rx0", "backend": "sim", "channels": [0, 1], "sim": {"realtime": False}}]}
    with pytest.raises(ConfigError) as e_in:
        open_device(config_from_dict(bad).devices[0], config_from_dict(bad))
    app = config_from_dict(bad)
    app.devices[0].isolation = "subprocess"
    with pytest.raises(ConfigError) as e_sub:
        open_device(app.devices[0], app)
    assert type(e_sub.value) is type(e_in.value) and str(e_sub.value) == str(e_in.value)
    assert "SimRxDevice" in e_sub.value.child_traceback and isinstance(e_sub.value.__cause__, RemoteTraceback)

    with pytest.raises(DeviceNotFound, match="sahte cihaz bulunamadı"):
        ProcessRxDevice(fake_cfg(open_error="DeviceNotFound"), opener=FAKE)
    with pytest.raises(ProfileViolation) as pv:
        ProcessRxDevice(fake_cfg(open_violation=["fc aralık dışı", "fs çok yüksek"]), opener=FAKE)
    assert pv.value.violations == ["fc aralık dışı", "fs çok yüksek"]
    with pytest.raises(ConfigError, match="RxDevice döndürmedi"):
        ProcessRxDevice(fake_cfg(open_wrong_type=True), opener=FAKE)

    dev = ProcessRxDevice(fake_cfg(fail_fc=1e9), opener=FAKE)
    opened.append(dev)
    with pytest.raises(DeviceError) as e:
        dev.set_center_freq(1e9)
    assert type(e.value) is DeviceError and str(e.value) == "f0: PLL kilitlenmedi (1000000000 Hz)"
    assert "_apply_center_freq" in e.value.child_traceback
    with pytest.raises(ConfigError, match="aralık dışı"):
        dev.set_ppm_correction(500.0)
    with pytest.raises(DeviceError, match="KeyError"):      # non-EDS -> DeviceError naming the type
        dev.set_ppm_correction(999)
    assert dev.set_center_freq(915e6) == 915e6              # the worker survives remote errors

    _, sim = sim_device("subprocess")
    opened.append(sim)
    with pytest.raises(ConfigError) as e_ppm:
        sim.set_ppm_correction(1e6)
    _, sim_in = sim_device("inproc")
    opened.append(sim_in)
    with pytest.raises(ConfigError) as e_ppm_in:
        sim_in.set_ppm_correction(1e6)
    assert str(e_ppm.value) == str(e_ppm_in.value)


def test_state_warnings_agc_limits_and_sensors_are_mirrored(opened):
    dev = ProcessRxDevice(fake_cfg(), opener=FAKE)
    opened.append(dev)
    dev.set_center_freq(2.4e9)
    assert dev.set_gain(70.0) == 60.0                       # runtime gain limit of the worker
    dev.set_bandwidth(1e6)
    assert "BW 1000000 uyarısı" in dev.warnings
    assert dev.set_ppm_correction(3.5) == "hardware"
    assert (dev.ppm_hw, dev.ppm_hw_via) == (3.5, "hardware") and dev.info.extra["xo"] == "+3.50"
    assert dev.set_tracking(dc=False, quadrature=True) == {"dc": "fake", "quadrature": "fake"}
    assert dev._settle_until > dev._index                   # settling marked by the proxy
    sens = dev.read_sensors()
    assert sens["temperature_c"] == 41.5 and sens["pid"] == float(dev.pid) != os.getpid()
    dev.set_sample_rate(1e6)
    dev.set_gain_mode("hw_slow")
    dev.start_stream(1024)
    blk = dev.read(256)
    assert dev.gains == [12.5] and blk.gain_db == (12.5,)  # hardware AGC gain from the worker
    assert dev.info.serial == "FAKE-f0" and dev.info.driver == "fake"


def test_control_is_not_blocked_by_a_pending_read(opened):
    dev = ProcessRxDevice(fake_cfg(script=[{"sleep": 1.5, "k": 32}]), opener=FAKE)
    opened.append(dev)
    dev.set_sample_rate(1e6)
    dev.start_stream(64)
    got: list = []
    th = threading.Thread(target=lambda: got.append(dev.read(32, timeout_s=3.0)))
    th.start()
    time.sleep(0.2)
    t0 = time.monotonic()
    dev.set_center_freq(1e9)                                 # separate control pipe
    assert time.monotonic() - t0 < 0.8
    th.join(5.0)
    assert got and got[0].n == 32


def test_interrupted_wait_keeps_requests_and_replies_in_step(opened, monkeypatch):
    import eds.hal.proc as proc_mod

    dev = ProcessRxDevice(fake_cfg(), opener=FAKE)
    opened.append(dev)
    real_wait = proc_mod._Pipe.wait
    hits = {"n": 1}

    def ctrl_c_once(self, wait_s):
        if hits["n"]:
            hits["n"] -= 1
            raise KeyboardInterrupt
        return real_wait(self, wait_s)

    monkeypatch.setattr(proc_mod._Pipe, "wait", ctrl_c_once)
    with pytest.raises(KeyboardInterrupt):
        dev.set_center_freq(1e9)              # done in the worker, its reply left in the pipe
    assert dev.set_center_freq(2e9) == 2e9 and dev.fc == 2e9   # not the stale 1e9 reply
    dev.set_sample_rate(1e6)
    dev.start_stream(64)
    assert dev.read(32).samples[0, 0] == 0    # ramp: value = absolute sample index
    hits["n"] = 1
    with pytest.raises(KeyboardInterrupt):
        dev.read(32)                          # samples 32..63 reach the pipe but are dropped
    blk = dev.read(32)
    assert blk.overflow and blk.samples[0, 0] == 64 and dev.stats.overflows == 1


def test_interruption_inside_a_message_breaks_the_proxy_cleanly():
    dev = ProcessRxDevice(fake_cfg(), opener=FAKE)
    pid = dev.pid
    conn = dev._ctl.conn
    real_recv = conn.recv

    def torn(*a):
        real_recv(*a)
        raise KeyboardInterrupt               # as if interrupted halfway through the reply

    conn.recv = torn
    with pytest.raises(KeyboardInterrupt):
        dev.set_center_freq(1e9)
    conn.recv = real_recv
    with pytest.raises(DeviceError, match="iletişim bozuldu"):
        dev.set_center_freq(2e9)
    dev.close()                               # EOF -> the worker closes its device and exits
    assert pid_gone(pid)


# ----------------------------------------------------------------------------- lifecycle
def test_close_terminates_and_reaps_the_worker():
    dev = ProcessRxDevice(fake_cfg(), opener=FAKE)
    pid = dev.pid
    assert not pid_gone(pid, 0.1)
    dev.start_stream(64)
    dev.close()
    assert pid_gone(pid) and not dev.alive
    dev.close()                                              # idempotent
    with pytest.raises(DeviceError, match="kapalı"):
        dev.set_center_freq(1e9)
    with pytest.raises(StreamError):
        dev._read_raw(16, 0.1)
    assert dev.describe()["isolation"]["alive"] is False


def test_close_error_of_the_backend_propagates_and_still_reaps():
    dev = ProcessRxDevice(fake_cfg(close_error=True), opener=FAKE)
    pid = dev.pid
    with pytest.raises(DeviceError, match="kapatma hatası"):
        dev.close()
    assert pid_gone(pid)


def test_garbage_collected_proxy_shuts_its_worker_down():
    dev = ProcessRxDevice(fake_cfg(), opener=FAKE)
    pid = dev.pid
    del dev
    gc.collect()
    assert pid_gone(pid)


def test_worker_death_raises_device_error_and_close_only_reaps():
    dev = ProcessRxDevice(fake_cfg(crash_fc=1e9), opener=FAKE)
    pid = dev.pid
    with pytest.raises(DeviceError, match="beklenmedik şekilde sonlandı.*çıkış kodu 3"):
        dev.set_center_freq(1e9)
    t0 = time.monotonic()
    with pytest.raises(DeviceError, match="sonlandı"):
        dev.set_gain(10.0)
    with pytest.raises(StreamError):
        dev._read_raw(16, 0.1)
    assert time.monotonic() - t0 < 1.0
    dev.close()                                              # no raise: nothing left to close
    assert pid_gone(pid)


def test_worker_death_during_read_is_a_stream_error():
    dev = ProcessRxDevice(fake_cfg(script=[{"k": 8}, {"exit": 5}]), opener=FAKE)
    pid = dev.pid
    dev.set_sample_rate(1e6)
    dev.start_stream(64)
    with pytest.raises(StreamError, match="çıkış kodu 5"):
        dev.read(64)
    dev.close()
    assert pid_gone(pid)


def test_hung_worker_is_killed_after_the_rpc_deadline():
    dev = ProcessRxDevice(fake_cfg(hang_bw=3e6), opener=FAKE)
    pid = dev.pid
    dev.rpc_timeout_s = 0.5
    t0 = time.monotonic()
    with pytest.raises(DeviceError, match="yanıt vermedi"):
        dev.set_bandwidth(3e6)
    assert time.monotonic() - t0 < 5.0
    dev.close()
    assert pid_gone(pid)


# ----------------------------------------------------------------------------- throughput
def test_pipe_throughput_at_least_20_msps(opened, record_property):
    dev = ProcessRxDevice(fake_cfg(fixed=True), opener=FAKE)
    opened.append(dev)
    dev.set_sample_rate(20e6)
    n = 65536
    dev.start_stream(n)
    dev.read(n)
    best = 0.0
    for _ in range(3):
        blocks = 64                                          # 4 Mi samples = 32 MiB per run
        t0 = time.perf_counter()
        for _ in range(blocks):
            blk = dev.read(n)
        dt = time.perf_counter() - t0
        best = max(best, blocks * n / dt)
    assert blk.samples.shape == (1, n) and blk.samples[0, 5] == 5
    record_property("proc_throughput_msps", round(best / 1e6, 1))
    print(f"\nProcessRxDevice boru hattı: {best / 1e6:.1f} MS/s complex64 ({best * 8 / 1e6:.0f} MB/s)")
    assert best >= 20e6


# ----------------------------------------------------------------------------- factory placement
def _soapy(name: str, **kw) -> dict:
    d = {"name": name, "backend": "soapy", "profile": "ad9363_pluto"}
    d.update(kw)
    return d


PLUTO_A = {"driver": "plutosdr", "uri": "usb:1.5.5"}
PLUTO_B = {"driver": "plutosdr", "uri": "usb:1.6.5"}


@pytest.mark.parametrize("raw, expected", [
    # 2x Pluto DF group -> both in workers
    ({"device": [_soapy("rx0", args=PLUTO_A), _soapy("rx1", args=PLUTO_B)]},
     {"rx0": "subprocess", "rx1": "subprocess"}),
    # profile soapy_driver = plutosdr counts as a Pluto even without args.driver
    ({"device": [_soapy("rx0", args={"uri": "usb:1.5.5"}), _soapy("rx1", args=PLUTO_B)]},
     {"rx0": "subprocess", "rx1": "subprocess"}),
    # Pluto DUT + pluto_tx source: the SOURCE is isolated (open_source), the DUT stays here
    ({"device": [_soapy("rx0", args=PLUTO_A)],
      "measure": {"source": "pluto_tx", "source_args": {"args": {"driver": "plutosdr", "uri": "ip:192.168.3.1"}}}},
     {"rx0": "inproc"}),
    # ... unless the source opts out of isolation: then the DUT moves to a worker
    ({"device": [_soapy("rx0", args=PLUTO_A)],
      "measure": {"source": "pluto_tx", "source_args": {"isolate": False}}},
     {"rx0": "subprocess"}),
    # a single Pluto, sim devices, non-Pluto radios stay in-process
    ({"device": [_soapy("rx0", args=PLUTO_A)]}, {"rx0": "inproc"}),
    ({"device": [{"name": "a", "backend": "sim"}, {"name": "b", "backend": "sim"}]}, {"a": "inproc", "b": "inproc"}),
    ({"device": [_soapy("rx0", args=PLUTO_A), _soapy("hrf", profile="ad9363_pluto", args={"driver": "hackrf"})]},
     {"rx0": "inproc", "hrf": "inproc"}),
    # forced choices are honoured; one forced inproc leaves the other auto Pluto in a worker
    ({"device": [_soapy("rx0", args=PLUTO_A, isolation="inproc"), _soapy("rx1", args=PLUTO_B)]},
     {"rx0": "inproc", "rx1": "subprocess"}),
    ({"device": [{"name": "a", "backend": "sim", "isolation": "subprocess"}, _soapy("rx0", args=PLUTO_A)]},
     {"a": "subprocess", "rx0": "inproc"}),
])
def test_isolation_plan_decision_table(raw, expected):
    assert isolation_plan(config_from_dict(raw)) == expected


def test_isolation_plan_warns_when_two_plutos_are_forced_inproc(caplog):
    raw = {"device": [_soapy("rx0", args=PLUTO_A, isolation="inproc"), _soapy("rx1", args=PLUTO_B, isolation="inproc")]}
    with caplog.at_level(logging.WARNING, logger="eds.hal.factory"):
        assert isolation_plan(config_from_dict(raw)) == {"rx0": "inproc", "rx1": "inproc"}
    assert "TEK libiio" in caplog.text
    assert pluto_tx_isolated(None) and pluto_tx_isolated({"isolate": "evet"}) and not pluto_tx_isolated({"isolate": 0})
    with pytest.raises(ConfigError):
        pluto_tx_isolated({"isolate": "belki"})


def test_open_devices_places_two_plutos_in_workers(monkeypatch):
    made: list = []

    class Recorder:
        def __init__(self, dev, app):
            made.append((dev.name, dev.isolation, app))
            self.name = dev.name

        def close(self):
            pass

    import eds.hal.proc as proc_mod

    monkeypatch.setattr(proc_mod, "ProcessRxDevice", Recorder)
    app = config_from_dict({"device": [_soapy("rx0", args=PLUTO_A), _soapy("rx1", args=PLUTO_B)]})
    devs = open_devices(app)
    assert [m[0] for m in made] == ["rx0", "rx1"] and all(m[2] is app for m in made)
    assert all(isinstance(d, Recorder) for d in devs)
    made.clear()
    assert isinstance(open_device(dataclasses.replace(app.devices[0], isolation="subprocess"), app), Recorder)
    assert made and made[0][0] == "rx0"


def test_open_group_of_forced_worker_sims(opened):
    app = config_from_dict({
        "device": [{"name": "a", "backend": "sim", "isolation": "subprocess", "sim": {"realtime": False}},
                   {"name": "b", "backend": "sim", "isolation": "subprocess", "sim": {"realtime": False}}],
        "rx": {"sample_rate_hz": 2e6, "bandwidth_hz": 2e6, "buffer_size": 8192},
    })
    grp = open_group(app)
    opened.append(grp)
    assert isinstance(grp, DeviceGroup) and all(isinstance(m, ProcessRxDevice) for m in grp.members)
    assert "_eds_sim_world" not in app.__dict__              # nothing simulated in the parent
    grp.configure(app.rx)
    blk = grp.capture(8192)
    assert blk.samples.shape == (2, 8192) and not blk.settling
    pids = [m.pid for m in grp.members]
    grp.close()
    assert all(pid_gone(p) for p in pids)


def test_plain_app_roundtrip_drops_the_attached_world():
    app = sim_app()
    get_sim_world(app)
    plain = app_to_plain(app)
    pickle.dumps(plain)                                      # no SimWorld / locks inside
    back = app_from_plain(pickle.loads(pickle.dumps(plain)))
    assert repr(back.to_dict()) == repr(app.to_dict())      # repr: cal.ppm_override is NaN
    assert "_eds_sim_world" not in back.__dict__
    assert isinstance(back.devices[0], DeviceConfig)


def test_factory_rejects_unknown_isolation():
    dc = DeviceConfig(name="x", isolation="thread")
    with pytest.raises(ConfigError, match="isolation"):
        factory.open_device(dc)


# ----------------------------------------------------------------------------- signal source
def test_process_signal_source_forwards_and_mirrors(tmp_path):
    marker = tmp_path / "closed.txt"
    src = ProcessSignalSource(SRC, kwargs={"marker": str(marker)})
    pid = src.pid
    assert pid != os.getpid()
    assert (src.name, src.absolute_power, src.max_tones) == ("fake_src", True, 2)
    assert src.power_range_db == (-100.0, -10.0)
    assert src.set_frequency(1e9) == 1e9 + 1.0
    assert src.freq_range_hz == (1e9 - 1e6, 1e9 + 1e6)       # mirrored after every call
    assert src.set_power(-50.0) == -50.0
    assert src.set_lo_offset(250e3) == 250e3
    src.set_tones([1e9, 1.001e9], [-60.0, -60.0])
    t = src.set_enabled(True)
    assert abs(t - time.time_ns()) < 5e9                     # wall clock is shared between processes
    with pytest.raises(ConfigError, match="üst sınırın"):
        src.set_power(0.0)
    assert src.pid == pid and src.lo_offset == 250e3          # public attribute fetched on access
    assert src.history()[0] == ("set_frequency", 1e9)         # public method forwarded
    with pytest.raises(AttributeError):
        _ = src.no_such_attribute
    assert getattr(src, "no_such_attribute", "yok") == "yok"
    d = src.describe()
    assert d["freq"] == 1e9 and d["pid"] == pid and d["isolation"]["mode"] == "subprocess"
    with src:
        pass                                                 # __exit__: RF off, then close
    assert pid_gone(pid)
    assert marker.read_text(encoding="utf-8") == "closed enabled=False"
    assert isinstance(src.set_enabled(False), int)
    with pytest.raises(StreamError):
        src.set_enabled(True)
    src.close()


def test_process_signal_source_errors_and_not_supported():
    with pytest.raises(DeviceNotFound, match="sahte kaynak") as e:
        ProcessSignalSource(FakeSource, kwargs={"fail_open": True})
    assert "fake_proc.py" in e.value.child_traceback and "__init__" in e.value.child_traceback
    src = ProcessSignalSource("tests.hal.fakes.fake_proc:FakeSourceNoLo")
    try:
        with pytest.raises(NotSupported, match="LO ofseti"):
            src.set_lo_offset(1e5)
    finally:
        src.close()
    with pytest.raises(ConfigError):
        ProcessSignalSource(lambda: None)                    # not importable by the worker


def test_orphaned_source_worker_turns_rf_off_and_closes(tmp_path):
    marker = tmp_path / "closed.txt"
    src = ProcessSignalSource(SRC, kwargs={"marker": str(marker)})
    pid = src.pid
    src.set_frequency(1e9)
    src.set_enabled(True)
    del src                                                  # never closed: finalizer -> EOF in the worker
    gc.collect()
    assert pid_gone(pid)
    assert marker.read_text(encoding="utf-8") == "closed enabled=False"


def test_open_source_pluto_tx_is_isolated_by_default(monkeypatch):
    made: list = []

    class Recorder:
        def __init__(self, target, args=(), kwargs=None):
            made.append((target, dict(kwargs or {})))

    import eds.hal.proc as proc_mod

    monkeypatch.setattr(proc_mod, "ProcessSignalSource", Recorder)
    s = open_source("pluto_tx", {"fs": 1e6, "isolate": True, "args": {"driver": "plutosdr"}})
    assert isinstance(s, Recorder)
    assert made == [("eds.hal.tx_hw:SoapyTxSource", {"fs": 1e6, "args": {"driver": "plutosdr"}})]


def test_open_source_pluto_tx_errors_come_from_the_worker():
    # Without SoapySDR the worker raises BackendUnavailable, with it DeviceNotFound: either way
    # the error crossed the process boundary with its EDS type and the worker's traceback.
    with pytest.raises(DeviceError) as e:
        open_source("pluto_tx", {"args": {"driver": "eds_test_no_such_driver"}})
    assert "SoapyTxSource" in e.value.child_traceback or "import_soapy" in e.value.child_traceback
