"""PPM calibration from a GSM BCCH carrier (FCCH bursts) — no lab reference needed.

A GSM base station's carrier is locked to a far better reference than the Pluto's TCXO
(3GPP: ±0.05 ppm). Its BCCH carrier transmits a **frequency correction burst** (FCCH) in
TDMA frames 0, 10, 20, 30, 40 of every 51-multiframe: 148 bits of all-zeros, which GMSK
turns into a *pure tone* at ``carrier + 1625/24 kHz`` (+67.708 kHz) lasting
``148 · 48/13 µs ≈ 546 µs``, roughly every 10 frames (frame = 120/26 ms ≈ 4.615 ms).

Algorithm (:func:`estimate_ppm_gsm_fcch`)
------------------------------------------
1. Tune ``fc = carrier − 200 kHz`` (FCCH tone at ≈ +267.7 kHz, far from the zero-IF DC
   spike and its own IQ image), sample at ``fs`` (default 1 MS/s), analog BW = fs.
2. Capture contiguous chunks (an overflow starts a new contiguous segment).
3. Short-time FFT frames (Hann, ≈ 256 µs, hop 1/4 frame). Per frame the *peak-to-mean
   ratio* inside the search window ``f_exp ± (1.5 · ref_ppm · f + margin)`` is computed,
   the mean excluding the peak's main lobe (robust against the tone's own leakage). A
   GMSK/noise frame reads a few dB; an FCCH frame reads ≈ the per-bin SNR.
4. Frames above a robust threshold (median + max(6 dB, 6σ_MAD) of all frames, at least
   10 dB) are candidates; runs of consecutive candidates with a consistent peak frequency
   and a plausible length form bursts.
5. Per burst: coarse frequency on the best frame, burst edges refined on the demodulated
   residual ``|z − c|²`` (inside the burst only noise remains, outside the GMSK), then the
   precise frequency on the burst core with :func:`eds.dsp.peaks.tone_frequency`.
6. ``ppm`` per burst (hardware correction composed in), robust aggregate: median, outliers
   beyond ``5 · 1.4826 · MAD`` (min 0.05 ppm) rejected, std = ``1.4826 · MAD``. At least
   3 bursts are required.
"""

from __future__ import annotations

import logging
import math
from dataclasses import dataclass
from typing import Any

import numpy as np
from numpy.lib.stride_tricks import sliding_window_view

from ..core.errors import CalibrationError, ConfigError, MeasurementError
from ..core.timeutil import now_ns
from ..core.units import fmt_hz
from ..dsp.peaks import tone_frequency
from ..dsp.windows import get_window
from .ppm import (
    SEARCH_TOLERANCE_FACTOR,
    PpmEstimate,
    _finite,
    _positive_int,
    channel_ppm_hw,
    compose_ppm,
    device_temperature_c,
    ppm_from_apparent,
    preserve_rx_settings,
)

log = logging.getLogger(__name__)

GSM_FRAME_S: float = 120e-3 / 26.0
GSM_BIT_S: float = 48e-6 / 13.0
GSM_FCCH_BURST_S: float = 148.0 * GSM_BIT_S
GSM_FCCH_OFFSET_HZ: float = 1625e3 / 24.0
#: LO is placed this far below the carrier (FCCH at ≈ +267.7 kHz baseband).
FCCH_LO_OFFSET_HZ: float = 200e3
#: Minimum number of bursts for a valid estimate.
MIN_BURSTS: int = 3

_BANDS = {"gsm900": "GSM900", "egsm": "GSM900", "egsm900": "GSM900", "pgsm": "GSM900",
          "dcs1800": "DCS1800", "gsm1800": "DCS1800", "dcs": "DCS1800"}


