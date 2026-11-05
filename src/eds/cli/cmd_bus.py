"""``eds bus proxy | echo | send`` — message bus tools, plus the one-shot publisher used by
other commands (``--publish``)."""

from __future__ import annotations

import argparse
import sys
import time
import uuid
from typing import Any

from ..bus.pubsub import Message, ProxyThread, Publisher, Subscriber
from ..bus.topics import ALL_TOPICS, CTL_ACK, CTL_RX, registry
from ..core.config import AppConfig, BusConfig
from ..core.errors import BusError, ConfigError
from ..core.timeutil import mono
from ..core.units import fmt_hz
from .util import compact_json, load_app, nonfinite_paths, parse_kv_list

#: Max wait for the subscription of a one-shot message's receiver (ZMQ slow joiner).
ONE_SHOT_WAIT_S = 1.0
#: How long unsent messages may linger after close (one-shot publishers).
ONE_SHOT_LINGER_MS = 1000
#: Minimum time between connecting the ack subscriber and sending the command.
ACK_SUB_SETTLE_S = 0.2
#: ``ctl.rx`` ``req_id`` length limit (contracts/schemas/ctl.rx.schema.json).
REQ_ID_MAX_LEN = 128


def _publisher(bus: BusConfig, src: str, endpoint: str | None = None) -> Publisher:
    """XPUB publisher for one-shot messages: direct → bind (``endpoint`` or ``pub_endpoint``),
    proxy → connect (``endpoint`` or ``proxy_frontend``)."""
    if bus.mode == "direct":
        return Publisher(endpoint or bus.pub_endpoint, bind=True, src=src, validate=bus.validate, hwm=bus.hwm,
                         linger_ms=ONE_SHOT_LINGER_MS, use_xpub=True)
    if bus.mode == "proxy":
        return Publisher(endpoint or bus.proxy_frontend, bind=False, src=src, validate=bus.validate, hwm=bus.hwm,
                         linger_ms=ONE_SHOT_LINGER_MS, use_xpub=True)
    raise ConfigError(f"bus.mode = {bus.mode!r} geçersiz; seçenekler: ['direct', 'proxy']")


def _sub_endpoint(bus: BusConfig) -> str:
    if bus.mode == "direct":
        return bus.sub_endpoint
    if bus.mode == "proxy":
        return bus.proxy_backend
    raise ConfigError(f"bus.mode = {bus.mode!r} geçersiz; seçenekler: ['direct', 'proxy']")


def one_shot_publish(app: AppConfig, src: str, topic: str, data: dict[str, Any],
                     wait_s: float = ONE_SHOT_WAIT_S) -> dict[str, Any]:
    """Publish one message (waiting up to ``wait_s`` for a subscriber of ``topic``)."""
    pub = _publisher(app.bus, src)
    try:
        if not pub.wait_for_subscribers(1, wait_s, topic=topic):
            print(f"UYARI: '{topic}' için {wait_s:g} s içinde abone görülmedi; mesaj yine de gönderildi "
                  "(dinleyen yoksa kaybolur)", file=sys.stderr)
        env = pub.publish(topic, data)
        print(f"Yayınlandı: {topic} → {pub.endpoint} (seq {env['seq']})")
        return env
    finally:
        pub.close()


# =============================================================================== proxy
def cmd_bus_proxy(args: argparse.Namespace) -> int:
    app = load_app(args)
    front = args.frontend or app.bus.proxy_frontend
    back = args.backend_ep or app.bus.proxy_backend
    px = ProxyThread(front, back, hwm=max(int(app.bus.hwm), 1000))
    px.start()
    dur = "Ctrl-C ile durdurun" if args.duration is None else f"{args.duration:g} s"
    print(f"Bus proxy çalışıyor ({dur}): yayıncılar → {px.frontend_endpoint} (XSUB), "
          f"aboneler → {px.backend_endpoint} (XPUB)", file=sys.stderr)
    try:
        end = None if args.duration is None else mono() + float(args.duration)
        while px.running:
            if end is not None and mono() >= end:
                break
            time.sleep(0.2 if end is None else min(0.2, max(end - mono(), 0.0)))
        if not px.running:
            raise BusError("Proxy iş parçacığı beklenmedik şekilde durdu")
    except KeyboardInterrupt:
        print("Proxy durduruluyor…", file=sys.stderr)
    finally:
        px.stop()
    return 0


