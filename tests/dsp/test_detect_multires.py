"""Multi-resolution CFAR (eds.dsp.detect, DetectConfig.resolutions).

Ground truth: synthetic complex baseband with known emitters (frequency-domain shaped
Gaussian noise of exact band edges, CW tones) analysed with the production Welch estimator,
plus Gamma-distributed iid bins for the false-alarm budget. A full-band sweep of the
config/rx_sim.toml scene checks the end-to-end result on the simulator.
"""

from __future__ import annotations

from pathlib import Path

import numpy as np
import pytest

from eds.core.config import DetectConfig, load_config
from eds.core.errors import ConfigError
from eds.dsp import detect, welch
from eds.dsp.detect import coarse_statistics, resolution_passes
from eds.dsp.windows import bin_correlation
from eds.hal import open_device
from eds.rx import Sweeper
from tests.dsp.helpers import synthetic_spectrum

FS = 100e6                      # 100 MHz synthetic span
NFFT_SWEEP = 10240              # 9.77 kHz bins = the sweep's 20 MS/s / 2048 grid
NOISE = 1e-6                    # total complex noise power over FS
REPO = Path(__file__).resolve().parents[2]

LTE = (-20e6, 10e6)             # (centre, bandwidth) relative to fc
WIFI = (25e6, 20e6)


def _band_noise(rng: np.random.Generator, n: int, f0: float, bw: float, power: float) -> np.ndarray:
    """Gaussian noise flat over [f0 - bw/2, f0 + bw/2] (exact band edges), total ``power``."""
    spec = rng.standard_normal(n) + 1j * rng.standard_normal(n)
    f = np.fft.fftfreq(n, 1.0 / FS)
    spec[np.abs(f - f0) > bw / 2] = 0.0
    x = np.fft.ifft(spec)
    return x * np.sqrt(power / np.mean(np.abs(x) ** 2))


def _scene(seed: int, nfft: int, snr_db: float, emitters=(LTE, WIFI), cw: tuple[float, float] | None = None,
           averages: int = 4, fc: float = 2.4e9):
    """Welch spectrum (BH, 50 %) of white noise + flat emitters ``snr_db`` above the noise
    density + optional CW ``(f, dB relative to the emitter per-bin level)``."""
    rng = np.random.default_rng(seed)
    n = nfft + nfft // 2 * (averages - 1)
    x = np.sqrt(NOISE / 2) * (rng.standard_normal(n) + 1j * rng.standard_normal(n))
    dens = NOISE / FS * 10 ** (snr_db / 10)
    for f0, bw in emitters:
        x += _band_noise(rng, n, f0, bw, dens * bw)
    if cw is not None:
        f, rel_db = cw
        amp = np.sqrt(dens * FS / nfft * 10 ** (rel_db / 10))     # tone power = rel_db x per-bin power
        x += amp * np.exp(2j * np.pi * f * np.arange(n) / FS)
    return welch(x.astype(np.complex64), FS, nfft, fc=fc)


def _covering(dets, f: float):
    return [d for d in dets if d.f_lo_hz <= f <= d.f_hi_hz]


