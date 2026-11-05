"""Tests of the end-to-end latency measurement (eds.measure.specs.latency) on the simulator.

The real pipeline runs (RxNode → ZMQ → subscriber). On the real-time simulator an emitter
switched on at host time T appears exactly from the first sample produced at/after T (scene
ground truth), so Mode B measures true RF-event → delivery latency.
"""

from __future__ import annotations

import math
import threading
import time

import numpy as np
import pytest

from eds.core.config import AppConfig, CalConfig, DeviceConfig, DspConfig, MeasureConfig, RxConfig, SimConfig
from eds.core.timeutil import now_ns
from eds.hal.tx import SignalSource
from eds.measure.base import run_measurement
from eds.measure.session import build_context, close_context
from eds.measure.specs.latency import (
    BLOCKER_MODE_A,
    NONREALTIME_WARNING,
    P95_CI_MIN_N,
    P99_MIN_N,
    latency_stats,
    quantile_ci,
)
from eds.measure.targets import REASON_NONREALTIME

FC, FS, BLOCK = 915e6, 2e6, 32768


def make_app(realtime: bool, tmp_path, **params) -> AppConfig:
    return AppConfig(
        devices=[DeviceConfig(name="rx0", backend="sim", sim={"realtime": realtime})],
        rx=RxConfig(center_freq_hz=FC, sample_rate_hz=FS, bandwidth_hz=FS, gain_db=50.0, block_size=BLOCK,
                    buffer_size=BLOCK),
        dsp=DspConfig(nfft=2048, averages=8),
        cal=CalConfig(dir=str(tmp_path / "cal")),
        measure=MeasureConfig(source="sim", out_dir=str(tmp_path), params={"latency": params} if params else {}),
        sim=SimConfig(emitters=[{"name": "bg", "kind": "cw", "freq_hz": 914.6e6, "power_dbm": -90.0}], seed=11),
    )


def run_latency(app: AppConfig):
    ctx = build_context(app, quick=True)
    try:
        return run_measurement("latency", ctx), ctx
    finally:
        close_context(ctx)


# --------------------------------------------------------------------------- statistics
def test_quantile_ci_covers_true_p95():
    """Distribution-free order-statistic CI: coverage of the true p95 ≈ nominal 95 %."""
    rng = np.random.default_rng(20261105)
    true_p95 = -math.log(0.05)                     # exponential(1) quantile
    n, trials, hit = 100, 1500, 0
    for _ in range(trials):
        ci = quantile_ci(np.sort(rng.exponential(1.0, n)), 0.95)
        assert ci is not None and ci[0] <= ci[1]
        hit += ci[0] <= true_p95 <= ci[1]
    assert 0.93 <= hit / trials <= 0.995
    assert quantile_ci(np.array([1.0]), 0.95) is None
    assert quantile_ci(np.arange(10.0), 0.95) is None            # too few samples for 95 % at p95
    # the two-sided 95 % CI of the p95 needs the max to be an upper bound: 0.95^N <= 0.025
    assert quantile_ci(np.arange(float(P95_CI_MIN_N - 1)), 0.95) is None
    assert quantile_ci(np.arange(float(P95_CI_MIN_N)), 0.95) is not None


def test_latency_stats_percentiles():
    st = latency_stats(list(np.arange(1.0, 101.0)) + [math.nan])
    assert st.n == 100 and st.max == 100.0 and st.min == 1.0
    assert st.p50 == pytest.approx(50.5) and st.p95 == pytest.approx(95.05) and st.p99 == pytest.approx(99.01)
    assert st.p95_ci is not None
    assert latency_stats([math.nan]) is None
    # MM-11: below P99_MIN_N the "p99" is only an interpolation of the largest samples
    small = latency_stats(list(np.arange(1.0, float(P99_MIN_N))))
    assert small.p99 is None and small.to_dict("x")["x_p99_ms"] is None