def arfcn_to_freq(arfcn: int, band: str = "GSM900") -> float:
    """Downlink (BCCH) carrier frequency of an ARFCN (3GPP TS 45.005), Hz.

    * ``GSM900``: P-GSM 1..124 → ``935 + 0.2·n`` MHz; E-GSM 0 and 975..1023 →
      ``935 + 0.2·(n − 1024)`` MHz (n = 0 → 935.0 MHz).
    * ``DCS1800``: 512..885 → ``1805.2 + 0.2·(n − 512)`` MHz.
    """
    if isinstance(arfcn, bool) or not isinstance(arfcn, (int, np.integer)):
        raise ConfigError(f"ARFCN tamsayı olmalı, {arfcn!r} geldi")
    n = int(arfcn)
    key = _BANDS.get(str(band).strip().lower().replace("-", "").replace("_", ""))
    if key is None:
        raise ConfigError(f"Bilinmeyen GSM bandı {band!r}; seçenekler: GSM900, DCS1800")
    if key == "GSM900":
        if 1 <= n <= 124:
            return 935e6 + 0.2e6 * n
        if n == 0:
            return 935e6
        if 975 <= n <= 1023:
            return 935e6 + 0.2e6 * (n - 1024)
        raise ConfigError(f"GSM900 ARFCN {n} geçersiz (1..124, E-GSM 0 ve 975..1023)")
    if 512 <= n <= 885:
        return 1805.2e6 + 0.2e6 * (n - 512)
    raise ConfigError(f"DCS1800 ARFCN {n} geçersiz (512..885)")


def _resolve_carrier(carrier: Any, band: str | None) -> float:
    """``carrier`` as Hz, or an integer ARFCN (< 2048) converted with ``band`` (auto)."""
    if isinstance(carrier, (int, np.integer)) and not isinstance(carrier, bool) and 0 <= int(carrier) < 2048:
        b = band if band is not None else ("DCS1800" if 512 <= int(carrier) <= 885 else "GSM900")
        return arfcn_to_freq(int(carrier), b)
    f = _finite(carrier, "carrier_hz")
    if f <= 0:
        raise ConfigError(f"GSM taşıyıcı frekansı pozitif olmalı, {f!r} geldi")
    return f


@dataclass
class FcchBurst:
    """One detected FCCH burst."""

    start: int              # absolute device sample index of the burst core start
    length: int             # core length (samples)
    f_bb_hz: float          # apparent baseband frequency (nominal-Hz)
    f_app_hz: float         # apparent RF frequency
    snr_db: float           # in-burst per-sample SNR (tone / residual)
    ppm_residual: float
    ppm: float              # total (hardware correction composed in)

    def to_dict(self) -> dict[str, Any]:
        return {"start": self.start, "length": self.length, "f_bb_hz": self.f_bb_hz, "f_app_hz": self.f_app_hz,
                "snr_db": self.snr_db, "ppm_residual": self.ppm_residual, "ppm": self.ppm}


