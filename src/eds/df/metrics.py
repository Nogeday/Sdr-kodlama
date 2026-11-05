"""DF accuracy metrics (degrees, errors wrapped to (−180, 180]).

``rms = sqrt(mean(wrap(est − true)²))`` — the KTR ``df_rms`` figure (docs/MEASUREMENTS.md
section 6). :func:`summarize` adds the bias (mean error), the spread around it
(population std, so ``rms² = mean² + std²``) and the largest absolute error.
"""

from __future__ import annotations

import math
from typing import Any

import numpy as np

from ..core.errors import ConfigError
from ..core.units import wrap_deg


def angle_errors(est_deg: Any, true_deg: Any) -> np.ndarray:
    """Wrapped errors ``wrap_deg(est − true)``; ``true`` may be a scalar (broadcast)."""
    e = np.asarray(est_deg, dtype=np.float64)
    t = np.asarray(true_deg, dtype=np.float64)
    try:
        eb, tb = np.broadcast_arrays(e, t)
    except ValueError as err:
        raise ConfigError(f"Kestirim ve gerçek açı dizileri uyumsuz: {e.shape} / {t.shape}") from err
    if eb.size == 0:
        raise ConfigError("Açı hatası için en az bir kestirim gerekli")
    if not (np.all(np.isfinite(eb)) and np.all(np.isfinite(tb))):
        raise ConfigError("Açılar sonlu olmalı (NaN/sonsuz var)")
    return np.asarray(wrap_deg(eb - tb), dtype=np.float64).ravel()


def rms_angle_error(est_deg: Any, true_deg: Any) -> float:
    """RMS of the wrapped angle errors (degrees)."""
    err = angle_errors(est_deg, true_deg)
    return float(math.sqrt(float(np.mean(err * err))))


def summarize(est_deg: Any, true_deg: Any) -> dict[str, float | int]:
    """``{rms, mean, std, max_abs, n}`` of the wrapped errors (degrees)."""
    err = angle_errors(est_deg, true_deg)
    return {
        "rms": float(math.sqrt(float(np.mean(err * err)))),
        "mean": float(np.mean(err)),
        "std": float(np.std(err)),
        "max_abs": float(np.max(np.abs(err))),
        "n": int(err.size),
    }
