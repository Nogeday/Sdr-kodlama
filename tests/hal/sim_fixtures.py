"""Helpers for simulator tests (and for other suites that validate measurements on the sim).

Spectral helpers follow docs/ARCHITECTURE.md section 2 exactly:

* power-spectrum scaling ``P[k] = |sum w x e^{-j..}|^2 / (sum w)^2`` (a tone at a bin centre
  reads its power), fftshift-ed axis ``f[k] = (k - N/2) fs / N``,
* band power ``sum P[k] / ENBW_bins`` with ``ENBW_bins = N sum w^2 / (sum w)^2``,
* density ``P / RBW`` with ``RBW = ENBW_bins * fs / N``.

They are implemented here with numpy only so the simulator tests do not depend on
``eds.dsp`` (written concurrently by another agent).
"""

from __future__ import annotations

import math
from dataclasses import dataclass

import numpy as np

from eds.core.config import DeviceConfig, RxConfig
from eds.core.profile import load_profile
from eds.hal.sim import Emitter, SimRxDevice, SimWorld

PROFILE = load_profile("ad9363_pluto")


# --------------------------------------------------------------------------- construction
def make_world(*emitters: Emitter, seed: int = 1234, temperature_c: float = 35.0) -> SimWorld:
    return SimWorld(list(emitters), temperature_c=temperature_c, seed=seed)


def make_device(world: SimWorld, name: str = "rx0", *, fc: float = 1e9, fs: float = 2e6,
                bw: float | None = None, gain: float = 40.0, buffer_size: int = 4096,
                gain_mode: str = "manual", dc_tracking: bool = True, quad_tracking: bool = True,
                **sim) -> SimRxDevice:
    """Configured simulated Pluto; non-realtime unless ``realtime=True`` is passed."""
    sim.setdefault("realtime", False)
    dev = SimRxDevice(DeviceConfig(name=name, backend="sim", sim=sim), PROFILE, world)
    dev.configure(RxConfig(center_freq_hz=fc, sample_rate_hz=fs, bandwidth_hz=bw if bw is not None else fs,
                           gain_db=gain, gain_mode=gain_mode, buffer_size=buffer_size, block_size=buffer_size,
                           dc_tracking=dc_tracking, quad_tracking=quad_tracking))
    return dev


def apparent_bb(dev: SimRxDevice, f_true: float, fc: float | None = None) -> float:
    """Nominal-Hz baseband frequency at which ``f_true`` appears (ppm model)."""
    e = dev.residual_ppm * 1e-6
    return f_true / (1.0 + e) - (dev.fc if fc is None else fc)


# --------------------------------------------------------------------------- spectra
def blackman_harris(n: int) -> np.ndarray:
    """Periodic (DFT-even) 4-term Blackman-Harris window."""
    k = np.arange(n) * (2.0 * np.pi / n)
    return 0.35875 - 0.48829 * np.cos(k) + 0.14128 * np.cos(2 * k) - 0.01168 * np.cos(3 * k)


@dataclass
class Psd:
    f: np.ndarray            # Hz relative to fc (fftshift order)
    p: np.ndarray            # power-spectrum scaled, linear (dBFS reference)
    enbw_bins: float
    fs: float
    nfft: int

    @property
    def rbw(self) -> float:
        return self.enbw_bins * self.fs / self.nfft

    def band_power(self, f_lo: float, f_hi: float) -> float:
        m = (self.f >= f_lo) & (self.f <= f_hi)
        return float(self.p[m].sum() / self.enbw_bins)

    def band_power_db(self, f_lo: float, f_hi: float) -> float:
        return 10.0 * math.log10(max(self.band_power(f_lo, f_hi), 1e-30))

    def density_db(self, f_lo: float, f_hi: float) -> float:
        """Mean power density (dB/Hz) over bins in ``[f_lo, f_hi]``."""
        m = (self.f >= f_lo) & (self.f <= f_hi)
        return 10.0 * math.log10(float(self.p[m].mean()) / self.rbw)


