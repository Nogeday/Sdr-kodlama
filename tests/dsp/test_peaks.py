"""eds.dsp.peaks: parabolic interpolation, peak search, high-precision tone frequency."""

from __future__ import annotations

import numpy as np
import pytest

from eds.core.errors import ConfigError, MeasurementError
from eds.dsp import find_peaks, interp_peak, tone_frequency, welch
from tests.dsp.helpers import cnoise, synthetic_spectrum, tone

FS = 1.0e6


@pytest.mark.parametrize("d0", [-0.45, -0.2, 0.0, 0.13, 0.4999])
def test_interp_peak_exact_on_parabola(d0: float) -> None:
    j = np.arange(20, dtype=float)
    k = 9
    p = -37.5 - 2.75 * (j - (k + d0)) ** 2
    delta, peak = interp_peak(p, k)
    assert delta == pytest.approx(d0, abs=1e-12)
    assert peak == pytest.approx(-37.5, abs=1e-12)


def test_interp_peak_edges_flat_and_clamp() -> None:
    p = np.array([0.0, -1.0, -3.0, -1.0, 5.0])
    assert interp_peak(p, 0) == (0.0, 0.0)
    assert interp_peak(p, 4) == (0.0, 5.0)
    assert interp_peak(np.zeros(5), 2) == (0.0, 0.0)              # zero curvature
    assert interp_peak(p, 2) == (0.0, -3.0)                       # a minimum, not a maximum
    # vertex at +0.8 bins -> clamped to +0.5, value of the parabola there
    j = np.arange(5, dtype=float)
    q = -(j - 2.8) ** 2
    delta, peak = interp_peak(q, 2)
    assert delta == 0.5 and peak == pytest.approx(-(0.3 ** 2), abs=1e-12)
    with pytest.raises(ConfigError):
        interp_peak(p, 7)


def test_find_peaks_order_frequency_and_levels() -> None:
    rng = np.random.default_rng(4)
    nfft, n = 4096, 4096 * 16
    df = FS / nfft
    tones = [(101.3, -20.0), (-777.6, -35.0), (1500.25, -50.0)]
    x = cnoise(rng, n, 1e-9)
    for kb, pdb in tones:
        x = x + tone(n, kb * df, FS, pdb, phase=kb)
    s = welch(x, FS, nfft, fc=433e6)
    peaks = find_peaks(s, n=3)
    assert [round(p.p_db) for p in peaks] == [-20, -35, -50]        # power descending
    for p, (kb, pdb) in zip(peaks, tones):
        assert abs(p.f_hz - (433e6 + kb * df)) < 0.1 * df
        assert p.p_db == pytest.approx(pdb, abs=0.6)                 # parabolic level, BH scalloping <= 0.83 dB
        assert p.bin == nfft // 2 + round(kb) and -0.5 <= p.delta <= 0.5


def test_find_peaks_separation_dc_and_threshold() -> None:
    p = np.full(256, 1e-6)
    p[128] = 1.0                    # DC spike
    p[60] = 0.5
    p[62] = 0.4                     # 2 bins from the 0.5 peak
    p[200] = 0.01
    s = synthetic_spectrum(p)
    assert [pk.bin for pk in find_peaks(s, n=4, min_sep_bins=3)] == [128, 60, 200]
    assert [pk.bin for pk in find_peaks(s, n=4, min_sep_bins=2)] == [128, 60, 62, 200]
    assert [pk.bin for pk in find_peaks(s, n=4, min_sep_bins=3, exclude_dc_bins=2)] == [60, 200]
    assert [pk.bin for pk in find_peaks(s, n=4, threshold_db=-10.0)] == [128, 60]
    assert find_peaks(s, n=0) == []


@pytest.mark.parametrize("seed,f_bb", [(1, 123456.789), (2, -301234.5678), (3, 17.25)])
def test_tone_frequency_sub_hz_precision(seed: int, f_bb: float) -> None:
    rng = np.random.default_rng(seed)
    n, fc = 2 ** 18, 2.4e9
    x = (tone(n, f_bb, FS, -20.0, phase=seed) + cnoise(rng, n, 10 ** (-40 / 10))).astype(np.complex64)  # SNR 20 dB
    f = tone_frequency(x, FS, fc=fc)
    assert abs(f - (fc + f_bb)) < 0.05


def test_tone_frequency_short_record_near_crlb() -> None:
    rng = np.random.default_rng(8)
    n = 4096
    errs = []
    for _ in range(30):
        f0 = rng.uniform(-0.4, 0.4) * FS
        x = tone(n, f0, FS, 0.0, phase=rng.uniform(0, 6)) + cnoise(rng, n, 0.01)
        errs.append(tone_frequency(x, FS) - f0)
    crlb = FS / (2 * np.pi) * np.sqrt(6 / (100 * n ** 3))
    assert np.sqrt(np.mean(np.square(errs))) < 4 * crlb


def test_tone_frequency_hint_selects_weaker_tone() -> None:
    n, fc = 2 ** 14, 100e6
    x = tone(n, 200e3, FS, 0.0) + tone(n, -150e3 + 0.3, FS, -30.0) + cnoise(np.random.default_rng(1), n, 1e-7)
    assert tone_frequency(x, FS, fc=fc) == pytest.approx(fc + 200e3, abs=0.05)
    f = tone_frequency(x, FS, fc=fc, f_hint=fc - 150e3, search_hz=2e3)
    assert f == pytest.approx(fc - 150e3 + 0.3, abs=0.05)
    # search_hz alone is centred on fc; hint alone uses +-fs/10
    assert tone_frequency(x, FS, fc=fc, search_hz=180e3) == pytest.approx(fc - 150e3 + 0.3, abs=0.05)
    assert tone_frequency(x, FS, fc=fc, f_hint=fc - 100e3) == pytest.approx(fc - 150e3 + 0.3, abs=0.05)


def test_tone_frequency_errors() -> None:
    x = tone(1024, 1e3, FS, 0.0)
    with pytest.raises(MeasurementError):
        tone_frequency(x, FS, f_hint=10e3, search_hz=1.0)           # no padded bin inside the window
    with pytest.raises(ConfigError):
        tone_frequency(x, FS, zero_pad=0)
    with pytest.raises(ConfigError):
        tone_frequency(x[:4], FS)
