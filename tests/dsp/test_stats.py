"""eds.dsp.stats: incomplete gamma, quantiles, CFAR multipliers, Welch effective averages."""

from __future__ import annotations

import math

import numpy as np
import pytest

from eds.core.errors import ConfigError
from eds.dsp import (
    ca_cfar_alpha,
    cfar_alpha,
    effective_averages,
    gamma_isf,
    gamma_quantile,
    get_window,
    os_cfar_alpha,
    os_cfar_scale,
    os_rank_index,
    regularized_gamma_p,
    regularized_gamma_q,
    welch,
)
from tests.dsp.helpers import cnoise


@pytest.mark.parametrize("x", [1e-8, 0.3, 1.0, 2.5, 7.0, 40.0])
def test_regularized_gamma_closed_forms(x: float) -> None:
    assert regularized_gamma_p(1.0, x) == pytest.approx(-math.expm1(-x), rel=1e-13)
    assert regularized_gamma_q(1.0, x) == pytest.approx(math.exp(-x), rel=1e-13)
    p2 = x * x / 2 - x ** 3 / 3 + x ** 4 / 8 if x < 1e-3 else -math.expm1(-x) - x * math.exp(-x)
    assert regularized_gamma_p(2.0, x) == pytest.approx(p2, rel=1e-11)
    assert regularized_gamma_p(0.5, x) == pytest.approx(math.erf(math.sqrt(x)), rel=1e-13)
    assert regularized_gamma_q(0.5, x) == pytest.approx(math.erfc(math.sqrt(x)), rel=1e-12)
    assert regularized_gamma_p(3.3, x) + regularized_gamma_q(3.3, x) == pytest.approx(1.0, abs=1e-14)


def test_regularized_gamma_matches_scipy() -> None:
    special = pytest.importorskip("scipy.special")
    for k in (0.1, 0.9, 4.0, 16.0, 150.0, 3000.0):
        for x in (1e-3, 0.5 * k, k, k + 1, 1.5 * k + 5, 3 * k + 30):
            assert regularized_gamma_p(k, x) == pytest.approx(special.gammainc(k, x), rel=1e-11, abs=1e-300)
            assert regularized_gamma_q(k, x) == pytest.approx(special.gammaincc(k, x), rel=1e-10, abs=1e-300)


def test_gamma_quantile_closed_form_k1() -> None:
    for p in (1e-12, 1e-3, 0.5, 0.9, 1 - 1e-6):
        assert gamma_quantile(p, 1.0) == pytest.approx(-math.log1p(-p), rel=1e-10)
    for q in (1e-300, 1e-12, 1e-6, 0.3, 0.999):
        assert gamma_isf(q, 1.0) == pytest.approx(-math.log(q), rel=1e-10)


def test_gamma_quantile_matches_scipy_to_1e8() -> None:
    special = pytest.importorskip("scipy.special")
    for k in (0.05, 0.3, 1.0, 2.5, 7.98, 15.7, 100.0, 1000.0, 2e4):
        for p in (1e-200, 1e-12, 1e-6, 0.01, 0.3, 0.5, 0.9, 1 - 1e-6):
            ref = special.gammaincinv(k, p)
            if ref == 0.0:
                continue
            assert gamma_quantile(p, k) == pytest.approx(ref, rel=1e-8)
            assert regularized_gamma_p(k, gamma_quantile(p, k)) == pytest.approx(p, rel=1e-8)
        for q in (1e-300, 1e-12, 1e-6, 0.01, 0.5, 0.9, 1 - 1e-6):
            assert gamma_isf(q, k) == pytest.approx(special.gammainccinv(k, q), rel=1e-8)


def test_cfar_alpha() -> None:
    for pfa in (1e-2, 1e-6, 1e-12):
        assert cfar_alpha(pfa, 1) == pytest.approx(-math.log(pfa), rel=1e-12)
    special = pytest.importorskip("scipy.special")
    for n in (2, 7.98, 16, 64.5):
        for pfa in (1e-3, 1e-6):
            assert cfar_alpha(pfa, n) == pytest.approx(special.gammaincinv(n, 1 - pfa) / n, rel=1e-8)
    # more averages -> narrower noise distribution -> smaller multiplier, tending to 1
    a = [cfar_alpha(1e-6, n) for n in (1, 4, 16, 64, 1e4)]
    assert all(x > y for x, y in zip(a, a[1:])) and a[-1] == pytest.approx(1.0, abs=0.06)


def test_cfar_alpha_monte_carlo() -> None:
    rng = np.random.default_rng(12)
    n, pfa = 4.0, 1e-3
    x = rng.gamma(n, 1 / n, 2_000_000)
    assert np.mean(x > cfar_alpha(pfa, n)) == pytest.approx(pfa, rel=0.1)


