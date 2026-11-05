"""N receivers as one multi-channel :class:`~eds.hal.base.RxDevice` (phase 0: 2× Pluto).

Channels are stacked in member order (member 0's channels first). Each member is read by
its **own persistent worker thread** (one single-thread executor per member, created once),
so ``read(n)`` pulls ``n`` samples from every member concurrently and returns one
``(Σ channels, n)`` block.

Coherence: separate Plutos have independent TCXOs and LO phases, so the group reports
``coherent = False`` unless ``assume_coherent=True`` (shared reference + phase calibration
done upstream, see ``eds.df.coherence``). Alignment between members is only *coarse*
(host time); ``last_skew_ns`` is the spread of the members' first-sample host times for
the last block. Any member overflow — or a failed/timeouted member read, after which the
members are no longer read in lock-step — sets ``overflow`` on the group block: alignment
is lost and DF must re-calibrate.

USB load: the group profile scales the interface limits by the number of members (each
device has its own USB link). If several Plutos share one USB 2.0 host controller the
*total* is what counts — :meth:`DeviceGroup.configure` adds a warning for that case;
verify with ``eds measure max_rate``.
"""

from __future__ import annotations

import dataclasses
import logging
import threading
from collections.abc import Sequence
from concurrent.futures import Future, ThreadPoolExecutor
from concurrent.futures import TimeoutError as FutureTimeout
from typing import Any

import numpy as np

from ..core.config import DeviceConfig, RxConfig
from ..core.errors import ConfigError, DeviceError, EDSError, StreamError, StreamTimeout
from ..core.types import IQBlock
from .base import RawRead, RxDevice

log = logging.getLogger(__name__)


