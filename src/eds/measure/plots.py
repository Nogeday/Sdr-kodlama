"""Render declarative :class:`~eds.measure.base.FigureSpec` objects to PNG.

Measurement code never imports matplotlib; it returns ``FigureSpec`` objects and this
module turns them into jury-facing images:

* ``"xy"``       generic plot — series styles ``line | marker | step | bar`` (+ ``yerr``),
                 labelled reference lines (``hlines`` / ``vlines``), annotated ``markers``,
                 log axes, limits, grid, legend (≥ 2 labelled series).
* ``"spectrum"`` spectrum-analyzer "screenshot": dark screen (``#0b0f14``), bright trace,
                 10×8 graticule, top info bar (Ref, scale, RBW, VBW/Avg, Span, Center and
                 any other ``fig.info`` entries), bottom bar (Start / per-division / Stop),
                 marker readout ``Mkr1: f, level``. X data are in the unit named in
                 ``xlabel`` (``[GHz]``/``[MHz]``/``[kHz]``/``[Hz]``; MHz when absent, as
                 produced by :func:`eds.measure.common.spectrum_screenshot`). Without
                 explicit ``markers`` a peak-search marker is placed on the trace maximum.
* ``"hist"``     histogram of ``series[i].y`` (common bin edges) + empirical CDF [%] on a
                 twin axis, percentile lines from ``vlines`` (p50/p95/p99 when empty),
                 statistics line (N, mean, σ, min, max) under the title.
* ``"polar"``    bearing plot: ``x`` = angle [°], ``y`` = radius (power, count, …);
                 ``vlines`` = labelled bearings, ``hlines`` = labelled circles. Options in
                 ``fig.info``: ``theta_zero`` (``N``/``E``/``S``/``W``, default ``N``) and
                 ``theta_direction`` (``cw`` default, or ``ccw``).

Every image is 1280×720 px (``dpi`` 100–160 changes only the text scale), uses DejaVu Sans
(full Turkish glyph coverage) and, when ``fig.info`` holds any of ``version`` / ``date`` /
``serial``, a traceability footer ``EDS <version> - <date> - <serial>``. Rendering is
deterministic: no implicit timestamps (the PNG carries only Title/Software text chunks),
no global pyplot state (object-oriented ``Figure`` + Agg canvas; figures are cleared after
saving), so identical specs give byte-identical files.
"""

from __future__ import annotations

import math
import re
import textwrap
import threading
from collections.abc import Callable, Sequence
from pathlib import Path
from typing import Any

import numpy as np

from .. import __version__
from ..core.errors import BackendUnavailable, MeasurementError
from ..core.units import fmt_hz
from .base import FigureSpec, Series

KINDS: tuple[str, ...] = ("xy", "spectrum", "hist", "polar")
STYLES: tuple[str, ...] = ("line", "marker", "step", "bar")
WIDTH_PX, HEIGHT_PX = 1280, 720
DEFAULT_DPI = 128
DPI_RANGE = (100, 160)
FOOTER_KEYS: tuple[str, ...] = ("version", "date", "serial")
OPTION_KEYS: tuple[str, ...] = ("theta_zero", "theta_direction")
FONT = "DejaVu Sans"
FONT_MONO = "DejaVu Sans Mono"

# Light theme (reports, print). Categorical order is fixed (never re-ranked).
PALETTE: tuple[str, ...] = ("#2a78d6", "#eb6834", "#1baf7a", "#eda100",
                            "#e87ba4", "#008300", "#6250d6", "#e34948")
INK, INK2, INK3 = "#0b0b0b", "#52514e", "#8a8984"
GRID, REF_LINE, SURFACE = "#dedcd6", "#52514e", "#ffffff"

# Spectrum-analyzer screen
SA_BG, SA_PANEL, SA_EDGE, SA_GRID = "#0b0f14", "#131a23", "#3b4a5a", "#26313d"
SA_TEXT, SA_MUTED = "#e8eef4", "#93a4b5"
SA_TRACES: tuple[str, ...] = ("#ffd400", "#00e5ff", "#ff5fd2", "#7dff5a")
SA_HLINE, SA_VLINE = "#ff7a59", "#58c4ff"
SA_X_DIV, SA_Y_DIV = 10, 8

