"""eds.dsp.detect: CFAR noise estimation, false-alarm calibration, runs/merging, DC handling."""

from __future__ import annotations

import json
import time
from dataclasses import replace

import numpy as np
import pytest

from eds.core.config import DetectConfig
from eds.core.errors import ConfigError, MeasurementError
from eds.dsp import (
    default_n_avg,
    detect,
    effective_averages,
    effective_training_cells,
    estimate_noise,
    usable_mask,
    welch,
)
from eds.dsp.detect import _training_cells
from tests.dsp.helpers import cnoise, synthetic_spectrum, tone

FS = 1.0e6


def test_usable_mask() -> None:
    m = usable_mask(4096, 0.8, 3)
    assert m.dtype == bool and m.shape == (4096,)
    assert m.sum() == 2 * 1638 + 1 - 7
    assert not m[2048] and not m[2048 + 3] and m[2048 + 4] and m[2048 - 4]
    assert m[2048 + 1638] and not m[2048 + 1639] and m[2048 - 1638] and not m[2048 - 1639]
    assert usable_mask(64, 1.0, -1).all()
    assert usable_mask(64, 1.0, 0).sum() == 63
    with pytest.raises(ConfigError):
        usable_mask(64, 0.0, 3)


@pytest.mark.parametrize("n,g,t", [(50, 2, 4), (40, 0, 3), (64, 4, 8), (17, 1, 5)])
def test_training_cells_match_naive_layout(n: int, g: int, t: int) -> None:
    p = np.arange(n, dtype=np.float64)
    cells = _training_cells(p, g, t)
    for i in range(n):
        if i - g - t >= 0 and i + g + t <= n - 1:
            ref = list(range(i - g - t, i - g)) + list(range(i + g + 1, i + g + t + 1))
        elif i - g - t < 0:
            ref = list(range(i + g + 1, i + g + 1 + 2 * t))
        else:
            ref = list(range(i - g - 2 * t, i - g))
        assert sorted(cells[i].tolist()) == ref, i
        assert i not in ref and all(abs(c - i) > g for c in ref)


@pytest.mark.parametrize("method", ["os_cfar", "ca_cfar"])
@pytest.mark.parametrize("n_avg", [1, 8])
def test_false_alarm_rate_iid_gamma_bins(method: str, n_avg: int) -> None:
    """Pure-noise spectra synthesized as Gamma(n, 1/n) bins: measured Pfa ~= design Pfa
    (single native pass; the multi-resolution budget is tested in test_detect_multires)."""
    rng = np.random.default_rng(100 + n_avg)
    pfa = 1e-3
    cfg = DetectConfig(method=method, pfa=pfa, min_snr_db=0.0, exclude_dc=False, resolutions=[1])
    hits = total = 0
    for _ in range(4):
        n = 2 ** 18
        spec = synthetic_spectrum(rng.gamma(n_avg, 1.0 / n_avg, n) * 3e-7, averages=n_avg)
        dets, thr_db = detect(spec, cfg)
        hits += int(np.count_nonzero(spec.p_db > thr_db))
        total += n
        assert len(dets) <= np.count_nonzero(spec.p_db > thr_db)
    ratio = hits / total / pfa
    assert 1 / 3 < ratio < 3                      # requirement
    assert 0.8 < ratio < 1.25                     # exact finite-M correction (rect: independent bins)


