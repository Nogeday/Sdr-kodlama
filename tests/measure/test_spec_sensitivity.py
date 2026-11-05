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
from eds.dsp.stats import cfar_alpha, effective_averages, gamma_quantile
from eds.measure.base import run_measurement
from eds.measure.common import LevelRef
from eds.measure.specs.sensitivity import (
    fit_pd_probit,
    isotonic_increasing,
    norm_cdf,
    norm_ppf,
    patnaik_pd,
    required_snr_db,
    scalloping_loss_db,
    stratified_bin_offsets,
    wilson_halfwidth,
)
from eds.measure.stimulus import (
    ANCHOR_UNCERTAINTY_DB,
    ATTENUATION_LINEARITY_DB_PER_DB,
    DEFAULT_LEVEL_UNCERTAINTY_DB,
    level_uncertainty_budget,
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
    # Pd = 0.9 level vs analytic truth (N_RBW + SNR_req from cfar_alpha); the tone is spread over
    # ±0.5 bin, so the frequency-averaged value sits about the mean scalloping loss above it
    snr_truth = truth_snr_req_db(app.detect.pfa, app.detect.min_snr_db)
    sens_truth = danl_true + 10.0 * math.log10(RBW) + snr_truth
    assert ex["value_source"] == "pd_curve"
    assert res.value == pytest.approx(sens_truth + ex["scalloping_mean_db"], abs=2.0)
    assert ex["bin_offset"] == "uniform" and "düzgün dağılımlı" in res.summary
    assert ex["sensitivity_worst_case"] - res.value == pytest.approx(
        ex["scalloping_worst_db"] - ex["scalloping_mean_db"])
    # statistical uncertainty = 95 % profile-likelihood interval of the probit crossing
    lo, hi = ex["snr_ci95_db"]
    assert lo < ex["snr_at_sensitivity_db"] < hi
    assert ex["stat_uncertainty_db"] == pytest.approx(max(ex["snr_at_sensitivity_db"] - lo, hi - ex["snr_at_sensitivity_db"]))
    assert 0.3 < res.uncertainty < 3.0 and res.uncertainty == pytest.approx(ex["stat_uncertainty_db"])
    assert ex["sensitivity_ci95"][0] < res.value < ex["sensitivity_ci95"][1]
    # MM-12: the design Pfa is shown next to what the false-alarm test can actually demonstrate
    n_fa = ex["fa_bins_tested"]
    assert ex["pfa_empirical_upper95"] == pytest.approx(gamma_quantile(0.95, ex["fa_detections"] + 1.0) / n_fa, rel=1e-6)
    assert f"Pfa tasarım {app.detect.pfa:g}, ölçülen ≤ {ex['pfa_empirical_upper95']:.1e}" in res.summary
    # MDS formula consistent with its own definition and with the Pd curve
    assert ex["mds_rbw_dbm"] == pytest.approx(ex["danl_dbm_hz"] + 10.0 * math.log10(RBW) + ex["snr_req_db"], abs=1e-6)
    assert ex["snr_req_db"] == pytest.approx(snr_truth, abs=1.0)
    assert abs(ex["sens_minus_mds_db"]) < 1.5
    # the tone sits on a bin centre and the in-situ ppm equals the simulated TCXO error
    assert ex["in_situ_ppm"] == pytest.approx(dev.true_ppm, abs=0.01)
    assert (ex["tone_hz"] - FC) / (FS / NFFT) == pytest.approx(round(0.15 * NFFT), abs=1e-9)
    # Pd table: monotone (probit) fit, crosses from < pd_stop to >= pd_target, quick trials
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
    assert ex["level_uncertainty_db"] == 1.0 and "kalibrasyon tablosu" in ex["level_uncertainty_budget"]
    assert res.uncertainty >= 1.0
    assert not left_on


# --------------------------------------------------------------------------- MM-07: Pd fit
def _procedure_levels(truth: float, rng: np.random.Generator) -> np.ndarray:
    """SNR grid like the measurement: 2 dB coarse descent to Pd < 0.2, 0.5 dB fine grid."""
    alpha, n_eff = 10 ** (6.15 / 10), 7.98
    start = truth + 4.0 + rng.uniform(-1.0, 1.0)
    coarse = [start]
    while patnaik_pd(coarse[-1], alpha, n_eff) >= 0.2:
        coarse.append(coarse[-1] - 2.0)
    fine = np.arange(coarse[-1] + 0.5, start - 1e-9, 0.5)
    return np.unique(np.round(np.concatenate([coarse, fine]), 6))


def test_probit_fit_interval_has_nominal_coverage_unlike_the_two_point_slope():
    """Monte Carlo on the detector model of the finding (α = 6.15 dB, n_eff = 7.98, 10 trials):
    the 95 % profile-likelihood interval covers the true crossing ~95 % of the time; the old
    'Wilson half-width / slope between two neighbouring isotonic points' interval does not."""
    alpha, n_eff, trials = 10 ** (6.15 / 10), 7.98, 10
    truth = required_snr_db(0.9, alpha, n_eff)
    rng = np.random.default_rng(20261105)
    cover_new = cover_old = 0
    runs = 150
    for _ in range(runs):
        x = _procedure_levels(truth, rng)
        h = rng.binomial(trials, [patnaik_pd(v, alpha, n_eff) for v in x])
        fit = fit_pd_probit(x, h, np.full(x.size, trials), 0.9)
        cover_new += fit.ci_lo <= truth <= fit.ci_hi
        iso = isotonic_increasing(h / trials, np.full(x.size, float(trials)))
        i = int(np.flatnonzero(iso >= 0.9)[0])
        slope = (iso[i] - iso[i - 1]) / (x[i] - x[i - 1])
        x90 = x[i - 1] + (0.9 - iso[i - 1]) / slope
        cover_old += abs(x90 - truth) <= wilson_halfwidth(0.9, trials) / slope
    assert 0.91 <= cover_new / runs <= 0.99                 # nominal 95 % (binomial sd ≈ 0.018 for 150 runs)
    assert cover_old / runs < 0.85                          # the old "± 0.3 dB" style interval: ≈ 75 %


def test_probit_fit_on_the_saturated_example_is_not_overconfident():
    """Finding example: 7/10 at 5.5 dB next to 10/10 at 6.0 dB gave '± 0.3 dB'."""
    fit = fit_pd_probit([4.0, 4.5, 5.0, 5.5, 6.0, 8.0], [1, 2, 4, 7, 10, 10], [10] * 6, 0.9)
    assert 5.0 < fit.x_target < 6.5 and not fit.open_interval
    assert fit.halfwidth_db > 0.5 and fit.ci_lo < fit.x_target < fit.ci_hi
    assert float(fit.pd(fit.x_target, 0.9)) == pytest.approx(0.9, abs=1e-6)
    assert fit.pd([fit.x_target - 1.0, fit.x_target + 1.0], 0.9).tolist() == sorted(
        fit.pd([fit.x_target - 1.0, fit.x_target + 1.0], 0.9).tolist())
    assert norm_ppf(0.9) == pytest.approx(1.2815515655446004, abs=1e-6)
    assert float(norm_cdf(-1.959963984540054)) == pytest.approx(0.025, abs=1e-7)


# --------------------------------------------------------------------------- RF-06: scalloping
def test_bin_offsets_and_scalloping_loss():
    offs = stratified_bin_offsets(10)
    assert offs == pytest.approx(np.arange(-0.45, 0.46, 0.1)) and abs(float(np.mean(offs))) < 1e-12
    loss = scalloping_loss_db("blackmanharris", NFFT, [0.0, 0.2, 0.4, 0.5])
    assert loss == pytest.approx([0.0, 0.13, 0.53, 0.83], abs=0.01)
    mean = float(np.mean(scalloping_loss_db("blackmanharris", NFFT, np.linspace(-0.5, 0.5, 201))))
    assert mean == pytest.approx(0.28, abs=0.01)


def test_sensitivity_spreads_the_tone_over_the_bin(tmp_path):
    """Every trial of a Pd level uses its own offset inside ±0.5 bin (RF-06)."""
    app = sim_app(tmp_path, fc=FC)
    with sim_context(app, quick=True) as ctx:
        seen: list[float] = []
        orig = ctx.source.set_frequency
        ctx.source.set_frequency = lambda hz: (seen.append(float(hz)), orig(hz))[1]
        res = run_measurement("sensitivity", ctx)
        e = ctx.dut.true_ppm * 1e-6
    assert res.status == "ok", res.error
    ex = res.extra
    bins = {round((f / (1 + e) - ex["tone_hz"]) / (FS / NFFT), 2) + 0.0 for f in seen
            if abs(f / (1 + e) - ex["tone_hz"]) < FS / NFFT}
    trial_offsets = {round(float(v), 2) for v in stratified_bin_offsets(10)}
    assert bins - {0.0} == trial_offsets            # 0: the high-SNR probe and the screenshot (bin centre)


# --------------------------------------------------------------------------- MM-10: level budget
class _Ctx:
    def __init__(self, source):
        self.source = source


class _AnchoredTx:
    name, absolute_power, anchor_dbm, path_loss_db = "pluto_tx", True, -37.5, 0.0


class _Generator:
    name, absolute_power = "scpi", True


def test_level_uncertainty_budget_is_source_specific():
    insitu = LevelRef(0.0, "kaynak referanslı (in-situ)", True, "dBm", setting=-93.0)
    # anchored Pluto TX: anchor ⊕ 2 %/dB of extrapolation below it
    u, txt = level_uncertainty_budget(_Ctx(_AnchoredTx()), insitu, None, setting=-133.0)
    assert u == pytest.approx(math.hypot(ANCHOR_UNCERTAINTY_DB, ATTENUATION_LINEARITY_DB_PER_DB * 95.5))
    assert u > 2.0 and "çapalı" in txt
    u_probe, _ = level_uncertainty_budget(_Ctx(_AnchoredTx()), insitu, None)          # default: probe setting
    assert u_probe == pytest.approx(math.hypot(ANCHOR_UNCERTAINTY_DB, ATTENUATION_LINEARITY_DB_PER_DB * 55.5))
    assert level_uncertainty_budget(_Ctx(_AnchoredTx()), LevelRef(0.0, "x", True, "dBm"), None)[0] is None
    # lab generator, calibration table, explicit override, uncalibrated
    assert level_uncertainty_budget(_Ctx(_Generator()), insitu, None)[0] == DEFAULT_LEVEL_UNCERTAINTY_DB
    table = LevelRef(0.0, "kalibrasyon tablosu", True, "dBm")
    assert level_uncertainty_budget(_Ctx(_AnchoredTx()), table, None)[0] == DEFAULT_LEVEL_UNCERTAINTY_DB
    assert level_uncertainty_budget(_Ctx(_AnchoredTx()), insitu, 0.4, setting=-133.0)[0] == 0.4
    unc = LevelRef(0.0, "kalibrasyonsuz (dBFS)", False, "dBFS")
    assert level_uncertainty_budget(_Ctx(None), unc, None)[0] is None
