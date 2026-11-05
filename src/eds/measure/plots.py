"""Render declarative :class:`~eds.measure.base.FigureSpec` objects to PNG.

Measurement code never imports matplotlib; it returns ``FigureSpec`` objects and this
module turns them into jury-facing images:

* ``"xy"``       generic plot — series styles ``line | marker | step | bar`` (+ ``yerr``),
                 labelled reference lines (``hlines`` / ``vlines``), annotated ``markers``,
                 log axes, limits, grid, legend (≥ 2 labelled series).
* ``"spectrum"`` spectrum-analyzer "screenshot": dark screen (``#0b0f14``), bright trace,
                 top info bar (Ref, scale, RBW, VBW/Avg, Span, Center and any other
                 ``fig.info`` entries), marker table under the info bar (``Mkr1 f level``,
                 never drawn over the trace), bottom bar (Start / per-division / Stop).
                 Instrument scales are computed here whatever ``ylim`` says: 1/2/5/10/20
                 dB/div over 10 (or 8) divisions, Ref rounded to the division (≤ 0 dBFS
                 when the trace stays below full scale), tick labels on the graticule;
                 ``ylim`` is only a hint for the reference level and the depth to show.
                 The frequency axis is ``Center ± Span/2`` when ``fig.info`` holds
                 parseable ``Center``/``Span`` values that match the data (else ``xlim`` or
                 the data extent). X data are in the unit named in ``xlabel``
                 (``[GHz]``/``[MHz]``/``[kHz]``/``[Hz]``; MHz when absent, as produced by
                 :func:`eds.measure.common.spectrum_screenshot`). Without explicit
                 ``markers`` a peak-search marker is placed on the trace maximum.
* ``"hist"``     histogram of ``series[i].y`` (common bin edges) + empirical CDF [%] on a
                 twin axis, percentile lines from ``vlines`` (p50/p95/p99 when empty),
                 statistics line (N, mean, σ, min, max) under the title.
* ``"polar"``    bearing plot: ``x`` = angle [°], ``y`` = radius (power, count, …);
                 ``vlines`` = labelled bearings, ``hlines`` = labelled circles. Options in
                 ``fig.info``: ``theta_zero`` (``N``/``E``/``S``/``W``, default ``N``) and
                 ``theta_direction`` (``cw`` default, or ``ccw``).

Series colours: categorical by position, except for series with a *semantic role*
(``pass`` green, ``fail`` red, ``warn`` amber, ``ref`` grey — the HTML report's badge
colours), which keep their colour whatever their position. The role comes from an
optional ``Series.role`` attribute when present, else from a ``{pass}``/``{fail}``/
``{warn}``/``{ref}`` tag in the label (stripped from the legend), else from the label's
wording: ``GEÇTİ…``/``KALDI…`` at its start, ``kırpma``/``sıkışma``/``clip`` anywhere.
Other series of such a figure avoid green/red/amber.

Every image is 1280×720 px (``dpi`` 100–160 changes only the text scale), uses DejaVu Sans
(full Turkish glyph coverage) and, when ``fig.info`` holds any of ``version`` / ``date`` /
``serial``, a traceability footer ``EDS <version> - <date> - <serial>``; ``flags`` (e.g.
``SİMÜLASYON · HIZLI MOD - KTR'de kullanılmaz``) is printed bold red at the bottom left
and stored in the PNG ``Description`` chunk. Titles are wrapped / shrunk by their rendered
width and the axes are moved so that tick and axis labels stay on the canvas. Rendering is
deterministic: no implicit timestamps (the PNG carries only Title/Software text chunks),
no global pyplot state (object-oriented ``Figure`` + Agg canvas; figures are cleared after
saving), so identical specs give byte-identical files.
"""

from __future__ import annotations

import math
import re
import threading
from collections import Counter
from collections.abc import Callable, Sequence
from pathlib import Path
from typing import Any, NamedTuple

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
FOOTER_KEYS: tuple[str, ...] = ("version", "date", "serial", "flags")
OPTION_KEYS: tuple[str, ...] = ("theta_zero", "theta_direction")
FONT = "DejaVu Sans"
FONT_MONO = "DejaVu Sans Mono"

# Light theme (reports, print). Categorical order is fixed (never re-ranked).
PALETTE: tuple[str, ...] = ("#2a78d6", "#eb6834", "#1baf7a", "#eda100",
                            "#e87ba4", "#008300", "#6250d6", "#e34948")
