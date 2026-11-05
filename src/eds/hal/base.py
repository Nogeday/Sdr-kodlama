"""Hardware abstraction contract.

Every receiver (SoapySDR device, simulator, 2×Pluto group, later KrakenSDR/Heimdall) is an
:class:`RxDevice`. Upper layers (streamer, sweeper, calibration, DF, measurements) only
talk to this interface, so new hardware = new backend file, no rewrite upstream.

Backends implement the small ``_apply_*`` / ``_start`` / ``_read_raw`` / ``_stop`` /
``_close`` primitives. This base class owns everything that must behave identically on
every backend:

* validation against the :class:`~eds.core.profile.DeviceProfile` (before hardware),
* gain clamping to the band-dependent gain range,
* **settling bookkeeping** — after a retune / gain change the next
  ``settle_buffers × buffer_size`` samples are flagged ``settling`` (stale libiio kernel
  buffers + PLL lock), and :meth:`capture` / :meth:`flush` discard them,
* **overflow handling** — :meth:`read` never returns a block with an internal gap: if the
  driver reports an overflow mid-block, the partial data is discarded and the block is
  restarted with ``overflow=True`` (gap *before* the block),
* stream statistics (:class:`~eds.core.types.StreamStats`).
"""

from __future__ import annotations

import logging
import threading
from abc import ABC, abstractmethod
from dataclasses import dataclass
from typing import Any

import numpy as np

from ..core.config import DeviceConfig, RxConfig
from ..core.errors import NotSupported, StreamError, StreamTimeout
from ..core.profile import DeviceProfile
from ..core.timeutil import mono, now_ns
from ..core.types import DeviceInfo, GainRange, IQBlock, StreamStats

log = logging.getLogger(__name__)

GAIN_MODES = ("manual", "hw_slow", "hw_fast", "hw_hybrid")


@dataclass(slots=True)
class RawRead:
    """What a backend's ``_read_raw`` returns.

    samples        complex64 (n_channels, k), 0 <= k <= requested, normalized full scale = 1.0
    overflow_before  True if samples were lost *before* ``samples[:, 0]`` (driver overflow)
    timeout        True if nothing arrived within the timeout (k must be 0)
    t_ns           host wall-clock time (time.time_ns) when the read returned
    hw_time_ns     device timestamp of samples[:, 0] if the driver provides one, else None
    """

    samples: np.ndarray
    overflow_before: bool = False
    timeout: bool = False
    t_ns: int = 0
    hw_time_ns: int | None = None


