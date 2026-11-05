"""Tests of the scan speed measurement (eds.measure.specs.scan_speed) on the simulator.

Ground truth: the plan of an identically configured :class:`Sweeper` (steps), the
real-time simulator's USB model (every flushed / captured sample crosses a 24 MB/s link, so
settle and capture times have a physical floor) and the TCXO error of the simulated device
(apparent frequency of a CW emitter).
"""

from __future__ import annotations

import dataclasses
import statistics
import time

import pytest

from eds.core.config import DeviceConfig, RxConfig
from eds.core.units import fmt_hz
from eds.hal import open_device
from eds.measure.specs.scan_speed import NONREALTIME_WARNING, TIMING_KEYS
from eds.measure.targets import REASON_NONREALTIME
from eds.rx.sweep import Sweeper

from .spec_fixtures_b import assert_result_contract, run_spec, single_sim_app

CW_HZ = 500.3e6
USB_BYTES_PER_S = 24e6
BYTES_PER_SAMPLE = 4


def plan_steps(app, start: float, stop: float, fs: float, rbw: float) -> tuple[int, float]:
    """Steps and RBW of the plan the measurement must have used (independent Sweeper)."""
    dev = open_device(DeviceConfig(name="planner", backend="sim", sim={"realtime": False}))
    try:
        cfg = dataclasses.replace(app.sweep, start_hz=start, stop_hz=stop, sample_rate_hz=fs, bandwidth_hz=fs,
                                  rbw_hz=rbw)
        sw = Sweeper(dev, cfg, app.dsp, app.detect)
        return len(sw.plan()), sw.rbw_hz
    finally:
        dev.close()


@pytest.mark.realtime
def test_scan_speed_quick_realtime_timing_budget_and_detection(tmp_path):
    app = single_sim_app(tmp_path, realtime=True, spec_id="scan_speed",
                         rx=RxConfig(center_freq_hz=915e6, sample_rate_hz=4e6, bandwidth_hz=4e6, gain_db=40.0),
                         emitters=[{"name": "cw", "kind": "cw", "freq_hz": CW_HZ, "power_dbm": -80.0}])
    t0 = time.perf_counter()
    res, ctx = run_spec(app, "scan_speed", quick=True)
    elapsed = time.perf_counter() - t0
    assert_result_contract(res, tmp_path)
    assert elapsed < 20.0
    ex = res.extra
    assert res.unit == "GHz/s" and res.value > 0.0
    assert res.conditions["start_hz"] == 325e6 and res.conditions["stop_hz"] == 825e6
    assert len(ex["configs"]) == 1 and ex["configs"][0]["fs_hz"] == 20e6     # quick: reference config only
    (row,) = res.table
    # value = span / wall-clock sweep duration
    assert res.value == pytest.approx(500e6 / row["duration_s"] / 1e9, rel=1e-9)
    # steps and RBW = the plan of an identical Sweeper
    n_steps, rbw = plan_steps(app, 325e6, 825e6, 20e6, 50e3)
    assert row["n_steps"] == ex["n_steps"] == n_steps and ex["rbw_hz"] == pytest.approx(rbw)
    assert ex["rbw_hz"] <= 50e3
    # the four timing components account for the whole duration (within 10 %)
    parts_s = sum(row[f"{k}_ms"] for k in TIMING_KEYS) * row["n_steps"] / 1e3
    assert parts_s == pytest.approx(row["duration_s"], rel=0.10)
    assert 0.9 <= ex["timing_sum_ratio"] <= 1.0 + 1e-9
    # physical floor of the realtime USB model: the settle flush moves
    # settle_buffers x buffer_size samples and the capture one buffer through 24 MB/s
    floor_settle_ms = 1e3 * res.conditions["settle_buffers"] * ex["buffer_size"] * BYTES_PER_SAMPLE / USB_BYTES_PER_S
    floor_capture_ms = 1e3 * ex["buffer_size"] * BYTES_PER_SAMPLE / USB_BYTES_PER_S
    assert ex["settle_ms"] >= 0.95 * floor_settle_ms
    assert ex["capture_ms"] >= 0.95 * floor_capture_ms
    assert ex["dominant_component"] == "settle"            # stale-buffer flush dominates the Pluto step
    # revisit time of the full official band, extrapolated from the measured rate
    assert ex["revisit_extrapolated"] is True and ex["gap_between_sweeps_s"] is None
    assert ex["revisit_full_s"] == pytest.approx(3.475e9 / (res.value * 1e9), rel=1e-9)
    # the CW emitter is detected at its apparent (uncorrected TCXO) frequency
    true_ppm = ctx.devices[0].true_ppm
    f_app = CW_HZ / (1.0 + true_ppm * 1e-6)
    assert ex["detections"] >= 1
    assert min(abs(f - f_app) for f in ex["detections_hz"]) <= 2.0 * ex["rbw_hz"]
    figs = {f.name: f for f in res.figures}
    bars = figs["scan_speed_step_breakdown"]
    assert [s.style for s in bars.series] == ["bar"] * 4
    # cumulative (stacked) bars: total first, tune last
    tops = [s.y[0] for s in bars.series]
    assert tops == sorted(tops, reverse=True)
    assert tops[0] == pytest.approx(sum(ex[f"{k}_ms"] for k in TIMING_KEYS))
    shot = figs["scan_speed_sweep_spectrum"]
    assert shot.kind == "spectrum" and shot.info["Span"].startswith("500")
    assert any(abs(m[0] * 1e6 - f_app) <= 2.0 * ex["rbw_hz"] for m in shot.markers)


