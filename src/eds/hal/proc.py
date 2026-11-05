"""Process isolation: run a receiver or a signal source in its own worker process.

Why
---
SoapyPlutoSDR keeps ONE libiio context per *process*: ``PlutoSDR_Settings.cpp`` declares
``static iio_context *ctx = nullptr;``, the constructor only creates a context while that
static is null (otherwise it reuses it, whatever ``uri`` / ``hostname`` the new device was
opened with), and the destructor runs ``iio_context_destroy(ctx); ctx = nullptr;``. So a
second ``SoapySDR.Device(driver=plutosdr, uri=<Pluto B>)`` in the same process silently
drives Pluto A (shared clock chain, EBUSY on the second RX buffer, TX from the wrong
radio), and closing either device frees the context the other one still uses. Phase 0
needs two Plutos at once (2x Pluto DF group, Pluto B as ``pluto_tx`` stimulus), so each
extra Pluto lives in its own process, where it has its own static context.

What
----
* :class:`ProcessRxDevice` — an :class:`~eds.hal.base.RxDevice` whose backend device runs in
  a child process (``multiprocessing`` *spawn* context). The child rebuilds the device with
  :func:`eds.hal.factory.open_device` from plain configuration data (``isolation`` forced to
  ``"inproc"``; the parent's attached ``SimWorld`` and its locks are never pickled). The
  parent keeps all :class:`RxDevice` base logic (profile validation, gain clamping, settling
  bookkeeping, gap-free read assembly, statistics) and forwards only the backend primitives
  (``_apply_*``, ``_start``, ``_read_raw``, ``_stop``, ``_close``) and the optional features
  (``set_tracking``, ``set_ppm_correction``, ``read_sensors``, ``runtime_limits``,
  ``describe``) as RPCs. ``info`` and the device state (fc, fs, bw, gains, gain mode, ppm,
  new warnings) are mirrored from the child after every control call.
* :class:`ProcessSignalSource` — a :class:`~eds.hal.tx.SignalSource` proxy running any
  source class (default use: ``eds.hal.tx_hw:SoapyTxSource``) in a child process. Method
  calls (including ``set_lo_offset``) are forwarded; ``name``, ``absolute_power``,
  ``max_tones``, ``power_range_db`` and ``freq_range_hz`` are mirrored as plain attributes;
  other public attributes are fetched on access. When the parent disappears the child turns
  the RF off and closes the source.

Transport
---------
Two duplex ``multiprocessing`` pipes per receiver, each served by its own thread in the
child, so a blocking read never delays a retune / ``stop_stream`` (same concurrency as an
in-process driver):

* **control**: pickled ``(op, args, sync)`` requests (``sync`` = the parent's
  ``buffer_size`` / ``settle_buffers``, which the sim's stale-buffer model reads), pickled
  ``("ok" | "err", payload, state)`` replies.
* **data**: request ``struct <qd`` (n, timeout_s); reply header ``struct <BBxxiqqq``
  (status, flags overflow_before|timeout|has_hw_time, n_channels, k, t_ns, hw_time_ns)
  followed by the channel gains (float64, mirrored in hardware-AGC modes) and then the raw
  complex64 buffer of shape (n_channels, k) via ``send_bytes`` / ``recv_bytes_into`` (no
  array pickling). An error reply (status 1) is followed by a pickled error record.

Errors raised in the child are re-raised in the parent as the same
:mod:`eds.core.errors` class with the same message (``ProfileViolation`` with the same
``violations``); the child's formatted traceback is attached as ``err.child_traceback``
and as the ``__cause__`` (:class:`RemoteTraceback`). A non-EDS exception becomes
:class:`~eds.core.errors.DeviceError` naming the original type.

Lifecycle: ``close()`` asks the child to close the device, the child exits, the parent
reaps it (no zombie). If the child dies, the pending call (and every later one) raises
:class:`~eds.core.errors.DeviceError` (:class:`~eds.core.errors.StreamError` on the read
path); ``close()`` of a dead proxy only reaps. A proxy that is garbage-collected or still
open at interpreter exit is shut down by a ``multiprocessing.util.Finalize`` (runs before
``multiprocessing`` terminates daemonic children): closing the pipes makes the child close
its device and exit, with terminate/kill as the fallback. The child ignores SIGINT so a
Ctrl-C in the terminal reaches only the parent, which then closes the devices in order.

Costs / limits
--------------
* Opening spawns a Python interpreter (~0.15-1 s incl. numpy/eds imports). As with any
  ``multiprocessing`` *spawn* program, a user script that opens an isolated device must
  keep its top level behind ``if __name__ == "__main__":`` (the ``eds`` CLI, ``python -m
  eds`` and pytest already do); otherwise the worker re-imports the script and fails.
* Each read costs one pipe round trip (tens of µs) plus two memory copies; the measured
  transport throughput (~250-320 MS/s complex64 on a 4-core dev VM,
  ``tests/hal/test_proc.py``) is far above the Pluto's USB 2.0 rate.
* Calls are serialized per pipe: one read at a time on the data pipe, one control call at a
  time on the control pipe (each with its own lock). A call interrupted while waiting for
  its reply (Ctrl-C) leaves the pipes in step (:class:`_Pipe`); the samples of an
  interrupted read are dropped and the next block is flagged ``overflow`` (gap before it).
* Only the :class:`RxDevice` interface crosses the process boundary; backend-specific
  extras (``SoapyRxDevice.thin_layer()``, ``SimRxDevice.true_ppm`` ...) are not reachable
  (their values appear in :meth:`ProcessRxDevice.describe`). ``backend_name`` is
  ``"proc"``; ``info.backend`` is the child's backend.
* A ``sim`` device forced into a subprocess builds its OWN ``SimWorld`` from ``app.sim``:
  the shared scene is lost (no common geometry / emitter switching with other devices or a
  ``sim`` source in the parent, every isolated device registers as the world's first device
  for the default ppm). Use it only to exercise the transport; ``factory`` never isolates a
  sim device unless ``isolation = "subprocess"`` is forced.
"""

