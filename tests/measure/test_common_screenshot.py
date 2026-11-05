"""Spectrum "screenshot" figures (eds.measure.common.spectrum_screenshot): exact frequency axis,
Center precision, never a "-0" reference level."""

from __future__ import annotations

import numpy as np
import pytest

from eds.dsp.psd import Spectrum
from eds.measure.common import spectrum_screenshot


def _spec(fc: float = 935.399353e6, fs: float = 4e6, nfft: int = 1024, level_db: float = -100.0) -> Spectrum:
    f = fc + (np.arange(nfft) - nfft // 2) * fs / nfft
    p = np.full(nfft, 10.0 ** (level_db / 10.0))
    return Spectrum(f_hz=f, p_lin=p, fc=fc, fs=fs, nfft=nfft, window="blackmanharris", enbw_bins=2.004, averages=8)


def test_full_span_screenshot_axis_is_center_pm_half_span():
    spec = _spec()
    fig = spectrum_screenshot(spec, "s", "t")
    # the fftshift grid ends one bin short of +fs/2; the screen still shows exactly fc ± fs/2
    assert fig.xlim == pytest.approx(((935.399353e6 - 2e6) / 1e6, (935.399353e6 + 2e6) / 1e6))
    assert fig.info["Center"] == "935.399353 MHz"                     # 9 significant digits, not 935.399
    assert fig.info["Span"] == "4 MHz"


def test_slice_and_stitched_spectra_leave_the_axis_to_center_span():
    spec = _spec()
    sl = spec.slice(935.3e6, 935.5e6)
    fig = spectrum_screenshot(sl, "z", "t", extra_info={"Center": "935.4 MHz", "Span": "200 kHz"})
    assert fig.xlim is None and fig.info["Center"] == "935.4 MHz"
    stitched = _spec()
    stitched.meta["stitched"] = True
    assert spectrum_screenshot(stitched, "w", "t").xlim is None
    assert spectrum_screenshot(stitched, "w", "t", xlim_hz=(325e6, 825e6)).xlim == (325.0, 825.0)


@pytest.mark.parametrize("level_db", [-5.0, -10.0, -14.9])
def test_reference_level_is_never_negative_zero(level_db):
    fig = spectrum_screenshot(_spec(level_db=level_db), "r", "t")
    assert fig.info["Ref"] == "0 dBFS" and fig.ylim == (-100.0, 0.0)
    assert np.copysign(1.0, fig.ylim[1]) > 0
