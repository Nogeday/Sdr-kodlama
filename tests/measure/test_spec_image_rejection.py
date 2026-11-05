"""Image-rejection measurement (eds.measure.specs.image_rejection) against the simulator's IQ
imbalance model: ``IRR = 10 log10((1 + 2g cos φ + g²)/(1 − 2g cos φ + g²))`` with the residual
imbalance when quadrature tracking is on and the raw imbalance when it is off."""

from __future__ import annotations

import math

import pytest

from eds.calib.store import CalibrationStore
from eds.hal.sim import signals as sg
from eds.measure.base import run_measurement

from .spec_fixtures import check_contract, run_spec, sim_app, sim_context, source_enabled

FC, FS = 915e6, 4e6


def test_image_rejection_tracking_on_off_matches_iq_model(tmp_path):
    run = run_spec(sim_app(tmp_path, fc=FC, fs=FS), "image_rejection", quick=True)
    res, dev = run.result, run.dev
    check_contract(res, unit="dB", min_spectrum_figs=2, render_dir=tmp_path)
    sp = dev.sim_params
    irr_on = sg.image_rejection_db(sp["iq_residual_gain_db"], sp["iq_residual_phase_deg"])
    irr_off = sg.image_rejection_db(sp["iq_gain_db"], sp["iq_phase_deg"])
    ex = res.extra
    assert res.value == pytest.approx(irr_on, abs=1.0)
    assert ex["irr_off_worst_db"] == pytest.approx(irr_off, abs=1.0)
    assert ex["irr_on_mean_db"] == pytest.approx(irr_on, abs=0.5)
    assert ex["irr_off_mean_db"] == pytest.approx(irr_off, abs=0.5)
    assert ex["improvement_db"] == pytest.approx(irr_on - irr_off, abs=1.5)
    assert ex["lower_bounds_on"] == 0 and ex["lower_bounds_off"] == 0
    rows = res.table
    assert len(rows) == 2 * 2 * 5                      # tracking × side × offset
    assert {r["offset_frac"] for r in rows} == {s * f for s in (1, -1) for f in (0.05, 0.1, 0.2, 0.3, 0.4)}
    for r in rows:
        truth = irr_on if r["tracking"] == "açık" else irr_off
        assert r["irr_db"] == pytest.approx(truth, abs=1.0)
        assert r["f_image_hz"] == pytest.approx(2 * FC - r["f_tone_hz"])
        assert r["tone_dbfs"] == pytest.approx(-10.0, abs=1.5)         # −1 dB analog roll-off at 0.4·fs
    # tracking restored ON, source off
    assert dev.get_tracking()["quadrature"] is True
    assert not run.source_left_on


def test_noise_limited_image_is_reported_as_lower_bound(tmp_path):
    """No residual imbalance → the image is pure noise: every ON point is a flagged lower bound."""
    app = sim_app(tmp_path, fc=FC, fs=FS, sim={"iq_residual_gain_db": 0.0, "iq_residual_phase_deg": 0.0})
    res = run_spec(app, "image_rejection", quick=True).result
    check_contract(res, unit="dB", min_spectrum_figs=2)
    on = [r for r in res.table if r["tracking"] == "açık"]
    assert all(r["lower_bound"] for r in on)
    assert res.extra["irr_on_worst_is_bound"] is True and res.extra["lower_bounds_on"] == len(on)
    for r in on:
        assert r["irr_db"] == pytest.approx(r["tone_dbfs"] - r["image_dbfs"])
        assert r["image_dbfs"] < r["noise_band_dbfs"] + 6.0
        # bound tightened: tone re-measured near full scale (−3 dBFS, −1 dB roll-off at 0.4·fs)
        assert r["boosted"] and r["tone_dbfs"] == pytest.approx(-3.0, abs=1.5)
    assert res.value > 75.0 and "≥" in res.summary
    off = [r for r in res.table if r["tracking"] == "kapalı"]
    assert not any(r["lower_bound"] or r["boosted"] for r in off)


def test_save_stores_worst_on_irr_and_skips_invalid_fc(tmp_path):
    params = {"image_rejection": {"fcs": [915e6, 100e6], "save": True, "offsets_frac": [0.1, 0.3]}}
    app = sim_app(tmp_path, fc=FC, fs=FS, params=params)
    with sim_context(app, quick=True) as ctx:
        res = run_measurement("image_rejection", ctx)
        serial = ctx.dut.info.serial
        saved = dict(ctx.cal.image_rejection_db)
        assert not source_enabled(ctx)
    check_contract(res, unit="dB", min_spectrum_figs=2)
    assert "100 MHz atlandı" in res.summary
    assert saved == {"915000000": pytest.approx(res.value)}
    stored = CalibrationStore(str(tmp_path / "cal")).load(serial)
    assert stored.image_rejection_db["915000000"] == pytest.approx(res.value)
    assert res.extra["saved_to"].endswith(".json")
    assert len(res.table) == 2 * 2 * 2 and math.isfinite(res.extra["irr_off_worst_db"])


def test_image_rejection_requires_a_source(tmp_path):
    res = run_spec(sim_app(tmp_path, fc=FC, fs=FS, source="none"), "image_rejection").result
    assert res.status == "skipped" and "kaynağı gerekli" in res.error
