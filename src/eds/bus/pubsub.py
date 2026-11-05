"""ZeroMQ pub/sub transport.

Wire format: multipart ``[topic.encode("utf-8"), envelope JSON (utf-8)]`` (see
:mod:`eds.bus.envelope`). Two deployment modes (``[bus] mode`` in the config):

* ``direct`` — each publisher *binds* (``bus.pub_endpoint``), subscribers *connect*
  (``bus.sub_endpoint``).
* ``proxy`` — a broker (:func:`run_proxy` / :class:`ProxyThread`, XSUB<->XPUB) binds
  ``bus.proxy_frontend`` (publishers connect) and ``bus.proxy_backend`` (subscribers
  connect). Lets several producers (2× Pluto RX nodes, calib, measure, df) share one
  well-known subscriber endpoint.

Slow joiner: a PUB socket drops everything published before a subscriber's subscription
has arrived. For one-shot messages (``ctl.*``, ``meas.result``, ``cal.result``) open the
publisher with ``use_xpub=True`` and call :meth:`Publisher.wait_for_subscribers` first.

Context: ``context=None`` uses the process-wide ``zmq.Context.instance()``, so ``inproc://``
endpoints work between any publisher/subscriber/proxy of the process that use the default.
"""

from __future__ import annotations

import logging
import threading
import uuid
from collections import OrderedDict
from collections.abc import Iterator, Mapping, Sequence
from dataclasses import asdict, dataclass
from typing import Any

import zmq

from ..core.config import BusConfig
from ..core.errors import ConfigError, NotSupported
from ..core.timeutil import mono, now_ns
from .envelope import (
    BusError,
    MessageDecodeError,
    decode,
    dumps_compact,
    encode_spliced,
    make_envelope,
)

log = logging.getLogger(__name__)

_SUB_FRAME = b"\x01"
_UNSUB_FRAME = b"\x00"
_POLL_SLICE_S = 0.01             # max time the publisher lock is held while waiting
_DRAIN_LIMIT = 100_000           # safety bound for non-blocking drain loops


def _endpoints(endpoint: str | Sequence[str]) -> list[str]:
    eps = [endpoint] if isinstance(endpoint, str) else list(endpoint)
    if not eps or not all(isinstance(e, str) and e for e in eps):
        raise BusError(f"Geçersiz ZMQ uç noktası (endpoint): {endpoint!r}")
    return eps


def _attach(sock: zmq.Socket, eps: list[str], bind: bool) -> list[str]:
    """Bind/connect ``sock`` to every endpoint; returns the *actual* endpoints
    (wildcard ports such as ``tcp://127.0.0.1:*`` are resolved after bind)."""
    actual: list[str] = []
    for ep in eps:
        try:
            if bind:
                sock.bind(ep)
                actual.append(sock.getsockopt_string(zmq.LAST_ENDPOINT))
            else:
                sock.connect(ep)
                actual.append(ep)
        except zmq.ZMQError as e:
            verb = "bind edilemedi" if bind else "bağlanılamadı"
            raise BusError(f"ZMQ uç noktasına {verb}: {ep} ({e})") from e
    return actual


def _set_verbose(sock: zmq.Socket) -> None:
    """Pass every subscription (and, with libzmq >= 4.3, every unsubscription) of an XPUB
    socket up to the application, duplicates included -> per-subscriber counts."""
    opt = getattr(zmq, "XPUB_VERBOSER", None)
    if opt is not None:
        try:
            sock.setsockopt(opt, 1)
            return
        except zmq.ZMQError as e:
            log.info("XPUB_VERBOSER desteklenmiyor (%s); XPUB_VERBOSE kullanılıyor "
                     "(abonelikten çıkmalar sayılmaz)", e)
    sock.setsockopt(zmq.XPUB_VERBOSE, 1)


def _ms(timeout_s: float | None) -> int | None:
    return None if timeout_s is None else max(int(round(timeout_s * 1000.0)), 0)