@pytest.mark.parametrize("m", [8, 16, 64])
@pytest.mark.parametrize("pfa", [1e-3, 1e-6])
def test_ca_cfar_alpha_closed_form_exponential(m: int, pfa: float) -> None:
    # exponential noise, M-cell mean estimate: Pfa = (1 + alpha/M)^-M
    assert ca_cfar_alpha(pfa, 1.0, m) == pytest.approx(m * (pfa ** (-1.0 / m) - 1.0), rel=1e-4)


def test_ca_cfar_alpha_matches_f_distribution() -> None:
    stats = pytest.importorskip("scipy.stats")
    for n in (4.0, 14.9):
        for m in (16, 64):
            # X / mean_train ~ F(2n, 2nM)
            assert ca_cfar_alpha(1e-6, n, m) == pytest.approx(stats.f.isf(1e-6, 2 * n, 2 * m * n), rel=1e-4)
    assert ca_cfar_alpha(1e-6, 8.0, 1e6) == pytest.approx(cfar_alpha(1e-6, 8.0), rel=1e-3)


def _rohling_beta(pfa: float, m: int, k: int) -> float:
    """Exponential-noise OS-CFAR: prod_{i<k} (M-i)/(M-i+beta) = pfa, solved by bisection."""
    lo, hi = 1e-6, 1e12
    for _ in range(200):
        mid = math.sqrt(lo * hi)
        val = sum(math.log((m - i) / (m - i + mid)) for i in range(k))
        if val > math.log(pfa):
            lo = mid
        else:
            hi = mid
    return math.sqrt(lo * hi)


@pytest.mark.parametrize("m,k", [(64, 48), (24, 18), (32, 24), (16, 16), (16, 1)])
@pytest.mark.parametrize("pfa", [1e-3, 1e-6])
def test_os_cfar_alpha_closed_form_exponential(m: int, k: int, pfa: float) -> None:
    beta = os_cfar_alpha(pfa, 1.0, m, k) / os_cfar_scale(1.0, m, k)
    assert beta == pytest.approx(_rohling_beta(pfa, m, k), rel=2e-4)


def test_os_scale_and_rank() -> None:
    # exponential: E[X_(k)] = sum_{i=M-k+1}^{M} 1/i ; the (k-0.5)/M quantile is close to it
    m, k = 64, 48
    exact = sum(1.0 / i for i in range(m - k + 1, m + 1))
    assert os_cfar_scale(1.0, m, k) == pytest.approx(exact, rel=0.01)
    assert os_rank_index(0.75, 64) == 48
    assert os_rank_index(1.0, 10) == 10 and os_rank_index(0.01, 10) == 1
    with pytest.raises(ConfigError):
        os_rank_index(0.0, 10)


@pytest.mark.parametrize("name", ["rect", "hann", "blackmanharris"])
def test_effective_averages_formula(name: str) -> None:
    nfft, k = 512, 16
    assert effective_averages(k, 0.0, name, nfft) == pytest.approx(k)
    w = get_window(name, nfft)
    rho1 = (np.dot(w[:256], w[256:]) / np.dot(w, w)) ** 2
    expected = k / (1 + 2 * (1 - 1 / k) * rho1)
    assert effective_averages(k, 0.5, name, nfft) == pytest.approx(expected, rel=1e-12)
    assert effective_averages(1, 0.5, name, nfft) == 1.0


def test_effective_averages_monte_carlo_rect_50pct() -> None:
    # rect window, 50% overlap: rho(1) = 1/4 -> n_eff = K / (1 + (1 - 1/K)/2) — strong effect
    rng = np.random.default_rng(31)
    nfft, k = 64, 16
    n_eff = effective_averages(k, 0.5, "rect", nfft)
    assert n_eff == pytest.approx(k / (1 + 0.5 * (1 - 1 / k)), rel=1e-12)
    vals = []
    for _ in range(300):
        s = welch(cnoise(rng, nfft + 32 * (k - 1), 1.0), 1.0, nfft, window="rect", overlap=0.5)
        vals.append(s.p_lin)
    p = np.concatenate(vals)
    assert 1.0 / (np.var(p) / np.mean(p) ** 2) == pytest.approx(n_eff, rel=0.05)


def test_invalid_arguments() -> None:
    with pytest.raises(ConfigError):
        gamma_quantile(1.0, 2.0)
    with pytest.raises(ConfigError):
        gamma_quantile(0.5, -1.0)
    with pytest.raises(ConfigError):
        cfar_alpha(0.0, 4)
    with pytest.raises(ConfigError):
        regularized_gamma_p(2.0, -1.0)
    with pytest.raises(ConfigError):
        effective_averages(0, 0.5, "hann", 64)
    with pytest.raises(ConfigError):
        os_cfar_alpha(1e-3, 1.0, 16, 17)