# --------------------------------------------------------------------------- measurement
@pytest.mark.realtime
def test_latency_realtime_sim_mode_b_detects_every_event(tmp_path):
    res, ctx = run_latency(make_app(True, tmp_path))
    assert res.status == "ok", (res.error, res.extra.get("traceback"))
    assert res.title == "Uçtan uca gecikme"
    ex = res.extra
    assert ex["mode"] == "B" and ex["realtime"] is True and "warning" not in ex
    assert ex["mode_b_events"] == 6 and ex["mode_b_detected"] == 6 and ex["mode_b_timeouts"] == 0
    assert math.isfinite(res.value) and 0.0 < res.value < 1000.0 and res.unit == "ms"
    assert res.value == pytest.approx(ex["mode_b_p95_ms"], abs=1e-3)
    assert ex["mode_b_p50_ms"] <= ex["mode_b_p95_ms"] <= ex["mode_b_max_ms"] + 1e-9
    assert ex["mode_a_p50_ms"] <= ex["mode_a_p95_ms"] <= ex["mode_a_max_ms"] + 1e-9
    # JURY-02 / MM-11: 6 events → no p99, no CI of the p95, hence no uncertainty — and it is said
    assert ex["mode_b_p99_ms"] is None and ex["mode_b_p95_ci95_ms"] is None and res.uncertainty is None
    assert "hesaplanamadı (N = 6 < 72)" in res.summary and "p99" not in res.summary.split(";")[0]
    assert "N = 6, güven aralığı hesaplanamadı" in res.method
    # MM-09: the simulator source switches the scene exactly: no correction, no uncertainty
    assert ex["source_timing"]["kind"] == "exact" and ex["source_timing"]["uncertainty_ms"] == 0.0
    assert res.blockers == []
    # tone set by the adaptive level loop: >= 25 dB SNR at fc + 0.15 fs
    tone = ex["mode_b_tone"]
    assert tone["snr_db"] >= 25.0 and tone["f_src_hz"] == pytest.approx(FC + 0.15 * FS)
    # Mode A: the analysed window is exactly welch_len(nfft, averages) / fs and NOT a latency component
    assert ex["window_samples"] == 2048 + 1024 * 7
    assert ex["mode_a_window_mean_ms"] == pytest.approx(ex["window_samples"] / FS * 1e3, abs=1e-3)
    bd = ex["mode_a_breakdown_mean_ms"]
    assert set(bd) == {"processing_ms", "publish_ms", "transport_ms"} and bd["transport_ms"] < 50.0
    assert sum(bd.values()) == pytest.approx(ex["mode_a_mean_ms"], rel=1e-6)
    # STR-09: the USB transfer of one buffer is the known minimum Mode A misses
    assert ex["usb_transfer_min_ms"] == pytest.approx(1e3 * BLOCK * 4 / 24e6)
    b_rows = [r for r in res.table if r["mode"] == "B"]
    assert len(b_rows) == 6 and all(r["status"] == "tespit" for r in b_rows)
    for r in b_rows:
        # causality: the detecting window ends after the switch-on and the message arrives after it
        assert r["acquisition_ms"] >= 0.0 and r["latency_ms"] > r["acquisition_ms"]
        parts = r["acquisition_ms"] + r["processing_ms"] + r["publish_ms"] + r["transport_ms"]
        assert parts == pytest.approx(r["latency_ms"], abs=1e-3)
        assert abs(r["f_det_hz"] - tone["f_src_hz"]) <= tone["tolerance_hz"]
    assert 0 < sum(r["mode"] == "A" for r in res.table) <= 200
    figs = {f.name: f for f in res.figures}
    assert {n: f.kind for n, f in figs.items()} == {
        "latency_mode_b_cdf": "hist", "latency_mode_a_cdf": "hist",
        "latency_mode_a_breakdown": "xy", "latency_mode_b_breakdown": "xy"}
    assert [v[1].split(" =")[0] for v in figs["latency_mode_b_cdf"].vlines] == ["p50", "p95"]
    # JURY-13: stacked bars — the bar top is the mean latency of the mode (window excluded)
    for name, key, n in (("latency_mode_a_breakdown", "mode_a_breakdown_mean_ms", 3),
                         ("latency_mode_b_breakdown", "mode_b_breakdown_mean_ms", 4)):
        f = figs[name]
        assert all(s.style == "bar" and s.x == [1.0] for s in f.series) and len(f.series) == n
        assert max(s.y[0] for s in f.series) == pytest.approx(sum(max(v, 0.0) for v in ex[key].values()))
        assert not any("Pencere" in s.label for s in f.series)
        assert len(f.xticks) == 1 and f.xticks[0][0] == 1.0 and f.xticks[0][1].startswith("Mod " + name.split("_")[2].upper())
    assert res.conditions["block_size"] == BLOCK and res.conditions["transport"] == "tcp"
    # the tone setting carries its unit (simulator source = absolute dBm) and respects the RX ceiling
    assert "tone_power" not in res.conditions and "power" not in tone
    assert res.conditions["tone_power_dbm"] == tone["power_dbm"] <= res.conditions["rx_max_input_dbm"] == -5.0
    assert res.value == ex["mode_b_p95_ms"]                       # unrounded (the report formats digits)
    # the node's hardware ppm correction was undone; the device is idle again
    assert ctx.dut.ppm_hw == 0.0 and not ctx.dut.streaming


