"""Sensitivity measurement (eds.measure.specs.sensitivity) against simulator ground truth.

Truth: thermal noise density at the input is ``kT + NF`` (NF = 5 dB in the simulator), so
``DANL = −168.975 dBm/Hz``; the Pd = 0.9 level of a bin-centred tone is ``N_RBW + SNR_req``
with ``SNR_req`` derived here independently (Gaussian approximation of the averaged
periodogram bin against the ideal CFAR multiplier ``cfar_alpha``).
"""

from __future__ import annotations

import math

import numpy as np
import pytest

from eds.calib.power import PowerCalTable
from eds.core.units import KT_DBM_HZ
from eds.dsp.stats import cfar_alpha, effective_averages
from eds.measure.base import run_measurement
from eds.measure.specs.sensitivity import (
    isotonic_increasing,
    patnaik_pd,
    required_snr_db,
    wilson_halfwidth,
)

from .spec_fixtures import check_contract, run_spec, sim_app, sim_context, source_enabled

FC, FS, NFFT, AVG = 915e6, 2e6, 4096, 8
RBW = 2.0043529382170475 * FS / NFFT          # Blackman-Harris ENBW
Z90 = 1.2815515655446004                       # standard normal quantile of 0.9


def truth_snr_req_db(pfa: float, min_snr_db: float, passes: int = 2) -> float:
    """Independent estimate of the per-bin SNR for Pd = 0.9: Gaussian approximation of the
    n_eff-average periodogram bin ``(S + N, σ² = (N² + 2SN)/n)`` against ``α·N``."""
    n = effective_averages(AVG, 0.5, "blackmanharris", NFFT)
    alpha = max(cfar_alpha(pfa / passes, n), 10.0 ** (min_snr_db / 10.0))
    lo, hi = 1e-3, 1e3
    for _ in range(200):
        s = math.sqrt(lo * hi)
        if 1.0 + s - Z90 * math.sqrt((1.0 + 2.0 * s) / n) >= alpha:
            hi = s
        else:
            lo = s
    return 10.0 * math.log10(math.sqrt(lo * hi))


# --------------------------------------------------------------------------- helpers
def test_patnaik_pd_matches_monte_carlo_of_averaged_periodogram():
    rng = np.random.default_rng(20261105)
    n, alpha, trials = 8, 4.0, 40000
    for snr_db in (2.0, 5.0, 6.5, 8.0):
        a = math.sqrt(10.0 ** (snr_db / 10.0))
        w = (rng.standard_normal((trials, n)) + 1j * rng.standard_normal((trials, n))) / math.sqrt(2.0)
        x = np.mean(np.abs(a + w) ** 2, axis=1)
        pd_mc = float(np.mean(x > alpha))
        assert patnaik_pd(snr_db, alpha, n) == pytest.approx(pd_mc, abs=0.02)
    s = required_snr_db(0.9, alpha, n)
    assert patnaik_pd(s, alpha, n) == pytest.approx(0.9, abs=1e-4)


def test_isotonic_and_wilson():
    fit = isotonic_increasing(np.array([0.1, 0.0, 0.3, 0.2, 0.9, 1.0]), np.ones(6))
    assert fit == pytest.approx([0.05, 0.05, 0.25, 0.25, 0.9, 1.0])
    fit_w = isotonic_increasing(np.array([0.4, 0.0]), np.array([1.0, 3.0]))
    assert fit_w == pytest.approx([0.1, 0.1])
    # Wilson 95 % interval of 18/20 = [0.699, 0.972]
    assert wilson_halfwidth(0.9, 20) == pytest.approx((0.97213 - 0.69897) / 2.0, abs=1e-3)


