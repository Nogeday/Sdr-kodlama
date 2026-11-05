"""Tests of eds.rx.stream.RxStreamer (simulator + failure-injecting fake device)."""

from __future__ import annotations

import threading
import time

import numpy as np
import pytest

from eds.core.config import AppConfig, DeviceConfig, RxConfig, SimConfig
from eds.core.errors import ConfigError, DeviceError, StreamError, StreamTimeout
from eds.core.types import IQBlock, StreamStats
from eds.hal import open_device
from eds.rx import RxStreamer

# eds.hal.factory caches one SimWorld per id(AppConfig): keep configs alive so ids are unique.
_APPS: list[AppConfig] = []


def sim_device(realtime: bool = False, fs: float = 1e6, buffer_size: int = 4096, emitters=(), **sim):
    sim.setdefault("realtime", realtime)
    sim.setdefault("ppm", 0.0)
    app = AppConfig(devices=[DeviceConfig(name="rx0", backend="sim", sim=sim)],
                    sim=SimConfig(emitters=[dict(e) for e in emitters]))
    _APPS.append(app)
    dev = open_device(app.devices[0], app)
    dev.configure(RxConfig(center_freq_hz=915e6, sample_rate_hz=fs, bandwidth_hz=fs, gain_db=40.0,
                           buffer_size=buffer_size, block_size=buffer_size))
    return dev


def wait_until(pred, timeout_s: float = 5.0) -> bool:
    t_end = time.monotonic() + timeout_s
    while time.monotonic() < t_end:
        if pred():
            return True
        time.sleep(0.002)
    return pred()


class FakeDevice:
    """Minimal RxDevice stand-in; ``behaviour(dev, i, n, timeout_s)`` produces read ``i``."""

    def __init__(self, behaviour):
        self.name = "fake0"
        self.streaming = False
        self.stats = StreamStats()
        self.reads = 0
        self.starts = 0
        self.stops = 0
        self.unblock = threading.Event()
        self._behaviour = behaviour

    def start_stream(self, buffer_size: int | None = None) -> None:
        self.streaming = True
        self.starts += 1
        self.stats = StreamStats()

    def stop_stream(self) -> None:
        self.streaming = False
        self.stops += 1
        self.unblock.set()

    def read(self, n: int | None = None, timeout_s: float = 2.0) -> IQBlock:
        i = self.reads
        self.reads += 1
        return self._behaviour(self, i, n or 16, timeout_s)


def block(i: int, n: int) -> IQBlock:
    return IQBlock(samples=np.zeros((1, n), np.complex64), fs=1e6, fc=1e9, t_ns=i, index=i * n, seq=i,
                   gain_db=(0.0,))


def ok_reads(dev, i, n, timeout_s):
    time.sleep(0.0005)
    dev.stats.blocks += 1
    dev.stats.samples += n
    return block(i, n)


# --------------------------------------------------------------------------- queue policy
def test_full_queue_drops_the_oldest_blocks_and_keeps_the_freshest():
    dev = sim_device()
    s = RxStreamer(dev, block_size=4096, queue_blocks=4)
    s.start()
    assert s.running and dev.streaming
    assert wait_until(lambda: s.blocks_read >= 40)
    s.stop()
    assert not s.running and s.error is None
    assert not dev.streaming                      # the streamer started it, so it stops it
    n = s.blocks_read
    assert s.queued == 4
    assert s.host_drops == n - 4
    st = s.stats
    assert st.host_drops == n - 4 and st.blocks == n and st.samples == 4096 * n
    blocks = s.drain()
    assert [b.seq for b in blocks] == list(range(n - 4, n))     # the newest four, in order
    assert all(b.n == 4096 and b.samples.dtype == np.complex64 for b in blocks)
    assert [b.index for b in blocks] == [4096 * k for k in range(n - 4, n)]
    assert s.drain() == [] and s.get(0.01) is None