def test_latency_nonrealtime_sim_warns_and_inproc_transport(tmp_path):
    app = make_app(False, tmp_path, transport="inproc", n_events=2, quick_n_events=2, quick_duration_s=0.4,
                   warmup_s=0.1)
    res, _ = run_latency(app)
    assert res.status == "ok", (res.error, res.extra.get("traceback"))
    assert res.extra["realtime"] is False and res.extra["warning"] == NONREALTIME_WARNING
    assert res.summary.startswith(NONREALTIME_WARNING) and REASON_NONREALTIME in res.blockers
    assert res.extra["transport"] == "inproc" and res.extra["mode_b_events"] == 2
    assert math.isfinite(res.value)


def test_latency_without_source_uses_mode_a(tmp_path):
    app = make_app(False, tmp_path, quick_duration_s=0.3, warmup_s=0.0)
    app.measure.source = "none"
    res, _ = run_latency(app)
    assert res.status == "ok", (res.error, res.extra.get("traceback"))
    assert res.extra["mode"] == "A" and "mode_b_events" not in res.extra
    assert res.value == pytest.approx(res.extra["mode_a_p95_ms"], abs=1e-3)
    assert [f.name for f in res.figures] == ["latency_mode_a_cdf", "latency_mode_a_breakdown"]
    assert "Mod B atlandı" in res.summary
    # STR-09: Mode A is host-side only — labelled as such, a lower bound, and never compared with the target
    assert BLOCKER_MODE_A in res.blockers and "FIFO/USB" in res.summary
    lower = res.extra["mode_a_p95_plus_transfer_ms"]
    assert f"uçtan uca gecikme ≥ {lower:.1f} ms" in res.summary
    assert res.conditions["headline_mode"] == "A"


def test_latency_rejects_bad_transport(tmp_path):
    app = make_app(False, tmp_path, transport="udp")
    res, _ = run_latency(app)
    assert res.status == "failed" and "transport" in res.error