INK, INK2, INK3 = "#0b0b0b", "#52514e", "#8a8984"
GRID, REF_LINE, SURFACE = "#dedcd6", "#52514e", "#ffffff"

# Semantic series colours (same as the HTML report badges): independent of series order.
ROLE_COLORS: dict[str, str] = {"pass": "#1a7f37", "fail": "#cf222e", "warn": "#9a6700", "ref": "#52514e"}
ROLES: tuple[str, ...] = tuple(ROLE_COLORS)
# Neutral colours for the other series of a figure that uses roles (no green / red / amber).
NEUTRAL_PALETTE: tuple[str, ...] = ("#2a78d6", "#6250d6", "#e87ba4")
_ROLE_MARKERS: tuple[str, ...] = ("o", "s", "^", "D", "v", "P")
FLAG_LIGHT, FLAG_DARK = "#cf222e", "#ff6b6b"

# Spectrum-analyzer screen
SA_BG, SA_PANEL, SA_EDGE, SA_GRID = "#0b0f14", "#131a23", "#3b4a5a", "#26313d"
SA_TEXT, SA_MUTED = "#e8eef4", "#93a4b5"
SA_TRACES: tuple[str, ...] = ("#ffd400", "#00e5ff", "#ff5fd2", "#7dff5a")
SA_HLINE, SA_VLINE = "#ff7a59", "#58c4ff"
SA_X_DIV = 10
SA_Y_DIVS: tuple[int, ...] = (10, 8)                      # preferred first
SA_DB_PER_DIV: tuple[float, ...] = (1.0, 2.0, 5.0, 10.0, 20.0)
SA_DEFAULT_DEPTH_DB = 100.0
SA_MARKER_ROWS = 3

_SA_INFO_ORDER = ("Ref", "Att", "RBW", "VBW", "Avg", "Span", "Center", "Window", "Detector")
_UNIT_RE = re.compile(r"\[([^\]]+)\]\s*$")
_X_SCALES = {"ghz": 1e9, "mhz": 1e6, "khz": 1e3, "hz": 1.0}
_HZ_RE = re.compile(r"^\s*([-+]?\d+(?:\.\d*)?(?:[eE][-+]?\d+)?)\s*(GHz|MHz|kHz|Hz)\s*$")
_ROLE_TAG_RE = re.compile(r"\s*\{(pass|fail|warn|ref)\}\s*", re.IGNORECASE)
_ROLE_WORDS: tuple[tuple[str, re.Pattern[str]], ...] = (
    ("pass", re.compile(r"^(gecti|pass(ed)?)\b")),
    ("fail", re.compile(r"^(kaldi|fail(ed)?)\b")),
    ("warn", re.compile(r"\b(kirp\w*|sikisma\w*|clip\w*|compress\w*)\b")),
)
_FOLD = str.maketrans({"ı": "i", "İ": "i", "ş": "s", "Ş": "s", "ç": "c", "Ç": "c", "ğ": "g", "Ğ": "g",
                       "ö": "o", "Ö": "o", "ü": "u", "Ü": "u", "̇": None})
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
            meta = {"Title": fig.title, "Software": f"EDS {__version__}"}
            flags = str(fig.info.get("flags", "") or "").strip()
            if flags:
                meta["Description"] = flags
            f.savefig(out, dpi=dpi, format="png", facecolor=f.get_facecolor(), metadata=meta)
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


def _fold(text: str) -> str:
    """Case/diacritic folding for Turkish label matching (``GEÇTİ`` → ``gecti``)."""
    return text.translate(_FOLD).lower().translate(_FOLD)


def series_role(s: Series) -> tuple[str, str]:
    """``(role, legend label)`` of a series; role ∈ :data:`ROLES` or ``""`` (categorical).

    Precedence: a ``role`` attribute on the series (forward compatible with an optional
    ``Series.role`` field), a ``{pass}``/``{fail}``/``{warn}``/``{ref}`` tag in the label
    (removed from the legend), then the label's wording (``GEÇTİ…`` / ``KALDI…`` at the
    start, ``kırpma`` / ``sıkışma`` / ``clip`` anywhere).
    """
    label = s.label or ""
    role = str(getattr(s, "role", "") or "").strip().lower()
    m = _ROLE_TAG_RE.search(label)
    if m:
        label = _ROLE_TAG_RE.sub(" ", label).strip()
        role = role or m.group(1).lower()
    if role in ROLES:
        return role, label
    folded = _fold(label).strip()
    for name, pattern in _ROLE_WORDS:
        if pattern.search(folded):
            return name, label
    return "", label


