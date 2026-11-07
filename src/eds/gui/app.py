"""EDS operator console — main window.

The window is a passive **bus subscriber**: it renders ``rx.spectrum`` (live PSD +
waterfall), ``rx.detections`` (emitter table / parameters / spectrum overlay),
``rx.status`` (status bar), ``df.bearing`` (compass) and ``meas.result`` (``df_rms``).
It runs in its own process and never slows the pipeline down (see :mod:`eds.gui.busclient`).

    python -m eds.gui                                   # default endpoint tcp://127.0.0.1:5555
    python -m eds.gui --connect tcp://10.0.0.5:5551     # remote pipeline (e.g. Jetson) via proxy
    python -m eds.gui -c config/sim_gui.toml            # endpoint from the config's [bus] section
"""

from __future__ import annotations

import argparse
import math
import signal
import sys
import time
from collections.abc import Sequence
from pathlib import Path

from PySide6 import QtCore, QtGui, QtWidgets

from ..bus.topics import DF_BEARING, MEAS_RESULT, RX_DETECTIONS, RX_SPECTRUM, RX_STATUS
from ..core.timeutil import now_ns
from . import theme as th
from .busclient import BusClient
from .model import EmitterTracker, LatencyStats, SpectrumFrame, health_view
from .widgets import DfCompass, EmitterTable, ParamPanel, SpectrumPanel, StatusStrip, WaterfallPanel

Qt = QtCore.Qt
DEFAULT_ENDPOINT = "tcp://127.0.0.1:5555"


def endpoint_from_config(path: str | None) -> str:
    if not path:
        return DEFAULT_ENDPOINT
    from ..core.config import load_config

    bus = load_config(path).bus
    return bus.proxy_backend if bus.mode == "proxy" else bus.sub_endpoint


class Header(QtWidgets.QWidget):
    def __init__(self, endpoint: str, parent: QtWidgets.QWidget | None = None):
        super().__init__(parent)
        self.setStyleSheet(f"background: {th.PANEL}; border-bottom: 1px solid {th.BORDER};")
        lay = QtWidgets.QHBoxLayout(self)
        lay.setContentsMargins(12, 6, 12, 6)
        mark = QtWidgets.QLabel("◢◤")
        mark.setStyleSheet(f"color: {th.ACCENT}; font-size: 14pt; border: 0;")
        title = QtWidgets.QLabel("EDS · ELEKTRONİK DESTEK — OPERATÖR KONSOLU")
        title.setObjectName("title")
        title.setStyleSheet("border: 0; font-size: 12pt; font-weight: bold; letter-spacing: 1px;")
        self.mode = QtWidgets.QLabel("KAYNAK: —")
        self.mode.setObjectName("badge")
        self.ep = QtWidgets.QLabel(f"bus: {endpoint}")
        self.ep.setStyleSheet(f"color: {th.MUTED}; font-family: '{th.FONT_MONO}'; font-size: 8pt; border: 0;")
        self.clock = QtWidgets.QLabel("--:--:--")
        self.clock.setStyleSheet(f"color: {th.TEXT}; font-family: '{th.FONT_MONO}'; font-size: 11pt; border: 0;")
        for wdg in (mark, title):
            lay.addWidget(wdg)
        lay.addSpacing(14)
        lay.addWidget(self.mode)
        lay.addStretch(1)
        lay.addWidget(self.ep)
        lay.addSpacing(14)
        lay.addWidget(self.clock)
        self.set_mode(None)

    def set_mode(self, backend: str | None) -> None:
        if backend == "sim":
            text, col = "SİMÜLASYON", th.WARN
        elif backend in ("soapy", "group", "proc"):
            text, col = "DONANIM", th.OK
        elif backend:
            text, col = backend.upper(), th.ACCENT
        else:
            text, col = "KAYNAK YOK", th.NONE
        self.mode.setText(text)
        self.mode.setStyleSheet(f"color: {th.BG}; background: {col}; border-radius: 4px; padding: 2px 8px;"
                                " font-weight: bold;")

    def tick(self) -> None:
        self.clock.setText(time.strftime("%H:%M:%S"))


