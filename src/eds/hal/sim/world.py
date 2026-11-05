"""Shared simulated RF scene: emitters seen by every simulated receiver.

:class:`SimWorld` is the "air" (or the cable bench). All :class:`~eds.hal.sim.device.SimRxDevice`
instances opened from one configuration share one world, so two simulated Plutos receive
the *same* physical waveforms (same emitter phases, same noise-jammer realizations) and
DF / coherence / delay-estimation code can be validated end to end.

Time model
----------
Every mutation of the scene (add / remove / update / on-off switch) is committed as an
immutable :class:`Scene` snapshot stamped with the host monotonic clock
(``time.perf_counter``) and the wall clock (``time.time_ns``). A device synthesizes each
sample with the scene that was active at the sample's *production time* (see the device
docstring). Hence an emitter switched on at host time ``T`` appears exactly from the first
sample produced at or after ``T`` — the ground truth for the end-to-end latency
measurement. History older than :attr:`SimWorld.HISTORY_S` seconds is pruned (the oldest
retained scene then stands in for anything earlier).

Thread safety: every public method takes the world's ``RLock``; measurement threads can
toggle emitters while device threads synthesize.

Emitter kinds (``Emitter.kind``) and their ``params`` (defaults in brackets):

* ``cw``        pure carrier at ``freq_hz``.
* ``noise``     band-limited complex Gaussian noise, flat over ``freq_hz +- bw_hz/2``,
                total power ``power_dbm`` (``bw_hz`` > 0 required).
* ``fm``        sine-modulated FM: ``dev_hz`` [5e3] peak deviation, ``mod_hz`` [1e3].
* ``pulse``     carrier gated by a pulse train: ``pri_s`` [1e-3], ``pw_s`` [1e-4],
                ``t0_s`` [0] (first leading edge, world time). Carrier phase is continuous
                (coherent pulse train), pulse power = ``power_dbm``.
* ``gsm_fcch``  GSM BCCH carrier for PPM calibration: GMSK-like Gaussian noise of
                ``bw_hz`` [200 kHz] at ``power_dbm``; once every ``period_frames`` [10] TDMA
                frames (4.615 ms) a 546.5 us FCCH burst replaces the noise by a pure tone at
                ``freq_hz + 1625e3/24`` (+67.708 kHz) of the same power. ``t0_s`` [0] = world
                time of the first burst.

Every kind accepts ``phase_deg`` (carrier phase at world time 0); without it the phase is
derived deterministically from the world seed and the emitter name.
"""

from __future__ import annotations

import bisect
import dataclasses
import math
import threading
import time
from collections.abc import Iterable
from dataclasses import dataclass, field
from typing import Any

from ...core.config import SimConfig
from ...core.errors import ConfigError
from .signals import GSM_DEFAULT_BW_HZ, unit_from_hash

# Indirections so tests can drive the scene with a fake clock.
_perf = time.perf_counter
_time_ns = time.time_ns

EMITTER_KINDS: tuple[str, ...] = ("cw", "noise", "fm", "pulse", "gsm_fcch")

_KIND_PARAMS: dict[str, dict[str, float]] = {
    "cw": {},
    "noise": {},
    "fm": {"dev_hz": 5e3, "mod_hz": 1e3},
    "pulse": {"pri_s": 1e-3, "pw_s": 1e-4, "t0_s": 0.0},
    "gsm_fcch": {"t0_s": 0.0, "period_frames": 10.0},
}
_COMMON_PARAMS: frozenset[str] = frozenset({"phase_deg"})

#: Ppm of the first device registered in a world (deterministic "typical bad TCXO").
FIRST_DEVICE_PPM: float = 11.7
#: Other devices get a name-hashed ppm in [-HASHED_PPM_SPAN, +HASHED_PPM_SPAN].
HASHED_PPM_SPAN: float = 15.0


