#!/usr/bin/env python3
"""EDS komut gönderme örneği: ``ctl.rx set_center_freq`` yayınla, ``ctl.ack`` yanıtını bekle.

Ne işe yarar?
    GUI'deki "frekansa git" düğmesinin ya da DF programının RX düğümünü yeniden ayarlamasının
    arkasındaki kod budur. ``send_command()`` fonksiyonunu kendi programınıza kopyalayabilir ya da
    bu dosyayı içe aktarabilirsiniz (aşağıda).

RX düğümü komutları nereden dinler?
    * ``[bus] mode = "direct"`` (varsayılan): düğüm ``bus.ctl_endpoints`` listesindeki uçlara
      BAĞLANIR, komut gönderen o ucu BIND eder. Liste boşsa uzaktan kontrol kapalıdır!
      Düğümü şöyle başlatın::

          eds rx -c config/rx_sim.toml --set "bus.ctl_endpoints=['tcp://127.0.0.1:5556']"

    * ``mode = "proxy"``: herkes ``eds bus proxy`` aracına bağlanır (komut: proxy_frontend,
      yanıt: proxy_backend).

Kullanım::

    python examples/komut_gonder.py 433.92e6                     # direct, varsayılan uçlar
    python examples/komut_gonder.py 2.437e9 --target rx0 --timeout 3
    python examples/komut_gonder.py 446e6 -c benim_config.toml   # uçları konfigürasyondan al

Başka programdan (GUI: kanalı bir kez açın, her düğmede send çağırın)::

    from komut_gonder import Commander
    kanal = Commander("tcp://127.0.0.1:5556", "tcp://127.0.0.1:5555")
    acks = kanal.send("set_gain", {"db": 30})
    print(acks[0]["ok"], acks[0]["result"])          # True {'gain_db': [30.0], 'applied_db': 30.0}
    kanal.close()                                     # program kapanırken

Çıkış kodu: 0 = komut uygulandı (ok), 2 = düğüm komutu reddetti (ok = false, ör. profil
dışı frekans), 1 = dinleyen düğüm yok ya da yanıt gelmedi.
"""

from __future__ import annotations

import argparse
import sys
import time
import uuid
from collections.abc import Mapping, Sequence
from typing import Any

from eds.bus import CTL_ACK, CTL_RX, Publisher, Subscriber
from eds.core.config import load_config

#: Varsayılan uçlar (direct mod): komut ucu (bu program bind eder) ve yayın ucu (ctl.ack).
DEFAULT_CTL_ENDPOINT = "tcp://127.0.0.1:5556"
DEFAULT_ACK_ENDPOINT = "tcp://127.0.0.1:5555"
#: Yanıt aboneliğinin düğüme ulaşması için kısa bekleme (ZeroMQ "slow joiner").
SUBSCRIBE_SETTLE_S = 0.2


class NoListener(RuntimeError):
    """Komut ucunu dinleyen RX düğümü yok."""


def endpoints_from_config(path: str) -> tuple[str, str, bool]:
    """``(komut ucu, yanıt ucu, bind?)``: direct → (ctl_endpoints[0], sub_endpoint, bind),
    proxy → (proxy_frontend, proxy_backend, connect)."""
    bus = load_config(path).bus
    if bus.mode == "proxy":
        return bus.proxy_frontend, bus.proxy_backend, False
    if not bus.ctl_endpoints:
        raise SystemExit(f"{path}: [bus] ctl_endpoints boş — RX düğümü komut dinlemiyor. "
                         "ctl_endpoints = [\"tcp://127.0.0.1:5556\"] yazın ya da --ctl-endpoint verin.")
    return bus.ctl_endpoints[0], bus.sub_endpoint, True


