"""RX node service: device → stream → Welch PSD → CFAR → ZeroMQ bus (the competition pipeline).

:class:`RxNode` glues the layers together exactly as they run on the field:

* **Device.** ``device=None`` opens ``app.devices[0]`` (and closes it on :meth:`RxNode.stop`); a
  device passed in is configured but never closed by the node. ``configure(app.rx)`` runs in
  the constructor — a :class:`~eds.core.errors.ProfileViolation` propagates; profile warnings
  are reported in every ``rx.status``.
* **Calibration.** :class:`~eds.calib.store.CalibrationStore` (``[cal] dir``) is loaded by the
  device serial unless ``cal`` is given; a finite ``[cal] ppm_override`` replaces the stored
  ppm. :func:`~eds.calib.apply.apply_ppm_to_device` corrects the reference in hardware when it
  can (``ppm.via`` = ``hardware | soapy``), otherwise every spectrum is corrected in software
  (:func:`~eds.calib.apply.apply_calibration`; never twice). A TCXO temperature drift since the
  calibration is reported as a warning.
* **Pipeline.** :class:`~eds.rx.stream.RxStreamer` (reader thread, drop-oldest queue; with
  ``gain_mode = "soft_agc"`` the :class:`~eds.rx.agc.GainController` runs on every block in
  the reader thread) feeds the processing thread, which

  1. skips settling blocks (stale buffers after a retune / gain change),
  2. accumulates *contiguous* samples — the accumulator is reset on an overflow (gap before
     the block), on a host-queue drop (``IQBlock.index`` jump) and when fc / fs / gain /
     hardware ppm change — until one Welch window ``welch_len(nfft, averages, overlap)`` is
     complete; consecutive windows do not overlap, so every sample is analysed once while
     the node keeps up (100 % probability of intercept inside the instantaneous band),
  3. computes the Welch power spectrum of channel 0 (ARCHITECTURE §2 power-spectrum
     scaling), sets ``meta['dc_notch_bins'] / meta['usable_fraction']`` from ``[dsp]``,
     applies the calibration (ppm axis, dBm if a power table exists and
     ``[cal] use_power_cal``) and runs :func:`~eds.dsp.detect.detect` with the usable-band
     mask,
  4. publishes ``rx.detections`` for **every** frame (empty list included) and ``rx.spectrum``
     at most ``bus.spectrum_rate_hz`` times per second, max-hold decimated to at most
     ``bus.spectrum_max_bins`` points (``decimation`` field).

  Timestamps (wall clock, ``time.time_ns``): ``t_acq_ns`` = estimated host time of the first
  sample of the analysed window, ``t_acq_end_ns`` = end of its last sample, ``t_proc_ns`` =
  after the DSP. Together with the envelope ``t_ns`` and the subscriber's receive time they
  give the latency budget measured by ``eds measure latency``.
* **Status.** Every ``bus.status_period_s`` an ``rx.status`` (state, tuning, gains, ppm
  {value, via, age_s}, the 8 stream counters accumulated since the node started plus the
  device's ``overflow_source`` when it states one (``"none"`` = sample loss NOT detected: the
  overflow counter is unknown, not 0), levels of the last block, sensors, warnings — the
  device's own later warnings included).
* **Control.** ``ctl.rx`` commands (proxy mode: ``bus.proxy_backend``; direct mode: every
  ``bus.ctl_endpoints``; none → remote control disabled) are executed by
  :meth:`RxNode.handle_command`, which publishes a ``ctl.ack`` for each command addressed to
  ``"*"`` or to this node. Device errors become ``ok = false`` acks with the Turkish message;
  the node keeps running. A ``sweep`` stops the streamer, runs the
  :class:`~eds.rx.sweep.Sweeper` (state ``sweeping``), publishes ``rx.sweep``, restores the RX
  settings (``app.rx`` plus every change made over ``ctl.rx``) and restarts the pipeline —
  also when the stream was in ``error`` before the sweep. If the restore or the restart fails
  the node enters ``error`` and the retry loop re-applies the settings before restarting.
  Sweeps received over the bus run in a worker thread so that ``ping`` / ``stop`` stay
  responsive; ``stop`` aborts a running sweep and stops the node.

Threads: reader (streamer), ``rx-node-proc``, ``rx-node-status``, ``rx-node-ctl`` (if control is
enabled) and a short-lived ``rx-node-sweep``. :meth:`RxNode.stop` is idempotent, callable from
any thread (including the node's own) and joins everything it started. If the stream fails
(USB unplugged, driver error) the node enters ``state = "error"``, reports the cause in
``rx.status`` warnings and retries the stream every :data:`RESTART_BACKOFF_S` seconds.
"""

from __future__ import annotations

import collections
import dataclasses
import logging
import math
import threading
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from typing import Any

import numpy as np

from ..bus.pubsub import Publisher, Subscriber, publisher_from_config
from ..bus.topics import CTL_ACK, CTL_RX, RX_DETECTIONS, RX_SPECTRUM, RX_STATUS, RX_SWEEP
from ..calib.apply import apply_calibration, apply_ppm_to_device, check_temperature
from ..calib.ppm import PpmEstimate
from ..calib.store import CalibrationStore, DeviceCalibration
from ..core.config import AppConfig, SweepConfig, config_from_dict
from ..core.errors import ConfigError, EDSError, ProfileViolation
from ..core.timeutil import mono, now_ns
from ..core.types import IQBlock, StreamStats
from ..core.units import fmt_hz
from ..dsp.detect import Detection, detect, usable_mask
from ..dsp.levels import iq_levels
from ..dsp.psd import Spectrum, spectrum_from_block
from ..hal.base import RxDevice
from .agc import GainController
from .stream import RxStreamer
from .sweep import Sweeper, SweepResult

log = logging.getLogger(__name__)

#: Node states (``rx.status`` ``state`` enum).
STATES: tuple[str, ...] = ("idle", "streaming", "sweeping", "error")
#: Gain modes accepted by the node (``soft_agc`` = manual hardware gain + software AGC).
NODE_GAIN_MODES: tuple[str, ...] = ("manual", "soft_agc", "hw_slow", "hw_fast", "hw_hybrid")
#: ``ctl.rx`` commands (contracts/schemas/ctl.rx.schema.json).
COMMANDS: tuple[str, ...] = ("ping", "set_center_freq", "set_gain", "set_gain_mode", "set_sample_rate",
                             "set_bandwidth", "sweep", "stop")
#: ``apply_ppm_to_device`` result → ``rx.status`` ``ppm.via`` enum.
PPM_VIA: dict[str, str] = {"hardware": "hardware", "soapy": "soapy", "software": "software", "off": "none"}
#: Seconds between stream restart attempts after a stream failure.
RESTART_BACKOFF_S: float = 2.0
#: Decimals of the dB values published in ``rx.spectrum`` (0.01 dB).
SPECTRUM_DECIMALS: int = 2
#: At most this many distinct warnings are kept (oldest dropped).
MAX_WARNINGS: int = 32

_GET_TIMEOUT_S = 0.2          # processing thread: max wait for a block (bounds stop latency)
_CTL_POLL_S = 0.1             # control thread: max wait for a command (bounds stop latency)
_JOIN_S = 5.0                 # max wait for each node thread on stop
_LOG_EVERY = 100              # repeated per-frame errors: log the first few, then every N-th

