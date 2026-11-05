"""Taşmasız maksimum örnekleme hızı [MS/s] — highest sustainable continuous sample rate.

Definition (docs/MEASUREMENTS.md section 8)::

    fs_max = max { fs : continuous stream of T s with overflow events = 0, read timeouts = 0,
                        (pipeline) host-queue drops = 0, and no sample missing in the count }
    data   = fs · bytes_per_sample   (CS16: 4 B → USB 2.0 theoretical bulk 53.248 MB/s)

Modes
-----
``pipeline``   **headline (system level, "DSP açık")**: the real competition pipeline, an
               :class:`~eds.rx.node.RxNode` on the DUT (reader thread + host queue → Welch →
               CFAR → ``rx.detections`` / ``rx.spectrum`` published on a private ZMQ endpoint),
               with the ``[rx]`` block size / queue and the ``[dsp]`` / ``[detect]`` settings.
``transport``  read-and-discard: USB + libiio + host only (tuning / diagnosis value).

The KTR uses the system-level value (doc section 8); with ``modes = ["transport"]`` only the
transport value exists and the result carries a blocker (no target verdict).

Procedure per (mode, rate)
--------------------------
Transport: stream stopped, ``set_sample_rate(rate)``, analog BW = rate clamped to the
profile's BW limits (:class:`~eds.core.errors.ProfileViolation` → point skipped and noted),
stream started with ``buffer_size``; the settle window is discarded, the FIFO backlog is
drained (reads until one had to WAIT for production), then ``duration_s`` of continuous
reads. After the first failure sign the window is cut to ``fail_window_s``.

Pipeline: a fresh RxNode is started at the rate; after its first frame and ``pipeline_warmup_s``
the node and device counters are snapshotted, the node runs for ``duration_s`` and is
snapshotted again. Besides overflows / timeouts / host-queue drops, the processing must keep
up: the samples read but not yet analysed at the end (host-queue + accumulator backlog)
must stay below half the host queue (``queue_blocks``) plus one Welch window.

Sample-count cross-check (some drivers report overflows late or never; on a Pluto the HAL
reads the RX DMA status register, ``conditions['overflow_source']``). Lost samples
``L = expected − delivered`` with the best clock available:

* ``hw_timestamp``   — device timestamps of the first block and the end of the last one
  (allowed: 1 sample + 1 ppm of rounding);
* ``device_counter`` — the device's absolute sample counter (``abs_index``, gaps included;
  the simulator): exact in transport mode; ±1 block in pipeline mode (the counters are
  snapshotted while the reader thread may be inside a read);
* ``wall_clock``     — transport mode, **upper envelope** of ``g_i = D_i − fs·(t_i − t0)``
  (``D_i`` samples delivered up to read ``i``, ``t_i`` its return time). A read that had to
  wait for production returns at "buffer complete + transfer time", a read from a backlog
  later than that, so ``max g`` is the production timeline (jitter only ever lowers ``g``).
  After the window the FIFO is drained (read-and-discard until two consecutive reads are
  production-paced, ``Δg ≤ 0.1·buffer``), so a backlog queued when the window ends is
  delivered, not lost: ``L = max g(window) − max g(drain)``. Allowed:
  ``max(buffer/2, ref_clock_ppm·expected + wall_clock_jitter_s·fs)``; a silently dropped
  buffer is whole (``buffer`` samples) — when the allowance reaches one buffer (very long
  runs with small buffers) the result says that one lost buffer is not resolvable;
* ``wall_clock_snapshot`` — pipeline mode without a device counter: ``fs × Δt`` between the
  snapshots, allowed ``(kernel_buffers + 2) × buffer`` + clock (gross loss only; single
  silent buffers are searched by the transport mode and the overflow flag).

Search: candidate rates ascending (``profile.candidate_sample_rates_hz`` inside the
profile/runtime limits; ``--quick``: 1, 2.4, 4, 5, 6, 8 MS/s). A mode stops after
``stop_after_failures`` consecutive failures. ``fs_max`` = highest rate that passed below the
first failure (a pass above a failure is reported as non-monotonic, never as the value);
with ``refine`` the gap between the last pass and the first failure is bisected
``refine_steps`` times (1 kS/s grid). The pipeline is scanned first; every rate it passes
below its first failure is an implied transport pass (same device and transport, strictly
more consumer load).

On a non-real-time simulator the pipeline cannot be measured (samples are produced without
waiting, the host queue overflows at any rate): only the transport mode runs and the
result is blocked. On the real-time simulator the transport limit is the simulator's USB
model (``usb_bytes_per_s``), which the summary states.

Hardware tuning matrix: ``buffer_sizes = [...]`` repeats the transport search for every
listed buffer size (``extra['matrix']``); on a Pluto the buffer size (the stream argument
``bufflen``) is the only stream setting SoapyPlutoSDR reads. Its device arguments are only
``uri``, ``hostname`` and ``label`` (``serial`` is resolved to ``uri`` by EDS): anything else
(``usb_direct``, ``timeout``, ``loopback``, ``timestamp_every`` …) is IGNORED by the driver
(the HAL warns), so it is not a measurement condition — such arguments are listed in
``conditions['device_args_ignored']`` and kept out of ``conditions['device_args']`` (the
arguments in effect). Arguments a driver honours only when the device is opened cannot be
switched by a running measurement: run it once per ``[[device]].args`` variant.

Overflow detection: ``conditions['overflow_source']`` is the device's own statement
(``"dma_status"`` / ``"driver"`` / ``"none"``; ``""`` when the device does not state one).
``"none"`` on real hardware (a Pluto without the thin libiio layer) means sample loss is NOT
flagged by the device — only the sample count can find it — and the result carries
:data:`BLOCKER_NO_OVERFLOW` (no target verdict).
"""

from __future__ import annotations

import contextlib
import dataclasses
import logging
import math
import os
import platform
import time
import uuid
from dataclasses import dataclass
from typing import Any

from ...core.errors import ConfigError, EDSError, ProfileViolation, StreamTimeout
from ...core.timeutil import mono
from ..base import FigureSpec, Measurement, MeasurementResult, Series, register
from ..common import base_conditions, capture_spectrum, spectrum_screenshot
from ..targets import REASON_NONREALTIME

log = logging.getLogger(__name__)

TITLE = "Taşmasız maksimum örnekleme hızı"
MODES: tuple[str, ...] = ("transport", "pipeline")
MODE_LABELS: dict[str, str] = {"transport": "yalnız taşıma", "pipeline": "DSP hattı açık"}
ACCOUNTING_LABELS: dict[str, str] = {
    "hw_timestamp": "donanım zaman damgası", "device_counter": "cihaz örnek sayacı",
    "wall_clock": "duvar saati zarfı (iki uçta boş FIFO)",
    "wall_clock_snapshot": "duvar saati (sayaç anlık görüntüsü)",
}
TRANSPORTS: tuple[str, ...] = ("tcp", "inproc")
QUICK_RATES_HZ: tuple[float, ...] = (1e6, 2.4e6, 4e6, 5e6, 6e6, 8e6)
#: Rates of the refinement bisection are rounded to this grid (S/s).
REFINE_GRID_HZ = 1e3
#: Zero counts are drawn at this level on the logarithmic overflow/loss plot.
PLOT_FLOOR = 0.1
#: A drain read that waited at least this fraction of a buffer period found the FIFO empty.
DRAIN_WAIT_FRACTION = 0.5
#: End drain: a read is production-paced when ``Δg <= PACED_FRACTION · buffer``.
PACED_FRACTION = 0.1
#: End drain: consecutive production-paced reads that prove the FIFO is empty.
PACED_READS = 2
#: hw_timestamp accounting: rounding allowance (samples, + ppm of the expected count).
HW_TS_ALLOWED_SAMPLES = 1.0
HW_TS_ALLOWED_PPM = 1.0
#: Pipeline: processing backlog limit, as a fraction of the host queue (plus one window).
BACKLOG_QUEUE_FRACTION = 0.5
#: Pipeline: counter polling period while the node runs (early failure detection).
POLL_S = 0.05
NONREALTIME_WARNING = ("UYARI: gerçek zamanlı olmayan simülatör (sim.realtime = false) — örnekler beklemeden üretilir, "
                       "USB/FIFO modeli devre dışıdır; ölçülen hız fiziksel olarak ANLAMLI DEĞİLDİR.")
