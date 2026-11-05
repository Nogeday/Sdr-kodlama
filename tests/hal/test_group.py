"""DeviceGroup (eds.hal.group) with a tiny deterministic fake RxDevice (not the simulator)."""

from __future__ import annotations

import dataclasses
import threading
from collections import deque

import numpy as np
import pytest

from eds.core.config import DeviceConfig, RxConfig
from eds.core.errors import ConfigError, StreamError, StreamTimeout
from eds.core.profile import load_profile
from eds.hal.base import RawRead, RxDevice
from eds.hal.group import DeviceGroup

PLUTO = load_profile("ad9363_pluto")


class FakeMember(RxDevice):
    """Ramp source: channel c of member value v delivers ``v + 1000·c + sample_index``."""

    backend_name = "fake"

    def __init__(self, name: str, value: float, *, t0_ns: int = 10**18, serial: str = "",
                 barrier: threading.Barrier | None = None, profile=PLUTO, limits=None):
        super().__init__(DeviceConfig(name=name, backend="sim", profile=profile.name, channels=[0]), profile)
        self.info.serial = serial or f"SN-{name}"
        self.value = float(value)
        self.t0_ns = int(t0_ns)
        self.barrier = barrier
        self.script: deque = deque()
        self.counter = 0
        self.read_threads: set[int] = set()
        self.started_with: int | None = None
        self.closed = False
        self.applied: list[tuple[str, float]] = []
        self.tracking: dict = {}
        self._limits = limits or {"gain_db": (0.0, 73.0), "freq_hz": (70e6, 6e9)}

    def runtime_limits(self):
        return dict(self._limits)

    def _apply_sample_rate(self, hz):
        self.applied.append(("fs", hz))
        return hz

    def _apply_bandwidth(self, hz):
        self.applied.append(("bw", hz))
        return hz

    def _apply_center_freq(self, hz):
        self.applied.append(("fc", hz))
        return hz

    def _apply_gain(self, db, channel):
        self.applied.append(("gain", db))
        return db

    def _apply_gain_mode(self, mode):
        self.applied.append(("mode", mode))

    def _start(self, buffer_size):
        self.started_with = buffer_size

    def _read_raw(self, n, timeout_s):
        self.read_threads.add(threading.get_ident())
        if self.barrier is not None:
            self.barrier.wait(timeout=2.0)
        ev = self.script.popleft() if self.script else None
        if isinstance(ev, RawRead):
            self.counter += ev.samples.shape[1]
            return ev
        empty = np.empty((1, 0), np.complex64)
        if ev == "timeout":
            return RawRead(empty, timeout=True)
        if ev == "overflow":
            self.counter += 100
            return RawRead(empty, overflow_before=True)
        if ev == "error":
            raise StreamError(f"{self.name}: boom")
        idx = np.arange(self.counter, self.counter + n, dtype=np.float64)
        self.counter += n
        t_ret = self.t0_ns + int(round(self.counter / self.fs * 1e9))
        return RawRead((self.value + idx).astype(np.complex64)[None, :], t_ns=t_ret)

    def _stop(self):
        pass

    def _close(self):
        self.closed = True

    def set_ppm_correction(self, ppm):
        self.ppm_hw, self.ppm_hw_via = float(ppm), "hardware"
        self.mark_settling()
        return "hardware"

    def set_tracking(self, dc=None, rf_dc=None, quadrature=None):
        self.tracking = {"dc": dc, "rf_dc": rf_dc, "quadrature": quadrature}
        # a Pluto has no Soapy DC-offset / IQ-balance API: thin layer ("iio") or nothing
        path = "iio" if self.name.endswith("A") else "unsupported"
        return {k: path for k, v in self.tracking.items() if v is not None}

    def read_sensors(self):
        return {"temperature_c": 40.0 + self.value / 1e6}


def rxcfg(**kw) -> RxConfig:
    base = dict(center_freq_hz=2.4e9, sample_rate_hz=2.4e6, bandwidth_hz=2e6, gain_db=40.0, buffer_size=256,
                settle_buffers=0)
    base.update(kw)
    return RxConfig(**base)


def make_group(*, barrier: bool = False, t_offset_ns: int = 0, **kw):
    bar = threading.Barrier(2) if barrier else None
    a = FakeMember("rxA", 1e6, barrier=bar)
    b = FakeMember("rxB", 2e6, barrier=bar, t0_ns=10**18 + t_offset_ns)
    return DeviceGroup([a, b], **kw), a, b


# ----------------------------------------------------------------------------- construction
def test_construction_identity_and_coherence():
    g, a, b = make_group()
    assert g.num_channels == 2 and g.name == "rxA+rxB" and g.backend_name == "group"
    assert g.info.backend == "group" and g.profile.name == "ad9363_pluto"
    assert g.cfg.channels == [0, 1] and g.members == [a, b]
    assert g.coherent is False
    assert g.info.extra["member_serials"] == "SN-rxA,SN-rxB"
    assert g.info.extra["rxB.serial"] == "SN-rxB" and g.info.serial == "SN-rxA+SN-rxB"
    g.close()
    g2, *_ = make_group(assume_coherent=True)
    assert g2.coherent is True
    g2.close()
    single = DeviceGroup([FakeMember("rxS", 0.0)])
    assert single.num_channels == 1 and single.coherent    # one 1-channel member is trivially coherent
    single.close()


