"""Operator-console panels (PySide6 + pyqtgraph).

Every panel is passive: the main window feeds it model objects (:mod:`eds.gui.model`)
from a GUI-thread timer; no panel touches the bus.
"""

from __future__ import annotations

import math
from typing import Any

import numpy as np
import pyqtgraph as pg
from PySide6 import QtCore, QtGui, QtWidgets

from . import theme as th
from .model import BW_CLASSES, HealthView, SpectrumFrame, Track, fmt_bw, fmt_level

Qt = QtCore.Qt


def _style_plot(pw: pg.PlotWidget) -> None:
    pi = pw.getPlotItem()
    pi.showGrid(x=True, y=True, alpha=0.18)
    for name in ("left", "bottom"):
        ax = pi.getAxis(name)
        ax.setPen(pg.mkPen(th.BORDER))
        ax.setTextPen(pg.mkPen(th.MUTED))
        ax.setStyle(tickFont=QtGui.QFont(th.FONT_MONO, 8))
    pi.getViewBox().setBackgroundColor(th.BG)
    pi.setMenuEnabled(False)
    pi.hideButtons()


def _header(title: str, right: QtWidgets.QWidget | None = None) -> QtWidgets.QWidget:
    w = QtWidgets.QWidget()
    w.setStyleSheet(f"background: {th.PANEL_2}; border-bottom: 1px solid {th.BORDER};")
    lay = QtWidgets.QHBoxLayout(w)
    lay.setContentsMargins(10, 4, 10, 4)
    lab = QtWidgets.QLabel(title)
    lab.setStyleSheet(f"color: {th.TEXT}; font-weight: bold; background: transparent; border: 0;")
    lay.addWidget(lab)
    lay.addStretch(1)
    if right is not None:
        lay.addWidget(right)
    return w


def bw_legend() -> QtWidgets.QWidget:
    w = QtWidgets.QWidget()
    w.setStyleSheet("background: transparent; border: 0;")
    lay = QtWidgets.QHBoxLayout(w)
    lay.setContentsMargins(0, 0, 0, 0)
    lay.setSpacing(12)
    for c in BW_CLASSES:
        lab = QtWidgets.QLabel(f'<span style="color:{c.color}">■</span> {c.label}')
        lab.setStyleSheet(f"color: {th.MUTED}; font-size: 8pt; background: transparent; border: 0;")
        lay.addWidget(lab)
    return w