# --------------------------------------------------------------------------- measurement
def test_sensitivity_pd_curve_matches_level_equation(tmp_path):
    app = sim_app(tmp_path, fc=FC)
    run = run_spec(app, "sensitivity", quick=True)
    res, dev = run.result, run.dev
    check_contract(res, unit="dBm", min_spectrum_figs=2, render_dir=tmp_path)
    ex = res.extra
    nf_true = dev.sim_params["noise_figure_db"]
    danl_true = KT_DBM_HZ + nf_true
    assert ex["danl_dbm_hz"] == pytest.approx(danl_true, abs=0.7)
    assert ex["nf_db"] == pytest.approx(nf_true, abs=0.7)
    assert ex["calibrated"] is True and ex["level_uncertainty_db"] == 0.0
    # Pd = 0.9 level vs analytic truth (N_RBW + SNR_req from cfar_alpha)
    snr_truth = truth_snr_req_db(app.detect.pfa, app.detect.min_snr_db)
    sens_truth = danl_true + 10.0 * math.log10(RBW) + snr_truth
    assert ex["value_source"] == "pd_curve"
    assert res.value == pytest.approx(sens_truth, abs=2.0)
    assert 0.0 < res.uncertainty < 3.0
    # MDS formula consistent with its own definition and with the Pd curve
    assert ex["mds_rbw_dbm"] == pytest.approx(ex["danl_dbm_hz"] + 10.0 * math.log10(RBW) + ex["snr_req_db"], abs=1e-6)
    assert ex["snr_req_db"] == pytest.approx(snr_truth, abs=1.0)
    assert abs(ex["sens_minus_mds_db"]) < 1.5
    # the tone sits on a bin centre and the in-situ ppm equals the simulated TCXO error
    assert ex["in_situ_ppm"] == pytest.approx(dev.true_ppm, abs=0.01)
    assert (ex["tone_hz"] - FC) / (FS / NFFT) == pytest.approx(round(0.15 * NFFT), abs=1e-9)
    # Pd table: monotone fit, crosses from < pd_stop to >= pd_target, quick trials
    pd_rows = [r for r in res.table if r["bölüm"] == "Pd"]
    assert len(pd_rows) >= 6 and all(r["trials"] == 10 for r in pd_rows)
    fits = [r["pd_fit"] for r in pd_rows]
    assert all(b >= a - 1e-12 for a, b in zip(fits, fits[1:]))
    assert pd_rows[0]["pd"] < 0.2 and pd_rows[-1]["pd"] >= 0.9
    assert any(r["phase"] == "ince" for r in pd_rows)
    # level grid: measured tone power follows the commanded SNR (absolute generator, exact)
    for r in pd_rows:
        assert r["p_in"] == pytest.approx(r["setting"])
    mds_rows = [r for r in res.table if r["bölüm"] == "MDS"]
    assert [r["bandwidth_hz"] for r in mds_rows] == pytest.approx([RBW, 25e3, 200e3, 1e6])
    assert mds_rows[2]["mds"] - mds_rows[1]["mds"] == pytest.approx(10.0 * math.log10(8.0))
    # empirical false alarms on the terminated input are rare (Pfa = 1e-6 per bin)
    assert ex["fa_detections"] <= 1
    assert not run.source_left_on
    assert {f.name for f in res.figures} == {"sensitivity_pd_curve", "sensitivity_at_level", "sensitivity_noise_floor"}
    shot = next(f for f in res.figures if f.name == "sensitivity_at_level")
    assert [s.label for s in shot.series] == ["Spektrum", "CFAR eşiği"]


def test_sensitivity_without_source_reports_uncalibrated_mds(tmp_path):
    app = sim_app(tmp_path, fc=FC, source="none")
    run = run_spec(app, "sensitivity", quick=True)
    res, dev = run.result, run.dev
    check_contract(res, unit="dBFS", min_spectrum_figs=1, min_xy_figs=0)
    ex = res.extra
    assert ex["value_source"] == "mds_formula" and ex["calibrated"] is False
    assert "nf_db" not in ex and "level_uncertainty_db" not in ex
    g = dev.gain_range(FC).max_db
    snr = truth_snr_req_db(app.detect.pfa, app.detect.min_snr_db)
    expected = dev.noise_density_dbfs_hz(g) + 10.0 * math.log10(RBW) + snr
    assert res.value == pytest.approx(expected, abs=1.0)
    assert ex["danl_dbfs_hz"] == pytest.approx(dev.noise_density_dbfs_hz(g), abs=0.5)
    assert "kalibrasyonsuz" in res.summary and "MDS" in res.summary
    assert res.uncertainty is None


def test_sensitivity_with_power_calibration_table(tmp_path):
    """Level reference from the stored table: DANL in dBm from the table, the generator's
    (assumed) level accuracy enters the uncertainty."""
    app = sim_app(tmp_path, fc=FC)
    with sim_context(app, quick=True) as ctx:
        g = ctx.dut.gain_range(FC).max_db
        offset = -(g + ctx.dut.sim_params["gain_offset_db"])           # exact simulator offset
        ctx.cal.power = PowerCalTable([(900e6, g, offset), (950e6, g, offset)])
        res = run_measurement("sensitivity", ctx)
        left_on = source_enabled(ctx)
    check_contract(res, unit="dBm", min_spectrum_figs=2)
    ex = res.extra
    assert ex["level_ref"] == "kalibrasyon tablosu" and ex["level_offset_db"] == pytest.approx(offset)
    assert ex["danl_dbm_hz"] == pytest.approx(KT_DBM_HZ + 5.0, abs=0.7)
    assert ex["level_uncertainty_db"] == 1.0
    assert res.uncertainty >= 1.0
    assert not left_on
