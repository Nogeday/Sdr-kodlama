"""BusClient against a real ZMQ publisher: newest spectrum only, events all kept."""

from __future__ import annotations

import time

from eds.bus import topics as T
from eds.bus.pubsub import Publisher
from eds.gui.busclient import BusClient


def _spectrum(i):
    return {"device": "rx0", "block_seq": i, "t_acq_ns": 1, "t_acq_end_ns": 2, "t_proc_ns": 3, "fc_hz": 434e6,
            "fs_hz": 4e6, "rbw_hz": 1957.0, "nfft": 4096, "averages": 16, "window": "blackmanharris",
            "f_start_hz": 432e6, "f_step_hz": 2e3, "n_bins": 4, "decimation": 1, "unit": "dBFS",
            "calibrated": False, "ppm_corrected": False, "gain_db": [40.0], "noise_floor": -80.0,
            "values": [-80.0, -70.0, -60.0, -80.0], "overflow": False}


def _detections(i):
    return {"device": "rx0", "block_seq": i, "t_acq_ns": 1, "t_acq_end_ns": 2, "t_proc_ns": 3, "fc_hz": 434e6,
            "rbw_hz": 1957.0, "unit": "dBFS", "method": "os_cfar", "pfa": 1e-6, "detections": []}


def _wait_connected(pub, client, timeout_s=5.0):
    end = time.monotonic() + timeout_s
    while time.monotonic() < end:
        pub.publish(T.RX_DETECTIONS, _detections(0))
        time.sleep(0.05)
        if client.received.get(T.RX_DETECTIONS):
            client.take()
            return
    raise AssertionError("abone bağlanamadı")


def test_busclient_keeps_only_newest_spectrum_but_every_event():
    pub = Publisher("tcp://127.0.0.1:*", bind=True, src="rx0", validate=True)
    client = BusClient(pub.endpoint).start()
    try:
        _wait_connected(pub, client)
        for i in range(1, 51):
            pub.publish(T.RX_SPECTRUM, _spectrum(i))
            pub.publish(T.RX_DETECTIONS, _detections(i))
        time.sleep(0.5)
        snap = client.take()
        assert snap.spectrum is not None and snap.spectrum.data["block_seq"] == 50   # newest frame
        seqs = [m.data["block_seq"] for m in snap.events if m.topic == T.RX_DETECTIONS]
        assert seqs == list(range(1, 51))                                           # no event lost
        again = client.take()
        assert again.spectrum is None and again.events == []
        assert client.received[T.RX_SPECTRUM] >= 1 and client.error == ""
    finally:
        client.stop()
        pub.close()
    assert not client.running