# ============================================================================ spectrum
class SpectrumPanel(QtWidgets.QWidget):
    """Live PSD (``rx.spectrum``) with detected-emitter overlays coloured by BW class."""

    def __init__(self, parent: QtWidgets.QWidget | None = None):
        super().__init__(parent)
        self.cal_label = QtWidgets.QLabel("—")
        self.cal_label.setStyleSheet(f"color: {th.MUTED}; font-family: '{th.FONT_MONO}'; font-size: 8pt;"
                                     " background: transparent; border: 0;")
        right = QtWidgets.QWidget()
        right.setStyleSheet("background: transparent; border: 0;")
        rl = QtWidgets.QHBoxLayout(right)
        rl.setContentsMargins(0, 0, 0, 0)
        rl.setSpacing(18)
        rl.addWidget(bw_legend())
        rl.addWidget(self.cal_label)
        self.plot = pg.PlotWidget()
        _style_plot(self.plot)
        self.plot.setLabel("bottom", "Frekans", units="MHz")
        self.plot.getAxis("bottom").enableAutoSIPrefix(False)
        self.plot.setLabel("left", "Seviye [dBFS]")
        # 1-px cosmetic pens: wider pens take Qt's slow stroking path (×5 repaint time)
        self.hold_curve = self.plot.plot(pen=pg.mkPen(th.HOLD, width=1))
        self.curve = self.plot.plot(pen=pg.mkPen(th.TRACE, width=1))
        for c in (self.hold_curve, self.curve):
            c.setDownsampling(auto=True, method="peak")      # peak-preserving decimation to screen width
            c.setClipToView(True)
        self.noise_line = pg.InfiniteLine(angle=0, pen=pg.mkPen(th.NOISE, width=1, style=Qt.DashLine))
        self.plot.addItem(self.noise_line)
        self.noise_text = pg.TextItem("", color=th.NOISE, anchor=(0, 1))
        self.plot.addItem(self.noise_text)
        self.no_data = pg.TextItem("Spektrum verisi bekleniyor…", color=th.MUTED, anchor=(0.5, 0.5))
        self.plot.addItem(self.no_data)
        self._regions: list[pg.LinearRegionItem] = []
        self._labels: list[pg.TextItem] = []
        self._hold: np.ndarray | None = None
        self._geom: tuple | None = None
        self._yrange: tuple[float, float] | None = None
        self.plot.setXRange(-0.5, 0.5, padding=0)
        self.plot.setYRange(-120.0, -20.0, padding=0)
        self.no_data.setPos(0.0, -70.0)
        self.header = _header("Canlı spektrum", right)
        lay = QtWidgets.QVBoxLayout(self)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.setSpacing(0)
        lay.addWidget(self.header)
        lay.addWidget(self.plot, 1)

    def _ensure_overlays(self, n: int) -> None:
        while len(self._regions) < n:
            r = pg.LinearRegionItem(values=(0, 0), movable=False)
            r.setZValue(-5)
            self.plot.addItem(r)
            t = pg.TextItem("", anchor=(0.5, 1.0))
            t.setFont(QtGui.QFont(th.FONT_MONO, 8))
            self.plot.addItem(t)
            self._regions.append(r)
            self._labels.append(t)

    def update_frame(self, f: SpectrumFrame, tracks: list[Track], selected: str | None, stale_ids: set[str]) -> None:
        self.no_data.setVisible(False)
        x = (f.f_start_hz + f.f_step_hz * np.arange(f.n)) / 1e6
        y = np.asarray(f.values, dtype=float)
        if f.geometry_key() != self._geom:
            self._geom = f.geometry_key()
            self._hold = None
            self._yrange = None
            self.plot.setXRange(x[0], x[-1], padding=0)
        self._hold = y.copy() if self._hold is None else np.maximum(self._hold * 1.0 - 0.15, y)
        self.curve.setData(x, y)
        self.hold_curve.setData(x, self._hold)
        unit = f.unit
        self.plot.setLabel("left", f"Seviye [{unit}]" if f.calibrated else f"Seviye [{unit}] (kalibrasyonsuz)")
        self.cal_label.setText(f"{f.cal_mode} · RBW {f.rbw_hz:.0f} Hz · ×{f.decimation} seyreltme")
        nf = f.noise_floor
        if math.isfinite(nf):
            self.noise_line.setPos(nf)
            self.noise_text.setText(f"gürültü {nf:.1f}")
            self.noise_text.setPos(x[0], nf)
        top = float(np.nanmax(y))
        lo = (nf if math.isfinite(nf) else float(np.nanmin(y))) - 15.0
        hi = max(top + 12.0, lo + 40.0)
        if self._yrange is None:
            self._yrange = (lo, hi)
        else:   # smooth to avoid jitter; react fast to new strong signals
            a = 0.15
            self._yrange = (self._yrange[0] + a * (lo - self._yrange[0]),
                            hi if hi > self._yrange[1] else self._yrange[1] + a * (hi - self._yrange[1]))
        self.plot.setYRange(*self._yrange, padding=0)
        vis = [t for t in tracks if x[0] * 1e6 <= t.f_center_hz <= x[-1] * 1e6]
        self._ensure_overlays(len(vis))
        for i, t in enumerate(vis):
            stale = t.id in stale_ids
            col = th.DIM if stale else t.bw_class.color
            alpha = 25 if stale else (90 if t.id == selected else 45)
            r = self._regions[i]
            lo_mhz, hi_mhz = t.f_lo_hz / 1e6, t.f_hi_hz / 1e6
            if hi_mhz - lo_mhz < 3 * f.f_step_hz / 1e6:          # keep narrow emitters visible
                c = t.f_center_hz / 1e6
                lo_mhz, hi_mhz = c - 1.5 * f.f_step_hz / 1e6, c + 1.5 * f.f_step_hz / 1e6
            r.setRegion((lo_mhz, hi_mhz))
            r.setBrush(pg.mkBrush(*th.hex_rgba(col, alpha)))
            for ln in r.lines:
                ln.setPen(pg.mkPen(col, width=2 if t.id == selected else 1))
            r.setVisible(True)
            lab = self._labels[i]
            lab.setText(t.id + (" imaj?" if t.image_of else ""))
            lab.setColor(pg.mkColor(th.DIM if t.image_of else col))
            lab.setPos(t.f_center_hz / 1e6, (t.peak_db if math.isfinite(t.peak_db) else top) + 1.5)
            lab.setVisible(True)
        for j in range(len(vis), len(self._regions)):
            self._regions[j].setVisible(False)
            self._labels[j].setVisible(False)

    def show_waiting(self, text: str) -> None:
        self.no_data.setText(text)
        self.no_data.setVisible(True)
        vr = self.plot.getViewBox().viewRange()
        self.no_data.setPos((vr[0][0] + vr[0][1]) / 2, (vr[1][0] + vr[1][1]) / 2)


