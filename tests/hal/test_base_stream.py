"""RxDevice.read() contract (eds.hal.base): labelling vs concurrent settings changes, timeout
gaps, uncertain overflow spans and the channel-count check — on a minimal scripted backend."""

from __future__ import annotations

import threading
import time
from collections import deque

import numpy as np
import pytest

from eds.core.config import DeviceConfig
from eds.core.errors import StreamError, StreamTimeout
from eds.core.profile import load_profile
from eds.hal.base import RawRead, RxDevice


class Probe(RxDevice):
    """Samples carry the 'hardware' LO in MHz at the time they are read; ``script`` entries
    (RawRead or int k) override the next ``_read_raw`` results."""

    backend_name = "probe"

    def __init__(self) -> None:
        super().__init__(DeviceConfig(name="p", backend="sim", channels=[0]), load_profile("ad9363_pluto"))
        self.hw_fc = 1000e6
        self.during_apply = None
        self.script: deque = deque()

    def _apply_sample_rate(self, hz):
        return hz

    def _apply_bandwidth(self, hz):
        return hz

    def _apply_center_freq(self, hz):
        self.hw_fc = hz
        if self.during_apply is not None:
            self.during_apply()
        return hz

    def _apply_gain(self, db, ch):
        return db

    def _apply_gain_mode(self, mode):
        pass

    def _start(self, buffer_size):
        pass

    def _stop(self):
        pass

    def _close(self):
        pass

    def _read_raw(self, n, timeout_s):
        if self.script:
            r = self.script.popleft()
            if isinstance(r, RawRead):
                return r
            n = min(n, int(r))
        return RawRead(samples=np.full((1, n), self.hw_fc / 1e6, dtype=np.complex64), t_ns=time.time_ns())


@pytest.fixture
def dev():
    d = Probe()
    d.fs, d.buffer_size, d.settle_buffers = 1e6, 64, 1
    d.set_center_freq(1000e6)
    d.start_stream()
    d.flush()
    return d


def test_block_read_during_a_retune_is_settling_or_correctly_labelled(dev):
    """The reader returns while the tuner is between the hardware change and its bookkeeping
    (finding STR-05): the block must not carry new-LO samples under the old label unflagged."""
    blocks: list = []

    def read_in_between():
        t = threading.Thread(target=lambda: blocks.append(dev.read(64)))
        t.start()
        t.join(0.3)            # with the fix the reader waits for the tuner's lock instead
        blocks.append(t)

    dev.during_apply = read_in_between
    dev.set_center_freq(1010e6)
    dev.during_apply = None
    t = blocks.pop()
    t.join(5.0)
    (blk,) = blocks
    assert blk.settling or abs(float(blk.samples[0, 0].real) - blk.fc / 1e6) < 1e-6
    assert blk.settling and blk.fc == 1010e6


def test_concurrent_retunes_never_mislabel_settled_blocks(dev):
    stop = threading.Event()
    bad: list = []

    def reader():
        while not stop.is_set():
            b = dev.read(64)
            if not b.settling and abs(float(b.samples[0, 0].real) - b.fc / 1e6) > 1e-3:
                bad.append(b.index)

    def tuner():
        f = [1000e6, 1010e6]
        i = 0
        while not stop.is_set():
            i ^= 1
            dev.set_center_freq(f[i])
            time.sleep(0.0002)

    th = [threading.Thread(target=reader), threading.Thread(target=tuner)]
    for t in th:
        t.start()
    time.sleep(0.5)
    stop.set()
    for t in th:
        t.join(5.0)
    assert bad == []


def test_timeout_is_a_gap_for_the_next_block(dev):
    """A StreamTimeout loses continuity (finding STR-08): partial samples are discarded and
    counted, the host index advances, and the next block says overflow (gap before it)."""
    i0 = dev._index
    dev.script.extend([40, RawRead(samples=np.empty((1, 0), np.complex64), timeout=True)])
    with pytest.raises(StreamTimeout):
        dev.read(64)
    assert dev.stats.timeouts == 1 and dev.stats.discarded_samples == 40 and dev._index == i0 + 40
    blk = dev.read(64)
    assert blk.overflow and blk.index == i0 + 40
    assert dev.stats.overflows == 0                         # not a driver-reported overflow event
    assert not dev.read(64).overflow


def test_uncertain_overflow_span_is_flagged_settling(dev):
    x = np.ones((1, 64), np.complex64)
    dev.script.append(RawRead(samples=x, overflow_before=True, uncertain_samples=200))
    blk = dev.read(64)
    assert blk.overflow and blk.settling and dev._settle_until == blk.index + 200
    flags = [dev.read(64).settling for _ in range(4)]
    assert flags == [True, True, True, False]               # 64+64+64 < 200 <= 256
    assert dev.stats.overflows == 1


def test_wrong_channel_count_from_backend_is_rejected(dev):
    dev.script.append(RawRead(samples=np.ones((0, 64), np.complex64)))
    with pytest.raises(StreamError, match="kanal"):
        dev.read(64)


def test_settings_change_marks_settling_before_the_hardware_call(dev):
    seen: list = []
    dev.during_apply = lambda: seen.append(dev._settle_until - dev._index)
    dev.set_center_freq(1020e6)
    assert seen == [dev.settle_samples]