# =============================================================================== echo
def _ts(t_ns: int) -> str:
    t = t_ns / 1e9
    return time.strftime("%H:%M:%S", time.localtime(t)) + f".{int((t % 1) * 1000):03d}"


def summarize(msg: Message, limit: int = 220) -> str:
    """One compact human-readable line for a bus message (falls back to compact JSON)."""
    d = msg.data
    t = msg.topic
    try:
        if t == "rx.status":
            st, ppm = d["stream"], d.get("ppm") or {}
            pv = ppm.get("value")
            ptxt = f"{pv:+.3f}" if isinstance(pv, (int, float)) else "—"
            s = (f"{d['state']} fc={fmt_hz(d['fc_hz'])} fs={d['fs_hz'] / 1e6:.4g}MS/s G={d['gain_db']} "
                 f"blok={st['blocks']} ovf={st['overflows']} drop={st['host_drops']} ppm={ptxt}({ppm.get('via', '-')})"
                 f" uyarı={len(d.get('warnings') or [])}")
        elif t == "rx.detections":
            dets = sorted(d["detections"], key=lambda x: x["snr_db"], reverse=True)[:5]
            lat = (msg.t_recv_ns - d["t_acq_end_ns"]) / 1e6
            s = (f"{len(d['detections'])} tespit (gecikme {lat:.1f} ms) "
                 + " ".join(f"{fmt_hz(x['f_peak_hz'], 9)}/{x['snr_db']:.0f}dB" for x in dets))
        elif t == "rx.spectrum":
            s = (f"fc={fmt_hz(d['fc_hz'])} {len(d['values'])} nokta (D={d['decimation']}) RBW={fmt_hz(d['rbw_hz'])} "
                 f"taban={d['noise_floor']:.1f} {d['unit']}")
        elif t == "rx.sweep":
            s = (f"{fmt_hz(d['f_start_hz'])}–{fmt_hz(d['f_stop_hz'])} {d['duration_s']:.3f}s "
                 f"{d['rate_hz_per_s'] / 1e9:.3g}GHz/s {len(d['detections'])} tespit {len(d['values'])} nokta")
        elif t == "ctl.rx":
            s = f"{d['req_id']} → {d['target']}: {d['cmd']} {compact_json(d.get('args') or {})}"
        elif t == "ctl.ack":
            res = "OK" if d["ok"] else f"HATA: {d.get('error')}"
            result = d.get("result")
            s = f"{d['req_id']} ← {d.get('target', '?')}: {d.get('cmd', '?')} {res}"
            if result is not None:
                s += f" {compact_json(result)}"
        elif t == "cal.result":
            s = f"{d['device']} {d['kind']} via={d.get('applied_via', '-')} {compact_json(d['values'])}"
        elif t == "meas.result":
            s = (f"{d.get('spec_id')} = {d.get('value')} {d.get('unit', '')} [{d.get('status')}] "
                 f"geçti={d.get('passed')}")
        else:
            s = compact_json(d)
    except (KeyError, TypeError, ValueError, AttributeError):
        s = compact_json(d)                      # foreign/partial payload: show it raw
    if len(s) > limit:
        s = s[: limit - 1] + "…"
    return s


def cmd_bus_echo(args: argparse.Namespace) -> int:
    app = load_app(args)
    topics = list(args.topics) if args.topics else list(ALL_TOPICS)
    known = registry()
    unknown = [t for t in topics if t not in known]
    if unknown:
        raise ConfigError(f"Bilinmeyen topic(ler): {unknown} — kayıtlı olanlar (contracts/topics.json): "
                          f"{', '.join(sorted(known))}")
    ep = args.endpoint or _sub_endpoint(app.bus)
    count = 0
    with Subscriber(ep, topics) as sub:
        print(f"Dinleniyor: {ep} topic={','.join(topics)}", file=sys.stderr)
        end = None if args.timeout is None else mono() + float(args.timeout)
        try:
            while args.count is None or count < args.count:
                remaining = None if end is None else end - mono()
                if remaining is not None and remaining <= 0:
                    break
                msg = sub.recv(0.25 if remaining is None else min(0.25, remaining))
                if msg is None:
                    continue
                count += 1
                if args.raw:
                    print(compact_json(msg.env), flush=True)
                else:
                    lat = (msg.t_recv_ns - msg.t_ns) / 1e6
                    print(f"{_ts(msg.t_recv_ns)} {msg.topic:<14} {msg.src}#{msg.seq} (+{lat:.2f} ms) "
                          f"{summarize(msg)}", flush=True)
        except KeyboardInterrupt:
            pass
        if sub.stats.seq_gaps:
            print(f"UYARI: {sub.stats.seq_gaps} mesaj kayıp (seq boşluğu; HWM aşımı)", file=sys.stderr)
    if args.count is not None and count < args.count:
        print(f"Zaman aşımı: {count}/{args.count} mesaj alındı", file=sys.stderr)
        return 1
    return 0