# ============================================================================ waterfall
class WaterfallPanel(QtWidgets.QWidget):
    """Rolling spectrogram (newest line on top). History length = ``history`` frames."""

    def __init__(self, history: int = 300, parent: QtWidgets.QWidget | None = None):
        super().__init__(parent)
        self.history = int(history)
        self.plot = pg.PlotWidget()
        _style_plot(self.plot)
        self.plot.setLabel("bottom", "Frekans", units="MHz")
        self.plot.getAxis("bottom").enableAutoSIPrefix(False)
        self.plot.setLabel("left", "Geçmiş [s]")
        self.plot.getAxis("left").enableAutoSIPrefix(False)
        self.plot.setXRange(-0.5, 0.5, padding=0)
        self.plot.setYRange(0.0, 10.0, padding=0)
        self.img = pg.ImageItem()
        self.img.setColorMap(pg.colormap.get("inferno"))
        self.img.setAutoDownsample(True)
        self.plot.addItem(self.img)
        self.plot.getViewBox().invertY(True)       # y = seconds ago: 0 (newest) at the top
        self._buf: np.ndarray | None = None
        self._count = 0
        self._geom: tuple | None = None
        self._levels: tuple[float, float] | None = None
        self._t_last: float | None = None
        self._dt = 0.05
        self.span_label = QtWidgets.QLabel("—")
        self.span_label.setStyleSheet(f"color: {th.MUTED}; font-size: 8pt; background: transparent; border: 0;")
        lay = QtWidgets.QVBoxLayout(self)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.setSpacing(0)
        self.header = _header("Waterfall (spektrogram)", self.span_label)
        lay.addWidget(self.header)
        lay.addWidget(self.plot, 1)

    def push(self, f: SpectrumFrame, t_mono: float) -> None:
        y = np.asarray(f.values, dtype=np.float32)
        if f.geometry_key() != self._geom or self._buf is None:
            self._geom = f.geometry_key()
            self._buf = np.full((self.history, f.n), np.nan, dtype=np.float32)
            self._count = 0
            self._levels = None
        if self._t_last is not None:
            self._dt = 0.9 * self._dt + 0.1 * min(max(t_mono - self._t_last, 1e-3), 2.0)
        self._t_last = t_mono
        self._buf[1:] = self._buf[:-1]
        self._buf[0] = y
        self._count = min(self._count + 1, self.history)
        nf = f.noise_floor if math.isfinite(f.noise_floor) else float(np.nanmedian(y))
        lo, hi = nf - 3.0, max(float(np.nanmax(y)), nf + 30.0)
        self._levels = (lo, hi) if self._levels is None else (0.9 * self._levels[0] + 0.1 * lo,
                                                               0.9 * self._levels[1] + 0.1 * hi)
        img = np.nan_to_num(self._buf, nan=self._levels[0])
        self.img.setImage(img, autoLevels=False, levels=self._levels)
        x0 = f.f_start_hz / 1e6
        w = f.f_step_hz * f.n / 1e6
        self.img.setRect(QtCore.QRectF(x0, 0.0, w, self.history * self._dt))
        self.plot.setYRange(0.0, max(self._count, 2) * self._dt, padding=0)
        self.span_label.setText(f"{self._count} satır · {self._count * self._dt:.1f} s geçmiş · "
                                f"renk {self._levels[0]:.0f}…{self._levels[1]:.0f} {f.unit}")