# =========================================================================== publisher
class Publisher:
    """Publishes envelopes on a PUB (or XPUB) socket.

    * ``seq`` auto-increments per topic from 0 and only advances when a message was
      actually handed to ZMQ, so subscriber-side seq gaps mean HWM drops.
    * ``validate=True`` checks every message against its JSON schema before sending
      (needs ``jsonschema``; raises :class:`eds.bus.validate.MessageValidationError`, the
      message is then neither sent nor counted).
    * ``t_ns`` is taken *after* the payload has been sanitized, validated and encoded,
      immediately before the send, so ``t_recv_ns - t_ns`` measures transport latency.
    * Thread safety: one internal lock serialises every socket operation, so a single
      Publisher may be shared by several threads (status + spectrum + ack threads).
    """

    def __init__(
        self,
        endpoint: str | Sequence[str],
        bind: bool = True,
        src: str = "rx0",
        validate: bool = False,
        hwm: int = 200,
        context: zmq.Context | None = None,
        linger_ms: int = 0,
        use_xpub: bool = False,
    ):
        if not isinstance(src, str) or not src:
            raise BusError("Yayıncı 'src' kimliği boş olmayan metin olmalı")
        if int(hwm) < 0:
            raise BusError(f"hwm >= 0 olmalı (gelen: {hwm})")
        eps = _endpoints(endpoint)
        self.src = src
        self.bind = bool(bind)
        self.use_xpub = bool(use_xpub)
        self.hwm = int(hwm)
        self.linger_ms = int(linger_ms)
        self._validate = None
        if validate:
            from . import validate as _v

            _v.require_backend()
            self._validate = _v.validate_message
        self._ctx = context if context is not None else zmq.Context.instance()
        self._lock = threading.Lock()
        self._seq: dict[str, int] = {}
        self._subs: dict[bytes, int] = {}
        self._closed = False
        self.sent = 0
        self.bytes_sent = 0
        sock = self._ctx.socket(zmq.XPUB if self.use_xpub else zmq.PUB)
        try:
            sock.setsockopt(zmq.SNDHWM, self.hwm)
            sock.setsockopt(zmq.LINGER, self.linger_ms)
            if self.use_xpub:
                _set_verbose(sock)
            self.endpoints = _attach(sock, eps, self.bind)
        except BaseException:
            sock.close(linger=0)
            raise
        self._sock = sock

    # ------------------------------------------------------------------ properties
    @property
    def endpoint(self) -> str:
        """First actual endpoint (after wildcard-port resolution)."""
        return self.endpoints[0]

    @property
    def closed(self) -> bool:
        return self._closed

    @property
    def validating(self) -> bool:
        return self._validate is not None

    def next_seq(self, topic: str) -> int:
        with self._lock:
            return self._seq.get(topic, 0)

    # ------------------------------------------------------------------ publish
    def publish(self, topic: str, data: Mapping[str, Any], t_ns: int | None = None) -> dict[str, Any]:
        """Send ``data`` on ``topic``; returns the envelope exactly as sent.

        ``t_ns`` overrides the send timestamp (replay / tests); by default it is
        ``time.time_ns()`` right before the send.
        """
        with self._lock:
            self._check_open()
            seq = self._seq.get(topic, 0)
            env = make_envelope(topic, self.src, seq, data, t_ns=t_ns if t_ns is not None else now_ns())
            if self._validate is not None:
                self._validate(topic, env)
            data_json = dumps_compact(env["data"])
            if t_ns is None:
                env["t_ns"] = now_ns()
            payload = encode_spliced(topic, self.src, seq, env["t_ns"], data_json)
            try:
                self._sock.send_multipart([topic.encode("utf-8"), payload])
            except zmq.ZMQError as e:
                raise BusError(f"'{topic}' mesajı gönderilemedi: {e}") from e
            self._seq[topic] = seq + 1
            self.sent += 1
            self.bytes_sent += len(payload)
            if self.use_xpub:
                self._drain_subscriptions()
        return env

    # ------------------------------------------------------------------ subscriptions (XPUB)
    def _drain_subscriptions(self) -> None:
        for _ in range(_DRAIN_LIMIT):
            try:
                frame = self._sock.recv(zmq.NOBLOCK)
            except zmq.Again:
                return
            kind, prefix = frame[:1], frame[1:]
            if kind == _SUB_FRAME:
                self._subs[prefix] = self._subs.get(prefix, 0) + 1
            elif kind == _UNSUB_FRAME:
                left = self._subs.get(prefix, 0) - 1
                if left > 0:
                    self._subs[prefix] = left
                else:
                    self._subs.pop(prefix, None)
            else:
                log.warning("XPUB soketine abonelik olmayan çerçeve geldi (%d bayt), yok sayıldı",
                            len(frame))

    def _count(self, topic: str | None) -> int:
        if topic is None:
            return sum(self._subs.values())
        t = topic.encode("utf-8")
        return sum(c for p, c in self._subs.items() if t.startswith(p))

    def subscriptions(self) -> dict[str, int]:
        """Active subscription prefixes seen so far -> count (XPUB mode only)."""
        if not self.use_xpub:
            raise NotSupported("Abonelik bilgisi yalnızca use_xpub=True ile açılan yayıncıda vardır")
        with self._lock:
            self._check_open()
            self._drain_subscriptions()
            return {p.decode("utf-8", errors="replace"): c for p, c in self._subs.items()}

    def wait_for_subscribers(self, n: int = 1, timeout_s: float = 2.0, topic: str | None = None) -> bool:
        """Block until ``n`` subscriptions are active (XPUB mode); False on timeout.

        With ``topic`` only subscriptions whose prefix matches ``topic`` count, i.e. ``n``
        subscribers that will receive it. Without ``topic`` every subscription counts (a
        subscriber subscribed to k topics counts k times). Through a proxy, subscriptions
        are merged per prefix by the XSUB side, so only ``n=1`` is reliable there.
        """
        if not self.use_xpub:
            raise NotSupported(
                "wait_for_subscribers yalnızca use_xpub=True ile açılan yayıncıda çalışır "
                "(PUB soketi abonelikleri göremez)"
            )
        deadline = mono() + max(float(timeout_s), 0.0)
        while True:
            with self._lock:
                self._check_open()
                self._drain_subscriptions()
                if self._count(topic) >= n:
                    return True
                remaining = deadline - mono()
                if remaining <= 0:
                    return False
                self._sock.poll(_ms(min(remaining, _POLL_SLICE_S)), zmq.POLLIN)

    # ------------------------------------------------------------------ lifecycle
    def _check_open(self) -> None:
        if self._closed:
            raise BusError("Yayıncı kapatılmış; yeni mesaj gönderilemez")

    def close(self) -> None:
        with self._lock:
            if self._closed:
                return
            self._closed = True
            self._sock.close(linger=self.linger_ms)

    def __enter__(self) -> Publisher:
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    def __repr__(self) -> str:
        kind = "XPUB" if self.use_xpub else "PUB"
        return f"Publisher({kind} {'bind' if self.bind else 'connect'} {self.endpoints}, src={self.src!r})"


