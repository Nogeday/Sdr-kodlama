"""eds.df.metrics: wrapped angle errors, RMS and summary statistics."""

from __future__ import annotations

import math

import numpy as np
import pytest

from eds.core.errors import ConfigError
from eds.df.metrics import angle_errors, rms_angle_error, summarize


def test_errors_wrap_across_plus_minus_180():
    assert angle_errors([179.0, -179.0, 10.0], [-179.0, 179.0, 370.0]).tolist() == pytest.approx([-2.0, 2.0, 0.0])
    assert rms_angle_error([179.0], [-179.0]) == pytest.approx(2.0)
    assert angle_errors([0.0], [180.0]).tolist() == [180.0]                 # wrap_deg range (-180, 180]


def test_rms_and_summary_numbers():
    est = np.array([1.0, -3.0, 2.0, 4.0])
    true = 0.0                                                              # scalar truth broadcasts
    s = summarize(est, true)
    assert s["n"] == 4
    assert s["rms"] == pytest.approx(math.sqrt((1 + 9 + 4 + 16) / 4))
    assert s["mean"] == pytest.approx(1.0)
    assert s["std"] == pytest.approx(float(np.std(est)))
    assert s["rms"] ** 2 == pytest.approx(s["mean"] ** 2 + s["std"] ** 2)
    assert s["max_abs"] == 4.0
    assert rms_angle_error(est, true) == pytest.approx(s["rms"])


def test_statistics_of_known_gaussian_errors():
    rng = np.random.default_rng(0)
    true = np.repeat(np.arange(-60.0, 61.0, 10.0), 200)
    est = true + 0.3 + 1.5 * rng.standard_normal(true.size)
    s = summarize(est, true)
    assert s["mean"] == pytest.approx(0.3, abs=0.05)
    assert s["std"] == pytest.approx(1.5, abs=0.05)
    assert s["rms"] == pytest.approx(math.hypot(0.3, 1.5), abs=0.05)


def test_invalid_inputs():
    with pytest.raises(ConfigError):
        rms_angle_error([], [])
    with pytest.raises(ConfigError):
        rms_angle_error([1.0, 2.0], [1.0, 2.0, 3.0])
    with pytest.raises(ConfigError):
        summarize([float("nan")], [0.0])
