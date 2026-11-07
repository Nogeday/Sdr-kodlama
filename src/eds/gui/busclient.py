"""Background ZeroMQ client for the GUI (no Qt here).

The GUI never touches a socket from its own thread. One receiver thread owns two
:class:`~eds.bus.pubsub.Subscriber` sockets:

* ``rx.spectrum`` with ``conflate=True`` and a small receive HWM: the socket is drained and
  only the **newest** frame is kept, so a slow GUI can never build a backlog (ZMQ_CONFLATE
  itself cannot be used: it breaks multipart ``[topic, json]`` messages). The publisher side
  never blocks either: a ZMQ PUB socket drops for a subscriber that is past its HWM.
* every other topic (detections, status, DF, measurement results, …) without conflation,
  into a bounded queue — these are small and each one matters.

The GUI thread calls :meth:`BusClient.take` from a timer and gets what arrived since the
previous call.
"""

from __future__ import annotations

import logging
import threading
from collections import deque
from dataclasses import dataclass, field

from ..bus.pubsub import Message, Subscriber
from ..bus.topics import (
    CAL_RESULT,
    CTL_ACK,
    DF_BEARING,
    MEAS_RESULT,
    RX_DETECTIONS,
    RX_SPECTRUM,
    RX_STATUS,
    RX_SWEEP,
)
from ..core.timeutil import now_ns

log = logging.getLogger(__name__)

EVENT_TOPICS: tuple[str, ...] = (RX_DETECTIONS, RX_STATUS, DF_BEARING, MEAS_RESULT, RX_SWEEP, CAL_RESULT, CTL_ACK)


@dataclass
class Snapshot:
    spectrum: Message | None = None                 # newest rx.spectrum since the last take (or None)
    events: list[Message] = field(default_factory=list)
    spectra_dropped: int = 0                         # frames superseded before the GUI drew them


class BusClient:
    """Receives bus messages in a daemon thread; :meth:`take` hands them to the GUI thread."""

    def __init__(self, endpoint: str, *, spectrum_hwm: int = 4, max_events: int = 5000):
        self.endpoint = endpoint
        self._spectrum_hwm = int(spectrum_hwm)
        self._lock = threading.Lock()
        self._latest: Message | None = None
        self._superseded = 0
        self._events: deque[Message] = deque(maxlen=int(max_events))
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self.error: str = ""
        self.received: dict[str, int] = {}
        self.last_rx_ns: dict[str, int] = {}

    # ------------------------------------------------------------------ lifecycle
    def start(self) -> BusClient:
        if self._thread is None:
            self._thread = threading.Thread(target=self._run, name="eds-gui-bus", daemon=True)
            self._thread.start()
        return self

    def stop(self, timeout_s: float = 2.0) -> None:
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout_s)
            self._thread = None

    @property
    def running(self) -> bool:
        return self._thread is not None and self._thread.is_alive()

    # ------------------------------------------------------------------ GUI side
    def take(self) -> Snapshot:
        with self._lock:
            snap = Snapshot(spectrum=self._latest, events=list(self._events), spectra_dropped=self._superseded)
            self._latest = None
            self._superseded = 0
            self._events.clear()
        return snap

    def seconds_since(self, topic: str) -> float:
        t = self.last_rx_ns.get(topic)
        return float("inf") if t is None else (now_ns() - t) / 1e9

    # ------------------------------------------------------------------ receiver thread
    def _note(self, m: Message) -> None:
        self.received[m.topic] = self.received.get(m.topic, 0) + 1
        self.last_rx_ns[m.topic] = m.t_recv_ns

    def _run(self) -> None:
        spec: Subscriber | None = None
        ev: Subscriber | None = None
        try:
            spec = Subscriber(self.endpoint, [RX_SPECTRUM], conflate=True, rcvhwm=self._spectrum_hwm)
            ev = Subscriber(self.endpoint, list(EVENT_TOPICS), rcvhwm=10_000)
            while not self._stop.is_set():
                got = False
                m = spec.recv(0)                       # drains the socket, newest frame only
                if m is not None:
                    got = True
                    self._note(m)
                    with self._lock:
                        if self._latest is not None:
                            self._superseded += 1
                        self._latest = m
                for _ in range(200):                   # bounded batch keeps the spectrum fresh
                    e = ev.recv(0 if got else 0.01)
                    if e is None:
                        break
                    got = True
                    self._note(e)
                    with self._lock:
                        self._events.append(e)
        except Exception as e:  # noqa: BLE001 - shown in the GUI status bar, never crash the GUI
            self.error = f"{type(e).__name__}: {e}"
            log.exception("GUI bus istemcisi durdu")
        finally:
            for s in (spec, ev):
                if s is not None:
                    try:
                        s.close()
                    except Exception:  # noqa: BLE001
                        pass
