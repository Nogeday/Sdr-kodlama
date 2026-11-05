"""Shared helpers for the scan_speed / max_rate / df_rms measurement tests (simulator).

Builds :class:`AppConfig` objects for one or two simulated Plutos, runs one measurement in
a fresh session context (``build_context`` → ``run_measurement`` → ``close_context``) and
checks the jury-facing result contract (summary, method, conditions, figures render).
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from eds.core.config import (
    AppConfig,
    CalConfig,
    DeviceConfig,
    DspConfig,
    MeasureConfig,
    RxConfig,
    SimConfig,
    SweepConfig,
)
from eds.measure import plots
from eds.measure.base import MeasurementResult, run_measurement
from eds.measure.session import build_context, close_context


def single_sim_app(tmp_path: Path, *, realtime: bool, spec_id: str, params: dict[str, Any] | None = None,
                   sim: dict[str, Any] | None = None, rx: RxConfig | None = None,
                   sweep: SweepConfig | None = None, emitters: list[dict[str, Any]] | None = None,
                   seed: int = 20261105) -> AppConfig:
    """One simulated Pluto (quiet scene unless ``emitters``), sim stimulus source."""
    dev_sim = {"realtime": realtime, "ppm": 11.7}
    dev_sim.update(sim or {})
    return AppConfig(
        devices=[DeviceConfig(name="rx0", backend="sim", sim=dev_sim)],
        rx=rx or RxConfig(center_freq_hz=915e6, sample_rate_hz=4e6, bandwidth_hz=4e6, gain_db=40.0),
        dsp=DspConfig(),
        sweep=sweep or SweepConfig(),
        cal=CalConfig(dir=str(tmp_path / "cal")),
        measure=MeasureConfig(source="sim", out_dir=str(tmp_path / "reports"),
                              params={spec_id: dict(params)} if params else {}),
        sim=SimConfig(emitters=list(emitters or []), seed=seed),
    )


def dual_sim_app(tmp_path: Path, *, shared_clock: bool = True, realtime: bool = False, time_offset: int = 0,
                 baseline_m: float = 0.15, fc: float = 1e9, params: dict[str, Any] | None = None,
                 emitters: list[dict[str, Any]] | None = None, seed: int = 4242) -> AppConfig:
    """Two simulated Plutos (rx0 at the origin, rx1 at +x ``baseline_m``) for DF."""
    clock = {"shared_clock": "ref40"} if shared_clock else {}
    rx0 = {"position_m": [0.0, 0.0], "realtime": realtime, "serial": "SIM-A", "ppm": 11.7, **clock}
    rx1 = {"position_m": [baseline_m, 0.0], "realtime": realtime, "serial": "SIM-B",
           "time_offset_samples": time_offset, **clock}
    if not shared_clock:
        rx1["ppm"] = -6.3
    df_params = {"baseline_m": baseline_m, "fc": fc}
    df_params.update(params or {})
    return AppConfig(
        devices=[DeviceConfig(name="rx0", backend="sim", sim=rx0), DeviceConfig(name="rx1", backend="sim", sim=rx1)],
        rx=RxConfig(center_freq_hz=fc, sample_rate_hz=2e6, bandwidth_hz=2e6, gain_db=40.0),
        cal=CalConfig(dir=str(tmp_path / "cal")),
        measure=MeasureConfig(source="sim", out_dir=str(tmp_path / "reports"),
                              source_args={"cabled": False, "aoa_deg": 0.0},
                              params={"df_rms": df_params}),
        sim=SimConfig(emitters=list(emitters or []), seed=seed),
    )


def run_spec(app: AppConfig, spec_id: str, *, quick: bool = True, **params: Any) -> tuple[MeasurementResult, Any]:
    """Run one measurement in a fresh context; returns ``(result, closed context)``."""
    ctx = build_context(app, quick=quick)
    try:
        return run_measurement(spec_id, ctx, **params), ctx
    finally:
        close_context(ctx)


def assert_result_contract(res: MeasurementResult, tmp_path: Path) -> list[Path]:
    """Common jury-facing contract; renders every figure (when matplotlib exists)."""
    assert res.status == "ok", (res.error, res.extra.get("traceback"))
    assert res.summary and res.method and res.conditions and res.table
    assert any(f.kind == "spectrum" for f in res.figures)
    assert all(k in res.conditions for k in ("backend", "serial", "profile"))
    res.to_dict()                                        # JSON-safe
    out: list[Path] = []
    if plots.available():
        for f in res.figures:
            out.append(plots.render(f, tmp_path / "fig" / f.name))
    return out