from __future__ import annotations

import dataclasses
import importlib
import logging
import multiprocessing
import os
import signal
import struct
import threading
import time
import traceback
import typing
from collections.abc import Callable, Mapping, Sequence
from functools import partial
from multiprocessing import util as mp_util
from typing import Any

import numpy as np

from ..core import errors as _errors
from ..core.config import AppConfig, DeviceConfig
from ..core.errors import ConfigError, DeviceError, EDSError, ProfileViolation, StreamError
from ..core.profile import load_profile
from ..core.timeutil import now_ns
from .base import RawRead, RxDevice
from .tx import SignalSource

log = logging.getLogger(__name__)

_CTX = multiprocessing.get_context("spawn")

_READ_REQ = struct.Struct("<qd")           # n, timeout_s
_READ_HDR = struct.Struct("<BBxxiqqq")     # status, flags, n_channels, k, t_ns, hw_time_ns
_F_OVERFLOW, _F_TIMEOUT, _F_HWTIME = 1, 2, 4
_POLL_S = 0.25                             # liveness check period while waiting for a reply
_EXIT_PRIORITY = 10                        # Finalize: before multiprocessing kills daemons

DEFAULT_OPENER = "eds.hal.factory:open_device"


class RemoteTraceback(Exception):
    """The worker's formatted traceback, attached as ``__cause__`` of a re-raised error."""

    def __init__(self, tb: str):
        super().__init__(tb)
        self.tb = tb

    def __str__(self) -> str:
        return self.tb


class _WorkerHung(Exception):
    """No reply within the deadline although the worker is alive (parent-internal)."""


class _CallableAttr:
    """Marker returned by the source worker for a callable attribute."""


# ---------------------------------------------------------------------------- helpers
def app_to_plain(app: AppConfig) -> dict[str, Any]:
    """``AppConfig`` as plain builtins (declared fields only: an attached ``SimWorld`` with
    its locks is not a field and is therefore never pickled)."""
    return dataclasses.asdict(app)


def app_from_plain(d: Mapping[str, Any]) -> AppConfig:
    """Inverse of :func:`app_to_plain` (no re-validation: the data came from a valid config)."""
    hints = typing.get_type_hints(AppConfig)
    kw: dict[str, Any] = {}
    for f in dataclasses.fields(AppConfig):
        if f.name not in d:
            continue
        v = d[f.name]
        tp = hints[f.name]
        if f.name == "devices":
            kw[f.name] = [DeviceConfig(**dict(x)) for x in v]
        elif dataclasses.is_dataclass(tp) and isinstance(tp, type):
            kw[f.name] = tp(**dict(v))
        else:
            kw[f.name] = v
    return AppConfig(**kw)


def target_path(target: str | Callable[..., Any]) -> str:
    """``"module:qualname"`` of an importable class/function (validated, not imported)."""
    if isinstance(target, str):
        mod, sep, qual = target.partition(":")
        if not (mod and sep and qual):
            raise ConfigError(f"Alt süreç hedefi 'modül:ad' biçiminde olmalı (geldi: {target!r})")
        return target
    mod, qual = getattr(target, "__module__", ""), getattr(target, "__qualname__", "")
    if not mod or not qual or "<locals>" in qual or "<lambda>" in qual or mod == "__main__":
        raise ConfigError(f"Alt süreç hedefi içe aktarılabilir bir modül düzeyi nesne olmalı (geldi: {target!r})")
    return f"{mod}:{qual}"


def resolve_target(path: str) -> Any:
    mod, _, qual = target_path(path).partition(":")
    obj: Any = importlib.import_module(mod)
    for part in qual.split("."):
        obj = getattr(obj, part)
    return obj


def pack_error(e: BaseException) -> dict[str, Any]:
    """Picklable record of an exception raised in a worker."""
    return {
        "classes": [c.__name__ for c in type(e).__mro__ if c.__module__ == _errors.__name__],
        "type": f"{type(e).__module__}.{type(e).__qualname__}",
        "message": str(e),
        "violations": list(e.violations) if isinstance(e, ProfileViolation) else None,
        "traceback": "".join(traceback.format_exception(type(e), e, e.__traceback__)),
        "pid": os.getpid(),
    }


def rebuild_error(rec: Mapping[str, Any]) -> EDSError:
    """The :mod:`eds.core.errors` exception described by :func:`pack_error`."""
    cls: type[EDSError] = DeviceError
    for name in rec.get("classes") or ():
        c = getattr(_errors, name, None)
        if isinstance(c, type) and issubclass(c, EDSError):
            cls = c
            break
    msg = str(rec.get("message", ""))
    if not rec.get("classes"):
        msg = f"alt süreçte {rec.get('type', '?')}: {msg}"
    err: EDSError
    try:
        if issubclass(cls, ProfileViolation):
            err = cls(list(rec.get("violations") or [msg]))
        else:
            err = cls(msg)
    except Exception:  # exotic constructor signature
        err = DeviceError(msg)
    err.child_traceback = str(rec.get("traceback", ""))  # type: ignore[attr-defined]
    return err