class DeviceGroup(RxDevice):
    """Combine member :class:`RxDevice` objects into one ``num_channels = Σ`` device."""

    backend_name = "group"
    _seq = 0
    _seq_lock = threading.Lock()

    def __init__(self, devices: Sequence[RxDevice], *, assume_coherent: bool = False,
                 read_margin_s: float = 5.0):
        members = list(devices)
        if not members:
            raise ConfigError("DeviceGroup: en az bir üye cihaz gerekli")
        if len({id(m) for m in members}) != len(members):
            raise ConfigError("DeviceGroup: aynı cihaz nesnesi gruba birden fazla kez eklenemez")
        names = [m.name for m in members]
        if len(set(names)) != len(names):
            raise ConfigError(f"DeviceGroup: üye adları benzersiz olmalı: {names}")
        pname = members[0].profile.name
        bad = [f"{m.name}={m.profile.name}" for m in members if m.profile.name != pname]
        if bad:
            raise ConfigError(f"DeviceGroup: tüm üyeler aynı cihaz profilini paylaşmalı ({pname}); farklı olanlar: {bad}")
        total = sum(m.num_channels for m in members)
        base = members[0].profile
        k = len(members)
        profile = dataclasses.replace(
            base,
            rx_channels=max(total, base.rx_channels),
            coherent_rx=bool(members[0].coherent) if k == 1 else bool(assume_coherent),
            interface_max_bytes_per_s=base.interface_max_bytes_per_s * k,
            interface_practical_bytes_per_s=base.interface_practical_bytes_per_s * k,
        )
        cfg = DeviceConfig(name="+".join(names), backend="group", profile=pname, args={},
                           control_uri="none", channels=list(range(total)))
        super().__init__(cfg, profile)
        self.member_profile = base
        self._members = members
        self._assume_coherent = bool(assume_coherent)
        self._read_margin_s = float(read_margin_s)
        self._chan_map: list[tuple[RxDevice, int]] = [(m, i) for m in members for i in range(m.num_channels)]
        with DeviceGroup._seq_lock:
            DeviceGroup._seq += 1
            self._thread_tag = f"eds-group{DeviceGroup._seq}"
        self._pools = [ThreadPoolExecutor(max_workers=1, thread_name_prefix=f"{self._thread_tag}-{m.name}")
                       for m in members]
        self._misaligned = False
        self._closed = False
        self.last_skew_ns = 0
        self.last_member_t_ns: list[int] = []
        self.ppm_hw_members: list[float] = [float(m.ppm_hw) for m in members]
        self.last_ppm_paths: dict[str, str] = {}

        self.last_tracking_paths: dict[str, dict[str, str]] = {}

        def uniq(xs: Any) -> str:  # order-preserving "+"-join of non-empty values
            return "+".join(dict.fromkeys(x for x in xs if x))

        serials = [m.info.serial for m in members]
        self.info.driver = uniq(m.info.driver for m in members)
        self.info.hardware = uniq(m.info.hardware for m in members)
        self.info.serial = "+".join(s or m.name for s, m in zip(serials, members))
        self.info.firmware = uniq(m.info.firmware for m in members)
        self.info.uri = "+".join(m.info.uri or m.name for m in members)
        self.info.extra = {"members": ",".join(names), "member_serials": ",".join(serials)}
        for m in members:
            self.info.extra[f"{m.name}.serial"] = m.info.serial
            self.info.extra[f"{m.name}.backend"] = m.info.backend

        # mirror the current member state
        m0 = members[0]
        self.fc, self.fs, self.bw = m0.fc, m0.fs, m0.bw
        self.gain_mode = m0.gain_mode
        self.gains = [float(g) for m in members for g in m.gains]
        self.buffer_size = m0.buffer_size
        self.settle_buffers = m0.settle_buffers

    # ------------------------------------------------------------------ capabilities
    @property
    def members(self) -> list[RxDevice]:
        return list(self._members)

    @property
    def coherent(self) -> bool:
        if len(self._members) == 1:
            return self._members[0].coherent
        return self._assume_coherent

    def runtime_limits(self) -> dict[str, Any]:
        """Intersection of the members' runtime limits."""
        out: dict[str, tuple[float, float]] = {}
        lims = [m.runtime_limits() for m in self._members]
        keys = set().union(*(set(x) for x in lims))
        for key in keys:
            vals = [x[key] for x in lims if key in x]
            lo, hi = max(v[0] for v in vals), min(v[1] for v in vals)
            if lo <= hi:
                out[key] = (lo, hi)
        return out

    # ------------------------------------------------------------------ configuration
    def configure(self, rx: RxConfig) -> list[str]:
        for m in self._members:
            m.buffer_size = int(rx.buffer_size)
            m.settle_buffers = int(rx.settle_buffers) if rx.settle_buffers >= 0 else m.profile.kernel_buffers + 1
        warns = super().configure(rx)
        p = self.member_profile
        wire_total = p.wire_rate_bytes_per_s(rx.sample_rate_hz, self.num_channels)
        if len(self._members) > 1 and wire_total > p.interface_practical_bytes_per_s:
            w = (f"{len(self._members)} cihaz toplamı {wire_total / 1e6:.1f} MB/s: cihazlar aynı {p.interface} "
                 f"denetleyicisini paylaşıyorsa (pratik ~{p.interface_practical_bytes_per_s / 1e6:.0f} MB/s) overflow "
                 "beklenir — ayrı USB denetleyicileri kullanın ve `eds measure max_rate` ile doğrulayın.")
            log.warning("%s: %s", self.name, w)
            warns.append(w)
            self.warnings.append(w)
        return warns

    def _apply_sample_rate(self, hz: float) -> float:
        return [m.set_sample_rate(hz) for m in self._members][0]

    def _apply_bandwidth(self, hz: float) -> float:
        return [m.set_bandwidth(hz) for m in self._members][0]

    def _apply_center_freq(self, hz: float) -> float:
        return [m.set_center_freq(hz) for m in self._members][0]

    def _apply_gain(self, db: float, channel: int) -> float:
        member, local = self._chan_map[channel]
        return float(member.set_gain(db, channel=local))

    def _apply_gain_mode(self, mode: str) -> None:
        for m in self._members:
            m.set_gain_mode(mode)

    # ------------------------------------------------------------------ streaming
    def _start(self, buffer_size: int) -> None:
        if self._closed:
            raise DeviceError(f"{self.name}: grup kapalı")
        started: list[RxDevice] = []
        try:
            for m in self._members:
                m.start_stream(buffer_size)
                started.append(m)
        except EDSError:
            for m in started:
                try:
                    m.stop_stream()
                except EDSError as e:
                    log.warning("%s: %s başlatma geri alınırken durdurulamadı: %s", self.name, m.name, e)
            raise
        self._misaligned = False

    def _read_raw(self, n: int, timeout_s: float) -> RawRead:
        if self._closed:
            raise StreamError(f"{self.name}: grup kapalı")
        futures: list[Future[IQBlock]] = [
            pool.submit(m.read, n, timeout_s) for pool, m in zip(self._pools, self._members)
        ]
        wait_s = float(timeout_s) + self._read_margin_s
        blocks: list[IQBlock] = []
        errors: list[EDSError] = []
        for m, fut in zip(self._members, futures):
            try:
                blocks.append(fut.result(timeout=wait_s))
            except FutureTimeout:
                errors.append(StreamError(f"{m.name}: üye okuması {wait_s:.1f} s içinde dönmedi (sürücü takıldı?)"))
            except EDSError as e:
                errors.append(e)
            except Exception as e:  # member bug: surface as a stream error of the group
                errors.append(StreamError(f"{m.name}: üye okuması başarısız: {e!r}"))
        if errors:
            # members are no longer read in lock-step: the next block must carry overflow
            self._misaligned = True
            hard = [e for e in errors if not isinstance(e, StreamTimeout)]
            if hard:
                raise hard[0]
            return RawRead(samples=np.empty((self.num_channels, 0), dtype=np.complex64), timeout=True)
        samples = np.concatenate([b.samples for b in blocks], axis=0)
        t_first = [int(b.t_ns) for b in blocks]
        self.last_member_t_ns = t_first
        self.last_skew_ns = max(t_first) - min(t_first)
        overflow = self._misaligned or any(b.overflow for b in blocks)
        self._misaligned = False
        if any(b.settling for b in blocks):
            # union of member settling windows and the group's own window
            self._settle_until = max(self._settle_until, self._index + int(samples.shape[1]))
        t_ret = max(int(b.t_end_ns) for b in blocks)
        return RawRead(samples=samples, overflow_before=overflow, t_ns=t_ret)

    def _stop(self) -> None:
        errs: list[str] = []
        for m in self._members:
            try:
                m.stop_stream()
            except EDSError as e:
                errs.append(f"{m.name}: {e}")
        if errs:
            raise StreamError(f"{self.name}: üye akışları durdurulamadı: " + "; ".join(errs))

    def _close(self) -> None:
        errs: list[str] = []
        for m in self._members:
            try:
                m.close()
            except EDSError as e:
                errs.append(f"{m.name}: {e}")
        for pool in self._pools:
            pool.shutdown(wait=True, cancel_futures=True)
        self._closed = True
        if errs:
            raise DeviceError(f"{self.name}: üyeler kapatılamadı: " + "; ".join(errs))

    # ------------------------------------------------------------------ optional features
    @staticmethod
    def _combine(paths: dict[str, str]) -> str:
        vals = set(paths.values())
        return vals.pop() if len(vals) == 1 else "mixed"

    def set_tracking(self, dc: bool | None = None, rf_dc: bool | None = None,
                     quadrature: bool | None = None) -> dict[str, str]:
        """Fan out; a feature's path is the common member path, else ``"mixed"``
        (per-member detail in ``self.last_tracking_paths``)."""
        with self._lock:
            per = {m.name: m.set_tracking(dc=dc, rf_dc=rf_dc, quadrature=quadrature) for m in self._members}
            self.last_tracking_paths = per
            out: dict[str, str] = {}
            for feature in ("dc", "rf_dc", "quadrature"):
                paths = {name: r[feature] for name, r in per.items() if feature in r}
                if paths:
                    out[feature] = self._combine(paths)
            if any(p != "unsupported" for p in out.values()):
                self.mark_settling()
            return out

    def set_ppm_correction(self, ppm: float | Sequence[float]) -> str:  # type: ignore[override]
        """``ppm``: one value for all members or one per member (member order).

        Returns the common mechanism, or ``"mixed"`` (per member: ``last_ppm_paths``).
        ``ppm_hw_members`` holds each member's compensated ppm; ``ppm_hw`` = member 0's.
        """
        if isinstance(ppm, (int, float, np.floating, np.integer)):
            values = [float(ppm)] * len(self._members)
        else:
            values = [float(v) for v in ppm]
            if len(values) != len(self._members):
                raise ConfigError(f"{self.name}: {len(self._members)} üye için {len(values)} ppm değeri verildi")
        with self._lock:
            paths = {m.name: m.set_ppm_correction(v) for m, v in zip(self._members, values)}
            self.last_ppm_paths = paths
            self.ppm_hw_members = [float(m.ppm_hw) for m in self._members]
            self.ppm_hw = self.ppm_hw_members[0]
            self.ppm_hw_via = self._combine(paths)
            if any(p != "none" for p in paths.values()):
                self.mark_settling()
            return self.ppm_hw_via

    def read_sensors(self) -> dict[str, float | str | None]:
        """Members' sensors with keys prefixed ``"<member name>."``. Never raises."""
        out: dict[str, float | str | None] = {}
        for m in self._members:
            try:
                vals = m.read_sensors()
            except Exception as e:
                log.debug("%s: %s sensörleri okunamadı: %s", self.name, m.name, e)
                out[f"{m.name}.error"] = str(e)
                continue
            for k, v in vals.items():
                out[f"{m.name}.{k}"] = v
        return out

    def describe(self) -> dict[str, Any]:
        d = super().describe()
        d["members"] = [m.describe() for m in self._members]
        d["last_skew_ns"] = self.last_skew_ns
        d["ppm_hw_members"] = list(self.ppm_hw_members)
        return d

    @property
    def worker_threads(self) -> list[threading.Thread]:
        """Live worker threads of this group (diagnostics / tests)."""
        prefixes = tuple(f"{self._thread_tag}-{m.name}_" for m in self._members)
        return [t for t in threading.enumerate() if t.name.startswith(prefixes)]
