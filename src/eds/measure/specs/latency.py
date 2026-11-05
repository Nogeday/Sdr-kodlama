"""Uçtan uca gecikme [ms] — RF event → ``rx.detections`` delivered to a ZMQ subscriber.

The measurement runs the REAL competition pipeline: an :class:`~eds.rx.node.RxNode` on the
device under test (stream → Welch → CFAR → publish) with a :class:`~eds.bus.pubsub.Publisher`
on a private endpoint (``transport = "tcp"``: ``tcp://127.0.0.1:<free port>``, same socket
path as a deployment; ``"inproc"``: in-process transport only) and a
:class:`~eds.bus.pubsub.Subscriber` playing the consumer (GUI / DF / fusion).

Mode B — RF event latency (stimulus source present; the KTR value)
    ``n_events`` times: source off → wait for a quiet frame (window started after the
    switch-off, no detection near the tone) → random hold (decorrelates the event from the
    block boundaries) → source on at ``fc + 0.15·fs`` with ≥ 25 dB SNR → first
    ``rx.detections`` message whose window ends at/after the earliest possible RF-on instant
    and contains a detection within ``±max(2·RBW, 5 kHz)`` of the tone (plus the TCXO
    tolerance when the node does not correct the reference) → ``L = t_recv − t_RF``. The
    tone level is set adaptively from the SNR the pipeline itself reports, so absolute and
    relative sources work alike. The level never drives the RX input above the profile's
    ``rx_max_input_dbm``: an absolute source is capped by
    :func:`~eds.measure.stimulus.source_power_range`, a relative one by
    :func:`~eds.measure.stimulus.relative_setting_cap` extrapolated from every reading of the
    tone (its band power as published by the node: dBm when calibrated, else dBFS through
    the nominal level model). The setting is recorded with its unit
    (``tone_power_dbm`` / ``tone_power_rel_db``).

    RF-on instant ``t_RF`` (the source call is bracketed by host times ``t_before`` /
    ``t_after``; ``t_ret`` is the time the call returns):

    * simulator source — the exact scene switch (``t_ret``), ± 0;
    * streaming TX (``estimated_queue_latency_s`` = Q, Pluto TX) — the tone starts after the
      samples already queued in the driver: ``t_RF = t_ret + Q`` ± ``Q/4`` (one of four
      kernel buffers; staging buffer / partially played buffer);
    * any other source (SCPI generator: ``:OUTP ON`` + ``*OPC?``; manual) — the RF switched
      during the call: ``t_RF`` = midpoint of ``[t_before, t_after]`` ± half its width.

    The correction, its uncertainty and the source type are recorded; an uncertainty larger
    than :data:`MAX_TIMING_UNC_FRACTION` of the p95 blocks the target verdict.

Mode A — host-side processing latency (always; NOT end-to-end)
    For every ``rx.detections`` message (empty frames included): ``L_A = t_recv_ns −
    t_acq_end_ns`` split into ``processing = t_proc − t_acq_end`` (queue + DSP),
    ``publish = env.t_ns − t_proc`` and ``transport = t_recv − env.t_ns`` (ZMQ). The window
    end ``t_acq_end`` is derived from the host time at which the block's READ RETURNED, so
    the time its last sample spent in the device FIFO and on the USB link is NOT included:
    Mode A is a lower bound of the RF-to-delivery latency (one buffer transfer,
    ``buffer_size · bytes_per_sample / interface_practical_bytes_per_s``, is added as the
    known minimum in the report). Without a source only Mode A exists and the result is
    blocked (no target verdict).

Headline value: p95 of Mode B (Mode A when no source). Statistics: p50, p95, max; p99 only
for N ≥ :data:`P99_MIN_N` events; the distribution-free 95 % confidence interval of the p95
(order statistics) exists only for N ≥ 72 and is then the result's uncertainty (combined
with the RF-on timing uncertainty). On a simulator that is not real-time the samples are
produced faster than the wall clock and the numbers are NOT meaningful: the result says so
and carries a blocker.
"""

from __future__ import annotations

import contextlib
import dataclasses
import logging
import math
import uuid
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

import numpy as np

from ... import __version__
from ...bus.pubsub import Message, Publisher, Subscriber
from ...bus.topics import RX_DETECTIONS, RX_SPECTRUM
from ...core.errors import ConfigError, EDSError, MeasurementError
from ...core.timeutil import iso_utc, mono, now_ns
from ...core.units import fmt_hz
from ..base import FigureSpec, Measurement, MeasurementResult, Series, register
from ..common import LevelRef, base_conditions
from ..stimulus import relative_setting_cap, rx_max_input_dbm, source_power_range
from ..targets import REASON_NONREALTIME

log = logging.getLogger(__name__)

TRANSPORTS: tuple[str, ...] = ("tcp", "inproc")
NONREALTIME_WARNING = ("UYARI: gerçek zamanlı olmayan simülatör (sim.realtime = false) — örnekler duvar saatinden "
                       "bağımsız üretilir; ölçülen gecikme fiziksel olarak ANLAMLI DEĞİLDİR.")
#: Nominal AD936x level model used only for the first tone-level guess: dBFS ≈ dBm + G − 5.
_LEVEL_MODEL_OFFSET_DB = -5.0
_SNR_TOLERANCE_DB = 3.0
_MAX_LEVEL_ITER = 8
_LEVEL_STEP_UP_DB = 20.0
_SNR_FRAMES = 3
#: p99 is reported only from this many events on (below it, it is just the largest samples).
P99_MIN_N = 100
#: Smallest N for which the two-sided 95 % order-statistic CI of the p95 exists.
P95_CI_MIN_N = 72
#: Streaming TX: half-width of the RF-on estimate as a fraction of its queue latency.
QUEUE_UNC_FRACTION = 0.25
#: RF-on timing uncertainty above this fraction of the headline p95 blocks the verdict (the
#: number would describe the source more than the receiver, e.g. a manual source).
MAX_TIMING_UNC_FRACTION = 0.5
TIMING_LABELS: dict[str, str] = {
    "exact": "simülatör sahne geçişi (birebir)",
    "queued": "akışlı TX: dönüş anı + kuyruk gecikmesi",
    "bracket": "komut süresinin ortası (komut + *OPC? / onay)",
}
BLOCKER_MODE_A = ("kaynak yok: RF olayı ölçülmedi (Mod A host tarafı işleme gecikmesidir, cihaz FIFO/USB süresini "
                  "içermez — uçtan uca gecikmenin alt sınırı)")