def _raise_remote(rec: Mapping[str, Any]) -> typing.NoReturn:
    err = rebuild_error(rec)
    raise err from RemoteTraceback(str(rec.get("traceback", "")))


_TRUE = ("1", "true", "yes", "on", "evet")
_FALSE = ("0", "false", "no", "off", "hayır", "hayir")


def pluto_tx_isolated(args: Mapping[str, Any] | None) -> bool:
    """True if ``open_source("pluto_tx", args)`` runs the TX Pluto in its own worker process
    (``args["isolate"]``, default True)."""
    v = (args or {}).get("isolate", True)
    if isinstance(v, bool):
        return v
    if isinstance(v, (int, float)):
        return bool(v)
    s = str(v).strip().lower()
    if s in _TRUE:
        return True
    if s in _FALSE:
        return False
    raise ConfigError(f"measure.source_args.isolate true/false olmalı (geldi: {v!r})")


def _log_level() -> int:
    return logging.getLogger("eds").getEffectiveLevel()


def _child_setup(log_level: int) -> None:
    """Worker process setup: Ctrl-C goes to the parent only; logs tagged with the pid."""
    try:
        signal.signal(signal.SIGINT, signal.SIG_IGN)
    except (ValueError, OSError):  # pragma: no cover - not the main thread / exotic platform
        pass
    logging.basicConfig(level=log_level, format="%(levelname)s %(name)s [alt süreç %(process)d]: %(message)s")


def _wait_readable(conn: Any, proc: Any, timeout_s: float) -> None:
    """Block until ``conn`` has data or EOF. ``EOFError`` if the worker died without
    replying, :class:`_WorkerHung` after ``timeout_s`` while it is still alive."""
    end = time.monotonic() + max(float(timeout_s), 0.0)
    while True:
        if conn.poll(_POLL_S):
            return
        if not proc.is_alive():
            if conn.poll(0):
                return
            raise EOFError("alt süreç yanıt vermeden sonlandı")
        if time.monotonic() >= end:
            raise _WorkerHung()


class _Pipe:
    """One request/reply connection to a worker, kept in step across interrupted calls.

    A call interrupted while *waiting* for its reply (KeyboardInterrupt in the main thread)
    leaves that reply in the pipe; it is recorded in ``owed`` and the next call reads and
    drops it first, so requests and replies never shift by one. Every state change (a
    message sent + ``owed`` appended, a reply read + ``owed`` popped) happens inside
    :meth:`io`; an interruption *inside* a message marks the pipe ``broken`` (the byte
    stream can no longer be parsed) and further use raises ``OSError``.
    """

    def __init__(self, conn: Any, proc: Any):
        self.conn = conn
        self.proc = proc
        self.lock = threading.Lock()
        self.owed: list[float] = []           # wait budget of each reply not read yet
        self.broken = False

    def _check(self) -> None:
        if self.broken:
            raise OSError("bağlantı bir kesinti nedeniyle senkronunu kaybetti")

    def io(self, fn: Callable[[], Any], *, settle: bool = False) -> Any:
        self._check()
        try:
            out = fn()
            if settle:
                self.owed.pop(0)
            return out
        except (EOFError, OSError):
            raise
        except BaseException:
            self.broken = True
            raise

    def request(self, send: Callable[[Any], Any], msg: Any, wait_s: float) -> None:
        def go() -> None:
            send(msg)
            self.owed.append(float(wait_s))
        self.io(go)

    def wait(self, wait_s: float) -> None:
        self._check()
        _wait_readable(self.conn, self.proc, wait_s)


def _reap_worker(proc: Any, conns: Sequence[Any], join_s: float) -> None:
    """Close our pipe ends (the worker sees EOF, closes its device and exits), then make sure
    the process is gone and reaped: join → terminate → kill."""
    for c in conns:
        try:
            c.close()
        except OSError:
            pass
    try:
        proc.join(join_s)
        if proc.is_alive():
            log.warning("alt süreç (pid %s) %.1f s içinde çıkmadı; sonlandırılıyor", proc.pid, join_s)
            proc.terminate()
            proc.join(2.0)
        if proc.is_alive():
            proc.kill()
            proc.join(2.0)
    except (OSError, ValueError, AssertionError) as e:  # never started / already closed
        log.debug("alt süreç toplanamadı: %s", e)


def _start_worker(target: Callable[..., None], args: tuple[Any, ...], n_pipes: int,
                  name: str) -> tuple[Any, list[Any]]:
    """Start a daemonic spawn-context worker with ``n_pipes`` duplex pipes; returns
    ``(process, parent_ends)``. The child ends are closed in the parent after the start so
    that the parent sees EOF as soon as the child dies."""
    pairs = [_CTX.Pipe(duplex=True) for _ in range(n_pipes)]
    proc = _CTX.Process(target=target, args=(*[c for _, c in pairs], *args), name=name, daemon=True)
    try:
        proc.start()
    except Exception as e:
        for p, c in pairs:
            p.close()
            c.close()
        raise DeviceError(f"{name}: alt süreç başlatılamadı: {e}") from e
    for _, c in pairs:
        c.close()
    return proc, [p for p, _ in pairs]