class MainWindow(QtWidgets.QMainWindow):
    def __init__(self, endpoint: str, *, fps: float = 25.0, history: int = 300, hold_s: float = 15.0):
        super().__init__()
        self.setWindowTitle("EDS — Operatör Konsolu")
        self.resize(1680, 980)
        self.endpoint = endpoint
        self.client = BusClient(endpoint).start()
        self.tracker = EmitterTracker(hold_s=hold_s)
        self.latency = LatencyStats()
        self.frame: SpectrumFrame | None = None
        self.status: dict | None = None
        self.status_t: int | None = None
        self.bearing: dict | None = None
        self.bearing_t: int | None = None
        self.df_rms: str = "—"
        self.frames_drawn = 0
        self._fps_t0 = time.monotonic()
        self._fps_n = 0
        self.gui_fps = 0.0
        self._slow_t = 0.0

        self.header = Header(endpoint)
        self.setMenuWidget(self.header)
        self.spectrum = SpectrumPanel()
        self.waterfall = WaterfallPanel(history=history)
        self.waterfall.plot.setXLink(self.spectrum.plot)
        split = QtWidgets.QSplitter(Qt.Vertical)
        split.addWidget(self.spectrum)
        split.addWidget(self.waterfall)
        split.setSizes([520, 400])
        self.setCentralWidget(split)

        self.table = EmitterTable()
        self.params = ParamPanel()
        self.compass = DfCompass()
        self.d_table = self._dock("Emiterler", self.table)
        self.d_params = self._dock("Parametreler", self.params)
        self.compass.header = None
        self.d_df = self._dock("Yön bulma (DF)", self.compass)
        self.addDockWidget(Qt.RightDockWidgetArea, self.d_table)
        self.splitDockWidget(self.d_table, self.d_params, Qt.Vertical)
        self.splitDockWidget(self.d_params, self.d_df, Qt.Horizontal)
        self.resizeDocks([self.d_table], [880], Qt.Horizontal)
        self.resizeDocks([self.d_table, self.d_params], [470, 430], Qt.Vertical)
        self.table.selectionChanged.connect(lambda _tid: self._refresh_slow(force=True))

        self.strip = StatusStrip()
        sb = self.statusBar()
        sb.setSizeGripEnabled(False)
        sb.setStyleSheet(f"QStatusBar {{ background: {th.PANEL_2}; border-top: 1px solid {th.BORDER}; }}")
        sb.addWidget(self.strip, 1)

        self.timer = QtCore.QTimer(self)
        self.timer.timeout.connect(self._tick)
        self.timer.start(max(5, int(1000 / max(fps, 1.0))))

    def _dock(self, title: str, w: QtWidgets.QWidget) -> QtWidgets.QDockWidget:
        d = QtWidgets.QDockWidget(title, self)
        d.setObjectName(title)
        d.setWidget(w)
        d.setFeatures(QtWidgets.QDockWidget.DockWidgetMovable | QtWidgets.QDockWidget.DockWidgetFloatable)
        hdr = getattr(w, "header", None)
        if hdr is not None:                      # the panel's own header doubles as the dock's drag bar
            w.layout().removeWidget(hdr)
            d.setTitleBarWidget(hdr)
        return d

    # ------------------------------------------------------------------ data flow
    def _tick(self) -> None:
        self.apply(self.client.take())

    def apply(self, snap) -> None:
        """Consume one :class:`~eds.gui.busclient.Snapshot` (called by the timer; tests inject)."""
        for m in snap.events:
            if m.topic == RX_DETECTIONS:
                self.tracker.update(m.data, m.t_recv_ns)
                self.latency.add(m.t_recv_ns, m.data.get("t_acq_end_ns"))
            elif m.topic == RX_STATUS:
                self.status, self.status_t = m.data, m.t_recv_ns
            elif m.topic == DF_BEARING:
                self.bearing, self.bearing_t = m.data, m.t_recv_ns
            elif m.topic == MEAS_RESULT and m.data.get("spec_id") == "df_rms":
                v = m.data.get("value")
                self.df_rms = f"{v:.2f}°" if isinstance(v, (int, float)) else "—"
        now = now_ns()
        if snap.spectrum is not None:
            try:
                self.frame = SpectrumFrame.from_data(snap.spectrum.data)
            except (KeyError, TypeError, ValueError):
                self.frame = None
            if self.frame is not None and self.frame.n > 1:
                stale = {t.id for t in self.tracker.tracks() if self.tracker.is_stale(t, now)}
                self.spectrum.update_frame(self.frame, self.tracker.tracks(), self.table.selected, stale)
                self.waterfall.push(self.frame, time.monotonic())
                self.frames_drawn += 1
                self._fps_n += 1
        elif self.client.seconds_since(RX_SPECTRUM) > 2.0:
            why = f" ({self.client.error})" if self.client.error else ""
            self.spectrum.show_waiting(f"Spektrum verisi bekleniyor… {self.endpoint}{why}")
        t = time.monotonic()
        if t - self._fps_t0 >= 1.0:
            self.gui_fps = self._fps_n / (t - self._fps_t0)
            self._fps_t0, self._fps_n = t, 0
        if t - self._slow_t >= 0.25:
            self._refresh_slow()

    def _refresh_slow(self, force: bool = False) -> None:
        self._slow_t = time.monotonic()
        now = now_ns()
        self.tracker.prune(now)
        tracks = self.tracker.tracks()
        stale = {x.id for x in tracks if self.tracker.is_stale(x, now)}
        hv = health_view(self.status, self.status_t, now)
        gain_state = hv.gain_text.split(" (")[0] if hv.connected else "—"
        cal = self.frame.cal_mode if self.frame is not None else "—"
        self.table.update_rows(tracks, now, stale, f"{gain_state} · {'dBm' if 'dBm' in cal else 'dBFS'}")
        sel = self.tracker.get(self.table.selected) if self.table.selected else None
        self.params.show_track(sel, now, sel is not None and sel.id in stale)
        b_age = (now - self.bearing_t) / 1e9 if self.bearing_t else math.inf
        self.compass.set_state(self.bearing, b_age, self.df_rms)
        backend = (self.status or {}).get("device", {}).get("backend") if hv.connected else None
        self.header.set_mode(backend)
        self.header.tick()
        bus_ok = self.client.running and not self.client.error and self.client.seconds_since(RX_STATUS) < 3.0
        bus_text = (f"bağlı · {self.gui_fps:.0f} fps" if bus_ok else
                    ("HATA: " + self.client.error[:40] if self.client.error else "veri yok"))
        self.strip.update_view(hv, latency_ms=(self.latency.median(), self.latency.p95()), cal_mode=cal,
                               bus_text=bus_text, bus_ok=bus_ok)

    def closeEvent(self, ev: QtGui.QCloseEvent) -> None:  # noqa: N802
        self.timer.stop()
        self.client.stop()
        super().closeEvent(ev)


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="python -m eds.gui",
                                description="EDS operatör konsolu (ZeroMQ bus abonesi; SoapySDR/libiio gerektirmez).")
    p.add_argument("--connect", metavar="UÇ", help=f"abone olunacak bus uç noktası (vars.: {DEFAULT_ENDPOINT}; "
                                                   "proxy modunda proxy_backend, ör. tcp://127.0.0.1:5551)")
    p.add_argument("-c", "--config", metavar="DOSYA", help="uç noktayı bu konfigürasyonun [bus] bölümünden al")
    p.add_argument("--fps", type=float, default=25.0, help="ekran yenileme hızı (vars.: 25)")
    p.add_argument("--history", type=int, default=300, help="waterfall geçmiş satır sayısı (vars.: 300)")
    p.add_argument("--hold", type=float, default=15.0, help="görülmeyen emiterin tablodan silinme süresi [s]")
    p.add_argument("--screenshot", metavar="PNG", help="belirtilen süre sonunda pencerenin görüntüsünü kaydet ve çık")
    p.add_argument("--after", type=float, default=8.0, help="--screenshot / --duration için bekleme [s]")
    p.add_argument("--duration", type=float, default=0.0, help="bu kadar saniye sonra kapan (0 = kapanma)")
    return p


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(list(argv) if argv is not None else None)
    endpoint = args.connect or endpoint_from_config(args.config)
    app = QtWidgets.QApplication.instance() or QtWidgets.QApplication(sys.argv[:1])
    app.setApplicationName("EDS Operatör Konsolu")
    th.apply(app)
    win = MainWindow(endpoint, fps=args.fps, history=args.history, hold_s=args.hold)
    win.show()
    signal.signal(signal.SIGINT, lambda *_: app.quit())          # Ctrl-C in the terminal closes the GUI
    keepalive = QtCore.QTimer()                                  # let Python see signals while Qt runs
    keepalive.timeout.connect(lambda: None)
    keepalive.start(200)
    if args.screenshot:
        def _shot() -> None:
            out = Path(args.screenshot)
            out.parent.mkdir(parents=True, exist_ok=True)
            win.grab().save(str(out))
            print(f"Ekran görüntüsü: {out}")
            app.quit()
        QtCore.QTimer.singleShot(int(args.after * 1000), _shot)
    elif args.duration > 0:
        QtCore.QTimer.singleShot(int(args.duration * 1000), app.quit)
    rc = app.exec()
    win.client.stop()
    return int(rc)