BLOCKER_TRANSPORT_ONLY = ("yalnız taşıma modu ölçüldü: KTR sistem düzeyi (DSP hattı açık) değeri ister "
                          "(max_rate.modes'a 'pipeline' ekleyin)")
BLOCKER_NO_OVERFLOW = ("örnek kaybı algılanamaz: overflow sayacı bildirilmiyor (overflow_source = none; Pluto'da "
                       "ince libiio katmanı / control_uri yok) — kayıp yalnız örnek sayımıyla aranır")
STATUS_TR = {"pass": "GEÇTİ", "fail": "KALDI", "skipped": "ATLANDI"}


def device_args_conditions(dev: Any) -> tuple[dict[str, str], list[str]]:
    """``(args in effect, ignored arg names)`` of the device's ``[[device]].args``.

    SoapyPlutoSDR reads only ``uri`` / ``hostname`` / ``label`` (+ ``driver``, ``serial`` →
    ``uri``) and the stream argument ``stream:bufflen``; every other argument of a Pluto is
    ignored by the driver and must not be reported as a measurement condition. Other
    backends: all arguments are reported as given."""
    from ...hal.soapy_backend import PLUTO_DEVICE_ARGS, PLUTO_DRIVER, PLUTO_STREAM_ARGS, STREAM_ARG_PREFIX

    args = {str(k): str(v) for k, v in (getattr(getattr(dev, "cfg", None), "args", None) or {}).items()}
    driver = str(args.get("driver") or getattr(getattr(dev, "profile", None), "soapy_driver", "") or "").lower()
    if getattr(getattr(dev, "info", None), "backend", "") == "sim" or driver != PLUTO_DRIVER:
        return args, []

    def honoured(k: str) -> bool:
        if k.startswith(STREAM_ARG_PREFIX):
            return k[len(STREAM_ARG_PREFIX):] in PLUTO_STREAM_ARGS
        return k in PLUTO_DEVICE_ARGS

    return {k: v for k, v in args.items() if honoured(k)}, sorted(k for k in args if not honoured(k))


@dataclass
class Trial:
    """One (mode, rate, buffer size) streaming test."""

    mode: str
    rate_hz: float
    buffer_size: int
    refine: bool = False
    status: str = "fail"                 # pass | fail | skipped
    actual_hz: float = math.nan
    overflows: int = 0
    timeouts: int = 0
    host_drops: int = 0
    discarded: int = 0
    settle_discarded: int = 0
    delivered: int = 0
    expected: float = math.nan
    lost: float = math.nan               # expected − delivered (samples)
    allowed_lost: float = math.nan       # count cross-check allowance (samples)
    loss: float = math.nan               # lost / expected
    elapsed_s: float = 0.0
    blocks: int = 0
    drain_reads: int = 0                 # wall_clock: reads of the end-of-window drain
    frames: int = 0                      # pipeline: Welch/CFAR frames processed
    backlog_s: float = math.nan          # pipeline: read-but-unprocessed samples at the end
    lag_ms: float = math.nan             # pipeline: window end → processed, last frame
    accounting: str = ""
    implied: bool = False                # transport pass implied by a pipeline pass (not run)
    note: str = ""

    @property
    def passed(self) -> bool:
        return self.status == "pass"

    def row(self, bytes_per_sample: int) -> dict[str, Any]:
        """Raw table row; the counters of an implied (not streamed) trial are ``None``."""
        row = {
            "mode": self.mode, "mode_tr": MODE_LABELS[self.mode], "rate_msps": self.rate_hz / 1e6,
            "fs_hz": self.actual_hz, "buffer_size": self.buffer_size, "refine": self.refine,
            "status": STATUS_TR[self.status] + (" (çıkarım)" if self.implied else ""), "implied": self.implied,
            "overflows": self.overflows, "timeouts": self.timeouts, "host_drops": self.host_drops,
            "discarded_samples": self.discarded, "settle_discarded_samples": self.settle_discarded,
            "delivered_samples": self.delivered, "expected_samples": self.expected, "lost_samples": self.lost,
            "allowed_lost_samples": self.allowed_lost, "loss": self.loss,
            "loss_ppm": self.loss * 1e6 if math.isfinite(self.loss) else math.nan, "elapsed_s": self.elapsed_s,
            "blocks": self.blocks, "drain_reads": self.drain_reads, "frames": self.frames,
            "backlog_ms": self.backlog_s * 1e3, "lag_ms": self.lag_ms,
            "mb_per_s": self.rate_hz * bytes_per_sample / 1e6, "accounting": self.accounting, "note": self.note,
        }
        if self.implied:
            for k in ("fs_hz", "overflows", "timeouts", "host_drops", "discarded_samples", "settle_discarded_samples",
                      "delivered_samples", "expected_samples", "lost_samples", "allowed_lost_samples", "loss",
                      "loss_ppm", "elapsed_s", "blocks", "drain_reads", "frames", "backlog_ms", "lag_ms"):
                row[k] = None
        return row

    def judge(self) -> None:
        """PASS ⇔ data, no overflow / timeout / host drop / HAL discard, count within allowance."""
        count_ok = math.isfinite(self.lost) and math.isfinite(self.allowed_lost) and self.lost <= self.allowed_lost
        ok = ((self.blocks > 0 or self.frames > 0) and self.overflows == 0 and self.timeouts == 0
              and self.host_drops == 0 and self.discarded == 0 and count_ok)
        if ok and self.mode == "pipeline" and not math.isfinite(self.backlog_s):
            ok = False
        self.status = "pass" if ok else "fail"


@dataclass
class ModeOutcome:
    """Search result of one mode."""

    mode: str
    trials: list[Trial]
    best: Trial | None                  # highest pass below the first failure (after refinement)
    first_fail: Trial | None            # lowest failure above ``best`` (after refinement)
    monotonic: bool
    all_passed: bool

    @property
    def max_hz(self) -> float | None:
        return self.best.rate_hz if self.best is not None else None


def mono_sleep(s: float) -> None:
    """Sleep ``s`` seconds (indirection so tests can drive a fake clock)."""
    if s > 0:
        time.sleep(s)


def _finite(v: Any, what: str, lo: float | None = None) -> float:
    if isinstance(v, bool) or not isinstance(v, (int, float)) or not math.isfinite(float(v)):
        raise ConfigError(f"max_rate.{what} sonlu bir sayı olmalı, {v!r} geldi")
    if lo is not None and float(v) < lo:
        raise ConfigError(f"max_rate.{what} en az {lo:g} olmalı, {v!r} geldi")
    return float(v)


