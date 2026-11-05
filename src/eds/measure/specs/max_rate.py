"""Taşma-sız maksimum örnekleme hızı [MS/s] — highest sustainable continuous sample rate.

Definition (docs/MEASUREMENTS.md section 8)::

    fs_max = max { fs : continuous stream of T s with
                        overflow events = 0, read timeouts = 0 and sample loss < 1e-4 }
    loss   = 1 − delivered / expected
    data   = fs · bytes_per_sample   (CS16: 4 B → USB 2.0 theoretical bulk 53.248 MB/s)

Modes
-----
``transport``  read-and-discard: USB + libiio + host only.
``pipeline``   the reading loop also computes a ``pipeline_nfft`` (4096) point Welch power
               spectrum of every block with the ``[dsp]`` window/overlap — the per-block
               load of the RX node (``pipeline_detect = true`` adds the CFAR as well).

Procedure per (mode, rate)
--------------------------
1. Stream stopped, ``set_sample_rate(rate)``, analog BW = rate clamped to the profile's BW
   limits (:class:`~eds.core.errors.ProfileViolation` → point skipped and noted), stream
   started with ``buffer_size``; the settle window (``(kernel_buffers + 1) × buffer_size``)
   is discarded.
2. Backlog drain: buffers are read until one read had to WAIT for production (≥ half a
   buffer period; at most ``kernel_buffers + 2`` reads), so the window does not start with
   samples that were queued before it.
3. Continuous reads for ``duration_s``. Counters: overflow events and read timeouts since the
   stream start (``device.stats`` deltas), samples delivered, samples the HAL discarded
   around a mid-block overflow. After the first failure sign the window is cut to
   ``fail_window_s`` (the rate has failed; the extra time only quantifies how badly).
4. Expected sample count, best available clock:
   * ``hw_timestamp``   — device timestamps of the first block and the end of the last one;
   * ``device_counter`` — the device's absolute sample counter (``abs_index``, gaps
     included; the simulator, whose production is frozen while it synthesizes, so its
     wall-clock rate is not the sample clock);
   * ``wall_clock``     — ``fs × (t_last_read − t_window_start)`` (perf_counter).
   PASS ⇔ overflows = 0, timeouts = 0 and loss < ``loss_tolerance`` (1e-4). The tolerance
   covers the TCXO rate error (±25 ppm = 2.5e-5) and the jitter of the read-return times
   of the wall-clock method (1e-4 × 5 s = 0.5 ms); one lost buffer is ≥ 1e-2 at these
   durations, i.e. orders of magnitude above it.

Search: candidate rates ascending (``profile.candidate_sample_rates_hz`` inside the
profile/runtime limits; ``--quick``: 1, 2.4, 4, 5, 6, 8 MS/s). A mode stops after
``stop_after_failures`` consecutive failures. ``fs_max`` = highest rate that passed below the
first failure (a pass above a failure is reported as non-monotonic, never as the value);
with ``refine`` the gap between the last pass and the first failure is bisected
``refine_steps`` times (1 kS/s grid). Headline value: ``fs_max`` of the transport mode.

Hardware tuning matrix: ``buffer_sizes = [...]`` repeats the transport search for every
listed buffer size (``extra['matrix']``). Device arguments that only act when the device is
opened (e.g. SoapyPlutoSDR ``usb_direct``, ``timeout``, ``loopback``) cannot be switched by a
running measurement: run it once per ``[[device]].args`` variant — the arguments in effect
are recorded in ``conditions['device_args']``.
"""

from __future__ import annotations

import logging
import math
import os
import platform
from dataclasses import dataclass
from typing import Any

from ...core.errors import ConfigError, EDSError, ProfileViolation, StreamTimeout
from ...core.timeutil import mono
from ...dsp.detect import detect
from ...dsp.psd import welch
from ...dsp.stats import effective_averages
from ..base import FigureSpec, Measurement, MeasurementResult, Series, register
from ..common import base_conditions, capture_spectrum, spectrum_screenshot

log = logging.getLogger(__name__)