# =========================================================================== wideband emitters
@pytest.mark.parametrize("nfft,snr_db,seed", [
    (NFFT_SWEEP, 6.0, 0), (NFFT_SWEEP, 10.0, 1), (NFFT_SWEEP, 20.0, 2), (NFFT_SWEEP, 8.0, 3), (1 << 16, 10.0, 4),
])
def test_10_and_20_mhz_emitters_detected_with_accurate_bandwidth(nfft, snr_db, seed):
    spec = _scene(seed, nfft, snr_db)
    dets, thr = detect(spec, DetectConfig())
    assert thr.shape == (spec.n_bins,)
    assert len(dets) == 2, [(d.f_center_hz, d.bw_hz) for d in dets]
    for (f0, bw), d in zip((LTE, WIFI), dets):
        f_true = spec.fc + f0
        assert abs(d.bw_hz - bw) <= 0.15 * bw
        assert abs(d.f_center_hz - f_true) <= 0.05 * bw
        assert d.f_lo_hz < f_true < d.f_hi_hz
        assert d.bw_hz == pytest.approx(d.n_bins * spec.df_hz)
        # level fields: band power = emitter + noise power in its band, noise = per-bin noise
        p_true = 10 * np.log10(NOISE / FS * bw * (1.0 + 10 ** (snr_db / 10)))
        assert d.band_power_db == pytest.approx(p_true, abs=0.6)
        noise_bin = 10 * np.log10(NOISE / nfft * spec.enbw_bins)
        assert d.noise_db == pytest.approx(noise_bin, abs=1.0)
        assert d.snr_db == pytest.approx(d.peak_db - d.noise_db)
        assert set(d.to_dict()) == {"f_center_hz", "f_peak_hz", "f_lo_hz", "f_hi_hz", "bw_hz", "peak_db",
                                    "band_power_db", "snr_db", "noise_db", "n_bins"}


def test_native_pass_alone_misses_wideband_emitters():
    """Control: the narrowband CFAR masks a 10/20 MHz flat signal (why the coarse passes exist)."""
    spec = _scene(1, NFFT_SWEEP, 10.0)
    dets, _ = detect(spec, DetectConfig(resolutions=[1]))
    for f0, bw in (LTE, WIFI):
        assert sum(d.bw_hz for d in _covering(dets, spec.fc + f0)) < 0.2 * bw


def test_edges_of_a_flat_signal_are_resolved_to_one_bin():
    """Gamma iid bins with an exact box signal: refined edges at the true bins (D-blur removed)."""
    rng = np.random.default_rng(21)
    n, lo, hi = 1 << 16, 20000, 21999                  # 2000-bin flat signal, 10 dB above noise
    p = rng.gamma(8, 1 / 8, n)
    p[lo:hi + 1] += 10.0 * rng.gamma(8, 1 / 8, hi - lo + 1)
    spec = synthetic_spectrum(p, averages=8)
    dets, _ = detect(spec, DetectConfig(exclude_dc=False))
    (d,) = dets
    k_lo = int(round((d.f_lo_hz + spec.df_hz / 2 - spec.f_hz[0]) / spec.df_hz))
    assert abs(k_lo - lo) <= 1 and abs(d.n_bins - (hi - lo + 1)) <= 2


# =========================================================================== narrowband behaviour
def test_narrow_cw_detected_at_the_right_frequency_and_not_blurred():
    f_cw = 7.3456e6
    spec = _scene(5, NFFT_SWEEP, 0.0, emitters=(), cw=(f_cw, 25.0))
    dets, _ = detect(spec, DetectConfig())
    assert len(dets) == 1
    d = dets[0]
    assert abs(d.f_peak_hz - (spec.fc + f_cw)) <= spec.df_hz / 4
    assert d.n_bins <= 6                                # native extent, not the D = 16 blob
    assert resolution_passes(spec, DetectConfig())[0].d == 16   # the coarse pass did run


def test_cw_15_db_above_wideband_level_is_reported_separately():
    f_cw = LTE[0] + 1.7e6
    spec = _scene(6, NFFT_SWEEP, 10.0, cw=(f_cw, 15.0))
    dets, _ = detect(spec, DetectConfig())
    cover = _covering(dets, spec.fc + f_cw)
    assert len(cover) == 2
    wide = max(cover, key=lambda d: d.bw_hz)
    narrow = min(cover, key=lambda d: d.bw_hz)
    assert abs(wide.bw_hz - LTE[1]) <= 0.15 * LTE[1]
    assert abs(narrow.f_peak_hz - (spec.fc + f_cw)) <= spec.df_hz / 2 and narrow.n_bins <= 6
    # a carrier only a few dB above the in-band level is part of the wideband detection
    spec2 = _scene(6, NFFT_SWEEP, 10.0, cw=(f_cw, 6.0))
    dets2, _ = detect(spec2, DetectConfig())
    cover2 = _covering(dets2, spec2.fc + f_cw)
    assert len(cover2) == 1 and abs(cover2[0].bw_hz - LTE[1]) <= 0.15 * LTE[1]


