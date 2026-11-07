"""Dark operator-console theme (high contrast, monospace numbers, consistent signal colours)."""

from __future__ import annotations

BG = "#090d12"          # window / plot background
PANEL = "#0f151c"       # panel background
PANEL_2 = "#141d27"     # header strips, table header
BORDER = "#22303e"
GRID = "#1c2733"
TEXT = "#d8e3ee"
MUTED = "#7d90a5"
DIM = "#4d5d6e"         # stale / uncalibrated
ACCENT = "#35d0ff"
TRACE = "#c8f25a"       # live spectrum trace (analyzer yellow-green)
HOLD = "#5c6f3a"        # max-hold trace
NOISE = "#5f7790"
OK = "#2fd27a"
WARN = "#f5b301"
BAD = "#ff4d4f"
NONE = "#56657a"

FONT_UI = "DejaVu Sans"
FONT_MONO = "DejaVu Sans Mono"

LEVEL_COLORS = {"ok": OK, "warn": WARN, "bad": BAD, "none": NONE}

STYLESHEET = f"""
QMainWindow, QWidget {{ background: {BG}; color: {TEXT}; font-family: "{FONT_UI}"; font-size: 10pt; }}
QDockWidget {{ color: {TEXT}; font-weight: bold; }}
QDockWidget::title {{ background: {PANEL_2}; padding: 5px 8px; border-bottom: 1px solid {BORDER};
                      text-align: left; }}
QSplitter::handle {{ background: {BORDER}; }}
QTableView {{ background: {PANEL}; alternate-background-color: #121a23; gridline-color: {GRID};
              selection-background-color: #1f4058; selection-color: #ffffff; border: 1px solid {BORDER};
              font-family: "{FONT_MONO}"; font-size: 9pt; }}
QHeaderView::section {{ background: {PANEL_2}; color: {MUTED}; padding: 4px 6px; border: 0;
                        border-right: 1px solid {BORDER}; border-bottom: 1px solid {BORDER};
                        font-family: "{FONT_UI}"; font-size: 9pt; font-weight: bold; }}
QTableWidget {{ background: {PANEL}; gridline-color: {GRID}; border: 1px solid {BORDER};
                font-family: "{FONT_MONO}"; font-size: 9pt; }}
QLabel#chip {{ background: {PANEL_2}; border: 1px solid {BORDER}; border-radius: 4px; padding: 3px 7px;
               font-family: "{FONT_MONO}"; font-size: 9pt; }}
QLabel#chipKey {{ color: {MUTED}; font-size: 8pt; }}
QLabel#title {{ font-size: 12pt; font-weight: bold; letter-spacing: 1px; }}
QLabel#muted {{ color: {MUTED}; }}
QLabel#badge {{ border-radius: 4px; padding: 2px 8px; font-weight: bold; }}
QToolTip {{ background: {PANEL_2}; color: {TEXT}; border: 1px solid {BORDER}; }}
QScrollBar:vertical {{ background: {PANEL}; width: 10px; }}
QScrollBar::handle:vertical {{ background: {BORDER}; border-radius: 4px; min-height: 20px; }}
"""


def apply(app) -> None:
    """Apply the theme to a QApplication (and pyqtgraph defaults)."""
    import pyqtgraph as pg
    from PySide6 import QtGui

    app.setStyle("Fusion")
    pal = QtGui.QPalette()
    for role, col in ((QtGui.QPalette.Window, BG), (QtGui.QPalette.Base, PANEL), (QtGui.QPalette.Text, TEXT),
                      (QtGui.QPalette.WindowText, TEXT), (QtGui.QPalette.Button, PANEL_2),
                      (QtGui.QPalette.ButtonText, TEXT), (QtGui.QPalette.Highlight, "#1f4058"),
                      (QtGui.QPalette.HighlightedText, "#ffffff"), (QtGui.QPalette.ToolTipBase, PANEL_2),
                      (QtGui.QPalette.ToolTipText, TEXT), (QtGui.QPalette.AlternateBase, "#121a23")):
        pal.setColor(role, QtGui.QColor(col))
    app.setPalette(pal)
    app.setStyleSheet(STYLESHEET)
    pg.setConfigOptions(antialias=False, background=BG, foreground=TEXT, imageAxisOrder="row-major")


def hex_rgba(hex_color: str, alpha: int) -> tuple[int, int, int, int]:
    h = hex_color.lstrip("#")
    return int(h[0:2], 16), int(h[2:4], 16), int(h[4:6], 16), int(alpha)