# =========================================================================== subscriber
@dataclass(frozen=True, slots=True)
class Message:
    """One received message. ``t_recv_ns`` is ``time.time_ns()`` when the frames were read."""

    topic: str
    env: dict[str, Any]
    t_recv_ns: int

    @property
    def data(self) -> dict[str, Any]:
        return self.env["data"]

    @property
    def src(self) -> str:
        return self.env["src"]

    @property
    def seq(self) -> int:
        return self.env["seq"]

    @property
    def t_ns(self) -> int:
        return self.env["t_ns"]

    @property
    def latency_s(self) -> float:
        """Transport latency ``t_recv_ns - t_ns`` [s] (same-host wall clocks)."""
        return (self.t_recv_ns - self.env["t_ns"]) / 1e9


@dataclass
class SubscriberStats:
    received: int = 0            # messages returned (or conflated away)
    foreign_dropped: int = 0     # prefix-matched frames dropped by the exact-topic filter
    decode_errors: int = 0       # malformed frames / envelopes (logged)
    seq_gaps: int = 0            # messages missing according to per-(src, topic) seq
    seq_resets: int = 0          # seq went backwards (publisher restarted)
    conflated: int = 0           # messages superseded by a newer one (conflate=True)

    def to_dict(self) -> dict[str, int]:
        return asdict(self)


