"""Background RX streaming: one reader thread per device feeding a bounded host queue.

Why a dedicated reader thread
-----------------------------
On a USB 2.0 Pluto the device-side FIFO is only ``kernel_buffers x buffer_size`` samples
deep (4 x 65536 = 65 ms at 4 MS/s). If the consumer (DSP, bus publishing, AGC) stalls for
longer than that, the *hardware* overflows and samples are lost before they ever reach the
host. :class:`RxStreamer` therefore decouples the two: a reader thread does nothing but
``device.read(block_size)`` and appends to an in-memory queue, so the USB pipe is drained
at line rate even when the consumer is momentarily slow.

Overload policy: drop the OLDEST block
--------------------------------------
The host queue is bounded (``queue_blocks``). When it is full the **oldest** queued block is
discarded and :attr:`StreamStats.host_drops` is incremented — an electronic-support
receiver must show what is on the air *now*; a backlog of stale spectra is worse than a gap.
Every delivered block is still internally contiguous (HAL invariant); a host drop is only a
gap *between* blocks, visible through ``IQBlock.seq`` / ``IQBlock.index`` jumps. Driver
overflows (gaps before a block) are flagged by the HAL in ``IQBlock.overflow`` and counted
in ``StreamStats.overflows``; :attr:`RxStreamer.stats` reports both kinds.

Error policy
------------
* :class:`~eds.core.errors.StreamTimeout` is counted and the loop continues, up to
  ``max_consecutive_timeouts`` *consecutive* timeouts; then the reader stops with a
  :class:`~eds.core.errors.StreamError` in :attr:`RxStreamer.error`.
* Any other exception (driver failure, unplugged device, an exception raised by the
  ``on_block`` callback) is logged, stored in :attr:`RxStreamer.error` and stops the reader.
  Nothing is swallowed: consumers see ``running == False`` and :meth:`RxStreamer.check`
  re-raises the failure as a :class:`~eds.core.errors.StreamError`.

Stopping never hangs: :meth:`RxStreamer.stop` sets a stop event (checked between reads),
waits for at most ``read_timeout_s`` (+ margin) for the in-flight read, and — if the
streamer started the device stream — stops it, which unblocks a read stuck in the driver.

Thread safety: :meth:`get`, :meth:`drain`, :attr:`stats` and the counters may be used from
any thread; :meth:`start` / :meth:`stop` are serialized. ``on_block`` runs in the reader
thread and must be fast (e.g. :meth:`eds.rx.agc.GainController.update`), otherwise it is the
reader that falls behind and the *device* overflows.
"""

from __future__ import annotations

import dataclasses
import logging
import math
import threading
from collections import deque
from collections.abc import Callable

from ..core.errors import ConfigError, StreamError, StreamTimeout
from ..core.timeutil import mono
from ..core.types import IQBlock, StreamStats
from ..hal.base import RxDevice

log = logging.getLogger(__name__)

#: Callback invoked in the reader thread for every block read (after it was queued).
OnBlock = Callable[[IQBlock], None]

_JOIN_MARGIN_S = 1.0          # extra time granted to the in-flight read when stopping
_UNBLOCK_JOIN_S = 1.0         # wait after stopping the device stream to unblock a stuck read