MODES: tuple[str, ...] = ("transport", "pipeline")
MODE_LABELS: dict[str, str] = {"transport": "yalnız taşıma", "pipeline": "DSP hattı açık"}
ACCOUNTING_LABELS: dict[str, str] = {
    "hw_timestamp": "donanım zaman damgası", "device_counter": "cihaz örnek sayacı",
    "wall_clock": "duvar saati (fs × geçen süre)",
}
QUICK_RATES_HZ: tuple[float, ...] = (1e6, 2.4e6, 4e6, 5e6, 6e6, 8e6)
LOSS_TOLERANCE = 1e-4
#: Rates of the refinement bisection are rounded to this grid (S/s).
REFINE_GRID_HZ = 1e3
#: Zero counts are drawn at this level on the logarithmic overflow/loss plot.
PLOT_FLOOR = 0.1
#: A drain read that waited at least this fraction of a buffer period found the FIFO empty.
DRAIN_WAIT_FRACTION = 0.5
NONREALTIME_WARNING = ("UYARI: gerçek zamanlı olmayan simülatör (sim.realtime = false) — örnekler beklemeden üretilir, "
                       "USB/FIFO modeli devre dışıdır; ölçülen hız fiziksel olarak ANLAMLI DEĞİLDİR.")
STATUS_TR = {"pass": "GEÇTİ", "fail": "KALDI", "skipped": "ATLANDI"}


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
    discarded: int = 0
    settle_discarded: int = 0
    delivered: int = 0
    expected: float = math.nan
    loss: float = math.nan
    elapsed_s: float = 0.0
    blocks: int = 0
    accounting: str = ""
    dsp_ms: float = math.nan             # mean per-block pipeline processing time
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
            "overflows": self.overflows, "timeouts": self.timeouts,
            "discarded_samples": self.discarded, "settle_discarded_samples": self.settle_discarded,
            "delivered_samples": self.delivered, "expected_samples": self.expected, "loss": self.loss,
            "loss_ppm": self.loss * 1e6 if math.isfinite(self.loss) else math.nan, "elapsed_s": self.elapsed_s,
            "blocks": self.blocks, "mb_per_s": self.rate_hz * bytes_per_sample / 1e6, "dsp_ms": self.dsp_ms,
            "accounting": self.accounting, "note": self.note,
        }
        if self.implied:
            for k in ("fs_hz", "overflows", "timeouts", "discarded_samples", "settle_discarded_samples",
                      "delivered_samples", "expected_samples", "loss", "loss_ppm", "elapsed_s", "blocks"):
                row[k] = None
        return row


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
    title = "Taşma-sız maksimum örnekleme hızı"
    unit = "MS/s"
    requires = frozenset()
    default_params = {
        "rates": None,                    # None = profile.candidate_sample_rates_hz (quick: QUICK_RATES_HZ)
        "duration_s": 5.0,
        "quick_duration_s": 0.6,
        "fail_window_s": 1.0,             # observation kept after the first failure sign
        "quick_fail_window_s": 0.3,
        "modes": list(MODES),
        "buffer_size": None,              # None = [rx] buffer_size
        "buffer_sizes": [],               # optional tuning matrix (transport mode)
        "stop_after_failures": 2,
        "refine": True,
        "quick_refine": False,
        "refine_steps": 3,
        "loss_tolerance": LOSS_TOLERANCE,
        "pipeline_nfft": 4096,
        "pipeline_detect": False,
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
        nfft = _int(p["pipeline_nfft"], "pipeline_nfft", 16)
        if "pipeline" in modes and bsize < nfft:
            raise ConfigError(f"max_rate: buffer_size ({bsize}) DSP hattı için pipeline_nfft'ten ({nfft}) küçük olamaz")
        for key in ("quick_refine" if quick else "refine", "pipeline_detect"):
            if not isinstance(p[key], bool):
                raise ConfigError(f"max_rate.{key} true/false olmalı, {p[key]!r} geldi")
        prm = {
            "modes": modes, "buffer_size": bsize, "matrix": [b for b in matrix if b != bsize],
            "duration_s": _finite(p["quick_duration_s"] if quick else p["duration_s"], "duration_s", 0.05),
            "fail_window_s": _finite(p["quick_fail_window_s"] if quick else p["fail_window_s"], "fail_window_s", 0.0),
            "stop_after": _int(p["stop_after_failures"], "stop_after_failures", 1),
            "refine": p["quick_refine"] if quick else p["refine"],
            "refine_steps": _int(p["refine_steps"], "refine_steps", 0),
            "loss_tolerance": _finite(p["loss_tolerance"], "loss_tolerance", 0.0),
            "pipeline_nfft": nfft, "pipeline_detect": p["pipeline_detect"],
            "timeout_s": None if p["timeout_s"] is None else _finite(p["timeout_s"], "timeout_s", 0.01),
            "spectrum_nfft": _int(p["spectrum_nfft"], "spectrum_nfft", 16),
            "spectrum_averages": _int(p["spectrum_averages"], "spectrum_averages", 1),
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

    # ------------------------------------------------------------------ one trial
    def _setup(self, dev: Any, rate: float, buffer_size: int) -> None:
        prof = dev.profile
        if dev.streaming:
            dev.stop_stream()
        dev.set_sample_rate(rate)
        dev.set_bandwidth(min(max(rate, prof.bw_min_hz), prof.bw_max_hz))
        dev.start_stream(buffer_size)

    def _trial(self, dev: Any, mode: str, rate: float, buffer_size: int, prm: dict[str, Any],
               refine: bool = False) -> Trial:
        tr = Trial(mode=mode, rate_hz=float(rate), buffer_size=int(buffer_size), refine=refine)
        try:
            self._setup(dev, rate, buffer_size)
        except ProfileViolation as e:
            tr.status, tr.note = "skipped", f"profil sınırı: {e}"
            log.warning("max_rate %s %.4g MS/s atlandı: %s", mode, rate / 1e6, e)
            return tr
        try:
            self._measure(dev, tr, prm)
        finally:
            dev.stop_stream()
        log.info("max_rate %-9s %8.4g MS/s buf %d: %s (overflow %d, timeout %d, kayıp %.3g, %s)", mode, rate / 1e6,
                 buffer_size, STATUS_TR[tr.status], tr.overflows, tr.timeouts, tr.loss, tr.accounting)
        return tr

    def _measure(self, dev: Any, tr: Trial, prm: dict[str, Any]) -> None:
        app = self.ctx.app
        fs = float(dev.fs)
        tr.actual_hz = fs
        b = int(tr.buffer_size)
        blk_dur = b / fs
        to = prm["timeout_s"] if prm["timeout_s"] is not None else max(float(app.rx.read_timeout_s), 4.0 * blk_dur)
        pipeline = tr.mode == "pipeline"
        nfft, window, overlap = prm["pipeline_nfft"], app.dsp.window, float(app.dsp.overlap)
        n_avg = effective_averages(max(1, (b - nfft) // max(1, int(round(nfft * (1 - overlap)))) + 1), overlap,
                                   window, nfft) if pipeline and prm["pipeline_detect"] else None
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
        dsp_s = 0.0
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
            hw = blk.meta.get("hw_time_ns")
            if hw is None:
                hw_all = False
            else:
                hw_first = int(hw) if hw_first is None else hw_first
                hw_end = float(hw) + blk.n / blk.fs * 1e9
            if pipeline:
                tp = mono()
                spec = welch(blk.samples[0], blk.fs, nfft, window, overlap, "mean", fc=blk.fc, t_ns=blk.t_ns)
                if n_avg is not None:
                    detect(spec, app.detect, n_avg=n_avg)
                dsp_s += mono() - tp
        st = dev.stats
        tr.overflows, tr.timeouts, tr.discarded = int(st.overflows), int(st.timeouts), int(st.discarded_samples)
        tr.elapsed_s = max(t_end - t_start, 0.0)
        if pipeline and tr.blocks:
            tr.dsp_ms = 1e3 * dsp_s / tr.blocks
        if tr.blocks:
            if hw_all and hw_first is not None:
                tr.accounting, tr.expected = "hw_timestamp", float(round((hw_end - hw_first) * 1e-9 * fs))
            elif use_counter:
                tr.accounting, tr.expected = "device_counter", float(int(dev.abs_index) - idx0)
            else:
                tr.accounting, tr.expected = "wall_clock", fs * tr.elapsed_s
            tr.loss = 1.0 - tr.delivered / tr.expected if tr.expected > 0 else math.nan
        ok = (tr.blocks > 0 and tr.overflows == 0 and tr.timeouts == 0 and math.isfinite(tr.loss)
              and tr.loss < prm["loss_tolerance"])
        tr.status = "pass" if ok else "fail"

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
            head = outcomes.get("transport") or outcomes[prm["modes"][0]]
            spec_rate = head.max_hz if head.max_hz is not None else prm["rates"][0]
            spectrum_fig = self._spectrum(dev, spec_rate, prm)
        finally:
            restore_errors = self._restore(dev, *orig)
        res = self._result(dev, prm, outcomes, matrix, matrix_trials, spectrum_fig, nonrt)
        if restore_errors:
            res.extra["restore_errors"] = restore_errors
        return res

    def _spectrum(self, dev: Any, rate: float, prm: dict[str, Any]) -> FigureSpec | None:
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
                                   f"Spektrum @ {rate / 1e6:.4g} MS/s (taşma-sız en yüksek hız, yalnız taşıma)")

    # ------------------------------------------------------------------ result
    def _result(self, dev: Any, prm: dict[str, Any], outcomes: dict[str, ModeOutcome], matrix: list[dict[str, Any]],
                matrix_trials: list[Trial], spectrum_fig: FigureSpec | None, nonrt: bool) -> MeasurementResult:
        prof, app = dev.profile, self.ctx.app
        bps = int(prof.bytes_per_sample)
        head_mode = "transport" if "transport" in outcomes else prm["modes"][0]
        head = outcomes[head_mode]
        fs_max = head.max_hz
        value = None if fs_max is None else fs_max / 1e6
        mb = None if fs_max is None else fs_max * bps / 1e6
        accounting = sorted({t.accounting for o in outcomes.values() for t in o.trials if t.accounting})
        extra: dict[str, Any] = {
            "headline_mode": head_mode,
            "mb_per_s_at_max": mb,
            "usb_theoretical_pct": None if mb is None else 100.0 * mb * 1e6 / prof.interface_max_bytes_per_s,
            "usb_practical_pct": None if mb is None else 100.0 * mb * 1e6 / prof.interface_practical_bytes_per_s,
            "buffer_size": prm["buffer_size"], "interface": prof.interface, "bytes_per_sample": bps,
            "accounting": accounting, "duration_s": prm["duration_s"], "loss_tolerance": prm["loss_tolerance"],
            "excluded_rates_hz": prm["excluded_rates"],
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
            extra["pipeline_dsp_ms_at_max"] = pipe.best.dsp_ms
        if matrix:
            extra["matrix"] = matrix
        if nonrt:
            extra["warning"] = NONREALTIME_WARNING

        conditions = base_conditions(dev)
        conditions.update({
            "fs_hz": fs_max, "bw_hz": None if fs_max is None else min(max(fs_max, prof.bw_min_hz), prof.bw_max_hz),
            "rates_hz": prm["rates"], "duration_s": prm["duration_s"], "buffer_size": prm["buffer_size"],
            "modes": prm["modes"], "loss_tolerance": prm["loss_tolerance"], "stop_after_failures": prm["stop_after"],
            "refine": prm["refine"], "refine_steps": prm["refine_steps"], "pipeline_nfft": prm["pipeline_nfft"],
            "pipeline_detect": prm["pipeline_detect"], "interface": prof.interface,
            "interface_max_bytes_per_s": prof.interface_max_bytes_per_s,
            "interface_practical_bytes_per_s": prof.interface_practical_bytes_per_s, "bytes_per_sample": bps,
            "stream_format": app.rx.stream_format, "device_args": dict(getattr(dev.cfg, "args", {}) or {}),
            "accounting": ", ".join(ACCOUNTING_LABELS.get(a, a) for a in accounting) or "-",
            "host": platform.node() or "-", "host_platform": platform.platform(), "cpu_count": os.cpu_count() or 0,
        })

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

        summary = (
            f"{prm['duration_s']:g} s sürekli akışta sıfır overflow, sıfır zaman aşımı ve < {prm['loss_tolerance']:g} "
            f"örnek kaybıyla en yüksek örnekleme hızı yalnız taşımada {fmt(outcomes.get('transport'))}, DSP hattı "
            f"açıkken {fmt(outcomes.get('pipeline'))}; buffer_size {prm['buffer_size']}, {prof.interface}"
            + (f", teorik sınırın %{extra['usb_theoretical_pct']:.0f}'i" if mb is not None else "") + "."
        )
        if nonrt:
            summary = f"{NONREALTIME_WARNING} {summary}"
        acc_tr = ", ".join(ACCOUNTING_LABELS.get(a, a) for a in accounting) or "duvar saati"
        method = (
            "Her aday hızda akış yeniden başlatılır, settle tamponları ve birikmiş FIFO içeriği atılır, ardından "
            f"{prm['duration_s']:g} s sürekli okunur (DSP hattı modunda her blok için {prm['pipeline_nfft']} noktalı "
            "Welch PSD hesaplanır). Overflow olayları ve zaman aşımları sayılır; örnek kaybı = 1 − teslim/beklenen, "
            f"beklenen örnek sayısı {acc_tr} ile bulunur. GEÇTİ ⇔ overflow = 0, zaman aşımı = 0 ve kayıp < "
            f"{prm['loss_tolerance']:g} (tolerans: TCXO ±25 ppm hız hatası ve geçen süre ölçümündeki ~0.5 ms zaman "
            "sapması); fs_max artan taramada ilk başarısızlıktan önceki en yüksek geçen hızdır"
            + ("; önce DSP hattı taranır, hattın geçtiği hızlar yalnız taşıma için de geçmiş sayılır (aynı taşıma, "
               "daha az yük) ve taşıma taraması hattın ilk başarısız hızından başlar" if len(prm["modes"]) > 1 else "")
            + (f", son geçen ile ilk kalan arasında {prm['refine_steps']} adımlı ikiye bölme ile inceltilir." if
               prm["refine"] else ".")
        )
        res = self.new_result(value, summary=summary, method=method, conditions=conditions, extra=extra)
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
            loss = [max(t.loss * 1e6, PLOT_FLOOR) if math.isfinite(t.loss) else PLOT_FLOOR for t in ts]
            ovf = [max(float(t.overflows + t.timeouts), PLOT_FLOOR) for t in ts]
            series.append(Series(x=x, y=loss, label=f"örnek kaybı [ppm] — {MODE_LABELS[m]}", style="line"))
            series.append(Series(x=x, y=ovf, label=f"overflow + zaman aşımı [olay] — {MODE_LABELS[m]}",
                                 style="marker"))
            for t, xi, yl, yo in zip(ts, x, loss, ovf):
                if t.passed:
                    pas_x.append(xi)
                    pas_y.append(yl)
                else:
                    fail_x.append(xi)
                    fail_y.append(max(yl, yo))
            if o.max_hz is not None:
                vlines.append((o.max_hz / 1e6, f"fs_max {MODE_LABELS[m]} = {o.max_hz / 1e6:.4g} MS/s"))
        series.append(Series(x=pas_x, y=pas_y, label="GEÇTİ", style="marker"))
        series.append(Series(x=fail_x, y=fail_y, label="KALDI", style="marker"))
        tol_ppm = prm["loss_tolerance"] * 1e6
        return FigureSpec(
            name="max_rate_overflow_loss",
            title="Taşma-sız maksimum hız — overflow olayları ve örnek kaybı (hız başına)",
            kind="xy", series=series, xlabel="Örnekleme hızı [MS/s]", ylabel="Olay sayısı / kayıp [ppm]",
            hlines=[(tol_ppm, f"kayıp toleransı {tol_ppm:g} ppm"), (PLOT_FLOOR, "0 (olay/kayıp yok)")],
            vlines=vlines, logy=True,
            info={"Süre": f"{prm['duration_s']:g} s / hız", "buffer_size": str(prm["buffer_size"])},
        )

    @staticmethod
    def _throughput_figure(outcomes: dict[str, ModeOutcome], prof: Any) -> FigureSpec:
        bps = int(prof.bytes_per_sample)
        rates = sorted({t.rate_hz for o in outcomes.values() for t in o.trials})
        series = [Series(x=[r / 1e6 for r in rates], y=[r * bps / 1e6 for r in rates],
                         label=f"gerekli veri hızı (fs × {bps} B)", style="line")]
        head = outcomes.get("transport") or next(iter(outcomes.values()))
        ts = [t for t in head.trials if t.status != "skipped"]
        groups = (("GEÇTİ", lambda t: t.passed and not t.implied),
                  ("GEÇTİ (DSP hattından çıkarım)", lambda t: t.implied),
                  ("KALDI", lambda t: not t.passed))
        for word, pick in groups:
            sel = sorted((t for t in ts if pick(t)), key=lambda t: t.rate_hz)
            if sel:
                series.append(Series(x=[t.rate_hz / 1e6 for t in sel], y=[t.rate_hz * bps / 1e6 for t in sel],
                                     label=f"{word} — {MODE_LABELS[head.mode]}", style="marker"))
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
            name="max_rate_buffer_matrix", title="Taşma-sız maksimum hız – buffer_size (yalnız taşıma)",
            kind="xy", series=[Series(x=[float(r["buffer_size"]) for r in rows], y=[r["max_rate_msps"] for r in rows],
                                      label="fs_max", style="bar")],
            xlabel="buffer_size [örnek]", ylabel="fs_max [MS/s]", logx=True,
        )