# ============================================================================ emitter table
COLUMNS = ("ID", "Frekans [MHz]", "BW", "Güç", "SNR [dB]", "Mod.", "A/S", "Güven", "Son gör.",
           "Kazanç · Kal.")
COLUMN_TIPS = ("Emiter kimliği (GUI izleyicisi atar); '⁎' = olası IQ imajı (sıfır-IF aynası)", "Merkez frekans", "İşgal edilen bant genişliği (−3 dB kenarlar)",
               "Tepe seviyesi; '~' = kalibrasyonsuz (göreli) seviye", "Sinyal/gürültü oranı (tepe − gürültü tabanı)",
               "Modülasyon sınıfı — sınıflandırıcı henüz yok", "Analog / sayısal — sınıflandırıcı henüz yok",
               "Sınıflandırma güveni — sınıflandırıcı henüz yok",
               "Son tespitten bu yana geçen süre ('eski' = birkaç saniyedir görülmüyor)",
               "Alıcı kazancı ve seviye birimi (dBm = kalibreli, dBFS = kalibrasyonsuz)")


class EmitterTableModel(QtCore.QAbstractTableModel):
    def __init__(self, parent: QtCore.QObject | None = None):
        super().__init__(parent)
        self._rows: list[Track] = []
        self._now = 0
        self._stale: set[str] = set()
        self._gain_state = "—"
        self._mono = QtGui.QFont(th.FONT_MONO, 9)

    def set_rows(self, rows: list[Track], now_ns: int, stale: set[str], gain_state: str) -> None:
        self.beginResetModel()
        self._rows, self._now, self._stale, self._gain_state = list(rows), now_ns, set(stale), gain_state
        self.endResetModel()

    def rowCount(self, parent: QtCore.QModelIndex = QtCore.QModelIndex()) -> int:  # noqa: B008, N802
        return 0 if parent.isValid() else len(self._rows)

    def columnCount(self, parent: QtCore.QModelIndex = QtCore.QModelIndex()) -> int:  # noqa: B008, N802
        return len(COLUMNS)

    def headerData(self, section: int, orientation: Qt.Orientation, role: int = Qt.DisplayRole) -> Any:  # noqa: N802
        if orientation == Qt.Horizontal and role == Qt.DisplayRole:
            return COLUMNS[section]
        if orientation == Qt.Horizontal and role == Qt.ToolTipRole:
            return COLUMN_TIPS[section]
        return None

    def track_at(self, row: int) -> Track | None:
        return self._rows[row] if 0 <= row < len(self._rows) else None

    def row_of(self, tid: str) -> int:
        for i, t in enumerate(self._rows):
            if t.id == tid:
                return i
        return -1

    def data(self, index: QtCore.QModelIndex, role: int = Qt.DisplayRole) -> Any:
        t = self.track_at(index.row())
        if t is None:
            return None
        c = index.column()
        stale = t.id in self._stale
        age = t.age_s(self._now)
        if role == Qt.DisplayRole:
            return (t.id + (" ⁎" if t.image_of else ""), f"{t.f_center_hz / 1e6:.4f}", fmt_bw(t.bw_hz), fmt_level(t.peak_db, t.unit, t.calibrated),
                    f"{t.snr_db:.1f}", "—", "—", "—", f"{age:.1f} s" + (" (eski)" if stale else ""),
                    self._gain_state)[c]
        if role == Qt.UserRole:     # numeric sort keys
            return (int(t.id[1:]), t.f_center_hz, t.bw_hz, t.peak_db, t.snr_db, "", "", "", age, "")[c]
        if role == Qt.ForegroundRole:
            if stale or (t.image_of and c != 0):
                return QtGui.QBrush(QtGui.QColor(th.DIM))
            if c in (0, 2):
                return QtGui.QBrush(QtGui.QColor(t.bw_class.color))
            if c == 3 and not t.calibrated:
                return QtGui.QBrush(QtGui.QColor(th.MUTED))
            if c in (5, 6, 7):
                return QtGui.QBrush(QtGui.QColor(th.DIM))
            return None
        if role == Qt.DecorationRole and c == 0:
            pm = QtGui.QPixmap(10, 10)
            pm.fill(QtGui.QColor(th.DIM if stale else t.bw_class.color))
            return pm
        if role == Qt.TextAlignmentRole and c in (1, 3, 4, 8):
            return int(Qt.AlignRight | Qt.AlignVCenter)
        if role == Qt.ToolTipRole:
            if t.image_of:
                return (f"Olası IQ imajı: {t.image_of} emiterinin LO'ya göre aynası, {t.image_delta_db:.0f} dB daha "
                        "zayıf. Sıfır-IF alıcının artefaktı olabilir; gerçek verici olduğunu varsaymayın.")
            if c == 3 and not t.calibrated:
                return "Kalibrasyonsuz (göreli) seviye: '~' ile işaretli. Mutlak dBm için güç kalibrasyonu gerekir."
            if c == 2:
                return t.bw_class.label
        if role == Qt.FontRole:
            return self._mono
        return None


