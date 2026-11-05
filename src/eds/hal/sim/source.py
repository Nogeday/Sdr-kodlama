"""Simulated signal generator: injects CW tone(s) into a :class:`SimWorld`.

Used by the measurement suite on the simulator exactly like a lab generator
(``measure.source = "sim"``). Power semantics follow :class:`~eds.hal.tx.SignalSource`
with ``absolute_power = True``: ``set_power(dbm)`` is the generator *output* setting and
the power at the DUT input connector is ``setting - path_loss_db`` (cable/attenuators).

The generator powers up with RF OFF (like a real instrument); call ``set_enabled(True)``.
Single-tone mode drives one emitter named ``name``; :meth:`set_tones` switches to
multi-tone mode with emitters ``name_0 .. name_{k-1}``. :meth:`set_frequency` /
:meth:`set_power` always (re)configure single-tone mode.
"""

from __future__ import annotations

import math
from typing import Any

from ...core.errors import ConfigError, DeviceError
from ..tx import SignalSource
from .world import Emitter, SimWorld


class SimSignalSource(SignalSource):
    absolute_power = True
    max_tones = 4
    power_range_db = (-150.0, 10.0)
    freq_range_hz = (1e3, 6e9)

    def __init__(self, world: SimWorld, name: str = "sim_src", aoa_deg: float = 0.0, cabled: bool = True,
                 path_loss_db: float = 0.0, freq_hz: float = 1e9, power_dbm: float = -60.0):
        if not isinstance(world, SimWorld):
            raise ConfigError(f"SimSignalSource: SimWorld bekleniyordu, {type(world).__name__} geldi")
        if not isinstance(name, str) or not name:
            raise ConfigError("SimSignalSource: ad boş olamaz")
        self.world = world
        self.name = name
        self._aoa = self._num(aoa_deg, "aoa_deg")
        if not isinstance(cabled, bool):
            raise ConfigError("SimSignalSource: cabled true/false olmalı")
        self._cabled = cabled
        self._path_loss = self._num(path_loss_db, "path_loss_db")
        self._freq = self._check_freq(freq_hz)
        self._power = self._check_power(power_dbm)
        self._tones: list[tuple[float, float]] | None = None
        self._enabled = False
        self._closed = False
        self.world.add_emitter(self._make(self.name, self._freq, self._power))

    # ------------------------------------------------------------------ validation helpers
    @staticmethod
    def _num(v: Any, what: str) -> float:
        if isinstance(v, bool) or not isinstance(v, (int, float)) or not math.isfinite(float(v)):
            raise ConfigError(f"SimSignalSource.{what}: sonlu sayı bekleniyordu, {v!r} geldi")
        return float(v)

    def _check_freq(self, hz: Any) -> float:
        f = self._num(hz, "freq_hz")
        lo, hi = self.freq_range_hz
        if not lo <= f <= hi:
            raise ConfigError(f"{self.name}: frekans {f:.6g} Hz kaynak aralığı dışında [{lo:.3g}, {hi:.3g}] Hz")
        return f

    def _check_power(self, dbm: Any) -> float:
        p = self._num(dbm, "power_dbm")
        lo, hi = self.power_range_db
        if not lo <= p <= hi:
            raise ConfigError(f"{self.name}: güç {p:g} dBm kaynak aralığı dışında [{lo:g}, {hi:g}] dBm")
        return p

    def _check_open(self) -> None:
        if self._closed:
            raise DeviceError(f"{self.name}: simülasyon kaynağı kapatıldı")

    def _make(self, ename: str, f: float, p: float) -> Emitter:
        return Emitter(name=ename, freq_hz=f, power_dbm=p - self._path_loss, kind="cw",
                       aoa_deg=self._aoa, cabled=self._cabled, enabled=self._enabled)

    def _names(self) -> list[str]:
        if self._tones is None:
            return [self.name]
        return [f"{self.name}_{i}" for i in range(len(self._tones))]

    def _to_single(self) -> None:
        if self._tones is not None:
            old = self._names()
            self._tones = None
            self.world.replace_emitters(remove=old, add=[self._make(self.name, self._freq, self._power)])

    # ------------------------------------------------------------------ SignalSource API
    def set_frequency(self, hz: float) -> float:
        self._check_open()
        self._freq = self._check_freq(hz)
        if self._tones is not None:
            self._to_single()
        else:
            self.world.update(self.name, freq_hz=self._freq)
        return self._freq

    def set_power(self, dbm: float) -> float:
        self._check_open()
        self._power = self._check_power(dbm)
        if self._tones is not None:
            self._to_single()
        else:
            self.world.update(self.name, power_dbm=self._power - self._path_loss)
        return self._power

    def set_enabled(self, on: bool) -> int:
        self._check_open()
        self._enabled = bool(on)
        return self.world.set_enabled_many(self._names(), self._enabled)

    def set_tones(self, freqs_hz: list[float], powers: list[float]) -> None:
        self._check_open()
        freqs, pows = list(freqs_hz), list(powers)
        if len(freqs) != len(pows):
            raise ConfigError(f"{self.name}: frekans ({len(freqs)}) ve güç ({len(pows)}) listeleri eşit uzunlukta olmalı")
        if not 1 <= len(freqs) <= self.max_tones:
            raise ConfigError(f"{self.name}: ton sayısı 1..{self.max_tones} olmalı (geldi: {len(freqs)})")
        tones = [(self._check_freq(f), self._check_power(p)) for f, p in zip(freqs, pows)]
        old = self._names()
        self._tones = tones
        add = [self._make(f"{self.name}_{i}", f, p) for i, (f, p) in enumerate(tones)]
        self.world.replace_emitters(remove=old, add=add)

    def set_aoa(self, deg: float) -> float:
        self._check_open()
        self._aoa = self._num(deg, "aoa_deg")
        for n in self._names():
            self.world.update(n, aoa_deg=self._aoa)
        return self._aoa

    @property
    def path_loss_db(self) -> float:
        return self._path_loss

    @property
    def emitter_names(self) -> list[str]:
        return self._names()

    def describe(self) -> dict[str, Any]:
        d = super().describe()
        d.update({
            "kind": "sim", "freq_hz": self._freq, "power_dbm": self._power, "path_loss_db": self._path_loss,
            "enabled": self._enabled, "aoa_deg": self._aoa, "cabled": self._cabled,
            "tones": [{"freq_hz": f, "power_dbm": p} for f, p in self._tones] if self._tones else [],
            "emitters": self._names(), "closed": self._closed,
        })
        return d

    def close(self) -> None:
        if self._closed:
            return
        self.world.replace_emitters(remove=self._names())
        self._closed = True

    def __exit__(self, *exc):
        if self._closed:
            return
        super().__exit__(*exc)
