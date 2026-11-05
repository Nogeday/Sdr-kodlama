"""Vectorized waveform kernels used by the simulator (numpy only, no scipy).

Everything here is a pure function of its arguments so the physics in
:mod:`eds.hal.sim.device` stays readable and every kernel can be unit-tested on its own.

Conventions (docs/ARCHITECTURE.md section 2): complex baseband, full scale = 1.0,
0 dBFS == ``mean(|x|^2) == 1``; frequencies in Hz, times in s.
"""

from __future__ import annotations

import math
import zlib
from fractions import Fraction
from functools import lru_cache

import numpy as np

# --------------------------------------------------------------------------- constants
#: Noise synthesis frame length (samples). Thermal noise and noise-like emitter envelopes
#: are generated in the frequency domain one frame at a time, frames aligned to absolute
#: sample indices, so the waveform does not depend on how a consumer slices its reads.
FRAME: int = 16384

#: Block length of the outer-product tone synthesizer (see :func:`tone_bank`).
TONE_BLOCK: int = 1024

#: GSM TDMA frame duration (120 ms / 26 = 4.615 ms).
GSM_FRAME_S: float = 120e-3 / 26.0
#: GSM bit period (48/13 us).
GSM_BIT_S: float = 48e-6 / 13.0
#: FCCH burst duration: 148 bits = 546.46 us.
GSM_FCCH_BURST_S: float = 148.0 * GSM_BIT_S
#: FCCH tone offset above the BCCH carrier: 1625/24 kHz = 67.708 kHz.
GSM_FCCH_OFFSET_HZ: float = 1625e3 / 24.0
#: Occupied bandwidth of the GMSK-like BCCH noise model.
GSM_DEFAULT_BW_HZ: float = 200e3

#: Butterworth order of the AD9363 analog baseband low-pass model.
LPF_ORDER: int = 3

_TWO_PI = 2.0 * math.pi


# --------------------------------------------------------------------------- helpers
def stable_hash(text: str) -> int:
    """Process-independent 32-bit hash (``hash()`` is salted per interpreter run)."""
    return zlib.crc32(text.encode("utf-8")) & 0xFFFFFFFF


def unit_from_hash(text: str) -> float:
    """Deterministic number in [0, 1) derived from ``text``."""
    return stable_hash(text) / 4294967296.0


def frame_rng(*key: int) -> np.random.Generator:
    """Counter-style RNG: an independent, reproducible stream for every integer key tuple."""
    return np.random.Generator(np.random.SFC64(np.random.SeedSequence([int(k) & ((1 << 63) - 1) for k in key])))


def exact_frac_cycles(freq_hz: float, t_start_s: float, dn: int, fs_act: float) -> float:
    """``frac(freq_hz * (t_start_s + dn / fs_act))`` computed exactly from the float inputs.

    Phase continuity over hours of samples needs more than float64 for the product of a
    large time and a frequency; :class:`fractions.Fraction` is exact and costs ~10 us.
    """
    if freq_hz == 0.0:
        return 0.0
    v = Fraction(freq_hz) * (Fraction(t_start_s) + Fraction(int(dn)) / Fraction(fs_act))
    return float(v - math.floor(v))


def exact_frac_product(a: float, n: int) -> float:
    """``frac(a * n)`` for float ``a`` and integer ``n`` (exact)."""
    if a == 0.0 or n == 0:
        return 0.0
    v = Fraction(a) * int(n)
    return float(v - math.floor(v))


# --------------------------------------------------------------------------- filters
def butterworth_mag(f_hz, corner_hz: float, order: int = LPF_ORDER):
    """|H(f)| of an ``order``-th order Butterworth low-pass with -3 dB corner ``corner_hz``.

    Magnitude only (zero phase): a real, even response that commutes with the IQ
    imbalance model, matching identical I and Q analog filters. A scalar ``f_hz`` returns a
    Python ``float`` (fast path, no numpy overhead), an array returns ``float64`` array.
    """
    if np.ndim(f_hz) == 0:
        r = abs(float(f_hz)) / float(corner_hz)
        return 1.0 / math.sqrt(1.0 + r ** (2 * order))
    r = np.asarray(f_hz, dtype=np.float64) / float(corner_hz)
    return 1.0 / np.sqrt(1.0 + r ** (2 * order))