_SA_INFO_ORDER = ("Ref", "Att", "RBW", "VBW", "Avg", "Span", "Center", "Window", "Detector")
_UNIT_RE = re.compile(r"\[([^\]]+)\]\s*$")
_X_SCALES = {"ghz": 1e9, "mhz": 1e6, "khz": 1e3, "hz": 1.0}
_RENDER_LOCK = threading.Lock()   # rc_context mutates global rcParams: serialize renders


# ----------------------------------------------------------------------------- public API
def available() -> bool:
    """True when matplotlib can be imported (figures will be rendered)."""
    try:
        _load_mpl()
    except BackendUnavailable:
        return False
    return True


def render(fig: FigureSpec, path: str | Path, *, dpi: int = DEFAULT_DPI) -> Path:
    """Render ``fig`` to a 1280×720 PNG at ``path`` and return the written path.

    A ``.png`` suffix is appended when ``path`` has another/no suffix; parent folders are
    created. Raises :class:`BackendUnavailable` when matplotlib is missing and
    :class:`MeasurementError` for an invalid spec (unknown kind/style, length mismatch).
    """
    if not DPI_RANGE[0] <= int(dpi) <= DPI_RANGE[1]:
        raise MeasurementError(f"dpi {DPI_RANGE[0]}..{DPI_RANGE[1]} aralığında olmalı (geldi: {dpi})")
    _validate(fig)
    mpl, figure_cls, canvas_cls = _load_mpl()
    out = Path(path)
    if out.suffix.lower() != ".png":
        out = out.with_name(out.name + ".png")
    out.parent.mkdir(parents=True, exist_ok=True)
    draw = _DRAWERS[fig.kind]
    with _RENDER_LOCK, mpl.rc_context(_rc(fig.kind)):
        f = figure_cls(figsize=(WIDTH_PX / dpi, HEIGHT_PX / dpi), dpi=dpi)
        canvas_cls(f)
        try:
            draw(f, fig)
            _footer(f, fig)
            f.savefig(out, dpi=dpi, format="png", facecolor=f.get_facecolor(),
                      metadata={"Title": fig.title, "Software": f"EDS {__version__}"})
        finally:
            f.clear()
    return out


# ----------------------------------------------------------------------------- helpers
def _load_mpl() -> tuple[Any, Any, Any]:
    try:
        import matplotlib
        from matplotlib.backends.backend_agg import FigureCanvasAgg
        from matplotlib.figure import Figure
    except ImportError as e:
        raise BackendUnavailable(
            "Grafik çizimi için matplotlib gerekli: pip install matplotlib "
            "(veya: pip install -e '.[report]')") from e
    return matplotlib, Figure, FigureCanvasAgg


def _rc(kind: str) -> dict[str, Any]:
    dark = kind == "spectrum"
    return {
        "font.family": [FONT], "font.size": 11, "axes.titlesize": 13, "axes.labelsize": 11,
        "axes.unicode_minus": True, "text.parse_math": False, "path.simplify": True,
        "svg.hashsalt": "eds", "figure.facecolor": SA_BG if dark else SURFACE,
        "axes.facecolor": SA_BG if dark else SURFACE,
        "axes.edgecolor": SA_EDGE if dark else INK3, "axes.labelcolor": SA_MUTED if dark else INK,
        "xtick.color": SA_MUTED if dark else INK2, "ytick.color": SA_MUTED if dark else INK2,
        "text.color": SA_TEXT if dark else INK, "grid.color": SA_GRID if dark else GRID,
        "grid.linewidth": 0.8, "legend.fontsize": 9, "legend.framealpha": 0.92,
    }


def _arr(v: Sequence[float] | None, what: str) -> np.ndarray:
    if v is None:
        return np.zeros(0)
    try:
        return np.asarray(v, dtype=float).ravel()
    except (TypeError, ValueError) as e:
        raise MeasurementError(f"Grafik verisi sayısal değil ({what}): {e}") from e


