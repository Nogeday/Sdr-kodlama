"""Measurement framework — the system is its own test instrument.

Each KTR quantitative spec is a :class:`Measurement` subclass registered under a stable
``spec_id``. A measurement returns a :class:`MeasurementResult` that carries:

* the headline number (``value`` + ``unit``) and a Turkish one-line ``summary``,
* the exact ``conditions`` (fc, fs, RBW, gain, nfft, averages, source, temperature, ...)
  — a number without conditions is worthless to the jury,
* ``method`` (how it was measured, TR) and optional ``uncertainty``,
* ``target`` comparison (from ``config/ktr_targets.toml``) → ``passed``,
* ``table`` rows (raw sweep points) and declarative ``figures`` (:class:`FigureSpec`) that
  ``eds.measure.plots`` renders to PNG — measurement code never imports matplotlib.

Spec IDs (stable; used in CLI, targets file, reports, bus messages):

    sensitivity, dynamic_range, inst_bw, scan_speed, freq_resolution,
    df_rms, image_rejection, max_rate, latency
"""

from __future__ import annotations

import logging
import traceback
from abc import ABC, abstractmethod
from collections.abc import Callable
from contextlib import contextmanager
from dataclasses import asdict, dataclass, field
from typing import Any, ClassVar

import numpy as np

from ..core.config import AppConfig
from ..core.errors import MeasurementError
from ..core.timeutil import mono, now_ns

log = logging.getLogger(__name__)

SPEC_IDS: tuple[str, ...] = (
    "sensitivity", "dynamic_range", "inst_bw", "scan_speed", "freq_resolution",
    "df_rms", "image_rejection", "max_rate", "latency",
)


@dataclass
class Series:
    """One line/marker series of a figure."""

    x: list[float]
    y: list[float]
    label: str = ""
    style: str = "line"            # line | marker | step | bar
    yerr: list[float] | None = None
    role: str = ""                 # "" | pass | fail | warn | ref → fixed colour (eds.measure.plots.ROLE_COLORS)


@dataclass
class FigureSpec:
    """Declarative plot. ``kind``:

    * ``"xy"``        generic line/marker plot
    * ``"spectrum"``  spectrum-analyzer style "screenshot" (dark theme, info box with
                      fc/span/RBW/ref level, peak markers) — the jury-facing ekran görüntüsü
    * ``"hist"``      histogram/CDF of ``series[0].y``
    * ``"polar"``     bearing plots (x = angle deg, y = value)
    """

    name: str                      # file stem, e.g. "sensitivity_pd_curve"
    title: str
    kind: str = "xy"
    series: list[Series] = field(default_factory=list)
    xlabel: str = ""
    ylabel: str = ""
    hlines: list[tuple[float, str]] = field(default_factory=list)     # (y, label)
    vlines: list[tuple[float, str]] = field(default_factory=list)     # (x, label)
    markers: list[tuple[float, float, str]] = field(default_factory=list)  # (x, y, text)
    info: dict[str, str] = field(default_factory=dict)                # spectrum info box
    logx: bool = False
    logy: bool = False
    ylim: tuple[float, float] | None = None
    xlim: tuple[float, float] | None = None
    # (x, label) tick positions + labels of a categorical x axis (bar plots); empty = automatic
    xticks: list[tuple[float, str]] = field(default_factory=list)