class EmitterTable(QtWidgets.QWidget):
    selectionChanged = QtCore.Signal(object)   # Track id (str) or None

    def __init__(self, parent: QtWidgets.QWidget | None = None):
        super().__init__(parent)
        self.model = EmitterTableModel(self)
        self.proxy = QtCore.QSortFilterProxyModel(self)
        self.proxy.setSourceModel(self.model)
        self.proxy.setSortRole(Qt.UserRole)
        self.view = QtWidgets.QTableView()
        self.view.setModel(self.proxy)
        self.view.setSortingEnabled(True)
        self.view.sortByColumn(1, Qt.AscendingOrder)
        self.view.setSelectionBehavior(QtWidgets.QAbstractItemView.SelectRows)
        self.view.setSelectionMode(QtWidgets.QAbstractItemView.SingleSelection)
        self.view.setAlternatingRowColors(True)
        self.view.verticalHeader().setVisible(False)
        self.view.verticalHeader().setDefaultSectionSize(22)
        hh = self.view.horizontalHeader()
        hh.setStretchLastSection(True)
        fm = QtGui.QFontMetrics(QtGui.QFont(th.FONT_UI, 9, QtGui.QFont.Bold))
        for i, name in enumerate(COLUMNS[:-1]):
            self.view.setColumnWidth(i, max(fm.horizontalAdvance(name) + 20, (74, 0, 80, 104, 0, 50, 42, 0, 70)[i]))
        self.count_label = QtWidgets.QLabel("0 emiter")
        self.count_label.setStyleSheet(f"color: {th.MUTED}; font-size: 8pt; background: transparent; border: 0;")
        lay = QtWidgets.QVBoxLayout(self)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.setSpacing(0)
        self.header = _header("Emiterler", self.count_label)
        lay.addWidget(self.header)
        lay.addWidget(self.view, 1)
        self.selected: str | None = None
        self.view.selectionModel().selectionChanged.connect(self._on_sel)

    def _on_sel(self, *_: Any) -> None:
        idx = self.view.selectionModel().selectedRows()
        tid = None
        if idx:
            t = self.model.track_at(self.proxy.mapToSource(idx[0]).row())
            tid = t.id if t else None
        if tid != self.selected:
            self.selected = tid
            self.selectionChanged.emit(tid)

    def update_rows(self, rows: list[Track], now_ns: int, stale: set[str], gain_state: str) -> None:
        keep = self.selected
        sm = self.view.selectionModel()
        sm.blockSignals(True)
        self.model.set_rows(rows, now_ns, stale, gain_state)
        if keep is not None:
            r = self.model.row_of(keep)
            if r >= 0:
                pidx = self.proxy.mapFromSource(self.model.index(r, 0))
                sm.select(pidx, QtCore.QItemSelectionModel.ClearAndSelect | QtCore.QItemSelectionModel.Rows)
            else:
                self.selected = None
        sm.blockSignals(False)
        n_live = sum(1 for t in rows if t.id not in stale)
        self.count_label.setText(f"{len(rows)} emiter · {n_live} aktif · başlığa tıklayıp sıralayın")

    def select_id(self, tid: str) -> None:
        r = self.model.row_of(tid)
        if r >= 0:
            self.view.selectRow(self.proxy.mapFromSource(self.model.index(r, 0)).row())


