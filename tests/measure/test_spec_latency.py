"""Tests of the end-to-end latency measurement (eds.measure.specs.latency) on the simulator.

The real pipeline runs (RxNode → ZMQ → subscriber). On the real-time simulator an emitter
switched on at host time T appears exactly from the first sample produced at/after T (scene
ground truth), so Mode B measures true RF-event → delivery latency.
"""

from __future__ import annotations

import math

import numpy as np
import pytest

from eds.core.config import AppConfig, CalConfig, DeviceConfig, DspConfig, MeasureConfig, RxConfig, SimConfig
from eds.measure.base import run_measurement
from eds.measure.session import build_context, close_context
from eds.measure.specs.latency import NONREALTIME_WARNING, latency_stats, quantile_ci

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


def test_latency_stats_percentiles():
    st = latency_stats(list(np.arange(1.0, 101.0)) + [math.nan])
    assert st.n == 100 and st.max == 100.0 and st.min == 1.0
    assert st.p50 == pytest.approx(50.5) and st.p95 == pytest.approx(95.05) and st.p99 == pytest.approx(99.01)
    assert latency_stats([math.nan]) is None


# --------------------------------------------------------------------------- measurement
@pytest.mark.realtime
def test_latency_realtime_sim_mode_b_detects_every_event(tmp_path):
    res, ctx = run_latency(make_app(True, tmp_path))
    assert res.status == "ok", (res.error, res.extra.get("traceback"))
    ex = res.extra
    assert ex["mode"] == "B" and ex["realtime"] is True and "warning" not in ex
    assert ex["mode_b_events"] == 6 and ex["mode_b_detected"] == 6 and ex["mode_b_timeouts"] == 0
    assert math.isfinite(res.value) and 0.0 < res.value < 1000.0 and res.unit == "ms"
    assert res.value == pytest.approx(ex["mode_b_p95_ms"], abs=1e-3)
    assert ex["mode_b_p50_ms"] <= ex["mode_b_p95_ms"] <= ex["mode_b_p99_ms"] <= ex["mode_b_max_ms"] + 1e-9
    assert ex["mode_a_p50_ms"] <= ex["mode_a_p95_ms"] <= ex["mode_a_max_ms"] + 1e-9
    # tone set by the adaptive level loop: >= 25 dB SNR at fc + 0.15 fs
    tone = ex["mode_b_tone"]
    assert tone["snr_db"] >= 25.0 and tone["f_src_hz"] == pytest.approx(FC + 0.15 * FS)
    # Mode A breakdown: the analysed window is exactly welch_len(nfft, averages) / fs
    assert ex["window_samples"] == 2048 + 1024 * 7
    assert ex["mode_a_breakdown_mean_ms"]["window_ms"] == pytest.approx(ex["window_samples"] / FS * 1e3, abs=1e-3)
    assert ex["mode_a_breakdown_mean_ms"]["transport_ms"] < 50.0
    b_rows = [r for r in res.table if r["mode"] == "B"]
    assert len(b_rows) == 6 and all(r["status"] == "tespit" for r in b_rows)
    for r in b_rows:
        # causality: the detecting window ends after the switch-on and the message arrives after it
        assert r["acquisition_ms"] >= 0.0 and r["latency_ms"] > r["acquisition_ms"]
        parts = r["acquisition_ms"] + r["processing_ms"] + r["publish_ms"] + r["transport_ms"]
        assert parts == pytest.approx(r["latency_ms"], abs=1e-3)
        assert abs(r["f_det_hz"] - tone["f_src_hz"]) <= tone["tolerance_hz"]
    assert 0 < sum(r["mode"] == "A" for r in res.table) <= 200
    kinds = {f.name: f.kind for f in res.figures}
    assert kinds == {"latency_mode_b_cdf": "hist", "latency_mode_a_cdf": "hist",
                     "latency_mode_a_breakdown": "xy", "latency_mode_b_breakdown": "xy"}
    hist_b = next(f for f in res.figures if f.name == "latency_mode_b_cdf")
    assert [v[1].split(" =")[0] for v in hist_b.vlines] == ["p50", "p95", "p99"]
    assert all(s.style == "bar" for f in res.figures if f.kind == "xy" for s in f.series)
    assert res.conditions["block_size"] == BLOCK and res.conditions["transport"] == "tcp"
    # the node's hardware ppm correction was undone; the device is idle again
    assert ctx.dut.ppm_hw == 0.0 and not ctx.dut.streaming


def test_latency_nonrealtime_sim_warns_and_inproc_transport(tmp_path):
    app = make_app(False, tmp_path, transport="inproc", n_events=2, quick_n_events=2, quick_duration_s=0.4,
                   warmup_s=0.1)
    res, _ = run_latency(app)
    assert res.status == "ok", (res.error, res.extra.get("traceback"))
    assert res.extra["realtime"] is False and res.extra["warning"] == NONREALTIME_WARNING
    assert res.summary.startswith(NONREALTIME_WARNING)
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


def test_latency_rejects_bad_transport(tmp_path):
    app = make_app(False, tmp_path, transport="udp")
    res, _ = run_latency(app)
    assert res.status == "failed" and "transport" in res.error
