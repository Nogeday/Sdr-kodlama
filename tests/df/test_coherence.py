"""eds.df.coherence: delay / phase / alignment on synthetic signals and the simulator, and the
coherence check (shared clock → coherent; independent clocks → drift 360·f·(e0 − e1) °/s)."""

from __future__ import annotations

import json
import math

import numpy as np
import pytest

from eds.core.errors import ConfigError
from eds.df.coherence import (
    CoherenceReport,
    align,
    analyze_coherence,
    check_coherence,
    estimate_delay,
    estimate_delay_ex,
    phase_offset,
    valid_slice,
)
from eds.hal.group import DeviceGroup
from eds.hal.sim import Emitter
from tests.hal.sim_fixtures import make_device, make_world

F0 = 1.0e9


def _cn(rng, n, power=1.0):
    return math.sqrt(power / 2) * (rng.standard_normal(n) + 1j * rng.standard_normal(n))


def _delayed_pair(rng, n, d, *, bw_frac=0.6, snr_db=20.0, phase=0.8, gain=0.7, pad=4096):
    """Band-limited noise s; x0 = s + n0, x1 = gain e^{j phase} s[n - d] + n1 (exact fractional delay)."""
    m = n + 2 * pad
    s = np.fft.fft(_cn(rng, m))
    f = np.fft.fftfreq(m)
    s[np.abs(f) > bw_frac / 2] = 0.0
    w = 2 * np.pi * f
    x = np.fft.ifft(s)[pad:pad + n]
    y = np.fft.ifft(s * np.exp(-1j * w * d))[pad:pad + n]
    p = float(np.mean(np.abs(x) ** 2))
    nv = p * 10 ** (-snr_db / 10)
    return x + _cn(rng, n, nv), gain * np.exp(1j * phase) * y + _cn(rng, n, nv * gain ** 2)


# =========================================================================== delay
@pytest.mark.parametrize("d", [0.0, 0.25, -0.4, 3.0, -7.6, 12.3, 37.0])
def test_estimate_delay_synthetic_fractional(d):
    rng = np.random.default_rng(int(abs(d) * 100) + 1)
    x0, x1 = _delayed_pair(rng, 1 << 15, d)
    est = estimate_delay_ex(x0, x1, fs=2e6)
    assert est.resolvable
    assert est.delay_samples == pytest.approx(d, abs=0.05)
    assert est.delay_samples == pytest.approx(d, abs=0.01)
    assert est.integer_lag == round(d)
    assert est.peak_corr > 0.85
    assert 0 < est.delay_std_samples < 0.01
    assert est.delay_s == pytest.approx(est.delay_samples / 2e6)
    assert estimate_delay(x0, x1) == pytest.approx((est.delay_samples, est.peak_corr))


def test_estimate_delay_low_snr_and_max_lag():
    rng = np.random.default_rng(5)
    x0, x1 = _delayed_pair(rng, 1 << 15, 5.3, snr_db=-3.0)
    d, rho = estimate_delay(x0, x1, max_lag=16)
    assert d == pytest.approx(5.3, abs=0.05) and 0.2 < rho < 0.7
    with pytest.raises(ConfigError):
        estimate_delay(x0, x1, max_lag=1 << 15)


def test_narrowband_signal_delay_is_not_resolvable():
    n = 1 << 14
    k = np.arange(n)
    rng = np.random.default_rng(2)
    x0 = np.exp(2j * np.pi * 0.11 * k) + _cn(rng, n, 1e-4)
    x1 = np.exp(2j * np.pi * 0.11 * (k - 3.0) + 1j) + _cn(rng, n, 1e-4)
    est = estimate_delay_ex(x0, x1)
    assert not est.resolvable and math.isnan(est.delay_std_samples)
    # two tones are not enough either (2π ambiguity between them)
    x0b = x0 + np.exp(2j * np.pi * -0.2 * k)
    x1b = x1 + np.exp(2j * np.pi * -0.2 * (k - 3.0))
    assert not estimate_delay_ex(x0b, x1b).resolvable


def test_align_inverts_delay():
    rng = np.random.default_rng(9)
    for d in (2.37, -4.81, 6.0):
        x0, x1 = _delayed_pair(rng, 1 << 14, d, snr_db=80.0, phase=0.0, gain=1.0)
        y = align(x1, d)
        sl = valid_slice(x0.size, d, guard=64)
        err = np.mean(np.abs(y[sl] - x0[sl]) ** 2) / np.mean(np.abs(x0[sl]) ** 2)
        assert err < 1e-4
    with pytest.raises(ConfigError):
        align(np.ones(16), 20.0)
    with pytest.raises(ConfigError):
        valid_slice(40, 20.0)


