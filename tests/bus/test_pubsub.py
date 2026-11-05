"""Envelope encoding and ZeroMQ transport: round trips (inproc/tcp, direct/proxy), exact
topic filtering, seq accounting, sanitization, validation on publish, proxy lifecycle."""

from __future__ import annotations

import json
import socket
import threading
import time
import uuid

import numpy as np
import pytest
import zmq

from eds.bus import topics as T
from eds.bus.envelope import (
    ENVELOPE_VERSION,
    BusError,
    MessageDecodeError,
    MessageEncodeError,
    decode,
    encode,
    make_envelope,
    sanitize,
)
from eds.bus.pubsub import (
    Message,
    ProxyThread,
    Publisher,
    Subscriber,
    publisher_from_config,
    run_proxy,
    subscriber_from_config,
)
from eds.core.config import BusConfig
from eds.core.errors import ConfigError, NotSupported
from tests.bus import examples as E

TIMEOUT = 2.0


def _inproc() -> str:
    return f"inproc://eds-test-{uuid.uuid4().hex}"


def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return int(s.getsockname()[1])


@pytest.fixture
def closer():
    items: list = []
    yield items.append
    for it in reversed(items):
        if isinstance(it, ProxyThread):
            it.stop()
        else:
            it.close()


def _link(pub: Publisher, topic: str, n: int = 1) -> None:
    assert pub.wait_for_subscribers(n, TIMEOUT, topic=topic), "abonelik zamanında gelmedi"


def _probe_link(pub: Publisher, sub: Subscriber, topic: str) -> None:
    """Slow-joiner sync for plain PUB sockets: publish probes until one arrives, drain."""
    deadline = time.monotonic() + TIMEOUT
    while time.monotonic() < deadline:
        pub.publish(topic, {"probe": True})
        if sub.recv(0.02) is not None:
            while sub.recv(0.05) is not None:
                pass
            return
    raise AssertionError("PUB/SUB bağlantısı kurulamadı")


# =========================================================================== envelope
def test_sanitize_non_finite_and_numpy():
    data = {
        "nan": float("nan"), "inf": float("inf"), "ninf": -np.inf,
        "f32": np.float32(1.5), "i64": np.int64(2**62), "u8": np.uint8(7), "b": np.bool_(True),
        "s": np.str_("Türkçe"), "arr": np.array([1.0, np.nan, np.inf, -2.5], dtype=np.float32),
        "iarr": np.arange(3, dtype=np.int16), "barr": np.array([True, False]),
        "m2": np.array([[1.0, np.nan], [3.0, 4.0]]), "tup": (1, (2.0, float("nan"))),
        "nested": [{"x": np.float64(np.nan)}], "set": {3, 1, 2}, np.int64(5): "int-key",
        1.5: "float-key", True: "bool-key", None: "none-key", "empty": np.array([]),
    }
    out = sanitize(data)
    assert out["nan"] is None and out["inf"] is None and out["ninf"] is None
    assert out["f32"] == 1.5 and type(out["f32"]) is float
    assert out["i64"] == 2**62 and type(out["i64"]) is int
    assert out["u8"] == 7 and type(out["u8"]) is int
    assert out["b"] is True and out["s"] == "Türkçe" and type(out["s"]) is str
    assert out["arr"] == [1.0, None, None, -2.5]
    assert out["iarr"] == [0, 1, 2] and all(type(v) is int for v in out["iarr"])
    assert out["barr"] == [True, False]
    assert out["m2"] == [[1.0, None], [3.0, 4.0]]
    assert out["tup"] == [1, [2.0, None]]
    assert out["nested"] == [{"x": None}]
    assert out["set"] == [1, 2, 3]
    assert out["5"] == "int-key" and out["1.5"] == "float-key"
    assert out["true"] == "bool-key" and out["null"] == "none-key"
    assert out["empty"] == []
    json.dumps(out, allow_nan=False)            # strictly valid JSON
    assert data["nan"] != data["nan"]           # input untouched


@pytest.mark.parametrize("bad", [1 + 2j, np.complex64(1j), np.array([1j]), b"raw", object(),
                                 {float("nan"): 1}, {(1, 2): 3}])
def test_sanitize_rejects_unrepresentable(bad):
    with pytest.raises(MessageEncodeError):
        sanitize({"x": bad})