# ---------------------------------------------------------------------------- RX worker
class _RxServer:
    """Child side of :class:`ProcessRxDevice`: control loop (main thread) + data thread."""

    def __init__(self, dev: RxDevice, ctl: Any, data: Any):
        self.dev = dev
        self.ctl = ctl
        self.data = data
        self._warn_sent = 0
        self._info_sent: dict[str, Any] | None = None

    def state(self) -> dict[str, Any]:
        d = self.dev
        st: dict[str, Any] = {
            "fc": float(d.fc), "fs": float(d.fs), "bw": float(d.bw), "gains": [float(g) for g in d.gains],
            "gain_mode": str(d.gain_mode), "ppm_hw": float(d.ppm_hw), "ppm_hw_via": str(d.ppm_hw_via),
        }
        if len(d.warnings) < self._warn_sent:
            self._warn_sent = 0
        if len(d.warnings) > self._warn_sent:
            st["warnings"] = [str(w) for w in d.warnings[self._warn_sent:]]
            self._warn_sent = len(d.warnings)
        info = d.info.to_dict()
        if info != self._info_sent:
            st["info"] = info
            self._info_sent = info
        return st

    def run(self) -> None:
        d = self.dev
        hello = {"backend_name": str(getattr(d, "backend_name", "")), "class": type(d).__qualname__,
                 "pid": os.getpid(), "num_channels": int(d.num_channels)}
        try:
            self.ctl.send(("ok", hello, self.state()))
        except Exception as e:
            log.error("açılış yanıtı gönderilemedi: %s", e)
            self._close_quietly()
            return
        threading.Thread(target=self._serve_data, name="eds-proc-data", daemon=True).start()
        closed = False
        try:
            closed = self._serve_ctl()
        finally:
            if not closed:          # parent gone (EOF) or loop failure: release the hardware
                self._close_quietly()

    def _close_quietly(self) -> None:
        try:
            self.dev.close()
        except Exception as e:
            log.warning("%s: cihaz kapatılamadı: %s", self.dev.name, e)

    def _dispatch(self, op: str, args: tuple[Any, ...]) -> Any:
        d = self.dev
        if op == "apply_sample_rate":
            d.fs = float(d._apply_sample_rate(*args))
            return d.fs
        if op == "apply_bandwidth":
            d.bw = float(d._apply_bandwidth(*args))
            return d.bw
        if op == "apply_center_freq":
            d.fc = float(d._apply_center_freq(*args))
            return d.fc
        if op == "apply_gain":
            db, ch = args
            g = float(d._apply_gain(db, ch))
            d.gains[ch] = g
            return g
        if op == "apply_gain_mode":
            d._apply_gain_mode(*args)
            d.gain_mode = str(args[0])
            return None
        if op == "start":
            d.buffer_size = int(args[0])
            d._start(int(args[0]))
            d._streaming = True
            return None
        if op == "stop":
            try:
                d._stop()
            finally:
                d._streaming = False
            return None
        if op == "close":
            d.close()
            return None
        if op == "set_tracking":
            return d.set_tracking(*args)
        if op == "set_ppm_correction":
            return d.set_ppm_correction(*args)
        if op == "read_sensors":
            return d.read_sensors()
        if op == "runtime_limits":
            return d.runtime_limits()
        if op == "describe":
            return d.describe()
        raise DeviceError(f"alt süreç: bilinmeyen işlem {op!r}")

    def _serve_ctl(self) -> bool:
        """Control loop; True after a ``close`` request was handled, False on EOF."""
        conn, d = self.ctl, self.dev
        while True:
            try:
                op, args, sync = conn.recv()
            except (EOFError, OSError):
                return False
            try:
                if sync:
                    d.buffer_size = int(sync.get("buffer_size", d.buffer_size))
                    d.settle_buffers = int(sync.get("settle_buffers", d.settle_buffers))
                reply = ("ok", self._dispatch(op, tuple(args)), self.state())
            except Exception as e:
                reply = ("err", pack_error(e), self.state())
            try:
                conn.send(reply)
            except (EOFError, OSError):
                return op == "close"
            except Exception as e:  # unpicklable result
                conn.send(("err", pack_error(DeviceError(f"{d.name}: {op} yanıtı aktarılamadı: {e}")), self.state()))
            if op == "close":
                return True

    def _serve_data(self) -> None:
        conn, d = self.data, self.dev
        while True:
            try:
                req = conn.recv_bytes()
            except (EOFError, OSError):
                return
            try:
                n, timeout_s = _READ_REQ.unpack(req)
                r = d._read_raw(int(n), float(timeout_s))
                x = np.ascontiguousarray(r.samples, dtype=np.complex64)
                if x.ndim == 1:
                    x = x.reshape(1, -1)
                nch, k = int(x.shape[0]), int(x.shape[1])
                flags = ((_F_OVERFLOW if r.overflow_before else 0) | (_F_TIMEOUT if r.timeout else 0)
                         | (_F_HWTIME if r.hw_time_ns is not None else 0))
                gains = [float(g) for g in d.gains]
                head = (_READ_HDR.pack(0, flags, nch, k, int(r.t_ns), int(r.hw_time_ns or 0))
                        + struct.pack(f"<{len(gains)}d", *gains))
            except Exception as e:
                try:
                    conn.send_bytes(_READ_HDR.pack(1, 0, 0, 0, 0, 0))
                    conn.send(pack_error(e))
                except (EOFError, OSError):
                    return
                continue
            try:
                conn.send_bytes(head)
                if k:
                    conn.send_bytes(x.reshape(-1).view(np.uint8))
            except (EOFError, OSError, ValueError):
                return