@dataclass
class MeasurementResult:
    spec_id: str
    title: str
    value: float | None
    unit: str
    summary: str = ""
    method: str = ""
    conditions: dict[str, Any] = field(default_factory=dict)
    extra: dict[str, Any] = field(default_factory=dict)        # secondary metrics
    uncertainty: float | None = None
    target: float | None = None
    target_op: str = ""                                        # "<=" | ">="
    passed: bool | None = None
    table: list[dict[str, Any]] = field(default_factory=list)
    figures: list[FigureSpec] = field(default_factory=list)
    status: str = "ok"                                         # ok | skipped | failed
    error: str = ""
    device: dict[str, Any] = field(default_factory=dict)
    t_start_ns: int = 0
    duration_s: float = 0.0
    artifacts: list[str] = field(default_factory=list)         # files written by the report
    # Reasons (TR) why ``value`` must NOT be compared with the KTR target even though it is a
    # number (e.g. "koherens yok", "gerçek zamanlı olmayan simülatör", "kalibrasyonsuz").
    # ``eds.measure.targets.apply_targets`` gives no verdict when this is non-empty.
    blockers: list[str] = field(default_factory=list)

    def evaluate_target(self, target: float | None, op: str) -> None:
        self.target, self.target_op = target, op
        if target is None or self.value is None or not np.isfinite(self.value):
            self.passed = None
        elif op == "<=":
            self.passed = bool(self.value <= target)
        elif op == ">=":
            self.passed = bool(self.value >= target)
        else:
            raise ValueError(f"target_op must be '<=' or '>=' (got {op!r})")

    def to_dict(self, with_figures: bool = True) -> dict[str, Any]:
        d = asdict(self)
        if not with_figures:
            d.pop("figures", None)
        return _jsonable(d)

    def bus_payload(self) -> dict[str, Any]:
        """Compact form for the ``meas.result`` bus topic."""
        return _jsonable({
            "spec_id": self.spec_id, "title": self.title, "value": self.value, "unit": self.unit,
            "summary": self.summary, "status": self.status, "passed": self.passed,
            "target": self.target, "target_op": self.target_op, "uncertainty": self.uncertainty,
            "conditions": self.conditions, "extra": self.extra, "t_start_ns": self.t_start_ns,
            "duration_s": self.duration_s, "device": self.device.get("info", {}).get("name", "")
            or self.device.get("name", ""),
        } | ({"extra": {**self.extra, "blockers": list(self.blockers)}} if self.blockers else {}))


def _jsonable(o: Any) -> Any:
    """Recursively convert numpy scalars/arrays and non-finite floats for JSON."""
    if isinstance(o, dict):
        return {str(k): _jsonable(v) for k, v in o.items()}
    if isinstance(o, (list, tuple)):
        return [_jsonable(v) for v in o]
    if isinstance(o, np.ndarray):
        return [_jsonable(v) for v in o.tolist()]
    if isinstance(o, (np.floating, float)):
        f = float(o)
        return f if np.isfinite(f) else None
    if isinstance(o, np.integer):
        return int(o)
    if isinstance(o, np.bool_):
        return bool(o)
    return o


jsonable = _jsonable  # public alias (report/bus serialisation)


@dataclass
class MeasurementContext:
    """Everything a measurement may use. Unused fields may be None.

    devices[0] is the device under test (DUT). ``df_rms`` uses ``devices`` (≥2 channels via
    ``eds.hal.group``) or a coherent multi-channel device. ``source`` is the stimulus.
    ``prompt`` asks the operator to do something physical (returns when done); in
    automated/sim runs it is a no-op that only logs.
    """

    app: AppConfig
    devices: list[Any]                                    # list[RxDevice]
    source: Any = None                                    # SignalSource | None
    world: Any = None                                     # SimWorld when running on simulator
    cal: Any = None                                       # eds.calib.store.DeviceCalibration | None
    out_dir: str = ""
    prompt: Callable[[str], None] = field(default=lambda msg: log.info("[OPERATÖR] %s", msg))
    params: dict[str, Any] = field(default_factory=dict)  # per-spec overrides from config/CLI
    quick: bool = False                                   # fewer points/shorter durations (CI)

    @property
    def dut(self):
        return self.devices[0]

    @property
    def is_sim(self) -> bool:
        return self.world is not None

    def _source_prefix(self) -> str | None:
        return getattr(self.source, "name", None) if self.source is not None else None

    @contextmanager
    def terminated_input(self):
        """RX input terminated in 50 Ω (noise-floor measurements).

        Simulator: every emitter (scene *and* source) is disabled for the duration and
        restored afterwards. Hardware: the operator is prompted to fit / remove the load.
        """
        if self.world is not None:
            saved = {e.name: e.enabled for e in self.world.emitters}
            for name in saved:
                self.world.set_enabled(name, False)
            try:
                yield
            finally:
                for name, on in saved.items():
                    if self.world.get(name) is not None:
                        self.world.set_enabled(name, on)
        else:
            self.prompt("DUT RX girişini anten/kablodan ayırıp 50 Ω sonlandırıcı takın, ardından Enter.")
            try:
                yield
            finally:
                self.prompt("50 Ω sonlandırıcıyı çıkarıp ölçüm kablosunu/anteni yeniden bağlayın, ardından Enter.")

    @contextmanager
    def isolated_source(self):
        """Only the stimulus reaches the RX (cabled bench setup).

        Simulator: scene emitters that do not belong to the source are disabled and
        restored afterwards. Hardware: the operator is asked to cable the source to the
        RX input (through the attenuator) — over-the-air signals are then negligible.
        """
        if self.world is not None:
            pref = self._source_prefix()
            saved = {e.name: e.enabled for e in self.world.emitters
                     if not (pref and (e.name == pref or e.name.startswith(pref + "_")))}
            for name in saved:
                self.world.set_enabled(name, False)
            try:
                yield
            finally:
                for name, on in saved.items():
                    if self.world.get(name) is not None:
                        self.world.set_enabled(name, on)
        else:
            self.prompt("Sinyal kaynağını zayıflatıcı üzerinden DUT RX girişine kablo ile bağlayın, ardından Enter.")
            yield

    def param(self, spec_id: str, key: str, default: Any = None) -> Any:
        p = self.params.get(spec_id, {})
        return p.get(key, default) if isinstance(p, dict) else default