def test_dc_spike_never_becomes_a_coarse_detection_and_dc_inside_a_signal_is_bridged():
    rng = np.random.default_rng(8)
    n = 1 << 15
    p = rng.gamma(8, 1 / 8, n)
    p[n // 2 - 3:n // 2 + 4] = 1e4                      # zero-IF LO leakage inside the DC notch
    dets, _ = detect(synthetic_spectrum(p, averages=8), DetectConfig())
    assert dets == []
    q = p.copy()
    q[n // 2 - 3000:n // 2 + 3000] += 10.0              # wideband signal straddling DC
    dets, _ = detect(synthetic_spectrum(q, averages=8), DetectConfig())
    assert len(dets) == 1 and abs(dets[0].n_bins - 6000) <= 3


# =========================================================================== false alarms
@pytest.mark.parametrize("method", ["os_cfar", "ca_cfar"])
def test_pure_noise_false_alarms_respect_the_bonferroni_budget(method):
    rng = np.random.default_rng(31)
    pfa, n, trials = 1e-3, 1 << 16, 12
    cfg = DetectConfig(method=method, pfa=pfa, min_snr_db=0.0, exclude_dc=False)
    n_dets = native_hits = 0
    coarse = {16: [0, 0], 128: [0, 0]}
    for _ in range(trials):
        spec = synthetic_spectrum(rng.gamma(4, 1 / 4, n), averages=4)
        dets, thr = detect(spec, cfg)
        n_dets += len(dets)
        native_hits += int(np.count_nonzero(spec.p_db > thr))
        passes = resolution_passes(spec, cfg)
        assert [rp.d for rp in passes] == [16, 128]
        for rp in passes:
            assert rp.pfa == pytest.approx(pfa / 3)                  # Bonferroni share
            coarse[rp.d][0] += int(np.count_nonzero(rp.p_lin > rp.threshold_lin))
            coarse[rp.d][1] += rp.p_lin.size
    # native pass: exact finite-M CFAR at pfa / 3 (independent bins)
    assert 0.75 < native_hits / (trials * n) / (pfa / 3) < 1.33
    # coarse passes: two one-sided tests at pfa / 6 each -> union <= pfa / 3
    for d, (hits, bins) in coarse.items():
        assert hits / bins <= 1.3 * pfa / 3, d
    assert coarse[16][0] > 0                                          # calibrated, not just silent
    # expected false alarms of all passes together <= pfa * n
    assert n_dets <= pfa * n * trials


def test_coarse_statistics_follow_the_window_correlation():
    # rectangular window: independent bins -> D_eff = D, coarse training cells independent
    assert coarse_statistics("rect", 4096, 16, 32) == pytest.approx((16.0, 64.0))
    d_eff, m_eff = coarse_statistics("blackmanharris", 4096, 128, 32)
    rho = bin_correlation("blackmanharris", 4096, 8)
    assert d_eff == pytest.approx(128 ** 2 / (128 + 2 * sum((128 - k) * rho[k] for k in range(1, 8))), rel=1e-9)
    assert 128 / 2.80 < d_eff < 128 / 2.70                           # ~ D / sum(rho), not D / ENBW (2.0)
    assert 60.0 < m_eff < 64.0                                       # coarse cells nearly independent


def test_coarse_false_alarm_rate_on_correlated_welch_bins():
    """Welch + Blackman-Harris (correlated bins): coarse passes stay within their budget."""
    rng = np.random.default_rng(13)
    nfft, avgs = 32768, 4
    cfg = DetectConfig(pfa=3e-2, min_snr_db=0.0, exclude_dc=False)
    hits = {16: [0, 0], 128: [0, 0]}
    for _ in range(12):
        m = nfft + nfft // 2 * (avgs - 1)
        x = np.sqrt(0.5) * (rng.standard_normal(m) + 1j * rng.standard_normal(m))
        for rp in resolution_passes(welch(x.astype(np.complex64), 1e6, nfft), cfg):
            hits[rp.d][0] += int(np.count_nonzero(rp.p_lin > rp.threshold_lin))
            hits[rp.d][1] += rp.p_lin.size
    for d, (h, b) in hits.items():
        assert 0.3 * cfg.pfa / 3 < h / b < 1.3 * cfg.pfa / 3, d


# =========================================================================== configuration
def test_single_runnable_pass_equals_the_single_resolution_cfar():
    rng = np.random.default_rng(2)
    p = rng.gamma(8, 1 / 8, 2048)
    p[700:704] = 50.0
    spec = synthetic_spectrum(p, averages=8)
    assert resolution_passes(spec, DetectConfig()) == []               # 2048 // 16 = 128 < 4 * 36
    a, ta = detect(spec, DetectConfig())
    b, tb = detect(spec, DetectConfig(resolutions=[1]))
    assert a == b and np.array_equal(ta, tb)
    # 4096 bins: native + D = 16 run -> the native threshold is the CFAR at pfa / 2
    spec4 = synthetic_spectrum(np.resize(p, 4096), averages=8)
    _, t2 = detect(spec4, DetectConfig())
    _, t_ref = detect(spec4, DetectConfig(resolutions=[1], pfa=DetectConfig().pfa / 2))
    np.testing.assert_allclose(t2, t_ref, atol=1e-12)


@pytest.mark.parametrize("res", [[], [16, 128], [0, 1], [1, 1.5], [1, True], "1"])
def test_bad_resolutions_rejected(res):
    spec = synthetic_spectrum(np.ones(4096))
    with pytest.raises(ConfigError):
        detect(spec, DetectConfig(resolutions=res))


def test_duplicate_and_unsorted_resolutions_are_normalized():
    spec = _scene(1, NFFT_SWEEP, 10.0)
    a, _ = detect(spec, DetectConfig(resolutions=[16, 1, 16, 128]))
    b, _ = detect(spec, DetectConfig())
    assert a == b


# =========================================================================== end to end on the simulator
def test_full_band_sweep_of_rx_sim_scene_detects_lte_and_wifi():
    app = load_config(REPO / "config" / "rx_sim.toml")
    app.devices[0].sim = dict(app.devices[0].sim, realtime=False)
    dev = open_device(app.devices[0], app)
    try:
        r = Sweeper(dev, app.sweep, app.dsp, app.detect).run()
    finally:
        dev.close()
    emitters = {e["name"]: e for e in app.sim.emitters}
    for name in ("lte_b3_dl", "wifi_ch6"):
        e = emitters[name]
        # the sim TCXO (+11.7 ppm, uncorrected) shifts apparent frequencies by -f * 11.7e-6
        f_app = e["freq_hz"] / (1 + 11.7e-6)
        cover = [d for d in r.detections if d.f_lo_hz <= f_app <= d.f_hi_hz]
        assert len(cover) == 1, name
        d = cover[0]
        assert abs(d.bw_hz - e["bw_hz"]) <= 0.15 * e["bw_hz"], name
        assert abs(d.f_center_hz - f_app) <= 0.05 * e["bw_hz"], name
    # narrowband emitters keep their native extents
    gsm = [d for d in r.detections if d.f_lo_hz <= 935.2e6 / (1 + 11.7e-6) <= d.f_hi_hz]
    assert len(gsm) == 1 and gsm[0].bw_hz <= 300e3
    # no alias ghosts of the strong 433.92 MHz link at +-k * 20 MHz (digital decimation filter)
    for k in (-4, -3, -2, -1, 1, 2, 3, 4):
        ghost = 433.92e6 / (1 + 11.7e-6) + k * 20e6
        assert not any(abs(d.f_peak_hz - ghost) <= 100e3 for d in r.detections), k