@pytest.mark.parametrize("method", ["os_cfar", "ca_cfar"])
@pytest.mark.parametrize("window,averages", [("blackmanharris", 8), ("hann", 1), ("flattop", 4)])
def test_false_alarm_rate_end_to_end_welch(method: str, window: str, averages: int) -> None:
    """Complex white noise -> Welch (overlap, correlated bins) -> CFAR with default n_avg."""
    rng = np.random.default_rng(7)
    nfft, pfa = 4096, 1e-3
    cfg = DetectConfig(method=method, pfa=pfa, min_snr_db=0.0, exclude_dc=False, resolutions=[1])
    hits = total = 0
    for _ in range(48):
        x = cnoise(rng, nfft + nfft // 2 * (averages - 1), 1e-4).astype(np.complex64)
        s = welch(x, FS, nfft, window=window, overlap=0.5)
        _, thr_db = detect(s, cfg)
        hits += int(np.count_nonzero(s.p_db > thr_db))
        total += nfft
    assert 1 / 3 < hits / total / pfa < 3


def test_default_n_avg_and_effective_cells() -> None:
    s = welch(np.zeros(4096 + 2048 * 15, dtype=np.complex64), FS, 4096, window="hann", overlap=0.5)
    assert default_n_avg(s) == pytest.approx(effective_averages(16, 0.5, "hann", 4096))
    assert default_n_avg(s) < 16
    s_max = welch(np.zeros(4096 * 4, dtype=np.complex64), FS, 4096, avg="max")
    assert default_n_avg(s_max) == 1.0
    assert effective_training_cells("rect", 4096, 4, 32) == pytest.approx(64.0)
    m_bh = effective_training_cells("blackmanharris", 4096, 4, 32)
    assert 20 < m_bh < 28                          # strongly correlated neighbouring bins
    est = estimate_noise(s, DetectConfig())
    assert est.m_cells == 64 and est.rank == 48 and est.m_eff == pytest.approx(
        effective_training_cells("hann", 4096, 4, 32))


def _two_tone_spectrum(rng: np.random.Generator, offsets: tuple[float, float]):
    nfft, avgs = 4096, 64
    n = nfft + nfft // 2 * (avgs - 1)
    df = FS / nfft
    noise_power = 1e-6
    s_noise_bin = noise_power * 2.00435 / nfft            # BH ENBW: noise power per bin
    tone_db = 10 * np.log10(s_noise_bin) + 20.0           # 20 dB above the per-bin noise
    k1, k2 = -900 + offsets[0], 1300 + offsets[1]
    x = cnoise(rng, n, noise_power) + tone(n, k1 * df, FS, tone_db, 0.3) + tone(n, k2 * df, FS, tone_db, 2.0)
    return welch(x.astype(np.complex64), FS, nfft, fc=868e6), (k1, k2), tone_db


@pytest.mark.parametrize("method", ["os_cfar", "ca_cfar"])
@pytest.mark.parametrize("offsets", [(0.0, 0.0), (0.3, -0.45)])
def test_two_tones_detected_with_frequency_and_power(method: str, offsets: tuple[float, float]) -> None:
    rng = np.random.default_rng(55)
    spec, (k1, k2), tone_db = _two_tone_spectrum(rng, offsets)
    cfg = DetectConfig(method=method)
    dets, thr = detect(spec, cfg, valid_mask=usable_mask(spec.n_bins, 0.8, 3))
    assert len(dets) == 2
    assert thr.shape == (spec.n_bins,)
    df = spec.df_hz
    for d, k in zip(dets, (k1, k2)):
        f_true = 868e6 + k * df
        assert abs(d.f_peak_hz - f_true) <= df / 4
        assert abs(d.f_center_hz - f_true) <= df / 2
        assert d.band_power_db == pytest.approx(tone_db, abs=0.5)
        assert d.f_lo_hz < f_true < d.f_hi_hz
        assert d.bw_hz == pytest.approx(d.n_bins * df)
        assert d.snr_db == pytest.approx(20.0, abs=2.5)
        assert d.noise_db == pytest.approx(tone_db - 20.0, abs=1.0)


def test_merge_gap_and_min_bw() -> None:
    p = np.ones(1024)
    p[100:103] = 1e3          # run A
    p[105:108] = 1e3          # run B, 2-bin gap
    p[300] = 1e3              # single-bin run
    spec = synthetic_spectrum(p)
    base = DetectConfig(method="os_cfar", pfa=1e-6, exclude_dc=False)
    dets, _ = detect(spec, replace(base, merge_gap_bins=2))
    assert [(d.n_bins, round(d.f_lo_hz)) for d in dets] == [(8, round(spec.f_hz[100] - spec.df_hz / 2)), (1, round(spec.f_hz[300] - spec.df_hz / 2))]
    # merged run's band power includes the (noise) gap bins
    assert dets[0].band_power_db == pytest.approx(10 * np.log10(6e3 + 2), abs=1e-9)
    dets, _ = detect(spec, replace(base, merge_gap_bins=1))
    assert [d.n_bins for d in dets] == [3, 3, 1]
    dets, _ = detect(spec, replace(base, merge_gap_bins=1, min_bw_bins=2))
    assert [d.n_bins for d in dets] == [3, 3]


def test_dc_exclusion_and_valid_mask() -> None:
    rng = np.random.default_rng(3)
    n = 2048
    p = rng.gamma(16, 1 / 16, n)
    p[n // 2 - 1:n // 2 + 2] = 1e4            # zero-IF DC spike
    p[1500] = 1e4
    spec = synthetic_spectrum(p, averages=16)
    dets, _ = detect(spec, DetectConfig(exclude_dc=True))
    assert len(dets) == 1 and abs(dets[0].f_peak_hz - spec.f_hz[1500]) < spec.df_hz / 2
    dets, _ = detect(spec, DetectConfig(exclude_dc=False))
    assert len(dets) == 2 and abs(dets[0].f_peak_hz - spec.fc) <= spec.df_hz
    spec.meta["dc_notch_bins"] = 0              # notch only the DC bin: spike shoulders remain
    dets, _ = detect(spec, DetectConfig(exclude_dc=True))          # 1-bin gap -> merged (gap <= 2)
    assert len(dets) == 2 and dets[0].n_bins == 3 and dets[0].f_center_hz == pytest.approx(spec.fc, abs=1e-6)
    dets, _ = detect(spec, DetectConfig(exclude_dc=True, merge_gap_bins=0))
    assert len(dets) == 3 and dets[0].n_bins == 1 and dets[1].n_bins == 1
    spec.meta["dc_notch_bins"] = 3
    mask = np.ones(n, dtype=bool)
    mask[1400:1600] = False
    dets, _ = detect(spec, DetectConfig(), valid_mask=mask)
    assert dets == []
    with pytest.raises(ConfigError):
        detect(spec, DetectConfig(), valid_mask=np.ones(10, dtype=bool))


def test_fixed_threshold_and_min_snr() -> None:
    rng = np.random.default_rng(9)
    n = 4096
    spec = synthetic_spectrum(rng.gamma(8, 1 / 8, n) * 1e-9, averages=8)
    cfg = DetectConfig(method="fixed", fixed_threshold=-75.0, min_snr_db=6.0)
    dets, thr = detect(spec, cfg)
    est = estimate_noise(spec, cfg)
    np.testing.assert_allclose(thr, np.maximum(-75.0, 10 * np.log10(est.noise_lin) + 6.0), atol=1e-9)
    assert dets == []
    cfg_low = DetectConfig(method="fixed", fixed_threshold=-200.0, min_snr_db=6.0)
    _, thr_low = detect(spec, cfg_low)
    np.testing.assert_allclose(thr_low, 10 * np.log10(est.noise_lin) + 6.0, atol=1e-9)
    # OS noise estimate is unbiased for the noise mean (median over bins)
    assert np.median(est.noise_lin) == pytest.approx(1e-9, rel=0.05)


def test_detection_to_dict_is_json_safe() -> None:
    p = np.ones(512)
    p[100:104] = 1e3
    dets, _ = detect(synthetic_spectrum(p, fc=2.4e9), DetectConfig(exclude_dc=False))
    d = dets[0].to_dict()
    assert set(d) == {"f_center_hz", "f_peak_hz", "f_lo_hz", "f_hi_hz", "bw_hz", "peak_db", "band_power_db",
                      "snr_db", "noise_db", "n_bins"}
    assert json.loads(json.dumps(d, allow_nan=False)) == d
    assert isinstance(d["n_bins"], int) and d["n_bins"] == 4


def test_short_spectrum_and_bad_config() -> None:
    spec = synthetic_spectrum(np.ones(40))
    dets, thr = detect(spec, DetectConfig(train_bins=32, guard_bins=4, exclude_dc=False))   # train shrinks
    assert dets == [] and thr.shape == (40,)
    with pytest.raises(ConfigError):
        detect(synthetic_spectrum(np.ones(10)), DetectConfig(guard_bins=4))
    with pytest.raises(ConfigError):
        detect(spec, DetectConfig(method="glrt"))
    with pytest.raises(ConfigError):
        detect(spec, DetectConfig(pfa=0.0))
    bad = np.ones(64)
    bad[10] = np.nan
    with pytest.raises(MeasurementError):
        detect(synthetic_spectrum(bad), DetectConfig())


@pytest.mark.realtime
@pytest.mark.parametrize("resolutions,limit_s", [([1], 0.060), ([1, 16, 128], 0.150)])
def test_os_cfar_speed_2_16_bins(resolutions: list[int], limit_s: float) -> None:
    rng = np.random.default_rng(0)
    spec = synthetic_spectrum(rng.gamma(16, 1 / 16, 2 ** 16), window="blackmanharris", enbw_bins=2.0044,
                              averages=16, overlap=0.5)
    cfg = DetectConfig(resolutions=resolutions)
    detect(spec, cfg)                               # warm-up: multiplier cache
    best = np.inf
    for _ in range(5):
        t0 = time.perf_counter()
        detect(spec, cfg)
        best = min(best, time.perf_counter() - t0)
    assert best < limit_s