@dataclass
class _Stats:
    n: int
    p50: float
    p95: float
    p99: float | None                     # None for n < P99_MIN_N
    max: float
    mean: float
    std: float
    min: float
    p95_ci: tuple[float, float] | None

    def to_dict(self, prefix: str) -> dict[str, Any]:
        d = {f"{prefix}_n": self.n, f"{prefix}_p50_ms": self.p50, f"{prefix}_p95_ms": self.p95,
             f"{prefix}_p99_ms": self.p99, f"{prefix}_max_ms": self.max, f"{prefix}_mean_ms": self.mean,
             f"{prefix}_std_ms": self.std, f"{prefix}_min_ms": self.min}
        d[f"{prefix}_p95_ci95_ms"] = list(self.p95_ci) if self.p95_ci is not None else None
        return d


@dataclass
class _ModeA:
    latency_ms: list[float] = field(default_factory=list)
    window_ms: list[float] = field(default_factory=list)
    processing_ms: list[float] = field(default_factory=list)
    publish_ms: list[float] = field(default_factory=list)
    transport_ms: list[float] = field(default_factory=list)
    n_detections: int = 0


@dataclass
class _Event:
    index: int
    t_on_ns: int
    hold_s: float
    detected: bool = False
    latency_ms: float = math.nan
    acquisition_ms: float = math.nan       # t_acq_end − t_on (event → end of the detecting window)
    processing_ms: float = math.nan
    publish_ms: float = math.nan
    transport_ms: float = math.nan
    f_det_hz: float = math.nan
    snr_db: float = math.nan
    quiet_ok: bool = True
    t_on_unc_ms: float = 0.0               # half-width of the RF-on estimate


# =============================================================================== statistics
def quantile_ci(sorted_x: np.ndarray, q: float, conf: float = 0.95) -> tuple[float, float] | None:
    """Distribution-free confidence interval of the ``q`` quantile from order statistics
    (binomial). ``None`` when the sample is too small for the requested confidence."""
    n = int(sorted_x.size)
    if n < 2:
        return None
    pmf = np.array([math.comb(n, k) * q ** k * (1.0 - q) ** (n - k) for k in range(n + 1)])
    cdf = np.cumsum(pmf)
    alpha = (1.0 - conf) / 2.0
    # lower index l: P(B <= l-1) <= alpha ; upper index u: P(B >= u) <= alpha  (1-based order stats)
    lo_candidates = [k for k in range(1, n + 1) if cdf[k - 1] <= alpha]
    hi_candidates = [k for k in range(1, n + 1) if 1.0 - cdf[k - 1] <= alpha]
    if not lo_candidates or not hi_candidates:
        return None
    lo_k, hi_k = max(lo_candidates), min(hi_candidates)
    if lo_k > hi_k:
        return None
    return float(sorted_x[lo_k - 1]), float(sorted_x[hi_k - 1])


def latency_stats(values_ms: list[float]) -> _Stats | None:
    x = np.asarray([v for v in values_ms if math.isfinite(v)], dtype=np.float64)
    if x.size == 0:
        return None
    xs = np.sort(x)
    return _Stats(n=int(x.size), p50=float(np.percentile(x, 50)), p95=float(np.percentile(x, 95)),
                  p99=float(np.percentile(x, 99)) if x.size >= P99_MIN_N else None, max=float(xs[-1]),
                  mean=float(np.mean(x)),
                  std=float(np.std(x, ddof=1)) if x.size > 1 else 0.0, min=float(xs[0]),
                  p95_ci=quantile_ci(xs, 0.95))


def _mean(v: list[float]) -> float:
    a = np.asarray([x for x in v if math.isfinite(x)], dtype=np.float64)
    return float(np.mean(a)) if a.size else math.nan


def _subsample(n: int, k: int) -> list[int]:
    if n <= k:
        return list(range(n))
    return sorted({int(round(i)) for i in np.linspace(0, n - 1, k)})


def _finite_param(v: Any, what: str, lo: float | None = None) -> float:
    if isinstance(v, bool) or not isinstance(v, (int, float)) or not math.isfinite(float(v)):
        raise ConfigError(f"latency.{what}: sonlu sayı bekleniyordu, {v!r} geldi")
    f = float(v)
    if lo is not None and f < lo:
        raise ConfigError(f"latency.{what} >= {lo:g} olmalı, {f:g} geldi")
    return f


def _int_param(v: Any, what: str, lo: int = 1) -> int:
    if isinstance(v, bool) or not isinstance(v, (int, float)) or int(v) != v or int(v) < lo:
        raise ConfigError(f"latency.{what} >= {lo} tamsayı olmalı, {v!r} geldi")
    return int(v)


