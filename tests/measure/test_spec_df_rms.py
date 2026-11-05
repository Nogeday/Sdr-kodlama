"""Tests of the DF RMS measurement (eds.measure.specs.df_rms) on two simulated Plutos.

Ground truth: the simulator's geometry (positions [0, 0] / [0.15, 0] m, emitter azimuth =
the true angle), the shared reference clock (coherent) vs independent TCXOs (phase drift
``360 · f · (1/(1+e1) − 1/(1+e0))`` °/s) and the stream offset ``time_offset_samples``
(phase slope ``360 · Δf_bb · offset / fs`` between two baseband frequencies).
"""

from __future__ import annotations

import gc
import math
import re
import threading
import time

import numpy as np
import pytest

from eds.core.timeutil import now_ns
from eds.core.units import wrap_deg
from eds.dsp.windows import window_info
from eds.hal import get_sim_world, open_devices
from eds.hal.sim.world import Emitter
from eds.hal.tx import SignalSource
from eds.measure.base import MeasurementContext, run_measurement
from eds.measure.specs.df_rms import BLOCKER_SIM, NO_COHERENCE_FLAG, SIM_FLAG, AngleResult, locate_tone
from eds.measure.targets import REASON_NO_COHERENCE

from .spec_fixtures_b import assert_result_contract, dual_sim_app, run_spec, single_sim_app

FC, FS = 1e9, 2e6
F_TARGET, F_PILOT = FC + 0.2 * FS, FC + 0.1 * FS


def _group_threads() -> set[str]:
    return {t.name for t in threading.enumerate() if t.name.startswith("eds-group")}


def test_df_rms_shared_clock_quick(tmp_path):
    bg = {"name": "bg", "kind": "cw", "freq_hz": 1.0003e9, "power_dbm": -70.0}
    app = dual_sim_app(tmp_path, emitters=[bg])
    before = _group_threads()
    res, ctx = run_spec(app, "df_rms", quick=True)
    assert_result_contract(res, tmp_path)
    ex = res.extra
    assert res.unit == "°" and res.value < 2.0 and math.isfinite(res.value)
    assert ex["coherent"] is True and ex["coherence"] > 0.99 and abs(ex["phase_drift_deg_per_s"]) < 1.0
    assert NO_COHERENCE_FLAG not in res.summary
    assert abs(ex["bias_deg"]) < 1.0 and ex["max_abs_deg"] < 3.0 and ex["n"] == 7 * 2
    # MM-06: realistic default SNR, always stated WITH its bandwidth (10 dB in 25 kHz ≈ 36 dB in a 61 Hz bin)
    assert ex["recalibrations"] == 0 and ex["snr_bw_hz"] == 25e3 and ex["snr_bw_db"] == pytest.approx(10.0, abs=1.0)
    rbw = window_info("blackmanharris", 65536).enbw_bins * FS / 65536
    assert ex["snr_db_mean"] == pytest.approx(ex["snr_bw_db"] - 10 * math.log10(rbw / 25e3), abs=1e-9)
    assert ex["snr_per_sample_db"] == pytest.approx(ex["snr_bw_db"] + 10 * math.log10(25e3 / FS), abs=1e-9)
    assert f"SNR {ex['snr_bw_db']:.1f} dB / 25 kHz" in res.summary
    assert 0.05 < res.value < 2.0                              # not a sub-millidegree "measurement"
    # only thermal noise is modelled: said first in summary and method, and no target verdict
    assert res.summary.startswith(SIM_FLAG) and "yalnız termal gürültü" in res.method
    assert res.blockers == [BLOCKER_SIM] and "snr_sweep" not in ex           # quick: no SNR sweep
    # geometry of the simulator: d = 0.15 m at the target frequency
    assert ex["baseline_m"] == 0.15 and ex["d_over_lambda"] == pytest.approx(0.15 * F_TARGET / 299_792_458.0)
    assert ex["unambiguous_max_deg"] > 80.0 and ex["ambiguous"] is False
    assert "geometry_warnings" not in ex
    # no stream offset: the phase at the pilot and at the target frequency agree
    assert abs(wrap_deg(ex["pilot_to_target_phase_deg"])) < 0.5
    assert [r["true_deg"] for r in res.table] == [-60.0, -40.0, -20.0, 0.0, 20.0, 40.0, 60.0]
    for r in res.table:
        assert r["n"] == 2 and abs(r["error_deg"]) < 2.0 and r["coherence"] > 0.99
        assert len(r["estimates_deg"]) == 2
    assert res.uncertainty is not None and res.uncertainty >= 0.0
    c = res.conditions
    assert c["f_target_hz"] == F_TARGET and c["f_pilot_hz"] == F_PILOT and c["fs_hz"] == FS
    assert c["rbw_hz"] == pytest.approx(window_info("blackmanharris", 65536).enbw_bins * FS / 65536)
    names = {f.name: f.kind for f in res.figures}
    assert names == {"df_rms_estimated_vs_true": "xy", "df_rms_error_vs_angle": "xy", "df_rms_polar": "polar",
                     "df_rms_spectrum": "spectrum"}
    # the scene is restored (bg back on, private emitters removed) and the group's worker
    # threads are gone; members were not closed by the measurement (session closes them)
    world = get_sim_world(app)
    assert [e.name for e in world.emitters] == ["bg"] and world.get("bg").enabled
    gc.collect()
    deadline = time.monotonic() + 2.0
    while _group_threads() - before and time.monotonic() < deadline:
        time.sleep(0.02)
    assert not (_group_threads() - before)


