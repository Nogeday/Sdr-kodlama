"""Measurement session: build a :class:`MeasurementContext` from an :class:`AppConfig`,
run a list of spec measurements with isolation between them, evaluate KTR targets,
write the report and (optionally) publish ``meas.result`` on the bus.

Used by ``eds measure`` and by tests/demos::

    app = load_config("config/rx_sim.toml")
    suite = run_suite(app, ["sensitivity", "max_rate"], quick=True)
    print(suite.report_dir)
"""

from __future__ import annotations

import logging
from collections.abc import Callable, Iterable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from ..core.config import AppConfig
from ..core.errors import ConfigError, MeasurementError
from ..core.paths import cal_dir, reports_dir
from .base import SPEC_IDS, MeasurementContext, MeasurementResult, run_measurement

log = logging.getLogger(__name__)


def _load_calibration(app: AppConfig, serial: str):
    try:
        from ..calib.store import CalibrationStore
    except ImportError:  # pragma: no cover - calib package always present in a checkout
        return None
    store = CalibrationStore(app.cal.dir or str(cal_dir()))
    return store.load(serial) if serial else None


def build_context(
    app: AppConfig,
    *,
    devices: list[Any] | None = None,
    quick: bool = False,
    source_kind: str | None = None,
    source_args: dict[str, Any] | None = None,
    out_dir: str | Path | None = None,
    prompt: Callable[[str], None] | None = None,
    params: dict[str, Any] | None = None,
    configure: bool = True,
) -> MeasurementContext:
    """Open devices (unless given), the stimulus source and the calibration of the DUT."""
    from ..hal import get_sim_world, open_devices
    from ..hal.tx import open_source

    devs = list(devices) if devices is not None else open_devices(app)
    if not devs:
        raise ConfigError("Ölçüm için en az bir cihaz gerekli")
    try:
        if configure:
            for d in devs:
                d.configure(app.rx)
        uses_sim = any(getattr(d, "backend_name", "") == "sim" for d in devs)
        world = get_sim_world(app) if uses_sim else None
        kind = source_kind or app.measure.source
        if kind == "sim" and world is None:
            raise ConfigError("measure.source = 'sim' yalnızca sim backend ile kullanılabilir")
        source = open_source(kind, source_args if source_args is not None else app.measure.source_args,
                             world=world)
        if source is not None:
            source.set_enabled(False)
        cal = _load_calibration(app, getattr(devs[0].info, "serial", ""))
        out = Path(out_dir or app.measure.out_dir or reports_dir())
        ctx = MeasurementContext(app=app, devices=devs, source=source, world=world, cal=cal,
                                 out_dir=str(out), quick=quick, params=dict(params or {}))
        if prompt is not None:
            ctx.prompt = prompt
        return ctx
    except Exception:
        if devices is None:
            for d in devs:
                d.close()
        raise


def close_context(ctx: MeasurementContext, close_devices: bool = True) -> None:
    if ctx.source is not None:
        try:
            ctx.source.set_enabled(False)
        finally:
            ctx.source.close()
    if close_devices:
        for d in ctx.devices:
            d.close()


def reset_baseline(ctx: MeasurementContext) -> None:
    """Return every device to the configured baseline (``app.rx``) and silence the source.

    Called before each measurement so that one measurement's settings (sample rate sweep,
    gain changes, tracking off) never leak into the next one.
    """
    if ctx.source is not None:
        ctx.source.set_enabled(False)
    for d in ctx.devices:
        d.stop_stream()
        d.configure(ctx.app.rx)


@dataclass
class SuiteResult:
    results: list[MeasurementResult] = field(default_factory=list)
    report_dir: Path | None = None

    @property
    def ok(self) -> bool:
        return all(r.status != "failed" for r in self.results)


def resolve_spec_ids(spec_ids: Iterable[str] | None) -> list[str]:
    ids = list(spec_ids or ["all"])
    if "all" in ids:
        return list(SPEC_IDS)
    unknown = [s for s in ids if s not in SPEC_IDS]
    if unknown:
        raise MeasurementError(f"Bilinmeyen ölçüm(ler): {unknown} — geçerli: {list(SPEC_IDS)}")
    return ids


def run_suite(
    app: AppConfig,
    spec_ids: Iterable[str] | None = None,
    *,
    ctx: MeasurementContext | None = None,
    write: bool = True,
    publish: bool = False,
    title: str | None = None,
    on_result: Callable[[MeasurementResult], None] | None = None,
    **build_kw: Any,
) -> SuiteResult:
    """Run measurements in order, isolated from each other; returns results + report dir."""
    ids = resolve_spec_ids(spec_ids)
    own_ctx = ctx is None
    if ctx is None:
        ctx = build_context(app, **build_kw)
    publisher = None
    suite = SuiteResult()
    try:
        if publish:
            from ..bus.pubsub import publisher_from_config

            publisher = publisher_from_config(app.bus, src="measure")
        try:
            from .targets import apply_targets, load_targets

            targets = load_targets(app.measure.targets_file)
        except Exception as e:  # noqa: BLE001 - targets are optional
            log.warning("KTR hedefleri yüklenemedi (%s); 'Hedef' sütunu boş kalacak", e)
            targets, apply_targets = {}, None
        for sid in ids:
            reset_baseline(ctx)
            log.info("ölçüm başlıyor: %s", sid)
            res = run_measurement(sid, ctx)
            if apply_targets is not None:
                apply_targets([res], targets)
            suite.results.append(res)
            if publisher is not None:
                from ..bus.topics import MEAS_RESULT

                publisher.publish(MEAS_RESULT, res.bus_payload())
            if on_result is not None:
                on_result(res)
            log.info("ölçüm bitti: %s -> %s %s (%s)", sid, res.value, res.unit, res.status)
        if write:
            from .report import write_report

            kw = {"title": title} if title else {}
            suite.report_dir = write_report(suite.results, ctx.out_dir, app=app, **kw)
    finally:
        if publisher is not None:
            publisher.close()
        if own_ctx:
            close_context(ctx)
    return suite