def _validate(fig: FigureSpec) -> None:
    if fig.kind not in KINDS:
        raise MeasurementError(f"Bilinmeyen grafik türü {fig.kind!r} ({fig.name}); geçerli: {list(KINDS)}")
    for i, s in enumerate(fig.series):
        where = f"{fig.name}: seri {i} ({s.label or '-'})"
        if s.style not in STYLES:
            raise MeasurementError(f"{where}: bilinmeyen stil {s.style!r}; geçerli: {list(STYLES)}")
        ny = _arr(s.y, where).size
        if fig.kind != "hist" and _arr(s.x, where).size != ny:
            raise MeasurementError(f"{where}: x ({len(s.x)}) ve y ({ny}) uzunlukları farklı")
        if s.yerr is not None and _arr(s.yerr, where).size != ny:
            raise MeasurementError(f"{where}: yerr ({len(s.yerr)}) ve y ({ny}) uzunlukları farklı")
    for lim, name in ((fig.xlim, "xlim"), (fig.ylim, "ylim")):
        if lim is not None and (len(lim) != 2 or not all(math.isfinite(float(v)) for v in lim)):
            raise MeasurementError(f"{fig.name}: {name} iki sonlu sayı olmalı (geldi: {lim!r})")
    if fig.kind == "polar":
        _polar_options(fig)


def _color(i: int) -> str:
    return PALETTE[i % len(PALETTE)]


