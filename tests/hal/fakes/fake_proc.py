"""Fakes for ``eds.hal.proc`` tests. They live in an importable module because the spawned
worker process imports them by name (``"tests.hal.fakes.fake_proc:open_fake"``).

``FakeRawDevice`` is driven by ``cfg.sim`` (plain data, crosses the process boundary):

* ``script``: list of read events consumed in order by ``_read_raw``; keys ``k`` (samples),
  ``overflow``, ``timeout``, ``hw_time_ns``, ``t_ns``, ``sleep`` (s), ``error`` (class name in
  ``eds.core.errors`` or ``"KeyError"``), ``exit`` (``os._exit`` code), ``uncertain``
  (``RawRead.uncertain_samples``; ``-1`` = the device's ``settle_samples``, like a Pluto DMA
  overflow), ``dma`` (count one ``dma_overflow_events``), ``ovf_source`` (set
  ``overflow_source`` before returning, e.g. detection switched off mid-stream). After the
  script: a ramp (sample value = absolute index) in chunks of at most ``chunk`` samples.
* ``pluto_like``: the device carries the SoapyRxDevice-like attributes ``overflow_source``
  (``"dma_status"``), ``dma_overflow_events``, ``thin_serial_verified``, ``stream_mtu`` (set
  by ``_start`` to ``mtu``) and its ``settle_samples`` follows ``max(buffer_size, stream_mtu)``
  (``stream:bufflen``); ``round_buffer``: ``_start`` rounds ``buffer_size`` up to a multiple
  of it (a backend that changes the parent-owned buffer size).
* ``fixed``: return slices of one preallocated buffer (transport throughput, no synthesis).
* ``crash_fc`` / ``fail_fc`` / ``hang_bw``: retune kills the worker / raises DeviceError,
  ``set_bandwidth(hang_bw)`` sleeps 30 s.
* ``open_error`` / ``open_violation``: the opener raises; ``open_wrong_type``: it returns a
  non-``RxDevice``.
* ``close_error``: ``_close`` raises DeviceError.

:func:`open_fake_pluto` opens a REAL :class:`~eds.hal.soapy_backend.SoapyRxDevice` on the
SoapySDR / pylibiio fakes (installed into ``sys.modules`` of the calling process), its board's
RX DMA status register wired to the driver's silent overflows; ``cfg.sim["rx_script"]``
preloads the fake driver's ``readStream`` script.
"""

from __future__ import annotations

import os
import sys
import time
from typing import Any

import numpy as np

from eds.core import errors as E
from eds.core.config import AppConfig, DeviceConfig
from eds.core.profile import load_profile
from eds.hal.base import RawRead, RxDevice
from eds.hal.tx import SignalSource


