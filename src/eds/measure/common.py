"""Shared helpers for spec measurements: bench setup, settled captures, tone power,
absolute level referencing (dBFS → dBm) and spectrum "screenshot" figures.

Level referencing order (:func:`level_reference`):

1. ``ctx.cal.power`` table present → ``offset(f, G)`` from the stored calibration.
2. Absolute stimulus available (sim source, SCPI generator, anchored Pluto TX) →
   *in-situ* reference: inject a tone of known power, ``offset = P_in − P_meas(dBFS)``.
3. Neither → offset 0, results stay in dBFS and are flagged ``calibrated = False``.
"""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from typing import Any

import numpy as np

from ..core.errors import MeasurementError
from ..core.units import fmt_hz
from .base import FigureSpec, MeasurementContext, Series


def setup_rx(dev, *, fc: float | None = None, fs: float | None = None, bw: float | None = None,
             gain: float | None = None, gain_mode: str = "manual",
             tracking: dict[str, bool] | None = None) -> None:
    """Apply RX settings in a safe order (rate → BW → fc → gain mode → gain → tracking)."""
    if fs is not None:
        dev.set_sample_rate(fs)
    if bw is not None:
        dev.set_bandwidth(bw)
    if fc is not None:
        dev.set_center_freq(fc)
    dev.set_gain_mode(gain_mode)
    if gain is not None and gain_mode == "manual":
        dev.set_gain(gain)
    if tracking:
        dev.set_tracking(**tracking)


def welch_len(nfft: int, averages: int, overlap: float = 0.5) -> int:
    """Samples needed for ``averages`` Welch segments of ``nfft`` at ``overlap``."""
    hop = max(1, int(round(nfft * (1.0 - overlap))))
    return int(nfft + hop * (max(1, averages) - 1))


def capture_spectrum(dev, nfft: int, averages: int, window: str = "blackmanharris",
                     overlap: float = 0.5, avg: str = "mean", channel: int = 0):
    """Settled capture → Welch power spectrum (dBFS, power-spectrum scaling)."""
    from ..dsp.psd import spectrum_from_block

    blk = dev.capture(welch_len(nfft, averages, overlap))
    return spectrum_from_block(blk, nfft, window, overlap, avg, channel)


def tone_power_db(spec, f_hz: float, halfwidth_hz: float | None = None) -> float:
    """Total power of a tone (scalloping-free band power over its main lobe)."""
    hw = halfwidth_hz if halfwidth_hz is not None else 5.0 * spec.df_hz
    return float(spec.band_power_db(f_hz - hw, f_hz + hw))


@contextmanager
def tone(ctx: MeasurementContext, f_hz: float, power: float) -> Iterator[Any]:
    """Enable the stimulus at ``f_hz``/``power`` for the duration of the block."""
    src = ctx.source
    if src is None:
        raise MeasurementError("Bu adım sinyal kaynağı gerektiriyor")
    src.set_frequency(f_hz)
    src.set_power(power)
    src.set_enabled(True)
    # samples already queued in the driver/kernel buffers predate the switch
    for d in ctx.devices:
        d.mark_settling()
    try:
        yield src
    finally:
        src.set_enabled(False)
        for d in ctx.devices:
            d.mark_settling()


@dataclass
class LevelRef:
    offset_db: float          # dBm = dBFS + offset_db  (0 when uncalibrated)
    method: str               # human readable (TR)
    calibrated: bool
    unit: str                 # "dBm" | "dBFS"


def level_reference(ctx: MeasurementContext, dev, f_hz: float, gain_db: float, *,
                    nfft: int = 4096, averages: int = 8, probe_offset_hz: float | None = None,
                    probe_dbfs: float = -25.0) -> LevelRef:
    """Find the dBFS→dBm offset at (f_hz, gain_db). See module docstring for the order.

    The in-situ case delegates to :func:`eds.measure.stimulus.reference_level`, which
    *searches* the probe tone within the TCXO tolerance (±25 ppm = ±23 kHz at 915 MHz —
    far more than a few bins at narrow RBW) and subtracts the noise contribution.
    """
    cal = ctx.cal
    power = getattr(cal, "power", None) if cal is not None else None
    if power is not None:
        return LevelRef(float(power.offset_db(f_hz, gain_db)), "kalibrasyon tablosu", True, "dBm")
    src = ctx.source
    if src is not None and getattr(src, "absolute_power", False):
        from .stimulus import reference_level

        frac = (probe_offset_hz / dev.fs) if (probe_offset_hz is not None and dev.fs) else 0.125
        return reference_level(ctx, dev, f_hz, nfft=nfft, averages=averages, window=ctx.app.dsp.window,
                               overlap=ctx.app.dsp.overlap, probe_frac=frac, probe_dbfs=probe_dbfs)
    return LevelRef(0.0, "kalibrasyonsuz (dBFS)", False, "dBFS")


def base_conditions(dev, spec=None, **extra: Any) -> dict[str, Any]:
    """Standard condition block recorded with every result."""
    d: dict[str, Any] = {
        "backend": dev.info.backend, "serial": dev.info.serial, "profile": dev.info.profile,
        "fc_hz": dev.fc, "fs_hz": dev.fs, "bw_hz": dev.bw, "gain_db": list(dev.gains),
        "gain_mode": dev.gain_mode, "ppm_hw": dev.ppm_hw, "ppm_hw_via": dev.ppm_hw_via,
    }
    try:
        temp = dev.read_sensors().get("temperature_c")
        if temp is not None:
            d["temperature_c"] = temp
    except Exception:  # noqa: BLE001 - sensors are best effort
        pass
    if spec is not None:
        d.update({"nfft": spec.nfft, "rbw_hz": spec.rbw_hz, "averages": spec.averages,
                  "window": spec.window})
    d.update(extra)
    return d


def spectrum_screenshot(spec, name: str, title: str, *, offset_db: float = 0.0, unit: str | None = None,
                        markers: list[tuple[float, float, str]] | None = None, max_points: int = 4001,
                        ref_level: float | None = None, extra_info: dict[str, str] | None = None) -> FigureSpec:
    """Spectrum-analyzer style figure (``kind='spectrum'``) from a :class:`Spectrum`.

    The trace is max-hold decimated to ``max_points`` so narrow peaks survive.
    """
    f = np.asarray(spec.f_hz, dtype=float)
    p = np.asarray(spec.p_db, dtype=float) + offset_db
    if f.size > max_points:
        k = int(np.ceil(f.size / max_points))
        m = (f.size // k) * k
        f = f[:m].reshape(-1, k).mean(axis=1)
        p = p[:m].reshape(-1, k).max(axis=1)
    u = unit or spec.unit
    top = float(np.max(p)) if ref_level is None else ref_level
    ref = 10.0 * np.ceil((top + 5.0) / 10.0)
    info = {
        "Center": fmt_hz(spec.fc), "Span": fmt_hz(spec.fs), "RBW": fmt_hz(spec.rbw_hz),
        "Avg": str(spec.averages), "Ref": f"{ref:.0f} {u}", "Window": spec.window,
    }
    if extra_info:
        info.update(extra_info)
    return FigureSpec(
        name=name, title=title, kind="spectrum",
        series=[Series(x=(f / 1e6).tolist(), y=p.tolist(), label=u)],
        xlabel="Frekans [MHz]", ylabel=f"Seviye [{u}]",
        markers=[(m[0] / 1e6, m[1], m[2]) for m in (markers or [])],
        info=info, ylim=(ref - 100.0, ref),
    )
