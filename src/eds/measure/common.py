"""Shared helpers for spec measurements: bench setup, settled captures, tone power,
absolute level referencing (dBFS → dBm) and spectrum "screenshot" figures.

Level referencing order (:func:`level_reference`):

1. ``ctx.cal.power`` table present → ``offset(f, G)`` from the stored calibration.
2. Absolute stimulus available (sim source, SCPI generator, anchored Pluto TX) →
   *in-situ* reference: inject a tone of known power, ``offset = P_in − P_meas(dBFS)``.
3. Neither → offset 0, results stay in dBFS and are flagged ``calibrated = False``.

Stimulus timing (:func:`stimulus_changed`): a source call returns before the new RF output
reaches the DUT (Pluto TX: the samples already queued in the 4 libiio TX kernel buffers still
play, ``SoapyTxSource.estimated_queue_latency_s = 4 × buffer_len / fs``, plus SoapyPlutoSDR's
own staging buffer; a lab generator: synthesizer / level settling). Every stimulus change
therefore waits :func:`source_settle_s` (Pluto TX: ``1.25 × estimated_queue_latency_s + 5 ms``)
of wall-clock time and only then flags the RX's stale buffers as settling: the discarded
``settle_samples`` cover the whole RX FIFO, so every later sample was taken after the wait.
"""

from __future__ import annotations

import math
import time
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


#: Added to a source's own queue-latency estimate (USB transfer / host scheduling jitter), s.
SOURCE_SETTLE_MARGIN_S: float = 0.005
#: Settling of a networked lab generator without its own estimate (``*OPC?`` returns when the
#: command is processed; synthesizer / ALC settling after that is typically a few ms), s.
SCPI_SETTLE_S: float = 0.010
#: The Pluto TX estimate (``estimated_queue_latency_s``) counts the 4 libiio kernel buffers;
#: SoapyPlutoSDR's own staging buffer comes on top: (4 + 1) / 4.
QUEUE_LATENCY_FACTOR: float = 1.25


def _num_attr(obj: Any, name: str) -> float | None:
    """Finite, non-negative numeric attribute (``None`` if absent / not a number / unreadable)."""
    try:
        v = getattr(obj, name, None)
    except Exception:  # noqa: BLE001 - remote proxies may fail on attribute access
        return None
    if isinstance(v, bool) or not isinstance(v, (int, float, np.integer, np.floating)):
        return None
    v = float(v)
    return v if math.isfinite(v) and v >= 0.0 else None


def source_settle_s(src: Any) -> float:
    """Wall-clock time from the return of a stimulus call (on/off, frequency, level) until
    the new RF output is at the DUT.

    * ``src.settle_s`` when the source states it (preferred),
    * else ``src.estimated_queue_latency_s`` (Pluto TX: libiio TX queue) ×
      :data:`QUEUE_LATENCY_FACTOR` + :data:`SOURCE_SETTLE_MARGIN_S`,
    * else :data:`SCPI_SETTLE_S` for an SCPI generator, 0 otherwise (simulator: immediate;
      manual source: the operator confirms every step).
    """
    if src is None:
        return 0.0
    v = _num_attr(src, "settle_s")
    if v is not None:
        return v
    q = _num_attr(src, "estimated_queue_latency_s")
    if q is not None:
        return QUEUE_LATENCY_FACTOR * q + SOURCE_SETTLE_MARGIN_S
    if str(getattr(src, "name", "")) == "scpi":
        return SCPI_SETTLE_S
    return 0.0


def stimulus_changed(ctx: MeasurementContext, settle_s: float | None = None) -> float:
    """Call after every stimulus change (on/off, frequency, level, tones): wait until the new
    RF output has reached the DUT (``settle_s``, default :func:`source_settle_s`), then flag
    the RX's stale buffers as settling. The ``settle_samples`` discarded by the next capture
    cover the whole RX FIFO, so every captured sample was taken after the wait (hardware
    and the real-time simulator alike). Returns the wait in seconds."""
    s = source_settle_s(ctx.source) if settle_s is None else max(0.0, float(settle_s))
    if s > 0.0:
        time.sleep(s)
    for d in ctx.devices:
        d.mark_settling()
    return s