def _finite(x: Any, what: str) -> float:
    if isinstance(x, bool) or not isinstance(x, (int, float)) or not math.isfinite(float(x)):
        raise ConfigError(f"{what}: sonlu bir sayı bekleniyordu, {x!r} geldi")
    return float(x)


@dataclass
class Emitter:
    """One RF emitter in the simulated scene.

    ``power_dbm`` is the power *at the receiver input connector* (path loss, antenna gain
    already included). ``aoa_deg`` is the azimuth of arrival measured from +y toward +x
    (see :class:`SimWorld`); ``cabled`` emitters (signal generator on a cable) have no
    geometric phase. ``params`` is merged with the per-kind defaults on construction.
    """

    name: str
    freq_hz: float
    power_dbm: float
    kind: str = "cw"
    bw_hz: float = 0.0
    aoa_deg: float = 0.0
    cabled: bool = False
    enabled: bool = True
    params: dict = field(default_factory=dict)

    def __post_init__(self) -> None:
        if not isinstance(self.name, str) or not self.name:
            raise ConfigError(f"Emitter adı boş olmayan bir metin olmalı (geldi: {self.name!r})")
        where = f"emitter '{self.name}'"
        if self.kind not in EMITTER_KINDS:
            raise ConfigError(f"{where}: bilinmeyen tür {self.kind!r}; seçenekler: {list(EMITTER_KINDS)}")
        self.freq_hz = _finite(self.freq_hz, f"{where}.freq_hz")
        if self.freq_hz <= 0.0:
            raise ConfigError(f"{where}: freq_hz pozitif olmalı (geldi: {self.freq_hz})")
        self.power_dbm = _finite(self.power_dbm, f"{where}.power_dbm")
        self.bw_hz = _finite(self.bw_hz, f"{where}.bw_hz")
        if self.bw_hz < 0.0:
            raise ConfigError(f"{where}: bw_hz negatif olamaz")
        self.aoa_deg = _finite(self.aoa_deg, f"{where}.aoa_deg")
        if not isinstance(self.cabled, bool) or not isinstance(self.enabled, bool):
            raise ConfigError(f"{where}: cabled/enabled true/false olmalı")
        if not isinstance(self.params, dict):
            raise ConfigError(f"{where}: params bir tablo olmalı")
        allowed = _KIND_PARAMS[self.kind]
        unknown = set(self.params) - set(allowed) - _COMMON_PARAMS
        if unknown:
            raise ConfigError(
                f"{where}: '{self.kind}' için bilinmeyen parametre(ler) {sorted(unknown)}; "
                f"geçerli: {sorted(set(allowed) | _COMMON_PARAMS)}"
            )
        merged: dict[str, float] = dict(allowed)
        for k, v in self.params.items():
            merged[k] = _finite(v, f"{where}.params.{k}")
        self.params = merged
        if self.kind == "noise" and self.bw_hz <= 0.0:
            raise ConfigError(f"{where}: 'noise' türü için bw_hz > 0 gerekli")
        if self.kind == "gsm_fcch" and self.bw_hz == 0.0:
            self.bw_hz = GSM_DEFAULT_BW_HZ
        if self.kind == "fm":
            if merged["mod_hz"] <= 0.0 or merged["dev_hz"] < 0.0:
                raise ConfigError(f"{where}: FM için mod_hz > 0 ve dev_hz >= 0 olmalı")
        if self.kind == "pulse":
            pri, pw = merged["pri_s"], merged["pw_s"]
            if not (pri > 0.0 and 0.0 < pw <= pri):
                raise ConfigError(f"{where}: darbe için 0 < pw_s <= pri_s olmalı (pri_s={pri}, pw_s={pw})")
        if self.kind == "gsm_fcch":
            pf = merged["period_frames"]
            if pf < 1.0 or pf != int(pf):
                raise ConfigError(f"{where}: period_frames >= 1 tamsayı olmalı")

    def copy(self) -> Emitter:
        return dataclasses.replace(self, params=dict(self.params))

    def to_dict(self) -> dict[str, Any]:
        d = dataclasses.asdict(self)
        d["params"] = dict(self.params)
        return d