def _rx_child_main(ctl: Any, data: Any, dev_plain: dict[str, Any], app_plain: dict[str, Any] | None,
                   opener: str, log_level: int) -> None:
    """Entry point of a :class:`ProcessRxDevice` worker (spawned)."""
    _child_setup(log_level)
    try:
        cfg = DeviceConfig(**dev_plain)
        cfg.isolation = "inproc"
        app = app_from_plain(app_plain) if app_plain is not None else None
        dev = resolve_target(opener)(cfg, app)
        if not isinstance(dev, RxDevice) or isinstance(dev, ProcessRxDevice):
            raise ConfigError(f"{cfg.name}: alt süreç açıcısı {opener} süreç içi bir RxDevice döndürmedi "
                              f"({type(dev).__name__})")
    except BaseException as e:
        try:
            ctl.send(("err", pack_error(e), None))
        except Exception:
            pass
        return
    _RxServer(dev, ctl, data).run()


# ---------------------------------------------------------------------------- RX proxy
class ProcessRxDevice(RxDevice):
    """:class:`RxDevice` whose backend runs in a worker process (see module docstring).

    ``cfg`` is the device configuration (its ``isolation`` is ignored here; the child always
    opens in-process). ``app`` supplies ``rx.stream_format`` and the ``[sim]`` scene, sent as
    plain data. ``opener`` (``"module:function"``, called as ``fn(cfg, app)`` in the child)
    defaults to :func:`eds.hal.factory.open_device`; tests pass fakes.
    """

    backend_name = "proc"
    start_timeout_s: float = 120.0      # spawn + imports + device open (libiio network contexts)
    rpc_timeout_s: float = 60.0         # control call without reply while the worker is alive
    read_margin_s: float = 10.0         # read reply deadline = timeout_s + this
    join_timeout_s: float = 5.0         # wait for the worker to exit after close/EOF

    def __init__(self, cfg: DeviceConfig, app: AppConfig | None = None, *,
                 opener: str | Callable[..., RxDevice] | None = None):
        super().__init__(cfg, load_profile(cfg.profile))
        self._dead: str | None = None
        self._closed = False
        self._pending_gap = False           # samples of an interrupted read were dropped
        self.child_backend = ""
        self.child_class = ""
        self.opener = target_path(opener) if opener is not None else DEFAULT_OPENER
        dev_plain = dataclasses.asdict(dataclasses.replace(cfg, isolation="inproc"))
        app_plain = app_to_plain(app) if app is not None else None
        self._proc, (ctl, data) = _start_worker(
            _rx_child_main, (dev_plain, app_plain, self.opener, _log_level()), 2, f"eds-dev-{cfg.name}")
        self._ctl, self._data = _Pipe(ctl, self._proc), _Pipe(data, self._proc)
        self._finalizer = mp_util.Finalize(self, _reap_worker, args=(self._proc, (ctl, data), self.join_timeout_s),
                                           exitpriority=_EXIT_PRIORITY)
        try:
            _wait_readable(ctl, self._proc, self.start_timeout_s)
            status, hello, state = ctl.recv()
        except _WorkerHung:
            self._finalizer()
            raise DeviceError(f"{cfg.name}: cihaz alt süreci {self.start_timeout_s:.0f} s içinde açılmadı") from None
        except (EOFError, OSError) as e:
            self._finalizer()
            raise DeviceError(f"{cfg.name}: cihaz alt süreci açılışta sonlandı "
                              f"(çıkış kodu {self._proc.exitcode}): {e}") from None
        except BaseException:
            self._finalizer()
            raise
        if status != "ok":
            self._finalizer()
            _raise_remote(hello)
        self.child_backend = str(hello.get("backend_name", ""))
        self.child_class = str(hello.get("class", ""))
        if int(hello.get("num_channels", self.num_channels)) != self.num_channels:
            self._finalizer()
            raise ConfigError(f"{cfg.name}: alt süreçteki cihaz {hello.get('num_channels')} kanal bildiriyor, "
                              f"konfigürasyon {self.num_channels}")
        self._mirror(state)
        log.info("%s: %s arka ucu alt süreçte (pid %s)", self.name, self.child_backend, self.pid)

    # ------------------------------------------------------------------ worker state
    @property
    def pid(self) -> int | None:
        return self._proc.pid

    @property
    def alive(self) -> bool:
        return not self._closed and self._dead is None and self._proc.is_alive()

    def _mirror(self, st: Mapping[str, Any] | None) -> None:
        if not st:
            return
        self.fc, self.fs, self.bw = float(st["fc"]), float(st["fs"]), float(st["bw"])
        for i, g in enumerate(list(st["gains"])[: len(self.gains)]):
            self.gains[i] = float(g)
        self.gain_mode = str(st["gain_mode"])
        self.ppm_hw, self.ppm_hw_via = float(st["ppm_hw"]), str(st["ppm_hw_via"])
        for w in st.get("warnings", ()):
            self.warnings.append(str(w))
        info = st.get("info")
        if info:
            for k, v in info.items():
                if k != "name" and hasattr(self.info, k):
                    setattr(self.info, k, dict(v) if isinstance(v, dict) else v)
            self.info.extra["isolation"] = "subprocess"

    def _dead_message(self) -> str:
        return self._dead or f"{self.name}: cihaz alt süreci sonlandı"

    def _mark_dead(self, why: str) -> None:
        if self._dead is not None:
            return
        try:
            self._proc.join(0.5)
        except (OSError, ValueError, AssertionError):
            pass
        if self._proc.is_alive():
            self._dead = f"{self.name}: cihaz alt süreciyle iletişim bozuldu ({why}); cihazı yeniden açın"
        else:
            self._dead = (f"{self.name}: cihaz alt süreci (pid {self._proc.pid}) beklenmedik şekilde sonlandı "
                          f"(çıkış kodu {self._proc.exitcode}; {why}); cihazı yeniden açın")
        log.error("%s", self._dead)

    def _kill_hung(self, what: str, waited_s: float) -> str:
        msg = (f"{self.name}: cihaz alt süreci {what} isteğine {waited_s:.1f} s içinde yanıt vermedi; "
               "alt süreç sonlandırıldı")
        self._dead = msg
        log.error("%s", msg)
        try:
            self._proc.kill()
            self._proc.join(2.0)
        except (OSError, ValueError, AssertionError):
            pass
        return msg

    def _check_usable(self, exc: type[EDSError]) -> None:
        if self._closed:
            raise exc(f"{self.name}: cihaz kapalı")
        if self._dead is not None:
            raise exc(self._dead_message())

    def _rpc(self, op: str, *args: Any, timeout_s: float | None = None) -> Any:
        wait_s = float(timeout_s if timeout_s is not None else self.rpc_timeout_s)
        p = self._ctl
        with p.lock:
            self._check_usable(DeviceError)
            sync = {"buffer_size": int(self.buffer_size), "settle_buffers": int(self.settle_buffers)}
            try:
                while p.owed:               # reply of an interrupted call: keep its state, drop its result
                    p.wait(p.owed[0])
                    self._mirror(p.io(p.conn.recv, settle=True)[2])
                p.request(p.conn.send, (op, args, sync), wait_s)
                p.wait(wait_s)
                status, payload, state = p.io(p.conn.recv, settle=True)
            except _WorkerHung:
                raise DeviceError(self._kill_hung(op, wait_s)) from None
            except (EOFError, OSError) as e:
                self._mark_dead(f"{op}: {e or type(e).__name__}")
                raise DeviceError(self._dead_message()) from None
        self._mirror(state)
        if status == "ok":
            return payload
        _raise_remote(payload)

    def _recv_read_reply(self) -> tuple[Any, ...]:
        """One complete read reply: header [+ samples | error record] (one :meth:`_Pipe.io`)."""
        conn = self._data.conn
        head = conn.recv_bytes()
        status, flags, nch, k, t_ns, hw = _READ_HDR.unpack_from(head)
        if status:
            return status, flags, k, t_ns, hw, (), conn.recv()
        n_g = (len(head) - _READ_HDR.size) // 8
        gains = struct.unpack_from(f"<{n_g}d", head, _READ_HDR.size)
        out = np.empty((nch, k), dtype=np.complex64)
        if k:
            got = conn.recv_bytes_into(out.reshape(-1).view(np.uint8))
            if got != out.nbytes:
                raise OSError(f"örnek yükü {got} bayt, beklenen {out.nbytes}")
        return status, flags, k, t_ns, hw, gains, out
    # ------------------------------------------------------------------ primitives
    def _apply_sample_rate(self, hz: float) -> float:
        return float(self._rpc("apply_sample_rate", float(hz)))

    def _apply_bandwidth(self, hz: float) -> float:
        return float(self._rpc("apply_bandwidth", float(hz)))

    def _apply_center_freq(self, hz: float) -> float:
        return float(self._rpc("apply_center_freq", float(hz)))

    def _apply_gain(self, db: float, channel: int) -> float:
        return float(self._rpc("apply_gain", float(db), int(channel)))

    def _apply_gain_mode(self, mode: str) -> None:
        self._rpc("apply_gain_mode", str(mode))

    def _start(self, buffer_size: int) -> None:
        self._rpc("start", int(buffer_size))

    def _stop(self) -> None:
        if self._closed or self._dead is not None:
            return                      # nothing left to stop in a dead worker
        self._rpc("stop")

    def _read_raw(self, n: int, timeout_s: float) -> RawRead:
        wait_s = max(float(timeout_s), 0.0) + self.read_margin_s
        p = self._data
        with p.lock:
            self._check_usable(StreamError)
            try:
                while p.owed:               # reply of an interrupted read: its samples are a gap
                    p.wait(p.owed[0])
                    st, fl, k_old = p.io(self._recv_read_reply, settle=True)[:3]
                    if st == 0 and (k_old or fl & _F_OVERFLOW):
                        self._pending_gap = True
                p.request(p.conn.send_bytes, _READ_REQ.pack(int(n), float(timeout_s)), wait_s)
                p.wait(wait_s)
                status, flags, k, t_ns, hw, gains, body = p.io(self._recv_read_reply, settle=True)
            except _WorkerHung:
                raise StreamError(self._kill_hung("okuma", wait_s)) from None
            except (EOFError, OSError) as e:
                self._mark_dead(f"okuma: {e or type(e).__name__}")
                raise StreamError(self._dead_message()) from None
        if status:
            _raise_remote(body)
        if self.gain_mode != "manual":  # hardware AGC moves the gain inside the worker
            for i, g in enumerate(gains[: len(self.gains)]):
                self.gains[i] = float(g)
        timeout = bool(flags & _F_TIMEOUT)
        overflow = bool(flags & _F_OVERFLOW)
        if self._pending_gap and not timeout:
            overflow, self._pending_gap = True, False
        return RawRead(samples=body, overflow_before=overflow, timeout=timeout, t_ns=int(t_ns),
                       hw_time_ns=int(hw) if flags & _F_HWTIME else None)

    def _close(self) -> None:
        if self._closed:
            return
        err: EDSError | None = None
        clean = False
        try:
            if self._dead is None:
                try:
                    self._rpc("close")
                    clean = True
                except EDSError as e:
                    if self._dead is None:
                        clean, err = True, e     # the backend's own close error: same as in-process
                    else:
                        log.warning("%s", e)
        finally:
            self._closed = True
            self._shutdown(clean)
        if err is not None:
            raise err

    def _shutdown(self, clean: bool) -> None:
        """Reap the worker, then release the pipes. After a handled ``close`` the worker exits
        by itself; otherwise closing the control pipe (EOF) makes it close its device and
        exit. The data pipe is closed only once an in-flight read has returned (EOF from the
        exited worker), so a reader thread never touches a recycled file descriptor."""
        if not clean:
            got = self._ctl.lock.acquire(timeout=self.join_timeout_s)
            try:
                self._ctl.conn.close()
            except OSError:
                pass
            finally:
                if got:
                    self._ctl.lock.release()
        try:
            self._proc.join(self.join_timeout_s)
        except (OSError, ValueError, AssertionError):
            pass
        got = self._data.lock.acquire(timeout=self.join_timeout_s)
        try:
            self._finalizer()
        finally:
            if got:
                self._data.lock.release()

    # ------------------------------------------------------------------ optional features
    def set_tracking(self, dc: bool | None = None, rf_dc: bool | None = None,
                     quadrature: bool | None = None) -> dict[str, str]:
        with self._lock:
            res = dict(self._rpc("set_tracking", dc, rf_dc, quadrature))
            if any(p != "unsupported" for p in res.values()):
                self.mark_settling()
            return res

    def set_ppm_correction(self, ppm: float) -> str:
        with self._lock:
            via = str(self._rpc("set_ppm_correction", ppm))
            if via != "none":
                self.mark_settling()
            return via

    def read_sensors(self) -> dict[str, float | str | None]:
        return dict(self._rpc("read_sensors"))

    def runtime_limits(self) -> dict[str, Any]:
        return dict(self._rpc("runtime_limits"))

    def describe(self) -> dict[str, Any]:
        """Backend-specific keys from the worker's ``describe()`` + this proxy's base state."""
        extra: dict[str, Any] = {}
        if self.alive:
            try:
                extra = dict(self._rpc("describe"))
            except EDSError as e:
                log.debug("%s: describe alt süreçten alınamadı: %s", self.name, e)
        out = dict(extra)
        out.update(super().describe())
        if "warnings" in extra:
            out["warnings"] = list(self.warnings)
        out["isolation"] = {"mode": "subprocess", "pid": self.pid, "alive": self.alive,
                            "backend": self.child_backend, "class": self.child_class}
        return out


