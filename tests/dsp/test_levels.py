"""eds.dsp.levels: dBFS peak / RMS / clipping / DC of IQ samples."""

from __future__ import annotations

import json
import math

import numpy as np
import pytest

from eds.core.errors import ConfigError
from eds.dsp import iq_levels
from tests.dsp.helpers import tone


def test_full_scale_complex_exponential_is_0_dbfs() -> None:
    x = tone(65536, 12345.0, 1e6, 0.0).astype(np.complex64)
    lv = iq_levels(x)
    assert lv.rms_dbfs == pytest.approx(0.0, abs=1e-5)
    assert lv.peak_dbfs == pytest.approx(0.0, abs=0.01)
    assert lv.clip_fraction > 0.0             # rails hit full scale near the axes
    assert lv.n == 65536


def test_half_scale_tone_levels_no_clipping() -> None:
    x = tone(10000, 1000.0, 1e6, -6.0)
    lv = iq_levels(x)
    assert lv.rms_dbfs == pytest.approx(-6.0, abs=1e-6)
    assert lv.peak_dbfs == pytest.approx(-6.0, abs=0.01)
    assert lv.clip_fraction == 0.0
    assert lv.headroom_db == pytest.approx(6.0, abs=0.01)
    assert lv.crest_db == pytest.approx(0.0, abs=0.01)


def test_clip_fraction_counts_complex_samples() -> None:
    x = np.full(1000, 0.1 + 0.1j, dtype=np.complex64)
    x[:20] = 1.0 + 0.0j                   # I clipped
    x[20:37] = 0.2 - 1.0j                 # Q clipped
    x[37:40] = -1.0 - 1.0j                # both rails clipped: still one sample
    x[40] = 2046.0 / 2048.0               # just below the clip level
    lv = iq_levels(x)
    assert lv.clip_fraction == pytest.approx(40 / 1000)
    assert lv.peak_dbfs == pytest.approx(0.0, abs=1e-6)
    assert iq_levels(x, clip_level=0.05).clip_fraction == 1.0


def test_dc_level() -> None:
    n = 4096
    x = tone(n, 64 * 1e6 / n, 1e6, -10.0) + 0.01 * np.exp(1j * 0.3)    # integer periods -> mean = DC
    lv = iq_levels(x)
    assert lv.dc_dbfs == pytest.approx(-40.0, abs=1e-6)
    assert lv.rms_dbfs == pytest.approx(10 * math.log10(0.1 + 1e-4), abs=1e-6)


def test_multichannel_aggregate() -> None:
    n = 2048
    quiet = tone(n, 16 * 1e6 / n, 1e6, -40.0)
    loud = tone(n, 32 * 1e6 / n, 1e6, -3.0) + 0.001
    lv = iq_levels(np.vstack([quiet, loud]).astype(np.complex64))
    assert lv.n == 2 * n
    assert lv.peak_dbfs == pytest.approx(-3.0, abs=0.02)
    assert lv.rms_dbfs == pytest.approx(10 * math.log10((1e-4 + 10 ** -0.3 + 1e-6) / 2), abs=1e-4)
    assert lv.dc_dbfs == pytest.approx(-60.0, abs=1e-3)           # worst channel
    d = lv.to_dict()
    assert json.loads(json.dumps(d)) == d and isinstance(d["n"], int)


def test_real_input_and_validation() -> None:
    lv = iq_levels(np.array([0.5, -0.5, 0.5, -0.5]))
    assert lv.rms_dbfs == pytest.approx(20 * math.log10(0.5))
    assert lv.dc_dbfs < -250.0
    with pytest.raises(ConfigError):
        iq_levels(np.zeros((0,), dtype=np.complex64))
    with pytest.raises(ConfigError):
        iq_levels(np.zeros((2, 2, 2), dtype=np.complex64))