@contextmanager
def tone(ctx: MeasurementContext, f_hz: float, power: float) -> Iterator[Any]:
    """Enable the stimulus at ``f_hz``/``power`` for the duration of the block.

    On entry the RF output has settled (:func:`stimulus_changed`), so a capture inside the
    block holds only samples of the new stimulus. On exit the source is switched off; a
    capture that needs the source *off* (not merely disconnected) calls
    :func:`stimulus_changed` itself."""
    src = ctx.source
    if src is None:
        raise MeasurementError("Bu adım sinyal kaynağı gerektiriyor")
    src.set_frequency(f_hz)
    src.set_power(power)
    src.set_enabled(True)
    stimulus_changed(ctx)
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
    setting: float | None = None   # in-situ: source setting of the probe tone (level-uncertainty budget)


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


def source_conditions(ctx: MeasurementContext) -> dict[str, Any]:
    """Stimulus block of the conditions: source name, absolute/relative level and the settle
    time waited after every stimulus change (:func:`source_settle_s`)."""
    src = ctx.source
    if src is None:
        return {"source": "yok"}
    return {"source": str(getattr(src, "name", "?")), "absolute_source": bool(getattr(src, "absolute_power", False)),
            "source_settle_s": source_settle_s(src)}


def _full_span(spec) -> bool:
    """The spectrum covers its whole ``fc ± fs/2`` grid (not a slice, not a stitched sweep)."""
    meta = getattr(spec, "meta", None) or {}
    return not meta.get("stitched") and int(np.size(spec.f_hz)) == int(spec.nfft)


def spectrum_screenshot(spec, name: str, title: str, *, offset_db: float = 0.0, unit: str | None = None,
                        markers: list[tuple[float, float, str]] | None = None, max_points: int = 4001,
                        ref_level: float | None = None, extra_info: dict[str, str] | None = None,
                        xlim_hz: tuple[float, float] | None = None) -> FigureSpec:
    """Spectrum-analyzer style figure (``kind='spectrum'``) from a :class:`Spectrum`.

    The trace is max-hold decimated to ``max_points`` so narrow peaks survive. The frequency
    axis is ``xlim_hz`` when given, else exactly ``fc ± fs/2`` for a full-span spectrum (the
    fftshift grid stops one bin short of ``+fs/2``); for a slice or a stitched sweep it is left
    to the renderer (``Center`` / ``Span`` of ``extra_info``, else the data extent). ``Center``
    is written with 9 significant digits (1 Hz at GHz).
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
    ref = float(10.0 * np.ceil((top + 5.0) / 10.0)) + 0.0         # "+ 0.0": never "-0"
    fc, fs = float(spec.fc), float(spec.fs)
    info = {
        "Center": fmt_hz(fc, 9), "Span": fmt_hz(fs), "RBW": fmt_hz(spec.rbw_hz),
        "Avg": str(spec.averages), "Ref": f"{ref:.0f} {u}", "Window": spec.window,
    }
    if extra_info:
        info.update(extra_info)
    if xlim_hz is None and _full_span(spec) and fs > 0:
        xlim_hz = (fc - fs / 2.0, fc + fs / 2.0)
    return FigureSpec(
        name=name, title=title, kind="spectrum",
        series=[Series(x=(f / 1e6).tolist(), y=p.tolist(), label=u)],
        xlabel="Frekans [MHz]", ylabel=f"Seviye [{u}]",
        markers=[(m[0] / 1e6, m[1], m[2]) for m in (markers or [])],
        info=info, ylim=(ref - 100.0, ref),
        xlim=None if xlim_hz is None else (float(xlim_hz[0]) / 1e6, float(xlim_hz[1]) / 1e6),
    )