# ---------------------------------------------------------------------------- source worker
_SOURCE_MIRRORED = ("name", "absolute_power", "max_tones", "power_range_db", "freq_range_hz")


def _source_snapshot(src: Any) -> dict[str, Any]:
    out: dict[str, Any] = {}
    for k in _SOURCE_MIRRORED:
        try:
            out[k] = getattr(src, k)
        except Exception as e:  # property raising: leave the mirror as it is
            log.debug("kaynak özniteliği %s okunamadı: %s", k, e)
    return out


def _source_child_main(conn: Any, target: str, args: tuple[Any, ...], kwargs: dict[str, Any],
                       log_level: int) -> None:
    """Entry point of a :class:`ProcessSignalSource` worker (spawned)."""
    _child_setup(log_level)
    try:
        src = resolve_target(target)(*args, **kwargs)
    except BaseException as e:
        try:
            conn.send(("err", pack_error(e), None))
        except Exception:
            pass
        return
    closed = False
    try:
        conn.send(("ok", None, _source_snapshot(src)))
        while True:
            try:
                op, name, a, kw = conn.recv()
            except (EOFError, OSError):
                break
            try:
                if op == "close":
                    closed = True
                    src.close()
                    reply: tuple[Any, ...] = ("ok", None, _source_snapshot(src))
                elif op == "getattr":
                    try:
                        v = getattr(src, name)
                    except AttributeError as e:
                        reply = ("noattr", str(e), _source_snapshot(src))
                    else:
                        reply = ("ok", _CallableAttr() if callable(v) else v, _source_snapshot(src))
                elif op == "call" and not str(name).startswith("_"):
                    res = getattr(src, name)(*a, **kw)
                    reply = ("ok", res, _source_snapshot(src))
                else:
                    raise DeviceError(f"alt süreç: geçersiz kaynak isteği {op!r} {name!r}")
            except Exception as e:
                reply = ("err", pack_error(e), _source_snapshot(src))
            try:
                conn.send(reply)
            except (EOFError, OSError):
                break
            except Exception as e:  # unpicklable result
                conn.send(("err", pack_error(DeviceError(f"kaynak {name} yanıtı aktarılamadı: {e}")),
                           _source_snapshot(src)))
            if closed:
                break
    finally:
        if not closed:              # parent gone: RF off first, then release the hardware
            try:
                src.set_enabled(False)
            except Exception as e:
                log.warning("kaynak RF kapatılamadı: %s", e)
            try:
                src.close()
            except Exception as e:
                log.warning("kaynak kapatılamadı: %s", e)