#: Defaults of the AD9363 RX digital decimation chain model (fractions of the output rate).
DIG_PASS_FRAC: float = 0.40
DIG_STOP_FRAC: float = 0.60
DIG_STOP_DB: float = -80.0


def digital_filter_db(f_hz, fs: float, pass_frac: float = DIG_PASS_FRAC, stop_frac: float = DIG_STOP_FRAC,
                      stop_db: float = DIG_STOP_DB):
    """Gain (dB) of the RX digital decimation chain at PRE-ALIAS baseband frequency ``f_hz``.

    Model of the AD9363 half-band decimators + programmable FIR: flat (0 dB) for
    ``|f| <= pass_frac * fs``, ``stop_db`` for ``|f| >= stop_frac * fs`` and a cosine taper
    *in dB* in between: ``stop_db * (1 - cos(pi x)) / 2`` with
    ``x = (|f| - pass_frac fs) / ((stop_frac - pass_frac) fs)``. ``fs`` is the OUTPUT
    sample rate. Even in ``f`` (real coefficients). Scalar in -> ``float``, array in ->
    ``float64`` array.
    """
    fs = float(fs)
    lo, hi = pass_frac * fs, stop_frac * fs
    if np.ndim(f_hz) == 0:
        a = abs(float(f_hz))
        if a <= lo:
            return 0.0
        if a >= hi:
            return float(stop_db)
        return float(stop_db) * 0.5 * (1.0 - math.cos(math.pi * (a - lo) / (hi - lo)))
    a = np.abs(np.asarray(f_hz, dtype=np.float64))
    x = np.clip((a - lo) / (hi - lo), 0.0, 1.0)
    return float(stop_db) * 0.5 * (1.0 - np.cos(np.pi * x))


# --------------------------------------------------------------------------- IQ imbalance
def iq_image_ratio(gain_db: float, phase_deg: float) -> complex:
    """Complex ratio ``r = K2/K1`` of the image path for an IQ gain/phase imbalance.

    Model: ``I' = I``, ``Q' = g (Q cos(phi) - I sin(phi))`` with ``g = 10^(gain_db/20)``,
    i.e. ``y = K1 z + K2 conj(z)``, ``K1 = (1 + g e^{-j phi})/2``, ``K2 = (1 - g e^{j phi})/2``.
    The simulator applies ``y = z + r conj(z)`` (direct path normalized to unity gain).
    """
    g = 10.0 ** (gain_db / 20.0)
    phi = math.radians(phase_deg)
    k1 = (1.0 + g * complex(math.cos(phi), -math.sin(phi))) / 2.0
    k2 = (1.0 - g * complex(math.cos(phi), math.sin(phi))) / 2.0
    return k2 / k1


def image_rejection_db(gain_db: float, phase_deg: float) -> float:
    """IRR = 10 log10((1 + 2 g cos(phi) + g^2) / (1 - 2 g cos(phi) + g^2)), g = 10^(eps/20)."""
    g = 10.0 ** (gain_db / 20.0)
    c = math.cos(math.radians(phase_deg))
    den = 1.0 - 2.0 * g * c + g * g
    if den <= 0.0:
        return math.inf
    return 10.0 * math.log10((1.0 + 2.0 * g * c + g * g) / den)


