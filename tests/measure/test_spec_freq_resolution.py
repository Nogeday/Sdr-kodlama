"""Frequency-resolution measurement (eds.measure.specs.freq_resolution) against ground truth:
the window's line shape (RBW, −3 dB line width, two-tone dip) and the simulator's TCXO error
(apparent frequency ``f / (1 + e)``, ARCHITECTURE ppm convention)."""

from __future__ import annotations

import numpy as np
import pytest

from eds.calib.ppm import PpmEstimate
from eds.dsp.windows import get_window, window_info
from eds.measure.base import run_measurement
from eds.measure.specs.freq_resolution import analytic_dip_db, dip_analysis

from .spec_fixtures import check_contract, run_spec, sim_app, sim_context, source_enabled

FC, FS, NFFT = 1e9, 2e6, 4096
ENBW = window_info("blackmanharris", NFFT).enbw_bins
RBW = ENBW * FS / NFFT


def window_line_3db_bins(name: str = "blackmanharris", n: int = NFFT, zp: int = 256) -> float:
    """−3 dB width (bins) of the window's power line, from a heavily zero-padded DFT."""
    w = get_window(name, n)
    line = np.abs(np.fft.fft(w, n * zp)) ** 2
    line = line / line[0]
    k = int(np.argmax(line < 0.5))
    x0, x1 = (k - 1) / zp, k / zp
    y0, y1 = 10 * np.log10(line[k - 1]), 10 * np.log10(line[k])
    return 2.0 * (x0 + (-3.0103 - y0) / (y1 - y0) * (x1 - x0))


def incoherent_resolution_rbw() -> float:
    seps = np.linspace(2.0, 4.0, 401)
    dips = analytic_dip_db("blackmanharris", NFFT, seps)
    return float(seps[np.argmax(dips >= 3.0)]) / ENBW


def test_dip_analysis_and_analytic_dip():
    f = np.linspace(-5.0, 5.0, 1001)
    p = np.exp(-((f + 1.5) ** 2)) + np.exp(-((f - 1.5) ** 2))
    d = dip_analysis(f, p, -1.5, 1.5, 0.5)
    assert d["two_peaks"] and d["f_dip_hz"] == pytest.approx(0.0, abs=0.01)
    assert d["dip_db"] == pytest.approx(10 * np.log10(p.max() / p[500]), abs=1e-6)
    assert dip_analysis(f, np.exp(-f ** 2), -0.2, 0.2, 0.5)["two_peaks"] is False
    # Blackman-Harris: equal incoherent tones resolve (3 dB dip) at ≈ 1.3 × RBW
    assert 1.2 < incoherent_resolution_rbw() < 1.45
    assert analytic_dip_db("blackmanharris", NFFT, np.array([1.0]))[0] == 0.0


def test_freq_resolution_two_tone_line_width_and_accuracy(tmp_path):
    app = sim_app(tmp_path, fc=FC, fs=FS)
    with sim_context(app, quick=True) as ctx:
        dev = ctx.dut
        e = dev.true_ppm * 1e-6
        ctx.cal.ppm = PpmEstimate(ppm=dev.true_ppm, std_ppm=0.001, f_measured_hz=FC / (1 + e), f_ref_hz=FC,
                                  snr_db=40.0, n=2 ** 18, method="cw")
        res = run_measurement("freq_resolution", ctx)
        left_on = source_enabled(ctx)
        single_tone = ctx.source.emitter_names == [ctx.source.name]
    check_contract(res, unit="Hz", min_spectrum_figs=3, render_dir=tmp_path)
    ex = res.extra
    assert ex["rbw_hz"] == pytest.approx(RBW, rel=1e-12)
    # (b) two-tone resolution: physical bounds and the window's incoherent analytic value
    assert 0.8 * RBW <= res.value <= 2.5 * RBW
    assert ex["min_separation_rbw"] == pytest.approx(incoherent_resolution_rbw(), abs=0.25)
    assert ex["dip_at_min_db"] >= 3.0 > ex["dip_at_max_unresolved_db"]
    assert res.uncertainty == pytest.approx(res.value - ex["max_unresolved_hz"]) and res.uncertainty > 0
    rows = [r for r in res.table if r["bölüm"] == "iki-ton"]
    assert len(rows) == 12 + 3
    assert all(r["resolved"] for r in rows if r["sep_hz"] >= res.value - 1e-6)
    assert not any(r["resolved"] for r in rows if r["sep_hz"] < res.value - 1e-6 and r["phase"] == "ikiye bölme")
    # (a) single-tone line −3 dB width = the window's analytic line width
    assert ex["line_3db_bins"] == pytest.approx(window_line_3db_bins(), rel=0.03)
    # (c) frequency accuracy at 1 GHz: raw error = −f·e/(1+e); corrected errors < 1 Hz
    f_ref = ex["f_ref_hz"]
    assert f_ref == pytest.approx(FC - 0.1 * FS)
    assert ex["freq_error_raw_hz"] == pytest.approx(-f_ref * e / (1 + e), abs=1.0)
    assert abs(ex["freq_error_corrected_hz"]) < 1.0
    assert abs(ex["freq_error_cal_hz"]) < 1.0
    assert ex["ppm_estimate"] == pytest.approx(dev.true_ppm, abs=1e-3)
    assert abs(ex["residual_ppm"]) < 1e-3
    assert not left_on and single_tone
    names = {f.name for f in res.figures}
    assert {"freq_resolution_resolved", "freq_resolution_unresolved", "freq_resolution_single_tone",
            "freq_resolution_dip_vs_separation"} <= names


def test_freq_resolution_single_tone_source_reports_rbw(tmp_path):
    app = sim_app(tmp_path, fc=FC, fs=FS)
    with sim_context(app, quick=True) as ctx:
        ctx.source.max_tones = 1                      # e.g. a single-output lab generator
        res = run_measurement("freq_resolution", ctx)
    check_contract(res, unit="Hz", min_spectrum_figs=1)
    assert res.value == pytest.approx(RBW) and res.extra["value_source"] == "RBW (hesap)"
    assert "çoklu ton kaynağı yok" in res.summary
    assert abs(res.extra["freq_error_corrected_hz"]) < 1.0       # accuracy part still measured


def test_freq_resolution_without_source(tmp_path):
    res = run_spec(sim_app(tmp_path, fc=FC, fs=FS, source="none"), "freq_resolution").result
    check_contract(res, unit="Hz", min_spectrum_figs=1)
    assert res.value == pytest.approx(RBW) and "kaynak yok" in res.summary
    assert "freq_error_raw_hz" not in res.extra
    dip = next(f for f in res.figures if f.name == "freq_resolution_dip_vs_separation")
    assert [s.label.startswith("Hesap") for s in dip.series] == [True]
