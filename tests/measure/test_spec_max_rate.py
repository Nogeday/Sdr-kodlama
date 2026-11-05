"""Tests of the maximum overflow-free sample rate measurement (eds.measure.specs.max_rate).

Ground truth:
* real-time simulator: USB model of ``usb_bytes_per_s = 24e6`` B/s → 6 MS/s of CS16 is the
  transport boundary; above it the delivered fraction is ``6 MS/s / fs``;
* a paced fake receiver (wall-clock production, optional hardware timestamps and a SILENT
  gap the driver never reports) for the wall-clock and timestamp accounting paths.
"""

from __future__ import annotations

import math
import time

import numpy as np
import pytest

from eds.core.config import AppConfig, CalConfig, DeviceConfig, MeasureConfig, RxConfig
from eds.core.profile import load_profile
from eds.core.timeutil import now_ns
from eds.hal.base import RawRead, RxDevice
from eds.measure.base import run_measurement
from eds.measure.session import build_context, close_context
from eds.measure.specs.max_rate import BLOCKER_TRANSPORT_ONLY, NONREALTIME_WARNING, Trial, evaluate
from eds.measure.targets import REASON_NONREALTIME

from .spec_fixtures_b import assert_result_contract, run_spec, single_sim_app

USB_SPS = 24e6 / 4        # sample rate the simulated USB link can carry


# ----------------------------------------------------------------------------- simulator
@pytest.mark.realtime
def test_max_rate_quick_realtime_usb_boundary(tmp_path):
    app = single_sim_app(tmp_path, realtime=True, spec_id="max_rate", sim={"usb_bytes_per_s": 24e6})
    t0 = time.perf_counter()
    res, _ = run_spec(app, "max_rate", quick=True)
    assert time.perf_counter() - t0 < 30.0
    assert_result_contract(res, tmp_path)
    ex = res.extra
    # MM-02: the headline is the system-level value (real RxNode pipeline), not the transport
    assert res.title == "Taşmasız maksimum örnekleme hızı" and res.unit == "MS/s"
    assert ex["headline_mode"] == "pipeline" and res.conditions["headline_mode"] == "pipeline"
    assert res.value == ex["max_rate_pipeline_msps"] and res.value in (4.0, 5.0, 6.0)
    assert ex["max_rate_transport_msps"] in (5.0, 6.0) and res.value <= ex["max_rate_transport_msps"]
    assert res.summary.startswith("Sistem düzeyi (DSP hattı açık") and "usb_bytes_per_s = 24 MB/s" in res.summary
    assert "RxNode" in res.method and "CFAR" in res.method
    assert res.blockers == []                                   # real-time simulator, pipeline measured
    assert ex["mb_per_s_at_max"] == pytest.approx(res.value * 4.0)
    assert ex["usb_theoretical_pct"] == pytest.approx(100.0 * res.value * 4e6 / 53.248e6)
    assert ex["accounting"] == ["device_counter"] and ex["duration_s"] == 0.6
    assert res.conditions["queue_blocks"] == app.rx.queue_blocks and res.conditions["nfft"] == app.dsp.nfft
    rows = res.table
    measured = [r for r in rows if not r["implied"]]
    # every rate >= 7.68 MS/s fails in both modes; overflows are counted
    high = [r for r in measured if r["rate_msps"] >= 7.68]
    assert high and all(r["status"] == "KALDI" and r["overflows"] > 0 for r in high)
    # above the USB boundary the delivered fraction is the link rate / fs (consumer overhead: +5 %)
    for r in (r for r in high if r["mode"] == "transport"):
        assert r["loss"] == pytest.approx(1.0 - USB_SPS / (r["rate_msps"] * 1e6), abs=0.05)
    # passing rates: no event; the device counter shows no gap (pipeline: ±1 block snapshot race)
    for r in measured:
        if r["status"] == "GEÇTİ":
            assert r["overflows"] == 0 and r["timeouts"] == 0 and r["host_drops"] == 0
            if r["mode"] == "transport":
                assert r["delivered_samples"] == r["expected_samples"] and r["loss"] == 0.0
            else:
                assert abs(r["lost_samples"]) <= r["allowed_lost_samples"] == app.rx.block_size
                assert r["frames"] > 0 and r["backlog_ms"] >= 0.0 and math.isfinite(r["lag_ms"])
            assert r["elapsed_s"] >= 0.6
    # pipeline passes below its first failure imply transport passes (not streamed)
    implied = [r for r in rows if r["implied"]]
    assert implied and all(r["mode"] == "transport" and r["status"] == "GEÇTİ (çıkarım)" for r in implied)
    assert all(r["overflows"] is None and r["loss"] is None for r in implied)
    assert max(r["rate_msps"] for r in implied) <= ex["max_rate_pipeline_msps"]
    names = {f.name: f for f in res.figures}
    assert set(names) == {"max_rate_overflow_loss", "max_rate_throughput", "max_rate_spectrum"}
    assert names["max_rate_overflow_loss"].logy
    assert names["max_rate_overflow_loss"].title.startswith("Taşmasız maksimum örnekleme hızı")
    hl = [h[1] for h in names["max_rate_throughput"].hlines]
    assert any("53.2" in t for t in hl) and any("24.0" in t for t in hl)
    assert names["max_rate_spectrum"].info["Span"].startswith(f"{res.value:g}")
    assert "DSP hattı açık" in names["max_rate_spectrum"].title