@dataclass(frozen=True)
class Scene:
    """Immutable snapshot of the emitter set, valid from host time ``t_mono`` onward."""

    t_mono: float
    t_ns: int
    version: int
    emitters: tuple[Emitter, ...]


class SimWorld:
    """Thread-safe shared RF scene (see module docstring).

    Geometry: positions are metres in a local x/y plane; an emitter with azimuth ``aoa``
    lies in direction ``u = (sin(aoa), cos(aoa))`` (``aoa`` from +y toward +x). The carrier
    phase at a receiver at ``p`` is advanced by ``2 pi f / c * dot(p, u)``.
    """

    HISTORY_S: float = 120.0
    HISTORY_MAX: int = 4096

    def __init__(self, emitters: Iterable[Emitter] = (), temperature_c: float = 35.0, seed: int = 1234):
        self._lock = threading.RLock()
        self._emitters: dict[str, Emitter] = {}
        for e in emitters:
            if not isinstance(e, Emitter):
                raise ConfigError(f"SimWorld: Emitter bekleniyordu, {type(e).__name__} geldi")
            if e.name in self._emitters:
                raise ConfigError(f"SimWorld: emitter adı tekrarlanmış: '{e.name}'")
            self._emitters[e.name] = e.copy()
        self._temperature_c = _finite(temperature_c, "sim.temperature_c")
        if isinstance(seed, bool) or not isinstance(seed, int) or seed < 0:
            raise ConfigError(f"sim.seed negatif olmayan bir tamsayı olmalı (geldi: {seed!r})")
        self._seed = int(seed)
        self._version = 0
        self._history: list[Scene] = [Scene(-math.inf, 0, 0, self._frozen_emitters())]
        self._switch_log: list[dict[str, Any]] = []
        self._devices: dict[str, int] = {}
        self._clocks: dict[str, float] = {}

    # ------------------------------------------------------------------ construction
    @classmethod
    def from_config(cls, cfg: SimConfig) -> SimWorld:
        """Build from the ``[sim]`` section; each ``cfg.emitters`` dict -> ``Emitter(**d)``."""
        emitters: list[Emitter] = []
        for i, d in enumerate(cfg.emitters):
            if not isinstance(d, dict):
                raise ConfigError(f"sim.emitters[{i}]: tablo bekleniyordu, {d!r} geldi")
            names = {f.name for f in dataclasses.fields(Emitter)}
            unknown = set(d) - names
            if unknown:
                raise ConfigError(f"sim.emitters[{i}]: bilinmeyen alan(lar) {sorted(unknown)}; geçerli: {sorted(names)}")
            missing = {"name", "freq_hz", "power_dbm"} - set(d)
            if missing:
                raise ConfigError(f"sim.emitters[{i}]: eksik alan(lar) {sorted(missing)}")
            emitters.append(Emitter(**d))
        return cls(emitters, temperature_c=cfg.temperature_c, seed=cfg.seed)

    # ------------------------------------------------------------------ properties
    @property
    def emitters(self) -> list[Emitter]:
        """Copies of the current emitters (mutating them has no effect; use :meth:`update`)."""
        with self._lock:
            return [e.copy() for e in self._emitters.values()]

    @property
    def temperature_c(self) -> float:
        with self._lock:
            return self._temperature_c

    @temperature_c.setter
    def temperature_c(self, value: float) -> None:
        v = _finite(value, "sim.temperature_c")
        with self._lock:
            self._temperature_c = v

    @property
    def seed(self) -> int:
        return self._seed

    @property
    def version(self) -> int:
        """Increments on every committed scene change."""
        with self._lock:
            return self._version

    # ------------------------------------------------------------------ mutation
    def add_emitter(self, e: Emitter) -> Emitter:
        """Add (or replace the emitter with the same name). Returns a copy of the stored one."""
        if not isinstance(e, Emitter):
            raise ConfigError(f"add_emitter: Emitter bekleniyordu, {type(e).__name__} geldi")
        with self._lock:
            self._emitters[e.name] = e.copy()
            self._commit()
            return self._emitters[e.name].copy()

    def remove_emitter(self, name: str, missing_ok: bool = False) -> None:
        """Remove an emitter. Unknown name -> :class:`ConfigError` unless ``missing_ok``."""
        with self._lock:
            if name not in self._emitters:
                if missing_ok:
                    return
                raise ConfigError(f"Simülasyonda '{name}' adlı emitter yok (mevcut: {sorted(self._emitters)})")
            del self._emitters[name]
            self._commit()

    def replace_emitters(self, remove: Iterable[str] = (), add: Iterable[Emitter] = ()) -> None:
        """Atomically remove ``remove`` (missing names ignored) and add ``add`` — one scene change."""
        add = list(add)
        for e in add:
            if not isinstance(e, Emitter):
                raise ConfigError(f"replace_emitters: Emitter bekleniyordu, {type(e).__name__} geldi")
        with self._lock:
            for n in remove:
                self._emitters.pop(n, None)
            for e in add:
                self._emitters[e.name] = e.copy()
            self._commit()

    def get(self, name: str) -> Emitter | None:
        with self._lock:
            e = self._emitters.get(name)
            return e.copy() if e is not None else None

    def set_enabled(self, name: str, on: bool) -> int:
        """Switch one emitter on/off. Returns host ``time.time_ns()`` of the switch."""
        return self.set_enabled_many([name], on)

    def set_enabled_many(self, names: Iterable[str], on: bool) -> int:
        """Switch several emitters in ONE scene change. Returns ``time.time_ns()`` of it."""
        names = list(names)
        with self._lock:
            missing = [n for n in names if n not in self._emitters]
            if missing:
                raise ConfigError(f"Simülasyonda olmayan emitter(lar): {missing} (mevcut: {sorted(self._emitters)})")
            for n in names:
                self._emitters[n] = dataclasses.replace(self._emitters[n], enabled=bool(on))
            t_mono, t_ns = self._commit()
            for n in names:
                self._switch_log.append({"name": n, "on": bool(on), "t_mono": t_mono, "t_ns": t_ns})
            if len(self._switch_log) > self.HISTORY_MAX:
                del self._switch_log[: len(self._switch_log) - self.HISTORY_MAX]
            return t_ns

    def update(self, name: str, **fields: Any) -> Emitter:
        """Change fields of an emitter (validated). ``name`` cannot be changed."""
        valid = {f.name for f in dataclasses.fields(Emitter)} - {"name"}
        unknown = set(fields) - valid
        if unknown:
            raise ConfigError(f"update('{name}'): bilinmeyen alan(lar) {sorted(unknown)}; geçerli: {sorted(valid)}")
        with self._lock:
            if name not in self._emitters:
                raise ConfigError(f"Simülasyonda '{name}' adlı emitter yok (mevcut: {sorted(self._emitters)})")
            cur = self._emitters[name]
            if "params" in fields:
                if not isinstance(fields["params"], dict):
                    raise ConfigError(f"update('{name}'): params bir tablo olmalı")
                fields = dict(fields)
                same_kind = fields.get("kind", cur.kind) == cur.kind
                # partial params update the current ones; a kind change starts from its defaults
                fields["params"] = {**cur.params, **fields["params"]} if same_kind else dict(fields["params"])
            elif "kind" in fields and fields["kind"] != cur.kind:
                fields = dict(fields)
                fields["params"] = {}
            new = dataclasses.replace(cur, **fields)
            self._emitters[name] = new
            if "enabled" in fields and bool(fields["enabled"]) != cur.enabled:
                t_mono, t_ns = self._commit()
                self._switch_log.append({"name": name, "on": new.enabled, "t_mono": t_mono, "t_ns": t_ns})
            else:
                self._commit()
            return new.copy()

    def switch_log(self) -> list[dict[str, Any]]:
        """Recorded on/off switches ``{name, on, t_mono, t_ns}`` (ground truth for latency)."""
        with self._lock:
            return [dict(s) for s in self._switch_log]

    # ------------------------------------------------------------------ device registry
    def register_device(self, name: str, shared_clock: str = "", ppm: float | None = None) -> float:
        """Register a receiver and return the TCXO ppm it must use.

        * Default ppm: :data:`FIRST_DEVICE_PPM` (+11.7) for the first device registered in
          this world, otherwise a name-hashed value in +-:data:`HASHED_PPM_SPAN`.
        * ``shared_clock`` group: every member uses the ppm of the group's first member
          (two Plutos on one 40 MHz reference). An explicit, different ppm for a later
          member is a :class:`ConfigError`.
        """
        with self._lock:
            if name not in self._devices:
                self._devices[name] = len(self._devices)
            order = self._devices[name]
            if ppm is None:
                own = FIRST_DEVICE_PPM if order == 0 else (2.0 * unit_from_hash(f"ppm/{name}") - 1.0) * HASHED_PPM_SPAN
            else:
                own = _finite(ppm, f"device '{name}': sim.ppm")
            if shared_clock:
                if shared_clock in self._clocks:
                    grp = self._clocks[shared_clock]
                    if ppm is not None and abs(own - grp) > 1e-12:
                        raise ConfigError(
                            f"device '{name}': shared_clock '{shared_clock}' grubunun ppm değeri {grp:+.4f}; "
                            f"farklı ppm ({own:+.4f}) verilemez (ortak referans = ortak ppm)"
                        )
                    return grp
                self._clocks[shared_clock] = own
            return own

    def clock_ppm(self, shared_clock: str) -> float | None:
        with self._lock:
            return self._clocks.get(shared_clock)

    @property
    def device_names(self) -> list[str]:
        with self._lock:
            return sorted(self._devices, key=self._devices.__getitem__)

    # ------------------------------------------------------------------ timeline (device side)
    def scenes_between(self, t_lo: float, t_hi: float) -> list[Scene]:
        """Scenes active during host-time interval ``[t_lo, t_hi]``, oldest first.

        The first element is the scene active at ``t_lo`` (or the oldest retained one).
        """
        with self._lock:
            h = self._history
            i0 = max(bisect.bisect_right(h, t_lo, key=lambda s: s.t_mono) - 1, 0)
            i1 = bisect.bisect_right(h, t_hi, key=lambda s: s.t_mono)
            return h[i0:max(i1, i0 + 1)]

    def current_scene(self) -> Scene:
        with self._lock:
            return self._history[-1]

    # ------------------------------------------------------------------ internals
    def _frozen_emitters(self) -> tuple[Emitter, ...]:
        return tuple(e.copy() for e in self._emitters.values())

    def _commit(self) -> tuple[float, int]:
        t_mono, t_ns = _perf(), _time_ns()
        self._version += 1
        last = self._history[-1].t_mono
        if t_mono < last:          # monotonic guard (never happens with perf_counter)
            t_mono = last
        self._history.append(Scene(t_mono, t_ns, self._version, self._frozen_emitters()))
        self._prune(t_mono)
        return t_mono, t_ns

    def _prune(self, now: float) -> None:
        h = self._history
        cut = 0
        horizon = now - self.HISTORY_S
        while cut + 1 < len(h) and h[cut + 1].t_mono < horizon:
            cut += 1
        if len(h) - cut > self.HISTORY_MAX:
            cut = len(h) - self.HISTORY_MAX
        if cut:
            del h[:cut]

    def describe(self) -> dict[str, Any]:
        with self._lock:
            return {
                "seed": self._seed, "temperature_c": self._temperature_c, "version": self._version,
                "emitters": [e.to_dict() for e in self._emitters.values()],
                "devices": self.device_names, "clocks": dict(self._clocks),
            }