#: ``on_publish(topic, envelope)`` hook, called after every successful publish.
OnPublish = Callable[[str, dict[str, Any]], None]

_COUNTER_KEYS: tuple[str, ...] = ("blocks", "samples", "overflows", "timeouts", "host_drops",
                                  "discarded_samples")


# =============================================================================== helpers
def _finite_number(v: Any, what: str) -> float:
    if isinstance(v, bool) or not isinstance(v, (int, float, np.integer, np.floating)):
        raise ConfigError(f"{what}: sayı bekleniyordu, {v!r} geldi")
    f = float(v)
    if not math.isfinite(f):
        raise ConfigError(f"{what}: sonlu bir sayı olmalı, {v!r} geldi")
    return f


def _arg(args: Mapping[str, Any], key: str, cmd: str) -> float:
    if key not in args:
        raise ConfigError(f"'{cmd}' komutu için '{key}' argümanı gerekli (args = {{\"{key}\": ...}})")
    return _finite_number(args[key], f"{cmd}.{key}")


def _sensor_value(v: Any) -> float | int | str | None:
    """Map a sensor reading onto the ``rx.status`` sensor value types (number | string | null)."""
    if v is None:
        return None
    if isinstance(v, (bool, np.bool_)):
        return "true" if v else "false"
    if isinstance(v, (int, np.integer)):
        return int(v)
    if isinstance(v, (float, np.floating)):
        f = float(v)
        return f if math.isfinite(f) else None
    return str(v)


