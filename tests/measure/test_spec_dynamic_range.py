"""Dynamic-range measurement (eds.measure.specs.dynamic_range) against simulator ground truth.

Truth: noise in one RBW = thermal (``kT + NF`` through the level equation) ⊕ white ADC floor
(transition noise + 12-bit rounding); ``P_max = min(ADC clipping, input 1 dB compression,
profile rx_max_input_dbm)`` — the strongest non-clipping CW tone has its per-rail peak just
below ``clip_peak_dbfs`` (−0.5 dBFS), the front end compresses by 1 dB at
``SimRxDevice.input_p1db_dbm(G)``; the dominant spur of a −3 dBFS carrier is its IQ image at
``IRR`` (quadrature tracking on) unless the −85 dBFS digital spur is stronger; the level
equation is ``dBFS = dBm + G − 5``.
"""

from __future__ import annotations

import math

import numpy as np
import pytest

from eds.calib.power import PowerCalTable
from eds.core.config import AppConfig
from eds.core.errors import MeasurementError
from eds.dsp.levels import IQLevels
from eds.dsp.psd import Spectrum
from eds.dsp.windows import get_window
from eds.hal.sim import signals as sg
from eds.measure.base import MeasurementContext, run_measurement
from eds.measure.specs.dynamic_range import COMPRESSION, RX_CEILING, DynamicRangeMeasurement
from eds.measure.stimulus import ToneProbe, adjust_tone

from .spec_fixtures import (
    SimLoSource,
    check_contract,
    input_p1db_dbm,
    n_rbw_dbfs,
    replace_source,
    run_spec,
    sim_app,
    sim_context,
    source_enabled,
)

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
    # P_max at the fixed gain: ADC clipping comes long before the front end's 1 dB compression
    assert ex["clip_input"] < input_p1db_dbm(dev, gain) - 3.0
    assert ex["p_max_criterion"] == "kırpma" and abs(ex["compression_at_p_max_db"]) < 0.3
    # (c) total DR: P_in,max(G_min) − MDS(G_max); at G_min clipping (+5 dBm) and P1dB are above the
    # AD9363 profile's rx_max_input_dbm, so the search stops there and DR_total is a lower bound
    g_min, g_max = ex["gain_min_db"], ex["gain_max_db"]
    assert (g_min, g_max) == (-1.0, 73.0)
    cap = dev.profile.rx_max_input_dbm
    assert ex["rx_max_input_dbm"] == cap and math.isfinite(cap)
    p_max_in_true = min(-0.75 - g_min - sp["gain_offset_db"], input_p1db_dbm(dev, g_min), cap)
    assert p_max_in_true == cap
    assert ex["p_max_in_gmin_dbm"] == pytest.approx(p_max_in_true, abs=0.6)
    assert ex["p_max_gmin_criterion"] == RX_CEILING and ex["dr_total_lower_bound"] is True
    gmin_rows = [r for r in res.table if r["bölüm"] == "doğrusallık (G_min)"]
    assert gmin_rows and max(r["p_in"] for r in gmin_rows) <= cap + 1e-9
    mds_true = n_rbw_dbfs(dev, g_max, RBW, FS) - g_max - sp["gain_offset_db"] + ex["snr_req_db"]
    assert ex["mds_gmax_dbm"] == pytest.approx(mds_true, abs=0.5)
    assert ex["dr_total_db"] == pytest.approx(p_max_in_true - mds_true, abs=1.0)
    assert 5.0 < ex["snr_req_db"] < 8.0 and ex["dr_total_method"] == "ölçüm"
    assert "DR_toplam ≥" in res.summary
    # SFDR search region and the stimulus (no own LO on the simulator source)
    assert ex["close_in_excluded_hz"] >= 25e3 and ex["dominant_spur_discrete"] is True
    assert ex["source_artifacts"] == [] and res.conditions["source_settle_s"] == 0.0
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
    # the level model (dBFS = dBm + G − 5) is the simulator's own: P_max = 0 dBFS at G_min would be
    # +6 dBm, above the rated RX input — the estimate never goes beyond rx_max_input_dbm
    mds = n_rbw_dbfs(dev, 73.0, RBW, FS) - 73.0 + 5.0 + ex["snr_req_db"]
    cap = dev.profile.rx_max_input_dbm
    assert ex["p_max_in_gmin_dbm"] == pytest.approx(min(0.0 + 1.0 + 5.0, cap))
    assert ex["dr_total_db"] == pytest.approx(min(0.0 + 1.0 + 5.0, cap) - mds, abs=0.5)
    assert ex["p_max_gmin_criterion"] == RX_CEILING
    assert "SFDR ölçülemedi" in res.summary and res.uncertainty is None
    assert math.isfinite(ex["noise_rbw_dbfs"])