def test_scan_speed_configs_repeats_and_profile_skip(tmp_path):
    configs = [{"fs": 20e6, "rbw_hz": 50e3}, {"fs": 20e6, "rbw_hz": 10e3}, {"fs": 10e6, "rbw_hz": 10e3},
               {"fs": 80e6, "rbw_hz": 50e3}]                       # 80 MS/s: above the AD9363 limit
    params = {"start_hz": 400e6, "stop_hz": 480e6, "configs": configs, "repeats": 2}
    app = single_sim_app(tmp_path, realtime=False, spec_id="scan_speed", params=params)
    res, _ = run_spec(app, "scan_speed", quick=False)
    assert_result_contract(res, tmp_path)
    ex = res.extra
    assert res.summary.startswith(NONREALTIME_WARNING) and ex["warning"] == NONREALTIME_WARNING
    assert REASON_NONREALTIME in res.blockers
    assert [c["fs_hz"] for c in ex["configs"]] == [20e6, 20e6, 10e6]
    # JURY-10: every label states the RBW actually used (ENBW·fs/nfft), never the requested one
    for c in ex["configs"]:
        assert f"RBW {fmt_hz(c['rbw_hz'], 4)}" in c["config"] and f"N = {c['nfft']}" in c["config"]
        assert c["rbw_hz"] < c["rbw_requested_hz"] and fmt_hz(c["rbw_requested_hz"], 4) not in c["config"]
    assert {r["config"] for r in res.table} == {c["config"] for c in ex["configs"]}
    assert "istenen RBW" in ex["skipped_configs"][0]["config"]
    bars = next(f for f in res.figures if f.name == "scan_speed_step_breakdown")
    # every bar is named on the x axis by its configuration; the markers give ms/step and the rate
    assert [t[1].replace("\n", ", ") for t in bars.xticks] == [c["config"] for c in ex["configs"]]
    assert [t[0] for t in bars.xticks] == [1.0, 2.0, 3.0]
    assert [m[2] for m in bars.markers] == [
        f"{sum(c[f'{k}_ms'] for k in TIMING_KEYS):.2f} ms/adım, {c['rate_ghz_per_s']:.3f} GHz/s" for c in ex["configs"]]
    assert res.conditions["rbw_hz"] == ex["rbw_hz"] and res.conditions["rbw_requested_hz"] == 50e3
    assert len(ex["skipped_configs"]) == 1 and ex["skipped_configs"][0]["fs_hz"] == 80e6
    assert "MS/s" in ex["skipped_configs"][0]["reason"]
    assert len(res.table) == 3 * 2
    ref_rows = [r for r in res.table if r["config"] == ex["configs"][0]["config"]]
    assert res.value == pytest.approx(statistics.median(r["rate_ghz_per_s"] for r in ref_rows))
    assert res.uncertainty == pytest.approx(statistics.stdev(r["rate_ghz_per_s"] for r in ref_rows))
    for c in ex["configs"]:
        n, rbw = plan_steps(app, 400e6, 480e6, c["fs_hz"], c["rbw_requested_hz"])
        assert c["n_steps"] == n and c["rbw_hz"] == pytest.approx(rbw) and c["rbw_hz"] <= c["rbw_requested_hz"]
        assert c["gap_s"] is not None and c["gap_s"] >= 0.0
    # half the sample rate -> half the step -> about twice the steps
    assert ex["configs"][2]["n_steps"] >= 1.8 * ex["configs"][0]["n_steps"]
    assert ex["configs"][2]["step_hz"] == pytest.approx(0.5 * ex["configs"][0]["step_hz"], rel=0.01)
    assert ex["gap_between_sweeps_s"] is not None
    assert ex["revisit_full_s"] == pytest.approx(3.475e9 / (res.value * 1e9) + ex["gap_between_sweeps_s"])
    bars = next(f for f in res.figures if f.name == "scan_speed_step_breakdown")
    assert all(s.x == [1.0, 2.0, 3.0] for s in bars.series)


def test_scan_speed_rejects_unknown_config_key(tmp_path):
    app = single_sim_app(tmp_path, realtime=False, spec_id="scan_speed",
                         params={"configs": [{"fs": 20e6, "rbw": 50e3}]})
    res, _ = run_spec(app, "scan_speed", quick=True)
    assert res.status == "failed" and "bilinmeyen anahtar" in res.error