class _Style(NamedTuple):
    color: str
    ls: str
    marker: str
    label: str | None


def _series_styles(series: Sequence[Series]) -> list[_Style]:
    """Colour / line style / marker / legend label per series.

    Without any semantic role the classic positional palette is used. With roles, role
    series get their fixed colour (several series of one role differ by marker shape and
    dash pattern) and the other series take neutral colours only."""
    roles = [series_role(s) for s in series]
    if not any(r for r, _ in roles):
        return [_Style(_color(i), _linestyle(i), "o", label or None) for i, (_, label) in enumerate(roles)]
    out: list[_Style] = []
    per_role: Counter[str] = Counter()
    k = 0
    for role, label in roles:
        if role:
            j = per_role[role]
            per_role[role] += 1
            out.append(_Style(ROLE_COLORS[role], ("-", "--", ":", "-.")[j % 4],
                              _ROLE_MARKERS[j % len(_ROLE_MARKERS)], label or None))
        else:
            cycle = k // len(NEUTRAL_PALETTE)
            out.append(_Style(NEUTRAL_PALETTE[k % len(NEUTRAL_PALETTE)], ("-", "--", ":", "-.")[cycle % 4],
                              _ROLE_MARKERS[cycle % len(_ROLE_MARKERS)], label or None))
            k += 1
    return out


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
    dark = fig.kind == "spectrum"
    flags = str(fig.info.get("flags", "") or "").strip()
    if flags:
        f.text(0.008, 0.012, flags, ha="left", va="bottom", fontsize=9, fontweight="bold",
               color=FLAG_DARK if dark else FLAG_LIGHT, zorder=10)
    if not any(k in fig.info for k in ("version", "date", "serial")):
        return
    version = fig.info.get("version", "")
    parts = [f"EDS {version}".strip()]
    parts += [str(fig.info[k]) for k in ("date", "serial") if fig.info.get(k)]
    f.text(0.992, 0.012, " - ".join(parts), ha="right", va="bottom", fontsize=8,
           color=SA_MUTED if dark else INK3)


def _canvas_px(f: Any) -> tuple[float, float]:
    return f.get_figwidth() * f.dpi, f.get_figheight() * f.dpi


def _fit_text(t: Any, text: str, avail_px: float, renderer: Any, sizes: Sequence[float],
              max_lines: int) -> None:
    """Set ``t`` to ``text`` wrapped at word boundaries by *rendered* width: one line at
    ``sizes[0]`` if it fits, else ≤ ``max_lines`` lines at the first of ``sizes[1:]`` that
    fits; at the smallest size the last line is shortened with an ellipsis."""
    def width(s: str) -> float:
        t.set_text(s)
        return t.get_window_extent(renderer).width

    def wrap(words: list[str]) -> list[str]:
        lines: list[str] = []
        cur = ""
        for w in words:
            cand = f"{cur} {w}" if cur else w
            if cur and width(cand) > avail_px:
                lines.append(cur)
                cur = w
            else:
                cur = cand
        return [*lines, cur] if cur else lines

    words = text.split()
    t.set_fontsize(sizes[0])
    if width(text) <= avail_px or not words:
        t.set_text(text)
        return
    lines: list[str] = []
    for size in sizes[1:] or sizes:
        t.set_fontsize(size)
        lines = wrap(words)
        if len(lines) <= max_lines and all(width(ln) <= avail_px for ln in lines):
            t.set_text("\n".join(lines))
            return
    lines = lines[:max_lines]
    last = lines[-1] + " …"
    while len(last) > 2 and width(last) > avail_px:
        last = last[:-3].rstrip() + " …"
    t.set_text("\n".join([*lines[:-1], last]))