def test_slow_consumer_sees_fifo_order_and_every_block_is_accounted_for():
    dev = sim_device()
    got: list[int] = []
    with RxStreamer(dev, block_size=4096, queue_blocks=8) as s:
        for _ in range(30):
            b = s.get(2.0)
            assert b is not None
            got.append(b.seq)
            time.sleep(0.003)                     # consumer much slower than the reader
    rest = [b.seq for b in s.drain()]
    seqs = got + rest
    assert all(a < b for a, b in zip(seqs, seqs[1:]))           # FIFO, never an older block
    n = s.blocks_read
    assert len(got) + len(rest) + s.host_drops == n
    assert s.host_drops > 0
    # the missing sequence numbers are exactly the dropped blocks
    assert seqs[-1] == n - 1
    assert (seqs[-1] + 1) - len(seqs) == s.host_drops


def test_stats_is_a_copy_with_host_drops():
    dev = sim_device()
    s = RxStreamer(dev, block_size=4096, queue_blocks=2)
    s.start()
    assert wait_until(lambda: s.host_drops >= 3)
    s.stop()
    st = s.stats
    assert isinstance(st, StreamStats) and st is not dev.stats
    assert st.host_drops == s.host_drops and dev.stats.host_drops == 0
    st.blocks = -1
    assert dev.stats.blocks >= 0


def test_on_block_is_called_in_the_reader_thread_for_every_block():
    dev = sim_device()
    seen: list[tuple[int, str]] = []
    with RxStreamer(dev, block_size=2048, queue_blocks=4,
                    on_block=lambda b: seen.append((b.seq, threading.current_thread().name))) as s:
        assert wait_until(lambda: len(seen) >= 20)
    assert len(seen) == s.blocks_read
    assert [k for k, _ in seen] == list(range(len(seen)))
    assert all(name == "rx-stream-rx0" for _, name in seen)


def test_stop_is_prompt_and_the_streamer_can_be_restarted():
    dev = sim_device()
    s = RxStreamer(dev, block_size=4096)
    s.start()
    s.start()                                     # idempotent while running
    assert wait_until(lambda: s.blocks_read >= 5)
    t0 = time.perf_counter()
    s.stop()
    assert time.perf_counter() - t0 < 1.0
    assert not s.running and not dev.streaming
    s.stop()                                      # idempotent
    s.start()
    assert s.running and dev.streaming
    assert wait_until(lambda: s.blocks_read >= 5)
    s.stop()
    assert s.error is None


def test_streamer_does_not_stop_a_stream_it_did_not_start():
    dev = sim_device()
    dev.start_stream()
    with RxStreamer(dev, block_size=4096) as s:
        assert wait_until(lambda: s.blocks_read >= 3)
    assert dev.streaming
    dev.close()


def test_stop_from_the_on_block_callback():
    dev = sim_device()
    holder: dict[str, RxStreamer] = {}

    def cb(b: IQBlock) -> None:
        if b.seq == 4:
            holder["s"].stop()

    s = RxStreamer(dev, block_size=1024, on_block=cb)
    holder["s"] = s
    s.start()
    assert wait_until(lambda: not s.running, 3.0)
    assert wait_until(lambda: not dev.streaming, 3.0)
    s.stop()
    assert s.error is None and s.blocks_read == 5


# --------------------------------------------------------------------------- error handling
def test_consecutive_timeouts_put_the_streamer_in_error_state():
    def behaviour(dev, i, n, timeout_s):
        raise StreamTimeout("zaman aşımı")

    dev = FakeDevice(behaviour)
    s = RxStreamer(dev, block_size=16, read_timeout_s=0.05, max_consecutive_timeouts=3)
    s.start()
    assert wait_until(lambda: not s.running, 3.0)
    assert isinstance(s.error, StreamError) and isinstance(s.error.__cause__, StreamTimeout)
    assert s.timeouts == 3 and s.consecutive_timeouts == 3 and dev.reads == 3
    t0 = time.perf_counter()
    assert s.get(5.0) is None                     # reader dead: no waiting
    assert time.perf_counter() - t0 < 0.5
    with pytest.raises(StreamError):
        s.check()
    s.stop()
    assert dev.stops == 1 and not dev.streaming


