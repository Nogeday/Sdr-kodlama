"""Dynamic-range measurement (eds.measure.specs.dynamic_range) against simulator ground truth.

Truth: noise in one RBW = thermal (``kT + NF`` through the level equation) ⊕ white ADC floor
(transition noise + 12-bit rounding); the strongest non-clipping CW tone has its per-rail
peak just below ``clip_peak_dbfs`` (−0.5 dBFS); the dominant spur of a −3 dBFS carrier is
its IQ image at ``IRR`` (quadrature tracking on) unless the −85 dBFS digital spur is
stronger; the level equation is ``dBFS = dBm + G − 5``.
"""

from __future__ import annotations

import math

import pytest

from eds.hal.sim import signals as sg
from eds.measure.specs.dynamic_range import DynamicRangeMeasurement

from .spec_fixtures import check_contract, n_rbw_dbfs, run_spec, sim_app

FC, FS, NFFT = 915e6, 4e6, 8192
RBW = 2.0043529382170475 * FS / NFFT
SPUR_DBFS, SPUR_HZ = -85.0, 1.234e6           # simulator default digital spur


def test_dynamic_range_matches_level_equation_and_sfdr_truth(tmp_path):
    run = run_spec(sim_app(tmp_path, fc=FC, fs=FS), "dynamic_range", quick=True)
    res, dev = run.result, run.dev
    check_contract(res, unit="dB", min_spectrum_figs=2, min_xy_figs=2, render_dir=tmp_path)
    ex = res.extra
    sp = dev.sim_params
    gain = DynamicRangeMeasurement.default_params["gain"]
    # (a) noise floor per RBW and DR_inst from the level equation
    n_true = n_rbw_dbfs(dev, gain, RBW, FS)
    assert ex["noise_rbw_dbfs"] == pytest.approx(n_true, abs=0.5)
    assert -1.1 <= ex["p_max_dbfs"] <= -0.4                  # bisection to 0.5 dB below the −0.5 dBFS peak limit
    assert res.value == pytest.approx(-0.75 - n_true, abs=1.5)
    assert res.value == pytest.approx(ex["p_max_dbfs"] - ex["noise_rbw_dbfs"])
    assert ex["p_max_lower_bound"] is False and ex["clip_input_unit"] == "dBm"
    # clip input in dBm = P_max(dBFS) − G − gain_offset (exact in-situ reference on the simulator)
    assert ex["clip_input"] == pytest.approx(ex["p_max_dbfs"] - gain - sp["gain_offset_db"], abs=0.1)
    # (b) SFDR: dominant spur is the IQ image (tracking on), excluding it the −85 dBFS spur
    irr_on = sg.image_rejection_db(sp["iq_residual_gain_db"], sp["iq_residual_phase_deg"])
    carrier = ex["carrier_dbfs"]
    assert carrier == pytest.approx(-3.0, abs=0.6)
    assert ex["sfdr_dbc"] == pytest.approx(min(irr_on, carrier - SPUR_DBFS), abs=2.0)
    assert ex["dominant_spur_is_image"] is True and ex["sfdr_noise_limited"] is False
    assert ex["dominant_spur_hz"] == pytest.approx(2 * FC - ex["f_carrier_hz"], abs=2 * FS / NFFT)
    assert ex["sfdr_dbfs"] == pytest.approx(-ex["dominant_spur_dbfs"])
    assert ex["sfdr_ex_image_dbc"] == pytest.approx(carrier - SPUR_DBFS, abs=2.0)
    assert ex["spur_ex_image_hz"] - FC == pytest.approx(SPUR_HZ, abs=2 * FS / NFFT)
    # (c) total DR: P_in,max(G_min) − MDS(G_max)
    g_min, g_max = ex["gain_min_db"], ex["gain_max_db"]
    assert (g_min, g_max) == (-1.0, 73.0)
    p_max_in_true = -0.75 - g_min - sp["gain_offset_db"]
    assert ex["p_max_in_gmin_dbm"] == pytest.approx(p_max_in_true, abs=0.6)
    mds_true = n_rbw_dbfs(dev, g_max, RBW, FS) - g_max - sp["gain_offset_db"] + ex["snr_req_db"]
    assert ex["mds_gmax_dbm"] == pytest.approx(mds_true, abs=0.5)
    assert ex["dr_total_db"] == pytest.approx(p_max_in_true - mds_true, abs=1.0)
    assert 5.0 < ex["snr_req_db"] < 8.0 and ex["dr_total_method"] == "ölçüm"
    # linearity: unclipped points follow the level equation 1:1
    lin = [r for r in res.table if r["bölüm"] == "doğrusallık"]
    for r in lin:
        if not r["clipped"] and r["tone_dbfs"] > n_true + 20.0:
            assert r["tone_dbfs"] - r["p_in"] == pytest.approx(gain + sp["gain_offset_db"], abs=0.3)
    assert any(r["clipped"] for r in lin) and any(r["phase"] == "ikiye bölme" for r in lin)
    # noise vs gain: high gain is thermal-limited, low gain ADC-limited
    by_gain = {r["gain_db"]: r["noise_rbw_dbfs"] for r in res.table if r["bölüm"] == "gürültü-kazanç"}
    assert by_gain[73.0] == pytest.approx(n_rbw_dbfs(dev, 73.0, RBW, FS), abs=0.5)
    assert by_gain[-1.0] == pytest.approx(n_rbw_dbfs(dev, -1.0, RBW, FS), abs=0.5)
    assert not run.source_left_on


def test_dynamic_range_without_source_uses_full_scale_and_level_model(tmp_path):
    run = run_spec(sim_app(tmp_path, fc=FC, fs=FS, source="none"), "dynamic_range", quick=True)
    res, dev = run.result, run.dev
    check_contract(res, unit="dB", min_spectrum_figs=1, min_xy_figs=1)
    ex = res.extra
    n_true = n_rbw_dbfs(dev, 40.0, RBW, FS)
    assert res.value == pytest.approx(-n_true, abs=0.5)
    assert ex["p_max_source"].startswith("varsayım") and "sfdr_dbc" not in ex
    assert "seviye modeli" in ex["dr_total_method"]
    # the level model (dBFS = dBm + G − 5) is the simulator's own: estimate ≈ truth with P_max = 0 dBFS
    mds = n_rbw_dbfs(dev, 73.0, RBW, FS) - 73.0 + 5.0 + ex["snr_req_db"]
    assert ex["dr_total_db"] == pytest.approx((0.0 + 1.0 + 5.0) - mds, abs=0.5)
    assert "SFDR ölçülemedi" in res.summary and res.uncertainty is None
    assert math.isfinite(ex["noise_rbw_dbfs"])