class Measurement(ABC):
    """Base class. Subclasses set the ClassVars and implement :meth:`run`."""

    spec_id: ClassVar[str]
    title: ClassVar[str]               # Turkish display name, e.g. "Duyarlılık (MDS)"
    unit: ClassVar[str]
    requires: ClassVar[frozenset[str]] = frozenset()   # {"source", "two_channels"}
    default_params: ClassVar[dict[str, Any]] = {}

    def __init__(self, ctx: MeasurementContext, **params: Any):
        self.ctx = ctx
        p = dict(self.default_params)
        p.update(ctx.app.measure.params.get(self.spec_id, {}))
        p.update(ctx.params.get(self.spec_id, {}) if isinstance(ctx.params.get(self.spec_id), dict) else {})
        p.update(params)
        self.p = p

    def check_requirements(self) -> str | None:
        """Return a reason string if this measurement cannot run in ``ctx``."""
        if "source" in self.requires and self.ctx.source is None:
            return "Sinyal kaynağı gerekli (measure.source = sim | pluto_tx | scpi | manual)"
        if "two_channels" in self.requires:
            chans = sum(getattr(d, "num_channels", 1) for d in self.ctx.devices)
            if chans < 2:
                return "En az 2 RX kanalı gerekli (2× Pluto veya koherent çok kanallı cihaz)"
        return None

    def new_result(self, value: float | None = None, **kw: Any) -> MeasurementResult:
        dev = self.ctx.dut
        return MeasurementResult(
            spec_id=self.spec_id, title=self.title, value=value, unit=self.unit,
            device=dev.describe() if hasattr(dev, "describe") else {}, **kw,
        )

    @abstractmethod
    def run(self) -> MeasurementResult: ...


MEASUREMENTS: dict[str, type[Measurement]] = {}


def register(cls: type[Measurement]) -> type[Measurement]:
    if cls.spec_id in MEASUREMENTS:
        raise ValueError(f"duplicate spec_id {cls.spec_id}")
    MEASUREMENTS[cls.spec_id] = cls
    return cls


def run_measurement(spec_id: str, ctx: MeasurementContext, **params: Any) -> MeasurementResult:
    """Run one measurement, never raising: failures become ``status='failed'`` results."""
    from . import specs  # noqa: F401  (populate registry)

    cls = MEASUREMENTS.get(spec_id)
    if cls is None:
        raise MeasurementError(f"Bilinmeyen ölçüm: {spec_id} (mevcut: {sorted(MEASUREMENTS)})")
    m = cls(ctx, **params)
    t0_ns, t0 = now_ns(), mono()
    reason = m.check_requirements()
    if reason:
        res = m.new_result(None, status="skipped", error=reason, summary=f"Atlandı: {reason}")
    else:
        try:
            res = m.run()
        except Exception as e:  # noqa: BLE001 - report every failure
            log.exception("measurement %s failed", spec_id)
            res = m.new_result(None, status="failed", error=f"{type(e).__name__}: {e}",
                               summary=f"Başarısız: {e}")
            res.extra["traceback"] = traceback.format_exc(limit=8)
    res.t_start_ns, res.duration_s = t0_ns, mono() - t0
    # every result records how it was obtained (jury report: quick / simulator banners)
    res.conditions.setdefault("quick", bool(ctx.quick))
    res.conditions.setdefault("simulated", bool(ctx.is_sim))
    return res