def test_max_rate_nonrealtime_profile_limits_and_matrix(tmp_path):
    params = {"rates": [0.01e6, 1e6, 2e6, 100e6], "quick_duration_s": 0.05, "quick_fail_window_s": 0.0,
              "modes": ["transport"], "buffer_sizes": [16384]}
    app = single_sim_app(tmp_path, realtime=False, spec_id="max_rate", params=params)
    res, ctx = run_spec(app, "max_rate", quick=True)
    assert_result_contract(res, tmp_path)
    ex = res.extra
    assert res.summary.startswith(NONREALTIME_WARNING) and ex["warning"] == NONREALTIME_WARNING
    # not comparable with the KTR target: non-real-time simulator AND transport-only value
    assert REASON_NONREALTIME in res.blockers and BLOCKER_TRANSPORT_ONLY in res.blockers
    assert ex["headline_mode"] == "transport" and res.summary.count("Yalnız taşıma") == 1
    assert ex["excluded_rates_hz"] == [0.01e6, 100e6]                     # outside 65.1 kS/s .. 61.44 MS/s
    assert res.value == 2.0 and ex["all_passed_transport"] is True        # lower bound: list exhausted
    assert "aday listesinin sonu" in res.summary
    assert [m["buffer_size"] for m in ex["matrix"]] == [65536, 16384]
    assert all(m["max_rate_msps"] == 2.0 for m in ex["matrix"])
    assert {r["buffer_size"] for r in res.table} == {65536, 16384}
    assert "max_rate_buffer_matrix" in {f.name for f in res.figures}
    # the device is returned to its configured state (rate, bandwidth, buffer, stream stopped)
    dev = ctx.devices[0]
    assert dev.fs == app.rx.sample_rate_hz and dev.bw == app.rx.bandwidth_hz
    assert dev.buffer_size == app.rx.buffer_size and not dev.streaming


def test_max_rate_nonrealtime_skips_pipeline_and_is_blocked(tmp_path):
    """The RxNode pipeline cannot be measured on a non-real-time simulator (its host queue
    overflows at any rate): only the transport runs, the reason is reported, no verdict."""
    params = {"rates": [1e6, 2e6], "quick_duration_s": 0.05, "quick_fail_window_s": 0.0}
    app = single_sim_app(tmp_path, realtime=False, spec_id="max_rate", params=params)
    res, _ = run_spec(app, "max_rate", quick=True)
    assert res.status == "ok", (res.error, res.extra.get("traceback"))
    assert res.extra["headline_mode"] == "transport" and "pipeline_skipped" in res.extra
    assert {r["mode"] for r in res.table} == {"transport"} and res.value == 2.0
    assert "DSP hattı: gerçek zamanlı olmayan" in res.summary
    assert REASON_NONREALTIME in res.blockers


def test_max_rate_rejects_bad_modes(tmp_path):
    app = single_sim_app(tmp_path, realtime=False, spec_id="max_rate", params={"modes": ["dsp"]})
    res, _ = run_spec(app, "max_rate", quick=True)
    assert res.status == "failed" and "max_rate.modes" in res.error


# ----------------------------------------------------------------------------- search logic
def _t(rate: float, ok: bool, refine: bool = False) -> Trial:
    return Trial(mode="transport", rate_hz=rate, buffer_size=65536, refine=refine, status="pass" if ok else "fail")


