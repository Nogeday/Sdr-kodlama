"""Spectral-analysis windows (periodic / DFT-even) and their figures of merit.

All windows are *generalized cosine* windows evaluated in their **periodic** (DFT-even)
form, i.e. ``w[n] = sum_m (-1)^m a_m cos(2 pi m n / N)`` for ``n = 0..N-1``. This is the
correct form for spectral analysis: the window's DFT is exactly ``a_0 N`` at bin 0 and
``+-a_m N / 2`` at bins ``+-m`` (sign alternating), so coherent gain and ENBW are exact
closed-form constants independent of ``N`` (for ``N`` larger than twice the number of
terms).

Coefficients (exact, as specified in docs/ARCHITECTURE.md):

=================  ===================================================================
``rect``           1
``hann``           0.5, 0.5
``hamming``        0.54, 0.46
``blackman``       0.42, 0.5, 0.08 ("exact" 3-term Blackman, -58 dB)
``blackmanharris`` 0.35875, 0.48829, 0.14128, 0.01168 (4-term, -92 dB)
``flattop``        0.21557895, 0.41663158, 0.277263158, 0.083578947, 0.006947368
=================  ===================================================================

:func:`get_window` returns cached, **read-only** ``float64`` arrays; callers that need to
modify a window must copy it. The window cache is an LRU bounded in *bytes*
(:data:`WINDOW_CACHE_MAX_BYTES`) because record-length windows (e.g. for
:func:`eds.dsp.peaks.tone_frequency`) can be large; it is thread-safe.
"""

from __future__ import annotations

import threading
from collections import OrderedDict
from dataclasses import dataclass
from functools import lru_cache

import numpy as np

from ..core.errors import ConfigError

WINDOW_COEFFS: dict[str, tuple[float, ...]] = {
    "rect": (1.0,),
    "hann": (0.5, 0.5),
    "hamming": (0.54, 0.46),
    "blackman": (0.42, 0.5, 0.08),
    "blackmanharris": (0.35875, 0.48829, 0.14128, 0.01168),
    "flattop": (0.21557895, 0.41663158, 0.277263158, 0.083578947, 0.006947368),
}

#: Canonical window names accepted everywhere (config enums use the same names).
WINDOW_NAMES: tuple[str, ...] = tuple(WINDOW_COEFFS)

#: Nominal highest-sidelobe level relative to the main-lobe peak (dB, negative), computed
#: numerically for the periodic windows above (asymptotic, large N).
SIDELOBE_DB: dict[str, float] = {
    "rect": -13.26,
    "hann": -31.47,
    "hamming": -42.66,
    "blackman": -58.11,
    "blackmanharris": -92.01,
    "flattop": -93.0,
}

_ALIASES: dict[str, str] = {
    "rectangular": "rect", "boxcar": "rect", "none": "rect", "uniform": "rect",
    "hanning": "hann",
    "blackman-harris": "blackmanharris", "blackman_harris": "blackmanharris",
    "bh4": "blackmanharris", "bh92": "blackmanharris",
    "flat_top": "flattop", "flat-top": "flattop",
}


def canonical_window_name(name: str) -> str:
    """Map a window name (case-insensitive, common aliases) to its canonical name."""
    if not isinstance(name, str):
        raise ConfigError(f"Pencere adı metin olmalı, {name!r} geldi")
    key = name.strip().lower()
    key = _ALIASES.get(key, key)
    if key not in WINDOW_COEFFS:
        raise ConfigError(f"Bilinmeyen pencere: {name!r}; seçenekler: {list(WINDOW_NAMES)}")
    return key


def _check_length(n: int) -> int:
    if isinstance(n, bool) or int(n) != n or int(n) < 1:
        raise ConfigError(f"Pencere uzunluğu pozitif tamsayı olmalı, {n!r} geldi")
    return int(n)


#: Upper bound of the memory held by the window cache (least recently used evicted first).
WINDOW_CACHE_MAX_BYTES: int = 64 * 2 ** 20

_cache: OrderedDict[tuple[str, int], np.ndarray] = OrderedDict()
_cache_bytes = 0
_cache_lock = threading.Lock()