# ============================================================================ parameters
class ParamPanel(QtWidgets.QWidget):
    def __init__(self, parent: QtWidgets.QWidget | None = None):
        super().__init__(parent)
        self.title = QtWidgets.QLabel("—")
        self.title.setStyleSheet(f"color: {th.MUTED}; font-size: 8pt; background: transparent; border: 0;")
        self.table = QtWidgets.QTableWidget(0, 2)
        self.table.setHorizontalHeaderLabels(["Parametre", "Değer"])
        self.table.verticalHeader().setVisible(False)
        self.table.horizontalHeader().setStretchLastSection(True)
        self.table.setColumnWidth(0, 150)
        self.table.setEditTriggers(QtWidgets.QAbstractItemView.NoEditTriggers)
        self.table.setSelectionMode(QtWidgets.QAbstractItemView.NoSelection)
        self.table.verticalHeader().setDefaultSectionSize(20)
        self.placeholder = QtWidgets.QLabel("Emiter tablosundan bir satır seçin:\nseçilen emiterin çıkarılan "
                                            "tüm parametreleri burada görünür.")
        self.placeholder.setAlignment(Qt.AlignCenter)
        self.placeholder.setStyleSheet(f"color: {th.MUTED}; padding: 16px;")
        lay = QtWidgets.QVBoxLayout(self)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.setSpacing(0)
        self.header = _header("Parametreler", self.title)
        lay.addWidget(self.header)
        lay.addWidget(self.placeholder, 1)
        lay.addWidget(self.table, 1)
        self.table.setVisible(False)

    def show_track(self, t: Track | None, now_ns: int, stale: bool) -> None:
        if t is None:
            self.table.setVisible(False)
            self.placeholder.setVisible(True)
            self.title.setText("—")
            return
        self.placeholder.setVisible(False)
        self.table.setVisible(True)
        rows = t.params(now_ns)
        self.table.setRowCount(len(rows))
        col = QtGui.QColor(th.DIM if stale else th.TEXT)
        for i, (k, v) in enumerate(rows):
            ki = QtWidgets.QTableWidgetItem(k)
            ki.setForeground(QtGui.QBrush(QtGui.QColor(th.MUTED)))
            vi = QtWidgets.QTableWidgetItem(v)
            vi.setForeground(QtGui.QBrush(col if not (k == "BW sınıfı") else QtGui.QColor(t.bw_class.color)))
            self.table.setItem(i, 0, ki)
            self.table.setItem(i, 1, vi)
        self.title.setText(f"{t.id}" + (" · eski (artık görülmüyor)" if stale else ""))