def test_evaluate_max_below_first_failure_and_refinement():
    out = evaluate("transport", [_t(1e6, True), _t(2e6, True), _t(3e6, False), _t(4e6, True)])
    assert out.max_hz == 2e6 and out.first_fail.rate_hz == 3e6
    assert out.monotonic is False and out.all_passed is False      # a pass above a failure is never the value
    out = evaluate("transport", [_t(1e6, True), _t(2e6, True), _t(3e6, False), _t(2.5e6, True, True),
                                 _t(2.75e6, False, True), _t(2.625e6, True, True)])
    assert out.max_hz == 2.625e6 and out.first_fail.rate_hz == 2.75e6 and out.monotonic
    out = evaluate("transport", [_t(1e6, False), _t(2e6, False)])
    assert out.max_hz is None and out.first_fail.rate_hz == 1e6
    out = evaluate("transport", [_t(1e6, True), _t(2e6, True)])
    assert out.max_hz == 2e6 and out.all_passed and out.first_fail is None


# ----------------------------------------------------------------------------- accounting
class PacedRx(RxDevice):
    """Fake receiver producing ``fs`` samples per second of WALL-CLOCK time (no sample
    counter). ``gap_at_s`` / ``gap_s``: once, the production silently skips ``gap_s`` worth
    of samples without any overflow report (drivers that never report USB overflows)."""

    backend_name = "paced"

    def __init__(self, timestamps: bool = False, gap_at_s: float | None = None, gap_s: float = 0.0):
        super().__init__(DeviceConfig(name="paced", backend="sim"), load_profile("ad9363_pluto"))
        self._timestamps = timestamps
        self._gap_at = gap_at_s
        self._gap = gap_s
        self.gap_samples = 0                               # once per device lifetime (first stream)
        self._gap_done = False
        self._t0 = 0.0
        self._next = 0                                     # device index of the next sample

    def _apply_sample_rate(self, hz: float) -> float:
        return hz

    def _apply_bandwidth(self, hz: float) -> float:
        return hz

    def _apply_center_freq(self, hz: float) -> float:
        return hz

    def _apply_gain(self, db: float, channel: int) -> float:
        return db

    def _apply_gain_mode(self, mode: str) -> None:
        return None

    def _start(self, buffer_size: int) -> None:
        self._t0, self._next = time.perf_counter(), 0

    def _stop(self) -> None:
        return None

    def _close(self) -> None:
        return None

    def _read_raw(self, n: int, timeout_s: float) -> RawRead:
        fs = self.fs
        if self._gap_at is not None and not self._gap_done and self._next >= self._gap_at * fs:
            self.gap_samples = int(round(self._gap * fs))
            self._next += self.gap_samples                # silently lost, no overflow flag
            self._gap_done = True
        t_ready = self._t0 + (self._next + n) / fs
        delay = t_ready - time.perf_counter()
        if delay > 0:
            time.sleep(delay)
        hw = int(round(self._next / fs * 1e9)) if self._timestamps else None
        self._next += n
        return RawRead(samples=np.zeros((1, n), np.complex64), t_ns=now_ns(), hw_time_ns=hw)


def _run_paced(tmp_path, dev: PacedRx):
    app = AppConfig(devices=[DeviceConfig(name="paced", backend="sim")],
                    rx=RxConfig(center_freq_hz=915e6, sample_rate_hz=1e6, bandwidth_hz=1e6, buffer_size=16384),
                    cal=CalConfig(dir=str(tmp_path / "cal")), measure=MeasureConfig(source="none", out_dir=str(tmp_path)))
    ctx = build_context(app, devices=[dev], quick=True)
    try:
        return run_measurement("max_rate", ctx, rates=[1e6], modes=["transport"], quick_duration_s=0.6,
                               quick_fail_window_s=1.0, spectrum_nfft=1024, spectrum_averages=4)
    finally:
        close_context(ctx, close_devices=False)


@pytest.mark.realtime
def test_wall_clock_accounting_detects_silent_gap(tmp_path):
    clean = _run_paced(tmp_path, PacedRx())
    assert clean.status == "ok", (clean.error, clean.extra.get("traceback"))
    (row,) = clean.table
    assert row["accounting"] == "wall_clock" and row["overflows"] == 0
    assert abs(row["loss"]) < 5e-3                         # read-return jitter only (ms over 0.6 s)
    dev = PacedRx(gap_at_s=0.25, gap_s=0.05)
    res = _run_paced(tmp_path, dev)
    (row,) = res.table
    assert row["overflows"] == 0 and row["timeouts"] == 0 and row["status"] == "KALDI"
    truth = dev.gap_samples / (row["delivered_samples"] + dev.gap_samples)
    assert row["loss"] == pytest.approx(truth, abs=5e-3) and truth > 0.05
    assert res.value is None and "hiçbir hız geçmedi" in res.summary