# ---------------------------------------------------------------------------- source proxy
class ProcessSignalSource(SignalSource):
    """:class:`SignalSource` running ``target(*args, **kwargs)`` in a worker process.

    ``target`` is a ``"module:qualname"`` string or an importable module-level class.
    Explicit methods are forwarded; any other public attribute is fetched from the worker
    on access (callables come back as forwarding functions).
    """

    start_timeout_s: float = 120.0
    rpc_timeout_s: float = 60.0
    join_timeout_s: float = 5.0

    def __init__(self, target: str | type, args: Sequence[Any] = (), kwargs: Mapping[str, Any] | None = None):
        self._closed = False
        self._dead: str | None = None
        self._target = target_path(target)
        label = self._target.rpartition(":")[2]
        self._proc, (conn,) = _start_worker(
            _source_child_main, (self._target, tuple(args), dict(kwargs or {}), _log_level()), 1,
            f"eds-src-{label}")
        self._pipe = _Pipe(conn, self._proc)
        self._finalizer = mp_util.Finalize(self, _reap_worker, args=(self._proc, (conn,), self.join_timeout_s),
                                           exitpriority=_EXIT_PRIORITY)
        try:
            _wait_readable(conn, self._proc, self.start_timeout_s)
            status, payload, snap = conn.recv()
        except _WorkerHung:
            self._finalizer()
            raise DeviceError(f"{label}: kaynak alt süreci {self.start_timeout_s:.0f} s içinde açılmadı") from None
        except (EOFError, OSError) as e:
            self._finalizer()
            raise DeviceError(f"{label}: kaynak alt süreci açılışta sonlandı "
                              f"(çıkış kodu {self._proc.exitcode}): {e}") from None
        except BaseException:
            self._finalizer()
            raise
        if status != "ok":
            self._finalizer()
            _raise_remote(payload)
        self._apply_snapshot(snap)
        log.info("%s: kaynak alt süreçte (pid %s)", self.name, self.pid)

    @property
    def pid(self) -> int | None:
        return self._proc.pid

    @property
    def alive(self) -> bool:
        return not self._closed and self._dead is None and self._proc.is_alive()

    def _apply_snapshot(self, snap: Mapping[str, Any] | None) -> None:
        for k, v in (snap or {}).items():
            if k in _SOURCE_MIRRORED:
                object.__setattr__(self, k, v)

    def _request(self, op: str, name: str = "", args: tuple[Any, ...] = (),
                 kwargs: Mapping[str, Any] | None = None) -> Any:
        p = self._pipe
        with p.lock:
            if self._closed:
                raise StreamError(f"{self.name}: kaynak kapalı")
            if self._dead is not None:
                raise DeviceError(self._dead)
            try:
                while p.owed:               # reply of an interrupted call: keep its snapshot only
                    p.wait(p.owed[0])
                    self._apply_snapshot(p.io(p.conn.recv, settle=True)[2])
                p.request(p.conn.send, (op, name, tuple(args), dict(kwargs or {})), self.rpc_timeout_s)
                p.wait(self.rpc_timeout_s)
                status, payload, snap = p.io(p.conn.recv, settle=True)
            except _WorkerHung:
                self._dead = (f"{self.name}: kaynak alt süreci {op} {name} isteğine {self.rpc_timeout_s:.0f} s "
                              "içinde yanıt vermedi; alt süreç sonlandırıldı")
                self._proc.kill()
                raise DeviceError(self._dead) from None
            except (EOFError, OSError) as e:
                try:
                    self._proc.join(0.5)
                except (OSError, ValueError, AssertionError):
                    pass
                why = e or type(e).__name__
                self._dead = (f"{self.name}: kaynak alt süreciyle iletişim bozuldu ({why})" if self._proc.is_alive()
                              else f"{self.name}: kaynak alt süreci (pid {self._proc.pid}) beklenmedik şekilde "
                                   f"sonlandı (çıkış kodu {self._proc.exitcode}; {why})")
                raise DeviceError(self._dead) from None
        self._apply_snapshot(snap)
        if status == "ok":
            return payload
        if status == "noattr":
            raise AttributeError(payload)
        _raise_remote(payload)

    def _call(self, method: str, *args: Any, **kwargs: Any) -> Any:
        return self._request("call", method, args, kwargs)

    # ------------------------------------------------------------------ SignalSource API
    def set_frequency(self, hz: float) -> float:
        return self._call("set_frequency", hz)

    def set_power(self, dbm: float) -> float:
        return self._call("set_power", dbm)

    def set_enabled(self, on: bool) -> int:
        if self._closed:
            if on:
                raise StreamError(f"{self.name}: kaynak kapalı")
            return now_ns()
        return self._call("set_enabled", bool(on))

    def set_tones(self, freqs_hz: list[float], powers: list[float]) -> None:
        return self._call("set_tones", list(freqs_hz), list(powers))

    def set_lo_offset(self, hz: float) -> float:
        return self._call("set_lo_offset", hz)

    def describe(self) -> dict[str, Any]:
        d: dict[str, Any] = {"name": self.name, "absolute_power": self.absolute_power}
        if self.alive:
            d = dict(self._call("describe"))
        d["isolation"] = {"mode": "subprocess", "pid": self.pid, "alive": self.alive, "target": self._target}
        return d

    def close(self) -> None:
        if self._closed:
            return
        err: EDSError | None = None
        try:
            if self._dead is None:
                try:
                    self._request("close")
                except EDSError as e:
                    if self._dead is None:
                        err = e
                    else:
                        log.warning("%s", e)
        finally:
            self._closed = True
            self._finalizer()
        if err is not None:
            raise err

    def __getattr__(self, name: str) -> Any:
        # only called for attributes not found normally; private names are never forwarded
        if name.startswith("_"):
            raise AttributeError(name)
        if self.__dict__.get("_closed", True) or self.__dict__.get("_dead") is not None:
            raise AttributeError(f"{name} (kaynak alt süreci kapalı)")
        value = self._request("getattr", name)
        if isinstance(value, _CallableAttr):
            return partial(self._call, name)
        return value
