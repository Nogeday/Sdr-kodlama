#!/usr/bin/env python3
"""EDS bus'ını eds paketi OLMADAN dinlemek: yalnızca pyzmq + json (çerçeve biçimi referansı).

C++, C#, MATLAB, JavaScript ya da Rust ile GUI/DF yazan takım arkadaşları için: aynı adımları
kendi dilinizin ZeroMQ kütüphanesiyle (libzmq, NetMQ, zeromq.js, ...) uygulayın.

1. SUB soketi aç, RX düğümünün yayın ucuna BAĞLAN (varsayılan tcp://127.0.0.1:5555).
2. Topic önekine abone ol (ör. "rx.detections"). ZeroMQ ÖNEKLE süzer: "rx.s" hem rx.status hem
   rx.spectrum'u getirir. Bu yüzden gelen topic'i ayrıca tam eşitlikle kontrol edin.
3. Her mesaj 2 parçalıdır (multipart): [topic (UTF-8), zarf (UTF-8 JSON)].
4. Zarf: {"topic", "v": 1, "src", "seq", "t_ns", "data"}. "seq" kaynak+topic başına 0'dan artar
   (boşluk = kaçırılan mesaj); "t_ns" gönderim anı, 1970'ten beri nanosaniye (2**53'ten büyük:
   JavaScript'te BigInt kullanın). "data" topic'in şemasına uyar: contracts/schemas/.

Kullanım:  python examples/saf_zmq_dinleyici.py [UÇ] [ADET]
"""

import json
import sys

import zmq

endpoint = sys.argv[1] if len(sys.argv) > 1 else "tcp://127.0.0.1:5555"
adet = int(sys.argv[2]) if len(sys.argv) > 2 else 10
TOPIC = "rx.detections"

ctx = zmq.Context()
sock = ctx.socket(zmq.SUB)
sock.connect(endpoint)
sock.setsockopt(zmq.SUBSCRIBE, TOPIC.encode("utf-8"))
sock.setsockopt(zmq.RCVTIMEO, 5000)          # 5 s içinde mesaj yoksa zmq.Again

try:
    for _ in range(adet):
        topic, payload = sock.recv_multipart()
        if topic.decode("utf-8") != TOPIC:
            continue
        env = json.loads(payload)
        d = env["data"]
        peaks = ", ".join(f"{x['f_peak_hz'] / 1e6:.4f} MHz ({x['snr_db']:.0f} dB)" for x in d["detections"])
        print(f"{env['src']} seq={env['seq']} fc={d['fc_hz'] / 1e6:.3f} MHz birim={d['unit']}: "
              f"{len(d['detections'])} tespit {peaks}")
except zmq.Again:
    print(f"5 s içinde mesaj gelmedi ({endpoint}): RX düğümü çalışıyor mu?", file=sys.stderr)
    sys.exit(1)
finally:
    sock.close(linger=0)
    ctx.term()