class Subscriber:
    """SUB socket subscribed to ``topics``.

    * ``exact=True`` (default): ZMQ filters by *prefix* (``"rx.s"`` would match
      ``"rx.status"`` and ``"rx.spectrum"``); frames whose topic is not exactly one of the
      subscribed topics are dropped and counted in ``stats.foreign_dropped``.
    * Malformed frames are logged and counted (``stats.decode_errors``) and skipped;
      ``strict=True`` raises :class:`MessageDecodeError` instead.
    * ``conflate=True`` keeps only the newest message per ``(src, topic)``: every
      :meth:`recv` first drains the socket and then returns the pending entry whose newest
      message is the oldest. (ZMQ_CONFLATE is not used: it breaks multipart messages.)
    * ``endpoint`` may be a list (e.g. one SUB connected to two RX nodes in direct mode).
    * Not thread-safe (ZMQ socket rule): use a Subscriber from one thread at a time.
    """

    def __init__(
        self,
        endpoint: str | Sequence[str],
        topics: str | Sequence[str],
        bind: bool = False,
        exact: bool = True,
        context: zmq.Context | None = None,
        rcvhwm: int = 1000,
        conflate: bool = False,
        strict: bool = False,
    ):
        topic_list = [topics] if isinstance(topics, str) else list(topics)
        if not topic_list:
            raise BusError("En az bir topic'e abone olunmalı")
        for t in topic_list:
            if not isinstance(t, str):
                raise BusError(f"Topic metin olmalı: {t!r}")
            if exact and t == "":
                raise BusError("exact=True iken boş topic (tüm mesajlar) kullanılamaz; exact=False verin")
        if int(rcvhwm) < 0:
            raise BusError(f"rcvhwm >= 0 olmalı (gelen: {rcvhwm})")
        eps = _endpoints(endpoint)
        self.exact = bool(exact)
        self.conflate = bool(conflate)
        self.strict = bool(strict)
        self.bind = bool(bind)
        self.rcvhwm = int(rcvhwm)
        self.stats = SubscriberStats()
        self._topics: set[str] = set()
        self._last_seq: dict[tuple[str, str], int] = {}
        self._latest: OrderedDict[tuple[str, str], Message] = OrderedDict()
        self._closed = False
        self._ctx = context if context is not None else zmq.Context.instance()
        sock = self._ctx.socket(zmq.SUB)
        try:
            sock.setsockopt(zmq.RCVHWM, self.rcvhwm)
            sock.setsockopt(zmq.LINGER, 0)
            for t in topic_list:
                sock.setsockopt(zmq.SUBSCRIBE, t.encode("utf-8"))
                self._topics.add(t)
            self.endpoints = _attach(sock, eps, self.bind)
        except BaseException:
            sock.close(linger=0)
            raise
        self._sock = sock

    @property
    def topics(self) -> frozenset[str]:
        return frozenset(self._topics)

    @property
    def endpoint(self) -> str:
        return self.endpoints[0]

    @property
    def closed(self) -> bool:
        return self._closed

    def subscribe(self, topic: str) -> None:
        self._check_open()
        if self.exact and topic == "":
            raise BusError("exact=True iken boş topic (tüm mesajlar) kullanılamaz")
        if topic not in self._topics:
            self._sock.setsockopt(zmq.SUBSCRIBE, topic.encode("utf-8"))
            self._topics.add(topic)

    def unsubscribe(self, topic: str) -> None:
        self._check_open()
        if topic in self._topics:
            self._sock.setsockopt(zmq.UNSUBSCRIBE, topic.encode("utf-8"))
            self._topics.discard(topic)

    # ------------------------------------------------------------------ receive
    def _bad(self, reason: str, topic: str | None) -> None:
        self.stats.decode_errors += 1
        n = self.stats.decode_errors
        if n <= 5 or n % 1000 == 0:
            log.warning("Bozuk bus mesajı atlandı (topic=%r, toplam %d): %s", topic, n, reason)
        if self.strict:
            raise MessageDecodeError(f"Bozuk bus mesajı (topic={topic!r}): {reason}")

    def _parse(self, frames: list[bytes], t_recv_ns: int) -> Message | None:
        if len(frames) != 2:
            self._bad(f"{len(frames)} çerçeve geldi (beklenen 2: topic, json)", None)
            return None
        try:
            topic = frames[0].decode("utf-8")
        except UnicodeDecodeError as e:
            self._bad(f"topic çerçevesi UTF-8 değil ({e})", None)
            return None
        if self.exact and topic not in self._topics:
            self.stats.foreign_dropped += 1
            return None
        try:
            env = decode(frames[1])
        except MessageDecodeError as e:
            self._bad(str(e), topic)
            return None
        if env["topic"] != topic:
            self._bad(f"zarf topic'i ({env['topic']!r}) çerçeve topic'iyle uyuşmuyor", topic)
            return None
        key = (env["src"], topic)
        last = self._last_seq.get(key)
        seq = env["seq"]
        if last is not None:
            if seq > last + 1:
                self.stats.seq_gaps += seq - last - 1
            elif seq <= last:
                self.stats.seq_resets += 1
        self._last_seq[key] = seq
        self.stats.received += 1
        return Message(topic=topic, env=env, t_recv_ns=t_recv_ns)

    def _read_available(self) -> Message | None | bool:
        """Non-blocking read of one frame set. Returns a Message, None (frame set was
        dropped) or False (socket queue empty)."""
        try:
            frames = self._sock.recv_multipart(zmq.NOBLOCK)
        except zmq.Again:
            return False
        return self._parse(frames, now_ns())

    def _recv_plain(self, timeout_s: float | None) -> Message | None:
        deadline = None if timeout_s is None else mono() + max(float(timeout_s), 0.0)
        overrun = 0
        while True:
            got = self._read_available()
            if isinstance(got, Message):
                return got
            remaining = None if deadline is None else deadline - mono()
            expired = remaining is not None and remaining <= 0
            if got is None:
                # dropped frame set: a valid message may already be queued behind it, so keep
                # reading — but a never-ending foreign flood must not block past the timeout
                if expired:
                    overrun += 1
                    if overrun >= _DRAIN_LIMIT:
                        return None
                continue
            if expired:
                return None
            self._sock.poll(_ms(remaining), zmq.POLLIN)

    def _store_latest(self, msg: Message) -> None:
        key = (msg.src, msg.topic)
        if key in self._latest:
            del self._latest[key]
            self.stats.conflated += 1
        self._latest[key] = msg

    def _drain_latest(self) -> None:
        for _ in range(_DRAIN_LIMIT):
            got = self._read_available()
            if got is False:
                return
            if isinstance(got, Message):
                self._store_latest(got)

    def recv(self, timeout_s: float | None = None) -> Message | None:
        """Next message, or None after ``timeout_s`` (None = wait forever, 0 = poll)."""
        self._check_open()
        if not self.conflate:
            return self._recv_plain(timeout_s)
        self._drain_latest()
        if not self._latest:
            msg = self._recv_plain(timeout_s)
            if msg is None:
                return None
            self._store_latest(msg)
            self._drain_latest()
        return self._latest.popitem(last=False)[1]

    def iter_messages(self, duration_s: float | None = None) -> Iterator[Message]:
        """Yield messages for ``duration_s`` seconds (None = until the caller stops)."""
        deadline = None if duration_s is None else mono() + max(float(duration_s), 0.0)
        while True:
            remaining = None if deadline is None else deadline - mono()
            if remaining is not None and remaining <= 0:
                return
            msg = self.recv(remaining)
            if msg is None:
                return
            yield msg

    # ------------------------------------------------------------------ lifecycle
    def _check_open(self) -> None:
        if self._closed:
            raise BusError("Abone kapatılmış")

    def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        self._sock.close(linger=0)

    def __enter__(self) -> Subscriber:
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    def __repr__(self) -> str:
        return (f"Subscriber({'bind' if self.bind else 'connect'} {self.endpoints}, "
                f"topics={sorted(self._topics)}, exact={self.exact})")