def test_construction_errors():
    with pytest.raises(ConfigError):
        DeviceGroup([])
    a = FakeMember("rxA", 0.0)
    with pytest.raises(ConfigError):
        DeviceGroup([a, a])
    with pytest.raises(ConfigError):
        DeviceGroup([FakeMember("rx", 0.0), FakeMember("rx", 1.0)])
    other = dataclasses.replace(PLUTO, name="ad9364_pluto_ext")
    with pytest.raises(ConfigError, match="profil"):
        DeviceGroup([FakeMember("rxA", 0.0), FakeMember("rxB", 0.0, profile=other)])


# ----------------------------------------------------------------------------- configuration
def test_configure_fans_out_without_channel_violation():
    g, a, b = make_group()
    warns = g.configure(rxcfg(center_freq_hz=915e6, gain_db=73.0))   # 2 channels on a 1-ch profile: OK
    assert warns == []
    assert a.gains == b.gains == [73.0]
    g.set_center_freq(2.4e9)
    for m in (a, b):
        assert (m.fc, m.fs, m.bw) == (2.4e9, 2.4e6, 2e6)
        assert m.gains == [71.0]                             # band clamp at 2.4 GHz on every member
        assert m.buffer_size == 256 and m.settle_buffers == 0
    assert g.gains == [71.0, 71.0] and (g.fc, g.fs, g.bw) == (2.4e9, 2.4e6, 2e6)
    assert g.set_gain(30.0, channel=1) == 30.0
    assert a.gains == [71.0] and b.gains == [30.0]
    g.set_gain_mode("hw_slow")
    assert a.gain_mode == b.gain_mode == "hw_slow"
    g.close()


def test_configure_shared_usb_warning():
    g, *_ = make_group()
    warns = g.configure(rxcfg(sample_rate_hz=5e6, bandwidth_hz=5e6))   # 2×20 MB/s > 24 MB/s practical
    assert len(warns) == 1 and "USB" in warns[0] and "max_rate" in warns[0]
    g.close()


def test_runtime_limits_intersection():
    a = FakeMember("rxA", 0.0, limits={"gain_db": (0.0, 73.0), "freq_hz": (70e6, 6e9)})
    b = FakeMember("rxB", 0.0, limits={"gain_db": (-1.0, 70.0)})
    g = DeviceGroup([a, b])
    assert g.runtime_limits() == {"gain_db": (0.0, 70.0), "freq_hz": (70e6, 6e9)}
    assert g.gain_range(2.4e9).max_db == 70.0
    g.close()


# ----------------------------------------------------------------------------- streaming
def test_read_stacks_channels_concurrently_on_persistent_workers():
    g, a, b = make_group(barrier=True)                       # barrier(2): only passes if reads overlap
    g.configure(rxcfg())
    g.start_stream()
    assert a.started_with == b.started_with == 256
    blocks = [g.read(256, timeout_s=1.0) for _ in range(3)]
    blk = blocks[-1]
    assert blk.samples.shape == (2, 256) and blk.samples.dtype == np.complex64
    np.testing.assert_array_equal(blk.samples[0].real, 1e6 + np.arange(512, 768))
    np.testing.assert_array_equal(blk.samples[1].real, 2e6 + np.arange(512, 768))
    assert not any(b_.overflow for b_ in blocks)
    # one persistent worker thread per member, distinct, never the caller's thread
    assert len(a.read_threads) == 1 and len(b.read_threads) == 1
    assert a.read_threads != b.read_threads
    assert threading.get_ident() not in a.read_threads | b.read_threads
    assert len(g.worker_threads) == 2
    g.close()


def test_member_overflow_propagates():
    g, a, b = make_group()
    g.configure(rxcfg())
    g.start_stream()
    g.read(256)
    b.script.append("overflow")
    blk = g.read(256)
    assert blk.overflow and g.stats.overflows == 1 and b.stats.overflows == 1
    np.testing.assert_array_equal(blk.samples[1].real, 2e6 + np.arange(356, 612))   # after the 100-sample gap
    np.testing.assert_array_equal(blk.samples[0].real, 1e6 + np.arange(256, 512))
    assert not g.read(256).overflow
    g.close()


def test_timeout_propagates_and_marks_alignment_lost():
    g, a, b = make_group()
    g.configure(rxcfg())
    g.start_stream()
    a.script.append("timeout")
    with pytest.raises(StreamTimeout):
        g.read(256, timeout_s=0.1)
    assert g.stats.timeouts == 1
    blk = g.read(256)                                        # members no longer in lock-step
    assert blk.overflow
    assert not g.read(256).overflow
    g.close()