# =============================================================================== send
def cmd_bus_send(args: argparse.Namespace) -> int:
    app = load_app(args)
    bus = app.bus
    if bus.mode == "direct" and not args.endpoint and not bus.ctl_endpoints:
        raise ConfigError("bus.ctl_endpoints boş: komutun bind edileceği uç noktayı --endpoint ile verin "
                          "(RX düğümleri bus.ctl_endpoints'e bağlanır)")
    endpoint = args.endpoint or (bus.ctl_endpoints[0] if bus.mode == "direct" else bus.proxy_frontend)
    req_id = uuid.uuid4().hex[:16] if args.req_id is None else str(args.req_id)
    if not 1 <= len(req_id) <= REQ_ID_MAX_LEN:
        raise ConfigError(f"--req-id 1–{REQ_ID_MAX_LEN} karakter olmalı ({len(req_id)} karakter geldi)")
    if not args.target:
        raise ConfigError("--target boş olamaz (cihaz adı ya da '*')")
    data: dict[str, Any] = {"req_id": req_id, "target": args.target, "cmd": args.cmd}
    cmd_args = parse_kv_list(args.arg, "--arg")
    bad = nonfinite_paths(cmd_args)
    if bad:
        raise ConfigError(f"--arg: sonlu olmayan sayı (nan/inf) bus üzerinden gönderilemez (JSON'da yok): "
                          f"{', '.join(bad)}")
    if cmd_args:
        data["args"] = cmd_args
    ack_sub: Subscriber | None = None
    t_sub = mono()
    try:
        if args.wait_ack and args.wait_ack > 0:
            ack_sub = Subscriber(args.ack_endpoint or _sub_endpoint(bus), [CTL_ACK])
            t_sub = mono()
        pub = _publisher(bus, args.src, endpoint)
        try:
            if not pub.wait_for_subscribers(1, float(args.sub_timeout), topic=CTL_RX):
                print(f"HATA: {args.sub_timeout:g} s içinde ctl.rx dinleyen RX düğümü bulunamadı ({pub.endpoint})",
                      file=sys.stderr)
                return 1
            if ack_sub is not None:
                settle = ACK_SUB_SETTLE_S - (mono() - t_sub)
                if settle > 0:
                    time.sleep(settle)            # SUB → PUB subscription propagation (slow joiner)
            pub.publish(CTL_RX, data)
            print(f"Gönderildi: ctl.rx {data['cmd']} → {data['target']} (req_id {data['req_id']}) via {pub.endpoint}")
        finally:
            pub.close()
        if ack_sub is None:
            return 0
        acks: list[dict[str, Any]] = []
        end = mono() + float(args.wait_ack)
        while (remaining := end - mono()) > 0:
            msg = ack_sub.recv(remaining)
            if msg is None or msg.data.get("req_id") != data["req_id"]:
                continue
            acks.append(msg.data)
            print(f"{_ts(msg.t_recv_ns)} ctl.ack {msg.src}#{msg.seq} {summarize(msg, limit=2000)}")
            if args.target != "*":
                break
        if not acks:
            print(f"HATA: {args.wait_ack:g} s içinde ctl.ack alınamadı (req_id {data['req_id']})", file=sys.stderr)
            return 1
        return 0 if all(a.get("ok") for a in acks) else 2
    finally:
        if ack_sub is not None:
            ack_sub.close()