def decimate_max(p_db: np.ndarray, max_points: int) -> tuple[np.ndarray, int]:
    """Max-hold decimation of ``p_db`` to at most ``max_points`` values.

    Returns ``(values, D)`` with ``D = ceil(n / max_points)`` bins per value; the last group
    may be partial (``len(values) = ceil(n / D)``). A narrow emitter never disappears.
    """
    p = np.asarray(p_db, dtype=np.float64)
    n = p.size
    d = max(1, math.ceil(n / max(int(max_points), 1)))
    if d == 1:
        return p.copy(), 1
    full = (n // d) * d
    out = p[:full].reshape(-1, d).max(axis=1)
    if full < n:
        out = np.append(out, p[full:].max())
    return out, d


@dataclass
class Window:
    """One contiguous analysis window cut from the stream (see :class:`WindowAccumulator`)."""

    samples: np.ndarray       # complex64 (n_ch, n), contiguous in time
    fs: float
    fc: float
    t_acq_ns: int             # estimated host time of the first sample (= t_acq_end_ns − n/fs)
    t_acq_end_ns: int         # estimated host time just after the last sample (last block's timeline)
    seq: int                  # IQBlock.seq of the block holding the first sample
    index: int                # absolute index of the first sample
    gain_db: tuple[float, ...]
    gap_before: bool          # samples were lost right before this window (overflow / host drop)
    clip_level: float
    meta: dict[str, Any]


class WindowAccumulator:
    """Cuts consecutive, non-overlapping windows of ``n`` *contiguous* samples from blocks.

    The buffer is reset (older samples discarded) when the next block is not contiguous with
    it: ``block.overflow`` (driver gap), an ``index`` jump (host-queue drop) or a change of
    fc / fs / gain / channel count / hardware ppm correction. The first window after such a
    reset reports ``gap_before = True`` when samples were actually lost.
    """

    def __init__(self, n: int):
        if isinstance(n, bool) or int(n) != n or int(n) < 1:
            raise ConfigError(f"Pencere uzunluğu pozitif tamsayı olmalı, {n!r} geldi")
        self.n = int(n)
        self._chunks: collections.deque[list[Any]] = collections.deque()   # [block, offset]
        self._avail = 0
        self._key: tuple[Any, ...] | None = None
        self._next_index: int | None = None
        self._gap = False
        self.resets = 0

    @property
    def available(self) -> int:
        """Samples buffered (per channel)."""
        return self._avail

    def reset(self, gap: bool = False) -> None:
        if self._avail:
            self.resets += 1
        self._chunks.clear()
        self._avail = 0
        self._key = None
        self._next_index = None
        self._gap = self._gap or gap

    @staticmethod
    def _block_key(blk: IQBlock) -> tuple[Any, ...]:
        return (float(blk.fc), float(blk.fs), tuple(float(g) for g in blk.gain_db), int(blk.n_channels),
                float(blk.meta.get("ppm_hw", 0.0)))

    def push(self, blk: IQBlock) -> None:
        """Append a (non-settling) block, resetting first if it does not continue the buffer."""
        key = self._block_key(blk)
        jump = self._next_index is not None and int(blk.index) != self._next_index
        if blk.overflow or jump:
            self.reset(gap=True)
        elif self._key is not None and key != self._key:
            self.reset(gap=False)
        self._key = key
        if blk.n:
            self._chunks.append([blk, 0])
            self._avail += blk.n
        self._next_index = int(blk.index) + blk.n

    def pop(self) -> Window | None:
        """Next complete window, or ``None`` if fewer than ``n`` samples are buffered."""
        if self._avail < self.n:
            return None
        need = self.n
        pieces: list[np.ndarray] = []
        first_blk, first_off = self._chunks[0]
        last_blk, last_end = first_blk, first_off
        while need:
            entry = self._chunks[0]
            blk, off = entry
            take = min(blk.n - off, need)
            pieces.append(blk.samples[:, off:off + take])
            need -= take
            last_blk, last_end = blk, off + take
            if off + take >= blk.n:
                self._chunks.popleft()
            else:
                entry[1] = off + take
        self._avail -= self.n
        x = pieces[0] if len(pieces) == 1 else np.concatenate(pieces, axis=1)
        fs = float(first_blk.fs)
        gap, self._gap = self._gap, False
        # Anchor the window on the host-time estimate of its LAST block (the freshest one; a
        # block's t_ns derives from the time its read returned) and span exactly n / fs.
        t_end = int(last_blk.t_ns + round(last_end / fs * 1e9))
        return Window(
            samples=np.ascontiguousarray(x, dtype=np.complex64), fs=fs, fc=float(first_blk.fc),
            t_acq_ns=t_end - int(round(self.n / fs * 1e9)), t_acq_end_ns=t_end,
            seq=int(first_blk.seq), index=int(first_blk.index) + int(first_off),
            gain_db=tuple(float(g) for g in first_blk.gain_db), gap_before=gap,
            clip_level=float(first_blk.clip_level), meta=dict(first_blk.meta),
        )


# =============================================================================== node
class RxNode:
    """RX node service (see the module docstring).

    Parameters
    ----------
    app
        Effective configuration.
    device
        Device to use (configured, never closed by the node); ``None`` opens
        ``app.devices[0]`` and closes it on :meth:`stop`.
    publisher
        Bus publisher (not closed by the node); ``None`` creates one from ``[bus]``
        (``validate = bus.validate``) and closes it on :meth:`stop`.
    cal
        Calibration to use; ``None`` loads it from the calibration store by device serial.
    name
        Logical node name (bus ``src``, ``ctl.rx`` target); default ``device.name``.
    ctl_subscriber
        Subscriber delivering ``ctl.rx`` (not closed by the node); ``None`` builds one from
        ``[bus]`` (proxy: ``proxy_backend``; direct: ``ctl_endpoints``, empty = no remote
        control).
    on_publish
        ``hook(topic, envelope)`` called after every message the node publishes.
    """

    def __init__(self, app: AppConfig, device: RxDevice | None = None, publisher: Publisher | None = None,
                 cal: DeviceCalibration | None = None, *, name: str | None = None,
                 ctl_subscriber: Subscriber | None = None, on_publish: OnPublish | None = None):
        if not isinstance(app, AppConfig):
            raise ConfigError(f"RxNode: AppConfig bekleniyordu, {type(app).__name__} geldi")
        if on_publish is not None and not callable(on_publish):
            raise ConfigError("on_publish çağrılabilir (callable) olmalı: on_publish(topic, envelope)")
        self.app = app
        self._check_bus(app)
        self._on_publish = on_publish
        self._lock = threading.RLock()           # state, streamer reference, counters, warnings
        self._cmd_lock = threading.RLock()       # serializes device-changing commands / sweeps
        self._stop_lock = threading.Lock()
        self._stop_evt = threading.Event()       # set by stop(): every node thread exits
        self._halt = threading.Event()           # stop requested (ctl 'stop') or stopped -> run() returns
        self._warnings: list[str] = []
        self._owns_device = device is None
        self._owns_publisher = publisher is None
        self._owns_ctl = False
        self._dev: RxDevice | None = None
        self._pub: Publisher | None = None
        self._ctl: Subscriber | None = None
        self._streamer: RxStreamer | None = None
        self._threads: dict[str, threading.Thread] = {}
        self._sweep_thread: threading.Thread | None = None
        self._sweeper: Sweeper | None = None
        self._state = "idle"
        self._started = False
        self._closed = False
        self._gen = 0
        self._last_block: IQBlock | None = None
        self._last_sensors: dict[str, Any] = {}
        self._agc: GainController | None = None
        self._soft_agc = False
        self._last_restart = 0.0
        self._reconfigure_pending = False        # RX settings must be re-applied before restarting
        self._dev_warn_seen = 0                  # device.warnings already copied into the node warnings
        self._acc_stats: dict[str, float] = {k: 0 for k in _COUNTER_KEYS}
        self._acc_stats["elapsed_s"] = 0.0
        self._counters: dict[str, int] = collections.Counter()
        self._last_proc_ms = math.nan
        try:
            self._dev = device if device is not None else self._open_device(app)
            self.name = str(name) if name else self._dev.name
            # warnings a passed-in device raised before this node existed are not reported
            self._dev_warn_seen = len(getattr(self._dev, "warnings", None) or [])
            if not self.name:
                raise ConfigError("RX düğümü adı boş olamaz")
            self._rx_cfg = dataclasses.replace(app.rx)
            self._gain_mode = app.rx.gain_mode
            if self._gain_mode not in NODE_GAIN_MODES:
                raise ConfigError(f"rx.gain_mode = {self._gain_mode!r} geçersiz; seçenekler: {list(NODE_GAIN_MODES)}")
            for w in self._dev.configure(app.rx):
                self._warn(w, log_it=False)
            if self._gain_mode == "soft_agc":
                self._enable_soft_agc()
            self._win_len = self._window_length(app)
            self._streamer = self._make_streamer()           # validates block_size / queue_blocks
            self._setup_calibration(cal)
            self._pub = publisher if publisher is not None else publisher_from_config(
                app.bus, src=self.name, validate=app.bus.validate)
            if ctl_subscriber is not None:
                self._ctl = ctl_subscriber
            else:
                self._ctl = self._ctl_from_config(app)
                self._owns_ctl = self._ctl is not None
        except BaseException:
            self._release_resources()
            raise

    # ------------------------------------------------------------------ construction helpers
    @staticmethod
    def _open_device(app: AppConfig) -> RxDevice:
        from ..hal.factory import open_device

        if not app.devices:
            raise ConfigError("Konfigürasyonda cihaz yok ([[device]] gerekli)")
        return open_device(app.devices[0], app)

    @staticmethod
    def _check_bus(app: AppConfig) -> None:
        b = app.bus
        if not (isinstance(b.status_period_s, (int, float)) and math.isfinite(b.status_period_s)
                and b.status_period_s > 0):
            raise ConfigError(f"bus.status_period_s pozitif sonlu bir sayı olmalı, {b.status_period_s!r} geldi")
        if not (isinstance(b.spectrum_rate_hz, (int, float)) and math.isfinite(b.spectrum_rate_hz)
                and b.spectrum_rate_hz >= 0):
            raise ConfigError(f"bus.spectrum_rate_hz >= 0 sonlu bir sayı olmalı (0 = spektrum yayını kapalı), "
                              f"{b.spectrum_rate_hz!r} geldi")
        if isinstance(b.spectrum_max_bins, bool) or int(b.spectrum_max_bins) < 1:
            raise ConfigError(f"bus.spectrum_max_bins pozitif tamsayı olmalı, {b.spectrum_max_bins!r} geldi")

    @staticmethod
    def _window_length(app: AppConfig) -> int:
        from ..measure.common import welch_len

        d = app.dsp
        if isinstance(d.nfft, bool) or int(d.nfft) < 16:
            raise ConfigError(f"dsp.nfft en az 16 olmalı, {d.nfft!r} geldi")
        if isinstance(d.averages, bool) or int(d.averages) < 1:
            raise ConfigError(f"dsp.averages pozitif tamsayı olmalı, {d.averages!r} geldi")
        return welch_len(int(d.nfft), int(d.averages), float(d.overlap))

    def _ctl_from_config(self, app: AppConfig) -> Subscriber | None:
        b = app.bus
        if b.mode == "proxy":
            return Subscriber(b.proxy_backend, [CTL_RX])
        if b.mode == "direct":
            if not b.ctl_endpoints:
                log.info("%s: bus.ctl_endpoints boş — uzaktan kontrol (ctl.rx) kapalı", self.name)
                return None
            return Subscriber(list(b.ctl_endpoints), [CTL_RX])
        raise ConfigError(f"bus.mode = {b.mode!r} geçersiz; seçenekler: ['direct', 'proxy']")

    def _setup_calibration(self, cal: DeviceCalibration | None) -> None:
        dev = self._dev
        app = self.app
        if cal is None:
            serial = str(dev.info.serial or "")
            if serial:
                cal = CalibrationStore(app.cal.dir or None).load(serial)
            else:
                cal = DeviceCalibration(serial="")
                self._warn(f"{self.name}: cihaz seri numarası okunamadı — kalibrasyon deposu kullanılamıyor")
        elif not isinstance(cal, DeviceCalibration):
            raise ConfigError(f"cal bir DeviceCalibration olmalı, {type(cal).__name__} geldi")
        override = float(app.cal.ppm_override)
        if math.isfinite(override):
            est = PpmEstimate(ppm=override, std_ppm=math.nan, f_measured_hz=math.nan, f_ref_hz=math.nan,
                              snr_db=math.nan, n=0, method="manual", t_ns=0,
                              extra={"source": "cal.ppm_override"})
            cal = dataclasses.replace(cal, ppm=est)
            log.info("%s: cal.ppm_override = %+.4f ppm kullanılıyor (depo atlandı)", self.name, override)
        self._cal = cal
        self._cal_spec = cal if app.cal.use_power_cal else dataclasses.replace(cal, power=None)
        via = apply_ppm_to_device(dev, cal, app.cal.ppm_mode)
        self._ppm_via = PPM_VIA.get(via, "none")
        if via not in PPM_VIA:
            self._warn(f"{self.name}: PPM düzeltme yolu tanınmadı ({via!r}); 'none' raporlanıyor")
        if cal.ppm is None and app.cal.ppm_mode != "off":
            # apply_ppm_to_device has logged the same fact; here it only goes to rx.status
            self._warn(f"{self.name}: PPM kalibrasyonu yok — frekans ekseni düzeltilmiyor (eds cal ppm)",
                       log_it=False)
        t = check_temperature(cal, dev, app.cal.temp_warn_delta_c)
        if t:
            self._warn(t, log_it=False)

    def _make_streamer(self) -> RxStreamer:
        rx = self.app.rx
        return RxStreamer(self._dev, rx.block_size, rx.queue_blocks, on_block=self._on_block,
                          read_timeout_s=rx.read_timeout_s)

    def _enable_soft_agc(self) -> None:
        if self._agc is None:
            self._agc = GainController(self._dev, self.app.agc)
        else:
            self._agc.reset()
        self._soft_agc = True

    # ------------------------------------------------------------------ properties
    @property
    def device(self) -> RxDevice:
        return self._dev

    @property
    def publisher(self) -> Publisher:
        return self._pub

    @property
    def calibration(self) -> DeviceCalibration:
        return self._cal

    @property
    def ppm_via(self) -> str:
        """Where the reference correction is applied: ``hardware | soapy | software | none``."""
        return self._ppm_via

    @property
    def state(self) -> str:
        with self._lock:
            return self._state

    @property
    def gain_mode(self) -> str:
        return self._gain_mode

    @property
    def window_samples(self) -> int:
        """Samples per analysed window (``welch_len(nfft, averages, overlap)``)."""
        return self._win_len

    @property
    def running(self) -> bool:
        return self._started and not self._stop_evt.is_set()

    @property
    def control_enabled(self) -> bool:
        return self._ctl is not None

    @property
    def warnings(self) -> list[str]:
        with self._lock:
            return list(self._warnings)

    @property
    def stats(self) -> dict[str, Any]:
        """Node counters + accumulated stream counters (snapshot)."""
        with self._lock:
            d: dict[str, Any] = {k: int(self._counters.get(k, 0)) for k in (
                "frames", "detection_msgs", "detections", "spectrum_msgs", "status_msgs", "acks", "commands",
                "sweeps", "publish_errors", "frame_errors", "stream_errors", "stream_restarts",
                "settling_blocks_skipped", "window_resets")}
            d["state"] = self._state
            d["last_proc_ms"] = self._last_proc_ms
        d["stream"] = self.stream_counters()
        return d

    def stream_counters(self) -> dict[str, Any]:
        """The 8 ``rx.status`` stream counters, accumulated over every stream since start."""
        with self._lock:
            acc = dict(self._acc_stats)
            streamer = self._streamer if self._started else None
        cur = streamer.stats if streamer is not None else None
        out: dict[str, Any] = {k: int(acc[k]) + (int(getattr(cur, k)) if cur is not None else 0)
                               for k in _COUNTER_KEYS}
        elapsed = float(acc["elapsed_s"]) + (float(cur.elapsed_s) if cur is not None else 0.0)
        out["throughput_sps"] = float(out["samples"] / elapsed) if elapsed > 0 else 0.0
        out["elapsed_s"] = float(elapsed)
        return out

    # ------------------------------------------------------------------ lifecycle
    def start(self) -> None:
        """Start streaming, processing, status and control threads (non-blocking)."""
        with self._lock:
            if self._closed or self._stop_evt.is_set():
                raise EDSError(f"{self.name}: RX düğümü durduruldu; yeniden başlatılamaz (yeni RxNode oluşturun)")
            if self._started:
                return
            self._started = True
        try:
            with self._cmd_lock:
                if self._reconfigure_pending:              # a sweep could not restore the RX settings
                    self._restore_after_sweep()
                    self._reconfigure_pending = False
                self._start_streamer(first=True)
            self._spawn("proc", self._proc_loop)
            self._spawn("status", self._status_loop)
            if self._ctl is not None:
                self._spawn("ctl", self._ctl_loop)
        except BaseException:
            self.stop()
            raise
        log.info("%s: RX düğümü başladı (fc %s, fs %.4g MS/s, pencere %d örnek, ppm %s)", self.name,
                 fmt_hz(self._dev.fc), self._dev.fs / 1e6, self._win_len, self._ppm_via)

    def run(self, duration_s: float | None = None) -> None:
        """Blocking: start, run for ``duration_s`` (``None`` = until :meth:`stop`, a ``stop``
        command or Ctrl-C), then stop. KeyboardInterrupt is handled (clean stop)."""
        if duration_s is not None:
            duration_s = _finite_number(duration_s, "duration_s")
            if duration_s < 0:
                raise ConfigError("duration_s negatif olamaz")
        self.start()
        try:
            deadline = None if duration_s is None else mono() + duration_s
            while not self._halt.is_set():
                wait = 0.5
                if deadline is not None:
                    remaining = deadline - mono()
                    if remaining <= 0:
                        break
                    wait = min(wait, remaining)
                self._halt.wait(wait)
        except KeyboardInterrupt:
            log.info("%s: Ctrl-C — RX düğümü durduruluyor", self.name)
        finally:
            self.stop()

    def stop(self) -> None:
        """Stop the node (idempotent, any thread): abort a sweep, join the node threads, stop
        the stream and close the resources the node opened."""
        self._stop_evt.set()
        self._halt.set()
        sw = self._sweeper
        if sw is not None:
            sw.abort()
        me = threading.current_thread()
        own = me in self._all_threads()
        if not self._stop_lock.acquire(blocking=not own):
            return                              # another thread is already stopping the node
        try:
            if self._closed:
                return
            for th in self._all_threads():
                if th is not me and th.is_alive():
                    th.join(_JOIN_S)
                    if th.is_alive():
                        log.error("%s: '%s' iş parçacığı %.0f s içinde durmadı", self.name, th.name, _JOIN_S)
            with self._lock:
                streamer, self._streamer = self._streamer, None
            if streamer is not None:
                self._retire_streamer(streamer, fold=self._started)
            with self._lock:
                self._state = "idle"
            self._release_resources()
            self._closed = True
            log.info("%s: RX düğümü durdu", self.name)
        finally:
            self._stop_lock.release()

    def __enter__(self) -> RxNode:
        self.start()
        return self

    def __exit__(self, *exc: object) -> None:
        self.stop()

    def _release_resources(self) -> None:
        errors: list[BaseException] = []
        if self._owns_ctl and self._ctl is not None:
            try:
                self._ctl.close()
            except Exception as e:
                errors.append(e)
        if self._owns_publisher and self._pub is not None:
            try:
                self._pub.close()
            except Exception as e:
                errors.append(e)
        if self._owns_device and self._dev is not None:
            try:
                self._dev.close()
            except Exception as e:
                errors.append(e)
        for e in errors:
            log.error("%s: kaynak kapatılamadı: %s", getattr(self, "name", "rx"), e)

    def _spawn(self, key: str, target: Callable[[], None]) -> None:
        th = threading.Thread(target=target, name=f"rx-node-{key}-{self.name}", daemon=True)
        self._threads[key] = th
        th.start()

    def _all_threads(self) -> list[threading.Thread]:
        ths = list(self._threads.values())
        if self._sweep_thread is not None:
            ths.append(self._sweep_thread)
        return ths

    # ------------------------------------------------------------------ streamer management
    def _start_streamer(self, first: bool = False) -> None:
        """(Re)start the pipeline stream (caller holds ``_cmd_lock``)."""
        streamer = self._streamer if (first and self._streamer is not None) else self._make_streamer()
        streamer.start()
        with self._lock:
            self._streamer = streamer
            self._gen += 1
            self._state = "streaming"

    def _retire_streamer(self, streamer: RxStreamer, fold: bool = True) -> None:
        """Stop ``streamer`` and fold its final counters into the accumulated totals."""
        try:
            streamer.stop()
        finally:
            if fold:
                st: StreamStats = streamer.stats
                with self._lock:
                    for k in _COUNTER_KEYS:
                        self._acc_stats[k] += int(getattr(st, k))
                    self._acc_stats["elapsed_s"] += float(st.elapsed_s)

    def _pause_pipeline(self, new_state: str) -> bool:
        """Stop the running streamer (if any) and enter ``new_state``. Returns whether the
        pipeline must be (re)started afterwards: the node is started and not stopping —
        whether or not a streamer was running. A node in ``state = "error"`` (stream failed,
        restart pending) has no streamer but must still resume streaming after the pause."""
        with self._lock:
            streamer, self._streamer = self._streamer, None
            resume = self._started and not self._stop_evt.is_set()
            fold = streamer is not None and self._started
            self._state = new_state
            self._gen += 1
        if streamer is not None:
            self._retire_streamer(streamer, fold=fold)
        return resume

    def _on_block(self, blk: IQBlock) -> None:
        """Reader-thread hook: software AGC (``soft_agc``)."""
        agc = self._agc
        if self._soft_agc and agc is not None:
            agc.update(blk)

    # ------------------------------------------------------------------ processing thread
    def _proc_loop(self) -> None:
        st = {"acc": WindowAccumulator(self._win_len), "gen": -1, "last_spec": -math.inf}
        while not self._stop_evt.is_set():
            try:
                self._proc_step(st)
            except Exception as e:  # never kill the pipeline: count, log, report, continue
                n = self._count("frame_errors")
                if n <= 3 or n % _LOG_EVERY == 0:
                    log.exception("%s: kare işlenemedi (%d. hata): %s", self.name, n, e)
                self._warn(f"{self.name}: spektrum karesi işlenemedi: {type(e).__name__}: {e}", log_it=False)
                st["acc"].reset()

    def _proc_step(self, st: dict[str, Any]) -> None:
        """One iteration of the processing thread: get a block, cut windows, process them."""
        acc: WindowAccumulator = st["acc"]
        with self._lock:
            streamer, state, cur_gen = self._streamer, self._state, self._gen
        if cur_gen != st["gen"]:
            acc.reset()
            st["gen"] = cur_gen
        if streamer is None:
            if state == "error":
                self._try_restart()
            self._stop_evt.wait(_GET_TIMEOUT_S)
            return
        blk = streamer.get(_GET_TIMEOUT_S)
        if blk is None:
            if self._stop_evt.is_set():
                return
            with self._lock:
                current = streamer is self._streamer and self._state == "streaming"
            if current and not streamer.running:
                self._stream_failed(streamer, streamer.error)
            return
        with self._lock:
            if streamer is not self._streamer or self._state != "streaming":
                return                            # stale block of a paused / replaced stream
            self._last_block = blk
        if blk.settling:
            self._count("settling_blocks_skipped")
            acc.reset()
            return
        before = acc.resets
        acc.push(blk)
        if acc.resets != before:
            self._count("window_resets")
        while not self._stop_evt.is_set():
            win = acc.pop()
            if win is None:
                break
            st["last_spec"] = self._process_window(win, st["last_spec"])

    def _process_window(self, win: Window, last_spec: float) -> float:
        app = self.app
        dsp = app.dsp
        blk = IQBlock(samples=win.samples, fs=win.fs, fc=win.fc, t_ns=win.t_acq_ns, index=win.index, seq=win.seq,
                      gain_db=win.gain_db, overflow=win.gap_before, settling=False, clip_level=win.clip_level,
                      meta=win.meta)
        spec = spectrum_from_block(blk, int(dsp.nfft), dsp.window, float(dsp.overlap), dsp.avg_mode, channel=0)
        spec.meta["dc_notch_bins"] = int(dsp.dc_notch_bins)
        spec.meta["usable_fraction"] = float(dsp.usable_fraction)
        spec = apply_calibration(spec, self._cal_spec, app.cal.ppm_mode)
        mask = usable_mask(spec.n_bins, float(dsp.usable_fraction), int(dsp.dc_notch_bins))
        dets, _threshold = detect(spec, app.detect, valid_mask=mask)
        t_proc = now_ns()
        self._publish(RX_DETECTIONS, self._detections_payload(spec, win, dets, t_proc))
        with self._lock:
            self._counters["frames"] += 1
            self._counters["detection_msgs"] += 1
            self._counters["detections"] += len(dets)
            self._last_proc_ms = (t_proc - win.t_acq_end_ns) / 1e6
        rate = float(app.bus.spectrum_rate_hz)
        now = mono()
        if rate > 0 and now - last_spec >= 1.0 / rate:
            self._publish(RX_SPECTRUM, self._spectrum_payload(spec, win, t_proc))
            self._count("spectrum_msgs")
            return now
        return last_spec

    def _detections_payload(self, spec: Spectrum, win: Window, dets: list[Detection], t_proc: int) -> dict[str, Any]:
        return {
            "device": self.name, "block_seq": int(win.seq), "t_acq_ns": int(win.t_acq_ns),
            "t_acq_end_ns": int(win.t_acq_end_ns), "t_proc_ns": int(t_proc), "fc_hz": float(spec.fc),
            "rbw_hz": float(spec.rbw_hz), "unit": spec.unit, "method": str(self.app.detect.method),
            "pfa": float(self.app.detect.pfa), "detections": [d.to_dict() for d in dets],
        }

    def _spectrum_payload(self, spec: Spectrum, win: Window, t_proc: int) -> dict[str, Any]:
        values, d = decimate_max(spec.p_db, int(self.app.bus.spectrum_max_bins))
        df = spec.df_hz
        return {
            "device": self.name, "block_seq": int(win.seq), "t_acq_ns": int(win.t_acq_ns),
            "t_acq_end_ns": int(win.t_acq_end_ns), "t_proc_ns": int(t_proc), "fc_hz": float(spec.fc),
            "fs_hz": float(spec.fs), "rbw_hz": float(spec.rbw_hz), "nfft": int(spec.nfft),
            "averages": int(spec.averages), "window": spec.window,
            "f_start_hz": float(spec.f_hz[0] + (d - 1) / 2.0 * df), "f_step_hz": float(d * df),
            "n_bins": int(spec.n_bins), "decimation": int(d), "unit": spec.unit,
            "calibrated": bool(spec.meta.get("calibrated", False)),
            "ppm_corrected": bool(spec.meta.get("ppm_corrected", False)),
            "gain_db": [float(g) for g in spec.gain_db], "noise_floor": float(spec.noise_floor_db),
            # numpy array: the envelope sanitizer converts it in one call (no per-element walk)
            "values": np.round(values, SPECTRUM_DECIMALS), "overflow": bool(win.gap_before),
        }

    def _stream_failed(self, streamer: RxStreamer, err: BaseException | None) -> None:
        with self._lock:
            if streamer is not self._streamer:
                return
            self._streamer = None
            self._state = "error"
            self._gen += 1
            self._counters["stream_errors"] += 1
        msg = f"{self.name}: akış durdu: {err}" if err is not None else f"{self.name}: akış beklenmedik şekilde durdu"
        log.error("%s — %.0f s sonra yeniden denenecek", msg, RESTART_BACKOFF_S)
        self._warn(msg, log_it=False)
        self._last_restart = mono()
        try:
            self._retire_streamer(streamer)
        except Exception as e:
            log.error("%s: hatalı akış durdurulamadı: %s", self.name, e)

    def _try_restart(self) -> None:
        if mono() - self._last_restart < RESTART_BACKOFF_S or self._stop_evt.is_set():
            return
        self._last_restart = mono()
        with self._cmd_lock:
            with self._lock:
                if self._state != "error" or self._stop_evt.is_set():
                    return
            try:
                if self._reconfigure_pending:          # RX settings not restored after a sweep
                    self._restore_after_sweep()
                    self._reconfigure_pending = False
                self._start_streamer()
            except EDSError as e:
                log.error("%s: akış yeniden başlatılamadı: %s", self.name, e)
                self._warn(f"{self.name}: akış yeniden başlatılamadı: {e}", log_it=False)
                return
        self._count("stream_restarts")
        log.warning("%s: akış yeniden başlatıldı", self.name)

    # ------------------------------------------------------------------ status
    def _status_loop(self) -> None:
        period = float(self.app.bus.status_period_s)
        nxt = mono()
        while not self._stop_evt.is_set():
            try:
                self.publish_status()
            except Exception as e:  # report, keep the status heartbeat alive
                n = self._count("status_errors")
                if n <= 3 or n % _LOG_EVERY == 0:
                    log.exception("%s: durum mesajı oluşturulamadı: %s", self.name, e)
            nxt += period
            delay = nxt - mono()
            if delay < 0:                         # fell behind (e.g. long sensor read): resync
                nxt = mono()
                delay = 0.0
            self._stop_evt.wait(delay)

    def status_payload(self) -> dict[str, Any]:
        """``rx.status`` data (contracts/schemas/rx.status.schema.json)."""
        dev = self._dev
        info = dev.info
        with self._lock:
            state = self._state
            blk = self._last_block
        levels: dict[str, Any] = {"peak_dbfs": None, "rms_dbfs": None, "clip_fraction": None}
        if blk is not None:
            lv = iq_levels(blk.samples, blk.clip_level)
            levels = {"peak_dbfs": float(lv.peak_dbfs), "rms_dbfs": float(lv.rms_dbfs),
                      "clip_fraction": float(min(max(lv.clip_fraction, 0.0), 1.0))}
        if state != "sweeping":
            self._last_sensors = self._read_sensors()
        self._import_device_warnings()
        stream = self.stream_counters()
        ovf_src = self.overflow_source
        if ovf_src is not None:
            stream["overflow_source"] = ovf_src
        ppm = self._cal.ppm
        value = float(ppm.ppm) if (ppm is not None and self._ppm_via != "none") else None
        age = max((now_ns() - ppm.t_ns) / 1e9, 0.0) if (ppm is not None and ppm.t_ns > 0) else None
        gains = [float(g) for g in dev.gains] or [0.0]
        return {
            "device": {"name": self.name, "backend": str(info.backend or "unknown"), "driver": str(info.driver or ""),
                       "serial": str(info.serial or ""), "profile": str(info.profile or "")},
            "state": state, "fc_hz": max(float(dev.fc), 0.0), "fs_hz": max(float(dev.fs), 0.0),
            "bw_hz": max(float(dev.bw), 0.0), "gain_db": gains, "gain_mode": self._gain_mode,
            "ppm": {"value": value, "via": self._ppm_via, "age_s": age},
            "stream": stream, "levels": levels, "sensors": dict(self._last_sensors),
            "warnings": self.warnings,
        }

    @property
    def overflow_source(self) -> str | None:
        """How the device detects sample loss (``RxDevice.overflow_source``: ``"driver"``,
        ``"dma_status"`` or ``"none"`` = NOT detected, the overflow counter stays 0 whatever
        happens); ``None`` when the backend does not say (simulator: exact)."""
        v = getattr(self._dev, "overflow_source", None)
        return v if isinstance(v, str) and v else None

    def _import_device_warnings(self) -> None:
        """Report warnings the device raised after ``configure`` (e.g. at stream start: overflow
        detection unavailable) in ``rx.status`` too."""
        dev_warns = getattr(self._dev, "warnings", None)
        if not isinstance(dev_warns, list):
            return
        with self._lock:
            if len(dev_warns) < self._dev_warn_seen:   # the device reset its list
                self._dev_warn_seen = 0
            new = list(dev_warns[self._dev_warn_seen:])
            self._dev_warn_seen += len(new)
            for w in new:
                self._warn(str(w), log_it=False)        # the device logged it already

    def publish_status(self) -> dict[str, Any] | None:
        """Build and publish one ``rx.status``; returns the envelope (``None`` if not sent)."""
        env = self._publish(RX_STATUS, self.status_payload())
        if env is not None:
            self._count("status_msgs")
        return env

    def _read_sensors(self) -> dict[str, Any]:
        try:
            raw = self._dev.read_sensors()
        except Exception as e:  # a sensor failure must not stop the status heartbeat
            self._warn(f"{self.name}: sensörler okunamadı: {type(e).__name__}: {e}")
            return {}
        if not isinstance(raw, Mapping):
            self._warn(f"{self.name}: read_sensors() sözlük döndürmedi ({type(raw).__name__})")
            return {}
        return {str(k): _sensor_value(v) for k, v in raw.items()}

    # ------------------------------------------------------------------ control
    def _ctl_loop(self) -> None:
        sub = self._ctl
        while not self._stop_evt.is_set():
            try:
                msg = sub.recv(_CTL_POLL_S)
            except Exception as e:  # transport hiccup: report, keep the control channel alive
                if self._stop_evt.is_set():
                    break
                n = self._count("ctl_errors")
                if n <= 3 or n % _LOG_EVERY == 0:
                    log.error("%s: ctl.rx alınamadı: %s", self.name, e)
                self._stop_evt.wait(_CTL_POLL_S)
                continue
            if msg is None or msg.topic != CTL_RX:
                continue
            data = msg.data
            if isinstance(data, Mapping) and data.get("cmd") == "sweep" and self._addressed(data):
                self._dispatch_sweep(data)
            else:
                self.handle_command(data)
            if self._halt.is_set():
                self.stop()
                break

    def _addressed(self, cmd: Mapping[str, Any]) -> bool:
        target = cmd.get("target", "*")
        return target == "*" or target == self.name

    def _dispatch_sweep(self, cmd: Mapping[str, Any]) -> None:
        """Run a bus-received sweep in a worker so ``ping`` / ``stop`` stay responsive."""
        th = self._sweep_thread
        if th is not None and th.is_alive():
            self._ack(cmd, ok=False, error=f"{self.name}: tarama zaten sürüyor; komut reddedildi")
            return
        th = threading.Thread(target=self.handle_command, args=(dict(cmd),), name=f"rx-node-sweep-{self.name}",
                              daemon=True)
        self._sweep_thread = th
        th.start()

    def handle_command(self, cmd: Mapping[str, Any]) -> dict[str, Any] | None:
        """Execute one ``ctl.rx`` command and publish its ``ctl.ack``.

        Returns the ack data, or ``None`` when the command is addressed to another node
        (``target`` neither ``"*"`` nor this node's name). Never raises for bad commands or
        device errors: they produce ``ok = false`` with a Turkish ``error``.
        """
        if not isinstance(cmd, Mapping):
            return self._ack({}, ok=False, error=f"Komut bir JSON nesnesi olmalı, {type(cmd).__name__} geldi")
        if not self._addressed(cmd):
            return None
        self._count("commands")
        name = cmd.get("cmd")
        args = cmd.get("args")
        try:
            if args is None:
                args = {}
            if not isinstance(args, Mapping):
                raise ConfigError(f"'args' bir nesne olmalı, {type(args).__name__} geldi")
            handler = self._handlers().get(name) if isinstance(name, str) else None
            if handler is None:
                raise ConfigError(f"Bilinmeyen komut: {name!r} (geçerli: {', '.join(COMMANDS)})")
            result = handler(dict(args))
        except EDSError as e:
            log.warning("%s: '%s' komutu başarısız: %s", self.name, name, e)
            return self._ack(cmd, ok=False, error=str(e) or type(e).__name__)
        except Exception as e:  # a bug must not take the node down: report it in the ack
            log.exception("%s: '%s' komutunda iç hata", self.name, name)
            return self._ack(cmd, ok=False, error=f"İç hata ({type(e).__name__}): {e}")
        ack = self._ack(cmd, ok=True, result=result)
        if name == "stop":
            self._after_stop_command()
        return ack

    def _ack(self, cmd: Mapping[str, Any], ok: bool, *, error: str | None = None,
             result: dict[str, Any] | None = None) -> dict[str, Any]:
        req = cmd.get("req_id", "") if isinstance(cmd, Mapping) else ""
        name = cmd.get("cmd") if isinstance(cmd, Mapping) else None
        data: dict[str, Any] = {"req_id": req if isinstance(req, str) else str(req), "target": self.name,
                                "cmd": name if isinstance(name, str) else str(name), "ok": bool(ok),
                                "error": None if ok else (error or "Bilinmeyen hata"),
                                "result": result if ok else None}
        if self._pub is not None and not self._pub.closed:
            if self._publish(CTL_ACK, data) is not None:
                self._count("acks")
        return data

    def _handlers(self) -> dict[str, Callable[[dict[str, Any]], dict[str, Any]]]:
        return {
            "ping": self._cmd_ping, "set_center_freq": self._cmd_set_center_freq, "set_gain": self._cmd_set_gain,
            "set_gain_mode": self._cmd_set_gain_mode, "set_sample_rate": self._cmd_set_sample_rate,
            "set_bandwidth": self._cmd_set_bandwidth, "sweep": self._cmd_sweep, "stop": self._cmd_stop,
        }

    def _check_settable(self, cmd: str) -> None:
        with self._lock:
            state = self._state
        if state == "sweeping":
            raise ConfigError(f"{self.name}: tarama sürüyor — '{cmd}' komutu reddedildi (önce taramanın bitmesini "
                              "bekleyin ya da 'stop' gönderin)")
        if self._closed or self._stop_evt.is_set():
            raise ConfigError(f"{self.name}: düğüm durduruluyor — '{cmd}' komutu reddedildi")

    def _cmd_ping(self, args: dict[str, Any]) -> dict[str, Any]:
        dev = self._dev
        return {"state": self.state, "fc_hz": float(dev.fc), "fs_hz": float(dev.fs),
                "gain_db": [float(g) for g in dev.gains], "gain_mode": self._gain_mode}

    def _cmd_set_center_freq(self, args: dict[str, Any]) -> dict[str, Any]:
        hz = _arg(args, "hz", "set_center_freq")
        with self._cmd_lock:
            self._check_settable("set_center_freq")
            fc = self._dev.set_center_freq(hz)
            self._rx_cfg.center_freq_hz = hz
            if self._agc is not None:
                self._agc.reset()
        log.info("%s: merkez frekans → %s", self.name, fmt_hz(fc))
        return {"fc_hz": float(fc), "gain_db": [float(g) for g in self._dev.gains]}

    def _cmd_set_gain(self, args: dict[str, Any]) -> dict[str, Any]:
        db = _arg(args, "db", "set_gain")
        with self._cmd_lock:
            self._check_settable("set_gain")
            if self._gain_mode.startswith("hw_"):
                raise ConfigError(f"{self.name}: donanım AGC'si ({self._gain_mode}) açıkken kazanç ayarlanamaz; "
                                  "önce set_gain_mode manual (ya da soft_agc) gönderin")
            dev = self._dev
            err, _ = dev.profile.check_rx(center_freq_hz=dev.fc or None, gain_db=db)
            if err:
                raise ProfileViolation(err)
            g = dev.set_gain(db)
            self._rx_cfg.gain_db = float(g)
        return {"gain_db": [float(x) for x in self._dev.gains], "applied_db": float(g)}

    def _cmd_set_gain_mode(self, args: dict[str, Any]) -> dict[str, Any]:
        mode = args.get("mode")
        if mode not in NODE_GAIN_MODES:
            raise ConfigError(f"set_gain_mode: 'mode' {mode!r} geçersiz; seçenekler: {list(NODE_GAIN_MODES)}")
        with self._cmd_lock:
            self._check_settable("set_gain_mode")
            dev = self._dev
            if mode == "soft_agc":
                dev.set_gain_mode("manual")
                self._enable_soft_agc()
            else:
                self._soft_agc = False
                dev.set_gain_mode(mode)
            self._gain_mode = mode
            self._rx_cfg.gain_mode = mode
            if mode in ("manual", "soft_agc"):
                self._rx_cfg.gain_db = float(dev.gains[0])
        return {"gain_mode": mode, "gain_db": [float(g) for g in self._dev.gains]}

    def _cmd_set_sample_rate(self, args: dict[str, Any]) -> dict[str, Any]:
        hz = _arg(args, "hz", "set_sample_rate")
        with self._cmd_lock:
            self._check_settable("set_sample_rate")
            dev = self._dev
            _, warns = dev.profile.check_rx(sample_rate_hz=hz, bandwidth_hz=dev.bw or None,
                                            n_channels=dev.num_channels)
            fs = dev.set_sample_rate(hz)
            self._rx_cfg.sample_rate_hz = hz
        for w in warns:
            self._warn(w)
        return {"fs_hz": float(fs), "warnings": list(warns)}

    def _cmd_set_bandwidth(self, args: dict[str, Any]) -> dict[str, Any]:
        hz = _arg(args, "hz", "set_bandwidth")
        with self._cmd_lock:
            self._check_settable("set_bandwidth")
            dev = self._dev
            _, warns = dev.profile.check_rx(sample_rate_hz=dev.fs or None, bandwidth_hz=hz,
                                            n_channels=dev.num_channels)
            bw = dev.set_bandwidth(hz)
            self._rx_cfg.bandwidth_hz = hz
        for w in warns:
            self._warn(w)
        return {"bw_hz": float(bw), "warnings": list(warns)}

    def _cmd_stop(self, args: dict[str, Any]) -> dict[str, Any]:
        self._halt.set()
        sw = self._sweeper
        aborted = sw is not None and self.state == "sweeping"
        if sw is not None:
            sw.abort()
        return {"state": "stopping", "sweep_aborted": bool(aborted)}

    def _after_stop_command(self) -> None:
        me = threading.current_thread()
        if me in self._all_threads():
            return                    # control loop / run() perform the stop after the ack
        if self._started:
            self.stop()

    # ------------------------------------------------------------------ sweep
    def sweep_config(self, overrides: Mapping[str, Any] | None = None) -> SweepConfig:
        """``[sweep]`` with ``overrides`` (validated like the config file: unknown keys,
        types and enums are errors)."""
        base = dataclasses.asdict(self.app.sweep)
        if overrides:
            base.update(dict(overrides))
        cfg = config_from_dict({"sweep": base}, source="ctl.rx sweep").sweep
        # checked BEFORE the sweep runs: an invalid value would otherwise fail only when the
        # finished sweep is packed into rx.sweep, losing the result
        if cfg.publish_max_points < 1:
            raise ConfigError(f"sweep.publish_max_points pozitif tamsayı olmalı, {cfg.publish_max_points!r} geldi")
        return cfg

    def calibrate_spectrum(self, spec: Spectrum) -> Spectrum:
        """Calibration closure for the sweeper (ppm residual w.r.t. the hardware correction
        currently applied, dBm if a power table is used)."""
        return apply_calibration(spec, self._cal_spec, self.app.cal.ppm_mode, ppm_hw=float(self._dev.ppm_hw))

    def _cmd_sweep(self, args: dict[str, Any]) -> dict[str, Any]:
        cfg = self.sweep_config(args)
        dev = self._dev
        with self._cmd_lock:
            self._check_settable("sweep")
            sweeper = Sweeper(dev, cfg, self.app.dsp, self.app.detect, calibrate=self.calibrate_spectrum,
                              agc=self.app.agc, timeout_s=float(self.app.rx.read_timeout_s))
            if self._gain_mode in ("manual", "soft_agc") and dev.gains:
                self._rx_cfg.gain_db = float(dev.gains[0])
            resume = self._pause_pipeline("sweeping")
            self._sweeper = sweeper
        log.info("%s: tarama başladı %s – %s (%d adım)", self.name, fmt_hz(cfg.start_hz), fmt_hz(cfg.stop_hz),
                 len(sweeper.plan()))
        result: SweepResult | None = None
        try:
            result = sweeper.run()
        finally:
            with self._cmd_lock:
                self._sweeper = None
                restore_err: Exception | None = None
                try:
                    self._restore_after_sweep()
                except Exception as e:  # never leave the node 'sweeping'
                    # e.g. USB still down: never stream with the sweep's last settings — the
                    # retry loop (or start()) re-applies the RX settings before streaming
                    restore_err = e
                    self._reconfigure_pending = True
                # Resume whenever the node runs — also when it was in 'error' before the sweep
                # (no streamer): a failed (re)start enters 'error' and keeps retrying.
                if resume and not self._stop_evt.is_set():
                    if restore_err is not None:
                        self._stream_restart_failed(restore_err)
                    else:
                        try:
                            self._start_streamer()
                        except EDSError as e:
                            self._stream_restart_failed(e)
                else:
                    with self._lock:
                        if self._state == "sweeping":
                            self._state = "idle"
                    if restore_err is not None:
                        self._warn(f"{self.name}: tarama sonrası RX ayarları geri yüklenemedi: {restore_err}")
        self._count("sweeps")
        payload = result.to_bus_payload(self.name, int(cfg.publish_max_points))
        self._publish(RX_SWEEP, payload)
        log.info("%s: tarama bitti: %.3f s, %.3g GHz/s, %d tespit", self.name, result.duration_s,
                 result.rate_hz_per_s / 1e9, len(result.detections))
        return {"duration_s": float(result.duration_s), "rate_hz_per_s": float(result.rate_hz_per_s),
                "n_detections": len(result.detections), "sweep_id": int(result.sweep_id),
                "n_steps": int(result.n_steps), "rbw_hz": float(result.rbw_hz)}

    def _restore_after_sweep(self) -> None:
        dev = self._dev
        for w in dev.configure(self._rx_cfg):
            self._warn(w, log_it=False)
        if self._gain_mode == "soft_agc":
            self._enable_soft_agc()

    def _stream_restart_failed(self, e: BaseException) -> None:
        with self._lock:
            self._state = "error"
            self._counters["stream_errors"] += 1
        self._last_restart = mono()
        msg = f"{self.name}: tarama sonrası akış yeniden başlatılamadı: {e}"
        log.error(msg)
        self._warn(msg, log_it=False)

    # ------------------------------------------------------------------ bus / bookkeeping
    def _publish(self, topic: str, data: dict[str, Any]) -> dict[str, Any] | None:
        pub = self._pub
        try:
            env = pub.publish(topic, data)
        except EDSError as e:
            n = self._count("publish_errors")
            if n <= 3 or n % _LOG_EVERY == 0:
                log.error("%s: '%s' yayınlanamadı (%d. hata): %s", self.name, topic, n, e)
            self._warn(f"{self.name}: '{topic}' yayınlanamadı: {e}", log_it=False)
            return None
        hook = self._on_publish
        if hook is not None:
            try:
                hook(topic, env)
            except Exception:
                log.exception("%s: on_publish kancası hata verdi (topic %s)", self.name, topic)
        return env

    def _count(self, key: str, k: int = 1) -> int:
        with self._lock:
            self._counters[key] += k
            return int(self._counters[key])

    def _warn(self, msg: str, log_it: bool = True) -> None:
        if log_it:
            log.warning(msg)
        with self._lock:
            if msg in self._warnings:
                return
            self._warnings.append(msg)
            if len(self._warnings) > MAX_WARNINGS:
                del self._warnings[: len(self._warnings) - MAX_WARNINGS]