def test_df_rms_members_stay_open_and_stream_delay_is_calibrated_out(tmp_path):
    """rx1 runs 37 samples ahead: a CW pilot cannot resolve that delay, so a calibration at
    another baseband frequency would be off by 360·Δf_bb·37/fs; calibrating at the target
    frequency keeps the bearings exact."""
    app = dual_sim_app(tmp_path, time_offset=37, params={"angles_deg": [-45.0, 0.0, 30.0], "sim_snr_db": 40.0})
    from eds.measure.session import build_context, close_context

    ctx = build_context(app, quick=True)
    try:
        res = run_measurement("df_rms", ctx)
        for d in ctx.devices:                                # still open and usable
            assert not d.streaming and d.capture(1024).n == 1024
    finally:
        close_context(ctx)
    assert res.status == "ok", (res.error, res.extra.get("traceback"))
    assert res.value < 2.0 and res.extra["coherent"] is True
    expected = wrap_deg(360.0 * (F_TARGET - F_PILOT) / FS * 37)       # = -108°
    assert float(wrap_deg(res.extra["pilot_to_target_phase_deg"] - expected)) == pytest.approx(0.0, abs=1.0)
    assert [r["true_deg"] for r in res.table] == [-45.0, 0.0, 30.0]


def test_df_rms_independent_clocks_flags_non_coherence(tmp_path):
    app = dual_sim_app(tmp_path, shared_clock=False, params={"angles_deg": [-20.0, 20.0]})
    res, ctx = run_spec(app, "df_rms", quick=True)
    assert res.status == "ok", (res.error, res.extra.get("traceback"))
    ex = res.extra
    assert NO_COHERENCE_FLAG in res.summary and ex["coherent"] is False
    assert REASON_NO_COHERENCE in res.blockers
    e0, e1 = (d.true_ppm * 1e-6 for d in ctx.devices)
    drift = 360.0 * F_PILOT * (1.0 / (1.0 + e1) - 1.0 / (1.0 + e0))
    assert ex["phase_drift_deg_per_s"] == pytest.approx(drift, rel=0.01)
    assert ex["coherence"] < 0.5


def test_df_rms_single_device_is_skipped(tmp_path):
    app = single_sim_app(tmp_path, realtime=False, spec_id="df_rms")
    res, _ = run_spec(app, "df_rms", quick=True)
    assert res.status == "skipped" and "2 RX kanalı" in res.error


