"""Instantaneous-bandwidth measurement (eds.measure.specs.inst_bw) against the simulator's
analytic receiver response ``SimRxDevice.response_db`` (analog Butterworth × digital chain)."""

from __future__ import annotations

import numpy as np
import pytest

from eds.measure.base import run_measurement
from eds.measure.specs.inst_bw import bracket, crossings

from .spec_fixtures import (
    DelayedSimSource,
    check_contract,
    replace_source,
    sim_app,
    sim_context,
    source_enabled,
)

FC = 915e6


def truth_width(dev, fs: float, bw: float, level_db: float) -> float:
    """Analytic width at ``level_db`` of the (even) response for the given (fs, bw)."""
    dev.set_sample_rate(fs)
    dev.set_bandwidth(bw)
    lo, hi = 0.0, fs / 2.0
    for _ in range(80):
        mid = 0.5 * (lo + hi)
        if dev.response_db(mid) >= level_db:
            lo = mid
        else:
            hi = mid
    return 2.0 * lo


def truth_flatness(dev, fs: float, bw: float, frac: float = 0.4) -> float:
    dev.set_sample_rate(fs)
    dev.set_bandwidth(bw)
    r = dev.response_db(np.linspace(-frac * fs, frac * fs, 2001))
    return float(np.max(r) - np.min(r))


def test_crossings_and_bracket_on_synthetic_response():
    f = np.linspace(-10.0, 10.0, 41)
    r = -3.0 * (f / 6.2) ** 2                         # exact −3 dB edges at ±6.2 (between grid points)
    e = crossings(f, r, -3.0)
    assert not e.limited and e.lo_hz == pytest.approx(-6.2, abs=0.01) and e.hi_hz == pytest.approx(6.2, abs=0.01)
    assert bracket(f, r, -3.0, upper=True) == (6.0, 6.5)
    assert bracket(f, r, -3.0, upper=False) == (-6.0, -6.5)
    flat = crossings(f, np.zeros_like(f), -3.0)
    assert flat.limited and flat.width_hz == pytest.approx(20.0)


def test_inst_bw_tone_stepping_matches_analytic_response(tmp_path):
    app = sim_app(tmp_path, fc=FC)
    with sim_context(app, quick=False) as ctx:
        res = run_measurement("inst_bw", ctx)
        left_on = source_enabled(ctx)
        check_contract(res, unit="MHz", min_spectrum_figs=3, render_dir=tmp_path)
        cfgs = res.extra["configs"]
        assert [(c["fs_hz"], c["bw_hz"]) for c in cfgs] == [(20e6, 20e6), (10e6, 10e6), (5e6, 5e6)]
        widths = []
        for c in cfgs:
            fs, bw = c["fs_hz"], c["bw_hz"]
            w3, w1 = truth_width(ctx.dut, fs, bw, -3.0), truth_width(ctx.dut, fs, bw, -1.0)
            widths.append(w3)
            assert c["bw_3db_hz"] == pytest.approx(w3, rel=0.01)
            assert c["bw_1db_hz"] == pytest.approx(w1, rel=0.01)
            assert c["f_hi_3db_hz"] == pytest.approx(-c["f_lo_3db_hz"], rel=0.01)     # symmetric response
            assert c["flatness_db"] == pytest.approx(truth_flatness(ctx.dut, fs, bw), abs=0.2)
            assert c["in_situ_ppm"] == pytest.approx(ctx.dut.true_ppm, abs=0.05)
            assert not c["bw_3db_lower_bound"] and c["method"] == "ton"
    assert res.value == pytest.approx(max(widths) / 1e6, rel=0.01)
    assert res.extra["capped_at_profile"] is False and res.extra["method"] == "ton adımlama"
    pts = [r for r in res.table if r["bölüm"] == "nokta" and r["config"] == 0]
    assert len(pts) >= 41 and sum(r["phase"] == "kenar" for r in pts) >= 6
    assert max(abs(r["f_bb_hz"]) for r in pts) == pytest.approx(0.49 * 20e6, rel=0.01)
    assert min(abs(r["f_bb_hz"]) for r in pts) > 2 * 20e6 / 4096
    assert not left_on
    # JURY-10b: the screenshot marks the interpolated −3 dB edges (vertical lines); the marker is
    # the nearest measured tone point and states its own normalized response
    shots = [f for f in res.figures if f.name.startswith("inst_bw_screenshot_")]
    assert len(shots) == len(cfgs)
    for f, c in zip(shots, cfgs):
        assert [v[1] for v in f.vlines] == ["−3 dB kenarı"] * 2
        assert sorted(v[0] for v in f.vlines) == pytest.approx([(FC + c["f_lo_3db_hz"]) / 1e6,
                                                               (FC + c["f_hi_3db_hz"]) / 1e6])
        (_, _, text), = f.markers
        assert text.startswith("en yakın ton noktası (−") and text.endswith(" dB)")
        assert -6.0 <= float(text.split("(")[1].split(" dB")[0].replace("−", "-")) <= 0.0
        assert f.info["−3 dB BW"] == f"{c['bw_3db_hz'] / 1e6:.2f} MHz"