def test_intermittent_timeouts_are_tolerated():
    def behaviour(dev, i, n, timeout_s):
        if i % 3 != 2:                            # two timeouts, then one good block
            raise StreamTimeout("zaman aşımı")
        return ok_reads(dev, i, n, timeout_s)

    dev = FakeDevice(behaviour)
    s = RxStreamer(dev, block_size=16, max_consecutive_timeouts=3)
    s.start()
    assert wait_until(lambda: s.blocks_read >= 10)
    s.stop()
    assert s.error is None
    s.check()
    assert s.timeouts >= 2 * s.blocks_read and s.consecutive_timeouts <= 2


def test_device_error_stops_the_reader_and_keeps_queued_blocks():
    def behaviour(dev, i, n, timeout_s):
        if i == 5:
            raise DeviceError("USB bağlantısı koptu")
        return ok_reads(dev, i, n, timeout_s)

    dev = FakeDevice(behaviour)
    s = RxStreamer(dev, block_size=16, queue_blocks=32)
    s.start()
    assert wait_until(lambda: not s.running, 3.0)
    assert isinstance(s.error, DeviceError)
    assert [s.get(0.1).seq for _ in range(5)] == [0, 1, 2, 3, 4]
    assert s.get(1.0) is None
    with pytest.raises(StreamError) as ei:
        s.check()
    assert isinstance(ei.value.__cause__, DeviceError)
    s.stop()
    # a restart clears the error state
    dev2 = FakeDevice(ok_reads)
    s2 = RxStreamer(dev2, block_size=16)
    s2.start()
    assert wait_until(lambda: s2.blocks_read >= 3)
    s2.stop()
    assert s2.error is None


def test_on_block_exception_is_reported():
    def cb(b: IQBlock) -> None:
        if b.seq == 2:
            raise ValueError("geri çağırma hatası")

    dev = FakeDevice(ok_reads)
    s = RxStreamer(dev, block_size=16, on_block=cb)
    s.start()
    assert wait_until(lambda: not s.running, 3.0)
    assert isinstance(s.error, ValueError) and s.blocks_read == 3
    s.stop()


def test_stop_never_hangs_on_a_stuck_driver_read():
    def behaviour(dev, i, n, timeout_s):
        if i < 2:
            return ok_reads(dev, i, n, timeout_s)
        dev.unblock.wait(10.0)                    # driver ignores the timeout ...
        raise StreamError("akış durduruldu")      # ... until the stream is stopped

    dev = FakeDevice(behaviour)
    s = RxStreamer(dev, block_size=16, read_timeout_s=0.1)
    s.start()
    assert wait_until(lambda: dev.reads >= 3)
    t0 = time.perf_counter()
    s.stop(timeout_s=0.2)
    assert time.perf_counter() - t0 < 2.0
    assert not s.running and dev.stops == 1
    assert s.error is None                        # the stop-induced exception is not an error
    assert s.queued == 2


@pytest.mark.parametrize("kw", [
    {"block_size": 0}, {"block_size": 1.5}, {"queue_blocks": 0}, {"read_timeout_s": 0.0},
    {"read_timeout_s": float("nan")}, {"max_consecutive_timeouts": 0}, {"on_block": 42},
])
def test_invalid_arguments_are_rejected(kw):
    args = {"block_size": 16}
    args.update(kw)
    with pytest.raises(ConfigError):
        RxStreamer(FakeDevice(ok_reads), **args)


# --------------------------------------------------------------------------- realtime
@pytest.mark.realtime
def test_realtime_stream_delivers_the_sample_rate_without_loss():
    fs = 2e6
    dev = sim_device(realtime=True, fs=fs, buffer_size=16384)
    n = 0
    with RxStreamer(dev, block_size=16384, queue_blocks=16) as s:
        t0 = time.perf_counter()
        while time.perf_counter() - t0 < 0.6:
            b = s.get(1.0)
            assert b is not None
            n += b.n
        elapsed = time.perf_counter() - t0
        st = s.stats
    # 2 MS/s x 4 B = 8 MB/s is far below the 24 MB/s USB model: no loss anywhere
    assert st.overflows == 0 and st.host_drops == 0 and s.error is None
    assert n / elapsed == pytest.approx(fs, rel=0.2)