def test_make_envelope_fields():
    t0 = time.time_ns()
    env = make_envelope(T.RX_STATUS, "rx0", 4, {"v": np.float64(1.0)})
    assert list(env) == ["topic", "v", "src", "seq", "t_ns", "data"]
    assert env["topic"] == T.RX_STATUS and env["v"] == ENVELOPE_VERSION == 1
    assert env["src"] == "rx0" and env["seq"] == 4 and env["data"] == {"v": 1.0}
    assert t0 <= env["t_ns"] <= time.time_ns()
    assert make_envelope(T.RX_STATUS, "rx0", np.int64(0), {}, t_ns=np.int64(7))["t_ns"] == 7


@pytest.mark.parametrize("kw", [
    {"topic": ""}, {"topic": 5}, {"src": ""}, {"seq": -1}, {"seq": True}, {"seq": 1.0},
    {"data": [1, 2]}, {"data": None}, {"t_ns": -1}, {"t_ns": 1.5},
])
def test_make_envelope_rejects(kw):
    args = {"topic": T.RX_STATUS, "src": "rx0", "seq": 0, "data": {}, "t_ns": None}
    args.update(kw)
    with pytest.raises(MessageEncodeError):
        make_envelope(**args)


def test_encode_is_compact_strict_utf8():
    env = make_envelope(T.CTL_ACK, "rx0", 1, {"req_id": "a", "ok": False, "error": "Kazanç çok yüksek",
                                             "x": float("nan")}, t_ns=123)
    raw = encode(env)
    text = raw.decode("utf-8")
    assert text.startswith('{"topic":"ctl.ack","v":1,"src":"rx0","seq":1,"t_ns":123,"data":{')
    assert ", " not in text and ": " not in text
    assert "Kazanç çok yüksek" in text                 # no \u escapes
    assert "NaN" not in text and '"x":null' in text
    assert decode(raw)["data"]["x"] is None


def test_encode_rejects_non_envelope():
    with pytest.raises(MessageEncodeError):
        encode({"topic": "x"})
    with pytest.raises(MessageEncodeError):
        encode([1, 2])                                  # type: ignore[arg-type]


@pytest.mark.parametrize("payload", [
    b"{not json", b"[1, 2]", b'"str"', b"\xff\xfe\x00",
    b'{"topic":"rx.status","v":1,"src":"rx0","seq":0,"t_ns":1,"data":{"x":NaN}}',
    b'{"topic":"rx.status","v":1,"src":"rx0","seq":0,"t_ns":1,"data":{"x":Infinity}}',
    b'{"topic":"rx.status","v":2,"src":"rx0","seq":0,"t_ns":1,"data":{}}',
    b'{"topic":"rx.status","v":true,"src":"rx0","seq":0,"t_ns":1,"data":{}}',
    b'{"topic":"rx.status","v":1,"src":"rx0","seq":0.5,"t_ns":1,"data":{}}',
    b'{"topic":"rx.status","v":1,"src":"rx0","seq":0,"t_ns":"1","data":{}}',
    b'{"topic":"rx.status","v":1,"src":"","seq":0,"t_ns":1,"data":{}}',
    b'{"topic":"rx.status","v":1,"src":"rx0","seq":0,"t_ns":1,"data":[]}',
    b'{"topic":"rx.status","v":1,"src":"rx0","seq":0,"t_ns":1}',
    b'{"topic":"rx.status","v":1,"src":"rx0","seq":0,"t_ns":1,"data":{},"x":1}',
])
def test_decode_rejects_malformed(payload):
    with pytest.raises(MessageDecodeError):
        decode(payload)


def test_big_int_preserved_by_codec():
    big = 2**63 + 987_654_321
    env = make_envelope(T.RX_SPECTRUM, "rx0", 0, {"t_acq_ns": big}, t_ns=big + 1)
    back = decode(encode(env))
    assert back["t_ns"] == big + 1 and back["data"]["t_acq_ns"] == big
    assert decode(memoryview(encode(env)))["t_ns"] == big + 1