@pytest.mark.realtime
def test_hardware_timestamp_accounting_is_exact(tmp_path):
    clean = _run_paced(tmp_path, PacedRx(timestamps=True))
    (row,) = clean.table
    assert row["accounting"] == "hw_timestamp" and row["loss"] == 0.0 and row["status"] == "GEÇTİ"
    assert clean.value == 1.0
    dev = PacedRx(timestamps=True, gap_at_s=0.25, gap_s=0.05)
    res = _run_paced(tmp_path, dev)
    (row,) = res.table
    assert row["status"] == "KALDI"
    assert row["loss"] == pytest.approx(dev.gap_samples / (row["delivered_samples"] + dev.gap_samples), abs=1e-12)


class FifoRx(PacedRx):
    """Wall-clock fake of a Pluto-like stream: whole buffers of ``buffer_size`` samples are
    produced at ``fs`` into a ``kernel_buffers``-deep device FIFO and cross a USB link at
    ``usb_bytes_per_s`` when read. No sample counter, no timestamps, no overflow flag (a
    full FIFO silently drops the oldest buffer, like an old Pluto). ``stall_every`` /
    ``stall_periods``: the consumer is descheduled for ``stall_periods`` buffer periods
    before every ``stall_every``-th read (a momentary backlog that drains again)."""

    backend_name = "fifo"

    def __init__(self, usb_bytes_per_s: float = 30e6, stall_every: int = 0, stall_periods: float = 0.0,
                 drop_at_s: float | None = None, drop_periods: float = 0.0):
        super().__init__()
        self._usb = usb_bytes_per_s
        self._stall_every, self._stall_periods = stall_every, stall_periods
        self._drop_at, self._drop_periods = drop_at_s, drop_periods
        self.dropped_buffers = 0
        self._reads = 0

    def _start(self, buffer_size: int) -> None:
        self._t0, self._buf, self._off, self._reads, self._dropped_once = time.perf_counter(), 0, 0, 0, False

    def _read_raw(self, n: int, timeout_s: float) -> RawRead:
        fs, b = self.fs, int(self.buffer_size)
        period = b / fs
        self._reads += 1
        stall = 0.0
        if self._stall_every and self._reads % self._stall_every == 0:
            stall = self._stall_periods * period
        if self._drop_at is not None and not self._dropped_once and time.perf_counter() - self._t0 >= self._drop_at:
            stall, self._dropped_once = self._drop_periods * period, True
        if stall:
            time.sleep(stall)
        completed = int((time.perf_counter() - self._t0) / period)
        oldest = completed - int(self.profile.kernel_buffers)
        if self._buf < oldest:                            # FIFO overflowed: silently lost buffers
            self.dropped_buffers += oldest - self._buf
            self._buf, self._off = oldest, 0
        wait = self._t0 + (self._buf + 1) * period - time.perf_counter()
        if wait > 0:
            time.sleep(wait)
        k = min(n, b - self._off)
        time.sleep(k * 4.0 / self._usb)
        self._off += k
        if self._off >= b:
            self._buf, self._off = self._buf + 1, 0
        return RawRead(samples=np.zeros((1, k), np.complex64), t_ns=now_ns())


def _run_fifo(tmp_path, dev: FifoRx, rate: float, buffer_size: int, duration_s: float = 0.6):
    app = AppConfig(devices=[DeviceConfig(name="fifo", backend="sim")],
                    rx=RxConfig(center_freq_hz=915e6, sample_rate_hz=rate, bandwidth_hz=min(rate, 20e6),
                                buffer_size=buffer_size),
                    cal=CalConfig(dir=str(tmp_path / "cal")), measure=MeasureConfig(source="none", out_dir=str(tmp_path)))
    ctx = build_context(app, devices=[dev], quick=True)
    try:
        return run_measurement("max_rate", ctx, rates=[rate], modes=["transport"], quick_duration_s=duration_s,
                               quick_fail_window_s=1.0, spectrum_nfft=1024, spectrum_averages=4)
    finally:
        close_context(ctx, close_devices=False)