def test_member_timeout_flags_that_member_and_the_group():
    """The member that timed out mid-block also flags its own next block (gap), not only the
    group's misalignment flag (finding STR-08)."""
    g, a, b = make_group()
    g.configure(rxcfg())
    g.start_stream()
    b.script.append("timeout")
    with pytest.raises(StreamTimeout):
        g.read(256, timeout_s=0.1)
    assert b._gap_pending and not a._gap_pending
    assert g.read(256).overflow and b.stats.overflows == 0
    g.close()


def test_member_uncertain_overflow_span_becomes_group_settling():
    g, a, b = make_group()
    g.configure(rxcfg())
    g.start_stream()
    g.read(256)
    x = np.ones((1, 256), np.complex64)
    b.script.append(RawRead(x, overflow_before=True, uncertain_samples=600))   # e.g. Pluto DMA status
    blk = g.read(256)
    assert blk.overflow and blk.settling
    assert [g.read(256).settling for _ in range(3)] == [True, True, False]
    g.close()


def test_member_error_propagates_as_stream_error():
    g, a, b = make_group()
    g.configure(rxcfg())
    g.start_stream()
    b.script.append("error")
    with pytest.raises(StreamError, match="boom") as ei:
        g.read(256)
    assert not isinstance(ei.value, StreamTimeout)
    assert g.read(256).overflow
    g.close()


def test_timestamps_and_skew():
    g, a, b = make_group(t_offset_ns=5_000)
    g.configure(rxcfg())
    g.start_stream()
    blk = g.read(256)
    ta, tb = g.last_member_t_ns
    assert tb - ta == 5_000 and g.last_skew_ns == 5_000
    assert blk.t_ns == max(ta, tb)                           # group t_ns = latest member first-sample time
    g.close()


def test_member_settling_propagates():
    g, a, b = make_group()
    g.configure(rxcfg())
    g.start_stream()
    assert not g.read(256).settling                          # settle_buffers = 0
    b.settle_buffers = 1
    b.mark_settling()                                        # e.g. a member-local retune
    blk = g.read(256)
    assert blk.settling
    assert not g.read(256).settling
    g.set_center_freq(915e6)
    g.settle_buffers = 2
    g.mark_settling()
    assert g.read(256).settling and g.read(256).settling and not g.read(256).settling
    g.close()


# ----------------------------------------------------------------------------- optional features
def test_ppm_correction_scalar_and_per_member():
    g, a, b = make_group()
    assert g.set_ppm_correction(3.0) == "hardware"
    assert a.ppm_hw == b.ppm_hw == 3.0 and g.ppm_hw == 3.0 and g.ppm_hw_via == "hardware"
    assert g.set_ppm_correction([1.5, -2.5]) == "hardware"
    assert (a.ppm_hw, b.ppm_hw) == (1.5, -2.5) and g.ppm_hw_members == [1.5, -2.5]
    assert g.last_ppm_paths == {"rxA": "hardware", "rxB": "hardware"}
    with pytest.raises(ConfigError):
        g.set_ppm_correction([1.0, 2.0, 3.0])
    g.close()


def test_tracking_and_sensors_fan_out():
    g, a, b = make_group()
    res = g.set_tracking(dc=True, quadrature=False)
    assert a.tracking == b.tracking == {"dc": True, "rf_dc": None, "quadrature": False}
    assert res == {"dc": "mixed", "quadrature": "mixed"}
    assert g.last_tracking_paths["rxA"] == {"dc": "iio", "quadrature": "iio"}
    s = g.read_sensors()
    assert s == {"rxA.temperature_c": 41.0, "rxB.temperature_c": 42.0}
    d = g.describe()
    assert len(d["members"]) == 2 and d["num_channels"] == 2 and d["coherent"] is False
    g.close()


@pytest.mark.parametrize("sources, expected", [
    ((None, None), ""),                                      # simulators: exact, nothing stated
    (("driver", "driver"), "driver"),
    (("dma_status", "driver"), "dma_status"),
    (("dma_status", "dma_status"), "dma_status"),
    (("dma_status", "none"), "none"),                        # one blind member: losses may go unseen
    (("none", None), "none"),
    ((None, "driver"), "driver"),
])
def test_overflow_source_is_the_weakest_member(sources, expected):
    g, a, b = make_group()
    for m, src in zip((a, b), sources):
        if src is not None:
            m.overflow_source = src
    assert g.overflow_source == expected
    d = g.describe()
    assert d["overflow_source"] == expected
    assert d["overflow_source_members"] == {"rxA": sources[0] or "", "rxB": sources[1] or ""}
    b.overflow_source = "none"                               # e.g. a member's DMA poll failed mid-stream
    assert g.overflow_source == "none"
    g.close()


def test_close_closes_members_and_workers():
    g, a, b = make_group()
    g.configure(rxcfg())
    g.start_stream()
    g.read(256)
    assert len(g.worker_threads) == 2
    g.close()
    assert a.closed and b.closed and not a.streaming and not b.streaming
    assert g.worker_threads == []
    with pytest.raises(StreamError):
        g.read(16)
    g.close()                                                # idempotent