# =========================================================================== round trips
def test_inproc_round_trip_plain_pub(closer):
    ep = _inproc()
    pub = Publisher(ep, src="rx0")
    closer(pub)
    sub = Subscriber(ep, [T.RX_STATUS])
    closer(sub)
    _probe_link(pub, sub, T.RX_STATUS)
    sent = pub.publish(T.RX_STATUS, E.example(T.RX_STATUS))
    msg = sub.recv(TIMEOUT)
    assert isinstance(msg, Message)
    assert msg.topic == T.RX_STATUS and msg.env == sent
    assert msg.data == E.example(T.RX_STATUS)
    assert msg.src == "rx0" and msg.seq == sent["seq"] and msg.t_ns == sent["t_ns"]
    assert msg.t_recv_ns >= msg.t_ns and 0 <= msg.latency_s < 1.0


def test_tcp_round_trip_all_topics_xpub(closer):
    pub = Publisher("tcp://127.0.0.1:*", src="rx0", use_xpub=True, hwm=1000)
    closer(pub)
    assert pub.endpoint.startswith("tcp://127.0.0.1:") and not pub.endpoint.endswith("*")
    sub = Subscriber(pub.endpoint, list(T.ALL_TOPICS))
    closer(sub)
    _link(pub, None, n=len(T.ALL_TOPICS))
    sent = {t: pub.publish(t, E.example(t)) for t in T.ALL_TOPICS}
    got = {}
    for _ in T.ALL_TOPICS:
        m = sub.recv(TIMEOUT)
        assert m is not None
        got[m.topic] = m
    assert set(got) == set(T.ALL_TOPICS)
    for t in T.ALL_TOPICS:
        assert got[t].env == sent[t] and got[t].seq == 0
    assert sub.stats.received == len(T.ALL_TOPICS) and sub.stats.seq_gaps == 0


def test_tcp_round_trip_explicit_port(closer):
    port = _free_port()
    pub = Publisher(f"tcp://127.0.0.1:{port}", src="meas")
    closer(pub)
    sub = Subscriber(f"tcp://127.0.0.1:{port}", T.MEAS_RESULT)
    closer(sub)
    assert pub.endpoint == f"tcp://127.0.0.1:{port}"
    _probe_link(pub, sub, T.MEAS_RESULT)
    env = pub.publish(T.MEAS_RESULT, E.example(T.MEAS_RESULT))
    m = sub.recv(TIMEOUT)
    assert m is not None and m.env == env and m.src == "meas"


def test_wire_bytes_equal_encode(closer):
    """The publisher's spliced encoding is byte-identical to encode(envelope)."""
    ctx = zmq.Context.instance()
    pub = Publisher(_inproc(), use_xpub=True)
    closer(pub)
    raw = ctx.socket(zmq.SUB)
    closer(raw)
    raw.setsockopt(zmq.LINGER, 0)
    raw.setsockopt(zmq.SUBSCRIBE, b"")
    raw.connect(pub.endpoint)
    _link(pub, T.RX_SPECTRUM)
    data = E.example(T.RX_SPECTRUM)
    data["values"][5] = float("nan")
    data["extra_txt"] = "İzmir ğüşıöç"
    env = pub.publish(T.RX_SPECTRUM, data)
    assert raw.poll(int(TIMEOUT * 1000))
    frames = raw.recv_multipart()
    assert frames[0] == b"rx.spectrum"
    assert frames[1] == encode(env)
    assert env["data"]["values"][5] is None
    assert b"NaN" not in frames[1]


def test_exact_topic_filtering(closer):
    pub = Publisher(_inproc(), use_xpub=True)
    closer(pub)
    exact = Subscriber(pub.endpoint, [T.RX_STATUS])
    closer(exact)
    loose = Subscriber(pub.endpoint, [T.RX_STATUS], exact=False)
    closer(loose)
    _link(pub, T.RX_STATUS, n=2)
    pub.publish("rx.status.debug", {"a": 1})        # ZMQ prefix match, not our topic
    pub.publish("rx.statusx", {"a": 2})
    pub.publish(T.CTL_RX, {"a": 3})                  # not even a prefix match
    env = pub.publish(T.RX_STATUS, {"a": 4})
    m = exact.recv(TIMEOUT)
    assert m is not None and m.topic == T.RX_STATUS and m.env == env
    assert exact.stats.foreign_dropped == 2
    assert exact.recv(0.05) is None
    topics = [loose.recv(TIMEOUT).topic for _ in range(3)]
    assert topics == ["rx.status.debug", "rx.statusx", T.RX_STATUS]
    assert loose.stats.foreign_dropped == 0


