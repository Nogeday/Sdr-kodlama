#!/usr/bin/env python3
"""EDS bus dinleyici örneği: ``rx.detections`` ve ``rx.status`` mesajlarını dinler.

Ne işe yarar?
    RX düğümü (``eds rx``) tespitlerini ve durumunu ZeroMQ üzerinden yayınlar. GUI, DF ya da
    kayıt programı yazan takım arkadaşının ihtiyacı olan tek şey bu yayına abone olmaktır.
    Bu dosya o işin en küçük, çalışan örneğidir. Kopyalayıp kendi programınıza uyarlayın.

Kullanım (önce başka bir terminalde RX düğümünü başlatın: ``eds rx -c config/rx_sim.toml``)::

    python examples/bus_dinleyici.py                         # tcp://127.0.0.1:5555'i dinle
    python examples/bus_dinleyici.py -c config/rx_sim.toml   # uç noktayı konfigürasyondan al
    python examples/bus_dinleyici.py --min-snr 20 --duration 10

Her tespit için bir satır yazar (saat, kaynak, tepe frekansı, bant genişliği, güç, SNR) ve
her ``rx.status`` mesajında kısa bir durum satırı ekler. Ctrl-C ile durur.

Çerçeve biçimi (dil bağımsız): ZeroMQ multipart ``[topic, json]``; JSON bir zarftır:
``{"topic", "v", "src", "seq", "t_ns", "data"}``. Alanların tanımı:
``contracts/schemas/<topic>.schema.json``.
"""

from __future__ import annotations

import argparse
import sys
import time
from collections.abc import Callable, Sequence
from typing import Any

from eds.bus import RX_DETECTIONS, RX_STATUS, Subscriber
from eds.core.config import load_config

#: ``eds rx`` varsayılan konfigürasyonda bu uca yayın yapar (``[bus] sub_endpoint``).
DEFAULT_ENDPOINT = "tcp://127.0.0.1:5555"


def endpoint_from_config(path: str) -> str:
    """Abonenin bağlanacağı uç: direct → ``bus.sub_endpoint``, proxy → ``bus.proxy_backend``."""
    bus = load_config(path).bus
    return bus.proxy_backend if bus.mode == "proxy" else bus.sub_endpoint


def clock(t_ns: int) -> str:
    """Duvar saati ``SS:DD:ss.mmm`` (zarftaki ``t_ns`` = time.time_ns(), nanosaniye)."""
    t = t_ns / 1e9
    return time.strftime("%H:%M:%S", time.localtime(t)) + f".{int((t % 1) * 1000):03d}"


def detection_line(src: str, data: dict[str, Any], det: dict[str, Any], t_recv_ns: int) -> str:
    """Bir tespit için tek satır. Güç alanları mesajın biriminde (``data['unit']``: dBm / dBFS)."""
    unit = data.get("unit", "dB")
    return (f"{clock(t_recv_ns)}  {src:<5} TESPİT  {det['f_peak_hz'] / 1e6:11.6f} MHz  "
            f"BW {det['bw_hz'] / 1e3:8.1f} kHz  {det['band_power_db']:7.1f} {unit:<5} "
            f"SNR {det['snr_db']:5.1f} dB")


def status_line(src: str, data: dict[str, Any], t_recv_ns: int) -> str:
    """``rx.status`` özeti: durum, merkez frekansı, kazanç, overflow/drop sayaçları, ppm."""
    st = data["stream"]
    ppm = (data.get("ppm") or {}).get("value")
    ppm_txt = f"{ppm:+.3f}" if isinstance(ppm, (int, float)) else "—"
    gains = "/".join(f"{g:g}" for g in data["gain_db"])
    return (f"{clock(t_recv_ns)}  {src:<5} DURUM   {data['state']}  fc {data['fc_hz'] / 1e6:.3f} MHz  "
            f"G {gains} dB  overflow {st['overflows']}  drop {st['host_drops']}  ppm {ppm_txt}")


def listen(endpoint: str, *, duration_s: float | None = None, min_snr_db: float = 10.0,
           count: int | None = None, show_status: bool = True,
           out: Callable[[str], None] = print) -> int:
    """``endpoint``'i dinle; yazılan tespit satırı sayısını döndür.

    ``duration_s`` (None = sonsuza dek) ya da ``count`` tespit satırı yazılınca durur.
    """
    n = 0
    deadline = None if duration_s is None else time.monotonic() + duration_s
    with Subscriber(endpoint, [RX_DETECTIONS, RX_STATUS]) as sub:
        while count is None or n < count:
            wait = 0.5 if deadline is None else min(0.5, deadline - time.monotonic())
            if wait <= 0:
                break
            msg = sub.recv(wait)                 # None = bu süre içinde mesaj gelmedi
            if msg is None:
                continue
            if msg.topic == RX_STATUS:
                if show_status:
                    out(status_line(msg.src, msg.data, msg.t_recv_ns))
                continue
            # rx.detections: HER analiz karesi için bir mesaj gelir (boş liste dahil)
            for det in sorted(msg.data["detections"], key=lambda d: d["snr_db"], reverse=True):
                if det["snr_db"] < min_snr_db:
                    continue
                out(detection_line(msg.src, msg.data, det, msg.t_recv_ns))
                n += 1
                if count is not None and n >= count:
                    break
        if sub.stats.seq_gaps:
            print(f"UYARI: {sub.stats.seq_gaps} mesaj kaçırıldı (seq boşluğu: dinleyici yavaş kaldı)",
                  file=sys.stderr)
    return n


def main(argv: Sequence[str] | None = None) -> int:
    p = argparse.ArgumentParser(description="EDS rx.detections / rx.status dinleyici örneği")
    p.add_argument("-c", "--config", metavar="DOSYA", help="uç noktayı bu konfigürasyonun [bus] bölümünden al")
    p.add_argument("--endpoint", metavar="UÇ", help=f"bağlanılacak uç (vars. {DEFAULT_ENDPOINT})")
    p.add_argument("--duration", type=float, metavar="S", help="en fazla S saniye dinle (vars. Ctrl-C'ye kadar)")
    p.add_argument("--count", type=int, metavar="N", help="N tespit satırından sonra çık")
    p.add_argument("--min-snr", type=float, default=10.0, metavar="DB", help="bu SNR'nin altındaki tespitleri gizle")
    p.add_argument("--no-status", action="store_true", help="rx.status satırlarını yazma")
    a = p.parse_args(argv)
    endpoint = a.endpoint or (endpoint_from_config(a.config) if a.config else DEFAULT_ENDPOINT)
    print(f"Dinleniyor: {endpoint} (rx.detections, rx.status) — durdurmak için Ctrl-C", file=sys.stderr)
    try:
        n = listen(endpoint, duration_s=a.duration, min_snr_db=a.min_snr, count=a.count,
                   show_status=not a.no_status)
    except KeyboardInterrupt:
        return 0
    if n == 0:
        print("Hiç tespit alınmadı: RX düğümü çalışıyor mu, uç nokta doğru mu?", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
