"""Statistics for spectral detection: incomplete gamma, gamma quantiles, CFAR multipliers.

Noise model
-----------
For complex white Gaussian noise of power ``sigma^2`` per bin, one periodogram bin is
exponential: ``|X|^2 ~ sigma^2 * Gamma(1, 1)``. The **mean** of ``n`` independent such
bins (Welch averaging) is ``sigma^2 * Gamma(n, 1/n)`` (shape ``n``, scale ``1/n``, mean
``sigma^2``). Overlapping Welch segments are correlated; :func:`effective_averages` gives the
equivalent number of independent averages ``n_eff`` (Welch 1967), which is then used as the
(non-integer) gamma shape.

CFAR multipliers
----------------
* :func:`cfar_alpha` — ideal multiplier when the noise mean is *known*:
  ``P(X > alpha sigma^2) = pfa``.
* :func:`ca_cfar_alpha` / :func:`os_cfar_alpha` — the noise mean is *estimated* from ``M``
  training cells, which raises the false-alarm rate for the same multiplier. The
  **finite-M correction** here is exact under the model "training cells are independent
  ``Gamma(n, 1/n)`` noise": the false-alarm probability

      Pfa(alpha) = E_Y[ Q(n, n * alpha * Y) ]

  (``Y`` = normalized noise estimate, ``Q`` = upper regularized incomplete gamma) is
  evaluated by numerical integration over the exact distribution of ``Y`` (CA: ``Y ~
  Gamma(nM, 1/(nM))``; OS: ``Y`` = rank-``k`` order statistic of ``M`` cells, scaled) and
  solved for ``alpha``. For ``n = 1`` this reproduces the textbook closed forms
  (``alpha_CA = M (pfa^{-1/M} - 1)``; OS: ``prod_{i<k} (M-i)/(M-i+beta) = pfa``). The
  correction in dB is ``10 log10(alpha_finite / alpha_ideal)`` (positive).

Cells under test that are weighted gamma sums
---------------------------------------------
A bin of a block-averaged (coarse) spectrum is the mean of ``D`` *correlated* native bins.
For Gaussian noise it is a quadratic form whose eigen-decomposition gives independent
components: ``X = sum_i a_i G_i`` with ``G_i ~ Gamma(n, 1)`` iid and ``a_i`` proportional
to the eigenvalues of the ``D x D`` bin covariance (:class:`CellModel`). Its tail is
governed by the *largest* eigenvalue and is heavier than that of the moment-matched
``Gamma(n D_eff)``, so CFAR multipliers derived from the moment-matched model give a
false-alarm rate several times the design value at ``pfa <= 1e-5``. The ``cut=`` argument
of :func:`ca_cfar_alpha` / :func:`os_cfar_alpha` replaces the CUT's gamma survival by the
survival of such a sum, computed with the second-order Lugannani-Rice saddlepoint
approximation (:func:`weighted_gamma_sf`; relative error < 1 % in the tail, exact
cumulant generating function ``K(s) = -n sum_i log(1 - a_i s)``). The training-cell
estimate keeps its moment-matched gamma model (its distribution is set by the body, not
the tail, of the cell distribution).

Only numpy + :mod:`math` are used (no scipy in ``src/``).
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from functools import lru_cache

import numpy as np

from ..core.errors import ConfigError, MeasurementError
from .windows import canonical_window_name, get_window

_EPS = 4.0 * np.finfo(np.float64).eps
_FPMIN = np.finfo(np.float64).tiny / _EPS
_XMIN = float(np.finfo(np.float64).tiny)     # smallest normal double
_GRID_POINTS = 1601          # integration grid for the finite-M CFAR correction
_TAIL_LO = 1e-4              # neglected lower-tail mass of the noise estimate, relative to pfa
_TAIL_HI = 1e-16             # neglected upper-tail mass of the noise estimate


# --------------------------------------------------------------------------- validation
def _check_shape(k: float) -> float:
    k = float(k)
    if not (math.isfinite(k) and k > 0.0):
        raise ConfigError(f"Gamma şekil parametresi pozitif ve sonlu olmalı, {k!r} geldi")
    return k


def _check_prob(p: float, what: str) -> float:
    p = float(p)
    if not (0.0 < p < 1.0):
        raise ConfigError(f"{what} (0, 1) aralığında olmalı, {p!r} geldi")
    return p


# --------------------------------------------------------------------------- incomplete gamma
def _max_iter(a: float) -> int:
    return 300 + int(40.0 * math.sqrt(a))


def _gamma_log_pq(a: float, x: np.ndarray) -> tuple[np.ndarray, np.ndarray]:
    """Vectorized ``(log P(a, x), log Q(a, x))`` for scalar shape ``a`` and array ``x >= 0``.

    Numerical Recipes ``gammp``/``gammq``: power series for ``x < a + 1`` and the modified
    Lentz continued fraction otherwise. Both tails are returned in the log domain so that
    probabilities far below 1e-300 are still usable.
    """
    x = np.asarray(x, dtype=np.float64)
    log_p = np.empty_like(x)
    log_q = np.empty_like(x)
    zero = x <= 0.0
    log_p[zero] = -np.inf
    log_q[zero] = 0.0
    gln = math.lgamma(a)
    ser = (~zero) & (x < a + 1.0)
    cfr = (~zero) & (~ser)
    maxit = _max_iter(a)

    if np.any(ser):
        xs = x[ser]
        ap = a
        term = np.full_like(xs, 1.0 / a)
        total = term.copy()
        for _ in range(maxit):
            ap += 1.0
            term *= xs / ap
            total += term
            if np.all(np.abs(term) < np.abs(total) * _EPS):
                break
        else:
            raise MeasurementError(f"Eksik gamma serisi yakınsamadı (a={a:g})")
        lp = np.log(total) - xs + a * np.log(xs) - gln
        log_p[ser] = lp
        log_q[ser] = np.log1p(-np.minimum(np.exp(lp), 1.0))

    if np.any(cfr):
        xc = x[cfr]
        b = xc + 1.0 - a
        c = np.full_like(xc, 1.0 / _FPMIN)
        d = 1.0 / b
        h = d.copy()
        for i in range(1, maxit + 1):
            an = -i * (i - a)
            b = b + 2.0
            d = an * d + b
            d = np.where(np.abs(d) < _FPMIN, _FPMIN, d)
            c = b + an / c
            c = np.where(np.abs(c) < _FPMIN, _FPMIN, c)
            d = 1.0 / d
            delta = d * c
            h *= delta
            if np.all(np.abs(delta - 1.0) < _EPS):
                break
        else:
            raise MeasurementError(f"Eksik gamma sürekli kesri yakınsamadı (a={a:g})")
        lq = np.log(h) - xc + a * np.log(xc) - gln
        log_q[cfr] = lq
        log_p[cfr] = np.log1p(-np.minimum(np.exp(lq), 1.0))
    return log_p, log_q


def regularized_gamma_p(k: float, x: float) -> float:
    """Lower regularized incomplete gamma ``P(k, x) = gamma(k, x) / Gamma(k)``."""
    k = _check_shape(k)
    x = float(x)
    if not (x >= 0.0) or math.isnan(x):
        raise ConfigError(f"x >= 0 olmalı, {x!r} geldi")
    if math.isinf(x):
        return 1.0
    log_p, _ = _gamma_log_pq(k, np.array([x]))
    return float(np.exp(log_p[0]))


def regularized_gamma_q(k: float, x: float) -> float:
    """Upper regularized incomplete gamma ``Q(k, x) = 1 - P(k, x)`` (accurate in the tail)."""
    k = _check_shape(k)
    x = float(x)
    if not (x >= 0.0) or math.isnan(x):
        raise ConfigError(f"x >= 0 olmalı, {x!r} geldi")
    if math.isinf(x):
        return 0.0
    _, log_q = _gamma_log_pq(k, np.array([x]))
    return float(np.exp(log_q[0]))


# --------------------------------------------------------------------------- quantiles
def _norm_ppf_approx(p: float) -> float:
    """Standard normal quantile, Abramowitz & Stegun 26.2.23 (|err| < 4.5e-4).

    Only used as an initial guess; ``p`` in (0, 1)."""
    if p > 0.5:
        return -_norm_ppf_approx(1.0 - p)
    t = math.sqrt(-2.0 * math.log(p))
    num = 2.515517 + t * (0.802853 + t * 0.010328)
    den = 1.0 + t * (1.432788 + t * (0.189269 + t * 0.001308))
    return -(t - num / den)


def _initial_guess(target: float, k: float, upper: bool) -> float:
    """Wilson-Hilferty (k >= 1), asymptotic upper tail, or small-shape approximations."""
    if upper and -math.log(target) > 4.0 * k + 10.0:
        # far upper tail: Q(k, x) ~ x^(k-1) e^-x / Gamma(k); fixed-point refinements
        x0 = max(-math.log(target) - math.lgamma(k), 1.0)
        for _ in range(4):
            x0 = max(-math.log(target) - math.lgamma(k) + (k - 1.0) * math.log(x0), 1.0)
        return x0
    z = -_norm_ppf_approx(target) if upper else _norm_ppf_approx(target)
    if k >= 1.0:
        x0 = k * (1.0 - 1.0 / (9.0 * k) + z / (3.0 * math.sqrt(k))) ** 3
        if x0 > 0.0:
            return x0
    if not upper:
        # P(k, x) ~ x^k / Gamma(k + 1) for small x
        return math.exp((math.log(target) + math.lgamma(k + 1.0)) / k)
    return max(-math.log(target), 1e-3)


def _gamma_inverse(target: float, k: float, upper: bool) -> float:
    """Solve ``P(k, x) = target`` (``upper=False``) or ``Q(k, x) = target`` (``upper=True``).

    The equation is always solved on the side whose probability is ``<= 0.5`` so the
    residual is computed without cancellation. Halley iteration (as in Numerical Recipes
    ``invgammp``) safeguarded by a bisection bracket.
    """
    if target > 0.5:
        target, upper = 1.0 - target, not upper
    gln = math.lgamma(k)
    lo, hi = _XMIN, math.inf
    if not upper:
        log_p_min, _ = _gamma_log_pq(k, np.array([_XMIN]))
        if float(log_p_min[0]) >= math.log(target):
            return 0.0      # quantile below the smallest normal double
    x = _initial_guess(target, k, upper)
    if not (math.isfinite(x) and x > _XMIN):
        x = 2.0 * _XMIN
    prev_abs_f = math.inf
    for _ in range(400):
        log_p, log_q = _gamma_log_pq(k, np.array([x]))
        val = float(np.exp(log_q[0] if upper else log_p[0]))
        f = val - target
        if f == 0.0:
            return x
        too_small = (f > 0.0) if upper else (f < 0.0)
        if too_small:
            lo = x
        else:
            hi = x
        x_new = math.nan
        log_dens = (k - 1.0) * math.log(x) - x - gln
        if abs(f) <= 0.5 * prev_abs_f and log_dens < 700.0:   # Halley only while it makes real progress
            dens = math.exp(log_dens)
            if dens > 0.0:
                u = f / (-dens if upper else dens)
                x_new = x - u / (1.0 - 0.5 * min(1.0, u * ((k - 1.0) / x - 1.0)))
        prev_abs_f = abs(f)
        if not (lo < x_new < hi):
            if not math.isfinite(hi):
                x_new = 2.0 * max(x, lo)
            elif hi > 4.0 * lo:
                x_new = math.sqrt(lo * hi)          # geometric bisection over decades
            else:
                x_new = 0.5 * (lo + hi)
            prev_abs_f = math.inf                   # let Halley try again from the new point
        if abs(x_new - x) <= 1e-13 * abs(x_new) or (math.isfinite(hi) and hi - lo <= 1e-13 * hi):
            return x_new
        x = x_new
    raise MeasurementError(f"Gamma ters fonksiyonu yakınsamadı (hedef={target:g}, k={k:g})")


def gamma_quantile(p: float, k: float) -> float:
    """Inverse of the lower regularized incomplete gamma: ``x`` with ``P(k, x) = p``.

    Shape ``k``, unit scale. Relative accuracy better than 1e-10 for ``p`` in
    ``[1e-300, 1 - 1e-6]`` (for ``p`` closer to 1 use :func:`gamma_isf`)."""
    return _gamma_inverse(_check_prob(p, "Olasılık p"), _check_shape(k), upper=False)


def gamma_isf(q: float, k: float) -> float:
    """Inverse survival function: ``x`` with ``Q(k, x) = q`` (accurate for tiny ``q``)."""
    return _gamma_inverse(_check_prob(q, "Olasılık q"), _check_shape(k), upper=True)


# --------------------------------------------------------------------------- weighted gamma sums
_SQRT_2PI = math.sqrt(2.0 * math.pi)
_LOG_SQRT_2PI = 0.5 * math.log(2.0 * math.pi)
_W_SMALL = 0.05              # |w| below this: limit form of the saddlepoint correction
_MILLS_CF_FROM = 8.0         # Mills ratio by continued fraction from here on
_MILLS_CF_TERMS = 80
_TABLE_TAIL_DECADES = 14.0   # saddlepoint table: 1 - a_max s from 1 down to 1e-14 ...
_TABLE_BODY_DECADES = 8.0    # ... and from 1 up to 1e8 (s < 0, x below the mean)
_TABLE_POINTS = 6001


@dataclass(frozen=True)
class CellModel:
    """Cell-under-test distribution ``X = sum_i weights[i] * G_i``, ``G_i ~ Gamma(shape, 1)`` iid.

    The mean is ``shape * sum(weights)`` (normally 1: the CFAR noise estimates are noise
    *means*). Hashable, so CFAR multipliers that use it are cached. See the module
    docstring; built e.g. by :func:`eds.dsp.detect.coarse_cell_model`.
    """

    weights: tuple[float, ...]
    shape: float

    def __post_init__(self) -> None:
        w = tuple(float(v) for v in self.weights)
        if not w or any(not (math.isfinite(v) and v >= 0.0) for v in w) or max(w) <= 0.0:
            raise ConfigError(f"CellModel ağırlıkları sonlu, negatif olmayan ve en az biri pozitif olmalı, "
                              f"{self.weights!r} geldi")
        object.__setattr__(self, "weights", w)
        object.__setattr__(self, "shape", _check_shape(self.shape))

    @property
    def mean(self) -> float:
        return self.shape * math.fsum(self.weights)


def _cgf_parts(a: np.ndarray, c: np.ndarray, v: np.ndarray) -> tuple[np.ndarray, ...]:
    """``(s, K, K', K'', K''', K'''')`` at ``s = (1 - v) / a_max`` (``v > 0``; rows = ``v``).

    ``1 - a_i s`` is formed as ``(a_max - a_i + a_i v) / a_max`` so it stays exact for the
    largest component when ``v`` is tiny (far tail).
    """
    a_max = float(a[0])
    vv = v[:, None]
    om = (a_max - a[None, :] + a[None, :] * vv) / a_max          # 1 - a_i s  (> 0)
    r = a[None, :] / om
    s = (1.0 - v) / a_max
    k0 = -np.sum(c * np.log(om), axis=1)
    k1 = np.sum(c * r, axis=1)
    r2 = r * r
    k2 = np.sum(c * r2, axis=1)
    k3 = 2.0 * np.sum(c * r2 * r, axis=1)
    k4 = 6.0 * np.sum(c * r2 * r2, axis=1)
    return s, k0, k1, k2, k3, k4


def _mills_ratio(w: np.ndarray) -> np.ndarray:
    """``Q_N(w) / phi(w)`` for ``w > 0`` (no underflow for large ``w``)."""
    out = np.empty_like(w)
    small = w < _MILLS_CF_FROM
    if np.any(small):
        ws = w[small]
        q = 0.5 * np.array([math.erfc(float(t) / math.sqrt(2.0)) for t in ws])
        out[small] = q * _SQRT_2PI * np.exp(0.5 * ws * ws)
    if not np.all(small):
        wl = w[~small]
        t = np.zeros_like(wl)
        for k in range(_MILLS_CF_TERMS, 0, -1):
            t = k / (wl + t)
        out[~small] = 1.0 / (wl + t)
    return out


def _lr_log_sf(a: np.ndarray, c: np.ndarray, v: np.ndarray) -> tuple[np.ndarray, np.ndarray]:
    """``(x, log P(X > x))`` at the saddlepoints ``s = (1 - v)/a_max`` (second-order
    Lugannani-Rice, Daniels 1987)."""
    s, k0, x, k2, k3, k4 = _cgf_parts(a, c, v)
    w = np.sign(s) * np.sqrt(np.maximum(2.0 * (s * x - k0), 0.0))
    u = s * np.sqrt(k2)
    l3 = k3 / k2 ** 1.5
    l4 = k4 / (k2 * k2)
    with np.errstate(divide="ignore", invalid="ignore"):
        corr = ((1.0 / u - 1.0 / w) + (l4 / 8.0 - 5.0 * l3 * l3 / 24.0) / u - 1.0 / u ** 3
                - l3 / (2.0 * u * u) + 1.0 / w ** 3)
    near = np.abs(w) < _W_SMALL
    corr = np.where(near, -l3 / 6.0, corr)                  # lim_{w->0} (1/u - 1/w) = -lambda_3 / 6
    log_phi = -0.5 * w * w - _LOG_SQRT_2PI
    log_sf = np.empty_like(w)
    pos = w > 0.0
    if np.any(pos):
        m = _mills_ratio(w[pos]) + corr[pos]
        bad = m <= 0.0                                       # never seen in practice: LR1, then plain normal tail
        if np.any(bad):
            m1 = _mills_ratio(w[pos]) + np.where(near[pos], -l3[pos] / 6.0, 1.0 / u[pos] - 1.0 / w[pos])
            m = np.where(bad, np.where(m1 > 0.0, m1, _mills_ratio(w[pos])), m)
        log_sf[pos] = log_phi[pos] + np.log(m)
    neg = ~pos
    if np.any(neg):
        q = 0.5 * np.array([math.erfc(float(t) / math.sqrt(2.0)) for t in w[neg]])
        sf = q + np.exp(log_phi[neg]) * corr[neg]
        log_sf[neg] = np.log(np.clip(sf, _XMIN, 1.0))
    return x, np.minimum(log_sf, 0.0)


_EXACT_COMPONENTS = 64       # largest weights kept exactly ...
_GROUPED_COMPONENTS = 192    # ... the rest merged into this many groups of neighbouring weights


def _components(model: CellModel) -> tuple[np.ndarray, np.ndarray]:
    """Weights sorted descending (negligible ones dropped) and the per-component shapes.

    Beyond :data:`_EXACT_COMPONENTS` components, the smaller (sorted, hence neighbouring)
    weights are merged in contiguous groups: weight = group mean, shape = shape x group size
    (mean preserved; the tail is set by the largest weights, which stay exact)."""
    a = np.sort(np.asarray(model.weights, dtype=np.float64))[::-1]
    a = a[a > a[0] * 1e-12]
    c = np.full(a.size, model.shape)
    if a.size <= _EXACT_COMPONENTS + _GROUPED_COMPONENTS:
        return a, c
    rest = np.array_split(a[_EXACT_COMPONENTS:], _GROUPED_COMPONENTS)
    a_g = np.array([float(np.mean(g)) for g in rest])
    c_g = np.array([model.shape * g.size for g in rest], dtype=np.float64)
    return np.concatenate((a[:_EXACT_COMPONENTS], a_g)), np.concatenate((c[:_EXACT_COMPONENTS], c_g))


def weighted_gamma_sf(x, weights, shape: float):
    """``P(X > x)`` for ``X = sum_i weights[i] * G_i``, ``G_i ~ Gamma(shape, 1)`` iid.

    Second-order Lugannani-Rice saddlepoint approximation with the exact cumulant
    generating function (the saddlepoint is solved per ``x``); relative error in the upper
    tail against the exact gamma: 0.7 % for ``shape = 1``, < 0.15 % for ``shape >= 2``
    (~3 % for ``shape = 0.5``). Scalar in -> ``float``; array in -> ``float64`` array.
    """
    a, c = _components(CellModel(tuple(np.ravel(np.asarray(weights, dtype=np.float64)).tolist()), shape))
    xs = np.atleast_1d(np.asarray(x, dtype=np.float64))
    if np.any(np.isnan(xs)):
        raise ConfigError("weighted_gamma_sf: x NaN olamaz")
    out = np.ones_like(xs)
    act = xs > 0.0
    fin = act & np.isfinite(xs)
    out[act & ~np.isfinite(xs)] = 0.0
    if np.any(fin):
        xt = xs[fin]
        # K'(s) is increasing in s: bisection on log v, v = 1 - a_max s in (1e-60, 1e60)
        lo = np.full(xt.size, -138.0)
        hi = np.full(xt.size, 138.0)
        for _ in range(90):
            mid = 0.5 * (lo + hi)
            _, _, k1, _, _, _ = _cgf_parts(a, c, np.exp(mid))
            big = k1 > xt                                   # v too small -> raise it
            lo = np.where(big, mid, lo)
            hi = np.where(big, hi, mid)
        _, log_sf = _lr_log_sf(a, c, np.exp(0.5 * (lo + hi)))
        out[fin] = np.exp(log_sf)
    if np.ndim(x) == 0:
        return float(out[0])
    return out.reshape(np.shape(x))


@lru_cache(maxsize=64)
def _log_sf_table(model: CellModel) -> tuple[np.ndarray, np.ndarray]:
    """Saddlepoint table ``(x, log P(X > x))`` (ascending ``x``) for fast interpolation."""
    a, c = _components(model)
    v = np.concatenate((np.logspace(_TABLE_BODY_DECADES, 0.0, _TABLE_POINTS // 3, endpoint=False),
                        np.logspace(0.0, -_TABLE_TAIL_DECADES, _TABLE_POINTS - _TABLE_POINTS // 3)))
    x, log_sf = _lr_log_sf(a, c, v)
    order = np.argsort(x, kind="stable")
    x, log_sf = x[order], log_sf[order]
    log_sf = np.minimum.accumulate(log_sf)                   # survival is non-increasing
    return x, log_sf


def _cell_log_sf(model: CellModel, x: np.ndarray) -> np.ndarray:
    """``log P(X > x)`` for the cell model by interpolation of its saddlepoint table."""
    xt, lt = _log_sf_table(model)
    out = np.interp(x, xt, lt)
    hi = x > xt[-1]
    if np.any(hi):                                           # beyond the table: asymptotic slope
        slope = (lt[-1] - lt[-2]) / (xt[-1] - xt[-2])
        out[hi] = lt[-1] + slope * (x[hi] - xt[-1])
    out[x <= 0.0] = 0.0
    return out


# --------------------------------------------------------------------------- CFAR
def cfar_alpha(pfa: float, n_avg: float) -> float:
    """Ideal threshold multiplier for averaged ``|X|^2`` noise with *known* mean.

    A noise bin that is the mean of ``n_avg`` independent exponential values is
    ``Gamma(n_avg, 1/n_avg) * sigma^2``; ``alpha = gamma_quantile(1 - pfa, n_avg) / n_avg``
    gives ``P(X > alpha sigma^2) = pfa``. Computed through :func:`gamma_isf` so ``pfa`` down
    to 1e-300 keeps full precision. ``n_avg`` may be non-integer (effective averages).
    For ``n_avg = 1``: ``alpha = -ln(pfa)``.
    """
    pfa = _check_prob(pfa, "pfa")
    n = _check_shape(n_avg)
    return gamma_isf(pfa, n) / n


def os_rank_index(rank: float, n_train: int) -> int:
    """1-based order-statistic index ``k = round(rank * M)`` clamped to ``1..M``."""
    m = _check_cells(n_train)
    r = float(rank)
    if not (0.0 < r <= 1.0):
        raise ConfigError(f"OS-CFAR sırası (0, 1] aralığında olmalı, {rank!r} geldi")
    return int(min(m, max(1, round(r * m))))


def _check_cells(n_train: float) -> int:
    if isinstance(n_train, bool) or int(n_train) != n_train or int(n_train) < 1:
        raise ConfigError(f"Eğitim hücresi sayısı pozitif tamsayı olmalı, {n_train!r} geldi")
    return int(n_train)


def os_cfar_scale(n_avg: float, n_train: int, k: int) -> float:
    """Approximate expected value of the rank-``k`` (1-based) order statistic of ``n_train``
    ``Gamma(n_avg, 1/n_avg)`` cells, i.e. the noise-*mean*-normalized OS level:
    ``c_k = gamma_quantile((k - 0.5)/M, n) / n``. Dividing the measured order statistic by
    ``c_k`` gives an estimate of the noise mean."""
    n = _check_shape(n_avg)
    m = _check_cells(n_train)
    if not (1 <= int(k) <= m):
        raise ConfigError(f"OS-CFAR sırası 1..{m} aralığında olmalı, {k!r} geldi")
    return _os_cfar_scale_cached(round(float(n), 9), int(m), int(k))


@lru_cache(maxsize=1024)
def _os_cfar_scale_cached(n: float, m: int, k: int) -> float:
    return gamma_quantile((k - 0.5) / m, n) / n


def _log_grid(y_lo: float, y_hi: float) -> tuple[np.ndarray, np.ndarray]:
    t = np.linspace(math.log(y_lo), math.log(y_hi), _GRID_POINTS)
    trap = np.full(_GRID_POINTS, t[1] - t[0])
    trap[0] *= 0.5
    trap[-1] *= 0.5
    return t, trap


def _normalize_weights(log_density: np.ndarray, trap: np.ndarray) -> np.ndarray:
    w = np.exp(log_density - np.max(log_density)) * trap
    return w / np.sum(w)


def _ca_estimate_grid(n: float, m_cells: float, pfa: float) -> tuple[np.ndarray, np.ndarray]:
    """Grid + weights for Y = mean of ``m_cells`` cells: ``Gamma(nM, 1/(nM))``."""
    shape = n * m_cells
    y_lo = gamma_quantile(min(_TAIL_LO * pfa, 0.25), shape) / shape
    y_hi = gamma_isf(_TAIL_HI, shape) / shape
    t, trap = _log_grid(y_lo, y_hi)
    y = np.exp(t)
    # density of ln(Y): f_Y(y) * y
    log_density = shape * t - shape * y
    return y, _normalize_weights(log_density, trap)


def _os_estimate_grid(n: float, m: int, k: int, pfa: float) -> tuple[np.ndarray, np.ndarray]:
    """Grid + weights for Y = rank-``k`` order statistic of ``m`` ``Gamma(n, 1/n)`` cells."""
    ln_binom_lo = math.lgamma(m + 1) - math.lgamma(k + 1) - math.lgamma(m - k + 1)
    # P(Y < y) <= C(m, k) F(y)^k  ->  lower cut-off
    f_lo = math.exp((math.log(_TAIL_LO * pfa) - ln_binom_lo) / k)
    y_lo = gamma_quantile(min(f_lo, 0.25), n) / n
    # P(Y > y) <= C(m, m-k+1) Q(y)^(m-k+1)  ->  upper cut-off
    ln_binom_hi = math.lgamma(m + 1) - math.lgamma(m - k + 2) - math.lgamma(k)
    q_hi = math.exp((math.log(_TAIL_HI) - ln_binom_hi) / (m - k + 1))
    y_hi = gamma_isf(min(q_hi, 0.25), n) / n
    t, trap = _log_grid(y_lo, y_hi)
    y = np.exp(t)
    log_p, log_q = _gamma_log_pq(n, n * y)
    log_density = (k - 1) * log_p + (m - k) * log_q + n * t - n * y   # f_(k)(y) * y, unnormalized
    return y, _normalize_weights(log_density, trap)


def _solve_multiplier(pfa: float, n: float, y: np.ndarray, w: np.ndarray, cut: CellModel | None = None) -> float:
    """Solve ``sum_j w_j S(beta y_j) = pfa`` for ``beta`` (bisection in log beta); ``S`` is the
    CUT survival: ``Q(n, n x)`` (``Gamma(n, 1/n)``) or that of ``cut`` when given."""
    log_pfa = math.log(pfa)

    def log_pfa_of(beta: float) -> float:
        if cut is None:
            _, log_q = _gamma_log_pq(n, n * beta * y)
        else:
            log_q = _cell_log_sf(cut, beta * y)
        mx = float(np.max(log_q))
        return mx + math.log(float(np.sum(w * np.exp(log_q - mx))))

    mean_y = float(np.sum(w * y))
    beta0 = cfar_alpha(pfa, n) / mean_y
    lo, hi = beta0, beta0
    for _ in range(200):
        if log_pfa_of(lo) >= log_pfa:
            break
        lo *= 0.5
    else:
        raise MeasurementError("CFAR çarpanı için alt sınır bulunamadı")
    for _ in range(200):
        if log_pfa_of(hi) <= log_pfa:
            break
        hi *= 2.0
    else:
        raise MeasurementError("CFAR çarpanı için üst sınır bulunamadı")
    log_lo, log_hi = math.log(lo), math.log(hi)
    while log_hi - log_lo > 1e-11:
        mid = 0.5 * (log_lo + log_hi)
        if log_pfa_of(math.exp(mid)) > log_pfa:
            log_lo = mid
        else:
            log_hi = mid
    return math.exp(0.5 * (log_lo + log_hi))


def _check_cut(cut: CellModel | None) -> CellModel | None:
    if cut is None:
        return None
    if not isinstance(cut, CellModel):
        raise ConfigError(f"cut bir CellModel olmalı, {type(cut).__name__} geldi")
    if abs(cut.mean - 1.0) > 1e-6:
        raise ConfigError(f"cut modelinin ortalaması 1 olmalı (gürültü ortalamasına normalize), {cut.mean!r} geldi")
    return cut


@lru_cache(maxsize=512)
def _ca_cfar_alpha_cached(pfa: float, n: float, m_cells: float, cut: CellModel | None = None) -> float:
    y, w = _ca_estimate_grid(n, m_cells, pfa)
    return _solve_multiplier(pfa, n, y, w, cut)


def ca_cfar_alpha(pfa: float, n_avg: float, n_train: float, *, cut: CellModel | None = None) -> float:
    """CA-CFAR multiplier on the *mean of the training cells* with the finite-M correction.

    ``P(X > alpha * mean_train) = pfa`` for ``X`` and the ``n_train`` training cells iid
    ``Gamma(n_avg, 1/n_avg) sigma^2``. ``n_train`` may be non-integer (effective number of
    independent cells when neighbouring bins are correlated): the mean estimate is then
    modelled as ``Gamma(n M, 1/(n M))`` (exact for integer M, moment-matched otherwise).
    Tends to :func:`cfar_alpha` as ``n_train -> inf``. ``cut`` (mean 1) replaces the
    cell-under-test distribution (training cells keep the gamma model; module docstring).
    """
    pfa = _check_prob(pfa, "pfa")
    n = _check_shape(n_avg)
    m = float(n_train)
    if not (math.isfinite(m) and m > 0.0):
        raise ConfigError(f"Eğitim hücresi sayısı pozitif olmalı, {n_train!r} geldi")
    return _ca_cfar_alpha_cached(pfa, n, m, _check_cut(cut))


@lru_cache(maxsize=512)
def _os_cfar_beta_cached(pfa: float, n: float, m: int, k: int, cut: CellModel | None = None) -> float:
    y, w = _os_estimate_grid(n, m, k, pfa)
    return _solve_multiplier(pfa, n, y, w, cut)


def os_cfar_alpha(pfa: float, n_avg: float, n_train: int, k: int, *, cut: CellModel | None = None) -> float:
    """OS-CFAR multiplier on the *noise-mean estimate* ``X_(k) / c_k`` (finite-M exact).

    ``X_(k)`` is the rank-``k`` (1-based) order statistic of ``n_train`` iid
    ``Gamma(n_avg, 1/n_avg) sigma^2`` training cells and ``c_k`` = :func:`os_cfar_scale`.
    The returned ``alpha`` satisfies ``P(X > alpha * X_(k) / c_k) = pfa``; i.e. the raw
    multiplier on ``X_(k)`` is ``alpha / c_k``. ``cut`` (mean 1) replaces the
    cell-under-test distribution (training cells keep the gamma model; module docstring).
    """
    pfa = _check_prob(pfa, "pfa")
    n = _check_shape(n_avg)
    m = _check_cells(n_train)
    if not (1 <= int(k) <= m):
        raise ConfigError(f"OS-CFAR sırası 1..{m} aralığında olmalı, {k!r} geldi")
    beta = _os_cfar_beta_cached(pfa, n, m, int(k), _check_cut(cut))
    return beta * os_cfar_scale(n, m, int(k))


# --------------------------------------------------------------------------- Welch
@lru_cache(maxsize=256)
def _effective_averages_cached(n_seg: int, hop: int, window: str, nfft: int) -> float:
    if n_seg <= 1:
        return float(n_seg)
    w = get_window(window, nfft)
    s2 = float(np.sum(w * w))
    total = 0.0
    for k in range(1, n_seg):
        shift = k * hop
        if shift >= nfft:
            break
        rho = (float(np.dot(w[: nfft - shift], w[shift:])) / s2) ** 2
        total += (1.0 - k / n_seg) * rho
    return n_seg / (1.0 + 2.0 * total)


def effective_averages(n_segments: int, overlap: float, window: str, nfft: int) -> float:
    """Equivalent number of independent averages of a Welch estimate (Welch 1967).

    ``hop = round(nfft (1 - overlap))``; segment correlation for white noise
    ``rho(k) = (sum w[n] w[n + k hop])^2 / (sum w^2)^2`` and
    ``n_eff = K / (1 + 2 sum_{k=1}^{K-1} (1 - k/K) rho(k))``. Equals ``K`` without overlap.
    """
    if isinstance(n_segments, bool) or int(n_segments) != n_segments or int(n_segments) < 1:
        raise ConfigError(f"Segment sayısı pozitif tamsayı olmalı, {n_segments!r} geldi")
    ov = float(overlap)
    if not (0.0 <= ov < 1.0):
        raise ConfigError(f"Örtüşme [0, 1) aralığında olmalı, {overlap!r} geldi")
    if isinstance(nfft, bool) or int(nfft) != nfft or int(nfft) < 1:
        raise ConfigError(f"nfft pozitif tamsayı olmalı, {nfft!r} geldi")
    hop = max(1, int(round(int(nfft) * (1.0 - ov))))
    return _effective_averages_cached(int(n_segments), hop, canonical_window_name(window), int(nfft))