def test_df_rms_geometry_mismatch_is_reported(tmp_path):
    app = dual_sim_app(tmp_path, params={"baseline_m": 0.10, "angles_deg": [0.0, 30.0], "sim_snr_db": 40.0})
    res, _ = run_spec(app, "df_rms", quick=True)
    assert res.status == "ok"
    assert res.extra["geometry_warnings"] and "UYARI" in res.summary
    # a wrong baseline scales sin(θ̂): the 30° bearing is clearly wrong (0° stays right)
    rows = {r["true_deg"]: r for r in res.table}
    assert abs(rows[0.0]["error_deg"]) < 1.0 and abs(rows[30.0]["error_deg"]) > 5.0


@pytest.mark.realtime
def test_df_rms_realtime_stream_keeps_alignment(tmp_path):
    app = dual_sim_app(tmp_path, realtime=True, params={"angles_deg": [-30.0, 0.0, 30.0]})
    t0 = time.perf_counter()
    res, _ = run_spec(app, "df_rms", quick=True)
    assert time.perf_counter() - t0 < 20.0
    assert res.status == "ok", (res.error, res.extra.get("traceback"))
    assert res.value < 2.0 and res.extra["coherent"] is True and res.extra["recalibrations"] == 0


# ----------------------------------------------------------------------------- hardware path
class BenchOperator(SignalSource):
    """Plays the operator AND the signal generator of the hardware bench on the simulator:
    prompts re-cable (splitter ↔ antennas) and turn the array; the generator drives
    whichever path is connected."""

    name = "bench_operator"

    def __init__(self, world, power_dbm: float = -60.0):
        self.world = world
        world.replace_emitters(add=[
            Emitter(name="gen_cabled", freq_hz=FC, power_dbm=power_dbm, cabled=True, enabled=False),
            Emitter(name="gen_ota", freq_hz=FC, power_dbm=power_dbm, cabled=False, enabled=False),
        ])
        self.path: str | None = None
        self.az = 0.0
        self.freq = FC
        self.on = False
        self.prompts: list[str] = []

    def __call__(self, msg: str) -> None:                     # ctx.prompt
        self.prompts.append(msg)
        if "güç bölücü" in msg:
            self.path = "cabled"
        elif "antenleri" in msg:
            self.path = "ota"
        m = re.search(r"([+-]\d+(?:\.\d+)?)° konumuna", msg)
        if m:
            self.az = float(m.group(1))
        self._apply()

    def _apply(self) -> None:
        self.world.update("gen_cabled", freq_hz=self.freq, enabled=self.on and self.path == "cabled")
        self.world.update("gen_ota", freq_hz=self.freq, aoa_deg=self.az, enabled=self.on and self.path == "ota")

    def set_frequency(self, hz: float) -> float:
        self.freq = float(hz)
        self._apply()
        return self.freq

    def set_power(self, dbm: float) -> float:
        return float(dbm)

    def set_enabled(self, on: bool) -> int:
        self.on = bool(on)
        self._apply()
        return now_ns()


def test_df_rms_hardware_bench_flow_with_operator_prompts(tmp_path):
    app = dual_sim_app(tmp_path, params={"angles_deg": [-40.0, 10.0, 50.0]})
    devs = open_devices(app)
    try:
        for d in devs:
            d.configure(app.rx)
        op = BenchOperator(get_sim_world(app))
        # world=None: the measurement takes the hardware path (prompts + ctx.source)
        ctx = MeasurementContext(app=app, devices=devs, source=op, world=None, prompt=op, quick=True,
                                 out_dir=str(tmp_path))
        res = run_measurement("df_rms", ctx)
    finally:
        for d in devs:
            d.close()
    assert res.status == "ok", (res.error, res.extra.get("traceback"))
    assert res.value < 2.0 and res.extra["coherent"] is True
    assert res.conditions["source"].startswith("donanım")
    rotations = [p for p in op.prompts if "konumuna" in p]
    assert [re.search(r"([+-]\d+\.\d)°", p).group(1) for p in rotations] == ["-40.0", "+10.0", "+50.0"]
    assert sum("güç bölücü" in p for p in op.prompts) == 1 and sum("antenleri" in p for p in op.prompts) == 1
    assert op.on is False                                      # generator left off