class FakeRawDevice(RxDevice):
    backend_name = "fake"

    def __init__(self, cfg: DeviceConfig):
        super().__init__(cfg, load_profile(cfg.profile))
        self.p: dict[str, Any] = dict(cfg.sim)
        self.script: list[dict[str, Any]] = [dict(e) for e in self.p.get("script", [])]
        self.n_next = 0
        self.chunk = int(self.p.get("chunk", 1 << 30))
        self._fixed = np.arange(1 << 17, dtype=np.float32).astype(np.complex64).reshape(1, -1) \
            if self.p.get("fixed") else None
        self.info.driver = "fake"
        self.info.serial = f"FAKE-{cfg.name}"
        self.info.uri = f"fake://{cfg.name}"
        self.info.extra = {"pid": str(os.getpid())}
        self.ops = 0
        self.started_with = 0
        if self.p.get("pluto_like"):
            self.overflow_source = "dma_status"
            self.dma_overflow_events = 0
            self.thin_serial_verified = True
            self.stream_mtu = 0

    @property
    def settle_samples(self) -> int:
        return int(self.settle_buffers * max(int(self.buffer_size), int(getattr(self, "stream_mtu", 0) or 0)))

    # ------------------------------------------------------------------ primitives
    def _apply_sample_rate(self, hz: float) -> float:
        self.ops += 1
        return float(round(hz))

    def _apply_bandwidth(self, hz: float) -> float:
        self.ops += 1
        if hz == self.p.get("hang_bw"):
            time.sleep(30.0)
        self.warnings.append(f"BW {hz:.0f} uyarısı")
        return float(hz)

    def _apply_center_freq(self, hz: float) -> float:
        self.ops += 1
        if hz == self.p.get("crash_fc"):
            os._exit(3)
        if hz == self.p.get("fail_fc"):
            raise E.DeviceError(f"{self.name}: PLL kilitlenmedi ({hz:.0f} Hz)")
        return float(hz)

    def _apply_gain(self, db: float, channel: int) -> float:
        self.ops += 1
        return float(db)

    def _apply_gain_mode(self, mode: str) -> None:
        self.ops += 1

    def _start(self, buffer_size: int) -> None:
        self.started_with = int(buffer_size)
        step = int(self.p.get("round_buffer", 0))
        if step:
            self.buffer_size = -(-int(buffer_size) // step) * step
        if hasattr(self, "stream_mtu"):
            self.stream_mtu = int(self.p.get("mtu", 0))

    def _stop(self) -> None:
        pass

    def _close(self) -> None:
        if self.p.get("close_error"):
            raise E.DeviceError(f"{self.name}: kapatma hatası")

    def _read_raw(self, n: int, timeout_s: float) -> RawRead:
        if self.gain_mode != "manual":
            self.gains[0] = 12.5                      # "hardware AGC" moved the gain
        if self._fixed is not None:
            k = min(int(n), self._fixed.shape[1])
            return RawRead(samples=self._fixed[:, :k], t_ns=time.time_ns())
        ev: dict[str, Any] = self.script.pop(0) if self.script else {}
        if "sleep" in ev:
            time.sleep(float(ev["sleep"]))
        if "exit" in ev:
            os._exit(int(ev["exit"]))
        if "error" in ev:
            name = ev["error"]
            raise KeyError("boom") if name == "KeyError" else getattr(E, name)(f"{self.name}: senaryo hatası {name}")
        if "ovf_source" in ev:
            self.overflow_source = str(ev["ovf_source"])
        if ev.get("dma"):
            self.dma_overflow_events += 1
        t_ns = int(ev.get("t_ns", time.time_ns()))
        if ev.get("timeout"):
            return RawRead(samples=np.zeros((1, 0), np.complex64), timeout=True, t_ns=t_ns)
        k = int(ev["k"]) if "k" in ev else min(int(n), self.chunk)
        x = (self.n_next + np.arange(k, dtype=np.float64)).astype(np.complex64).reshape(1, -1)
        x = x + np.complex64(1j) * np.float32(len(self.script))
        self.n_next += k
        unc = int(ev.get("uncertain", 0))
        return RawRead(samples=x, overflow_before=bool(ev.get("overflow", False)), t_ns=t_ns,
                       hw_time_ns=ev.get("hw_time_ns"), uncertain_samples=self.settle_samples if unc < 0 else unc)

    # ------------------------------------------------------------------ optional features
    def set_tracking(self, dc=None, rf_dc=None, quadrature=None) -> dict[str, str]:
        return {k: "fake" for k, v in (("dc", dc), ("rf_dc", rf_dc), ("quadrature", quadrature)) if v is not None}

    def set_ppm_correction(self, ppm: float) -> str:
        if ppm == 999:
            raise KeyError("boom")
        if abs(ppm) > 100:
            raise E.ConfigError(f"{self.name}: ppm düzeltmesi aralık dışı ({ppm:+.1f})")
        self.ppm_hw, self.ppm_hw_via = float(ppm), "hardware"
        self.info.extra["xo"] = f"{ppm:+.2f}"
        return "hardware"

    def read_sensors(self) -> dict[str, float | str | None]:
        return {"temperature_c": 41.5, "pid": float(os.getpid())}

    def runtime_limits(self) -> dict[str, Any]:
        return {"gain_db": (0.0, 60.0)}

    def describe(self) -> dict[str, Any]:
        d = super().describe()
        d["fake"] = {"ops": self.ops, "started_with": self.started_with}
        return d


def open_fake(cfg: DeviceConfig, app: AppConfig | None = None) -> RxDevice:
    err = cfg.sim.get("open_error")
    if err:
        raise getattr(E, err)(f"{cfg.name}: sahte cihaz bulunamadı")
    viol = cfg.sim.get("open_violation")
    if viol:
        raise E.ProfileViolation(list(viol))
    if cfg.sim.get("open_wrong_type"):
        return object()  # type: ignore[return-value]  # not an RxDevice on purpose
    return FakeRawDevice(cfg)


def open_fake_pluto(cfg: DeviceConfig, app: AppConfig | None = None) -> RxDevice:
    """A real SoapyRxDevice (Pluto) on the fakes; see the module docstring. A test calling it
    in-process must protect ``sys.modules["SoapySDR"]`` / ``["iio"]`` (monkeypatch)."""
    from eds.hal.soapy_backend import SoapyRxDevice
    from tests.hal.fakes import fake_iio, fake_soapy

    board = fake_iio.make_pluto_board(cfg.control_uri)
    sys.modules["iio"] = fake_iio.make_module([board])
    sys.modules["SoapySDR"] = fake_soapy.make_module(overflow_hook=board.dma_overflow, overflow_drop=3000)
    dev = SoapyRxDevice(cfg, load_profile(cfg.profile), overflow_poll_s=0.0)
    dev._dev.rx_script.extend(cfg.sim.get("rx_script", []))
    return dev


# ---------------------------------------------------------------------------- sources
class FakeSource(SignalSource):
    """Records calls; ``close()`` writes ``"closed enabled=<state>"`` to ``marker``."""

    name = "fake_src"
    max_tones = 2

    def __init__(self, marker: str = "", absolute: bool = True, fail_open: bool = False):
        if fail_open:
            raise E.DeviceNotFound("sahte kaynak bulunamadı")
        self.absolute_power = bool(absolute)
        self.power_range_db = (-100.0, -10.0)
        self.freq_range_hz = (70e6, 6e9)
        self.marker = marker
        self.freq: float | None = None
        self.power: float | None = None
        self.enabled = False
        self.lo_offset = 0.0
        self.calls: list[tuple[str, Any]] = []
        self.pid = os.getpid()

    def set_frequency(self, hz: float) -> float:
        self.calls.append(("set_frequency", hz))
        self.freq = float(hz)
        self.freq_range_hz = (float(hz) - 1e6, float(hz) + 1e6)
        return self.freq + 1.0

    def set_power(self, dbm: float) -> float:
        if dbm > self.power_range_db[1]:
            raise E.ConfigError(f"güç {dbm:+.1f} dBm üst sınırın üzerinde")
        self.calls.append(("set_power", dbm))
        self.power = float(dbm)
        return self.power

    def set_enabled(self, on: bool) -> int:
        self.calls.append(("set_enabled", bool(on)))
        self.enabled = bool(on)
        return time.time_ns()

    def set_tones(self, freqs_hz: list[float], powers: list[float]) -> None:
        self.calls.append(("set_tones", (list(freqs_hz), list(powers))))

    def set_lo_offset(self, hz: float) -> float:
        self.calls.append(("set_lo_offset", hz))
        self.lo_offset = float(hz)
        return self.lo_offset

    def history(self) -> list[tuple[str, Any]]:
        return list(self.calls)

    def describe(self) -> dict[str, Any]:
        return {"name": self.name, "absolute_power": self.absolute_power, "freq": self.freq, "pid": self.pid}

    def close(self) -> None:
        if self.marker:
            with open(self.marker, "w", encoding="utf-8") as f:
                f.write(f"closed enabled={self.enabled}")


class FakeSourceNoLo(FakeSource):
    """A source without an internal LO: the base class' ``set_lo_offset`` (NotSupported)."""

    set_lo_offset = SignalSource.set_lo_offset