def test_dynamic_range_p_max_is_the_1db_compression_point_when_the_front_end_compresses_first(tmp_path):
    """A front end that compresses below ADC full scale: P_max = input P1dB (not the clip level)."""
    sim = {"p1db_min_gain_dbm": -30.0, "p1db_max_gain_dbm": -50.0}
    run = run_spec(sim_app(tmp_path, fc=FC, fs=FS, sim=sim), "dynamic_range", quick=True)
    res, dev = run.result, run.dev
    check_contract(res, unit="dB", min_spectrum_figs=2, min_xy_figs=2)
    ex = res.extra
    sp = dev.sim_params
    gain = DynamicRangeMeasurement.default_params["gain"]
    p1 = input_p1db_dbm(dev, gain)
    clip_in = -0.75 - gain - sp["gain_offset_db"]
    assert p1 < clip_in - 3.0                                   # compression is the binding limit
    assert ex["p_max_criterion"] == COMPRESSION and ex["p_max_lower_bound"] is False
    assert ex["p1db_input"] == pytest.approx(p1, abs=0.3)
    assert ex["p_max_input"] == pytest.approx(p1, abs=0.6) and ex["p_max_input"] <= ex["p1db_input"] + 1e-9
    # output at P1dB: small-signal level − 1 dB (compression of the last good point < 1 dB)
    assert ex["p_max_dbfs"] == pytest.approx(dev.level_dbfs(ex["p_max_input"], gain)
                                             - dev.compression_db(ex["p_max_input"], gain), abs=0.3)
    assert 0.5 <= ex["compression_at_p_max_db"] < 1.0
    assert res.value == pytest.approx(ex["p_max_dbfs"] - ex["noise_rbw_dbfs"])
    lin = [r for r in res.table if r["bölüm"] == "doğrusallık"]
    assert any(r["limit"] == COMPRESSION and r["compression_db"] >= 1.0 for r in lin)
    assert not any(r["clipped"] for r in lin if r["p_in"] <= ex["p_max_input"])
    # total DR at G_min: also compression-limited (P1dB(G_min) = −30 dBm < rx_max_input_dbm)
    g_min = ex["gain_min_db"]
    assert ex["p_max_gmin_criterion"] == COMPRESSION and ex["dr_total_lower_bound"] is False
    assert ex["p1db_input_gmin_dbm"] == pytest.approx(input_p1db_dbm(dev, g_min), abs=0.3)
    assert ex["p_max_in_gmin_dbm"] == pytest.approx(input_p1db_dbm(dev, g_min), abs=0.6)
    assert "1 dB sıkışma" in res.summary and "DR_toplam ≥" not in res.summary
    assert not run.source_left_on
    # input levels carry their unit (absolute simulator source → dBm)
    assert ex["p_max_input_unit"] == ex["p1db_input_unit"] == ex["clip_input_unit"] == "dBm"
    # linearity figure: compression / clipping markers and the ideal line have fixed semantic roles
    fig = next(f for f in res.figures if f.name == "dynamic_range_linearity")
    roles = {s.label: s.role for s in fig.series}
    assert roles["İdeal 1:1"] == "ref" and roles["≥ 1 dB sıkışma"] == "warn" and roles["Ölçülen ton gücü"] == ""
    assert roles.get("Kırpma", "fail") == "fail"


def _record_powers(src) -> list[float]:
    """Wrap ``src.set_power`` to record every commanded level."""
    seen: list[float] = []
    orig = src.set_power

    def set_power(dbm: float) -> float:
        seen.append(float(dbm))
        return orig(dbm)

    src.set_power = set_power
    return seen