# ============================================================================ DF compass
class DfCompass(QtWidgets.QWidget):
    """Bearing display for ``df.bearing``. 0° = array broadside (up), +az toward element 1."""

    def __init__(self, parent: QtWidgets.QWidget | None = None):
        super().__init__(parent)
        self.setMinimumSize(240, 240)
        self.bearing: dict[str, Any] | None = None
        self.age_s = math.inf
        self.rms_text = "—"

    def set_state(self, bearing: dict[str, Any] | None, age_s: float, rms_text: str) -> None:
        self.bearing, self.age_s, self.rms_text = bearing, age_s, rms_text
        self.update()

    def paintEvent(self, _ev: QtGui.QPaintEvent) -> None:  # noqa: N802
        p = QtGui.QPainter(self)
        p.setRenderHint(QtGui.QPainter.Antialiasing)
        p.fillRect(self.rect(), QtGui.QColor(th.PANEL))
        w, h = self.width(), self.height()
        text_h = 58
        r = max(20.0, min(w, h - text_h) / 2 - 28)
        cx, cy = w / 2, (h - text_h) / 2 + 4
        p.setPen(QtGui.QPen(QtGui.QColor(th.BORDER), 1.5))
        p.setBrush(QtGui.QColor(th.BG))
        p.drawEllipse(QtCore.QPointF(cx, cy), r, r)
        p.setPen(QtGui.QPen(QtGui.QColor(th.GRID), 1))
        p.drawEllipse(QtCore.QPointF(cx, cy), r * 0.66, r * 0.66)
        p.drawEllipse(QtCore.QPointF(cx, cy), r * 0.33, r * 0.33)
        p.setFont(QtGui.QFont(th.FONT_MONO, 7))
        for deg in range(0, 360, 30):
            a = math.radians(deg)
            x1, y1 = cx + r * math.sin(a), cy - r * math.cos(a)
            x0, y0 = cx + (r - 7) * math.sin(a), cy - (r - 7) * math.cos(a)
            p.setPen(QtGui.QPen(QtGui.QColor(th.MUTED), 1))
            p.drawLine(QtCore.QPointF(x0, y0), QtCore.QPointF(x1, y1))
            lab = {0: "0°", 90: "+90°", 180: "180°", 270: "−90°"}.get(deg)
            if lab:
                tx, ty = cx + (r + 11) * math.sin(a), cy - (r + 11) * math.cos(a)
                p.drawText(QtCore.QRectF(tx - 20, ty - 7, 40, 14), Qt.AlignCenter, lab)
        fresh = self.bearing is not None and self.age_s <= 5.0
        p.setFont(QtGui.QFont(th.FONT_UI, 9))
        if not fresh:
            p.setPen(QtGui.QColor(th.MUTED))
            msg = "DF verisi yok" if self.bearing is None else f"DF verisi eski ({self.age_s:.0f} s)"
            p.drawText(QtCore.QRectF(cx - r, cy - 10, 2 * r, 20), Qt.AlignCenter, msg)
            p.drawText(QtCore.QRectF(0, h - text_h, w, 20), Qt.AlignCenter,
                       "df.bearing bekleniyor · RMS: " + self.rms_text)
            p.end()
            return
        b = self.bearing
        az = float(b.get("az_deg", 0.0))
        for amb in b.get("ambiguous_deg") or []:
            self._needle(p, cx, cy, r * 0.9, float(amb), th.MUTED, 1.2, dashed=True)
        self._needle(p, cx, cy, r * 0.94, 180.0 - az, th.DIM, 1.0, dashed=True)     # front/back mirror
        self._needle(p, cx, cy, r * 0.94, az, th.ACCENT, 3.0)
        p.setBrush(QtGui.QColor(th.ACCENT))
        p.drawEllipse(QtCore.QPointF(cx, cy), 3, 3)
        p.setPen(QtGui.QColor(th.TEXT))
        p.setFont(QtGui.QFont(th.FONT_MONO, 10, QtGui.QFont.Bold))
        p.drawText(QtCore.QRectF(0, h - text_h, w, 20), Qt.AlignCenter,
                   f"Azimut {az:+.1f}°  ·  RMS {self.rms_text}")
        p.setFont(QtGui.QFont(th.FONT_MONO, 8))
        p.setPen(QtGui.QColor(th.MUTED))
        cal = "kalibreli" if b.get("calibrated") else "kalibrasyonsuz"
        p.drawText(QtCore.QRectF(0, h - text_h + 20, w, 18), Qt.AlignCenter,
                   f"{float(b.get('f_hz', 0)) / 1e6:.3f} MHz · kalite {float(b.get('quality', 0)):.2f} · "
                   f"koherens {float(b.get('coherence', 0)):.2f} · {cal}")
        p.drawText(QtCore.QRectF(0, h - text_h + 36, w, 18), Qt.AlignCenter,
                   f"{b.get('method', '')} · {self.age_s:.1f} s önce · kesikli: belirsiz/ön-arka ayna")
        p.end()

    @staticmethod
    def _needle(p: QtGui.QPainter, cx: float, cy: float, r: float, deg: float, color: str, width: float,
                dashed: bool = False) -> None:
        a = math.radians(deg)
        pen = QtGui.QPen(QtGui.QColor(color), width)
        if dashed:
            pen.setStyle(Qt.DashLine)
        p.setPen(pen)
        p.drawLine(QtCore.QPointF(cx, cy), QtCore.QPointF(cx + r * math.sin(a), cy - r * math.cos(a)))