# --------------------------------------------------------------------------- RF-on instant (MM-09)
class DelayedSource(SignalSource):
    """Wraps the simulator source with a real source's timing (same emitter name, so the
    scene isolation keeps it):

    * ``queue_s``: streaming TX (Pluto) — ``set_enabled`` returns at once, the RF changes
      ``queue_s`` later (samples already queued in the driver); ``estimated_queue_latency_s``;
    * ``before_s`` / ``after_s``: SCPI generator — the RF switches ``before_s`` into the call,
      which returns after a further ``after_s`` (``*OPC?`` turnaround)."""

    def __init__(self, inner, queue_s: float = 0.0, before_s: float = 0.0, after_s: float = 0.0):
        self.inner = inner
        self.name = inner.name
        self.absolute_power = inner.absolute_power
        self.power_range_db = inner.power_range_db
        self._q, self._before, self._after = queue_s, before_s, after_s
        self._timers: list[threading.Timer] = []
        if queue_s:
            self.estimated_queue_latency_s = queue_s

    def set_frequency(self, hz):
        return self.inner.set_frequency(hz)

    def set_power(self, dbm):
        return self.inner.set_power(dbm)

    def set_enabled(self, on):
        if self._q:
            t = threading.Timer(self._q, self.inner.set_enabled, [on])
            t.daemon = True
            t.start()
            self._timers.append(t)
            return now_ns()
        time.sleep(self._before)
        self.inner.set_enabled(on)
        time.sleep(self._after)
        return now_ns()

    def close(self):
        for t in self._timers:
            t.join()
        self.inner.close()


@pytest.mark.realtime
@pytest.mark.parametrize("kind, kw", [("queued", {"queue_s": 0.06}), ("bracket", {"before_s": 0.03, "after_s": 0.03})])
def test_latency_rf_on_instant_of_real_sources(tmp_path, kind, kw):
    """The RF-on estimate of every event lies within its stated uncertainty of the true
    scene switch (simulator ground truth). Before the fix t_on was the call's return:
    +60 ms late for the queued TX (latency inflated) and 30 ms late for the SCPI-like
    source (latency deflated)."""
    from eds.hal import get_sim_world

    app = make_app(True, tmp_path)
    ctx = build_context(app, quick=True)
    src = DelayedSource(ctx.source, **kw)
    ctx.source = src
    try:
        res = run_measurement("latency", ctx)
    finally:
        close_context(ctx)
    assert res.status == "ok", (res.error, res.extra.get("traceback"))
    timing = res.extra["source_timing"]
    assert timing["kind"] == kind
    rows = [r for r in res.table if r["mode"] == "B"]
    assert len(rows) == 6 and all(r["status"] == "tespit" for r in rows)
    on = [e["t_ns"] for e in get_sim_world(app).switch_log() if e["name"] == src.name and e["on"]]
    truth = on[-len(rows):]                                   # the events' switch-ons (after the level loop)
    for r, t_true in zip(rows, truth):
        err_ms = (r["t_rf_ns"] - t_true) / 1e6
        assert abs(err_ms) <= r["t_rf_unc_ms"] + 3.0, (err_ms, r["t_rf_unc_ms"])
        assert r["acquisition_ms"] >= -r["t_rf_unc_ms"] - 3.0
    blocked = any("RF açılma anı belirsizliği" in b for b in res.blockers)
    if kind == "queued":
        assert timing["correction_ms"] == pytest.approx(60.0) and timing["uncertainty_ms"] == pytest.approx(15.0)
        assert "+60.0 ms" in res.summary
        assert blocked is (15.0 > 0.5 * res.value)
    else:
        assert timing["correction_ms"] == 0.0 and 29.0 <= timing["uncertainty_ms"] <= 40.0
        assert blocked is (timing["uncertainty_ms"] > 0.5 * res.value)
    assert res.conditions["source_timing_unc_ms"] == timing["uncertainty_ms"]
    assert f"±{timing['uncertainty_ms']:.2f} ms" in res.summary