def _int(v: Any, what: str, lo: int = 0) -> int:
    if isinstance(v, bool) or not isinstance(v, (int, float)) or int(v) != v or int(v) < lo:
        raise ConfigError(f"max_rate.{what} en az {lo} olan bir tamsayı olmalı, {v!r} geldi")
    return int(v)


def evaluate(mode: str, trials: list[Trial]) -> ModeOutcome:
    """``fs_max`` of one mode from its trials (scan + refinement, any order)."""
    scan = sorted((t for t in trials if not t.refine and t.status != "skipped"), key=lambda t: t.rate_hz)
    best: Trial | None = None
    first_fail: Trial | None = None
    for t in scan:
        if first_fail is not None:
            break
        if t.passed:
            best = t
        else:
            first_fail = t
    monotonic = not (first_fail is not None and any(t.passed and t.rate_hz > first_fail.rate_hz for t in scan))
    lo = best.rate_hz if best is not None else -math.inf
    hi = first_fail.rate_hz if first_fail is not None else math.inf
    for t in sorted((t for t in trials if t.refine and t.status != "skipped"), key=lambda t: t.rate_hz):
        if lo < t.rate_hz < hi:
            if t.passed:
                best, lo = t, t.rate_hz
            elif first_fail is None or t.rate_hz < first_fail.rate_hz:
                first_fail, hi = t, t.rate_hz
    return ModeOutcome(mode, list(trials), best, first_fail, monotonic,
                       all_passed=bool(scan) and first_fail is None)