@pytest.mark.realtime
@pytest.mark.parametrize("rate, buffer_size, every, periods", [(1e6, 16384, 3, 1.6), (5e6, 65536, 4, 1.0)])
def test_wall_clock_backlog_at_window_end_is_not_loss(tmp_path, rate, buffer_size, every, periods):
    """STR-03: momentary consumer stalls leave buffers queued in the FIFO most of the time
    (at 5 MS/s the 30 MB/s link is 67 % loaded, a backlog drains at 1/3 buffer per read), so
    the window usually ends with a backlog; nothing is lost (the FIFO never fills) and every
    trial must pass. The old ``fs × (t_last − t0)`` accounting failed most of these windows
    (loss ≈ backlog / (fs·T) ≈ 1e-2 ≫ 1e-4)."""
    for _ in range(3):
        dev = FifoRx(stall_every=every, stall_periods=periods)
        res = _run_fifo(tmp_path, dev, rate, buffer_size)
        assert res.status == "ok", (res.error, res.extra.get("traceback"))
        (row,) = res.table
        assert dev.dropped_buffers == 0
        assert row["accounting"] == "wall_clock" and row["status"] == "GEÇTİ", row
        assert abs(row["lost_samples"]) <= row["allowed_lost_samples"] < buffer_size
        assert row["drain_reads"] >= 2 and "boşalmadı" not in row["note"]


@pytest.mark.realtime
def test_wall_clock_detects_one_silently_dropped_buffer(tmp_path):
    """MM-14: one whole buffer silently dropped (FIFO full, no overflow flag) must fail; the
    old relative tolerance (1e-4) only resolved it for short runs."""
    dev = FifoRx(drop_at_s=0.3, drop_periods=5.5)          # 4-deep FIFO: 5.5 periods → 1 lost buffer
    res = _run_fifo(tmp_path, dev, 1e6, 16384)
    (row,) = res.table
    assert dev.dropped_buffers == 1 and row["overflows"] == 0
    assert row["status"] == "KALDI"
    assert row["lost_samples"] == pytest.approx(16384, abs=2000)
    assert row["allowed_lost_samples"] == pytest.approx(8192)


class BlindPlutoRx(PacedRx):
    """Pluto-like receiver whose driver reports no overflow (``overflow_source = "none"``:
    no thin libiio layer) opened with arguments SoapyPlutoSDR partly ignores."""

    overflow_source = "none"

    def __init__(self):
        super().__init__()
        self.cfg = DeviceConfig(name="paced", backend="soapy", args={
            "driver": "plutosdr", "uri": "usb:1.5.5", "usb_direct": "1", "timeout": "500",
            "stream:bufflen": "16384", "stream:loopback": "0"})


@pytest.mark.realtime
def test_max_rate_blind_overflow_and_ignored_pluto_args(tmp_path):
    """Real hardware without an overflow counter: no verdict (blocker named in the summary);
    driver-ignored Pluto args are not reported as measurement conditions."""
    from eds.measure.specs.max_rate import BLOCKER_NO_OVERFLOW

    res = _run_paced(tmp_path, BlindPlutoRx())
    assert res.status == "ok", (res.error, res.extra.get("traceback"))
    c = res.conditions
    assert c["overflow_source"] == "none" and res.extra["overflow_source"] == "none"
    assert BLOCKER_NO_OVERFLOW in res.blockers and BLOCKER_NO_OVERFLOW in res.summary
    assert c["device_args"] == {"driver": "plutosdr", "uri": "usb:1.5.5", "stream:bufflen": "16384"}
    assert c["device_args_ignored"] == ["stream:loopback", "timeout", "usb_direct"]
    assert "usb_direct" in res.summary
    # the clean paced device (no overflow_source attribute) is not blocked and lists no ignored args
    clean = _run_paced(tmp_path, PacedRx())
    assert BLOCKER_NO_OVERFLOW not in clean.blockers and clean.conditions["overflow_source"] == ""
    assert "device_args_ignored" not in clean.conditions


def test_max_rate_figure_roles_are_explicit():
    from types import SimpleNamespace

    from eds.measure.specs.max_rate import MaxRateMeasurement

    out = {"transport": evaluate("transport", [_t(1e6, True), _t(2e6, False)])}
    loss = MaxRateMeasurement._loss_figure(out, {"duration_s": 1.0, "buffer_size": 65536})
    roles = {s.label.split(" — ")[0]: s.role for s in loss.series}
    assert roles["GEÇTİ"] == "pass" and roles["KALDI"] == "fail"
    assert roles["sayım izni [örnek]"] == "ref" and roles["overflow + zaman aşımı + host drop [olay]"] == "warn"
    prof = SimpleNamespace(bytes_per_sample=4, interface="USB 2.0", interface_max_bytes_per_s=53.248e6,
                           interface_practical_bytes_per_s=24e6)
    thr = MaxRateMeasurement._throughput_figure(out, prof)
    assert [s.role for s in thr.series] == ["ref", "pass", "fail"]