def _compute_window(name: str, n: int) -> np.ndarray:
    coeffs = WINDOW_COEFFS[name]
    phase = np.arange(n, dtype=np.float64) * (2.0 * np.pi / n)
    w = np.full(n, coeffs[0], dtype=np.float64)
    for m, a in enumerate(coeffs[1:], start=1):
        w += ((-1.0) ** m) * a * np.cos(m * phase)
    w.flags.writeable = False
    return w


def _window_cached(name: str, n: int) -> np.ndarray:
    global _cache_bytes
    key = (name, n)
    with _cache_lock:
        w = _cache.get(key)
        if w is not None:
            _cache.move_to_end(key)
            return w
    w = _compute_window(name, n)
    with _cache_lock:
        if key not in _cache and w.nbytes <= WINDOW_CACHE_MAX_BYTES // 4:
            _cache[key] = w
            _cache_bytes += w.nbytes
            while _cache_bytes > WINDOW_CACHE_MAX_BYTES:
                _, old = _cache.popitem(last=False)
                _cache_bytes -= old.nbytes
    return w


def get_window(name: str, n: int) -> np.ndarray:
    """Periodic (DFT-even) window of length ``n`` as a cached, read-only float64 array."""
    return _window_cached(canonical_window_name(name), _check_length(n))


@dataclass(frozen=True)
class WindowInfo:
    """Figures of merit of a window of length ``N``.

    coherent_gain    ``sum(w) / N`` (amplitude gain for a bin-centred tone)
    enbw_bins        equivalent noise bandwidth in bins, ``N sum(w^2) / sum(w)^2``
    scallop_loss_db  amplitude loss (positive dB) for a tone exactly half a bin off centre,
                     computed numerically from the window's DTFT
    sidelobe_db      nominal highest sidelobe relative to the main lobe (negative dB)
    """

    coherent_gain: float
    enbw_bins: float
    scallop_loss_db: float
    sidelobe_db: float


@lru_cache(maxsize=128)
def _window_info_cached(name: str, n: int) -> WindowInfo:
    w = _window_cached(name, n)
    s1 = float(np.sum(w))
    s2 = float(np.sum(w * w))
    half_bin = np.exp(-1j * np.pi * np.arange(n) / n)       # DTFT evaluated 0.5 bin off centre
    ratio = abs(complex(np.dot(w, half_bin))) / s1
    return WindowInfo(
        coherent_gain=s1 / n,
        enbw_bins=n * s2 / (s1 * s1),
        scallop_loss_db=float(-20.0 * np.log10(ratio)),
        sidelobe_db=SIDELOBE_DB[name],
    )


def window_info(name: str, n: int) -> WindowInfo:
    """Coherent gain, ENBW (bins), half-bin scalloping loss and nominal sidelobe level."""
    return _window_info_cached(canonical_window_name(name), _check_length(n))


def bin_correlation(name: str, n: int, max_lag: int) -> np.ndarray:
    """Correlation coefficient of ``|X[k]|^2`` and ``|X[k+d]|^2`` for white Gaussian noise.

    For a complex Gaussian white input the DFT bins of the windowed segment are jointly
    Gaussian with ``corr(X[k], X[k+d]) = W2[d] / W2[0]`` where ``W2`` is the DFT of ``w^2``;
    the power correlation is the squared magnitude of that. Returns ``rho[d]`` for
    ``d = 0..max_lag`` (``rho[0] == 1``; lags ``>= n`` are reported as 0). Used by CFAR to
    compute the *effective* number of independent training cells. For the cosine windows
    here ``rho[d] = 0`` for ``2*(terms-1) < d < n - 2*(terms-1)``.
    """
    n = _check_length(n)
    if isinstance(max_lag, bool) or int(max_lag) != max_lag or int(max_lag) < 0:
        raise ConfigError(f"max_lag negatif olmayan tamsayı olmalı, {max_lag!r} geldi")
    w2 = _window_cached(canonical_window_name(name), n) ** 2
    spec = np.fft.fft(w2)
    rho = (spec.real ** 2 + spec.imag ** 2) / float(np.sum(w2)) ** 2
    out = np.zeros(int(max_lag) + 1, dtype=np.float64)
    m = min(out.size, n)
    out[:m] = rho[:m]
    return out
