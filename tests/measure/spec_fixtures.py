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
* :class:`SimLoSource` — a Pluto-TX-like simulated generator with its own LO (LO leakage and
  TX IQ image emitters, ``set_lo_offset``); :class:`DelayedSimSource` — a generator whose on/off
  switching reaches the RF only ``delay_s`` after the call (libiio TX queue);
  :func:`replace_source` installs either in a context.
* :func:`without_quadrature_control` — a DUT whose quadrature tracking cannot be switched
  (Pluto without the thin libiio layer).

Other test modules may import these helpers read-only.
"""

from __future__ import annotations

import json
import math
import queue
import threading
import time
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from eds.core.config import AppConfig, CalConfig, DeviceConfig, DspConfig, MeasureConfig, RxConfig, SimConfig
from eds.core.errors import ConfigError
from eds.hal.sim.source import SimSignalSource
from eds.hal.sim.world import Emitter, SimWorld
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


def input_p1db_dbm(dev: Any, gain_db: float) -> float:
    """Simulator ground truth of the input 1 dB compression point (``inf`` without the model)."""
    fn = getattr(dev, "input_p1db_dbm", None)
    return float(fn(gain_db)) if callable(fn) else math.inf


# --------------------------------------------------------------------------- stimulus doubles
def replace_source(ctx: MeasurementContext, factory: Any) -> Any:
    """Close the context's stimulus, then install ``factory(ctx.world)`` (returned). The old
    source is closed first: a new simulated source may reuse its emitter name."""
    if ctx.source is not None:
        ctx.source.set_enabled(False)
        ctx.source.close()
    ctx.source = factory(ctx.world)
    return ctx.source


class SimLoSource(SimSignalSource):
    """Pluto-TX-like simulated generator: the tone ``f`` is synthesised ``lo_offset`` above an LO,
    so the LO leakage (``leak_dbc``) sits at ``f − lo_offset`` and the TX IQ image
    (``image_dbc``) at ``f − 2·lo_offset`` — both follow the tone's level and on/off state.
    ``set_lo_offset`` moves the LO (|offset| ≤ 0.4·``tx_fs``, else :class:`ConfigError`) and
    ``describe()`` reports ``lo_hz`` / ``tones_hz`` / ``tone_offset_hz`` like ``SoapyTxSource``."""

    max_tones = 1

    def __init__(self, world: SimWorld, *, name: str = "sim_src", lo_offset_hz: float = 199951.171875,
                 leak_dbc: float = -40.0, image_dbc: float = -40.0, tx_fs: float = 1e6, **kw: Any):
        self.tone_offset_hz = float(lo_offset_hz)
        self.leak_dbc, self.image_dbc, self.tx_fs = float(leak_dbc), float(image_dbc), float(tx_fs)
        self.lo_offsets_used: list[float] = []
        super().__init__(world, name=name, **kw)
        for e in self._artifacts():
            world.add_emitter(e)

    def _artifacts(self) -> list[Emitter]:
        f, off = self._freq, self.tone_offset_hz
        p = self._power - self._path_loss
        return [Emitter(name=f"{self.name}_lo", freq_hz=f - off, power_dbm=p + self.leak_dbc, kind="cw", cabled=True,
                        enabled=self._enabled),
                Emitter(name=f"{self.name}_img", freq_hz=f - 2.0 * off, power_dbm=p + self.image_dbc, kind="cw",
                        cabled=True, enabled=self._enabled)]

    def _sync(self) -> None:
        if not self._closed:
            self.world.replace_emitters(remove=[e.name for e in self._artifacts()], add=self._artifacts())

    def set_frequency(self, hz: float) -> float:
        out = super().set_frequency(hz)
        self._sync()
        return out

    def set_power(self, dbm: float) -> float:
        out = super().set_power(dbm)
        self._sync()
        return out

    def set_enabled(self, on: bool) -> int:
        self._check_open()
        self._enabled = bool(on)
        return self.world.set_enabled_many(self.emitter_names, self._enabled)

    def set_tones(self, freqs_hz: list[float], powers: list[float]) -> None:
        raise ConfigError("SimLoSource: yalnız tek ton")

    def set_lo_offset(self, hz: float) -> float:
        hz = float(hz)
        if hz == 0.0 or abs(hz) > 0.4 * self.tx_fs:
            raise ConfigError(f"LO ofseti {hz:g} Hz TX bandı dışında")
        self.tone_offset_hz = hz
        self.lo_offsets_used.append(hz)
        self._sync()
        return hz

    @property
    def emitter_names(self) -> list[str]:
        return [self.name, f"{self.name}_lo", f"{self.name}_img"]

    def describe(self) -> dict[str, Any]:
        d = super().describe()
        d.update({"lo_hz": self._freq - self.tone_offset_hz, "tones_hz": [self._freq],
                  "tone_offset_hz": self.tone_offset_hz})
        return d

    def close(self) -> None:
        if not self._closed:
            self.world.replace_emitters(remove=[f"{self.name}_lo", f"{self.name}_img"])
        super().close()


class DelayedSimSource(SimSignalSource):
    """Simulated generator whose on/off switching reaches the RF ``delay_s`` after the call
    (in call order), like the Pluto TX whose queued libiio buffers still play. ``settle_s`` is
    the settle time the source *reports* to the measurement helpers."""

    def __init__(self, world: SimWorld, *, delay_s: float, settle_s: float, **kw: Any):
        super().__init__(world, **kw)
        self.delay_s, self.settle_s = float(delay_s), float(settle_s)
        self._q: queue.Queue = queue.Queue()
        self._worker = threading.Thread(target=self._run, daemon=True)
        self._worker.start()

    def _run(self) -> None:
        while True:
            item = self._q.get()
            if item is None:
                return
            due, on = item
            time.sleep(max(0.0, due - time.perf_counter()))
            if not self._closed:
                SimSignalSource.set_enabled(self, on)

    def set_enabled(self, on: bool) -> int:
        self._check_open()
        self._q.put((time.perf_counter() + self.delay_s, bool(on)))
        return time.time_ns()

    def close(self) -> None:
        self._q.put(None)
        self._worker.join(timeout=5.0)
        super().close()


def without_quadrature_control(dev: Any) -> None:
    """Make ``dev`` behave like a Pluto without the thin libiio layer: quadrature tracking
    requests are ignored and reported as ``"unsupported"`` (BB/RF DC tracking still apply)."""
    orig = dev.set_tracking

    def set_tracking(dc: bool | None = None, rf_dc: bool | None = None, quadrature: bool | None = None):
        out = dict(orig(dc=dc, rf_dc=rf_dc)) if (dc is not None or rf_dc is not None) else {}
        if quadrature is not None:
            out["quadrature"] = "unsupported"
        return out

    dev.set_tracking = set_tracking