# =========================================================================== proxy
def _proxy_sockets(ctx: zmq.Context, frontend: str, backend: str, hwm: int) -> tuple[zmq.Socket, zmq.Socket]:
    xsub = ctx.socket(zmq.XSUB)
    xpub = ctx.socket(zmq.XPUB)
    try:
        for s in (xsub, xpub):
            s.setsockopt(zmq.LINGER, 0)
            s.setsockopt(zmq.SNDHWM, hwm)
            s.setsockopt(zmq.RCVHWM, hwm)
        # forward every subscription upstream so XPUB publishers can see new subscribers
        xpub.setsockopt(zmq.XPUB_VERBOSE, 1)
        _attach(xsub, [frontend], True)
        _attach(xpub, [backend], True)
    except BaseException:
        xsub.close(linger=0)
        xpub.close(linger=0)
        raise
    return xsub, xpub


def run_proxy(frontend: str, backend: str, context: zmq.Context | None = None, hwm: int = 1000) -> None:
    """Blocking XSUB(``frontend``, publishers connect) <-> XPUB(``backend``, subscribers
    connect) broker. Returns when the context is terminated; Ctrl-C propagates."""
    ctx = context if context is not None else zmq.Context.instance()
    xsub, xpub = _proxy_sockets(ctx, frontend, backend, hwm)
    log.info("bus proxy: frontend %s <-> backend %s", frontend, backend)
    try:
        zmq.proxy(xsub, xpub)
    except zmq.ContextTerminated:
        log.info("bus proxy: context sonlandırıldı, proxy durdu")
    finally:
        xsub.close(linger=0)
        xpub.close(linger=0)