def test_nonblocking_recv_skips_dropped_frames(closer):
    """recv(0) returns a queued valid message even if foreign frames precede it."""
    pub = Publisher(_inproc(), use_xpub=True)
    closer(pub)
    sub = Subscriber(pub.endpoint, [T.RX_STATUS])
    closer(sub)
    _link(pub, T.RX_STATUS)
    for i in range(20):
        pub.publish("rx.status.debug", {"i": i})
    env = pub.publish(T.RX_STATUS, {"ok": True})
    assert sub._sock.poll(int(TIMEOUT * 1000))
    m = sub.recv(0)
    assert m is not None and m.env == env
    assert sub.stats.foreign_dropped == 20
    assert sub.recv(0) is None


def test_seq_per_topic_and_per_source(closer):
    ep0, ep1 = _inproc(), _inproc()
    p0 = Publisher(ep0, src="rx0", use_xpub=True)
    closer(p0)
    p1 = Publisher(ep1, src="rx1", use_xpub=True)
    closer(p1)
    sub = Subscriber([ep0, ep1], [T.RX_STATUS, T.RX_SPECTRUM])
    closer(sub)
    assert sub.endpoints == [ep0, ep1]
    _link(p0, T.RX_SPECTRUM)
    _link(p1, T.RX_SPECTRUM)
    order = [T.RX_STATUS, T.RX_STATUS, T.RX_SPECTRUM, T.RX_STATUS, T.RX_SPECTRUM]
    sent0 = [p0.publish(t, {"i": i})["seq"] for i, t in enumerate(order)]
    sent1 = [p1.publish(T.RX_STATUS, {"i": i})["seq"] for i in range(2)]
    assert sent0 == [0, 1, 0, 2, 1] and sent1 == [0, 1]
    assert p0.next_seq(T.RX_STATUS) == 3 and p0.next_seq(T.RX_SPECTRUM) == 2
    assert p0.next_seq(T.CTL_ACK) == 0
    got: dict[tuple[str, str], list[int]] = {}
    for _ in range(len(order) + 2):
        m = sub.recv(TIMEOUT)
        assert m is not None
        got.setdefault((m.src, m.topic), []).append(m.seq)
    assert got == {("rx0", T.RX_STATUS): [0, 1, 2], ("rx0", T.RX_SPECTRUM): [0, 1],
                   ("rx1", T.RX_STATUS): [0, 1]}
    assert sub.stats.seq_gaps == 0 and sub.stats.seq_resets == 0


def test_nan_sanitized_over_the_wire(closer):
    pub = Publisher(_inproc(), use_xpub=True)
    closer(pub)
    sub = Subscriber(pub.endpoint, T.RX_STATUS)
    closer(sub)
    _link(pub, T.RX_STATUS)
    data = {"levels": {"peak_dbfs": np.float32(np.nan), "rms_dbfs": -np.inf},
            "arr": np.array([np.nan, 1.0]), "n": np.int32(3), "flag": np.bool_(False)}
    env = pub.publish(T.RX_STATUS, data)
    m = sub.recv(TIMEOUT)
    assert m is not None
    assert m.data == {"levels": {"peak_dbfs": None, "rms_dbfs": None}, "arr": [None, 1.0], "n": 3,
                      "flag": False}
    assert env["data"] == m.data


def test_big_int_t_ns_over_the_wire(closer):
    pub = Publisher(_inproc(), use_xpub=True)
    closer(pub)
    sub = Subscriber(pub.endpoint, T.RX_SPECTRUM)
    closer(sub)
    _link(pub, T.RX_SPECTRUM)
    big = 2**64 + 3
    pub.publish(T.RX_SPECTRUM, {"t_acq_ns": big - 1}, t_ns=big)
    m = sub.recv(TIMEOUT)
    assert m is not None and m.t_ns == big and m.data["t_acq_ns"] == big - 1


