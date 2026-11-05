"""eds.dsp.psd: power-spectrum scaling, axis, averaging, Spectrum methods."""

from __future__ import annotations

import math

import numpy as np
import pytest

from eds.core.errors import ConfigError, MeasurementError
from eds.core.types import IQBlock
from eds.dsp import WINDOW_NAMES, Spectrum, nfft_for_rbw, spectrum_from_block, welch, window_info
from tests.dsp.helpers import cnoise, tone

FS = 1.0e6


@pytest.mark.parametrize("name", WINDOW_NAMES)
def test_bin_centred_tone_reads_its_power(name: str) -> None:
    nfft, k = 1024, 100
    x = tone(8 * nfft, k * FS / nfft, FS, -20.0, phase=0.7)
    s = welch(x.astype(np.complex64), FS, nfft, window=name)
    assert s.p_db[nfft // 2 + k] == pytest.approx(-20.0, abs=0.01)
    assert int(np.argmax(s.p_lin)) == nfft // 2 + k


@pytest.mark.parametrize("name", WINDOW_NAMES)
@pytest.mark.parametrize("offset_bins", [0.5, 0.37, -0.21])
def test_off_bin_tone_band_power(name: str, offset_bins: float) -> None:
    nfft, k = 1024, -150
    f_bb = (k + offset_bins) * FS / nfft
    s = welch(tone(6 * nfft, f_bb, FS, -20.0, phase=1.3), FS, nfft, window=name, fc=2.4e9)
    f = 2.4e9 + f_bb
    assert s.band_power_db(f - 64 * s.df_hz, f + 64 * s.df_hz) == pytest.approx(-20.0, abs=0.05)


@pytest.mark.parametrize("name", WINDOW_NAMES)
def test_white_noise_reads_power_in_rbw(name: str) -> None:
    rng = np.random.default_rng(11)
    nfft, s2 = 1024, 1e-3
    x = cnoise(rng, nfft * 64, s2).astype(np.complex64)
    s = welch(x, FS, nfft, window=name, overlap=0.0)
    enbw = window_info(name, nfft).enbw_bins
    assert np.mean(s.p_lin) == pytest.approx(s2 * enbw / nfft, rel=0.02)
    # the whole span integrates to the noise power, the density to s2 / fs
    assert s.band_power(s.f_hz[0], s.f_hz[-1]) == pytest.approx(s2, rel=0.02)
    assert 10 * np.log10(np.mean(10 ** (s.density_db_hz / 10))) == pytest.approx(10 * np.log10(s2 / FS), abs=0.1)


def test_rbw_df_and_density_formula() -> None:
    s = welch(np.ones(4096, dtype=np.complex64), FS, 2048, window="hann")
    assert s.df_hz == pytest.approx(FS / 2048)
    assert s.enbw_bins == pytest.approx(1.5)
    assert s.rbw_hz == pytest.approx(1.5 * FS / 2048)
    np.testing.assert_allclose(s.density_db_hz, s.p_db - 10 * math.log10(s.rbw_hz))


@pytest.mark.parametrize("nfft", [256, 255])
def test_frequency_axis_and_dc_index(nfft: int) -> None:
    fc = 915e6
    k = 37
    x = tone(4 * nfft, k * FS / nfft, FS, 0.0) + tone(4 * nfft, -2 * k * FS / nfft, FS, -10.0)
    s = welch(x, FS, nfft, window="rect", fc=fc)
    dc = nfft // 2
    assert s.f_hz[dc] == fc
    np.testing.assert_allclose(s.f_hz, fc + (np.arange(nfft) - dc) * FS / nfft, rtol=0, atol=1e-6)
    assert s.p_db[dc + k] == pytest.approx(0.0, abs=1e-9)
    assert s.p_db[dc - 2 * k] == pytest.approx(-10.0, abs=1e-9)
    np.testing.assert_array_equal(s.dc_offset_bins, np.arange(nfft) - dc)
    np.testing.assert_allclose(s.f_rel_hz, (np.arange(nfft) - dc) * FS / nfft, atol=1e-6)


def test_segment_count_hop_and_meta() -> None:
    nfft = 512
    x = np.zeros(nfft + 256 * 9, dtype=np.complex64)
    s = welch(x, FS, nfft, overlap=0.5)
    assert s.averages == 10 and s.meta["hop"] == 256 and s.meta["overlap"] == 0.5
    assert s.meta["zero_padded"] is False and s.meta["segments"] == 10
    s0 = welch(np.zeros(nfft * 3 + 100, dtype=np.complex64), FS, nfft, overlap=0.0)
    assert s0.averages == 3 and s0.meta["hop"] == nfft
    s1 = welch(np.zeros(nfft + 10), FS, nfft, overlap=0.999)
    assert s1.meta["hop"] == 1 and s1.averages == 11


def test_zero_padding_short_input() -> None:
    nfft = 1024
    x = tone(256, 32 * FS / 256, FS, -6.0)           # bin-centred for the 256-sample record
    s = welch(x, FS, nfft, window="rect")
    assert s.averages == 1 and s.meta["zero_padded"] is True and s.nfft == nfft
    # rect window: only 256 of 1024 samples carry the tone -> amplitude * 1/4 -> -12 dB
    assert s.p_db[nfft // 2 + 128] == pytest.approx(-6.0 - 20 * math.log10(4.0), abs=1e-9)


def test_average_modes_exact() -> None:
    nfft, k = 64, 5
    amps = [0.1, 0.4, 0.2]
    x = np.concatenate([a * np.exp(2j * np.pi * k * np.arange(nfft) / nfft) for a in amps])
    powers = np.array(amps) ** 2
    for avg, expect in (("mean", powers.mean()), ("max", powers.max()), ("min", powers.min()),
                        ("median", np.median(powers))):
        s = welch(x, FS, nfft, window="rect", overlap=0.0, avg=avg)
        assert s.averages == 3 and s.meta["avg"] == avg
        assert s.p_lin[nfft // 2 + k] == pytest.approx(expect, rel=1e-12)


@pytest.mark.parametrize("avg", ["mean", "max", "min", "median"])
def test_vectorized_matches_naive_loop(avg: str) -> None:
    rng = np.random.default_rng(5)
    nfft, overlap = 128, 0.25
    x = cnoise(rng, 3000, 1.0)
    s = welch(x, FS, nfft, window="hamming", overlap=overlap, avg=avg)
    hop = round(nfft * (1 - overlap))
    w = np.hamming(nfft + 1)[:-1]
    segs = [x[i:i + nfft] for i in range(0, len(x) - nfft + 1, hop)]
    ps = np.array([np.abs(np.fft.fft(w * seg)) ** 2 for seg in segs]) / np.sum(w) ** 2
    ref = {"mean": ps.mean(0), "max": ps.max(0), "min": ps.min(0), "median": np.median(ps, 0)}[avg]
    assert s.averages == len(segs)
    np.testing.assert_allclose(s.p_lin, np.fft.fftshift(ref), rtol=1e-10, atol=0)


def test_large_input_is_chunked_consistently() -> None:
    rng = np.random.default_rng(9)
    x = cnoise(rng, 2 ** 21 + 77, 1.0).astype(np.complex64)
    s = welch(x, FS, 256, overlap=0.0)
    seg = x[: (len(x) // 256) * 256].reshape(-1, 256).astype(np.complex128)
    w = np.asarray(s.p_lin)
    ref = np.fft.fftshift(np.mean(np.abs(np.fft.fft(seg * _bh(256), axis=1)) ** 2, axis=0)) / np.sum(_bh(256)) ** 2
    np.testing.assert_allclose(w, ref, rtol=1e-9)


def _bh(n: int) -> np.ndarray:
    k = 2 * np.pi * np.arange(n) / n
    return 0.35875 - 0.48829 * np.cos(k) + 0.14128 * np.cos(2 * k) - 0.01168 * np.cos(3 * k)


def test_noise_floor_median_ignores_tone_and_dc() -> None:
    rng = np.random.default_rng(21)
    nfft, s2 = 2048, 1e-4
    n = nfft * 64
    x = cnoise(rng, n, s2) + tone(n, 200e3, FS, -10.0) + 0.05      # strong tone + DC
    s = welch(x, FS, nfft, overlap=0.0)
    expected = 10 * math.log10(s2 * s.enbw_bins / nfft)
    assert s.noise_floor_db == pytest.approx(expected, abs=0.2)
    assert s.usable_bins()[nfft // 2] == np.False_
    assert s.usable_bins()[nfft // 2 + 4]
    assert not s.usable_bins()[nfft // 2 + int(0.4 * nfft) + 1]


def test_band_power_slice_offset_copy() -> None:
    nfft = 1024
    x = tone(nfft * 4, 50 * FS / nfft, FS, -30.0) + cnoise(np.random.default_rng(2), nfft * 4, 1e-12)
    s = welch(x, FS, nfft, fc=100e6)
    f0 = 100e6 + 50 * FS / nfft
    sub = s.slice(f0 - 10 * s.df_hz, f0 + 10 * s.df_hz)
    assert sub.n_bins == 21 and sub.nfft == nfft and sub.fc == s.fc
    assert sub.band_power_db(f0 - 10 * s.df_hz, f0 + 10 * s.df_hz) == pytest.approx(-30.0, abs=0.01)
    np.testing.assert_array_equal(sub.dc_offset_bins, np.arange(40, 61))
    dbm = s.with_offset_db(-70.0, "dBm")
    assert dbm.unit == "dBm" and s.unit == "dBFS"
    np.testing.assert_allclose(dbm.p_db, s.p_db - 70.0, atol=1e-9)
    per_bin = np.linspace(-1, 1, nfft)
    np.testing.assert_allclose(s.with_offset_db(per_bin).p_db, s.p_db + per_bin, atol=1e-9)
    with pytest.raises(ConfigError):
        s.with_offset_db(np.zeros(3))
    c = s.copy()
    c.p_lin[:] = 0.0
    c.meta["x"] = 1
    assert np.max(s.p_lin) > 0 and "x" not in s.meta
    with pytest.raises(MeasurementError):
        s.band_power(1.0, 2.0)                       # no bin centre in band
    with pytest.raises(ConfigError):
        s.band_power(f0, f0 - 1.0)


def test_freq_correction_ppm_convention() -> None:
    # TCXO +20 ppm fast: a true 1000.1 MHz carrier appears at f_true / (1 + e)
    ppm, nfft = 20.0, 4096
    e = ppm * 1e-6
    fc_nom = 1000e6
    f_true = 1000.1e6
    f_app_bb = f_true / (1 + e) - fc_nom
    k = round(f_app_bb / (FS / nfft))
    x = tone(4 * nfft, k * FS / nfft, FS, -10.0)
    s = welch(x, FS, nfft, fc=fc_nom)
    c = s.with_freq_correction(ppm)
    assert c.fc == pytest.approx(fc_nom * (1 + e), rel=1e-15)
    assert c.fs == pytest.approx(FS * (1 + e), rel=1e-15)
    np.testing.assert_allclose(c.f_hz, s.f_hz * (1 + e), rtol=1e-15)
    np.testing.assert_allclose(c.f_hz, c.fc + (np.arange(nfft) - nfft // 2) * c.fs / nfft, rtol=1e-15)
    np.testing.assert_array_equal(c.dc_offset_bins, s.dc_offset_bins)
    f_peak = c.f_hz[np.argmax(c.p_lin)]
    assert abs(f_peak - f_true) < c.df_hz            # corrected axis reads the true frequency
    assert c.meta["ppm_applied"] == pytest.approx(ppm)
    assert c.with_freq_correction(-ppm).meta["ppm_applied"] == pytest.approx(-(ppm * ppm) * 1e-6, abs=1e-9)


def test_spectrum_from_block_carries_metadata() -> None:
    nfft = 256
    samples = np.vstack([tone(1024, 0.0, FS, -40.0), tone(1024, 10 * FS / nfft, FS, -3.0)]).astype(np.complex64)
    blk = IQBlock(samples=samples, fs=FS, fc=2.45e9, t_ns=123456789, index=4096, seq=7, gain_db=(30.0, 41.0),
                  overflow=True, meta={"ppm_hw": 0.0})
    s = spectrum_from_block(blk, nfft, "blackmanharris", 0.5, "mean", channel=1)
    assert s.fc == 2.45e9 and s.fs == FS and s.t_ns == 123456789 and s.overflow is True
    assert s.gain_db == (41.0,)
    assert s.meta["channel"] == 1 and s.meta["seq"] == 7 and s.meta["block_meta"] == {"ppm_hw": 0.0}
    assert s.p_db[nfft // 2 + 10] == pytest.approx(-3.0, abs=0.01)
    blk1 = IQBlock(samples=samples, fs=FS, fc=2.45e9, t_ns=0, index=0, seq=0, gain_db=(25.0,))
    assert spectrum_from_block(blk1, nfft, channel=1).gain_db == (25.0,)
    with pytest.raises(ConfigError):
        spectrum_from_block(blk, nfft, channel=2)


@pytest.mark.parametrize("name", ["rect", "hann", "blackmanharris", "flattop"])
def test_nfft_for_rbw(name: str) -> None:
    fs = 20e6
    for rbw in (1e3, 12.5e3, 100e3):
        n = nfft_for_rbw(rbw, fs, name)
        enbw = window_info(name, n).enbw_bins
        assert n & (n - 1) == 0
        assert enbw * fs / n <= rbw
        if n > 64:
            assert window_info(name, n // 2).enbw_bins * fs / (n // 2) > rbw
    assert nfft_for_rbw(1e9, fs, name) == 64
    assert nfft_for_rbw(0.001, fs, name) == 2 ** 22
    with pytest.raises(ConfigError):
        nfft_for_rbw(0.0, fs, name)


def test_input_validation() -> None:
    with pytest.raises(ConfigError):
        welch(np.zeros((2, 100), dtype=np.complex64), FS, 64)
    with pytest.raises(MeasurementError):
        welch(np.array([1.0, np.nan] * 64), FS, 64)
    with pytest.raises(ConfigError):
        welch(np.zeros(128), FS, 64, overlap=1.0)
    with pytest.raises(ConfigError):
        welch(np.zeros(128), FS, 64, avg="rms")
    with pytest.raises(ConfigError):
        welch(np.zeros(128), -1.0, 64)
    with pytest.raises(ConfigError):
        Spectrum(f_hz=np.zeros(3), p_lin=np.zeros(4), fc=0, fs=1, nfft=4, window="rect", enbw_bins=1, averages=1)
    # (1, n) input is accepted as a single channel
    s = welch(np.ones((1, 128), dtype=np.complex64), FS, 64, window="rect")
    assert s.p_db[32] == pytest.approx(0.0, abs=1e-9)