def test_phase_offset_phase_and_coherence():
    rng = np.random.default_rng(4)
    n = 1 << 16
    s = _cn(rng, n)
    x0 = s + _cn(rng, n, 0.1)
    x1 = 2.0 * np.exp(1j * math.radians(-123.0)) * s + _cn(rng, n, 0.4)
    ph, coh = phase_offset(x0, x1)
    assert math.degrees(ph) == pytest.approx(-123.0, abs=0.3)
    assert coh == pytest.approx(1 / 1.1, abs=0.01)                  # SNR 10 dB in each channel
    _, coh0 = phase_offset(_cn(rng, n), _cn(rng, n))
    assert coh0 < 0.02


# =========================================================================== simulator: alignment
@pytest.mark.parametrize("offset", [5, 37])
def test_sim_integer_time_offset_recovered(offset):
    w = make_world(Emitter("ns", F0 + 100e3, -50.0, kind="noise", bw_hz=600e3, cabled=True))
    a = make_device(w, "rxA", fs=1e6, gain=40, shared_clock="ref")
    b = make_device(w, "rxB", fs=1e6, gain=40, shared_clock="ref", time_offset_samples=offset)
    with DeviceGroup([a, b]) as g:
        blk = g.capture(1 << 15)
    est = estimate_delay_ex(blk.samples[0], blk.samples[1], fs=1e6)
    # device B samples world time n + offset: x1[n] = x0[n + offset] -> delay = -offset
    assert est.resolvable
    assert est.delay_samples == pytest.approx(-offset, abs=0.05)


# =========================================================================== coherence check
def test_shared_clock_with_cabled_noise_is_coherent():
    w = make_world(Emitter("ns", F0 + 100e3, -50.0, kind="noise", bw_hz=400e3, cabled=True))
    a = make_device(w, "rxA", fs=1e6, gain=40, shared_clock="ref40")
    b = make_device(w, "rxB", fs=1e6, gain=40, shared_clock="ref40", position_m=[0.15, 0.0], time_offset_samples=3)
    with DeviceGroup([a, b]) as g:
        rep = check_coherence(g, n=1 << 15, blocks=6)
        assert not g.streaming                                       # stream state restored
    assert rep.coherent, rep.note
    assert abs(rep.phase_drift_deg_per_s) < 1.0
    assert rep.coherence > 0.99 and rep.phase_std_deg < 0.5
    assert rep.delay_resolvable and rep.delay_samples == pytest.approx(-3.0, abs=0.05)
    assert rep.n_blocks == 6 and rep.n == 1 << 15 and rep.fs_hz == 1e6
    assert rep.note.startswith("Koherent:")
    json.dumps(rep.to_dict(), allow_nan=False)


def test_shared_clock_with_cabled_cw_is_coherent_but_delay_unresolved():
    w = make_world(Emitter("t", F0 + 120e3, -55.0, cabled=True))
    a = make_device(w, "rxA", fs=1e6, gain=40, shared_clock="ref40")
    b = make_device(w, "rxB", fs=1e6, gain=40, shared_clock="ref40")
    with DeviceGroup([a, b]) as g:
        rep = check_coherence(g, n=1 << 15, blocks=4)
    assert rep.coherent and not rep.delay_resolvable
    assert "gecikmesi çözülemedi" in rep.note


def test_independent_clocks_drift_matches_ppm_difference():
    f = F0 + 50e3
    w = make_world(Emitter("t", f, -50.0, cabled=True))
    a = make_device(w, "rxA", fs=1e6, gain=40, ppm=0.5)
    b = make_device(w, "rxB", fs=1e6, gain=40, ppm=0.4)
    with DeviceGroup([a, b]) as g:
        rep = check_coherence(g, n=1 << 15, blocks=6)
    e0, e1 = 0.5e-6, 0.4e-6
    expected = 360.0 * f * (e0 - e1)                                # ≈ 36000 °/s
    assert not rep.coherent
    assert rep.phase_drift_deg_per_s == pytest.approx(expected, rel=0.10)
    assert rep.phase_drift_deg_per_s == pytest.approx(360.0 * f * (1 / (1 + e1) - 1 / (1 + e0)), rel=1e-3)
    assert rep.freq_offset_hz == pytest.approx(expected / 360.0, rel=1e-3)
    assert rep.coherence < 0.5
    assert "Koherent DEĞİL" in rep.note and "referans" in rep.note