# =============================================================================== measurement
@register
class LatencyMeasurement(Measurement):
    spec_id = "latency"
    title = "Uçtan uca gecikme"
    unit = "ms"
    default_params = {
        "transport": "tcp",            # tcp (deployment path) | inproc
        "duration_s": 5.0,             # Mode A collection time
        "quick_duration_s": 1.5,
        "n_events": 100,               # Mode B events (≥ 72: CI of the p95; ≥ 100: p99)
        "quick_n_events": 6,
        "offset_frac": 0.15,           # tone at fc + offset_frac * fs
        "snr_db": 30.0,                # tone SNR target (pipeline-reported, RBW-referred)
        "min_snr_db": 25.0,            # minimum acceptable tone SNR
        "warmup_s": 0.5,               # frames discarded after the node started
        "event_timeout_s": 3.0,        # max wait for the detection of one event
        "quiet_timeout_s": 3.0,        # max wait for a quiet frame after switching off
        "hold_max_s": 0.0,             # random hold before each event; 0 = 2 block durations
        "seed": 20261105,              # RNG of the random holds (reproducible schedule)
        "max_table_rows": 200,         # Mode A rows kept in the report table (subsampled)
    }

    # ------------------------------------------------------------------ helpers
    def _params(self) -> dict[str, Any]:
        p = self.p
        quick = bool(self.ctx.quick)
        transport = p["transport"]
        if transport not in TRANSPORTS:
            raise ConfigError(f"latency.transport {transport!r} geçersiz; seçenekler: {list(TRANSPORTS)}")
        return {
            "transport": transport,
            "duration_s": _finite_param(p["quick_duration_s"] if quick else p["duration_s"], "duration_s", 0.1),
            "n_events": _int_param(p["quick_n_events"] if quick else p["n_events"], "n_events"),
            "offset_frac": _finite_param(p["offset_frac"], "offset_frac", 0.01),
            "snr_db": _finite_param(p["snr_db"], "snr_db", 6.0),
            "min_snr_db": _finite_param(p["min_snr_db"], "min_snr_db", 0.0),
            "warmup_s": _finite_param(p["warmup_s"], "warmup_s", 0.0),
            "event_timeout_s": _finite_param(p["event_timeout_s"], "event_timeout_s", 0.1),
            "quiet_timeout_s": _finite_param(p["quiet_timeout_s"], "quiet_timeout_s", 0.1),
            "hold_max_s": _finite_param(p["hold_max_s"], "hold_max_s", 0.0),
            "seed": _int_param(p["seed"], "seed", 0),
            "max_table_rows": _int_param(p["max_table_rows"], "max_table_rows", 1),
        }

    @staticmethod
    def _is_nonrealtime_sim(dev: Any) -> bool:
        info = getattr(dev, "info", None)
        if info is None or getattr(info, "backend", "") != "sim":
            return False
        return str(info.extra.get("realtime", "true")).lower() == "false"

    def _timing_kind(self) -> str:
        """How the RF-on instant of ``ctx.source`` is estimated (see module doc)."""
        from ...hal.sim.source import SimSignalSource

        src = self.ctx.source
        if isinstance(src, SimSignalSource):
            return "exact"
        if self._queue_latency_s() is not None:
            return "queued"
        return "bracket"

    def _queue_latency_s(self) -> float | None:
        try:
            q = getattr(self.ctx.source, "estimated_queue_latency_s", None)
        except Exception:  # noqa: BLE001 - a remote proxy may fail on attribute access
            return None
        if isinstance(q, bool) or not isinstance(q, (int, float)) or not math.isfinite(float(q)) or q <= 0:
            return None
        return float(q)

    def _switch(self, on: bool) -> tuple[int, int, int]:
        """Switch the source; ``(t_lo, t_est, t_hi)`` [ns]: the interval in which the RF
        output changed and its best estimate (module doc)."""
        src = self.ctx.source
        t_before = now_ns()
        t_ret = int(src.set_enabled(on))
        t_after = now_ns()
        kind = self._timing_kind()
        if kind == "exact":
            return t_ret, t_ret, t_ret
        if kind == "queued":
            q = self._queue_latency_s() or 0.0
            est = t_ret + int(round(q * 1e9))
            h = int(round(QUEUE_UNC_FRACTION * q * 1e9))
            return est - h, est, est + h
        lo, hi = min(t_before, t_ret), max(t_after, t_ret)
        return lo, (lo + hi) // 2, hi

    # ------------------------------------------------------------------ run
    def run(self) -> MeasurementResult:
        from ...rx.node import RxNode

        ctx = self.ctx
        app = ctx.app
        dev = ctx.dut
        prm = self._params()
        nonrt = self._is_nonrealtime_sim(dev)
        if nonrt:
            log.warning(NONREALTIME_WARNING)
        endpoint = ("tcp://127.0.0.1:*" if prm["transport"] == "tcp"
                    else f"inproc://eds-latency-{uuid.uuid4().hex}")
        node_app = dataclasses.replace(app, bus=dataclasses.replace(app.bus, mode="direct", ctl_endpoints=[],
                                                                       validate=False))
        events: list[_Event] = []
        level: dict[str, Any] = {}
        # cleanup runs in reverse order even if a step fails: node → subscriber → publisher →
        # source off → the device's hardware ppm correction restored
        with contextlib.ExitStack() as stack:
            stack.callback(self._restore_ppm, dev, float(dev.ppm_hw))
            if ctx.source is not None:
                stack.callback(ctx.source.set_enabled, False)
            pub = Publisher(endpoint, bind=True, src=dev.name, validate=False, hwm=int(app.bus.hwm))
            stack.callback(pub.close)
            sub = Subscriber(pub.endpoint, [RX_DETECTIONS, RX_SPECTRUM], rcvhwm=100_000)
            stack.callback(sub.close)
            node = RxNode(node_app, device=dev, publisher=pub, cal=ctx.cal, name=dev.name)
            stack.callback(node.stop)
            node.start()
            last_spec = self._warmup(sub, node, prm)
            mode_a = self._mode_a(sub, prm["duration_s"])
            if ctx.source is not None:
                with ctx.isolated_source():
                    level, events = self._mode_b(sub, node, prm, last_spec)
            node_stats = node.stats
            node_stats["ppm_via"] = node.ppm_via
            node_stats["window_samples"] = node.window_samples
            node_stats["subscriber"] = sub.stats.to_dict()
        return self._result(dev, prm, mode_a, events, level, node_stats, nonrt)

    @staticmethod
    def _restore_ppm(dev: Any, ppm_hw: float) -> None:
        """Undo the node's hardware ppm correction so the next measurement sees the device as
        the session configured it."""
        if float(dev.ppm_hw) == ppm_hw:
            return
        try:
            dev.set_ppm_correction(ppm_hw)
        except EDSError as e:
            log.error("%s: donanım ppm düzeltmesi eski değerine (%+.4f) döndürülemedi: %s", dev.name, ppm_hw, e)

    # ------------------------------------------------------------------ phases
    def _warmup(self, sub: Subscriber, node: Any, prm: dict[str, Any]) -> Message | None:
        """Wait for the first frame (slow joiner / stream start), then discard ``warmup_s``."""
        app = self.ctx.app
        block_s = float(app.rx.block_size) / max(float(node.device.fs), 1.0)
        first_timeout = max(5.0, 20.0 * block_s)
        last_spec: Message | None = None
        deadline = mono() + first_timeout
        got = False
        while not got:
            remaining = deadline - mono()
            if remaining <= 0:
                raise MeasurementError(
                    f"RX düğümünden {first_timeout:.1f} s içinde rx.detections mesajı gelmedi "
                    f"(düğüm durumu: {node.state}; uyarılar: {node.warnings[-3:]})")
            msg = sub.recv(remaining)
            if msg is None:
                continue
            if msg.topic == RX_SPECTRUM:
                last_spec = msg
            elif msg.topic == RX_DETECTIONS:
                got = True
        end = mono() + prm["warmup_s"]
        while (remaining := end - mono()) > 0:
            msg = sub.recv(remaining)
            if msg is not None and msg.topic == RX_SPECTRUM:
                last_spec = msg
        return last_spec

    def _mode_a(self, sub: Subscriber, duration_s: float) -> _ModeA:
        out = _ModeA()
        end = mono() + duration_s
        while (remaining := end - mono()) > 0:
            msg = sub.recv(remaining)
            if msg is None or msg.topic != RX_DETECTIONS:
                continue
            d = msg.data
            t_end, t_acq, t_proc, t_pub = int(d["t_acq_end_ns"]), int(d["t_acq_ns"]), int(d["t_proc_ns"]), msg.t_ns
            out.latency_ms.append((msg.t_recv_ns - t_end) / 1e6)
            out.window_ms.append((t_end - t_acq) / 1e6)
            out.processing_ms.append((t_proc - t_end) / 1e6)
            out.publish_ms.append((t_pub - t_proc) / 1e6)
            out.transport_ms.append((msg.t_recv_ns - t_pub) / 1e6)
            out.n_detections += len(d["detections"])
        if not out.latency_ms:
            raise MeasurementError(f"Mod A: {duration_s:.1f} s içinde hiç rx.detections mesajı alınamadı")
        return out

    def _tolerance_hz(self, node: Any, f_src: float, rbw: float) -> float:
        tol = max(2.0 * rbw, 5e3)
        if node.ppm_via == "none":
            # reference not corrected: the tone appears at f/(1+e), |e| <= profile TCXO tolerance
            tol += float(node.device.profile.ref_clock_ppm) * 1e-6 * f_src
        return tol

    def _flush_margin_ns(self, node: Any) -> int:
        """Worst-case age of samples a frame may still contain after a scene change: the
        device FIFO (``kernel_buffers × buffer_size``) plus one block. Frame timestamps are
        host-side estimates made when the block was read, so a frame whose window starts less
        than this after a switch may still hold samples produced before it."""
        dev = node.device
        fs = max(float(dev.fs), 1.0)
        fifo = max(int(dev.profile.kernel_buffers), 1) * int(dev.buffer_size) / fs
        return int(round((fifo + float(self.ctx.app.rx.block_size) / fs) * 1e9))

    @staticmethod
    def _near(msg: Message, f_src: float, tol: float) -> dict[str, Any] | None:
        best = None
        for det in msg.data["detections"]:
            if abs(float(det["f_peak_hz"]) - f_src) <= tol and (best is None or det["snr_db"] > best["snr_db"]):
                best = det
        return best

    def _wait_for(self, sub: Subscriber, timeout_s: float,
                  pred: Callable[[Message], Any]) -> tuple[Message, Any] | None:
        """First ``rx.detections`` message for which ``pred(msg)`` is truthy (within timeout)."""
        end = mono() + timeout_s
        while (remaining := end - mono()) > 0:
            msg = sub.recv(remaining)
            if msg is None or msg.topic != RX_DETECTIONS:
                continue
            r = pred(msg)
            if r:
                return msg, r
        return None

    @staticmethod
    def _initial_power(last_spec: Message | None, gain_db: float, snr_db: float, lo: float, hi: float) -> float:
        """First tone setting within ``[lo, hi]``: the pipeline's noise floor + ``snr_db``
        (dBm when the node is calibrated, else through the nominal level model); the middle of
        the range when no spectrum was seen yet."""
        if last_spec is None:
            guess = lo + 0.5 * (hi - lo)
        else:
            d = last_spec.data
            target = float(d["noise_floor"]) + snr_db
            guess = target if d["unit"] == "dBm" else target - gain_db - _LEVEL_MODEL_OFFSET_DB
        return float(np.clip(guess, lo, hi))

    @staticmethod
    def setting_unit(src: Any) -> str:
        """Unit of the source's power setting: ``"dBm"`` (absolute) or ``"dB (bağıl)"``."""
        return "dBm" if getattr(src, "absolute_power", False) else "dB (bağıl)"

    @staticmethod
    def setting_key(src: Any, stem: str = "tone_power") -> str:
        """Condition / extra key of a source setting carrying its unit (``<stem>_dbm`` for an
        absolute source, ``<stem>_rel_db`` for a relative one)."""
        return f"{stem}_dbm" if getattr(src, "absolute_power", False) else f"{stem}_rel_db"

    def _setting_cap(self, dev: Any, setting: float, det: dict[str, Any], unit: str) -> float:
        """Highest setting of a relative source that keeps the RX input at or below the
        profile ceiling, extrapolated from the tone's band power as published by the node
        (``inf`` for an absolute source, already capped by ``source_power_range``)."""
        ref = LevelRef(0.0, "RX düğümü (kalibrasyonlu)", True, "dBm") if unit == "dBm" else None
        return relative_setting_cap(self.ctx, dev, setting, float(det["band_power_db"]), ref)

    def _set_level(self, sub: Subscriber, node: Any, prm: dict[str, Any], last_spec: Message | None,
                   f_src: float, tol: float, margin_ns: int) -> dict[str, Any]:
        """Adaptive tone level: SNR (as reported by the pipeline) → ``snr_db`` target, never
        above the RX input ceiling (module doc)."""
        src = self.ctx.source
        dev = node.device
        lo, hi = source_power_range(src, dev)              # absolute source: capped at rx_max_input_dbm
        p = self._initial_power(last_spec, float(dev.gains[0]), prm["snr_db"], lo, hi)
        src.set_frequency(f_src)
        snr = math.nan
        history: list[tuple[float, float]] = []
        for _ in range(_MAX_LEVEL_ITER):
            p_set = float(src.set_power(p))
            t_on = self._switch(True)[2]                    # latest possible RF-on instant
            snrs: list[float] = []
            best: tuple[dict[str, Any], str] | None = None  # strongest reading of the tone (+ its unit)
            end = mono() + prm["event_timeout_s"]
            while len(snrs) < _SNR_FRAMES and (remaining := end - mono()) > 0:
                msg = sub.recv(remaining)
                if msg is None or msg.topic != RX_DETECTIONS or int(msg.data["t_acq_ns"]) < t_on + margin_ns:
                    continue
                det = self._near(msg, f_src, tol)
                snrs.append(float(det["snr_db"]) if det is not None else -math.inf)
                if det is not None and (best is None or det["snr_db"] > best[0]["snr_db"]):
                    best = (det, str(msg.data.get("unit", "")))
            self._switch(False)
            snr = max(snrs) if snrs else -math.inf
            history.append((p_set, snr))
            if not math.isfinite(snr) or best is None:
                if p_set >= hi:
                    raise MeasurementError(
                        f"Mod B: kaynak tonu ({fmt_hz(f_src)}) izin verilen en yüksek kaynak seviyesinde ({hi:g} "
                        f"{self.setting_unit(src)}; kaynak aralığı ve RX giriş sınırı) bile tespit edilemedi — "
                        "kablo/zayıflatıcı/kaynak bağlantısını kontrol edin")
                p = min(p_set + _LEVEL_STEP_UP_DB, hi)
                continue
            # relative source: never above the RX input limit, extrapolated from this reading
            hi = max(lo, min(hi, self._setting_cap(dev, p_set, *best)))
            if abs(snr - prm["snr_db"]) <= _SNR_TOLERANCE_DB and p_set <= hi:
                break
            new = float(np.clip(p_set + (prm["snr_db"] - snr), lo, hi))
            if abs(new - p_set) < 0.05:
                break                                   # at the source's limit / the RX ceiling
            p = new
        if not math.isfinite(snr):
            raise MeasurementError(f"Mod B: kaynak tonu ({fmt_hz(f_src)}) seviye ayarında tespit edilemedi")
        p_last = history[-1][0]
        if p_last > hi + 0.05:                          # iteration limit reached above the ceiling
            p_new = float(src.set_power(hi))
            snr -= p_last - p_new                       # linear regime: the SNR follows the level
            history.append((p_new, snr))
        key, cap_key = self.setting_key(src, "power"), self.setting_key(src, "power_max")
        return {key: float(history[-1][0]), cap_key: float(hi), "power_unit": self.setting_unit(src),
                "rx_max_input_dbm": rx_max_input_dbm(dev), "snr_db": float(snr),
                "absolute": bool(getattr(src, "absolute_power", False)), "iterations": len(history)}

    def _mode_b(self, sub: Subscriber, node: Any, prm: dict[str, Any],
                last_spec: Message | None) -> tuple[dict[str, Any], list[_Event]]:
        dev = node.device
        app = self.ctx.app
        fs = float(dev.fs)
        frac = min(prm["offset_frac"], 0.45 * float(app.dsp.usable_fraction))
        f_src = float(dev.fc) + frac * fs
        rbw = float(last_spec.data["rbw_hz"]) if last_spec is not None else 2.0 * fs / float(app.dsp.nfft)
        tol = self._tolerance_hz(node, f_src, rbw)
        margin = self._flush_margin_ns(node)
        t_iso = self._switch(False)[2]
        # the isolated scene must be quiet at the tone frequency before anything is measured
        quiet = self._wait_for(sub, prm["quiet_timeout_s"],
                               lambda m: int(m.data["t_acq_ns"]) >= t_iso + margin and self._near(m, f_src, tol) is None)
        if quiet is None:
            raise MeasurementError(
                f"Mod B: kaynak kapalıyken {fmt_hz(f_src)} ±{fmt_hz(tol)} bandında sinyal var (ya da düğüm kare "
                f"üretmiyor) — yalıtım/frekans çakışmasını kontrol edin ya da offset_frac'ı değiştirin")
        level = self._set_level(sub, node, prm, last_spec, f_src, tol, margin)
        level.update({"f_src_hz": f_src, "tolerance_hz": tol, "offset_frac": frac, "flush_margin_ms": margin / 1e6})
        if level["snr_db"] < prm["min_snr_db"]:
            log.warning("Mod B: ton SNR %.1f dB < %.1f dB (kaynak sınırı)", level["snr_db"], prm["min_snr_db"])
        block_s = float(app.rx.block_size) / fs
        hold_max = prm["hold_max_s"] if prm["hold_max_s"] > 0 else max(2.0 * block_s, 0.02)
        rng = np.random.default_rng(prm["seed"])
        events: list[_Event] = []
        for i in range(prm["n_events"]):
            t_off = self._switch(False)[2]
            quiet = self._wait_for(sub, prm["quiet_timeout_s"],
                                   lambda m, t=t_off: int(m.data["t_acq_ns"]) >= t + margin
                                   and self._near(m, f_src, tol) is None)
            hold = float(rng.uniform(0.0, hold_max))
            end = mono() + hold
            while (remaining := end - mono()) > 0:      # keep draining while holding
                sub.recv(remaining)
            t_lo, t_on, t_hi = self._switch(True)
            ev = _Event(index=i, t_on_ns=t_on, hold_s=hold, quiet_ok=quiet is not None,
                        t_on_unc_ms=(t_hi - t_lo) / 2e6)
            # a window that ended before the EARLIEST possible RF-on instant cannot hold the tone
            hit = self._wait_for(sub, prm["event_timeout_s"],
                                 lambda m, t=t_lo: self._near(m, f_src, tol)
                                 if int(m.data["t_acq_end_ns"]) >= t else None)
            if hit is not None:
                msg, det = hit
                d = msg.data
                ev.detected = True
                ev.latency_ms = (msg.t_recv_ns - t_on) / 1e6
                ev.acquisition_ms = (int(d["t_acq_end_ns"]) - t_on) / 1e6
                ev.processing_ms = (int(d["t_proc_ns"]) - int(d["t_acq_end_ns"])) / 1e6
                ev.publish_ms = (msg.t_ns - int(d["t_proc_ns"])) / 1e6
                ev.transport_ms = (msg.t_recv_ns - msg.t_ns) / 1e6
                ev.f_det_hz = float(det["f_peak_hz"])
                ev.snr_db = float(det["snr_db"])
            else:
                log.warning("Mod B: olay %d %.1f s içinde tespit edilmedi", i, prm["event_timeout_s"])
            events.append(ev)
        self._switch(False)
        return level, events

    # ------------------------------------------------------------------ result
    def _transfer_ms(self, dev: Any) -> float:
        """Known minimum the Mode-A clock misses: one device buffer crossing the interface."""
        prof = dev.profile
        rate = float(getattr(prof, "interface_practical_bytes_per_s", 0.0) or 0.0)
        if rate <= 0:
            return 0.0
        return 1e3 * float(dev.buffer_size) * float(prof.bytes_per_sample) * int(getattr(dev, "num_channels", 1)) / rate

    def _result(self, dev: Any, prm: dict[str, Any], a: _ModeA, events: list[_Event], level: dict[str, Any],
                node_stats: dict[str, Any], nonrt: bool) -> MeasurementResult:
        app = self.ctx.app
        st_a = latency_stats(a.latency_ms)
        b_lat = [e.latency_ms for e in events if e.detected]
        st_b = latency_stats(b_lat)
        n_det = len(b_lat)
        if events and n_det == 0:
            raise MeasurementError(f"Mod B: {len(events)} olayın hiçbiri tespit edilemedi (eşik/SNR/kaynak kontrol edin)")
        headline = st_b if st_b is not None else st_a
        mode = "B" if st_b is not None else "A"
        fs = float(dev.fs)
        nfft, averages = int(app.dsp.nfft), int(app.dsp.averages)
        transfer_ms = self._transfer_ms(dev)
        extra: dict[str, Any] = {"mode": mode, "transport": prm["transport"], "realtime": not nonrt}
        extra.update(st_a.to_dict("mode_a"))
        if st_b is not None:
            extra.update(st_b.to_dict("mode_b"))
        bd_a = {"processing_ms": _mean(a.processing_ms), "publish_ms": _mean(a.publish_ms),
                "transport_ms": _mean(a.transport_ms)}
        extra["mode_a_breakdown_mean_ms"] = bd_a
        extra["mode_a_window_mean_ms"] = _mean(a.window_ms)          # analysed span; NOT part of L_A
        extra["mode_a_messages"] = len(a.latency_ms)
        extra["mode_a_detections"] = a.n_detections
        extra["usb_transfer_min_ms"] = transfer_ms
        extra["mode_a_p95_plus_transfer_ms"] = st_a.p95 + transfer_ms
        timing_kind = self._timing_kind() if self.ctx.source is not None else ""
        timing_unc_ms = max((e.t_on_unc_ms for e in events), default=0.0)
        if events:
            bd_b = {"acquisition_ms": _mean([e.acquisition_ms for e in events if e.detected]),
                    "processing_ms": _mean([e.processing_ms for e in events if e.detected]),
                    "publish_ms": _mean([e.publish_ms for e in events if e.detected]),
                    "transport_ms": _mean([e.transport_ms for e in events if e.detected])}
            extra["mode_b_breakdown_mean_ms"] = bd_b
            extra["mode_b_events"] = len(events)
            extra["mode_b_detected"] = n_det
            extra["mode_b_timeouts"] = len(events) - n_det
            extra["mode_b_quiet_timeouts"] = sum(1 for e in events if not e.quiet_ok)
            extra["mode_b_tone"] = level
            q = self._queue_latency_s()
            extra["source_timing"] = {
                "kind": timing_kind, "label": TIMING_LABELS.get(timing_kind, timing_kind),
                "correction_ms": (q or 0.0) * 1e3 if timing_kind == "queued" else 0.0,
                "uncertainty_ms": timing_unc_ms,
                "uncertainty_mean_ms": _mean([e.t_on_unc_ms for e in events]),
            }
        extra.update({
            "block_size": int(app.rx.block_size), "fs_hz": fs, "nfft": nfft, "averages": averages,
            "window_samples": int(node_stats["window_samples"]), "window_ms": node_stats["window_samples"] / fs * 1e3,
            "block_ms": float(app.rx.block_size) / fs * 1e3, "spectrum_rate_hz": float(app.bus.spectrum_rate_hz),
            "ppm_via": node_stats["ppm_via"], "node_frames": int(node_stats.get("frames", 0)),
            "node_publish_errors": int(node_stats.get("publish_errors", 0)),
            "stream_overflows": int(node_stats["stream"]["overflows"]),
            "stream_host_drops": int(node_stats["stream"]["host_drops"]),
            "subscriber_seq_gaps": int(node_stats["subscriber"]["seq_gaps"]),
        })
        if nonrt:
            extra["warning"] = NONREALTIME_WARNING
        # uncertainty of the headline p95: distribution-free CI (N ≥ 72) + RF-on timing
        ci = headline.p95_ci
        unc: float | None = None
        if ci is not None:
            unc = max(headline.p95 - ci[0], ci[1] - headline.p95) + (timing_unc_ms if mode == "B" else 0.0)
        src = self.ctx.source
        conditions = base_conditions(
            dev, nfft=nfft, averages=averages, window=app.dsp.window, block_size=int(app.rx.block_size),
            buffer_size=int(dev.buffer_size), queue_blocks=int(app.rx.queue_blocks),
            spectrum_rate_hz=float(app.bus.spectrum_rate_hz), transport=prm["transport"],
            source=getattr(src, "name", "yok") if src is not None else "yok", n_events=len(events),
            mode_a_duration_s=prm["duration_s"], headline_mode=mode,
        )
        if events:
            conditions.update({"f_tone_hz": level["f_src_hz"], "tone_snr_db": level["snr_db"],
                               self.setting_key(src): level[self.setting_key(src, "power")],
                               self.setting_key(src, "tone_power_max"): level[self.setting_key(src, "power_max")],
                               "rx_max_input_dbm": level["rx_max_input_dbm"],
                               "freq_tolerance_hz": level["tolerance_hz"],
                               "source_timing": TIMING_LABELS.get(timing_kind, timing_kind),
                               "source_timing_unc_ms": timing_unc_ms,
                               "source_queue_correction_ms": extra["source_timing"]["correction_ms"]})

        def ms(v: float) -> str:
            return f"{v:.1f}"

        def pct(st: _Stats) -> str:
            tail = f"p99 {ms(st.p99)}, " if st.p99 is not None else ""
            return f"p50 {ms(st.p50)}, {tail}maks {ms(st.max)} ms"

        def ci_txt(st: _Stats) -> str:
            if st.p95_ci is None:
                return f"p95 için %95 güven aralığı hesaplanamadı (N = {st.n} < {P95_CI_MIN_N})"
            return f"p95 %95 GA [{ms(st.p95_ci[0])}, {ms(st.p95_ci[1])}] ms"

        parts = []
        if st_b is not None:
            parts.append(f"p95 = {ms(st_b.p95)} ms (Mod B, RF olayı → ZMQ teslimi, {n_det}/{len(events)} olay; "
                         f"{pct(st_b)}; {ci_txt(st_b)}; RF açılma anı: {TIMING_LABELS.get(timing_kind, timing_kind)}"
                         + (f", düzeltme +{extra['source_timing']['correction_ms']:.1f} ms" if timing_kind == "queued"
                            else "") + f", belirsizlik ±{timing_unc_ms:.2f} ms)")
            parts.append(f"host tarafı işleme (Mod A, FIFO/USB hariç) p95 = {ms(st_a.p95)} ms")
        else:
            parts.append(f"p95 = {ms(st_a.p95)} ms (Mod A: host tarafı işleme gecikmesi, okuma dönüşünden ZMQ "
                         f"teslimine, {st_a.n} kare; {pct(st_a)}; kaynak yok → Mod B atlandı). Cihaz FIFO/USB süresi "
                         f"dahil değildir: uçtan uca gecikme ≥ {ms(st_a.p95 + transfer_ms)} ms (+ bir tampon aktarımı "
                         f"{ms(transfer_ms)} ms)")
        summary = "; ".join(parts)
        if events and n_det < len(events):
            summary += f"; {len(events) - n_det} olay zaman aşımı"
        lost = int(node_stats["stream"]["host_drops"]) + int(node_stats["stream"]["overflows"])
        if lost:
            note = (f"UYARI: ölçüm sırasında örnek kaybı (overflow {node_stats['stream']['overflows']}, host drop "
                    f"{node_stats['stream']['host_drops']}) — boru hattı gerçek zamana yetişemedi; gecikme kuyruk "
                    "birikimini içerir")
            extra["pipeline_warning"] = note
            summary += f"; {note}"
        if nonrt:
            summary = f"{NONREALTIME_WARNING} {summary}"
        stat_txt = (f"Başlık değeri p95; p99 yalnız N ≥ {P99_MIN_N} olayda verilir. p95 için dağılımdan bağımsız %95 "
                    f"güven aralığı (sıra istatistikleri) yalnız N ≥ {P95_CI_MIN_N} olayda vardır"
                    + (f" ve belirsizlik olarak verilmiştir (± {unc:.2f} ms, RF açılma anı belirsizliği dahil)."
                       if unc is not None else f"; bu ölçümde N = {headline.n}, güven aralığı hesaplanamadı."))
        method = (
            "Yarışma boru hattının kendisi ölçülür: RxNode (akış → Welch PSD → CFAR → ZMQ yayını) DUT üzerinde "
            f"özel bir {prm['transport']} uç noktasına yayın yapar, ölçüm bir ZMQ abonesidir. Mod B (RF olayı): kaynak "
            "kapalı → sessiz kare doğrulaması → rastgele bekleme → kaynak açık → tonu (fc + 0.15·fs, ≥ 25 dB SNR; "
            "seviye RX giriş sınırı rx_max_input_dbm'i aşmayacak biçimde sınırlanır) içeren ve penceresi en erken olası RF açılma anından sonra biten ilk rx.detections mesajının aboneye "
            "ulaştığı an; L = t_recv − t_RF. t_RF: simülatörde sahne geçiş anı; akışlı TX'te (Pluto) çağrı dönüşü + "
            "sürücü kuyruğu gecikmesi (± çeyreği); diğer kaynaklarda (SCPI: :OUTP ON + *OPC?) komut süresinin ortası "
            "(± yarısı). Mod A (host tarafı): her rx.detections mesajı için t_recv − t_acq_end; t_acq_end blok "
            "okumasının dönüş anından türetildiği için cihaz FIFO'sunda ve USB'de geçen süre DAHİL DEĞİLDİR (alt "
            "sınır; döküm: işleme = kuyruk + DSP, yayın, iletim). Mod B dökümünde edinim/işleme ayrımı da aynı "
            "nedenle yaklaşıktır (toplam L bundan etkilenmez). Tüm zaman damgaları aynı host'ta time.time_ns() (duvar saati). "
            + stat_txt)
        res = self.new_result(float(headline.p95), summary=summary, method=method, conditions=conditions,
                              extra=extra, uncertainty=unc)
        if nonrt:
            res.blockers.append(REASON_NONREALTIME)
        if mode == "A":
            res.blockers.append(BLOCKER_MODE_A)
        elif timing_unc_ms > MAX_TIMING_UNC_FRACTION * headline.p95:
            res.blockers.append(f"RF açılma anı belirsizliği ±{timing_unc_ms:.1f} ms, p95'in %"
                                f"{100 * MAX_TIMING_UNC_FRACTION:.0f}'undan büyük ({self.ctx.source.name} kaynağı)")
        res.table = self._table(a, events, prm["max_table_rows"])
        res.figures = self._figures(dev, st_a, st_b, a, events, bd_a, extra, nonrt)
        return res

    @staticmethod
    def _table(a: _ModeA, events: list[_Event], max_rows: int) -> list[dict[str, Any]]:
        rows: list[dict[str, Any]] = []
        for e in events:
            rows.append({"mode": "B", "index": e.index, "latency_ms": e.latency_ms if e.detected else None,
                         "acquisition_ms": e.acquisition_ms if e.detected else None,
                         "window_ms": None,
                         "processing_ms": e.processing_ms if e.detected else None,
                         "publish_ms": e.publish_ms if e.detected else None,
                         "transport_ms": e.transport_ms if e.detected else None,
                         "f_det_hz": e.f_det_hz if e.detected else None, "snr_db": e.snr_db if e.detected else None,
                         "hold_s": e.hold_s, "t_rf_ns": e.t_on_ns, "t_rf_unc_ms": e.t_on_unc_ms,
                         "status": "tespit" if e.detected else "zaman aşımı"})
        for i in _subsample(len(a.latency_ms), max_rows):
            rows.append({"mode": "A", "index": i, "latency_ms": a.latency_ms[i], "acquisition_ms": None,
                         "window_ms": a.window_ms[i], "processing_ms": a.processing_ms[i],
                         "publish_ms": a.publish_ms[i], "transport_ms": a.transport_ms[i], "f_det_hz": None,
                         "snr_db": None, "hold_s": None, "t_rf_ns": None, "t_rf_unc_ms": None, "status": "kare"})
        return rows

    def _figures(self, dev: Any, st_a: _Stats, st_b: _Stats | None, a: _ModeA, events: list[_Event],
                 bd_a: dict[str, float], extra: dict[str, Any], nonrt: bool) -> list[FigureSpec]:
        info = {"fs": fmt_hz(extra["fs_hz"]), "Blok": f"{extra['block_size']} ({extra['block_ms']:.1f} ms)",
                "Pencere": f"{extra['window_ms']:.1f} ms (L'ye dahil değil)", "version": __version__,
                "date": iso_utc(now_ns()), "serial": str(getattr(dev.info, "serial", ""))}
        if nonrt:
            info["UYARI"] = "gerçek zamanlı olmayan simülatör — değer anlamsız"

        def pct_lines(st: _Stats) -> list[tuple[float, str]]:
            lines = [(st.p50, f"p50 = {st.p50:.1f} ms"), (st.p95, f"p95 = {st.p95:.1f} ms")]
            if st.p99 is not None:
                lines.append((st.p99, f"p99 = {st.p99:.1f} ms"))
            return lines

        figs = []
        if st_b is not None:
            figs.append(FigureSpec(
                name="latency_mode_b_cdf", title="Uçtan uca gecikme — Mod B (RF olayı → ZMQ teslimi)", kind="hist",
                series=[Series(x=[], y=[e.latency_ms for e in events if e.detected], label="Mod B olay gecikmesi")],
                xlabel="Gecikme [ms]", ylabel="Olay sayısı", vlines=pct_lines(st_b), info=dict(info)))
        figs.append(FigureSpec(
            name="latency_mode_a_cdf", title="Host tarafı işleme gecikmesi — Mod A (okuma dönüşü → ZMQ teslimi, "
                                             "FIFO/USB hariç)", kind="hist",
            series=[Series(x=[], y=list(a.latency_ms), label="Mod A kare gecikmesi")],
            xlabel="Gecikme [ms]", ylabel="Kare sayısı", vlines=pct_lines(st_a), info=dict(info)))
        # one stacked bar per mode (cumulative, largest first): the bar top is the mean latency
        # itself and every visible slice one component; the analysed window is NOT a component
        figs.append(self._breakdown_figure(
            "latency_mode_a_breakdown", "Gecikme dökümü — Mod A (host tarafı, ortalama; FIFO/USB hariç)",
            f"Mod A — host tarafı ({st_a.n} kare)",
            [("İşleme (kuyruk + DSP)", bd_a["processing_ms"]), ("Yayın (serileştirme)", bd_a["publish_ms"]),
             ("İletim (ZMQ)", bd_a["transport_ms"])], info))
        bd_b = extra.get("mode_b_breakdown_mean_ms")
        if st_b is not None and bd_b:
            figs.append(self._breakdown_figure(
                "latency_mode_b_breakdown", "Gecikme dökümü — Mod B (RF olayı → ZMQ, ortalama, olay başına)",
                f"Mod B — RF olayı → ZMQ ({st_b.n} olay)",
                [("Edinim (olay → pencere sonu)", bd_b["acquisition_ms"]),
                 ("İşleme (kuyruk + DSP)", bd_b["processing_ms"]), ("Yayın (serileştirme)", bd_b["publish_ms"]),
                 ("İletim (ZMQ)", bd_b["transport_ms"])], info))
        return figs

    @staticmethod
    def _breakdown_figure(name: str, title: str, bar_label: str, comps: list[tuple[str, float]],
                          info: dict[str, str]) -> FigureSpec:
        """Single stacked bar: cumulative sums drawn largest first, so the visible slice of the
        bar is one component and its top is the total (= the mean latency of the mode). The
        bar is named on the categorical x axis (``xticks``)."""
        vals = [(lab, float(v) if math.isfinite(v) and v > 0 else 0.0) for lab, v in comps]
        cum = np.cumsum([v for _, v in vals]).tolist()
        series = [Series(x=[1.0], y=[cum[i]], label=f"{lab}: {v:.2f} ms", style="bar")
                  for i, (lab, v) in reversed(list(enumerate(vals)))]
        total = cum[-1] if cum else 0.0
        return FigureSpec(
            name=name, title=title, kind="xy", series=series,
            markers=[(1.0, total, f"toplam {total:.2f} ms")], xlabel="yığılmış çubuk (bileşenler alttan üste)",
            ylabel="Süre [ms]", xlim=(0.4, 1.6), ylim=(0.0, 1.3 * total if total > 0 else 1.0), info=dict(info),
            xticks=[(1.0, bar_label)])
