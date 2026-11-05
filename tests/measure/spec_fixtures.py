"""Shared helpers for the receiver KTR spec tests (sensitivity, dynamic_range, inst_bw,
image_rejection, freq_resolution) on the simulator.

* :func:`sim_app` — one simulated Pluto (non-real-time, quiet scene, ``sim`` source, private
  calibration/report directories), deterministic (fixed world seed).
* :func:`sim_context` — ``build_context`` … ``close_context`` as a context manager, so a test
  can read the simulator's ground truth (``SimRxDevice``) and touch ``ctx.cal`` around a run.
* :func:`run_spec` — run one measurement in a fresh context, returning the result, the device
  and whether the stimulus was left enabled.
* :func:`check_contract` — the jury-facing result contract every spec must satisfy.
* :func:`thermal_n_rbw_dbfs`, :func:`adc_noise_dbfs` — level-equation ground truth.

Other test modules may import these helpers read-only.
"""

from __future__ import annotations

import json
import math
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from eds.core.config import AppConfig, CalConfig, DeviceConfig, DspConfig, MeasureConfig, RxConfig, SimConfig
from eds.measure import plots
from eds.measure.base import MeasurementContext, MeasurementResult, run_measurement
from eds.measure.session import build_context, close_context

#: AD9363 12-bit quantization noise (I+Q, uniform rounding, full scale = 2048 counts), dBFS.
QUANT_NOISE_DBFS: float = 10.0 * math.log10(2.0 / (12.0 * 2048.0 ** 2))

BASE_CONDITION_KEYS: tuple[str, ...] = ("backend", "serial", "profile", "fc_hz", "fs_hz", "bw_hz", "gain_db",
                                        "gain_mode", "ppm_hw", "temperature_c")


def sim_app(tmp_path: Path, *, fc: float = 915e6, fs: float = 2e6, source: str = "sim",
            sim: dict[str, Any] | None = None, params: dict[str, dict[str, Any]] | None = None,
            emitters: list[dict[str, Any]] | None = None, buffer_size: int = 16384, seed: int = 20261105,
            dsp: DspConfig | None = None) -> AppConfig:
    """Single non-real-time simulated device, quiet scene unless ``emitters`` is given."""
    sim_kw: dict[str, Any] = {"realtime": False}
    sim_kw.update(sim or {})
    return AppConfig(
        devices=[DeviceConfig(name="rx0", backend="sim", sim=sim_kw)],
        rx=RxConfig(center_freq_hz=fc, sample_rate_hz=fs, bandwidth_hz=fs, gain_db=40.0, buffer_size=buffer_size,
                    block_size=buffer_size),
        dsp=dsp or DspConfig(),
        cal=CalConfig(dir=str(tmp_path / "cal")),
        measure=MeasureConfig(source=source, out_dir=str(tmp_path / "reports"), params=dict(params or {})),
        sim=SimConfig(emitters=list(emitters or []), seed=seed),
    )


@contextmanager
def sim_context(app: AppConfig, *, quick: bool = True) -> Iterator[MeasurementContext]:
    ctx = build_context(app, quick=quick)
    try:
        yield ctx
    finally:
        close_context(ctx)


def source_enabled(ctx: MeasurementContext) -> bool:
    """True if any emitter of the stimulus source is still enabled in the simulated world."""
    src = ctx.source
    if src is None or ctx.world is None:
        return False
    names = set(getattr(src, "emitter_names", [getattr(src, "name", "")]))
    return any(e.enabled for e in ctx.world.emitters if e.name in names)


@dataclass
class SpecRun:
    result: MeasurementResult
    dev: Any                   # SimRxDevice (closed; ground-truth accessors still work)
    source_left_on: bool


def run_spec(app: AppConfig, spec_id: str, *, quick: bool = True) -> SpecRun:
    with sim_context(app, quick=quick) as ctx:
        res = run_measurement(spec_id, ctx)
        return SpecRun(result=res, dev=ctx.dut, source_left_on=source_enabled(ctx))


def check_contract(res: MeasurementResult, *, unit: str | None = None, min_spectrum_figs: int = 1,
                   min_xy_figs: int = 1, render_dir: Path | None = None) -> None:
    """Every spec result: ok status, finite value, Turkish summary/method, base conditions,
    raw table, at least one spectrum screenshot + one characteristic curve, JSON-safe, and
    (when ``render_dir`` is given and matplotlib is available) figures that render."""
    assert res.status == "ok", (res.error, res.extra.get("traceback"))
    assert res.value is not None and math.isfinite(res.value)
    if unit is not None:
        assert res.unit == unit
    assert res.summary and res.method and len(res.method) > 80
    missing = [k for k in BASE_CONDITION_KEYS if k not in res.conditions]
    assert not missing, missing
    assert res.table, "ham tablo boş"
    kinds = [f.kind for f in res.figures]
    assert kinds.count("spectrum") >= min_spectrum_figs, kinds
    assert kinds.count("xy") >= min_xy_figs, kinds
    assert len({f.name for f in res.figures}) == len(res.figures), "şekil adları benzersiz olmalı"
    json.dumps(res.to_dict(), allow_nan=False)
    if render_dir is not None and plots.available():
        for fig in res.figures:
            out = plots.render(fig, render_dir / f"{fig.name}.png")
            assert out.stat().st_size > 10_000


def db_sum(*levels_db: float) -> float:
    """Power sum of dB levels."""
    return 10.0 * math.log10(sum(10.0 ** (v / 10.0) for v in levels_db))


def adc_noise_dbfs(dev: Any) -> float:
    """White ADC floor over fs: transition noise + 12-bit rounding noise (dBFS)."""
    return db_sum(dev.adc_noise_dbfs, QUANT_NOISE_DBFS)


def thermal_n_rbw_dbfs(dev: Any, gain_db: float, rbw_hz: float) -> float:
    """In-band thermal noise power in one RBW at the ADC (level equation)."""
    return dev.noise_density_dbfs_hz(gain_db) + 10.0 * math.log10(rbw_hz)


def n_rbw_dbfs(dev: Any, gain_db: float, rbw_hz: float, fs: float) -> float:
    """Total noise in one RBW: thermal ⊕ ADC floor."""
    return db_sum(thermal_n_rbw_dbfs(dev, gain_db, rbw_hz), adc_noise_dbfs(dev) - 10.0 * math.log10(fs / rbw_hz))