# ----------------------------------------------------------------------------- helpers
def test_locate_tone_frequency_and_snr():
    rng = np.random.default_rng(20261105)
    n, fs = 65536, 2e6
    k = 13107                                                   # tone exactly on a bin (+399.99 kHz)
    f0 = k * fs / n
    amp, sigma2 = 1e-2, 1e-8                                   # tone power -40 dBFS, noise -80 dBFS total
    t = np.arange(n)
    noise = (rng.standard_normal(n) + 1j * rng.standard_normal(n)) * math.sqrt(sigma2 / 2.0)
    x = amp * np.exp(2j * np.pi * f0 * t / fs) + noise
    f, snr = locate_tone(x, fs, 0.2 * fs, 30e3)
    assert f == pytest.approx(f0)
    enbw = window_info("blackmanharris", n).enbw_bins
    snr_truth = 10.0 * math.log10(amp ** 2 / (sigma2 * enbw / n))   # SNR in RBW = ENBW·fs/n
    assert snr == pytest.approx(snr_truth, abs=0.5)


def test_df_rms_snr_sweep_follows_thermal_noise_law(tmp_path):
    """Outside --quick the simulator repeats the scan per SNR: with thermal noise only the
    phase error, hence the bearing RMS, scales as 1/√SNR (20 dB → ×10)."""
    app = dual_sim_app(tmp_path, params={"angles_deg": [-30.0, 0.0, 30.0], "repeats": 6,
                                         "snr_sweep_db": [0.0, 20.0]})
    res, _ = run_spec(app, "df_rms", quick=False)
    assert res.status == "ok", (res.error, res.extra.get("traceback"))
    sw = res.extra["snr_sweep"]
    assert [w["snr_target_db"] for w in sw] == [0.0, 20.0]
    for w in sw:
        assert w["snr_bw_db"] == pytest.approx(w["snr_target_db"], abs=1.0) and w["n"] == 18
    assert 4.0 < sw[0]["rms_deg"] / sw[1]["rms_deg"] < 25.0
    assert "RMS–SNR (25 kHz): +0 dB" in res.summary
    fig = next(f for f in res.figures if f.name == "df_rms_vs_snr")
    assert fig.series[0].y == [w["rms_deg"] for w in sw] and fig.vlines[0][0] == 10.0


def test_df_rms_uncertainty_uses_angles_as_independent_units():
    """MM-06: the K repeats at one angle share that angle's bias; the uncertainty of the RMS
    comes from the M per-angle mean squared errors, not from M·K 'independent' samples."""
    from eds.core.config import AppConfig
    from eds.df.interferometer import Bearing
    from eds.measure.specs.df_rms import DfRmsMeasurement

    m = DfRmsMeasurement(MeasurementContext(app=AppConfig(), devices=[]))
    rng = np.random.default_rng(7)
    angles = []
    for true, bias in ((-30.0, 0.0), (0.0, 2.0), (10.0, 0.0), (30.0, 2.0)):
        r = AngleResult(true_deg=true)
        for _ in range(50):
            r.bearings.append(Bearing(az_deg=true + bias + 1e-3 * rng.standard_normal(), phase_deg=0.0,
                                      coherence=1.0, quality=1.0))
            r.snr_db.append(30.0)
        angles.append(r)
    st = m._stats(angles, {"n": 65536, "fs": FS, "snr_bw_hz": 25e3})
    assert st["rms_deg"] == pytest.approx(math.sqrt(2.0), rel=1e-3)
    e2 = np.array([0.0, 4.0, 0.0, 4.0])
    assert st["rms_unc_deg"] == pytest.approx(np.std(e2, ddof=1) / (2 * math.sqrt(2.0) * 2.0), rel=1e-2)   # ≈ 0.41°