# --------------------------------------------------------------------------- tone level ceiling
class _LevelWorld:
    """Fake pipeline for the adaptive level loop: the tone's dBFS level follows the RX input
    exactly through the nominal level model (dBFS = dBm + G − 5), the node publishes its band
    power in dBFS and its SNR against a fixed noise floor; it is detected above 6 dB SNR."""

    G, NOISE_DBFS = 50.0, -60.0

    def __init__(self, absolute: bool, anchor_dbm: float = -50.0):
        self.absolute, self.anchor = absolute, anchor_dbm
        self.setting = math.nan
        self.inputs: list[float] = []

    def input_dbm(self, setting: float) -> float:
        return setting if self.absolute else setting + self.anchor


class _LevelSource:
    name = "fake"

    def __init__(self, world: _LevelWorld):
        self.w = world
        self.absolute_power = world.absolute
        self.power_range_db = (-140.0, 0.0) if world.absolute else (-90.0, 0.0)

    def set_frequency(self, hz):
        return hz

    def set_power(self, p):
        self.w.setting = float(p)
        return float(p)

    def set_enabled(self, on):
        if on:
            self.w.inputs.append(self.w.input_dbm(self.w.setting))
        return now_ns()


class _LevelSub:
    def __init__(self, world: _LevelWorld, f_src: float):
        self.w, self.f = world, f_src

    def recv(self, timeout):
        from types import SimpleNamespace

        from eds.bus.topics import RX_DETECTIONS

        level = self.w.input_dbm(self.w.setting) + self.w.G - 5.0
        snr = level - self.w.NOISE_DBFS
        dets = [{"f_peak_hz": self.f, "snr_db": snr, "band_power_db": level}] if snr > 6.0 else []
        return SimpleNamespace(topic=RX_DETECTIONS, data={"t_acq_ns": now_ns() + 10**12, "unit": "dBFS",
                                                         "detections": dets})


@pytest.mark.parametrize("absolute", [True, False], ids=["absolute", "relative"])
def test_latency_tone_level_respects_rx_ceiling(absolute):
    """The adaptive level must not drive the RX input above the profile ceiling: 30 dB SNR
    would need −75 dBm, the ceiling is −80 dBm (absolute source: capped by
    source_power_range; relative source: cap extrapolated from the published tone level)."""
    import dataclasses
    from types import SimpleNamespace

    from eds.core.profile import load_profile
    from eds.measure.base import MeasurementContext
    from eds.measure.specs.latency import LatencyMeasurement

    world = _LevelWorld(absolute)
    src = _LevelSource(world)
    prof = dataclasses.replace(load_profile("ad9363_pluto"), rx_max_input_dbm=-80.0)
    dev = SimpleNamespace(gains=(world.G,), profile=prof, fs=FS)
    m = LatencyMeasurement(MeasurementContext(app=AppConfig(), devices=[dev], source=src))
    prm = {"snr_db": 30.0, "event_timeout_s": 0.5}
    level = m._set_level(_LevelSub(world, FC), SimpleNamespace(device=dev), prm, None, FC, 5e3, 0)
    key = "power_dbm" if absolute else "power_rel_db"
    assert max(world.inputs) <= -80.0 + 1e-9 and world.input_dbm(level[key]) == pytest.approx(-80.0)
    assert level["snr_db"] == pytest.approx(25.0) and level["rx_max_input_dbm"] == -80.0
    assert level["power_unit"] == ("dBm" if absolute else "dB (bağıl)")
    assert m.setting_key(src) == ("tone_power_dbm" if absolute else "tone_power_rel_db")
    # without a ceiling the same loop reaches the SNR target (the cap is what stopped it)
    world2 = _LevelWorld(absolute)
    dev2 = SimpleNamespace(gains=(world.G,), profile=dataclasses.replace(prof, rx_max_input_dbm=10.0), fs=FS)
    m2 = LatencyMeasurement(MeasurementContext(app=AppConfig(), devices=[dev2], source=_LevelSource(world2)))
    level2 = m2._set_level(_LevelSub(world2, FC), SimpleNamespace(device=dev2), prm, None, FC, 5e3, 0)
    assert abs(level2["snr_db"] - 30.0) <= 3.0 and max(world2.inputs) > -80.0