class _FcchScanner:
    """Incremental FCCH burst detector over contiguous sample segments."""

    def __init__(self, fs: float, fc: float, f_fcch: float, f_exp_bb: float, half_hz: float, ppm_hw: float,
                 frame_len: int):
        self.fs, self.fc, self.f_fcch, self.ppm_hw = fs, fc, f_fcch, ppm_hw
        self.L = frame_len
        self.hop = max(1, frame_len // 4)
        self.win = np.asarray(get_window("hann", frame_len), dtype=np.float64)
        f = np.fft.fftfreq(frame_len, d=1.0 / fs)
        self.bins = np.flatnonzero(np.abs(f - f_exp_bb) <= half_hz)
        self.bins = self.bins[np.argsort(f[self.bins])]
        self.f_bins = f[self.bins]
        if self.bins.size < 8:
            raise ConfigError(
                f"FCCH arama penceresi yalnızca {self.bins.size} FFT bini içeriyor: örnekleme hızını ya da "
                "çerçeve süresini artırın.")
        self.burst_n = int(round(GSM_FCCH_BURST_S * fs))
        self.half_hz = half_hz
        self.f_exp_bb = f_exp_bb
        self.all_pmr: list[float] = []
        self.bursts: list[FcchBurst] = []
        self.rejected = 0
        self._accepted_starts: list[int] = []
        self._reset(0)

    # ------------------------------------------------------------------ segments
    def _reset(self, abs_start: int) -> None:
        self.buf = np.empty(0, dtype=np.complex128)
        self.buf0 = int(abs_start)
        self.next_frame = int(abs_start)
        self.fr_start: list[int] = []
        self.fr_pmr: list[float] = []
        self.fr_pk: list[float] = []
        self.carry = False           # first candidate run continues before the kept frames
        self.done: set[int] = set()

    def feed(self, x: np.ndarray, abs_start: int, gap_before: bool) -> None:
        if gap_before or self.buf.size == 0 or abs_start != self.buf0 + self.buf.size:
            self._reset(abs_start)
        self.buf = np.concatenate([self.buf, np.asarray(x, dtype=np.complex128)])
        self._frames()
        self._detect(final=False)
        self._trim()

    def finish(self) -> None:
        self._detect(final=True)

    # ------------------------------------------------------------------ frame statistics
    def _frames(self) -> None:
        end = self.buf0 + self.buf.size
        if self.next_frame + self.L > end:
            return
        a = self.next_frame - self.buf0
        m = (end - self.next_frame - self.L) // self.hop + 1
        seg = sliding_window_view(self.buf[a:a + (m - 1) * self.hop + self.L], self.L)[::self.hop][:m]
        spec = np.fft.fft(seg * self.win, axis=1)[:, self.bins]
        p = spec.real ** 2 + spec.imag ** 2
        k = np.argmax(p, axis=1)
        pk = p[np.arange(m), k]
        # mean outside the peak's Hann main lobe (+-2 bins)
        csum = np.concatenate([np.zeros((m, 1)), np.cumsum(p, axis=1)], axis=1)
        nb = p.shape[1]
        lo = np.clip(k - 2, 0, nb)
        hi = np.clip(k + 3, 0, nb)
        lobe = csum[np.arange(m), hi] - csum[np.arange(m), lo]
        rest = (csum[:, -1] - lobe) / np.maximum(nb - (hi - lo), 1)
        pmr = 10.0 * np.log10(np.maximum(pk, 1e-300) / np.maximum(rest, 1e-300))
        starts = self.next_frame + self.hop * np.arange(m)
        self.fr_start.extend(int(s) for s in starts)
        self.fr_pmr.extend(float(v) for v in pmr)
        self.fr_pk.extend(float(v) for v in self.f_bins[k])
        self.all_pmr.extend(float(v) for v in pmr)
        self.next_frame += m * self.hop

    def threshold_db(self) -> float:
        v = np.asarray(self.all_pmr)
        med = float(np.median(v))
        sig = 1.4826 * float(np.median(np.abs(v - med)))
        return max(med + max(6.0, 6.0 * sig), 10.0)

    # ------------------------------------------------------------------ detection
    def _detect(self, final: bool) -> None:
        if len(self.fr_pmr) < 8:
            return
        thr = self.threshold_db()
        cand = np.asarray(self.fr_pmr) >= thr
        if not np.any(cand):
            return
        edges = np.diff(np.concatenate([[0], cand.astype(np.int8), [0]]))
        starts = np.flatnonzero(edges == 1)
        stops = np.flatnonzero(edges == -1)          # exclusive
        n_fr = cand.size
        for i0, i1 in zip(starts.tolist(), stops.tolist()):
            key = self.fr_start[i0]
            if key in self.done:
                continue
            if i1 >= n_fr and not final:
                continue                              # run still open at the segment end
            need_end = self.fr_start[i1 - 1] + self.L + self.burst_n + 16
            if not final and self.buf0 + self.buf.size < need_end:
                continue                              # wait for the samples after the burst
            self.done.add(key)
            if i0 == 0 and self.carry:
                self.rejected += 1
                continue
            self._evaluate(i0, i1)

    def _evaluate(self, i0: int, i1: int) -> None:
        n_run = i1 - i0
        max_run = (self.burst_n + self.L) // self.hop + 2
        if n_run < 2 or n_run > max_run:
            self.rejected += 1
            return
        pks = np.asarray(self.fr_pk[i0:i1])
        bin_hz = self.fs / self.L
        if float(np.ptp(pks)) > 2.0 * bin_hz:
            self.rejected += 1                        # not one steady tone
            return
        best = i0 + int(np.argmax(self.fr_pmr[i0:i1]))
        s = self.fr_start[best]
        burst = self._refine(s, float(self.fr_pk[best]))
        if burst is None:
            self.rejected += 1
            return
        if any(abs(burst.start - a) < self.burst_n for a in self._accepted_starts):
            return
        self._accepted_starts.append(burst.start)
        self.bursts.append(burst)

    def _refine(self, s: int, f_peak: float) -> FcchBurst | None:
        """Precise frequency of the burst around frame start ``s`` (absolute index)."""
        fs, L = self.fs, self.L
        a = s - self.buf0
        frame = self.buf[a:a + L]
        try:
            f1 = tone_frequency(frame, fs, 0.0, f_hint=f_peak, search_hz=2.0 * fs / L, window="hann")
        except MeasurementError:
            return None
        lo = max(0, a - self.burst_n - 16)
        hi = min(self.buf.size, a + L + self.burst_n + 16)
        seg = self.buf[lo:hi]
        t = np.arange(lo, hi, dtype=np.float64) / fs
        z = seg * np.exp(-2j * np.pi * f1 * t)
        ia, ib = a - lo, a - lo + L
        c = np.mean(z[ia:ib])
        if abs(c) == 0.0:
            return None
        d = np.abs(z - c) ** 2
        k = 16
        sm = np.convolve(d, np.ones(k) / k, mode="same")
        inside = sm < 0.5 * abs(c) ** 2
        mid = (ia + ib) // 2
        if not inside[mid]:
            return None
        b0 = mid
        while b0 > 0 and inside[b0 - 1]:
            b0 -= 1
        b1 = mid
        while b1 < inside.size - 1 and inside[b1 + 1]:
            b1 += 1
        if (b0 == 0 and lo > 0) or (b1 == inside.size - 1 and hi < self.buf.size):
            return None                               # tone longer than a burst: not an FCCH
        guard = k // 2 + 2
        c0, c1 = b0 + guard, b1 - guard + 1
        length = c1 - c0
        if length < max(64, int(0.5 * self.burst_n)) or length > int(1.5 * self.burst_n):
            return None
        core = seg[c0:c1]
        try:
            f_bb = tone_frequency(core, fs, 0.0, f_hint=f1, search_hz=4.0 * fs / length, window="hann")
        except MeasurementError:
            return None
        tt = np.arange(length, dtype=np.float64) / fs
        e = np.exp(2j * np.pi * f_bb * tt)
        amp = np.vdot(e, core) / length
        resid = core - amp * e
        p_res = float(np.mean(np.abs(resid) ** 2))
        snr = 10.0 * math.log10(max(abs(amp) ** 2, 1e-300) / max(p_res, 1e-300))
        f_app = self.fc + f_bb
        r = ppm_from_apparent(self.f_fcch, f_app)
        return FcchBurst(start=self.buf0 + lo + c0, length=length, f_bb_hz=f_bb, f_app_hz=f_app, snr_db=snr,
                         ppm_residual=r, ppm=compose_ppm(self.ppm_hw, r))

    # ------------------------------------------------------------------ memory bound
    def _trim(self) -> None:
        if not self.fr_start:
            return
        keep_from = self.next_frame - 2 * self.burst_n - 4 * self.L
        cut = 0
        while cut < len(self.fr_start) and self.fr_start[cut] < keep_from:
            cut += 1
        if cut:
            thr = self.threshold_db() if len(self.all_pmr) >= 8 else math.inf
            # A candidate run crossing the cut continues "before" the kept frames: it can no
            # longer be measured as a whole, so the first kept run is rejected (carry).
            next_cand = self.fr_pmr[cut] >= thr if cut < len(self.fr_pmr) else True
            self.carry = bool(self.fr_pmr[cut - 1] >= thr and next_cand)
            del self.fr_start[:cut], self.fr_pmr[:cut], self.fr_pk[:cut]
        first_needed = (self.fr_start[0] if self.fr_start else self.next_frame) - self.burst_n - 32
        drop = first_needed - self.buf0
        if drop > 0:
            self.buf = self.buf[drop:].copy()
            self.buf0 += drop


def _robust_aggregate(vals: np.ndarray) -> tuple[float, float, np.ndarray]:
    """``(median, 1.4826·MAD, kept_mask)`` after rejecting outliers beyond 5σ (min 0.05 ppm)."""
    med = float(np.median(vals))
    mad = float(np.median(np.abs(vals - med)))
    lim = max(5.0 * 1.4826 * mad, 0.05)
    keep = np.abs(vals - med) <= lim
    kept = vals[keep]
    med = float(np.median(kept))
    std = 1.4826 * float(np.median(np.abs(kept - med)))
    if std == 0.0 and kept.size >= 2:
        std = float(np.std(kept, ddof=1))
    return med, std, keep


def estimate_ppm_gsm_fcch(device: Any, carrier_hz: float, n_bursts: int = 10, fs: float = 1.0e6,
                          max_capture_s: float = 5.0, *, band: str | None = None, gain_db: float | None = None,
                          channel: int = 0, ref_clock_ppm: float | None = None, frame_s: float = 256e-6,
                          chunk: int = 1 << 16, timeout_s: float = 2.0) -> PpmEstimate:
    """PPM calibration from the FCCH bursts of a GSM BCCH carrier (see module docstring).

    ``carrier_hz`` is the downlink BCCH carrier in Hz, or an integer ARFCN (< 2048; band
    from ``band`` or automatic: 512..885 → DCS1800, else GSM900). Stops after ``n_bursts``
    bursts or ``max_capture_s`` of samples. The device's settings are restored afterwards.
    Returns the TOTAL ppm (hardware correction composed in); per-burst details in
    ``extra['bursts']``.

    Raises :class:`CalibrationError` (Turkish, with hints) if fewer than 3 bursts are found.
    """
    f_carrier = _resolve_carrier(carrier_hz, band)
    n_bursts = _positive_int(n_bursts, "n_bursts", MIN_BURSTS)
    fs = _finite(fs, "fs")
    max_capture_s = _finite(max_capture_s, "max_capture_s")
    if max_capture_s <= 0:
        raise ConfigError("max_capture_s pozitif olmalı")
    frame_s = _finite(frame_s, "frame_s")
    if not 50e-6 <= frame_s <= GSM_FCCH_BURST_S:
        raise ConfigError(f"frame_s {frame_s * 1e6:.0f} µs; 50 µs ile FCCH süresi (546 µs) arasında olmalı")
    chunk = _positive_int(chunk, "chunk", 1024)
    channel = int(channel)
    if not 0 <= channel < int(device.num_channels):
        raise ConfigError(f"{device.name}: kanal {channel} yok ({device.num_channels} kanal)")
    ref_ppm = float(device.profile.ref_clock_ppm) if ref_clock_ppm is None else _finite(ref_clock_ppm, "ref_clock_ppm")
    f_fcch = f_carrier + GSM_FCCH_OFFSET_HZ
    fc = f_carrier - FCCH_LO_OFFSET_HZ
    f_exp_bb = f_fcch - fc
    frame_len = 1 << max(6, int(round(math.log2(frame_s * fs))))
    t0 = now_ns()
    total = 0
    max_samples = int(max_capture_s * fs)
    with preserve_rx_settings(device):
        device.set_sample_rate(fs)
        prof = device.profile
        device.set_bandwidth(min(max(fs, prof.bw_min_hz), prof.bw_max_hz))
        device.set_center_freq(fc)
        if gain_db is not None:
            device.set_gain_mode("manual")
            device.set_gain(_finite(gain_db, "gain_db"))
        fs_act = float(device.fs)
        temperature = device_temperature_c(device, channel)
        ppm_hw = channel_ppm_hw(device, channel)
        half = (SEARCH_TOLERANCE_FACTOR * ref_ppm + abs(ppm_hw)) * 1e-6 * f_fcch + 2.0 * fs_act / frame_len
        if abs(f_exp_bb) + half > 0.45 * fs_act:
            raise ConfigError(
                f"FCCH arama penceresi ({fmt_hz(f_exp_bb)} ± {fmt_hz(half)}) fs = {fmt_hz(fs_act)} bandına sığmıyor: "
                "örnekleme hızını artırın.")
        scanner = _FcchScanner(fs_act, float(device.fc), f_fcch, f_exp_bb, half, ppm_hw, frame_len)
        blk = device.capture(chunk, timeout_s)
        first = True
        while True:
            scanner.ppm_hw = channel_ppm_hw(device, channel, blk)
            scanner.feed(blk.samples[channel], blk.index, gap_before=bool(blk.overflow) and not first)
            first = False
            total += blk.n
            if len(scanner.bursts) >= n_bursts or total >= max_samples:
                break
            blk = device.read(chunk, timeout_s)
            if blk.settling:
                raise CalibrationError(f"{device.name}: FCCH yakalaması sırasında RX ayarı değişti (settling)")
        scanner.finish()
        gains = list(device.gains)
    bursts = scanner.bursts[:n_bursts] if len(scanner.bursts) > n_bursts else scanner.bursts
    if len(bursts) < MIN_BURSTS:
        raise CalibrationError(
            f"{device.name}: {fmt_hz(f_carrier)} üzerinde {total / fs_act:.2f} s içinde yalnızca {len(bursts)} FCCH "
            f"patlaması bulundu (en az {MIN_BURSTS} gerekli). Kontrol: taşıyıcı bir BCCH mi (ARFCN doğru mu), "
            "sinyal yeterince güçlü mü (anten/kazanç), bölgede GSM yayını var mı? max_capture_s'yi artırmayı deneyin.")
    vals = np.asarray([b.ppm for b in bursts])
    med, std, keep = _robust_aggregate(vals)
    used = [b for b, k in zip(bursts, keep.tolist()) if k]
    res = float(np.median([b.ppm_residual for b in used]))
    f_app = float(np.median([b.f_app_hz for b in used]))
    snr = float(np.median([b.snr_db for b in used]))
    if len(bursts) < n_bursts:
        log.warning("%s: %d FCCH patlaması istendi, %d bulundu (%.2f s)", device.name, n_bursts, len(bursts),
                    total / fs_act)
    est = PpmEstimate(
        ppm=med, std_ppm=std, f_measured_hz=f_app, f_ref_hz=f_fcch, snr_db=snr, n=total, method="gsm_fcch",
        temperature_c=temperature, t_ns=t0, fc_nominal_hz=fc, fs_hz=fs_act, ppm_residual=res,
        ppm_hw=scanner.ppm_hw, repeats=len(used),
        extra={"carrier_hz": f_carrier, "bursts": [b.to_dict() for b in bursts], "bursts_used": len(used),
               "bursts_outliers": int(len(bursts) - len(used)), "candidates_rejected": scanner.rejected,
               "frame_len": frame_len, "threshold_db": scanner.threshold_db(), "capture_s": total / fs_act,
               "gain_db": gains, "channel": channel, "device": device.name,
               "serial": getattr(device.info, "serial", ""),
               "snr_definition": "patlama içi örnek başına SNR (fs bandı)",
               "std_definition": "1.4826·MAD (patlamalar arası)"},
    )
    log.info("%s: %s", device.name, est.summary_tr())
    return est