def test_dynamic_range_never_drives_the_rx_input_above_the_profile_limit(tmp_path):
    """Absolute source: every commanded level ≤ rx_max_input_dbm; relative source with a power
    calibration table: the cap is extrapolated from the linear reading (exact on the simulator)."""
    app = sim_app(tmp_path, fc=FC, fs=FS)
    with sim_context(app, quick=True) as ctx:
        cap = ctx.dut.profile.rx_max_input_dbm
        seen = _record_powers(ctx.source)
        res = run_measurement("dynamic_range", ctx)
        assert res.status == "ok", res.error
        assert seen and max(seen) <= cap + 1e-9
    app = sim_app(tmp_path / "rel", fc=FC, fs=FS)
    with sim_context(app, quick=True) as ctx:
        dev = ctx.dut
        off = {g: -(g + dev.sim_params["gain_offset_db"]) for g in (-1.0, 40.0, 73.0)}
        ctx.cal.power = PowerCalTable([(f, g, o) for f in (900e6, 950e6) for g, o in off.items()])
        ctx.source.absolute_power = False                     # e.g. an un-anchored Pluto TX
        seen = _record_powers(ctx.source)
        res = run_measurement("dynamic_range", ctx)
        assert res.status == "ok", res.error
        cap = dev.profile.rx_max_input_dbm
        assert seen and max(seen) <= cap + 0.1                # sim setting = dBm at the DUT input
        assert res.extra["p_max_gmin_criterion"] == RX_CEILING and res.extra["dr_total_lower_bound"] is True
        assert res.extra["p_max_in_gmin_dbm"] == pytest.approx(cap, abs=0.1)


def test_adjust_tone_never_exceeds_the_rx_input_limit_when_the_tone_is_missing(tmp_path):
    """Cable / attenuator problem (tone invisible): the +20 dB search stops at rx_max_input_dbm."""
    app = sim_app(tmp_path, fc=FC, fs=FS)
    with sim_context(app, quick=True) as ctx:
        replace_source(ctx, lambda w: SimLoSource(w, path_loss_db=250.0, freq_hz=FC))
        cap = ctx.dut.profile.rx_max_input_dbm
        seen = _record_powers(ctx.source)
        with pytest.raises(MeasurementError, match="RX giriş sınırı"):
            adjust_tone(ctx, ctx.dut, FC + 0.1 * FS, -20.0, nfft=4096, averages=4, window="blackmanharris",
                        overlap=0.5)
        assert seen and max(seen) == pytest.approx(cap) and not source_enabled(ctx)


def test_sfdr_masks_the_sources_own_lo_leakage_and_tx_image(tmp_path):
    """Pluto-TX-like source (LO leakage / TX image at −40 dBc): the LO is moved beyond the tone and
    both artifacts are reported as source artifacts, not as the DUT's dominant spur."""
    app = sim_app(tmp_path, fc=FC, fs=FS, params={"dynamic_range": {"total": False}})
    with sim_context(app, quick=True) as ctx:
        src = replace_source(ctx, lambda w: SimLoSource(w, freq_hz=FC))
        res = run_measurement("dynamic_range", ctx)
        sp = ctx.dut.sim_params
        left_on = source_enabled(ctx)
        restored = src.tone_offset_hz
    check_contract(res, unit="dB", min_spectrum_figs=2, min_xy_figs=2)
    ex = res.extra
    assert src.lo_offsets_used[0] < 0                           # tone above fc → LO above the tone
    assert restored == pytest.approx(199951.171875)             # source left as configured
    arts = ex["source_artifacts"]
    assert sorted(a["label"] for a in arts) == ["kaynak LO kaçağı", "kaynak TX imajı"]
    for a in arts:
        assert a["dbc"] == pytest.approx(-40.0, abs=1.0) and a["f_hz"] > ex["f_carrier_hz"]
    irr_on = sg.image_rejection_db(sp["iq_residual_gain_db"], sp["iq_residual_phase_deg"])
    carrier = ex["carrier_dbfs"]
    assert ex["sfdr_dbc"] == pytest.approx(min(irr_on, carrier - SPUR_DBFS), abs=2.0)
    assert ex["sfdr_ex_image_dbc"] == pytest.approx(carrier - SPUR_DBFS, abs=2.0)    # not the −40 dBc artifacts
    assert ex["spur_ex_image_hz"] - FC == pytest.approx(SPUR_HZ, abs=2 * FS / NFFT)
    assert "kaynak kaynaklı" in res.summary and not left_on