# --------------------------------------------------------------------------- tones
def tone_bank(n: int, amps: np.ndarray, c0: np.ndarray, nu: np.ndarray) -> np.ndarray:
    """``sum_i amps[i] * exp(j 2 pi (c0[i] + nu[i] k))`` for ``k = 0..n-1`` as complex64.

    ``c0`` = start phase in cycles, ``nu`` = frequency in cycles/sample (aliasing is
    implicit). Uses the separable form ``k = q L + l``:
    ``exp(j2pi(c0 + nu q L)) * exp(j2pi nu l)`` so all tones are one small complex64
    matrix product (Q x T) @ (T x L) instead of ``T x n`` complex exponentials. Phases are
    reduced modulo 1 in float64 before every exponential, so precision does not degrade
    with ``n``.
    """
    out_n = int(n)
    amps = np.asarray(amps, dtype=np.complex128).reshape(-1)
    if out_n <= 0:
        return np.zeros(0, dtype=np.complex64)
    if amps.size == 0:
        return np.zeros(out_n, dtype=np.complex64)
    c0 = np.asarray(c0, dtype=np.float64).reshape(-1, 1)
    nu = np.asarray(nu, dtype=np.float64).reshape(-1, 1)
    blk = min(TONE_BLOCK, out_n)
    nq = -(-out_n // blk)
    q = np.arange(nq, dtype=np.float64)[None, :]
    lv = np.arange(blk, dtype=np.float64)[None, :]
    ph_q = c0 + (nu * blk) * q
    ph_q -= np.floor(ph_q)
    a_mat = (amps[:, None] * np.exp(1j * _TWO_PI * ph_q)).astype(np.complex64)      # (T, Q)
    ph_l = nu * lv
    ph_l -= np.floor(ph_l)
    b_mat = np.exp(1j * _TWO_PI * ph_l).astype(np.complex64)                          # (T, L)
    if amps.size == 1:                       # rank-1: outer product beats a k=1 GEMM
        return np.multiply.outer(a_mat[0], b_mat[0]).reshape(-1)[:out_n]
    return (a_mat.T @ b_mat).reshape(-1)[:out_n]


def tone_at(idx: np.ndarray, amp: complex, c0: float, nu: float) -> np.ndarray:
    """``amp * exp(j 2 pi (c0 + nu k))`` evaluated only at sample offsets ``idx`` (complex64)."""
    ph = idx.astype(np.float64) * nu
    ph += c0
    ph -= np.floor(ph)
    out = np.empty(idx.size, dtype=np.complex64)
    p32 = (ph * _TWO_PI).astype(np.float32)
    np.cos(p32, out=out.real)
    np.sin(p32, out=out.imag)
    out *= np.complex64(amp)
    return out


def gate_intervals(u0: float, du: float, n: int, duty: float) -> tuple[np.ndarray, np.ndarray]:
    """Index intervals ``[start, stop)`` where ``frac(u0 + du k) < duty``, ``k = 0..n-1``.

    ``u0`` in [0, 1) is the position inside the period of the first sample, ``du > 0`` the
    period fraction per sample. Computed analytically per period (no per-sample work).
    Used for pulse trains and GSM FCCH burst timing.
    """
    n = int(n)
    if n <= 0 or duty <= 0.0:
        return np.zeros(0, np.int64), np.zeros(0, np.int64)
    p = np.arange(0, math.floor(u0 + du * (n - 1)) + 1, dtype=np.float64)
    starts = np.ceil((p - u0) / du)
    stops = np.ceil((p + min(duty, 1.0) - u0) / du)
    starts = np.clip(starts, 0, n).astype(np.int64)
    stops = np.clip(stops, 0, n).astype(np.int64)
    keep = stops > starts
    return starts[keep], stops[keep]


def gate_mask(starts: np.ndarray, stops: np.ndarray, n: int) -> np.ndarray:
    """Boolean mask of length ``n`` that is True inside the given intervals."""
    mask = np.zeros(int(n), dtype=bool)
    if starts.size <= 512:
        for s, e in zip(starts.tolist(), stops.tolist()):
            mask[s:e] = True
        return mask
    edge = np.zeros(int(n) + 1, dtype=np.int32)
    np.add.at(edge, starts, 1)
    np.add.at(edge, stops, -1)
    return np.cumsum(edge[:-1]) > 0


def interval_indices(starts: np.ndarray, stops: np.ndarray) -> np.ndarray:
    """Concatenated ``arange(start, stop)`` of all intervals (int64)."""
    if starts.size == 0:
        return np.zeros(0, np.int64)
    lens = stops - starts
    total = int(lens.sum())
    offs = np.repeat(starts - np.concatenate(([0], np.cumsum(lens)[:-1])), lens)
    return np.arange(total, dtype=np.int64) + offs


def bessel_jn(beta: float, n_max: int) -> np.ndarray:
    """``J_n(beta)`` for ``n = -n_max..n_max`` (float64), numpy only.

    Uses the Jacobi-Anger identity ``exp(j beta sin t) = sum_n J_n(beta) exp(j n t)``: the
    DFT of ``exp(j beta sin(2 pi m / M))`` over a full period gives the coefficients with
    spectral (exponential) accuracy once ``M`` exceeds the occupied bandwidth.
    """
    m = 1 << max(6, math.ceil(math.log2(4.0 * (n_max + abs(beta) + 16.0))))
    tau = _TWO_PI * np.arange(m) / m
    c = np.fft.fft(np.exp(1j * beta * np.sin(tau))) / m
    n = np.arange(-n_max, n_max + 1)
    return c[n % m].real


def fm_sidebands(beta: float, tol: float = 1e-7) -> tuple[np.ndarray, np.ndarray]:
    """Significant Jacobi-Anger terms of sine FM: ``(n, J_n(beta))`` with ``|J_n| > tol``.

    Results are cached per ``(beta, tol)`` (pure function; the simulator evaluates it for
    every synthesized piece); the returned arrays are read-only.
    """
    return _fm_sidebands_cached(float(beta), float(tol))


@lru_cache(maxsize=128)
def _fm_sidebands_cached(beta: float, tol: float) -> tuple[np.ndarray, np.ndarray]:
    n_max = int(math.ceil(abs(beta) + 12.0 + 4.0 * abs(beta) ** (1.0 / 3.0)))
    j = bessel_jn(beta, n_max)
    n = np.arange(-n_max, n_max + 1)
    keep = np.abs(j) > tol
    n_out, j_out = n[keep], j[keep]
    n_out.flags.writeable = False
    j_out.flags.writeable = False
    return n_out, j_out


# --------------------------------------------------------------------------- ADC
def quantize_inplace(x: np.ndarray, full_scale_counts: float) -> None:
    """12-bit style quantization with HARD clipping of I and Q separately.

    ``x`` (contiguous complex64) is replaced by ``clip(rint(x * FS), -FS, FS - 1) / FS``.
    """
    if x.dtype != np.complex64 or not x.flags.c_contiguous:
        raise ValueError("quantize_inplace needs a contiguous complex64 array")
    v = x.view(np.float32)
    fs = np.float32(full_scale_counts)
    np.multiply(v, fs, out=v)
    np.rint(v, out=v)
    np.clip(v, -fs, fs - np.float32(1.0), out=v)
    np.multiply(v, np.float32(1.0) / fs, out=v)


def bins_to_slices(idx: np.ndarray) -> list[tuple[int, int]]:
    """Group sorted bin indices into contiguous ``[start, stop)`` runs."""
    if idx.size == 0:
        return []
    brk = np.nonzero(np.diff(idx) != 1)[0]
    starts = np.concatenate(([idx[0]], idx[brk + 1]))
    stops = np.concatenate((idx[brk] + 1, [idx[-1] + 1]))
    return [(int(s), int(e)) for s, e in zip(starts, stops)]


def fill_gaussian_bins(rng: np.random.Generator, row: np.ndarray, slices: list[tuple[int, int]],
                       scale: np.ndarray) -> None:
    """Write complex Gaussian values (unit variance per real component) times ``scale`` into
    ``row`` (complex64) at the given contiguous bin runs; ``scale`` is concatenated in run
    order.

    Exact Box-Muller: ``r = sqrt(-2 ln(1 - u1))`` with float64 ``u1`` (no tail truncation
    below 1e-16 probability), uniform phase ``2 pi u2``. ~1.7x faster than the ziggurat
    path for float32 output and statistically identical (complex circular Gaussian).
    """
    m = int(scale.size)
    if m == 0:
        return
    u = rng.random(m)
    np.subtract(1.0, u, out=u)
    np.log(u, out=u)
    u *= -2.0
    np.sqrt(u, out=u)
    r = u.astype(np.float32)
    r *= scale
    th = rng.random(m, dtype=np.float32)
    th *= np.float32(_TWO_PI)
    o = 0
    for s, e in slices:
        seg = row[s:e]
        n = e - s
        np.cos(th[o:o + n], out=seg.real)
        np.sin(th[o:o + n], out=seg.imag)
        seg *= r[o:o + n]
        o += n


def noise_bins(fs: float, nfft: int, bw_hz: float) -> tuple[np.ndarray, float]:
    """Frequency-domain bins of a flat band ``|f| <= bw/2`` on an ``nfft`` grid at ``fs``.

    Returns ``(bin_indices, m_eff)`` where ``m_eff = max(len(bins), bw * nfft / fs)`` is the
    divisor that makes the in-band power equal ``P * min(1, fs/bw)`` (a band wider than
    ``fs`` only delivers the part that fits). At least the DC bin is always returned so a
    band narrower than one bin still carries its power.
    """
    f = np.fft.fftfreq(nfft, d=1.0 / fs)
    idx = np.nonzero(np.abs(f) <= bw_hz / 2.0)[0]
    if idx.size == 0:
        idx = np.array([0], dtype=np.int64)
    m_eff = max(float(idx.size), bw_hz * nfft / fs)
    return idx, m_eff