class RxDevice(ABC):
    """One receiver with ``num_channels`` RX channels (coherent iff ``coherent``)."""

    backend_name: str = "abstract"

    def __init__(self, cfg: DeviceConfig, profile: DeviceProfile):
        self.cfg = cfg
        self.profile = profile
        self.info = DeviceInfo(name=cfg.name, backend=self.backend_name, profile=profile.name)
        self.stats = StreamStats()
        self.warnings: list[str] = []
        self.fc: float = 0.0
        self.fs: float = 0.0
        self.bw: float = 0.0
        self.gain_mode: str = "manual"
        self.gains: list[float] = [0.0] * len(cfg.channels)
        self.buffer_size: int = profile.default_buffer_size
        self.settle_buffers: int = profile.kernel_buffers + 1
        self.ppm_hw: float = 0.0              # ppm already compensated by hardware/driver
        self.ppm_hw_via: str = "none"         # "hardware" | "soapy" | "none"
        self._seq = 0
        self._index = 0                       # host-side index of next sample to be read
        self._settle_until = 0
        self._streaming = False
        self._lock = threading.RLock()

    # ------------------------------------------------------------------ capabilities
    @property
    def num_channels(self) -> int:
        return len(self.cfg.channels)

    @property
    def coherent(self) -> bool:
        """True if all channels share LO + sample clock (DF-ready without extra cal)."""
        return self.num_channels == 1 or self.profile.coherent_rx

    @property
    def name(self) -> str:
        return self.cfg.name

    @property
    def streaming(self) -> bool:
        return self._streaming

    @property
    def settle_samples(self) -> int:
        return int(self.settle_buffers * self.buffer_size)

    def runtime_limits(self) -> dict[str, Any]:
        """Limits reported by the driver at runtime (backend may override)."""
        return {}

    def gain_range(self, freq_hz: float | None = None) -> GainRange:
        """Profile gain range at ``freq_hz`` (defaults to current fc), ∩ runtime range."""
        gr = self.profile.gain_range(freq_hz if freq_hz is not None else (self.fc or None))
        rt = self.runtime_limits().get("gain_db")
        if rt:
            lo, hi = max(gr.min_db, rt[0]), min(gr.max_db, rt[1])
            if lo <= hi:
                gr = GainRange(lo, hi, gr.step_db)
        return gr

    # ------------------------------------------------------------------ backend primitives
    @abstractmethod
    def _apply_sample_rate(self, hz: float) -> float: ...

    @abstractmethod
    def _apply_bandwidth(self, hz: float) -> float: ...

    @abstractmethod
    def _apply_center_freq(self, hz: float) -> float: ...

    @abstractmethod
    def _apply_gain(self, db: float, channel: int) -> float: ...

    @abstractmethod
    def _apply_gain_mode(self, mode: str) -> None: ...

    @abstractmethod
    def _start(self, buffer_size: int) -> None: ...

    @abstractmethod
    def _read_raw(self, n: int, timeout_s: float) -> RawRead: ...

    @abstractmethod
    def _stop(self) -> None: ...

    @abstractmethod
    def _close(self) -> None: ...

    # optional backend features ----------------------------------------------------
    def set_tracking(self, dc: bool | None = None, rf_dc: bool | None = None,
                     quadrature: bool | None = None) -> dict[str, str]:
        """Enable/disable AD936x BB-DC / RF-DC / quadrature tracking.

        Returns ``{feature: path}`` where path is "soapy", "iio", "sim" or "unsupported".
        """
        return {k: "unsupported" for k, v in (("dc", dc), ("rf_dc", rf_dc), ("quadrature", quadrature))
                if v is not None}

    def set_ppm_correction(self, ppm: float) -> str:
        """Compensate reference-clock error in hardware if possible.

        Returns the mechanism used: "hardware" (e.g. Pluto ``xo_correction``), "soapy"
        (``setFrequencyCorrection``) or "none" (caller must correct in software).
        Implementations must set ``self.ppm_hw`` / ``self.ppm_hw_via`` accordingly.
        """
        self.ppm_hw, self.ppm_hw_via = 0.0, "none"
        return "none"

    def read_sensors(self) -> dict[str, float | str | None]:
        """Temperature (``temperature_c``), RSSI etc. Empty dict if unavailable."""
        return {}

    # ------------------------------------------------------------------ configuration
    def configure(self, rx: RxConfig) -> list[str]:
        """Validate *all* settings against the profile first, then apply. Returns warnings."""
        warns = self.profile.validate_rx(
            center_freq_hz=rx.center_freq_hz, sample_rate_hz=rx.sample_rate_hz,
            bandwidth_hz=rx.bandwidth_hz,
            gain_db=rx.gain_db if rx.gain_mode in ("manual", "soft_agc") else None,
            n_channels=self.num_channels,
        )
        self.buffer_size = int(rx.buffer_size)
        self.settle_buffers = int(rx.settle_buffers) if rx.settle_buffers >= 0 else self.profile.kernel_buffers + 1
        self.set_sample_rate(rx.sample_rate_hz)
        self.set_bandwidth(rx.bandwidth_hz)
        self.set_center_freq(rx.center_freq_hz)
        hw_mode = "manual" if rx.gain_mode == "soft_agc" else rx.gain_mode
        self.set_gain_mode(hw_mode)
        if hw_mode == "manual":
            self.set_gain(rx.gain_db)
        try:
            self.set_tracking(dc=rx.dc_tracking, rf_dc=rx.rf_dc_tracking, quadrature=rx.quad_tracking)
        except NotSupported as e:  # pragma: no cover - backend specific
            warns.append(str(e))
        for w in warns:
            log.warning("%s: %s", self.name, w)
        self.warnings.extend(warns)
        return warns

    def set_center_freq(self, hz: float) -> float:
        err, _ = self.profile.check_rx(center_freq_hz=hz)
        if err:
            from ..core.errors import ProfileViolation
            raise ProfileViolation(err)
        with self._lock:
            self.fc = float(self._apply_center_freq(float(hz)))
            self.mark_settling()
            # band-dependent gain range: re-clamp current gain at the new frequency
            if self.gain_mode == "manual":
                gr = self.gain_range(self.fc)
                for ch, g in enumerate(self.gains):
                    if not (gr.min_db <= g <= gr.max_db):
                        self.gains[ch] = float(self._apply_gain(gr.clamp(g), ch))
        return self.fc

    def set_sample_rate(self, hz: float) -> float:
        errs, warns = self.profile.check_rx(sample_rate_hz=hz, n_channels=self.num_channels)
        if errs:
            from ..core.errors import ProfileViolation
            raise ProfileViolation(errs)
        with self._lock:
            self.fs = float(self._apply_sample_rate(float(hz)))
            self.mark_settling()
        return self.fs

    def set_bandwidth(self, hz: float) -> float:
        errs, _ = self.profile.check_rx(bandwidth_hz=hz)
        if errs:
            from ..core.errors import ProfileViolation
            raise ProfileViolation(errs)
        with self._lock:
            self.bw = float(self._apply_bandwidth(float(hz)))
            self.mark_settling()
        return self.bw

    def set_gain(self, db: float, channel: int | None = None) -> float:
        """Set manual gain (clamped/quantized to the band's range). Returns actual gain."""
        gr = self.gain_range(self.fc or None)
        g = gr.clamp(db)
        chans = range(self.num_channels) if channel is None else [channel]
        with self._lock:
            for ch in chans:
                self.gains[ch] = float(self._apply_gain(g, ch))
            self.mark_settling()
        return self.gains[chans[0] if channel is not None else 0]

    def set_gain_mode(self, mode: str) -> None:
        if mode not in GAIN_MODES:
            raise ValueError(f"gain mode {mode!r} not in {GAIN_MODES}")
        with self._lock:
            self._apply_gain_mode(mode)
            self.gain_mode = mode
            self.mark_settling()

    def mark_settling(self, n_samples: int | None = None) -> None:
        """Flag the next ``n_samples`` (default ``settle_samples``) as settling."""
        self._settle_until = self._index + int(self.settle_samples if n_samples is None else n_samples)

    # ------------------------------------------------------------------ streaming
    def start_stream(self, buffer_size: int | None = None) -> None:
        with self._lock:
            if self._streaming:
                return
            if buffer_size:
                self.buffer_size = int(buffer_size)
            self._start(self.buffer_size)
            self._streaming = True
            self.stats = StreamStats(start_mono=mono(), last_mono=mono())
            self.mark_settling()

    def stop_stream(self) -> None:
        with self._lock:
            if not self._streaming:
                return
            try:
                self._stop()
            finally:
                self._streaming = False

    def read(self, n: int | None = None, timeout_s: float = 2.0) -> IQBlock:
        """Read exactly ``n`` contiguous samples per channel (default ``buffer_size``).

        Raises :class:`StreamTimeout` if the driver delivers nothing for ``timeout_s``.
        """
        if not self._streaming:
            raise StreamError(f"{self.name}: stream not started")
        n = int(n or self.buffer_size)
        parts: list[np.ndarray] = []
        got = 0
        overflow = False
        hw_first: int | None = None
        t_last = 0
        while got < n:
            r = self._read_raw(n - got, timeout_s)
            if r.timeout:
                self.stats.timeouts += 1
                raise StreamTimeout(f"{self.name}: {timeout_s:.2f} s içinde örnek gelmedi")
            if r.overflow_before:
                self.stats.overflows += 1
                overflow = True
                if got:
                    self.stats.discarded_samples += got
                    self._index += got
                    parts, got, hw_first = [], 0, None
            k = r.samples.shape[1]
            if k:
                if not parts:
                    hw_first = r.hw_time_ns
                parts.append(r.samples)
                got += k
                t_last = r.t_ns
        samples = parts[0] if len(parts) == 1 else np.concatenate(parts, axis=1)
        samples = np.ascontiguousarray(samples, dtype=np.complex64)
        index = self._index
        self._index += n
        settling = index < self._settle_until
        block = IQBlock(
            samples=samples, fs=self.fs, fc=self.fc,
            t_ns=int(t_last - round(n / self.fs * 1e9)) if self.fs else t_last or now_ns(),
            index=index, seq=self._seq, gain_db=tuple(self.gains), overflow=overflow,
            settling=settling, meta={"ppm_hw": self.ppm_hw, "ppm_hw_via": self.ppm_hw_via,
                                     "gain_mode": self.gain_mode, "bw": self.bw},
        )
        if hw_first is not None:
            block.meta["hw_time_ns"] = hw_first
        members = getattr(self, "ppm_hw_members", None)
        if members is not None:
            block.meta["ppm_hw_members"] = list(members)
        self._seq += 1
        st = self.stats
        st.blocks += 1
        st.samples += n
        st.settling_blocks += int(settling)
        st.last_mono = mono()
        return block

    def flush(self, timeout_s: float = 2.0) -> int:
        """Read and discard until the settling window has passed. Returns samples dropped."""
        dropped = 0
        while self._index < self._settle_until:
            k = min(self.buffer_size, self._settle_until - self._index)
            self.read(k, timeout_s)
            dropped += k
        return dropped

    def capture(self, n: int, timeout_s: float = 2.0, allow_overflow: bool = True) -> IQBlock:
        """One-shot contiguous, *settled* capture of ``n`` samples (starts stream if needed).

        With ``allow_overflow=False`` the capture is retried (max 5×) until the block is
        not preceded by an overflow — use for measurements that need a fresh, gap-free
        record relative to the previous one (e.g. phase continuity tests).
        """
        if not self._streaming:
            self.start_stream()
        for _ in range(6):
            self.flush(timeout_s)
            blk = self.read(n, timeout_s)
            if blk.settling:
                continue
            if blk.overflow and not allow_overflow:
                continue
            return blk
        raise StreamError(f"{self.name}: settled/overflow-free capture alınamadı")

    # ------------------------------------------------------------------ lifecycle
    def close(self) -> None:
        try:
            self.stop_stream()
        finally:
            self._close()

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()

    def describe(self) -> dict[str, Any]:
        """State snapshot for status messages / reports."""
        return {
            "info": self.info.to_dict(), "fc_hz": self.fc, "fs_hz": self.fs, "bw_hz": self.bw,
            "gain_db": list(self.gains), "gain_mode": self.gain_mode,
            "num_channels": self.num_channels, "coherent": self.coherent,
            "ppm_hw": self.ppm_hw, "ppm_hw_via": self.ppm_hw_via,
            "buffer_size": self.buffer_size, "settle_buffers": self.settle_buffers,
        }
