"""Operator console rendered off-screen: synthetic messages and a live simulated RX node."""

from __future__ import annotations

import os
import time

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
QtWidgets = pytest.importorskip("PySide6.QtWidgets")
pytest.importorskip("pyqtgraph")

from eds.bus import topics as T  # noqa: E402
from eds.bus.pubsub import Message, Publisher  # noqa: E402
from eds.core.config import (  # noqa: E402
    AppConfig,
    BusConfig,
    CalConfig,
    DeviceConfig,
    DspConfig,
    RxConfig,
    SimConfig,
)
from eds.core.timeutil import now_ns  # noqa: E402
from eds.gui import theme as th  # noqa: E402
from eds.gui.app import MainWindow, main  # noqa: E402
from eds.gui.busclient import Snapshot  # noqa: E402


@pytest.fixture(scope="module")
def qapp():
    app = QtWidgets.QApplication.instance() or QtWidgets.QApplication([])
    th.apply(app)
    return app


def msg(topic, data, t=None):
    t = now_ns() if t is None else t
    return Message(topic, {"topic": topic, "v": 1, "src": "rx0", "seq": 0, "t_ns": t, "data": data}, t)


def spectrum(fc=434e6, n=512):
    vals = [-90.0] * n
    vals[n // 2 + 40] = -40.0
    return {"device": "rx0", "block_seq": 1, "t_acq_ns": now_ns() - 20_000_000, "t_acq_end_ns": now_ns() - 15_000_000,
            "t_proc_ns": now_ns(), "fc_hz": fc, "fs_hz": 4e6, "rbw_hz": 1957.0, "nfft": 4096, "averages": 8,
            "window": "blackmanharris", "f_start_hz": fc - 2e6, "f_step_hz": 4e6 / n, "n_bins": n, "decimation": 8,
            "unit": "dBFS", "calibrated": False, "ppm_corrected": False, "gain_db": [50.0], "noise_floor": -90.0,
            "values": vals, "overflow": False}


def detections(f=434.3125e6):
    d = {"f_center_hz": f, "f_peak_hz": f, "f_lo_hz": f - 5e3, "f_hi_hz": f + 5e3, "bw_hz": 10e3, "peak_db": -40.0,
         "band_power_db": -41.0, "snr_db": 50.0, "noise_db": -90.0, "n_bins": 6}
    return {"device": "rx0", "block_seq": 1, "t_acq_ns": now_ns() - 20_000_000, "t_acq_end_ns": now_ns() - 15_000_000,
            "t_proc_ns": now_ns(), "fc_hz": 434e6, "rbw_hz": 1957.0, "unit": "dBFS", "method": "os_cfar", "pfa": 1e-6,
            "detections": [d]}


def status(overflows=0):
    return {"device": {"name": "rx0", "backend": "sim"}, "state": "streaming", "fc_hz": 434e6, "fs_hz": 4e6,
            "gain_db": [50.0], "gain_mode": "manual", "ppm": {"value": None, "via": "none", "age_s": None},
            "stream": {"blocks": 10, "samples": 10, "overflows": overflows, "timeouts": 0, "host_drops": 0,
                       "discarded_samples": 0, "throughput_sps": 3.9e6, "elapsed_s": 2.0},
            "levels": {"peak_dbfs": -20.0, "rms_dbfs": -40.0, "clip_fraction": 0.0},
            "sensors": {"temperature_c": 35.0}, "warnings": []}


def test_panels_render_synthetic_bus_messages(qapp):
    w = MainWindow("tcp://127.0.0.1:59997")
    try:
        w.show()
        assert "SİMÜLASYON" not in w.header.mode.text()
        w.apply(Snapshot(spectrum=msg(T.RX_SPECTRUM, spectrum()),
                         events=[msg(T.RX_DETECTIONS, detections()), msg(T.RX_STATUS, status(overflows=2))]))
        w._refresh_slow(force=True)
        qapp.processEvents()
        assert w.frames_drawn == 1 and len(w.spectrum.curve.getData()[0]) == 512
        assert w.waterfall._count == 1
        assert w.table.model.rowCount() == 1 and w.table.model.track_at(0).id == "E001"
        assert w.header.mode.text() == "SİMÜLASYON"
        assert w.strip.chips["Overflow"].text() == "2" and "akış" in w.strip.chips["Durum"].text()
        assert "kalibrasyonsuz" in w.strip.chips["Kal. modu"].text()
        assert "~" in w.table.model.data(w.table.model.index(0, 3))          # uncalibrated level marked
        # selecting the emitter fills the parameter panel
        w.table.select_id("E001")
        qapp.processEvents()
        w._refresh_slow(force=True)
        assert w.params.table.isVisible() and w.params.table.rowCount() > 15
        # DF: no data → "no data" state; then a bearing arrives
        assert w.compass.bearing is None
        bearing = {"array": "pluto_pair", "method": "interferometer2", "f_hz": 434.3e6, "az_deg": 21.5,
                   "ambiguous_deg": [], "phase_deg": 40.0, "coherence": 0.98, "quality": 0.9, "calibrated": True,
                   "t_acq_ns": now_ns()}
        w.apply(Snapshot(events=[msg(T.DF_BEARING, bearing),
                                 msg(T.MEAS_RESULT, {"spec_id": "df_rms", "title": "DF", "value": 1.23, "unit": "°",
                                                     "status": "ok"})]))
        w._refresh_slow(force=True)
        assert w.compass.bearing["az_deg"] == 21.5 and w.compass.rms_text == "1.23°"
        img = w.grab()
        assert img.width() > 800 and img.height() > 500
    finally:
        w.close()


def test_gui_shows_live_data_from_a_simulated_rx_node(qapp):
    from eds.rx.node import RxNode

    app_cfg = AppConfig(
        devices=[DeviceConfig(name="rx0", backend="sim", sim={"realtime": False, "ppm": 0.0})],
        rx=RxConfig(center_freq_hz=434e6, sample_rate_hz=2e6, bandwidth_hz=2e6, gain_db=50.0,
                    block_size=32768, buffer_size=32768),
        dsp=DspConfig(nfft=2048, averages=8),
        bus=BusConfig(status_period_s=0.2, spectrum_rate_hz=20.0, spectrum_max_bins=1024),
        cal=CalConfig(ppm_override=0.0),
        sim=SimConfig(emitters=[{"name": "cw", "kind": "cw", "freq_hz": 434.3e6, "power_dbm": -70.0}], seed=3),
    )
    pub = Publisher("tcp://127.0.0.1:*", bind=True, src="rx0")
    w = MainWindow(pub.endpoint)
    node = RxNode(app_cfg, publisher=pub)
    try:
        w.show()
        node.start()
        end = time.monotonic() + 20.0
        while time.monotonic() < end and not (w.frames_drawn >= 3 and len(w.tracker) >= 1 and w.status):
            qapp.processEvents()
            time.sleep(0.02)
        w._refresh_slow(force=True)
        assert w.frames_drawn >= 3, "GUI must draw live rx.spectrum frames"
        assert any(abs(t.f_center_hz - 434.3e6) < 20e3 for t in w.tracker.tracks()), "CW emitter in the table"
        assert w.header.mode.text() == "SİMÜLASYON" and w.status["state"] == "streaming"
        assert w.client.error == ""
    finally:
        node.stop()
        w.close()
        pub.close()


def test_screenshot_option_writes_png_and_exits(qapp, tmp_path):
    out = tmp_path / "shot.png"
    assert main(["--connect", "tcp://127.0.0.1:59996", "--screenshot", str(out), "--after", "0.3"]) == 0
    assert out.stat().st_size > 10_000