def test_publish_validation(closer):
    pytest.importorskip("jsonschema")
    from eds.bus.validate import MessageValidationError

    pub = Publisher(_inproc(), use_xpub=True, validate=True, src="rx0")
    closer(pub)
    assert pub.validating
    sub = Subscriber(pub.endpoint, T.RX_STATUS)
    closer(sub)
    _link(pub, T.RX_STATUS)
    bad = E.example(T.RX_STATUS)
    bad["state"] = "running"
    with pytest.raises(MessageValidationError):
        pub.publish(T.RX_STATUS, bad)
    assert sub.recv(0.1) is None                       # nothing was sent
    assert pub.next_seq(T.RX_STATUS) == 0 and pub.sent == 0
    nan_status = E.example(T.RX_STATUS)
    nan_status["fc_hz"] = float("nan")                 # -> null -> violates 'number'
    with pytest.raises(MessageValidationError):
        pub.publish(T.RX_STATUS, nan_status)
    env = pub.publish(T.RX_STATUS, E.example(T.RX_STATUS))
    assert env["seq"] == 0
    m = sub.recv(TIMEOUT)
    assert m is not None and m.seq == 0 and sub.stats.seq_gaps == 0
    with pytest.raises(Exception, match="Bilinmeyen topic"):
        pub.publish("rx.unknown", {})


def test_conflate_keeps_latest_per_source_and_topic(closer):
    pub = Publisher(_inproc(), use_xpub=True, hwm=1000)
    closer(pub)
    sub = Subscriber(pub.endpoint, [T.RX_SPECTRUM, T.RX_STATUS], conflate=True)
    closer(sub)
    _link(pub, T.RX_STATUS)
    _link(pub, T.RX_SPECTRUM)
    for i in range(50):
        pub.publish(T.RX_SPECTRUM, {"i": i})
    for i in range(3):
        pub.publish(T.RX_STATUS, {"i": i})
    got: list[Message] = []
    while (m := sub.recv(0.1)) is not None:
        got.append(m)
    spec = [m for m in got if m.topic == T.RX_SPECTRUM]
    stat = [m for m in got if m.topic == T.RX_STATUS]
    assert spec[-1].seq == 49 and stat[-1].seq == 2
    assert len(spec) <= 3 and len(stat) <= 2
    assert sub.stats.conflated == 53 - len(got)
    assert sub.stats.received == 53


def test_malformed_frames_are_counted_and_skipped(closer):
    ctx = zmq.Context.instance()
    raw = ctx.socket(zmq.XPUB)
    closer(raw)
    raw.setsockopt(zmq.LINGER, 0)
    raw.setsockopt(zmq.XPUB_VERBOSE, 1)
    ep = _inproc()
    raw.bind(ep)
    sub = Subscriber(ep, T.RX_STATUS)
    closer(sub)
    assert raw.poll(int(TIMEOUT * 1000)) and raw.recv() == b"\x01rx.status"
    good = encode(make_envelope(T.RX_STATUS, "rx0", 0, {"ok": 1}))
    raw.send(b"rx.status")                                                   # 1 frame
    raw.send_multipart([b"rx.status", b"{broken"])                           # not JSON
    raw.send_multipart([b"rx.status", good.replace(b'"ok":1', b'"ok":NaN')])  # NaN token
    raw.send_multipart([b"rx.status", encode(make_envelope(T.RX_SPECTRUM, "rx0", 0, {}))])  # mismatch
    raw.send_multipart([b"rx.status", good, b"extra"])                       # 3 frames
    raw.send_multipart([b"rx.status", good])
    m = sub.recv(TIMEOUT)
    assert m is not None and m.data == {"ok": 1}
    assert sub.stats.decode_errors == 5 and sub.stats.received == 1

    strict = Subscriber(ep, T.RX_STATUS, strict=True)
    closer(strict)
    assert raw.poll(int(TIMEOUT * 1000)) and raw.recv() == b"\x01rx.status"
    raw.send_multipart([b"rx.status", b"{broken"])
    with pytest.raises(MessageDecodeError):
        strict.recv(TIMEOUT)


def test_seq_gap_and_reset_detection(closer):
    ctx = zmq.Context.instance()
    raw = ctx.socket(zmq.XPUB)
    closer(raw)
    raw.setsockopt(zmq.LINGER, 0)
    raw.setsockopt(zmq.XPUB_VERBOSE, 1)
    ep = _inproc()
    raw.bind(ep)
    sub = Subscriber(ep, T.RX_STATUS)
    closer(sub)
    assert raw.poll(int(TIMEOUT * 1000)) and raw.recv() == b"\x01rx.status"
    for seq in (0, 1, 5, 0, 1):
        raw.send_multipart([b"rx.status", encode(make_envelope(T.RX_STATUS, "rx0", seq, {}))])
    seqs = [sub.recv(TIMEOUT).seq for _ in range(5)]
    assert seqs == [0, 1, 5, 0, 1]
    assert sub.stats.seq_gaps == 3 and sub.stats.seq_resets == 1
    assert sub.stats.to_dict()["seq_gaps"] == 3