class RxStreamer:
    """Continuous reader of ``device`` in a background thread, bounded drop-oldest queue.

    Parameters
    ----------
    device
        Any :class:`~eds.hal.base.RxDevice` (single device or ``DeviceGroup``).
    block_size
        Samples per channel per :class:`~eds.core.types.IQBlock`. Keep it ``<= device
        buffer_size`` so a gain/frequency change is fully covered by the HAL's settling
        window (``(kernel_buffers + 1) x buffer_size``).
    queue_blocks
        Host queue depth; when full the oldest block is dropped (``host_drops``).
    on_block
        Optional ``callable(block)`` run in the reader thread for every block.
    read_timeout_s
        Timeout of each ``device.read`` (also bounds how long :meth:`stop` waits).
    max_consecutive_timeouts
        Consecutive read timeouts tolerated before the reader enters the error state.
    """

    def __init__(self, device: RxDevice, block_size: int, queue_blocks: int = 32,
                 on_block: OnBlock | None = None, read_timeout_s: float = 2.0,
                 max_consecutive_timeouts: int = 3):
        if isinstance(block_size, bool) or int(block_size) != block_size or int(block_size) < 1:
            raise ConfigError(f"block_size pozitif tamsayı olmalı, {block_size!r} geldi")
        if isinstance(queue_blocks, bool) or int(queue_blocks) != queue_blocks or int(queue_blocks) < 1:
            raise ConfigError(f"queue_blocks pozitif tamsayı olmalı, {queue_blocks!r} geldi")
        if not (isinstance(read_timeout_s, (int, float)) and math.isfinite(read_timeout_s) and read_timeout_s > 0):
            raise ConfigError(f"read_timeout_s pozitif sonlu bir sayı olmalı, {read_timeout_s!r} geldi")
        if (isinstance(max_consecutive_timeouts, bool) or int(max_consecutive_timeouts) != max_consecutive_timeouts
                or int(max_consecutive_timeouts) < 1):
            raise ConfigError(
                f"max_consecutive_timeouts pozitif tamsayı olmalı, {max_consecutive_timeouts!r} geldi")
        if on_block is not None and not callable(on_block):
            raise ConfigError("on_block çağrılabilir (callable) olmalı")
        self._dev = device
        self._block_size = int(block_size)
        self._queue_blocks = int(queue_blocks)
        self._on_block = on_block
        self._read_timeout_s = float(read_timeout_s)
        self._max_timeouts = int(max_consecutive_timeouts)

        self._cond = threading.Condition(threading.Lock())   # guards queue + counters + state
        self._ctl = threading.Lock()                          # serializes start/stop
        self._queue: deque[IQBlock] = deque()
        self._stop_evt = threading.Event()
        self._thread: threading.Thread | None = None
        self._reader_alive = False
        self._started_device = False
        self._stop_from_reader = False
        self._error: BaseException | None = None
        self._host_drops = 0
        self._blocks_read = 0
        self._timeouts = 0
        self._consecutive_timeouts = 0

    # ------------------------------------------------------------------ properties
    @property
    def device(self) -> RxDevice:
        return self._dev

    @property
    def block_size(self) -> int:
        return self._block_size

    @property
    def queue_blocks(self) -> int:
        return self._queue_blocks

    @property
    def running(self) -> bool:
        """True while the reader thread is alive and no stop was requested."""
        with self._cond:
            return self._reader_alive and not self._stop_evt.is_set()

    @property
    def error(self) -> BaseException | None:
        """Exception that stopped the reader (``None`` if it is healthy or stopped cleanly)."""
        with self._cond:
            return self._error

    @property
    def host_drops(self) -> int:
        """Blocks discarded from the host queue (oldest first) since the device stream started."""
        with self._cond:
            return self._host_drops

    @property
    def blocks_read(self) -> int:
        """Blocks read from the device by this streamer since the device stream started."""
        with self._cond:
            return self._blocks_read

    @property
    def timeouts(self) -> int:
        """Read timeouts seen by the reader (total, not only consecutive)."""
        with self._cond:
            return self._timeouts

    @property
    def consecutive_timeouts(self) -> int:
        with self._cond:
            return self._consecutive_timeouts

    @property
    def queued(self) -> int:
        """Blocks currently waiting in the host queue."""
        with self._cond:
            return len(self._queue)

    @property
    def stats(self) -> StreamStats:
        """Copy of ``device.stats`` with ``host_drops`` filled in by this streamer."""
        with self._cond:
            st = dataclasses.replace(self._dev.stats)
            st.host_drops = self._host_drops
            return st

    # ------------------------------------------------------------------ lifecycle
    def start(self) -> None:
        """Start the device stream if needed and launch the reader thread (idempotent)."""
        with self._ctl:
            if self._thread is not None and self._thread.is_alive():
                if not self._stop_evt.is_set():
                    return
                raise StreamError(
                    f"{self._dev.name}: önceki okuyucu thread hâlâ sürücü okumasında; yeniden başlatılamıyor")
            self._stop_evt.clear()
            self._stop_from_reader = False
            with self._cond:
                self._queue.clear()
                self._error = None
                self._consecutive_timeouts = 0
            if not self._dev.streaming:
                self._dev.start_stream()
                with self._cond:            # device statistics restart with the stream
                    self._started_device = True
                    self._host_drops = 0
                    self._blocks_read = 0
                    self._timeouts = 0
            with self._cond:
                self._reader_alive = True
            t = threading.Thread(target=self._run, name=f"rx-stream-{self._dev.name}", daemon=True)
            self._thread = t
            try:
                t.start()
            except RuntimeError as e:
                with self._cond:
                    self._reader_alive = False
                self._stop_device_if_owned()
                raise StreamError(f"{self._dev.name}: okuyucu thread başlatılamadı: {e}") from e

    def stop(self, timeout_s: float | None = None) -> None:
        """Stop the reader (never hangs) and the device stream if this streamer started it.

        ``timeout_s`` bounds the wait for the in-flight read (default ``read_timeout_s + 1``).
        Queued blocks stay available to :meth:`get` / :meth:`drain`.
        """
        self._stop_evt.set()
        t = self._thread
        if t is not None and t is threading.current_thread():
            # Called from on_block: the loop exits once the callback returns; the reader
            # itself stops the device stream it owns (it cannot join itself).
            self._stop_from_reader = True
            return
        with self._ctl:
            t = self._thread
            wait = self._read_timeout_s + _JOIN_MARGIN_S if timeout_s is None else max(float(timeout_s), 0.0)
            if t is not None:
                t.join(wait)
                if t.is_alive() and self._owns_device():
                    # Stuck inside the driver: stopping the stream unblocks the read.
                    self._stop_device_if_owned()
                    t.join(_UNBLOCK_JOIN_S)
                if t.is_alive():
                    log.warning("%s: okuyucu thread %.1f s içinde durmadı; arka planda bırakıldı",
                                self._dev.name, wait)
            self._stop_device_if_owned()
            with self._cond:
                self._cond.notify_all()

    def _owns_device(self) -> bool:
        with self._cond:
            return self._started_device

    def _stop_device_if_owned(self) -> None:
        with self._cond:
            owned, self._started_device = self._started_device, False
        if owned:
            self._dev.stop_stream()

    def __enter__(self) -> RxStreamer:
        self.start()
        return self

    def __exit__(self, *exc) -> None:
        self.stop()

    # ------------------------------------------------------------------ consumer side
    def get(self, timeout_s: float | None = 1.0) -> IQBlock | None:
        """Oldest queued block, waiting up to ``timeout_s`` (``None`` = until one arrives
        or the reader stops). Returns ``None`` on timeout or when the reader has stopped and
        the queue is empty — check :attr:`error` / call :meth:`check` to tell them apart."""
        deadline = None if timeout_s is None else mono() + max(float(timeout_s), 0.0)
        with self._cond:
            while True:
                if self._queue:
                    return self._queue.popleft()
                if not self._reader_alive:
                    return None
                if deadline is None:
                    self._cond.wait()
                else:
                    remaining = deadline - mono()
                    if remaining <= 0.0:
                        return None
                    self._cond.wait(remaining)

    def drain(self) -> list[IQBlock]:
        """Remove and return every queued block (oldest first), without waiting."""
        with self._cond:
            out = list(self._queue)
            self._queue.clear()
            return out

    def check(self) -> None:
        """Raise :class:`StreamError` if the reader stopped because of an error."""
        err = self.error
        if err is not None:
            raise StreamError(f"{self._dev.name}: akış okuyucusu hata ile durdu: {err}") from err

    # ------------------------------------------------------------------ reader thread
    def _push(self, blk: IQBlock) -> None:
        with self._cond:
            if len(self._queue) >= self._queue_blocks:
                self._queue.popleft()
                self._host_drops += 1
            self._queue.append(blk)
            self._blocks_read += 1
            self._consecutive_timeouts = 0
            self._cond.notify()

    def _run(self) -> None:
        dev = self._dev
        try:
            while not self._stop_evt.is_set():
                try:
                    blk = dev.read(self._block_size, self._read_timeout_s)
                except StreamTimeout as e:
                    if self._stop_evt.is_set():
                        break
                    with self._cond:
                        self._timeouts += 1
                        self._consecutive_timeouts += 1
                        n = self._consecutive_timeouts
                    if n >= self._max_timeouts:
                        raise StreamError(
                            f"{dev.name}: art arda {n} okuma zaman aşımı ({self._read_timeout_s:.2f} s); "
                            "cihaz örnek göndermiyor (USB bağlantısı / sürücü kontrol edilmeli)") from e
                    log.warning("%s: okuma zaman aşımı (%d/%d art arda)", dev.name, n, self._max_timeouts)
                    continue
                except Exception:
                    if self._stop_evt.is_set():
                        break               # the stream was stopped under an in-flight read
                    raise
                self._push(blk)
                cb = self._on_block
                if cb is not None:
                    cb(blk)
        except Exception as e:  # stored + logged; consumers see running == False and .error
            log.error("%s: akış okuyucusu durdu: %s", dev.name, e)
            with self._cond:
                self._error = e
        finally:
            if self._stop_from_reader:
                try:
                    self._stop_device_if_owned()
                except Exception as e:
                    log.error("%s: cihaz akışı durdurulamadı: %s", dev.name, e)
                    with self._cond:
                        if self._error is None:
                            self._error = e
            with self._cond:
                self._reader_alive = False
                self._cond.notify_all()