class ProxyThread:
    """XSUB/XPUB proxy in a background thread that can really be stopped.

    Uses ``zmq.proxy_steerable`` with an inproc PAIR control socket; :meth:`stop` sends
    ``b"TERMINATE"`` and joins. Sockets are created and bound inside the thread;
    :meth:`start` waits for that and re-raises bind errors. ``frontend_endpoint`` /
    ``backend_endpoint`` hold the actual endpoints (wildcard ports resolved).
    """

    def __init__(self, frontend: str, backend: str, context: zmq.Context | None = None,
                 hwm: int = 1000, name: str = "eds-bus-proxy"):
        self.frontend = frontend
        self.backend = backend
        self.hwm = int(hwm)
        self.name = name
        self.frontend_endpoint = ""
        self.backend_endpoint = ""
        self._ctx = context if context is not None else zmq.Context.instance()
        self._ctl_ep = f"inproc://eds-proxy-ctl-{uuid.uuid4().hex}"
        self._thread: threading.Thread | None = None
        self._ready = threading.Event()
        self._error: BaseException | None = None
        self._ctl: zmq.Socket | None = None

    @property
    def running(self) -> bool:
        return self._thread is not None and self._thread.is_alive()

    def _run(self) -> None:
        ctl = None
        xsub = xpub = None
        try:
            xsub, xpub = _proxy_sockets(self._ctx, self.frontend, self.backend, self.hwm)
            ctl = self._ctx.socket(zmq.PAIR)
            ctl.setsockopt(zmq.LINGER, 0)
            ctl.bind(self._ctl_ep)
            self.frontend_endpoint = xsub.getsockopt_string(zmq.LAST_ENDPOINT)
            self.backend_endpoint = xpub.getsockopt_string(zmq.LAST_ENDPOINT)
        except BaseException as e:  # reported to start()
            self._error = e
            for s in (xsub, xpub, ctl):
                if s is not None:
                    s.close(linger=0)
            self._ready.set()
            return
        self._ready.set()
        try:
            zmq.proxy_steerable(xsub, xpub, None, ctl)
        except zmq.ContextTerminated:
            log.info("bus proxy (%s): context sonlandırıldı", self.name)
        except BaseException as e:  # keep for stop(); never lose it silently
            self._error = e
            log.exception("bus proxy (%s) beklenmedik hata ile durdu", self.name)
        finally:
            xsub.close(linger=0)
            xpub.close(linger=0)
            ctl.close(linger=0)

    def start(self, timeout_s: float = 5.0) -> ProxyThread:
        if self._thread is not None:
            raise BusError("Proxy zaten başlatılmış")
        self._error = None
        self._ready.clear()
        self._thread = threading.Thread(target=self._run, name=self.name, daemon=True)
        self._thread.start()
        if not self._ready.wait(timeout_s):
            raise BusError(f"Proxy {timeout_s} s içinde hazır olmadı")
        if self._error is not None:
            err, self._error = self._error, None
            self._thread.join(timeout_s)
            self._thread = None
            raise BusError(f"Proxy başlatılamadı: {err}") from err
        ctl = self._ctx.socket(zmq.PAIR)
        ctl.setsockopt(zmq.LINGER, 0)
        ctl.connect(self._ctl_ep)
        self._ctl = ctl
        log.info("bus proxy (%s): %s <-> %s", self.name, self.frontend_endpoint, self.backend_endpoint)
        return self

    def stop(self, timeout_s: float = 1.0) -> None:
        """Terminate the proxy and join its thread (raises BusError if it does not stop)."""
        th = self._thread
        if th is None:
            return
        try:
            if th.is_alive() and self._ctl is not None:
                self._ctl.send(b"TERMINATE")
            th.join(timeout_s)
            if th.is_alive():
                raise BusError(f"Proxy iş parçacığı {timeout_s} s içinde durmadı")
        finally:
            if not th.is_alive():
                self._thread = None
                if self._ctl is not None:
                    self._ctl.close(linger=0)
                    self._ctl = None
        if self._error is not None:
            err, self._error = self._error, None
            raise BusError(f"Proxy hata ile sonlanmıştı: {err}") from err

    def __enter__(self) -> ProxyThread:
        return self.start()

    def __exit__(self, *exc: object) -> None:
        self.stop()