class Commander:
    """Açık kalan komut kanalı: GUI / DF programı bunu BİR KEZ açar, her komutta :meth:`send` çağırır.

    Neden açık kalmalı? Direct modda komut ucu bu program tarafından BIND edilir; soketi her
    komutta kapatıp aynı portu hemen yeniden bind etmek "Address already in use" hatası verebilir
    ve düğümün yeniden bağlanması da zaman alır.
    """

    def __init__(self, ctl_endpoint: str = DEFAULT_CTL_ENDPOINT, ack_endpoint: str = DEFAULT_ACK_ENDPOINT,
                 *, bind: bool = True, src: str = "komut_gonder"):
        # 1) Önce yanıt aboneliği (yanıt komuttan hemen sonra gelir; geç abone olan kaçırır)
        self._ack = Subscriber(ack_endpoint, [CTL_ACK])
        try:
            # 2) Komut yayıncısı. XPUB: aboneleri görebiliriz → düğüm bağlanana kadar bekleyebiliriz
            self._pub = Publisher(ctl_endpoint, bind=bind, src=src, use_xpub=True, linger_ms=1000)
        except BaseException:
            self._ack.close()
            raise
        self._t_open = time.monotonic()
        self.ctl_endpoint = ctl_endpoint

    def send(self, cmd: str, args: Mapping[str, Any] | None = None, *, target: str = "*",
             timeout_s: float = 3.0, expect: int = 1) -> list[dict[str, Any]]:
        """``ctl.rx`` komutunu gönder, ``expect`` adet ``ctl.ack`` (ya da ``timeout_s``) bekle.

        Dönüş: alınan yanıtların ``data`` sözlükleri (``req_id, target, cmd, ok, error, result``).
        Boş liste = süre içinde yanıt gelmedi. Dinleyen düğüm yoksa :class:`NoListener`.
        """
        req_id = uuid.uuid4().hex[:16]           # yanıtı komutla eşleştiren kimlik
        data: dict[str, Any] = {"req_id": req_id, "target": target, "cmd": cmd}
        if args:
            data["args"] = dict(args)
        if not self._pub.wait_for_subscribers(1, timeout_s, topic=CTL_RX):
            raise NoListener(f"{timeout_s:g} s içinde {self.ctl_endpoint} ucunda ctl.rx dinleyen RX düğümü yok")
        time.sleep(max(0.0, SUBSCRIBE_SETTLE_S - (time.monotonic() - self._t_open)))
        self._pub.publish(CTL_RX, data)
        # 3) Aynı req_id'li yanıtları topla (önceki komutların geç yanıtları atlanır)
        acks: list[dict[str, Any]] = []
        deadline = time.monotonic() + timeout_s
        while len(acks) < expect and (left := deadline - time.monotonic()) > 0:
            msg = self._ack.recv(left)
            if msg is not None and msg.data.get("req_id") == req_id:
                acks.append(msg.data)
        return acks

    def close(self) -> None:
        self._pub.close()
        self._ack.close()

    def __enter__(self) -> Commander:
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()


def send_command(cmd: str, args: Mapping[str, Any] | None = None, *, ctl_endpoint: str = DEFAULT_CTL_ENDPOINT,
                 ack_endpoint: str = DEFAULT_ACK_ENDPOINT, bind: bool = True, target: str = "*",
                 timeout_s: float = 3.0, expect: int = 1) -> list[dict[str, Any]]:
    """Tek seferlik komut (betikler için): kanal aç → gönder → yanıtları döndür → kapat."""
    with Commander(ctl_endpoint, ack_endpoint, bind=bind) as c:
        return c.send(cmd, args, target=target, timeout_s=timeout_s, expect=expect)


def main(argv: Sequence[str] | None = None) -> int:
    p = argparse.ArgumentParser(description="RX düğümüne set_center_freq gönder ve ctl.ack bekle")
    p.add_argument("frekans_hz", type=float, help="yeni merkez frekansı [Hz], ör. 433.92e6")
    p.add_argument("-c", "--config", metavar="DOSYA", help="uçları bu konfigürasyonun [bus] bölümünden al")
    p.add_argument("--ctl-endpoint", metavar="UÇ", help=f"komut ucu (vars. {DEFAULT_CTL_ENDPOINT}, bind)")
    p.add_argument("--ack-endpoint", metavar="UÇ", help=f"ctl.ack dinleme ucu (vars. {DEFAULT_ACK_ENDPOINT})")
    p.add_argument("--target", default="*", metavar="AD", help="hedef düğüm adı ya da '*' (vars. '*')")
    p.add_argument("--timeout", type=float, default=3.0, metavar="S", help="bekleme süresi [s] (vars. 3)")
    a = p.parse_args(argv)
    ctl, ack, bind = DEFAULT_CTL_ENDPOINT, DEFAULT_ACK_ENDPOINT, True
    if a.config:
        ctl, ack, bind = endpoints_from_config(a.config)
    ctl, ack = a.ctl_endpoint or ctl, a.ack_endpoint or ack
    try:
        acks = send_command("set_center_freq", {"hz": a.frekans_hz}, ctl_endpoint=ctl, ack_endpoint=ack,
                            bind=bind, target=a.target, timeout_s=a.timeout)
    except NoListener as e:
        print(f"HATA: {e}", file=sys.stderr)
        return 1
    if not acks:
        print(f"HATA: {a.timeout:g} s içinde ctl.ack gelmedi", file=sys.stderr)
        return 1
    for ack_data in acks:
        if ack_data["ok"]:
            print(f"OK    {ack_data.get('target', '?')}: {ack_data.get('cmd')} → {ack_data.get('result')}")
        else:
            print(f"HATA  {ack_data.get('target', '?')}: {ack_data.get('cmd')} reddedildi: {ack_data.get('error')}")
    return 0 if all(x["ok"] for x in acks) else 2


if __name__ == "__main__":
    sys.exit(main())