# ============================================================================ status bar
class StatusStrip(QtWidgets.QWidget):
    """System health chips from ``rx.status`` + GUI-side latency / bus state."""

    KEYS = ("Durum", "Cihaz", "Merkez", "Örnekleme", "Kazanç", "Overflow", "Akış", "Gecikme", "Kal. modu",
            "PPM", "Sıcaklık", "Uyarılar", "Bus")

    def __init__(self, parent: QtWidgets.QWidget | None = None):
        super().__init__(parent)
        self.setStyleSheet(f"background: {th.PANEL_2}; border-top: 1px solid {th.BORDER};")
        lay = QtWidgets.QHBoxLayout(self)
        lay.setContentsMargins(8, 4, 8, 4)
        lay.setSpacing(6)
        self.lamp = QtWidgets.QLabel()
        self.lamp.setFixedSize(14, 14)
        lay.addWidget(self.lamp)
        self.chips: dict[str, QtWidgets.QLabel] = {}
        for k in self.KEYS:
            box = QtWidgets.QWidget()
            box.setStyleSheet("background: transparent; border: 0;")
            bl = QtWidgets.QVBoxLayout(box)
            bl.setContentsMargins(0, 0, 0, 0)
            bl.setSpacing(0)
            key = QtWidgets.QLabel(k)
            key.setObjectName("chipKey")
            key.setStyleSheet(f"color: {th.MUTED}; font-size: 7pt; background: transparent; border: 0;")
            val = QtWidgets.QLabel("—")
            val.setObjectName("chip")
            bl.addWidget(key)
            bl.addWidget(val)
            lay.addWidget(box)
            self.chips[k] = val
        lay.addStretch(1)

    def _set(self, key: str, text: str, level: str | None = None, tip: str | None = None) -> None:
        lab = self.chips[key]
        lab.setText(text)
        if level in ("bad", "warn", "ok"):
            col = th.LEVEL_COLORS[level]
            lab.setStyleSheet(f"color: {col}; border: 1px solid {col};" if level != "ok" else f"color: {col};")
        else:
            lab.setStyleSheet("")
        if tip is not None:
            lab.setToolTip(tip)

    def update_view(self, hv: HealthView, *, latency_ms: tuple[float, float], cal_mode: str, bus_text: str,
                    bus_ok: bool) -> None:
        col = th.LEVEL_COLORS.get(hv.level, th.NONE)
        self.lamp.setStyleSheet(f"background: {col}; border-radius: 7px; border: 1px solid {th.BORDER};")
        state_tr = {"streaming": "akış", "sweeping": "tarama", "idle": "boşta", "error": "HATA"}.get(hv.state, hv.state)
        self._set("Durum", ("● " if hv.connected else "○ ") + state_tr if hv.state != "—" else "veri yok",
                  hv.level if hv.level != "none" else None)
        self._set("Cihaz", hv.device or "—")
        self._set("Merkez", f"{hv.fc_hz / 1e6:.3f} MHz" if math.isfinite(hv.fc_hz) else "—")
        self._set("Örnekleme", f"{hv.fs_hz / 1e6:g} MS/s" if math.isfinite(hv.fs_hz) else "—")
        self._set("Kazanç", hv.gain_text)
        self._set("Overflow", hv.overflow_text, "bad" if hv.overflow_alarm else ("ok" if hv.connected else None),
                  "RX DMA/sürücü taşmaları ve GUI'ye gelemeden düşen bloklar (host drop).")
        self._set("Akış", hv.throughput_text)
        med, p95 = latency_ms
        lat = f"{med:.0f} ms (p95 {p95:.0f})" if math.isfinite(med) else "—"
        self._set("Gecikme", lat, "warn" if math.isfinite(p95) and p95 > 200 else None,
                  "Son örneğin yakalanmasından tespit mesajının GUI'ye ulaşmasına (aynı bilgisayarın saati).")
        self._set("Kal. modu", cal_mode, "warn" if "kalibrasyonsuz" in cal_mode else None)
        self._set("PPM", hv.ppm_text, "warn" if hv.ppm_text.startswith("yok") else None)
        self._set("Sıcaklık", hv.temperature_text)
        n = len(hv.warnings)
        self._set("Uyarılar", str(n), "warn" if n else None, "\n".join(hv.warnings) if n else "Uyarı yok")
        self._set("Bus", bus_text, None if bus_ok else "bad")