# =========================================================================== config helpers
def publisher_from_config(bus: BusConfig, src: str, validate: bool | None = None, *,
                          context: zmq.Context | None = None, use_xpub: bool = False) -> Publisher:
    """Publisher for ``[bus]``: direct -> bind ``pub_endpoint``; proxy -> connect
    ``proxy_frontend``. ``validate=None`` takes ``bus.validate``."""
    v = bus.validate if validate is None else bool(validate)
    if bus.mode == "direct":
        return Publisher(bus.pub_endpoint, bind=True, src=src, validate=v, hwm=bus.hwm,
                         context=context, use_xpub=use_xpub)
    if bus.mode == "proxy":
        return Publisher(bus.proxy_frontend, bind=False, src=src, validate=v, hwm=bus.hwm,
                         context=context, use_xpub=use_xpub)
    raise ConfigError(f"bus.mode = {bus.mode!r} geçersiz; seçenekler: ['direct', 'proxy']")


def subscriber_from_config(bus: BusConfig, topics: str | Sequence[str], *,
                           context: zmq.Context | None = None, exact: bool = True,
                           conflate: bool = False, rcvhwm: int = 1000) -> Subscriber:
    """Subscriber for ``[bus]``: direct -> connect ``sub_endpoint``; proxy -> connect
    ``proxy_backend``."""
    if bus.mode == "direct":
        ep = bus.sub_endpoint
    elif bus.mode == "proxy":
        ep = bus.proxy_backend
    else:
        raise ConfigError(f"bus.mode = {bus.mode!r} geçersiz; seçenekler: ['direct', 'proxy']")
    return Subscriber(ep, topics, bind=False, exact=exact, context=context, rcvhwm=rcvhwm,
                      conflate=conflate)
