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


# =========================================================================== weighted gamma sums (RF-03)
def _bh_block_eigenvalues(d: int, nfft: int = 4096) -> np.ndarray:
    """Eigenvalues of the d x d amplitude-correlation matrix of adjacent Blackman-Harris bins
    (independent re-statement: r = DFT(w^2) / sum(w^2), Toeplitz)."""
    w2 = get_window("blackmanharris", nfft) ** 2
    r = np.fft.fft(w2).real / w2.sum()
    lag = np.abs(np.arange(d)[:, None] - np.arange(d)[None, :])
    return np.linalg.eigvalsh(np.where(lag < 8, r[np.minimum(lag, 7)], 0.0))


def _exact_sf_exponential_sum(x: float, a: np.ndarray) -> float:
    """Exact P(sum a_i E_i > x), E_i ~ Exp(1), distinct a_i (partial fractions, 80 digits)."""
    from decimal import Decimal, localcontext

    with localcontext() as ctx:
        ctx.prec = 80
        aa = [Decimal(float(v)) for v in a if v > 1e-9]
        xx = Decimal(float(x))
        total = Decimal(0)
        for i, ai in enumerate(aa):
            c = Decimal(1)
            for j, aj in enumerate(aa):
                if j != i:
                    c *= ai / (ai - aj)
            total += c * (-xx / ai).exp()
        return float(total)


def test_weighted_gamma_sf_reduces_to_the_gamma_survival() -> None:
    from eds.dsp.stats import weighted_gamma_sf

    for shape in (1.0, 2.0, 4.0, 13.7):
        for x in (0.3, 1.0, 3.0, 10.0, 40.0):
            xs = x * shape
            exact = regularized_gamma_q(shape, xs)
            tol = 0.008 if shape < 2 else 0.002
            assert weighted_gamma_sf(xs, [1.0], shape) == pytest.approx(exact, rel=tol), (shape, x)
    # D equal weights of shape n == Gamma(n D); vectorized form; limits
    v = weighted_gamma_sf(np.array([0.0, 2.0, 5.0, np.inf]), [0.25] * 4, 1.0)
    assert v.shape == (4,) and v[0] == 1.0 and v[-1] == 0.0
    assert v[1] == pytest.approx(regularized_gamma_q(4.0, 8.0), rel=2e-3)
    assert v[2] == pytest.approx(regularized_gamma_q(4.0, 20.0), rel=2e-3)


def test_block_mean_tail_is_heavier_than_the_moment_matched_gamma() -> None:
    """RF-03: the D = 16 Blackman-Harris block mean (n = 1) against its exact distribution."""
    from eds.dsp.stats import weighted_gamma_sf

    d = 16
    lam = _bh_block_eigenvalues(d)
    a = lam / d                                              # mean 1
    d_eff = d * d / float(np.sum(lam ** 2))
    assert d_eff == pytest.approx(6.10, abs=0.01) and a.max() * d_eff == pytest.approx(1.426, abs=0.005)
    for target in (1e-2, 1e-4, 1e-7, 1e-10):
        x = gamma_isf(target, d_eff) / d_eff                 # moment-matched threshold
        exact = _exact_sf_exponential_sum(x, a)
        assert weighted_gamma_sf(x, a, 1.0) == pytest.approx(exact, rel=0.01), target
        if target <= 1e-7:
            assert exact > 3.0 * target                      # the old model's false-alarm excess


def test_block_mean_tail_monte_carlo() -> None:
    """Saddlepoint tail of the eigen model = empirical tail of real windowed-FFT block means."""
    from eds.dsp.stats import weighted_gamma_sf

    rng = np.random.default_rng(3)
    nfft, d = 1024, 16
    w = get_window("blackmanharris", nfft)
    vals = []
    for _ in range(8):
        x = rng.standard_normal((256, nfft)) + 1j * rng.standard_normal((256, nfft))
        p = np.abs(np.fft.fft(x * w, axis=1)) ** 2 / (2.0 * np.sum(w * w))
        vals.append(p.reshape(-1, d).mean(axis=1))
    v = np.concatenate(vals)                                  # 131072 block means, mean 1
    lam = _bh_block_eigenvalues(d, nfft)
    for q in (1e-2, 1e-3):
        x = float(np.quantile(v, 1.0 - q))
        assert weighted_gamma_sf(x, lam / d, 1.0) == pytest.approx(q, rel=0.25 if q < 1e-2 else 0.08)


def test_cfar_alphas_with_a_cut_model() -> None:
    from eds.dsp.stats import CellModel

    # a single component of shape n and weight 1/n IS Gamma(n, 1/n): same multipliers
    for n in (1.0, 4.0):
        cut = CellModel((1.0 / n,), n)
        assert os_cfar_alpha(1e-5, n, 32, 24, cut=cut) == pytest.approx(os_cfar_alpha(1e-5, n, 32, 24), rel=3e-3)
        assert ca_cfar_alpha(1e-5, n, 32.0, cut=cut) == pytest.approx(ca_cfar_alpha(1e-5, n, 32.0), rel=3e-3)
    # heavier CUT tail -> larger multiplier
    lam = _bh_block_eigenvalues(16)
    heavy = CellModel(tuple(lam / 16), 1.0)
    d_eff = 16 * 16 / float(np.sum(lam ** 2))
    assert os_cfar_alpha(1e-6, d_eff, 62, 46, cut=heavy) > 1.05 * os_cfar_alpha(1e-6, d_eff, 62, 46)
    with pytest.raises(ConfigError):
        CellModel((), 1.0)
    with pytest.raises(ConfigError):
        CellModel((0.5, -0.1), 1.0)
    with pytest.raises(ConfigError):
        CellModel((0.5,), 0.0)
    with pytest.raises(ConfigError):                          # must be normalised to mean 1
        os_cfar_alpha(1e-3, 1.0, 32, 24, cut=CellModel((0.5,), 1.0))