def test_multithreaded_publish_unique_seq(closer):
    pub = Publisher(_inproc(), use_xpub=True, hwm=0)
    closer(pub)
    sub = Subscriber(pub.endpoint, T.RX_STATUS, rcvhwm=0)
    closer(sub)
    _link(pub, T.RX_STATUS)
    seqs: list[int] = []
    lock = threading.Lock()

    def worker(k: int) -> None:
        for i in range(250):
            s = pub.publish(T.RX_STATUS, {"k": k, "i": i})["seq"]
            with lock:
                seqs.append(s)

    threads = [threading.Thread(target=worker, args=(k,)) for k in range(4)]
    for th in threads:
        th.start()
    for th in threads:
        th.join()
    assert sorted(seqs) == list(range(1000))
    recv = [sub.recv(TIMEOUT).seq for _ in range(1000)]
    assert recv == list(range(1000))                   # wire order == seq order
    assert sub.stats.seq_gaps == 0


def test_wait_for_subscribers_counts(closer):
    pub = Publisher(_inproc(), use_xpub=True)
    closer(pub)
    assert pub.wait_for_subscribers(1, 0.05) is False
    s1 = Subscriber(pub.endpoint, T.RX_STATUS)
    closer(s1)
    s2 = Subscriber(pub.endpoint, [T.RX_STATUS, T.RX_SPECTRUM])
    closer(s2)
    assert pub.wait_for_subscribers(2, TIMEOUT, topic=T.RX_STATUS)
    assert pub.wait_for_subscribers(1, TIMEOUT, topic=T.RX_SPECTRUM)
    assert pub.wait_for_subscribers(1, 0.05, topic=T.CTL_ACK) is False
    assert pub.subscriptions() == {T.RX_STATUS: 2, T.RX_SPECTRUM: 1}
    s1.close()
    deadline = time.monotonic() + TIMEOUT
    while pub.subscriptions().get(T.RX_STATUS) != 1 and time.monotonic() < deadline:
        time.sleep(0.005)
    assert pub.subscriptions() == {T.RX_STATUS: 1, T.RX_SPECTRUM: 1}


def test_plain_pub_cannot_wait(closer):
    pub = Publisher(_inproc())
    closer(pub)
    with pytest.raises(NotSupported):
        pub.wait_for_subscribers()
    with pytest.raises(NotSupported):
        pub.subscriptions()


def test_lifecycle_and_errors(closer):
    ep = _inproc()
    with Publisher(ep, src="rx0") as pub:
        assert not pub.closed and "PUB" in repr(pub)
    assert pub.closed
    pub.close()                                        # idempotent
    with pytest.raises(BusError):
        pub.publish(T.RX_STATUS, {})
    with Subscriber(ep, T.RX_STATUS) as sub:
        assert sub.topics == frozenset({T.RX_STATUS})
    with pytest.raises(BusError):
        sub.recv(0)
    with pytest.raises(BusError):
        Publisher(_inproc(), src="")
    with pytest.raises(BusError):
        Publisher([], src="rx0")
    with pytest.raises(BusError):
        Publisher("bogus://nowhere")
    with pytest.raises(BusError):
        Subscriber(_inproc(), [])
    with pytest.raises(BusError):
        Subscriber(_inproc(), [""])                    # exact + "all topics" contradicts
    port = _free_port()
    p1 = Publisher(f"tcp://127.0.0.1:{port}")
    closer(p1)
    with pytest.raises(BusError, match="bind"):
        Publisher(f"tcp://127.0.0.1:{port}")


