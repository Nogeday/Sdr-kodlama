"""eds.dsp.windows: periodic generalized-cosine windows and their figures of merit."""

from __future__ import annotations

import numpy as np
import pytest

from eds.core.errors import ConfigError
from eds.dsp import WINDOW_NAMES, bin_correlation, canonical_window_name, get_window, window_info
from eds.dsp.windows import WINDOW_COEFFS


def _closed_form_enbw(coeffs: tuple[float, ...]) -> float:
    a0 = coeffs[0]
    return (a0 * a0 + 0.5 * sum(a * a for a in coeffs[1:])) / (a0 * a0)


@pytest.mark.parametrize("name", WINDOW_NAMES)
def test_window_is_periodic_cosine_sum(name: str) -> None:
    n = 256
    w = get_window(name, n)
    assert w.dtype == np.float64 and w.shape == (n,)
    coeffs = WINDOW_COEFFS[name]
    ph = 2 * np.pi * np.arange(n) / n
    ref = sum(((-1) ** m) * a * np.cos(m * ph) for m, a in enumerate(coeffs))
    np.testing.assert_allclose(w, ref, rtol=0, atol=1e-15)
    # DFT-even symmetry: w[k] == w[N - k]
    np.testing.assert_allclose(w[1:], w[1:][::-1], rtol=0, atol=1e-15)
    # DFT is exactly a0*N at bin 0 and +-a_m*N/2 at bins +-m
    spec = np.fft.fft(w)
    assert spec[0].real == pytest.approx(coeffs[0] * n, rel=1e-13)
    for m, a in enumerate(coeffs[1:], start=1):
        assert abs(spec[m]) == pytest.approx(a * n / 2, rel=1e-12)


def test_exact_blackman_and_bh_coefficients() -> None:
    assert WINDOW_COEFFS["blackman"] == (0.42, 0.5, 0.08)
    assert WINDOW_COEFFS["blackmanharris"] == (0.35875, 0.48829, 0.14128, 0.01168)
    assert WINDOW_COEFFS["flattop"] == (0.21557895, 0.41663158, 0.277263158, 0.083578947, 0.006947368)


def test_window_cached_and_read_only() -> None:
    a = get_window("hann", 1024)
    b = get_window("HANN", 1024)
    assert a is b
    with pytest.raises(ValueError):
        a[0] = 1.0


def test_aliases_and_unknown_names() -> None:
    assert canonical_window_name("Blackman-Harris") == "blackmanharris"
    assert canonical_window_name("hanning") == "hann"
    assert canonical_window_name("boxcar") == "rect"
    with pytest.raises(ConfigError):
        get_window("kaiser", 64)
    with pytest.raises(ConfigError):
        get_window("hann", 0)


@pytest.mark.parametrize("name", WINDOW_NAMES)
def test_window_info_closed_forms(name: str) -> None:
    n = 4096
    info = window_info(name, n)
    coeffs = WINDOW_COEFFS[name]
    assert info.coherent_gain == pytest.approx(coeffs[0], rel=1e-12)
    assert info.enbw_bins == pytest.approx(_closed_form_enbw(coeffs), rel=1e-12)
    # scalloping: direct DTFT at half a bin
    w = get_window(name, n)
    half = abs(np.sum(w * np.exp(-1j * np.pi * np.arange(n) / n))) / np.sum(w)
    assert info.scallop_loss_db == pytest.approx(-20 * np.log10(half), abs=1e-9)


def test_window_info_textbook_values() -> None:
    n = 8192
    assert window_info("rect", n).scallop_loss_db == pytest.approx(3.92, abs=0.01)
    assert window_info("hann", n).scallop_loss_db == pytest.approx(1.42, abs=0.01)
    assert window_info("hann", n).enbw_bins == pytest.approx(1.5, abs=1e-12)
    assert window_info("hamming", n).enbw_bins == pytest.approx(1.3628, abs=1e-4)
    assert window_info("blackmanharris", n).enbw_bins == pytest.approx(2.0044, abs=1e-4)
    assert window_info("blackmanharris", n).scallop_loss_db == pytest.approx(0.83, abs=0.01)
    assert window_info("flattop", n).scallop_loss_db < 0.01


@pytest.mark.parametrize("name", WINDOW_NAMES)
def test_sidelobe_nominal_matches_numeric(name: str) -> None:
    n, pad = 2048, 2 ** 18
    w = get_window(name, n)
    spec = np.abs(np.fft.fft(w, pad)) ** 2
    spec /= spec[0]
    per_bin = pad // n
    main_lobe = len(WINDOW_COEFFS[name]) * per_bin       # first null of a K-term window at K bins
    sidelobe = 10 * np.log10(spec[main_lobe:pad // 2].max())
    assert window_info(name, n).sidelobe_db == pytest.approx(sidelobe, abs=0.1)


@pytest.mark.parametrize("name", ["hann", "hamming", "blackman", "blackmanharris", "flattop"])
def test_matches_scipy_periodic_windows(name: str) -> None:
    signal = pytest.importorskip("scipy.signal")
    ref = signal.get_window(name, 1000, fftbins=True)
    np.testing.assert_allclose(get_window(name, 1000), ref, rtol=0, atol=1e-12)


def test_bin_correlation_closed_form_and_monte_carlo() -> None:
    np.testing.assert_allclose(bin_correlation("rect", 512, 3), [1, 0, 0, 0], atol=1e-14)
    # Hann: w^2 = 3/8 - 1/2 cos + 1/8 cos2  ->  rho(1) = (1/4 / 3/8)^2, rho(2) = (1/16 / 3/8)^2
    np.testing.assert_allclose(bin_correlation("hann", 512, 3), [1, 4 / 9, 1 / 36, 0], atol=1e-14)
    # Monte Carlo for Blackman-Harris: corr(|X_k|^2, |X_k+1|^2) of white noise segments
    rng = np.random.default_rng(3)
    n = 256
    x = rng.standard_normal((400, n)) + 1j * rng.standard_normal((400, n))
    p = np.abs(np.fft.fft(x * get_window("blackmanharris", n), axis=1)) ** 2
    a, b = p[:, :-1].ravel(), p[:, 1:].ravel()
    measured = np.corrcoef(a, b)[0, 1]
    assert measured == pytest.approx(bin_correlation("blackmanharris", n, 1)[1], abs=0.02)


def test_window_cache_is_bounded_in_bytes(monkeypatch: pytest.MonkeyPatch) -> None:
    from eds.dsp import windows as win

    monkeypatch.setattr(win, "WINDOW_CACHE_MAX_BYTES", 8 * 4096 * 4)
    for n in range(1000, 1040):
        w = get_window("hann", n)
        assert not w.flags.writeable
    assert win._cache_bytes <= win.WINDOW_CACHE_MAX_BYTES
    assert sum(v.nbytes for v in win._cache.values()) == win._cache_bytes
    big = get_window("hann", 8 * 4096 * 4)       # larger than a quarter of the cap: not cached, still valid
    assert big.shape == (8 * 4096 * 4,) and not big.flags.writeable
    assert ("hann", 8 * 4096 * 4) not in win._cache


@pytest.mark.parametrize("name", WINDOW_NAMES)
def test_enbw_independent_of_length(name: str) -> None:
    ref = window_info(name, 2 ** 20).enbw_bins
    for n in (64, 100, 4096, 12345):
        assert window_info(name, n).enbw_bins == pytest.approx(ref, rel=1e-12)