def test_independent_default_clocks_large_drift():
    f = F0 + 80e3
    w = make_world(Emitter("t", f, -50.0, cabled=True))
    a = make_device(w, "rxA", fs=2e6, gain=40)                     # +11.7 ppm (first device)
    b = make_device(w, "rxB", fs=2e6, gain=40)                     # name-hashed ppm
    with DeviceGroup([a, b]) as g:
        rep = check_coherence(g, n=1 << 15, blocks=4)
    e0, e1 = a.true_ppm * 1e-6, b.true_ppm * 1e-6
    assert not rep.coherent
    assert rep.phase_drift_deg_per_s == pytest.approx(360.0 * f * (1 / (1 + e1) - 1 / (1 + e0)), rel=0.01)


def test_three_channel_group_reports_all_pairs():
    w = make_world(Emitter("ns", F0 + 100e3, -50.0, kind="noise", bw_hz=400e3, cabled=True))
    devs = [make_device(w, f"rx{i}", fs=1e6, gain=40, shared_clock="r", time_offset_samples=i) for i in range(3)]
    with DeviceGroup(devs) as g:
        rep = check_coherence(g, n=1 << 14, blocks=3)
    assert rep.coherent and len(rep.pairs) == 2
    assert sorted(round(p["delay_samples"]) for p in rep.pairs) == [-2, -1]


def test_check_coherence_requires_two_channels():
    dev = make_device(make_world(), fs=1e6)
    with pytest.raises(ConfigError, match="2 RX"):
        check_coherence(dev)


# =========================================================================== pure analysis
def _drifting_blocks(rng, drift_deg_s, nb=8, n=1 << 15, fs=1e6, snr_db=20.0, phase_jitter_deg=0.0):
    x0b, x1b = [], []
    for b in range(nb):
        t = (b * n + np.arange(n)) / fs
        s = _cn(rng, n)
        ph = math.radians(drift_deg_s) * t + math.radians(30.0 + phase_jitter_deg * rng.standard_normal())
        nv = 10 ** (-snr_db / 10)
        x0b.append(s + _cn(rng, n, nv))
        x1b.append(s * np.exp(1j * ph) + _cn(rng, n, nv))
    return x0b, x1b


@pytest.mark.parametrize("drift", [0.0, 0.5, -0.8, 3.0, 250.0])
def test_analyze_coherence_drift_estimate(drift):
    rng = np.random.default_rng(100 + int(abs(drift)))
    x0b, x1b = _drifting_blocks(rng, drift, snr_db=40.0)
    rep = analyze_coherence(x0b, x1b, 1e6)
    assert rep.phase_drift_deg_per_s == pytest.approx(drift, abs=0.05)
    assert rep.coherent is (abs(drift) < 1.0)
    assert rep.phase_std_deg < 0.01


def test_drift_uncertainty_is_reported_at_moderate_snr():
    # 20 dB per channel, 0.26 s span: the slope is only known to ~0.15 °/s (phase-noise limited)
    errs, ses = [], []
    for seed in range(4):
        x0b, x1b = _drifting_blocks(np.random.default_rng(seed), 0.4, snr_db=20.0)
        rep = analyze_coherence(x0b, x1b, 1e6)
        errs.append(rep.phase_drift_deg_per_s - 0.4)
        ses.append(rep.drift_std_deg_per_s)
    assert 0.05 < float(np.median(ses)) < 0.4
    assert all(abs(e) < 4 * s for e, s in zip(errs, ses))


def test_analyze_coherence_phase_jitter_breaks_coherence():
    rng = np.random.default_rng(8)
    x0b, x1b = _drifting_blocks(rng, 0.0, phase_jitter_deg=15.0)
    rep = analyze_coherence(x0b, x1b, 1e6)
    assert not rep.coherent and rep.phase_std_deg > 5.0 and "faz std" in rep.note


def test_analyze_coherence_validation():
    rng = np.random.default_rng(1)
    x0b, x1b = _drifting_blocks(rng, 0.0, nb=3, n=1024)
    with pytest.raises(ConfigError):
        analyze_coherence(x0b[:2], x1b[:2], 1e6)
    with pytest.raises(ConfigError):
        analyze_coherence(x0b, x1b[:2], 1e6)
    with pytest.raises(ConfigError):
        analyze_coherence(x0b, x1b, 1e6, t_s=[0.0, 0.0, 1.0])
    rep = CoherenceReport(delay_samples=0.0, phase_deg=0.0, coherence=float("nan"), phase_drift_deg_per_s=0.0,
                          phase_std_deg=0.0, n_blocks=3, coherent=False)
    assert rep.to_dict()["coherence"] is None