def test_dynamic_subscribe_and_all_topics(closer):
    pub = Publisher(_inproc(), use_xpub=True)
    closer(pub)
    sub = Subscriber(pub.endpoint, T.RX_STATUS)
    closer(sub)
    _link(pub, T.RX_STATUS)
    sub.subscribe(T.CTL_ACK)
    _link(pub, T.CTL_ACK)
    pub.publish(T.CTL_ACK, E.example(T.CTL_ACK))
    m = sub.recv(TIMEOUT)
    assert m is not None and m.topic == T.CTL_ACK
    sub.unsubscribe(T.RX_STATUS)
    assert sub.topics == frozenset({T.CTL_ACK})
    everything = Subscriber(pub.endpoint, [""], exact=False)
    closer(everything)
    _link(pub, T.DF_BEARING, n=1)
    pub.publish(T.DF_BEARING, E.example(T.DF_BEARING))
    m = everything.recv(TIMEOUT)
    assert m is not None and m.topic == T.DF_BEARING


@pytest.mark.realtime
def test_iter_messages_duration(closer):
    pub = Publisher(_inproc(), use_xpub=True)
    closer(pub)
    sub = Subscriber(pub.endpoint, T.RX_DETECTIONS)
    closer(sub)
    _link(pub, T.RX_DETECTIONS)
    for _ in range(5):
        pub.publish(T.RX_DETECTIONS, E.example(T.RX_DETECTIONS))
    t0 = time.monotonic()
    got = list(sub.iter_messages(0.3))
    dt = time.monotonic() - t0
    assert [m.seq for m in got] == [0, 1, 2, 3, 4]
    assert 0.29 <= dt < 1.0
    assert list(sub.iter_messages(0.0)) == []


@pytest.mark.realtime
def test_publish_throughput(closer):
    pub = Publisher(_inproc(), use_xpub=True, hwm=200)
    closer(pub)
    sub = Subscriber(pub.endpoint, T.RX_STATUS)
    closer(sub)
    _link(pub, T.RX_STATUS)
    payload = E.example(T.RX_STATUS)
    n = 3000
    t0 = time.perf_counter()
    for _ in range(n):
        pub.publish(T.RX_STATUS, payload)
    rate = n / (time.perf_counter() - t0)
    assert rate > 2000, f"yayın hızı {rate:.0f} msg/s"
    assert sub.recv(TIMEOUT) is not None
    assert pub.sent == n and pub.bytes_sent > n * 500


# =========================================================================== proxy
@pytest.mark.parametrize("transport", ["inproc", "tcp"])
def test_round_trip_via_proxy_thread(closer, transport):
    if transport == "inproc":
        fe, be = _inproc(), _inproc()
    else:
        fe, be = "tcp://127.0.0.1:*", "tcp://127.0.0.1:*"
    proxy = ProxyThread(fe, be).start()
    closer(proxy)
    assert proxy.running and not proxy.backend_endpoint.endswith("*")
    sub = Subscriber(proxy.backend_endpoint, [T.RX_SWEEP, T.CAL_RESULT])
    closer(sub)
    pub = Publisher(proxy.frontend_endpoint, bind=False, src="rx0", use_xpub=True)
    closer(pub)
    _link(pub, T.RX_SWEEP)
    _link(pub, T.CAL_RESULT)
    e1 = pub.publish(T.RX_SWEEP, E.example(T.RX_SWEEP))
    e2 = pub.publish(T.CAL_RESULT, E.example(T.CAL_RESULT))
    m1, m2 = sub.recv(TIMEOUT), sub.recv(TIMEOUT)
    assert m1 is not None and m2 is not None
    assert m1.env == e1 and m2.env == e2
    assert len(m1.data["values"]) == len(E.example(T.RX_SWEEP)["values"])


def test_proxy_two_producers_one_endpoint(closer):
    proxy = ProxyThread(_inproc(), _inproc()).start()
    closer(proxy)
    sub = Subscriber(proxy.backend_endpoint, T.RX_STATUS)
    closer(sub)
    pubs = []
    for name in ("rx0", "rx1"):
        p = Publisher(proxy.frontend_endpoint, bind=False, src=name, use_xpub=True)
        closer(p)
        _link(p, T.RX_STATUS)
        pubs.append(p)
    for p in pubs:
        p.publish(T.RX_STATUS, E.example(T.RX_STATUS))
    srcs = sorted(sub.recv(TIMEOUT).src for _ in pubs)
    assert srcs == ["rx0", "rx1"]