def psd(x: np.ndarray, fs: float, nfft: int = 4096, overlap: float = 0.5) -> Psd:
    """Welch average (mean) with Blackman-Harris and ARCHITECTURE power-spectrum scaling."""
    x = np.asarray(x, dtype=np.complex128).reshape(-1)
    w = blackman_harris(nfft)
    step = max(1, int(nfft * (1.0 - overlap)))
    segs = np.lib.stride_tricks.sliding_window_view(x, nfft)[::step]
    spec = np.fft.fft(segs * w, axis=1)
    p = np.mean(np.abs(spec) ** 2, axis=0) / (w.sum() ** 2)
    p = np.fft.fftshift(p)
    f = (np.arange(nfft) - nfft // 2) * fs / nfft
    enbw = nfft * float(np.sum(w ** 2)) / float(w.sum()) ** 2
    return Psd(f=f, p=p, enbw_bins=enbw, fs=fs, nfft=nfft)


def tone_power_db(x: np.ndarray, fs: float, f: float, nfft: int = 8192, half_bw: float | None = None) -> float:
    """Band power around a tone at ``f`` (default +-6 bins)."""
    s = psd(x, fs, nfft)
    hb = half_bw if half_bw is not None else 6.0 * fs / nfft
    return s.band_power_db(f - hb, f + hb)


def estimate_tone_freq(x: np.ndarray, fs: float, block: int = 256) -> float:
    """High-precision frequency (Hz, nominal fs) of the dominant tone in ``x``.

    Coarse: Hann-windowed 4x zero-padded FFT + parabolic interpolation. Fine: demodulate by
    the coarse estimate, average blocks of ``block`` samples and fit the unwrapped phase
    slope (least squares) — precision far below 1 mHz at high SNR.
    """
    x = np.asarray(x, dtype=np.complex128).reshape(-1)
    n = x.size
    w = 0.5 - 0.5 * np.cos(2 * np.pi * np.arange(n) / n)
    nz = 4 * (1 << int(math.ceil(math.log2(n))))
    sp = np.abs(np.fft.fft(x * w, nz)) ** 2
    k = int(np.argmax(sp))
    a, b, c = np.log(sp[k - 1] + 1e-300), np.log(sp[k] + 1e-300), np.log(sp[(k + 1) % nz] + 1e-300)
    d = 0.5 * (a - c) / (a - 2 * b + c) if (a - 2 * b + c) != 0 else 0.0
    kk = k + d
    if kk > nz / 2:
        kk -= nz
    f0 = kk * fs / nz
    y = x * np.exp(-2j * np.pi * f0 * np.arange(n) / fs)
    nb = n // block
    z = y[: nb * block].reshape(nb, block).sum(axis=1)
    ph = np.unwrap(np.angle(z))
    t = (np.arange(nb) * block + (block - 1) / 2.0) / fs
    slope = np.polyfit(t, ph, 1)[0]
    return f0 + slope / (2 * np.pi)


def phase_slope(z: np.ndarray, fs: float) -> float:
    """Least-squares slope (rad/s) of the unwrapped phase of ``z``."""
    ph = np.unwrap(np.angle(np.asarray(z)))
    t = np.arange(ph.size) / fs
    return float(np.polyfit(t, ph, 1)[0])


def wrap_deg(a: float) -> float:
    return (a + 180.0) % 360.0 - 180.0


def expected_irr_db(eps_db: float, phi_deg: float) -> float:
    g = 10.0 ** (eps_db / 20.0)
    c = math.cos(math.radians(phi_deg))
    return 10.0 * math.log10((1 + 2 * g * c + g * g) / (1 - 2 * g * c + g * g))


# --------------------------------------------------------------------------- fake clock
class FakeClock:
    """Deterministic replacement for perf_counter/sleep/time_ns inside the simulator."""

    NS0 = 1_700_000_000_000_000_000

    def __init__(self, t0: float = 1000.0):
        self.t = float(t0)

    def perf(self) -> float:
        return self.t

    def sleep(self, d: float) -> None:
        if d > 0:
            self.t += d

    def time_ns(self) -> int:
        return self.NS0 + int(round(self.t * 1e9))

    def advance(self, d: float) -> None:
        self.t += d


def install_fake_clock(monkeypatch) -> FakeClock:
    """Patch the simulator's clock indirections (device + world) with a :class:`FakeClock`."""
    import eds.hal.sim.device as dmod
    import eds.hal.sim.world as wmod

    clk = FakeClock()
    monkeypatch.setattr(dmod, "_perf", clk.perf)
    monkeypatch.setattr(dmod, "_sleep", clk.sleep)
    monkeypatch.setattr(dmod, "_time_ns", clk.time_ns)
    monkeypatch.setattr(wmod, "_perf", clk.perf)
    monkeypatch.setattr(wmod, "_time_ns", clk.time_ns)
    return clk