@register
class MaxRateMeasurement(Measurement):
    spec_id = "max_rate"
    title = TITLE
    unit = "MS/s"
    requires = frozenset()
    default_params = {
        "rates": None,                    # None = profile.candidate_sample_rates_hz (quick: QUICK_RATES_HZ)
        "duration_s": 5.0,
        "quick_duration_s": 0.6,
        "fail_window_s": 1.0,             # observation kept after the first failure sign
        "quick_fail_window_s": 0.3,
        "modes": list(MODES),             # pipeline = headline (system level); transport = diagnosis
        "buffer_size": None,              # None = [rx] buffer_size
        "buffer_sizes": [],               # optional tuning matrix (transport mode)
        "stop_after_failures": 2,
        "refine": True,
        "quick_refine": False,
        "refine_steps": 3,
        "wall_clock_jitter_s": 1e-3,      # wall-clock count allowance (best-case read-return jitter)
        "pipeline_transport": "tcp",      # RxNode publisher: tcp (deployment path) | inproc
        "pipeline_warmup_s": 0.2,         # node runs this long after its first frame before counting
        "timeout_s": None,                # None = max([rx] read_timeout_s, 4 buffer periods)
        "spectrum_nfft": 4096,
        "spectrum_averages": 16,
    }

    # ------------------------------------------------------------------ parameters
    def _params(self) -> dict[str, Any]:
        p, app, dev = self.p, self.ctx.app, self.ctx.dut
        quick = bool(self.ctx.quick)
        modes = p["modes"]
        if not isinstance(modes, (list, tuple)) or not modes or any(m not in MODES for m in modes):
            raise ConfigError(f"max_rate.modes {modes!r} geçersiz; seçenekler: {list(MODES)}")
        modes = [m for m in MODES if m in modes]
        bsize = _int(app.rx.buffer_size if p["buffer_size"] is None else p["buffer_size"], "buffer_size", 256)
        raw_bs = p["buffer_sizes"]
        if not isinstance(raw_bs, (list, tuple)):
            raise ConfigError(f"max_rate.buffer_sizes bir liste olmalı, {raw_bs!r} geldi")
        matrix = sorted({_int(b, "buffer_sizes[]", 256) for b in raw_bs})
        key = "quick_refine" if quick else "refine"
        if not isinstance(p[key], bool):
            raise ConfigError(f"max_rate.{key} true/false olmalı, {p[key]!r} geldi")
        if p["pipeline_transport"] not in TRANSPORTS:
            raise ConfigError(f"max_rate.pipeline_transport {p['pipeline_transport']!r} geçersiz; "
                              f"seçenekler: {list(TRANSPORTS)}")
        prm = {
            "modes": modes, "buffer_size": bsize, "matrix": [b for b in matrix if b != bsize],
            "duration_s": _finite(p["quick_duration_s"] if quick else p["duration_s"], "duration_s", 0.05),
            "fail_window_s": _finite(p["quick_fail_window_s"] if quick else p["fail_window_s"], "fail_window_s", 0.0),
            "stop_after": _int(p["stop_after_failures"], "stop_after_failures", 1),
            "refine": p[key],
            "refine_steps": _int(p["refine_steps"], "refine_steps", 0),
            "jitter_s": _finite(p["wall_clock_jitter_s"], "wall_clock_jitter_s", 0.0),
            "pipeline_transport": p["pipeline_transport"],
            "warmup_s": _finite(p["pipeline_warmup_s"], "pipeline_warmup_s", 0.0),
            "timeout_s": None if p["timeout_s"] is None else _finite(p["timeout_s"], "timeout_s", 0.01),
            "spectrum_nfft": _int(p["spectrum_nfft"], "spectrum_nfft", 16),
            "spectrum_averages": _int(p["spectrum_averages"], "spectrum_averages", 1),
            "pipeline_skipped": "",
        }
        rates, excluded = self._rates(dev)
        prm["rates"], prm["excluded_rates"] = rates, excluded
        return prm

    def _rates(self, dev: Any) -> tuple[list[float], list[float]]:
        raw = self.p["rates"]
        if raw is None:
            raw = QUICK_RATES_HZ if self.ctx.quick else dev.profile.candidate_sample_rates_hz
        if not isinstance(raw, (list, tuple)) or not raw:
            raise ConfigError("max_rate.rates boş olmayan bir liste olmalı (S/s) ya da boş bırakılmalı (profil adayları)")
        vals = sorted({_finite(r, "rates[]", 1.0) for r in raw})
        prof = dev.profile
        lo, hi = float(prof.sample_rate_min_hz), float(prof.sample_rate_max_hz)
        rt = dev.runtime_limits().get("sample_rate_hz")
        if rt:
            lo, hi = max(lo, float(rt[0])), min(hi, float(rt[1]))
        inside = [r for r in vals if lo <= r <= hi]
        excluded = [r for r in vals if not lo <= r <= hi]
        if excluded:
            log.warning("max_rate: profil/çalışma sınırları [%.4g, %.4g] MS/s dışındaki hızlar atlandı: %s",
                        lo / 1e6, hi / 1e6, [r / 1e6 for r in excluded])
        if not inside:
            raise ConfigError(f"max_rate: aday hızların hiçbiri [{lo / 1e6:.4g}, {hi / 1e6:.4g}] MS/s sınırları içinde değil")
        return inside, excluded

    @staticmethod
    def _is_nonrealtime_sim(dev: Any) -> bool:
        info = getattr(dev, "info", None)
        if info is None or getattr(info, "backend", "") != "sim":
            return False
        return str(info.extra.get("realtime", "true")).lower() == "false"

    def _wall_clock_allowance(self, dev: Any, expected: float, fs: float, buffer_size: int,
                              jitter_s: float) -> float:
        clock = float(dev.profile.ref_clock_ppm) * 1e-6 * max(expected, 0.0) + jitter_s * fs
        return max(0.5 * buffer_size, clock)

    # ------------------------------------------------------------------ transport trial
    def _setup(self, dev: Any, rate: float, buffer_size: int) -> None:
        prof = dev.profile
        if dev.streaming:
            dev.stop_stream()
        dev.set_sample_rate(rate)
        dev.set_bandwidth(min(max(rate, prof.bw_min_hz), prof.bw_max_hz))
        dev.start_stream(buffer_size)

    def _trial(self, dev: Any, mode: str, rate: float, buffer_size: int, prm: dict[str, Any],
               refine: bool = False) -> Trial:
        if mode == "pipeline":
            tr = self._pipeline_trial(dev, rate, buffer_size, prm, refine)
        else:
            tr = Trial(mode=mode, rate_hz=float(rate), buffer_size=int(buffer_size), refine=refine)
            try:
                self._setup(dev, rate, buffer_size)
            except ProfileViolation as e:
                tr.status, tr.note = "skipped", f"profil sınırı: {e}"
            else:
                try:
                    self._measure(dev, tr, prm)
                finally:
                    dev.stop_stream()
        if tr.status == "skipped":
            log.warning("max_rate %s %.4g MS/s atlandı: %s", mode, rate / 1e6, tr.note)
        else:
            log.info("max_rate %-9s %8.4g MS/s buf %d: %s (overflow %d, zaman aşımı %d, host drop %d, kayıp %.4g / "
                     "izin %.4g örnek, %s)", mode, rate / 1e6, buffer_size, STATUS_TR[tr.status], tr.overflows,
                     tr.timeouts, tr.host_drops, tr.lost, tr.allowed_lost, tr.accounting)
        return tr

    def _measure(self, dev: Any, tr: Trial, prm: dict[str, Any]) -> None:
        """Transport (read-and-discard) trial on a started stream."""
        app = self.ctx.app
        fs = float(dev.fs)
        tr.actual_hz = fs
        b = int(tr.buffer_size)
        blk_dur = b / fs
        to = prm["timeout_s"] if prm["timeout_s"] is not None else max(float(app.rx.read_timeout_s), 4.0 * blk_dur)
        failed_early = False
        try:
            tr.settle_discarded = int(dev.flush(to))
        except StreamTimeout:
            failed_early = True
            tr.note = "settle sırasında zaman aşımı"
        t_start = mono()
        if not failed_early:
            for _ in range(int(dev.profile.kernel_buffers) + 2):          # backlog drain
                ta = mono()
                try:
                    dev.read(b, to)
                except StreamTimeout:
                    failed_early = True
                    tr.note = "tampon boşaltmada zaman aşımı"
                    break
                t_start = mono()
                if t_start - ta >= DRAIN_WAIT_FRACTION * blk_dur:
                    break
        counter = getattr(dev, "abs_index", None)
        use_counter = isinstance(counter, int) and not isinstance(counter, bool)
        idx0 = int(counter) if use_counter else 0
        t_end = t_start
        hw_first: int | None = None
        hw_end = 0.0
        hw_all = True
        env_window = 0.0                     # upper envelope of g = D − fs·(t − t_start); anchor g = 0
        g_last = 0.0
        fail_t: float | None = t_start if failed_early else None
        while not failed_early:
            now = mono()
            if t_end - t_start >= prm["duration_s"] or (fail_t is not None and now - fail_t >= prm["fail_window_s"]):
                break
            try:
                blk = dev.read(b, to)
            except StreamTimeout:
                fail_t = mono() if fail_t is None else fail_t
                continue
            t_end = mono()
            if blk.overflow and fail_t is None:
                fail_t = t_end
            tr.delivered += blk.n
            tr.blocks += 1
            g_last = tr.delivered - fs * (t_end - t_start)
            env_window = max(env_window, g_last)
            hw = blk.meta.get("hw_time_ns")
            if hw is None:
                hw_all = False
            else:
                hw_first = int(hw) if hw_first is None else hw_first
                hw_end = float(hw) + blk.n / blk.fs * 1e9
        if tr.blocks and not (hw_all and hw_first is not None) and not use_counter:
            # wall clock: drain the FIFO so a backlog queued at the window end is delivered, not lost
            env_end = g_last
            if fail_t is None:
                env_end, t_end = self._end_drain(dev, tr, fs, t_start, g_last, to)
            tr.accounting = "wall_clock"
            tr.lost = max(env_window - env_end, 0.0) if math.isfinite(env_end) else math.inf
            tr.expected = tr.delivered + tr.lost
            tr.allowed_lost = self._wall_clock_allowance(dev, tr.expected, fs, b, prm["jitter_s"])
            if tr.allowed_lost >= b and not tr.note:
                tr.note = ("duvar saati sayımı bu sürede tek tamponluk sessiz kaybı ayırt edemez "
                           f"(izin {tr.allowed_lost:.0f} ≥ {b} örnek)")
        elif tr.blocks:
            if hw_all and hw_first is not None:
                tr.accounting, tr.expected = "hw_timestamp", float(round((hw_end - hw_first) * 1e-9 * fs))
                tr.allowed_lost = HW_TS_ALLOWED_SAMPLES + HW_TS_ALLOWED_PPM * 1e-6 * tr.expected
            else:
                tr.accounting, tr.expected = "device_counter", float(int(dev.abs_index) - idx0)
                tr.allowed_lost = 0.0
            tr.lost = tr.expected - tr.delivered
        st = dev.stats
        tr.overflows, tr.timeouts, tr.discarded = int(st.overflows), int(st.timeouts), int(st.discarded_samples)
        tr.elapsed_s = max(t_end - t_start, 0.0)
        if tr.blocks and math.isfinite(tr.lost) and tr.expected > 0:
            tr.loss = tr.lost / tr.expected
        tr.judge()

    def _end_drain(self, dev: Any, tr: Trial, fs: float, t_start: float, g_last: float,
                   timeout_s: float) -> tuple[float, float]:
        """Read and discard after the window until the FIFO is empty (``PACED_READS``
        consecutive reads paced by production, ``Δg ≤ PACED_FRACTION·buffer``). Returns the
        upper envelope of ``g`` over the drain reads and the return time of the last one."""
        b = int(tr.buffer_size)
        max_reads = 4 * (int(dev.profile.kernel_buffers) + 2)
        env, g_prev, paced, t = -math.inf, g_last, 0, mono()
        for _ in range(max_reads):
            try:
                blk = dev.read(b, timeout_s)
            except StreamTimeout:
                tr.note = "pencere sonu boşaltmada zaman aşımı"
                return env, t
            t = mono()
            tr.delivered += blk.n
            tr.drain_reads += 1
            g = tr.delivered - fs * (t - t_start)
            env = max(env, g)
            paced = paced + 1 if g - g_prev <= PACED_FRACTION * b else 0
            g_prev = g
            if paced >= PACED_READS:
                return env, t
        tr.note = f"FIFO pencere sonunda {max_reads} okumada boşalmadı (bağlantı/tüketici yetişemiyor)"
        return env, t

    # ------------------------------------------------------------------ pipeline trial
    def _pipeline_trial(self, dev: Any, rate: float, buffer_size: int, prm: dict[str, Any],
                        refine: bool) -> Trial:
        """System-level trial: the real RxNode (stream → Welch → CFAR → ZMQ) at ``rate``."""
        from ...bus.pubsub import Publisher
        from ...rx.node import RxNode

        tr = Trial(mode="pipeline", rate_hz=float(rate), buffer_size=int(buffer_size), refine=refine)
        app = self.ctx.app
        prof = dev.profile
        block = min(int(app.rx.block_size), int(buffer_size))
        rx = dataclasses.replace(app.rx, sample_rate_hz=float(rate), buffer_size=int(buffer_size), block_size=block,
                                 bandwidth_hz=min(max(float(rate), prof.bw_min_hz), prof.bw_max_hz))
        node_app = dataclasses.replace(app, rx=rx, bus=dataclasses.replace(app.bus, mode="direct", ctl_endpoints=[],
                                                                           validate=False))
        endpoint = ("tcp://127.0.0.1:*" if prm["pipeline_transport"] == "tcp"
                    else f"inproc://eds-max-rate-{uuid.uuid4().hex}")
        if dev.streaming:
            dev.stop_stream()
        with contextlib.ExitStack() as stack:
            stack.callback(self._restore_ppm, dev, float(dev.ppm_hw))
            pub = Publisher(endpoint, bind=True, src=dev.name, validate=False, hwm=int(app.bus.hwm))
            stack.callback(pub.close)
            try:
                node = RxNode(node_app, device=dev, publisher=pub, cal=self.ctx.cal, name=dev.name)
            except ProfileViolation as e:
                tr.status, tr.note = "skipped", f"profil sınırı: {e}"
                return tr
            stack.callback(node.stop)
            node.start()
            self._observe_node(dev, node, tr, prm, block)
        tr.judge()
        return tr

    @staticmethod
    def _restore_ppm(dev: Any, ppm_hw: float) -> None:
        """Undo the node's hardware ppm correction (the next trial / measurement sees the
        device as the session configured it)."""
        if float(dev.ppm_hw) == ppm_hw:
            return
        try:
            dev.set_ppm_correction(ppm_hw)
        except EDSError as e:
            log.error("%s: donanım ppm düzeltmesi eski değerine (%+.4f) döndürülemedi: %s", dev.name, ppm_hw, e)

    def _observe_node(self, dev: Any, node: Any, tr: Trial, prm: dict[str, Any], block: int) -> None:
        fs = float(dev.fs)
        tr.actual_hz = fs
        b = int(tr.buffer_size)
        win = int(node.window_samples)
        first_timeout = max(5.0, 20.0 * max(win, block) / fs)
        deadline = mono() + first_timeout
        while node.stats["frames"] == 0:
            if mono() > deadline or node.state == "error":
                tr.note = f"RX düğümü {first_timeout:.1f} s içinde kare üretmedi (durum {node.state})"
                tr.timeouts = max(1, int(node.stats["stream"]["timeouts"]))
                return
            mono_sleep(POLL_S)
        mono_sleep(prm["warmup_s"])

        def snap() -> tuple[dict[str, Any], int | None, int, float]:
            s = node.stats
            c = getattr(dev, "abs_index", None)
            c = int(c) if isinstance(c, int) and not isinstance(c, bool) else None
            return s, c, int(dev.stats.samples) + int(dev.stats.discarded_samples), mono()

        s0, c0, n0, t0 = snap()
        fail_t: float | None = None
        keys = ("overflows", "timeouts", "host_drops", "discarded_samples")

        def bad(s: dict[str, Any]) -> bool:
            return (any(int(s["stream"][k]) > int(s0["stream"][k]) for k in keys)
                    or int(s["stream_errors"]) > int(s0["stream_errors"]) or s["state"] == "error")

        while True:
            now = mono()
            if now - t0 >= prm["duration_s"] or (fail_t is not None and now - fail_t >= prm["fail_window_s"]):
                break
            mono_sleep(min(POLL_S, max(prm["duration_s"] - (now - t0), 0.0) + 1e-3))
            if fail_t is None and bad(node.stats):
                fail_t = mono()
        s1, c1, n1, t1 = snap()
        d = {k: int(s1["stream"][k]) - int(s0["stream"][k]) for k in (*keys, "samples", "blocks")}
        tr.overflows, tr.timeouts, tr.host_drops = d["overflows"], d["timeouts"], d["host_drops"]
        tr.discarded = d["discarded_samples"]
        tr.timeouts += int(s1["stream_errors"]) - int(s0["stream_errors"])
        tr.delivered, tr.blocks = d["samples"], d["blocks"]
        tr.frames = int(s1["frames"]) - int(s0["frames"])
        tr.elapsed_s = t1 - t0
        tr.lag_ms = float(s1.get("last_proc_ms", math.nan))
        queue_blocks = int(self.ctx.app.rx.queue_blocks)
        backlog = d["samples"] - tr.frames * win
        tr.backlog_s = max(backlog, 0) / fs
        if int(s1["frame_errors"]) > int(s0["frame_errors"]):
            tr.note = f"{int(s1['frame_errors']) - int(s0['frame_errors'])} kare işlenemedi"
            tr.backlog_s = math.nan
        elif backlog > BACKLOG_QUEUE_FRACTION * queue_blocks * block + win:
            tr.note = (f"işleme gerçek zamana yetişemiyor: pencere sonunda {backlog} örnek ({1e3 * backlog / fs:.0f} ms) "
                       "okunmuş ama işlenmemiş")
            tr.backlog_s = math.nan
        if c0 is not None and c1 is not None:
            tr.accounting = "device_counter"
            tr.expected = float(c1 - c0)
            tr.lost = tr.expected - float(n1 - n0)
            tr.allowed_lost = float(block)
        else:
            tr.accounting = "wall_clock_snapshot"
            tr.expected = fs * tr.elapsed_s
            tr.lost = tr.expected - float(n1 - n0)
            tr.allowed_lost = ((int(dev.profile.kernel_buffers) + 2) * b
                               + float(dev.profile.ref_clock_ppm) * 1e-6 * tr.expected + prm["jitter_s"] * fs)
        if tr.expected > 0:
            tr.loss = tr.lost / tr.expected

    # ------------------------------------------------------------------ search
    def _scan_mode(self, dev: Any, mode: str, rates: list[float], buffer_size: int, prm: dict[str, Any],
                   implied_below: float | None = None) -> list[Trial]:
        """Ascending scan of one mode; stops after ``stop_after`` consecutive failures. Rates
        below ``implied_below`` are recorded as implied passes without streaming."""
        trials: list[Trial] = []
        streak = 0
        for rate in rates:
            if implied_below is not None and rate < implied_below:
                trials.append(Trial(mode=mode, rate_hz=float(rate), buffer_size=int(buffer_size), status="pass",
                                    implied=True, note="DSP hattı bu hızda geçti (aynı taşıma + ek tüketici yükü); "
                                                       "yalnız taşıma ayrıca ölçülmedi"))
                continue
            tr = self._trial(dev, mode, rate, buffer_size, prm)
            trials.append(tr)
            if tr.status == "skipped":
                continue
            streak = 0 if tr.passed else streak + 1
            if streak >= prm["stop_after"]:
                break
        return trials

    def _scan(self, dev: Any, modes: list[str], rates: list[float], buffer_size: int,
              prm: dict[str, Any]) -> dict[str, list[Trial]]:
        """Both modes: the pipeline is scanned first; every rate it passes below its first
        failure is an implied transport pass (identical device and transport, strictly less
        consumer load), so the transport scan starts at the pipeline's first failure."""
        trials: dict[str, list[Trial]] = {}
        implied: float | None = None
        if "pipeline" in modes:
            trials["pipeline"] = self._scan_mode(dev, "pipeline", rates, buffer_size, prm)
            if "transport" in modes:
                out = evaluate("pipeline", trials["pipeline"])
                if out.best is not None and out.monotonic:
                    implied = out.first_fail.rate_hz if out.first_fail is not None else math.inf
        if "transport" in modes:
            trials["transport"] = self._scan_mode(dev, "transport", rates, buffer_size, prm, implied)
        return {m: trials[m] for m in modes}

    def _refine(self, dev: Any, mode: str, trials: list[Trial], buffer_size: int, prm: dict[str, Any]) -> None:
        out = evaluate(mode, trials)
        if out.best is None or out.first_fail is None:
            return
        lo, hi = out.best.rate_hz, out.first_fail.rate_hz
        for _ in range(prm["refine_steps"]):
            mid = round(0.5 * (lo + hi) / REFINE_GRID_HZ) * REFINE_GRID_HZ
            if not lo < mid < hi:
                break
            tr = self._trial(dev, mode, mid, buffer_size, prm, refine=True)
            trials.append(tr)
            if tr.status == "skipped":
                break
            if tr.passed:
                lo = mid
            else:
                hi = mid

    def _restore(self, dev: Any, fs: float, bw: float, buffer_size: int) -> list[str]:
        errors: list[str] = []
        try:
            dev.stop_stream()
        except EDSError as e:
            errors.append(f"akış durdurulamadı: {e}")
        dev.buffer_size = int(buffer_size)
        for what, setter, v in (("örnekleme hızı", dev.set_sample_rate, fs), ("analog BW", dev.set_bandwidth, bw)):
            if v > 0:
                try:
                    setter(v)
                except EDSError as e:
                    errors.append(f"{what} {v:g} geri yüklenemedi: {e}")
        for e in errors:
            log.error("max_rate: %s: %s", dev.name, e)
        return errors

    # ------------------------------------------------------------------ run
    def run(self) -> MeasurementResult:
        dev = self.ctx.dut
        prm = self._params()
        nonrt = self._is_nonrealtime_sim(dev)
        if nonrt:
            log.warning(NONREALTIME_WARNING)
            if "pipeline" in prm["modes"]:
                prm["pipeline_skipped"] = ("gerçek zamanlı olmayan simülatörde DSP hattı ölçülemez (örnekler beklemeden "
                                           "üretilir, kuyruk her hızda taşar)")
                prm["modes"] = [m for m in prm["modes"] if m != "pipeline"] or ["transport"]
        orig = (float(dev.fs), float(dev.bw), int(dev.buffer_size))
        spectrum_fig: FigureSpec | None = None
        try:
            trials = self._scan(dev, prm["modes"], prm["rates"], prm["buffer_size"], prm)
            if prm["refine"]:
                for m in prm["modes"]:
                    self._refine(dev, m, trials[m], prm["buffer_size"], prm)
            outcomes = {m: evaluate(m, trials[m]) for m in prm["modes"]}
            matrix: list[dict[str, Any]] = []
            matrix_trials: list[Trial] = []
            if prm["matrix"]:
                for bsz in [prm["buffer_size"], *prm["matrix"]]:
                    if bsz == prm["buffer_size"] and "transport" in outcomes:
                        out = outcomes["transport"]
                    else:
                        mt = self._scan(dev, ["transport"], prm["rates"], bsz, prm)["transport"]
                        matrix_trials.extend(mt)
                        out = evaluate("transport", mt)
                    matrix.append({"buffer_size": bsz, "max_rate_msps": None if out.max_hz is None else out.max_hz / 1e6,
                                   "first_fail_msps": None if out.first_fail is None else out.first_fail.rate_hz / 1e6,
                                   "all_passed": out.all_passed})
            head = outcomes[self._head_mode(outcomes)]
            spec_rate = head.max_hz if head.max_hz is not None else prm["rates"][0]
            spectrum_fig = self._spectrum(dev, spec_rate, prm, self._head_mode(outcomes))
        finally:
            restore_errors = self._restore(dev, *orig)
        res = self._result(dev, prm, outcomes, matrix, matrix_trials, spectrum_fig, nonrt)
        if restore_errors:
            res.extra["restore_errors"] = restore_errors
        return res

    @staticmethod
    def _head_mode(outcomes: dict[str, ModeOutcome]) -> str:
        """The system-level pipeline value is the headline whenever it was measured."""
        return "pipeline" if "pipeline" in outcomes else "transport"

    def _spectrum(self, dev: Any, rate: float, prm: dict[str, Any], mode: str) -> FigureSpec | None:
        """Spectrum-analyzer screenshot of a capture at the headline rate."""
        try:
            self._setup(dev, rate, prm["buffer_size"])
        except ProfileViolation as e:
            log.warning("max_rate: ekran görüntüsü %.4g MS/s'de alınamadı: %s", rate / 1e6, e)
            return None
        try:
            spec = capture_spectrum(dev, prm["spectrum_nfft"], prm["spectrum_averages"], self.ctx.app.dsp.window,
                                    float(self.ctx.app.dsp.overlap))
        finally:
            dev.stop_stream()
        return spectrum_screenshot(spec, "max_rate_spectrum",
                                   f"Spektrum @ {rate / 1e6:.4g} MS/s (taşmasız en yüksek hız, {MODE_LABELS[mode]})")

    # ------------------------------------------------------------------ result
    def _result(self, dev: Any, prm: dict[str, Any], outcomes: dict[str, ModeOutcome], matrix: list[dict[str, Any]],
                matrix_trials: list[Trial], spectrum_fig: FigureSpec | None, nonrt: bool) -> MeasurementResult:
        prof, app = dev.profile, self.ctx.app
        bps = int(prof.bytes_per_sample)
        head_mode = self._head_mode(outcomes)
        head = outcomes[head_mode]
        fs_max = head.max_hz
        value = None if fs_max is None else fs_max / 1e6
        mb = None if fs_max is None else fs_max * bps / 1e6
        accounting = sorted({t.accounting for o in outcomes.values() for t in o.trials if t.accounting})
        ovf_src = str(getattr(dev, "overflow_source", "") or "")
        is_sim = getattr(dev.info, "backend", "") == "sim"
        no_overflow = ovf_src == "none" and not is_sim          # real hardware: loss not flagged by the device
        dev_args, ignored_args = device_args_conditions(dev)
        unresolvable = sorted({t.buffer_size for o in outcomes.values() for t in o.trials
                               if t.accounting == "wall_clock" and t.allowed_lost >= t.buffer_size})
        extra: dict[str, Any] = {
            "headline_mode": head_mode,
            "mb_per_s_at_max": mb,
            "usb_theoretical_pct": None if mb is None else 100.0 * mb * 1e6 / prof.interface_max_bytes_per_s,
            "usb_practical_pct": None if mb is None else 100.0 * mb * 1e6 / prof.interface_practical_bytes_per_s,
            "buffer_size": prm["buffer_size"], "interface": prof.interface, "bytes_per_sample": bps,
            "accounting": accounting, "duration_s": prm["duration_s"], "wall_clock_jitter_s": prm["jitter_s"],
            "excluded_rates_hz": prm["excluded_rates"], "overflow_source": ovf_src, "realtime": not nonrt,
        }
        for m, o in outcomes.items():
            extra[f"max_rate_{m}_msps"] = None if o.max_hz is None else o.max_hz / 1e6
            extra[f"first_fail_{m}_msps"] = None if o.first_fail is None else o.first_fail.rate_hz / 1e6
            extra[f"monotonic_{m}"] = o.monotonic
            extra[f"all_passed_{m}"] = o.all_passed
        implied = [t.rate_hz / 1e6 for o in outcomes.values() for t in o.trials if t.implied]
        if implied:
            extra["implied_transport_rates_msps"] = implied
        pipe = outcomes.get("pipeline")
        if pipe is not None and pipe.best is not None:
            extra["pipeline_lag_ms_at_max"] = pipe.best.lag_ms
            extra["pipeline_frames_at_max"] = pipe.best.frames
        if prm["pipeline_skipped"]:
            extra["pipeline_skipped"] = prm["pipeline_skipped"]
        if unresolvable:
            extra["wall_clock_unresolvable_buffer_sizes"] = unresolvable
        if matrix:
            extra["matrix"] = matrix
        if nonrt:
            extra["warning"] = NONREALTIME_WARNING

        conditions = base_conditions(dev)
        conditions.update({
            "fs_hz": fs_max, "bw_hz": None if fs_max is None else min(max(fs_max, prof.bw_min_hz), prof.bw_max_hz),
            "headline_mode": head_mode, "rates_hz": prm["rates"], "duration_s": prm["duration_s"],
            "buffer_size": prm["buffer_size"], "modes": prm["modes"], "stop_after_failures": prm["stop_after"],
            "refine": prm["refine"], "refine_steps": prm["refine_steps"], "interface": prof.interface,
            "interface_max_bytes_per_s": prof.interface_max_bytes_per_s,
            "interface_practical_bytes_per_s": prof.interface_practical_bytes_per_s, "bytes_per_sample": bps,
            "stream_format": app.rx.stream_format, "device_args": dev_args,
            "accounting": ", ".join(ACCOUNTING_LABELS.get(a, a) for a in accounting) or "-",
            "overflow_source": ovf_src, "wall_clock_jitter_s": prm["jitter_s"],
            "host": platform.node() or "-", "host_platform": platform.platform(), "cpu_count": os.cpu_count() or 0,
        })
        if ignored_args:
            conditions["device_args_ignored"] = ignored_args
        if "pipeline" in prm["modes"]:
            conditions.update({"block_size": min(int(app.rx.block_size), prm["buffer_size"]),
                               "queue_blocks": int(app.rx.queue_blocks), "nfft": int(app.dsp.nfft),
                               "averages": int(app.dsp.averages), "detect_method": app.detect.method,
                               "pfa": float(app.detect.pfa), "spectrum_rate_hz": float(app.bus.spectrum_rate_hz),
                               "pipeline_transport": prm["pipeline_transport"]})

        def fmt(o: ModeOutcome | None) -> str:
            if o is None:
                return "ölçülmedi"
            if o.max_hz is None:
                low = min((t.rate_hz for t in o.trials if t.status != "skipped"), default=math.nan)
                return f"hiçbir hız geçmedi (< {low / 1e6:.4g} MS/s)"
            s = f"{o.max_hz / 1e6:.4g} MS/s ({o.max_hz * bps / 1e6:.1f} MB/s)"
            if o.all_passed:
                s += " — aday listesinin sonu, gerçek sınır daha yüksek olabilir"
            if not o.monotonic:
                s += " — ilk başarısızlığın üstünde geçen hız var (kararsız)"
            return s

        crit = (f"{prm['duration_s']:g} s sürekli akışta sıfır overflow, sıfır zaman aşımı"
                + (", sıfır host drop" if "pipeline" in prm["modes"] else "") + " ve örnek sayımında kayıpsız")
        if head_mode == "pipeline":
            summary = (f"Sistem düzeyi (DSP hattı açık: RxNode akış → Welch → CFAR → ZMQ yayını) taşmasız en yüksek "
                       f"örnekleme hızı {fmt(pipe)}; yalnız taşımada {fmt(outcomes.get('transport'))}")
        else:
            summary = (f"Yalnız taşıma (oku-at; DSP hattı ölçülmedi) taşmasız en yüksek örnekleme hızı "
                       f"{fmt(outcomes.get('transport'))}")
        summary += (f" — {crit}; buffer_size {prm['buffer_size']}, {prof.interface}"
                    + (f", teorik sınırın %{extra['usb_theoretical_pct']:.0f}'i" if mb is not None else "") + ".")
        if prm["pipeline_skipped"]:
            summary += f" DSP hattı: {prm['pipeline_skipped']}."
        if is_sim and not nonrt:
            usb = getattr(dev, "sim_params", {}).get("usb_bytes_per_s") if hasattr(dev, "sim_params") else None
            summary += (" Simülatör: taşıma sınırı yapılandırılmış USB modelinden gelir"
                        + (f" (usb_bytes_per_s = {float(usb) / 1e6:g} MB/s)" if usb else "") + ".")
        if no_overflow:
            summary += f" UYARI: {BLOCKER_NO_OVERFLOW} — hedef kararı verilmez."
        elif ovf_src == "none":
            summary += " UYARI: bu cihazda overflow bayrağı yok; kayıp yalnız örnek sayımıyla aranır."
        if ignored_args:
            summary += (f" Sürücünün yok saydığı cihaz argümanları ölçüm koşulu değildir: {', '.join(ignored_args)}.")
        if unresolvable:
            summary += (f" UYARI: buffer_size {unresolvable} için duvar saati sayımı tek tamponluk sessiz kaybı ayırt "
                        "edemez (süreyi kısaltın ya da tamponu büyütün).")
        if nonrt:
            summary = f"{NONREALTIME_WARNING} {summary}"
        acc_tr = ", ".join(ACCOUNTING_LABELS.get(a, a) for a in accounting) or "duvar saati"
        method = (
            "Başlık değeri sistem düzeyidir: her aday hızda yarışma boru hattı (RxNode: okuyucu iş parçacığı + host "
            f"kuyruğu ({int(app.rx.queue_blocks)} blok) → Welch PSD ({int(app.dsp.nfft)} nokta, {int(app.dsp.averages)} "
            f"ortalama) → CFAR ({app.detect.method}) → rx.detections/rx.spectrum ZMQ yayını) DUT üzerinde "
            f"{prm['duration_s']:g} s çalıştırılır; overflow, zaman aşımı, host kuyruğu düşürmesi sayılır ve pencere "
            "sonunda okunmuş ama işlenmemiş örnek birikimi kuyruğun yarısı + bir pencereyi aşmamalıdır. "
            if "pipeline" in prm["modes"] else
            "Yalnız taşıma modu ölçüldü (DSP hattı yok; KTR için sistem düzeyi değer gerekir). "
        ) + (
            "Yalnız taşıma modunda akış yeniden başlatılır, settle tamponları ve birikmiş FIFO içeriği atılır, "
            f"ardından {prm['duration_s']:g} s oku-at yapılır. Örnek sayımı çapraz kontrolü ({acc_tr}): kayıp = "
            "beklenen − teslim edilen; duvar saatinde g = teslim − fs·geçen süre dizisinin üst zarfı kullanılır ve "
            "pencere sonunda FIFO boşaltılır (kuyrukta kalan tampon kayıp sayılmaz); izin = max(tampon/2, "
            f"TCXO ±{float(prof.ref_clock_ppm):g} ppm × beklenen + {prm['jitter_s'] * 1e3:g} ms × fs), sessiz kayıp tam "
            "tampondur. fs_max artan taramada ilk başarısızlıktan önceki en yüksek geçen hızdır"
            + ("; önce DSP hattı taranır, hattın geçtiği hızlar yalnız taşıma için de geçmiş sayılır (aynı taşıma, "
               "daha az yük)" if len(prm["modes"]) > 1 else "")
            + (f"; son geçen ile ilk kalan arasında {prm['refine_steps']} adımlı ikiye bölme ile inceltilir." if
               prm["refine"] else ".")
            + f" Overflow kaynağı: {ovf_src or 'cihaz belirtmedi (sürücü bayrakları)'}."
        )
        res = self.new_result(value, summary=summary, method=method, conditions=conditions, extra=extra)
        if nonrt:
            res.blockers.append(REASON_NONREALTIME)
        if head_mode != "pipeline":
            res.blockers.append(BLOCKER_TRANSPORT_ONLY)
        if no_overflow:
            res.blockers.append(BLOCKER_NO_OVERFLOW)
        all_trials = [t for o in outcomes.values() for t in o.trials] + matrix_trials
        res.table = [t.row(bps) for t in sorted(all_trials, key=lambda t: (t.buffer_size != prm["buffer_size"],
                                                                         t.buffer_size, MODES.index(t.mode),
                                                                         t.rate_hz))]
        res.figures = [self._loss_figure(outcomes, prm), self._throughput_figure(outcomes, prof)]
        if spectrum_fig is not None:
            res.figures.append(spectrum_fig)
        if matrix:
            res.figures.append(self._matrix_figure(matrix))
        return res

    @staticmethod
    def _loss_figure(outcomes: dict[str, ModeOutcome], prm: dict[str, Any]) -> FigureSpec:
        """Per rate: lost samples (count cross-check) with their allowance, and the stream
        failure events (overflow + timeout + host drop)."""
        series: list[Series] = []
        pas_x: list[float] = []
        pas_y: list[float] = []
        fail_x: list[float] = []
        fail_y: list[float] = []
        vlines: list[tuple[float, str]] = []
        for m, o in outcomes.items():
            ts = sorted((t for t in o.trials if t.status != "skipped" and not t.implied), key=lambda t: t.rate_hz)
            if not ts:
                continue
            x = [t.rate_hz / 1e6 for t in ts]
            lost = [max(t.lost, PLOT_FLOOR) if math.isfinite(t.lost) else PLOT_FLOOR for t in ts]
            allowed = [max(t.allowed_lost, PLOT_FLOOR) if math.isfinite(t.allowed_lost) else PLOT_FLOOR for t in ts]
            ev = [max(float(t.overflows + t.timeouts + t.host_drops), PLOT_FLOOR) for t in ts]
            series.append(Series(x=x, y=lost, label=f"kayıp örnek — {MODE_LABELS[m]}", style="line"))
            series.append(Series(x=x, y=allowed, label=f"sayım izni [örnek] — {MODE_LABELS[m]}", style="step",
                                 role="ref"))
            series.append(Series(x=x, y=ev, label=f"overflow + zaman aşımı + host drop [olay] — {MODE_LABELS[m]}",
                                 style="marker", role="warn"))
            for t, xi, yl, yo in zip(ts, x, lost, ev):
                if t.passed:
                    pas_x.append(xi)
                    pas_y.append(yl)
                else:
                    fail_x.append(xi)
                    fail_y.append(max(yl, yo))
            if o.max_hz is not None:
                vlines.append((o.max_hz / 1e6, f"fs_max {MODE_LABELS[m]} = {o.max_hz / 1e6:.4g} MS/s"))
        series.append(Series(x=pas_x, y=pas_y, label="GEÇTİ", style="marker", role="pass"))
        series.append(Series(x=fail_x, y=fail_y, label="KALDI", style="marker", role="fail"))
        top = max((y for sr in series for y in sr.y), default=PLOT_FLOOR)
        return FigureSpec(
            name="max_rate_overflow_loss", ylim=(0.5 * PLOT_FLOOR, max(10.0, 3.0 * top)),
            title=f"{TITLE} — olaylar ve örnek sayımı (hız başına)",
            kind="xy", series=series, xlabel="Örnekleme hızı [MS/s]", ylabel="Olay sayısı / kayıp örnek",
            hlines=[(PLOT_FLOOR, "0 (olay/kayıp yok)"), (1.0, "1 olay / 1 örnek")], vlines=vlines, logy=True,
            info={"Süre": f"{prm['duration_s']:g} s / hız", "buffer_size": str(prm["buffer_size"])},
        )

    @staticmethod
    def _throughput_figure(outcomes: dict[str, ModeOutcome], prof: Any) -> FigureSpec:
        bps = int(prof.bytes_per_sample)
        rates = sorted({t.rate_hz for o in outcomes.values() for t in o.trials})
        series = [Series(x=[r / 1e6 for r in rates], y=[r * bps / 1e6 for r in rates],
                         label=f"gerekli veri hızı (fs × {bps} B)", style="line", role="ref")]
        groups = (("GEÇTİ", "pass", lambda t: t.passed and not t.implied),
                  ("GEÇTİ (DSP hattından çıkarım)", "pass", lambda t: t.implied),
                  ("KALDI", "fail", lambda t: not t.passed))
        for mode, o in outcomes.items():
            ts = [t for t in o.trials if t.status != "skipped"]
            for word, role, pick in groups:
                sel = sorted((t for t in ts if pick(t)), key=lambda t: t.rate_hz)
                if sel:
                    series.append(Series(x=[t.rate_hz / 1e6 for t in sel], y=[t.rate_hz * bps / 1e6 for t in sel],
                                         label=f"{word} — {MODE_LABELS[mode]}", style="marker", role=role))
        vlines = [(o.max_hz / 1e6, f"fs_max {MODE_LABELS[m]} = {o.max_hz / 1e6:.4g} MS/s")
                  for m, o in outcomes.items() if o.max_hz is not None]
        theo = prof.interface_max_bytes_per_s / 1e6
        prac = prof.interface_practical_bytes_per_s / 1e6
        top = max([theo, *(r * bps / 1e6 for r in rates)])
        return FigureSpec(
            name="max_rate_throughput",
            title=f"Veri hızı – örnekleme hızı ({prof.interface}: teorik / pratik sınırlar)",
            kind="xy", series=series, xlabel="Örnekleme hızı [MS/s]", ylabel="Veri hızı [MB/s]",
            hlines=[(theo, f"{prof.interface} teorik {theo:.1f} MB/s"), (prac, f"{prof.interface} pratik {prac:.1f} MB/s")],
            vlines=vlines, ylim=(0.0, 1.1 * top),
        )

    @staticmethod
    def _matrix_figure(matrix: list[dict[str, Any]]) -> FigureSpec:
        rows = [r for r in matrix if r["max_rate_msps"] is not None]
        return FigureSpec(
            name="max_rate_buffer_matrix", title=f"{TITLE} – buffer_size (yalnız taşıma)",
            kind="xy", series=[Series(x=[float(r["buffer_size"]) for r in rows], y=[r["max_rate_msps"] for r in rows],
                                      label="fs_max", style="bar")],
            xlabel="buffer_size [örnek]", ylabel="fs_max [MS/s]", logx=True,
        )