@pytest.mark.realtime
def test_proxy_thread_stops_within_one_second(closer):
    port_f, port_b = _free_port(), _free_port()
    fe, be = f"tcp://127.0.0.1:{port_f}", f"tcp://127.0.0.1:{port_b}"
    proxy = ProxyThread(fe, be).start()
    sub = Subscriber(be, T.CTL_RX)
    closer(sub)
    pub = Publisher(fe, bind=False, use_xpub=True, src="gui")
    closer(pub)
    _link(pub, T.CTL_RX)
    pub.publish(T.CTL_RX, E.example(T.CTL_RX))
    assert sub.recv(TIMEOUT) is not None
    t0 = time.perf_counter()
    proxy.stop(timeout_s=1.0)
    dt = time.perf_counter() - t0
    assert dt < 1.0 and not proxy.running
    proxy.stop()                                       # idempotent
    # sockets really released: the same ports can be bound again at once
    again = ProxyThread(fe, be).start()
    closer(again)
    assert again.frontend_endpoint == fe and again.backend_endpoint == be


def test_proxy_start_errors(closer):
    port = _free_port()
    first = ProxyThread(f"tcp://127.0.0.1:{port}", _inproc()).start()
    closer(first)
    clash = ProxyThread(f"tcp://127.0.0.1:{port}", _inproc())
    with pytest.raises(BusError, match="başlatılamadı"):
        clash.start()
    assert not clash.running
    with pytest.raises(BusError):
        ProxyThread("bogus://x", _inproc()).start()
    with pytest.raises(BusError, match="zaten"):
        first.start()


def test_run_proxy_returns_on_context_term():
    ctx = zmq.Context()
    fe, be = _inproc(), _inproc()
    th = threading.Thread(target=run_proxy, args=(fe, be, ctx), daemon=True)
    th.start()
    sub = Subscriber(be, T.DF_BEARING, context=ctx)
    pub = Publisher(fe, bind=False, use_xpub=True, context=ctx, src="df")
    try:
        _link(pub, T.DF_BEARING)
        env = pub.publish(T.DF_BEARING, E.example(T.DF_BEARING))
        m = sub.recv(TIMEOUT)
        assert m is not None and m.env == env
    finally:
        pub.close()
        sub.close()
    ctx.term()
    th.join(TIMEOUT)
    assert not th.is_alive()


# =========================================================================== config helpers
def test_from_config_direct(closer):
    port = _free_port()
    bus = BusConfig(mode="direct", pub_endpoint=f"tcp://127.0.0.1:{port}",
                    sub_endpoint=f"tcp://127.0.0.1:{port}", hwm=321, validate=False)
    pub = publisher_from_config(bus, src="rx0", use_xpub=True)
    closer(pub)
    assert pub.bind and pub.hwm == 321 and not pub.validating
    sub = subscriber_from_config(bus, [T.RX_STATUS])
    closer(sub)
    assert not sub.bind
    _link(pub, T.RX_STATUS)
    env = pub.publish(T.RX_STATUS, E.example(T.RX_STATUS))
    m = sub.recv(TIMEOUT)
    assert m is not None and m.env == env


def test_from_config_proxy(closer):
    pytest.importorskip("jsonschema")
    fp, bp = _free_port(), _free_port()
    bus = BusConfig(mode="proxy", proxy_frontend=f"tcp://127.0.0.1:{fp}",
                    proxy_backend=f"tcp://127.0.0.1:{bp}", validate=True)
    proxy = ProxyThread(bus.proxy_frontend, bus.proxy_backend).start()
    closer(proxy)
    sub = subscriber_from_config(bus, T.MEAS_RESULT)
    closer(sub)
    pub = publisher_from_config(bus, src="measure", use_xpub=True)
    closer(pub)
    assert not pub.bind and pub.validating                   # validate=None -> bus.validate
    _link(pub, T.MEAS_RESULT)
    env = pub.publish(T.MEAS_RESULT, E.example(T.MEAS_RESULT))
    m = sub.recv(TIMEOUT)
    assert m is not None and m.env == env and m.src == "measure"


def test_from_config_validate_override(closer):
    bus = BusConfig(mode="proxy", proxy_frontend=_inproc(), validate=True)
    pub = publisher_from_config(bus, src="x", validate=False)
    closer(pub)
    assert not pub.validating


def test_from_config_bad_mode():
    bus = BusConfig(mode="broadcast")
    with pytest.raises(ConfigError):
        publisher_from_config(bus, src="rx0")
    with pytest.raises(ConfigError):
        subscriber_from_config(bus, [T.RX_STATUS])