def _header(f: Any, fig: FigureSpec, subtitle: str = "", dark: bool = False) -> float:
    """Title (+ subtitle) fitted to the canvas width; returns the y (figure fraction) of
    the bottom of the header block."""
    renderer = f.canvas.get_renderer()
    w_px, h_px = _canvas_px(f)
    avail = (0.988 - 0.012) * w_px
    t = f.text(0.012, 0.965, "", ha="left", va="top", fontweight="bold", color=SA_TEXT if dark else INK)
    _fit_text(t, fig.title or fig.name, avail, renderer, (15, 13, 12, 11, 10), 2)
    bottom = t.get_window_extent(renderer).y0 / h_px
    if subtitle:
        s = f.text(0.012, bottom - 0.012, "", ha="left", va="top", color=SA_MUTED if dark else INK2)
        _fit_text(s, subtitle, avail, renderer, (9.5, 9.5, 8.5), 2)
        bottom = s.get_window_extent(renderer).y0 / h_px
    return bottom


def _fit_axes(f: Any, ax: Any, twin: Any = None, pad_px: float = 6.0) -> None:
    """Move/shrink ``ax`` (and its twin) horizontally so that the y tick labels and axis
    labels stay on the canvas (long tick labels such as ``−0.006`` must not push the
    axis label off the image)."""
    renderer = f.canvas.get_renderer()
    w_px, _ = _canvas_px(f)
    pos = ax.get_position()
    x0, x1 = pos.x0, pos.x1
    bb = ax.yaxis.get_tightbbox(renderer)
    if bb is not None and bb.x0 < pad_px:
        x0 += (pad_px - bb.x0) / w_px
    if twin is not None:
        bb2 = twin.yaxis.get_tightbbox(renderer)
        if bb2 is not None and bb2.x1 > w_px - pad_px:
            x1 -= (bb2.x1 - (w_px - pad_px)) / w_px
    if (x0, x1) != (pos.x0, pos.x1) and x1 - x0 > 0.3:
        for a in (ax, twin):
            if a is not None:
                a.set_position((x0, pos.y0, x1 - x0, pos.height))


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
    bottom of the graticule (marker labels sit at the top, next to the peaks)."""
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
    y_lo, y_hi = ax.get_ylim()
    for i, (x, y, text) in enumerate(markers):
        ax.plot([x], [y], marker="D", ms=8, color=mcol, mec=SA_BG if dark else SURFACE, mew=1.5,
                ls="none", zorder=7)
        label = str(i + 1) if numbered else text
        if not label:
            continue
        dy = 10 + 14 * (i % 3)
        if numbered and y_hi > y_lo and (y_hi - y) < 0.08 * (y_hi - y_lo):
            dy = -18 - 14 * (i % 3)                # marker at the top line: number below it
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
    from matplotlib.ticker import FuncFormatter, NullFormatter

    # mathtext is disabled (Turkish text safety), so log axes need plain-number labels
    plain = FuncFormatter(lambda v, _pos: f"{v:g}")
    if fig.logx:
        ax.set_xscale("log")
        ax.xaxis.set_major_formatter(plain)
        ax.xaxis.set_minor_formatter(NullFormatter())
    if fig.logy:
        ax.set_yscale("log")
        ax.yaxis.set_major_formatter(plain)
        ax.yaxis.set_minor_formatter(NullFormatter())
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
def _plot_series(ax: Any, s: Series, st: _Style, logx: bool) -> None:
    x, y = _arr(s.x, "x"), _arr(s.y, "y")
    yerr = _arr(s.yerr, "yerr") if s.yerr is not None else None
    color, ls, label = st.color, st.ls, st.label
    if s.style == "line":
        if yerr is not None:
            ax.errorbar(x, y, yerr=yerr, color=color, ls=ls, lw=2, elinewidth=1, capsize=3, label=label,
                        zorder=4)
        else:
            ax.plot(x, y, color=color, ls=ls, lw=2, label=label, zorder=4)
    elif s.style == "marker":
        ax.errorbar(x, y, yerr=yerr, fmt=st.marker, ms=7, color=color, mec=SURFACE, mew=1.2, elinewidth=1,
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
    bottom = _header(f, fig, _subtitle(fig))
    top = min(0.88, bottom - 0.035)
    ax = f.add_axes((0.075, 0.12, 0.9, top - 0.12))
    data = [s for s in fig.series if len(s.y)]
    for s, st in zip(fig.series, _series_styles(fig.series)):
        _plot_series(ax, s, st, fig.logx)
    _ref_lines(ax, fig)
    _annotate(ax, fig.markers)
    _apply_axes(ax, fig)
    if not data:
        _no_data(ax)
    _legend(ax, loc="best")
    _fit_axes(f, ax)


# ----------------------------------------------------------------------------- spectrum
def _fmt_level(v: float, unit: str) -> str:
    return f"{v:.1f} {unit}".strip() if math.isfinite(v) else "—"


def _parse_hz(text: Any) -> float | None:
    """``"935.5 MHz"`` → 935.5e6 (``None`` when not a plain frequency)."""
    m = _HZ_RE.match(str(text or ""))
    if not m:
        return None
    return float(m.group(1)) * _X_SCALES[m.group(2).lower()]


def _nice_divs() -> list[float]:
    return [m * 10.0 ** e for e in range(-4, 7) for m in (1.0, 2.0, 5.0)]


def sa_scale(hint: tuple[float, float] | None, top: float | None, unit: str) -> tuple[float, float, int]:
    """Instrument vertical scale ``(ref, per_div, n_div)`` of a spectrum screen.

    ``hint`` (the spec's ``ylim``) proposes the reference level (``hint[1]``) and the
    depth to show (``hint[1] − hint[0]``); without it the reference sits 5 dB above the
    trace peak ``top`` and the depth is 100 dB. The depth is covered by the smallest
    ``per_div × n_div`` with ``per_div`` ∈ {1, 2, 5, 10, 20} dB (1-2-5 steps for other
    units) and ``n_div`` ∈ {10, 8} (10 preferred). Ref is rounded up to 10 dB (to the
    division below 10 dB/div), never below the peak, and — for dBFS units with the trace
    below full scale — never above 0 dBFS.
    """
    db = unit.strip().lower().startswith("db")
    if hint is not None:
        want_top, depth = float(hint[1]), float(hint[1]) - float(hint[0])
    else:
        want_top, depth = (top + 5.0 if top is not None else 0.0), SA_DEFAULT_DEPTH_DB
    if top is not None:
        want_top = max(want_top, top)
    if not depth > 0:
        depth = SA_DEFAULT_DEPTH_DB
    divs = SA_DB_PER_DIV if db else _nice_divs()
    cands = sorted((d * n, -n, d) for d in divs for n in SA_Y_DIVS)
    _, neg_n, div = next((c for c in cands if c[0] >= depth * (1.0 - 1e-9)), cands[-1])
    step = 10.0 if db and div >= 10.0 else div
    ref = step * math.ceil(want_top / step - 1e-9)
    if unit.strip().lower().startswith("dbfs") and (top is None or top <= 0.0):
        ref = min(ref, 0.0)
    return ref + 0.0, div, -neg_n          # "+ 0.0" turns -0.0 into 0.0


def _sa_x_range(fig: FigureSpec, fx: np.ndarray, scale: float) -> tuple[float, float]:
    """Frequency axis: ``xlim``, else ``Center ± Span/2`` from the info bar when it matches
    the data, else the data extent."""
    if fig.xlim is not None:
        return float(fig.xlim[0]), float(fig.xlim[1])
    c, s = _parse_hz(fig.info.get("Center")), _parse_hz(fig.info.get("Span"))
    if fx.size and c is not None and s is not None and s > 0:
        lo, hi = (c - s / 2.0) / scale, (c + s / 2.0) / scale
        tol = 0.02 * (hi - lo)
        if fx.min() >= lo - tol and fx.max() <= hi + tol and (fx.max() - fx.min()) >= 0.5 * (hi - lo):
            return lo, hi
    if fx.size:
        return float(fx.min()), float(fx.max())
    return 0.0, 1.0


def _panel(f: Any, top: float, height: float) -> None:
    from matplotlib.patches import Rectangle

    f.add_artist(Rectangle((0.012, top - height), 0.976, height, transform=f.transFigure,
                           fc=SA_PANEL, ec=SA_EDGE, lw=0.8, zorder=1))


def _info_bar(f: Any, ordered: list[tuple[str, str]], bar_top: float) -> float:
    """Flow layout of key/value pairs (≤ 3 rows); returns the bar's bottom (fig fraction)."""
    renderer = f.canvas.get_renderer()
    fig_w_px, _ = _canvas_px(f)
    x_left, x_right, gap, kv_gap = 0.022, 0.978, 0.020, 0.006
    cells = []
    for k, v in ordered:
        key = f.text(0, 0, k, ha="left", va="center", fontsize=9, color=SA_MUTED, zorder=5)
        val = f.text(0, 0, v, ha="left", va="center", fontsize=10.5, color=SA_TEXT, fontweight="bold", zorder=5)
        cells.append((key, val, key.get_window_extent(renderer).width / fig_w_px,
                      val.get_window_extent(renderer).width / fig_w_px))
    rows: list[list[tuple[float, tuple[Any, Any, float, float]]]] = []
    cur: list[tuple[float, tuple[Any, Any, float, float]]] = []
    x = x_left
    for cell in cells:
        w = cell[2] + kv_gap + cell[3]
        if cur and x + w > x_right:
            rows.append(cur)
            cur, x = [], x_left
        cur.append((x, cell))
        x += w + gap
    if cur:
        rows.append(cur)
    for row in rows[3:]:                       # at most 3 rows; drop the rest
        for _, (key, val, _, _) in row:
            key.remove()
            val.remove()
    rows = rows[:3] or [[]]
    bar_h = 0.045 * len(rows)
    _panel(f, bar_top, bar_h)
    for r, row in enumerate(rows):
        yy = bar_top - 0.0225 - 0.045 * r
        for xx, (key, val, kw, _) in row:
            key.set_position((xx, yy))
            val.set_position((xx + kw + kv_gap, yy))
    return bar_top - bar_h


def _marker_table(f: Any, lines: list[str], top: float) -> float:
    """Marker readout as a table strip *outside* the graticule (like an analyzer's marker
    table): one or two columns, ≤ :data:`SA_MARKER_ROWS` rows. Returns its bottom."""
    renderer = f.canvas.get_renderer()
    fig_w_px, _ = _canvas_px(f)
    texts = [f.text(0, 0, ln, ha="left", va="center", fontsize=9.5, family=FONT_MONO, color=SA_TEXT, zorder=5)
             for ln in lines]
    widths = [t.get_window_extent(renderer).width / fig_w_px for t in texts]
    avail, gap = 0.956, 0.03
    cols = 2 if len(lines) > SA_MARKER_ROWS or (len(lines) > 2 and 2 * max(widths) + gap <= avail) else 1
    capacity = cols * SA_MARKER_ROWS
    if len(texts) > capacity:
        for t in texts[capacity - 1:]:
            t.remove()
        more = f.text(0, 0, f"… +{len(lines) - capacity + 1} işaretçi", ha="left", va="center", fontsize=9.5,
                      family=FONT_MONO, color=SA_MUTED, zorder=5)
        texts = [*texts[:capacity - 1], more]
    n_rows = math.ceil(len(texts) / cols)
    row_h = 0.036
    height = row_h * n_rows + 0.008
    _panel(f, top, height)
    col_w = avail / cols
    for i, t in enumerate(texts):
        r, c = i % n_rows, i // n_rows
        t.set_position((0.022 + c * col_w, top - 0.004 - row_h * (r + 0.5)))
    return top - height


def _draw_spectrum(f: Any, fig: FigureSpec) -> None:
    unit = _bracket_unit(fig.ylabel) or (fig.series[0].label if fig.series else "") or "dB"
    scale = _x_scale(fig.xlabel)
    xs = [_arr(s.x, "x") for s in fig.series]
    ys = [_arr(s.y, "y") for s in fig.series]
    fx, fy = _finite(*xs), _finite(*ys)

    x0, x1 = _sa_x_range(fig, fx, scale)
    if x1 <= x0:
        x0, x1 = x0 - 0.5, x0 + 0.5

    # markers (explicit, else peak search on trace 1)
    markers = list(fig.markers)
    if not markers and fy.size:
        x_t, y_t = xs[0], ys[0]
        ok = np.isfinite(x_t) & np.isfinite(y_t)
        if ok.any():
            k = int(np.argmax(np.where(ok, y_t, -np.inf)))
            markers = [(float(x_t[k]), float(y_t[k]), "")]
    peaks = _finite(fy, np.array([m[1] for m in markers], dtype=float))
    top = float(peaks.max()) if peaks.size else None
    hint = (float(fig.ylim[0]), float(fig.ylim[1])) if fig.ylim is not None else None
    ref, ydiv, ndiv = sa_scale(hint, top, unit)
    y1, y0 = ref, ref - ydiv * ndiv
    db = unit.lower().startswith("db")

    header_bottom = _header(f, fig, dark=True)

    # --- top info bar (Ref / scale are the ones actually drawn)
    items = dict(_info_items(fig, skip=("Start", "Stop")))
    items["Ref"] = f"{_fmt_tick(ref)} {unit}".strip()
    ordered = [(k, items.pop(k)) for k in _SA_INFO_ORDER if k in items]
    ordered.insert(1, ("Ölçek", f"{ydiv:g} {'dB' if db else unit}/div"))
    ordered += list(items.items())
    below = _info_bar(f, ordered, min(0.905, header_bottom - 0.012))

    # --- marker table (outside the screen: it never hides a marker or the trace)
    if markers:
        lines = []
        for i, (mx, my, text) in enumerate(markers):
            line = f"Mkr{i + 1}  {fmt_hz(mx * scale, 9):>15}  {_fmt_level(my, unit):>14}"
            lines.append(f"{line}  {text}" if text else line)
        below = _marker_table(f, lines, below - 0.008)

    # --- screen
    ax_top = below - 0.02
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
    ax.set_yticks([ref - k * ydiv for k in range(ndiv + 1)])
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
    if markers:
        _annotate(ax, markers, dark=True, numbered=True)
    _legend(ax, loc="upper left", facecolor=SA_PANEL, edgecolor=SA_EDGE, labelcolor=SA_TEXT)
    _fit_axes(f, ax)

    # --- bottom bar
    start = fig.info.get("Start") or fmt_hz(x0 * scale, 9)
    stop = fig.info.get("Stop") or fmt_hz(x1 * scale, 9)
    per_div = fmt_hz((x1 - x0) * scale / SA_X_DIV, 6) + "/div"
    _panel(f, 0.105, 0.055)
    for xx, ha, label, value in ((0.022, "left", "Start", start), (0.5, "center", "", per_div),
                                 (0.978, "right", "Stop", stop)):
        f.text(xx, 0.0775, f"{label}  {value}" if label else value, ha=ha, va="center", fontsize=10.5,
               color=SA_TEXT, fontweight="bold")


def _fmt_tick(v: float) -> str:
    """Graticule label: integer when integral (``-0`` → ``0``), else up to 3 decimals."""
    r = round(v)
    if abs(v - r) < 1e-9:
        return f"{int(r)}"
    return f"{v:.3f}".rstrip("0").rstrip(".")


def _tick_formatter() -> Any:
    from matplotlib.ticker import FuncFormatter

    return FuncFormatter(lambda v, _pos: _fmt_tick(v))


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
    bottom = _header(f, fig, _subtitle(fig, stats))
    top = min(0.84, bottom - 0.035)
    ax = f.add_axes((0.075, 0.12, 0.835, top - 0.12))
    if not sets:
        _no_data(ax)
        _apply_axes(ax, fig)
        _fit_axes(f, ax)
        return
    edges = _hist_edges(np.concatenate([d for d, _ in sets]), fig.logx)
    ax2 = ax.twinx()
    multi = len(sets) > 1
    styles = _series_styles([s for _, s in sets])
    roles = any(series_role(s)[0] for _, s in sets)
    for i, ((d, _s), st) in enumerate(zip(sets, styles)):
        color = st.color
        name = st.label or ("Histogram" if not multi else f"Seri {i + 1}")
        ax.hist(d, bins=edges, color=color, alpha=0.45 if multi else 0.85, edgecolor=SURFACE,
                linewidth=1.0, histtype="stepfilled" if multi else "bar", label=name, zorder=3)
        xs = np.sort(d)
        cdf = np.arange(1, xs.size + 1) / xs.size * 100.0
        cdf_color = color if multi else (INK2 if roles else _color(i + 1))
        ax2.step(np.concatenate([[xs[0]], xs]), np.concatenate([[0.0], cdf]), where="post",
                 color=cdf_color, lw=2, label=f"{name} — KDF", zorder=4)
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
    _fit_axes(f, ax, ax2)


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
    bottom = _header(f, fig, _subtitle(fig))
    top = min(0.83, bottom - 0.03)
    ax = f.add_axes((0.04, 0.07, 0.62, top - 0.07), projection="polar")
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

    for i, (s, st) in enumerate(zip(fig.series, _series_styles(fig.series))):
        th = np.deg2rad(_arr(s.x, "x"))
        r = ys[i]
        color, ls, label = st.color, st.ls, st.label
        yerr = _arr(s.yerr, "yerr") if s.yerr is not None else None
        if s.style == "marker":
            ax.errorbar(th, r, yerr=yerr, fmt=st.marker, ms=7, color=color, mec=SURFACE, mew=1.2,
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
