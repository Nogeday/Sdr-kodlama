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
    Every step is attempted even if an earlier one fails; the first error is then raised
    (``run_suite`` turns it into a ``failed`` result of the next measurement).
    """
    errors: list[Exception] = []
    steps: list[Callable[[], Any]] = []
    if ctx.source is not None:
        steps.append(lambda: ctx.source.set_enabled(False))
    for d in ctx.devices:
        steps.append(lambda d=d: (d.stop_stream(), d.configure(ctx.app.rx)))
    for step in steps:
        try:
            step()
        except Exception as e:  # noqa: BLE001 - attempt every device, report the first error
            errors.append(e)
    if errors:
        raise errors[0]


@dataclass
class SuiteResult:
    results: list[MeasurementResult] = field(default_factory=list)
    report_dir: Path | None = None
    #: Why the suite stopped before its last measurement (empty = ran to the end).
    aborted: str = ""

    @property
    def ok(self) -> bool:
        return not self.aborted and all(r.status != "failed" for r in self.results)


def resolve_spec_ids(spec_ids: Iterable[str] | None) -> list[str]:
    ids = list(spec_ids or ["all"])
    if "all" in ids:
        return list(SPEC_IDS)
    unknown = [s for s in ids if s not in SPEC_IDS]
    if unknown:
        raise MeasurementError(f"Bilinmeyen ölçüm(ler): {unknown} — geçerli: {list(SPEC_IDS)}")
    return ids


#: ``write_report`` header row written when the suite stopped early.
ABORT_META_KEY = "Ölçüm oturumu"


def _placeholder(ctx: MeasurementContext, sid: str, status: str, error: str, summary: str) -> MeasurementResult:
    """Result for a measurement that did not run (or ran into a session-level error)."""
    from . import specs  # noqa: F401  (populate registry)
    from .base import MEASUREMENTS

    cls = MEASUREMENTS.get(sid)
    try:
        dev = ctx.dut.describe() if hasattr(ctx.dut, "describe") else {}
    except Exception:  # noqa: BLE001 - a broken device must not hide the original error
        dev = {}
    res = MeasurementResult(spec_id=sid, title=getattr(cls, "title", sid), value=None,
                            unit=getattr(cls, "unit", ""), status=status, error=error, summary=summary, device=dev)
    res.conditions.setdefault("quick", bool(ctx.quick))
    res.conditions.setdefault("simulated", bool(ctx.is_sim))
    return res


def _run_one(ctx: MeasurementContext, sid: str) -> MeasurementResult:
    """Baseline reset + measurement; never raises for an ordinary error (STR-06): a device
    error while restoring the baseline becomes a ``failed`` result of that measurement
    (it is not run: its conditions would be unknown)."""
    try:
        reset_baseline(ctx)
    except Exception as e:  # noqa: BLE001 - isolate the measurements from each other
        log.exception("ölçüm öncesi temel duruma dönülemedi: %s", sid)
        err = f"{type(e).__name__}: {e}"
        return _placeholder(ctx, sid, "failed", f"Temel duruma (rx ayarları) dönülemedi: {err}",
                            f"Başarısız: ölçüm öncesi cihaz yapılandırılamadı ({err}); ölçüm çalıştırılmadı")
    log.info("ölçüm başlıyor: %s", sid)
    return run_measurement(sid, ctx)


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
    """Run measurements in order, isolated from each other; returns results + report dir.

    Isolation: an error of one measurement, of the baseline reset before it, of the target
    evaluation, of the bus publication or of ``on_result`` never discards the others. If
    the suite itself is interrupted (e.g. Ctrl-C), the measurements already completed are
    still written to a report marked as interrupted (the not-run ones as ``skipped``) and
    the exception is re-raised.
    """
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
            from .targets import VERDICT_NOTE_KEY, apply_targets, load_targets

            targets = load_targets(app.measure.targets_file)
        except Exception as e:  # noqa: BLE001 - targets are optional
            log.warning("KTR hedefleri yüklenemedi (%s); 'Hedef' sütunu boş kalacak", e)
            targets, apply_targets, VERDICT_NOTE_KEY = {}, None, "verdict_note"
        completed = 0
        running = False
        try:
            for sid in ids:
                running = True
                res = _run_one(ctx, sid)
                if apply_targets is not None:
                    try:
                        apply_targets([res], targets)
                    except Exception as e:  # noqa: BLE001
                        log.exception("KTR hedefi değerlendirilemedi: %s", sid)
                        res.passed = None
                        res.extra[VERDICT_NOTE_KEY] = f"Hedef değerlendirilemedi: {type(e).__name__}: {e}"
                suite.results.append(res)
                completed += 1
                running = False
                if publisher is not None:
                    try:
                        from ..bus.topics import MEAS_RESULT

                        publisher.publish(MEAS_RESULT, res.bus_payload())
                    except Exception as e:  # noqa: BLE001 - the bus is optional for a measurement
                        log.warning("meas.result yayınlanamadı (%s): %s", sid, e)
                        res.extra["publish_error"] = f"{type(e).__name__}: {e}"
                if on_result is not None:
                    try:
                        on_result(res)
                    except Exception:  # noqa: BLE001 - a caller's progress hook must not stop the suite
                        log.exception("on_result geri çağrısı hata verdi: %s", sid)
                log.info("ölçüm bitti: %s -> %s %s (%s)", sid, res.value, res.unit, res.status)
        except BaseException as e:
            why = f"{type(e).__name__}: {e}" if str(e) else type(e).__name__
            suite.aborted = why
            rest = ids[len(suite.results):]
            for k, sid in enumerate(rest):
                if k == 0 and running:
                    suite.results.append(_placeholder(ctx, sid, "failed", f"Ölçüm yarıda kesildi ({why})",
                                                      f"Başarısız: ölçüm yarıda kesildi ({why})"))
                else:
                    suite.results.append(_placeholder(ctx, sid, "skipped", f"Oturum yarıda kesildi ({why})",
                                                      "Atlandı: ölçüm oturumu bu ölçümden önce kesildi"))
            log.error("ölçüm oturumu yarıda kesildi (%s); tamamlanan %d/%d ölçüm", why, completed, len(ids))
            if write and completed:
                try:
                    suite.report_dir = _write(suite, ctx, app, title, completed=completed)
                    log.error("tamamlanan ölçümlerin kısmi raporu yazıldı: %s", suite.report_dir)
                except Exception:  # noqa: BLE001 - never mask the interruption
                    log.exception("kısmi rapor yazılamadı")
            raise
        if write:
            suite.report_dir = _write(suite, ctx, app, title)
    finally:
        if publisher is not None:
            publisher.close()
        if own_ctx:
            close_context(ctx)
    return suite


def _write(suite: SuiteResult, ctx: MeasurementContext, app: AppConfig, title: str | None,
           completed: int | None = None) -> Path:
    from .report import write_report

    kw: dict[str, Any] = {"title": title} if title else {}
    if suite.aborted:
        kw["meta"] = {ABORT_META_KEY: f"YARIDA KESİLDİ ({suite.aborted}) — {completed}/{len(suite.results)} ölçüm "
                                      "tamamlandı; kalanlar çalıştırılmadı"}
    return write_report(suite.results, ctx.out_dir, app=app, **kw)