def _linestyle(i: int) -> str:
    """Identity is never colour alone past the palette length: later series get dashes."""
    return "-" if i < len(PALETTE) else ("--", ":", "-.")[(i // len(PALETTE) - 1) % 3]


def _bracket_unit(label: str) -> str:
    m = _UNIT_RE.search(label or "")
    return m.group(1).strip() if m else ""


def _x_scale(label: str, default: float = 1e6) -> float:
    return _X_SCALES.get(_bracket_unit(label).lower(), default)


def _info_items(fig: FigureSpec, skip: Sequence[str] = ()) -> list[tuple[str, str]]:
    hidden = set(FOOTER_KEYS) | set(OPTION_KEYS) | set(skip)
    return [(str(k), str(v)) for k, v in fig.info.items() if k not in hidden]


def _footer(f: Any, fig: FigureSpec) -> None:
    if not any(k in fig.info for k in FOOTER_KEYS):
        return
    version = fig.info.get("version", "")
    parts = [f"EDS {version}".strip()]
    parts += [str(fig.info[k]) for k in ("date", "serial") if fig.info.get(k)]
    color = SA_MUTED if fig.kind == "spectrum" else INK3
    f.text(0.992, 0.012, " - ".join(parts), ha="right", va="bottom", fontsize=8, color=color)


def _header(f: Any, fig: FigureSpec, subtitle: str = "", dark: bool = False) -> None:
    title = fig.title or fig.name
    lines = textwrap.wrap(title, 100) or [""]
    size = 15 if len(lines) == 1 else 13
    f.text(0.012, 0.965, "\n".join(lines[:2]), ha="left", va="top", fontsize=size, fontweight="bold",
           color=SA_TEXT if dark else INK)
    if subtitle:
        sub = textwrap.wrap(subtitle, 165)
        f.text(0.012, 0.965 - (0.05 if len(lines) == 1 else 0.085), "\n".join(sub[:2]), ha="left",
               va="top", fontsize=9.5, color=SA_MUTED if dark else INK2)


def _subtitle(fig: FigureSpec, prefix: str = "") -> str:
    items = [f"{k}: {v}" for k, v in _info_items(fig)]
    return "  ·  ".join(([prefix] if prefix else []) + items)


def _no_data(ax: Any, dark: bool = False) -> None:
    ax.text(0.5, 0.5, "Veri yok", transform=ax.transAxes, ha="center", va="center", fontsize=16,
            color=SA_MUTED if dark else INK3)


def _finite(*arrays: np.ndarray) -> np.ndarray:
    vals = [a[np.isfinite(a)] for a in arrays if a.size]
    return np.concatenate(vals) if vals else np.zeros(0)


def _ref_lines(ax: Any, fig: FigureSpec, *, dark: bool = False) -> None:
    """Labelled reference lines. On the dark spectrum screen vertical labels sit at the
    bottom of the graticule (the marker readout occupies the top-right corner)."""
    hcol, vcol = (SA_HLINE, SA_VLINE) if dark else (REF_LINE, REF_LINE)
    tcol = SA_TEXT if dark else INK2
    box = {"boxstyle": "round,pad=0.2", "fc": SA_PANEL if dark else SURFACE, "ec": "none", "alpha": 0.85}
    for y, label in fig.hlines:
        ax.axhline(y, color=hcol, ls="--", lw=1.2, zorder=3)
        if label:
            ax.text(0.995, y, label, transform=ax.get_yaxis_transform(), ha="right", va="bottom",
                    fontsize=9, color=tcol, bbox=box, clip_on=True, zorder=6)
    for x, label in fig.vlines:
        ax.axvline(x, color=vcol, ls="-." if dark else ":", lw=1.4, zorder=3)
        if label:
            ax.text(x, 0.015 if dark else 0.985, label, transform=ax.get_xaxis_transform(), ha="right",
                    va="bottom" if dark else "top", rotation=90, fontsize=9, color=tcol, bbox=box,
                    clip_on=True, zorder=6)


def _annotate(ax: Any, markers: Sequence[tuple[float, float, str]], *, dark: bool = False,
              numbered: bool = False) -> None:
    fc, ec, tc = (SA_PANEL, SA_EDGE, SA_TEXT) if dark else (SURFACE, INK3, INK)
    mcol = "#ffffff" if dark else INK
    for i, (x, y, text) in enumerate(markers):
        ax.plot([x], [y], marker="D", ms=8, color=mcol, mec=SA_BG if dark else SURFACE, mew=1.5,
                ls="none", zorder=7)
        label = str(i + 1) if numbered else text
        if not label:
            continue
        dy = 10 + 14 * (i % 3)
        ax.annotate(label, (x, y), xytext=(8, dy), textcoords="offset points", fontsize=9, color=tc,
                    fontweight="bold" if numbered else "normal", zorder=8,
                    bbox=None if numbered else {"boxstyle": "round,pad=0.25", "fc": fc, "ec": ec},
                    arrowprops=None if numbered else {"arrowstyle": "-", "color": ec, "lw": 0.8})


def _bar_geometry(x: np.ndarray, logx: bool) -> tuple[np.ndarray, np.ndarray]:
    """Left edges + widths (bars 80 % of the local spacing; log-spaced on a log axis)."""
    xs = np.unique(x[np.isfinite(x)])
    if logx and xs.size and np.all(xs > 0):
        lx = np.log10(xs)
        h = 0.4 * (float(np.min(np.diff(lx))) if lx.size > 1 else 0.2)
        return x * 10.0 ** (-h), x * (10.0 ** h - 10.0 ** (-h))
    w = 0.8 * (float(np.min(np.diff(xs))) if xs.size > 1 else 1.0)
    return x - w / 2.0, np.full_like(x, w)


def _apply_axes(ax: Any, fig: FigureSpec) -> None:
    if fig.logx:
        ax.set_xscale("log")
    if fig.logy:
        ax.set_yscale("log")
    if fig.xlim is not None:
        ax.set_xlim(*fig.xlim)
    if fig.ylim is not None:
        ax.set_ylim(*fig.ylim)
    ax.grid(True, which="major", zorder=0)
    if fig.logx or fig.logy:
        ax.minorticks_on()
        ax.grid(True, which="minor", alpha=0.35, lw=0.5, zorder=0)
    ax.set_axisbelow(True)
    ax.set_xlabel(fig.xlabel)
    ax.set_ylabel(fig.ylabel)
    for side in ("top", "right"):
        ax.spines[side].set_visible(False)


def _legend(ax: Any, handles: list[Any] | None = None, labels: list[str] | None = None,
            min_entries: int = 2, **kw: Any) -> None:
    if handles is None:
        handles, labels = ax.get_legend_handles_labels()
    if labels is not None and len(labels) >= min_entries:
        ax.legend(handles, labels, **kw)


# ----------------------------------------------------------------------------- xy
def _plot_series(ax: Any, s: Series, i: int, logx: bool) -> None:
    x, y = _arr(s.x, "x"), _arr(s.y, "y")
    yerr = _arr(s.yerr, "yerr") if s.yerr is not None else None
    color, ls, label = _color(i), _linestyle(i), (s.label or None)
    if s.style == "line":
        if yerr is not None:
            ax.errorbar(x, y, yerr=yerr, color=color, ls=ls, lw=2, elinewidth=1, capsize=3, label=label,
                        zorder=4)
        else:
            ax.plot(x, y, color=color, ls=ls, lw=2, label=label, zorder=4)
    elif s.style == "marker":
        ax.errorbar(x, y, yerr=yerr, fmt="o", ms=7, color=color, mec=SURFACE, mew=1.2, elinewidth=1,
                    capsize=3, label=label, zorder=5)
    elif s.style == "step":
        ax.step(x, y, where="mid", color=color, ls=ls, lw=2, label=label, zorder=4)
        if yerr is not None:
            ax.errorbar(x, y, yerr=yerr, fmt="none", ecolor=color, elinewidth=1, capsize=3, zorder=4)
    else:  # bar
        left, width = _bar_geometry(x, logx)
        ax.bar(left, y, width=width, align="edge", color=color, edgecolor=SURFACE, linewidth=1.5,
               yerr=yerr, error_kw={"elinewidth": 1, "capsize": 3, "ecolor": INK2}, label=label, zorder=3)


def _draw_xy(f: Any, fig: FigureSpec) -> None:
    _header(f, fig, _subtitle(fig))
    top = 0.84 if _info_items(fig) or len(fig.title) > 100 else 0.88
    ax = f.add_axes((0.075, 0.12, 0.9, top - 0.12))
    data = [s for s in fig.series if len(s.y)]
    for i, s in enumerate(fig.series):
        _plot_series(ax, s, i, fig.logx)
    _ref_lines(ax, fig)
    _annotate(ax, fig.markers)
    _apply_axes(ax, fig)
    if not data:
        _no_data(ax)
    _legend(ax, loc="best")


# ----------------------------------------------------------------------------- spectrum
def _fmt_level(v: float, unit: str) -> str:
    return f"{v:.1f} {unit}".strip() if math.isfinite(v) else "—"


def _draw_spectrum(f: Any, fig: FigureSpec) -> None:
    from matplotlib.patches import Rectangle

    unit = _bracket_unit(fig.ylabel) or (fig.series[0].label if fig.series else "") or "dB"
    scale = _x_scale(fig.xlabel)
    xs = [_arr(s.x, "x") for s in fig.series]
    ys = [_arr(s.y, "y") for s in fig.series]
    fx, fy = _finite(*xs), _finite(*ys)

    if fig.xlim is not None:
        x0, x1 = float(fig.xlim[0]), float(fig.xlim[1])
    elif fx.size:
        x0, x1 = float(fx.min()), float(fx.max())
    else:
        x0, x1 = 0.0, 1.0
    if x1 <= x0:
        x0, x1 = x0 - 0.5, x0 + 0.5
    if fig.ylim is not None:
        y0, y1 = float(fig.ylim[0]), float(fig.ylim[1])
    else:
        top = float(fy.max()) if fy.size else 0.0
        y1 = 10.0 * math.ceil((top + 5.0) / 10.0)
        y0 = y1 - 100.0
    if y1 <= y0:
        y0, y1 = y0 - 50.0, y0 + 50.0
    ydiv = (y1 - y0) / SA_Y_DIV

    _header(f, fig, dark=True)

    # --- top info bar
    items = dict(_info_items(fig, skip=("Start", "Stop")))
    ordered = [(k, items.pop(k)) for k in _SA_INFO_ORDER if k in items]
    ordered.insert(1 if ordered and ordered[0][0] == "Ref" else 0,
                   ("Ölçek", f"{ydiv:g} {'dB' if unit.lower().startswith('db') else unit}/div"))
    ordered += list(items.items())
    per_row = 6
    rows = [ordered[i:i + per_row] for i in range(0, len(ordered), per_row)][:2]
    bar_h = 0.045 * len(rows)
    bar_top = 0.905
    f.add_artist(Rectangle((0.012, bar_top - bar_h), 0.976, bar_h, transform=f.transFigure,
                           fc=SA_PANEL, ec=SA_EDGE, lw=0.8))
    renderer = f.canvas.get_renderer()
    fig_w_px = f.get_figwidth() * f.dpi
    for r, row in enumerate(rows):
        yy = bar_top - 0.0225 - 0.045 * r
        for c, (k, v) in enumerate(row):
            xx = 0.022 + c * (0.966 / per_row)
            key = f.text(xx, yy, k, ha="left", va="center", fontsize=9, color=SA_MUTED)
            key_w = key.get_window_extent(renderer).width / fig_w_px
            f.text(xx + key_w + 0.006, yy, v, ha="left", va="center", fontsize=10.5,
                   color=SA_TEXT, fontweight="bold")

    # --- screen
    ax_top = bar_top - bar_h - 0.02
    ax = f.add_axes((0.075, 0.14, 0.905, ax_top - 0.14))
    ax.set_facecolor(SA_BG)
    for i, (x, y) in enumerate(zip(xs, ys)):
        label = fig.series[i].label or None
        style = fig.series[i].style
        color = SA_TRACES[i % len(SA_TRACES)]
        if style == "marker":
            ax.plot(x, y, "o", ms=4, color=color, label=label, zorder=4)
        elif style == "step":
            ax.step(x, y, where="mid", color=color, lw=1.1, label=label, zorder=4)
        elif style == "bar":
            ax.vlines(x, y0, y, color=color, lw=1.0, label=label, zorder=4)
        else:
            ax.plot(x, y, color=color, lw=1.0, label=label, zorder=4)
    ax.set_xlim(x0, x1)
    ax.set_ylim(y0, y1)
    ax.set_xticks(np.linspace(x0, x1, SA_X_DIV + 1))
    ax.set_yticks(np.linspace(y0, y1, SA_Y_DIV + 1))
    ax.grid(True, color=SA_GRID, lw=0.8, ls="-", zorder=0)
    ax.tick_params(axis="x", labelbottom=False, length=0)
    ax.tick_params(axis="y", length=0, labelsize=9, colors=SA_MUTED)
    ax.yaxis.set_major_formatter(_tick_formatter())
    for sp in ax.spines.values():
        sp.set_color(SA_EDGE)
    ax.set_ylabel(fig.ylabel or unit, color=SA_MUTED)
    _ref_lines(ax, fig, dark=True)
    if not fx.size:
        _no_data(ax, dark=True)

    # --- markers (explicit, else peak search on trace 1)
    markers = list(fig.markers)
    if not markers and fy.size:
        x_t, y_t = xs[0], ys[0]
        ok = np.isfinite(x_t) & np.isfinite(y_t)
        if ok.any():
            k = int(np.argmax(np.where(ok, y_t, -np.inf)))
            markers = [(float(x_t[k]), float(y_t[k]), "")]
    if markers:
        _annotate(ax, markers, dark=True, numbered=True)
        lines = []
        for i, (mx, my, text) in enumerate(markers):
            line = f"Mkr{i + 1}: {fmt_hz(mx * scale, 9)}, {_fmt_level(my, unit)}"
            lines.append(f"{line}  {text}" if text else line)
        ax.text(0.99, 0.975, "\n".join(lines), transform=ax.transAxes, ha="right", va="top",
                fontsize=9.5, family=FONT_MONO, color=SA_TEXT, zorder=9,
                bbox={"boxstyle": "round,pad=0.4", "fc": SA_PANEL, "ec": SA_EDGE, "alpha": 0.95})
    _legend(ax, loc="upper left", facecolor=SA_PANEL, edgecolor=SA_EDGE, labelcolor=SA_TEXT)

    # --- bottom bar
    start = fig.info.get("Start") or fmt_hz(x0 * scale, 9)
    stop = fig.info.get("Stop") or fmt_hz(x1 * scale, 9)
    per_div = fmt_hz((x1 - x0) * scale / SA_X_DIV, 6) + "/div"
    f.add_artist(Rectangle((0.012, 0.05), 0.976, 0.055, transform=f.transFigure, fc=SA_PANEL,
                           ec=SA_EDGE, lw=0.8))
    for xx, ha, label, value in ((0.022, "left", "Start", start), (0.5, "center", "", per_div),
                                 (0.978, "right", "Stop", stop)):
        f.text(xx, 0.0775, f"{label}  {value}" if label else value, ha=ha, va="center", fontsize=10.5,
               color=SA_TEXT, fontweight="bold")


def _tick_formatter() -> Any:
    from matplotlib.ticker import FuncFormatter

    return FuncFormatter(lambda v, _pos: f"{v:.1f}" if abs(v - round(v)) > 1e-9 else f"{v:.0f}")


# ----------------------------------------------------------------------------- hist
def _hist_edges(data: np.ndarray, logx: bool) -> np.ndarray:
    lo, hi = float(data.min()), float(data.max())
    if hi <= lo:
        w = max(abs(lo) * 0.01, 0.5)
        return np.array([lo - w, lo + w])
    if logx and lo > 0:
        n = int(np.clip(np.ceil(np.log2(data.size) + 1), 5, 80))
        return np.logspace(np.log10(lo), np.log10(hi), n + 1)
    edges = np.histogram_bin_edges(data, bins="auto")
    if edges.size - 1 > 80:
        edges = np.linspace(lo, hi, 81)
    elif edges.size - 1 < 5:
        edges = np.linspace(lo, hi, 6)
    return edges


def _fmt_stat(v: float) -> str:
    return f"{v:.4g}"


def _draw_hist(f: Any, fig: FigureSpec) -> None:
    unit = _bracket_unit(fig.xlabel)
    sets = [(_arr(s.y, "y"), s) for s in fig.series]
    sets = [(d[np.isfinite(d)], s) for d, s in sets]
    sets = [(d, s) for d, s in sets if d.size]
    stats = ""
    if sets:
        d0 = sets[0][0]
        u = f" {unit}" if unit else ""
        stats = (f"N = {d0.size}  ·  ortalama = {_fmt_stat(float(d0.mean()))}{u}  ·  "
                 f"σ = {_fmt_stat(float(d0.std(ddof=1)) if d0.size > 1 else 0.0)}{u}  ·  "
                 f"min = {_fmt_stat(float(d0.min()))}{u}  ·  maks = {_fmt_stat(float(d0.max()))}{u}")
    _header(f, fig, _subtitle(fig, stats))
    ax = f.add_axes((0.075, 0.12, 0.835, 0.72))
    if not sets:
        _no_data(ax)
        _apply_axes(ax, fig)
        return
    edges = _hist_edges(np.concatenate([d for d, _ in sets]), fig.logx)
    ax2 = ax.twinx()
    multi = len(sets) > 1
    for i, (d, s) in enumerate(sets):
        color = _color(i)
        name = s.label or ("Histogram" if not multi else f"Seri {i + 1}")
        ax.hist(d, bins=edges, color=color, alpha=0.45 if multi else 0.85, edgecolor=SURFACE,
                linewidth=1.0, histtype="stepfilled" if multi else "bar", label=name, zorder=3)
        xs = np.sort(d)
        cdf = np.arange(1, xs.size + 1) / xs.size * 100.0
        ax2.step(np.concatenate([[xs[0]], xs]), np.concatenate([[0.0], cdf]), where="post",
                 color=_color(i + 1) if not multi else color, lw=2, label=f"{name} — KDF", zorder=4)
    vlines = list(fig.vlines)
    if not vlines:
        d0 = sets[0][0]
        u = f" {unit}" if unit else ""
        vlines = [(float(np.percentile(d0, q)), f"p{q} = {_fmt_stat(float(np.percentile(d0, q)))}{u}")
                  for q in (50, 95, 99)]
    shown = FigureSpec(name=fig.name, title=fig.title, hlines=fig.hlines, vlines=vlines)
    _ref_lines(ax, shown)
    _annotate(ax, fig.markers)
    _apply_axes(ax, fig)
    ax.set_ylabel(fig.ylabel or "Adet")
    ax2.set_ylim(0.0, 100.0)
    ax2.set_ylabel("Kümülatif dağılım [%]")
    ax2.spines["top"].set_visible(False)
    ax.spines["right"].set_visible(True)
    h1, l1 = ax.get_legend_handles_labels()
    h2, l2 = ax2.get_legend_handles_labels()
    ax2.legend(h1 + h2, l1 + l2, loc="center right")


# ----------------------------------------------------------------------------- polar
_THETA_ZERO = ("N", "NE", "E", "SE", "S", "SW", "W", "NW")


def _polar_options(fig: FigureSpec) -> tuple[str, str]:
    zero = str(fig.info.get("theta_zero", "N")).upper()
    direction = str(fig.info.get("theta_direction", "cw")).lower()
    if zero not in _THETA_ZERO:
        raise MeasurementError(f"{fig.name}: theta_zero {zero!r} geçersiz; seçenekler: {list(_THETA_ZERO)}")
    if direction not in ("cw", "ccw"):
        raise MeasurementError(f"{fig.name}: theta_direction 'cw' ya da 'ccw' olmalı (geldi: {direction!r})")
    return zero, direction


def _draw_polar(f: Any, fig: FigureSpec) -> None:
    _header(f, fig, _subtitle(fig))
    ax = f.add_axes((0.04, 0.07, 0.62, 0.76), projection="polar")
    zero, direction = _polar_options(fig)
    ax.set_theta_zero_location(zero)
    ax.set_theta_direction(-1 if direction == "cw" else 1)
    ax.set_thetagrids(np.arange(0, 360, 30), labels=[f"{a}°" for a in range(0, 360, 30)])
    ax.set_rlabel_position(157.5)
    ax.tick_params(axis="y", labelsize=9, colors=INK3)
    ax.grid(True, color=GRID, lw=0.8)

    ys = [_arr(s.y, "y") for s in fig.series]
    fy = _finite(*ys, np.array([m[1] for m in fig.markers], dtype=float),
                 np.array([h[0] for h in fig.hlines], dtype=float))
    if fig.ylim is not None:
        r0, r1 = float(fig.ylim[0]), float(fig.ylim[1])
    elif fy.size:
        lo, hi = float(fy.min()), float(fy.max())
        pad = 0.05 * (hi - lo if hi > lo else max(abs(hi), 1.0))
        r0 = 0.0 if lo >= 0 and not fig.logy else lo - pad
        r1 = hi + pad
    else:
        r0, r1 = 0.0, 1.0
    if fig.logy:
        if r0 <= 0:
            r0 = float(fy[fy > 0].min()) * 0.8 if (fy > 0).any() else 1e-3
        ax.set_rscale("log")

    for i, s in enumerate(fig.series):
        th = np.deg2rad(_arr(s.x, "x"))
        r = ys[i]
        color, ls, label = _color(i), _linestyle(i), (s.label or None)
        yerr = _arr(s.yerr, "yerr") if s.yerr is not None else None
        if s.style == "marker":
            ax.errorbar(th, r, yerr=yerr, fmt="o", ms=7, color=color, mec=SURFACE, mew=1.2,
                        elinewidth=1, capsize=2, label=label, zorder=5)
        elif s.style == "bar":
            uniq = np.unique(th[np.isfinite(th)])
            w = 0.8 * (float(np.min(np.diff(uniq))) if uniq.size > 1 else np.deg2rad(5.0))
            ax.bar(th, r - r0, width=w, bottom=r0, color=color, edgecolor=SURFACE, linewidth=1.0,
                   label=label, zorder=3)
        else:
            ax.plot(th, r, color=color, ls=ls, lw=2, label=label, zorder=4,
                    drawstyle="steps-mid" if s.style == "step" else "default")
            if yerr is not None:
                ax.errorbar(th, r, yerr=yerr, fmt="none", ecolor=color, elinewidth=1, capsize=2, zorder=4)
    ax.set_rlim(r0, r1)

    # reference bearings / circles are identified in the legend (no text on the busy disc)
    circle = np.linspace(0.0, 2.0 * np.pi, 361)
    for j, (r, label) in enumerate(fig.hlines):
        ax.plot(circle, np.full_like(circle, r), color=REF_LINE, ls=("--", "-.", ":")[j % 3], lw=1.2,
                label=label or None, zorder=3)
    for j, (a, label) in enumerate(fig.vlines):
        th = np.deg2rad(a)
        ax.plot([th, th], [r0, r1], color=INK, ls=(":", "--", "-.")[j % 3], lw=1.8, label=label or None,
                zorder=3)
    _annotate(ax, [(float(np.deg2rad(x)), y, t) for x, y, t in fig.markers])
    desc = [f"Açı: {fig.xlabel}" if fig.xlabel else "", f"Yarıçap: {fig.ylabel}" if fig.ylabel else "",
            f"0° = {zero}, {'saat yönü' if direction == 'cw' else 'saat yönünün tersi'} pozitif"]
    f.text(0.70, 0.82, "\n".join(d for d in desc if d), fontsize=10, color=INK2, ha="left", va="top",
           linespacing=1.6)
    if not _finite(*ys).size:
        _no_data(ax)
    handles, labels = ax.get_legend_handles_labels()
    if labels:
        ax.legend(handles, labels, loc="upper left", bbox_to_anchor=(0.695, 0.66),
                  bbox_transform=f.transFigure)


_DRAWERS: dict[str, Callable[[Any, FigureSpec], None]] = {
    "xy": _draw_xy, "spectrum": _draw_spectrum, "hist": _draw_hist, "polar": _draw_polar,
}