def test_inst_bw_noise_shape_fallback_without_source(tmp_path):
    app = sim_app(tmp_path, fc=FC, source="none")
    with sim_context(app, quick=True) as ctx:
        res = run_measurement("inst_bw", ctx)
        check_contract(res, unit="MHz", min_spectrum_figs=1)
        assert res.extra["method"] == "gürültü şekli (yaklaşık)"
        assert "gürültü şekli" in res.method and "gürültü şekli" in res.summary
        assert len(res.extra["configs"]) == 1                     # quick: first configuration only
        w3 = truth_width(ctx.dut, 20e6, 20e6, -3.0)
    assert res.value * 1e6 == pytest.approx(w3, rel=0.02)


def test_inst_bw_skips_configuration_outside_profile(tmp_path):
    cfgs = [[20e6, 30e6], [10e6, 10e6]]                            # 30 MHz analog BW > AD9363 20 MHz
    app = sim_app(tmp_path, fc=FC, params={"inst_bw": {"configs": cfgs, "quick_configs": cfgs}})
    with sim_context(app, quick=True) as ctx:
        res = run_measurement("inst_bw", ctx)
        w3 = truth_width(ctx.dut, 10e6, 10e6, -3.0)
    check_contract(res, unit="MHz", min_spectrum_figs=1)
    assert [(c["fs_hz"], c["bw_hz"]) for c in res.extra["configs"]] == [(10e6, 10e6)]
    assert "atlandı" in res.summary and "profil ihlali" in res.summary
    assert res.value * 1e6 == pytest.approx(w3, rel=0.01)


def test_inst_bw_with_a_slow_switching_source_measures_the_new_tone_only(tmp_path):
    """MM-01: a source whose on/off reaches the RF 80 ms after the call (Pluto TX libiio queue at
    20 MS/s RX: far longer than the RX stale-buffer window). Every point waits the source's
    settle time, so the response still matches the analytic truth."""
    cfg = [[10e6, 10e6]]
    params = {"inst_bw": {"configs": cfg, "quick_configs": cfg, "quick_points": 11, "quick_refine_steps": 1}}
    app = sim_app(tmp_path, fc=FC, params=params)
    with sim_context(app, quick=True) as ctx:
        replace_source(ctx, lambda w: DelayedSimSource(w, delay_s=0.08, settle_s=0.1, freq_hz=FC))
        res = run_measurement("inst_bw", ctx)
        w3 = truth_width(ctx.dut, 10e6, 10e6, -3.0)
        settle = res.conditions.get("source_settle_s")
    assert res.status == "ok", res.error
    c = res.extra["configs"][0]
    assert c["bw_3db_hz"] == pytest.approx(w3, rel=0.02)
    assert c["flatness_db"] < 3.0
    assert settle == pytest.approx(0.1)