def _sfdr_probe(close_in_skirt_dbc: float, skirt_bins: int, spur_dbc: float = -80.0) -> tuple[ToneProbe, float]:
    """Synthetic averaged spectrum: −3 dBFS Blackman-Harris carrier, a continuous phase-noise-like
    skirt (``close_in_skirt_dbc`` at 7 bins, −20 dB/decade, out to ``skirt_bins``), one discrete
    spur at ``spur_dbc`` 600 bins away, Gamma(10) noise at −110 dBFS."""
    n, fs, fc = NFFT, FS, FC
    rng = np.random.default_rng(7)
    p = 10 ** (-110 / 10) * rng.gamma(10.0, 0.1, n)
    kc = n // 2 + 1111
    w = get_window("blackmanharris", n)
    line = np.abs(np.fft.fft(w, n * 16)) ** 2
    line /= line[0]
    car = 10 ** (-3 / 10)
    for d in range(-5, 6):
        p[kc + d] += car * line[(d * 16) % (n * 16)]
    for d in range(7, skirt_bins):              # averaged (n_eff = 10) continuous skirt on both sides
        lvl = car * 10 ** ((close_in_skirt_dbc - 20 * math.log10(d / 7)) / 10)
        p[kc + d] += lvl * rng.gamma(10.0, 0.1)
        p[kc - d] += lvl * rng.gamma(10.0, 0.1)
    ks = kc + 600
    for d in range(-5, 6):
        p[ks + d] += car * 10 ** (spur_dbc / 10) * line[(d * 16) % (n * 16)]
    f = fc + (np.arange(n) - n // 2) * fs / n
    spec = Spectrum(f_hz=f, p_lin=p, fc=fc, fs=fs, nfft=n, window="blackmanharris", enbw_bins=2.0043529382170475,
                    averages=10, meta={"avg": "mean", "overlap": 0.5})
    lv = IQLevels(peak_dbfs=-0.0, rms_dbfs=-3.0, clip_fraction=0.0, dc_dbfs=-200.0, n=n)
    return ToneProbe(f_src_hz=float(f[kc]), setting=-40.0, f_app_hz=float(f[kc]), level_dbfs=-3.0, noise_dbfs=-110.0,
                     levels=lv, k=kc, spec=spec), car


def test_sfdr_excludes_the_close_in_skirt_and_requires_a_discrete_line():
    m = DynamicRangeMeasurement(MeasurementContext(app=AppConfig(), devices=[object()]))
    prm = {**m.default_params, "main_lobe_bins": 6}
    # skirt −55 dBc at 3.4 kHz, inside the 25 kHz close-in region: the −80 dBc spur is the SFDR
    pr, _ = _sfdr_probe(-55.0, 40)
    sf = m._sfdr(pr, prm)
    assert sf["spur"]["f_hz"] == pytest.approx(pr.spec.f_hz[pr.k + 600])
    assert sf["sfdr_dbc"] == pytest.approx(80.0, abs=1.0) and sf["spur"]["discrete"]
    assert sf["close_in_max_dbc"] == pytest.approx(-55.0, abs=3.0)
    # the old ±main-lobe-only search would have reported the skirt as a −55 dBc "spur"
    old = m._sfdr(pr, {**prm, "close_in_hz": 0.0, "spur_contrast_db": 0.0})
    assert old["sfdr_dbc"] < 60.0
    # a skirt reaching far beyond the close-in region (−62 dBc at its edge) is continuous, not a line:
    # the weaker discrete spur on top of it is the SFDR, not the skirt
    pr2, _ = _sfdr_probe(-45.0, 2900, spur_dbc=-75.0)
    sf2 = m._sfdr(pr2, prm)
    assert sf2["spur"]["discrete"] and sf2["spur"]["f_hz"] == pytest.approx(pr2.spec.f_hz[pr2.k + 600])
    assert 70.0 < sf2["sfdr_dbc"] < 76.0
    assert m._sfdr(pr2, {**prm, "spur_contrast_db": 0.0})["sfdr_dbc"] < 65.0     # skirt bin without the line test
