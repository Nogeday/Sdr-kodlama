"""Jury-facing measurement report (KTR quantitative specs).

The KTR jury's first reason for point loss is *missing quantitative specs*, so every
number in the report is traceable: value + unit (+ uncertainty) + full conditions +
method + measurement time + device serial + software version and git commit.

``write_report(results, out_dir)`` creates ``out_dir/<YYYYmmdd-HHMMSS>/`` containing::

    figures/*.png          every FigureSpec rendered (eds.measure.plots); paths recorded
                           in ``result.artifacts`` (relative to the report folder)
    tables/<spec_id>.csv   all raw rows of ``result.table``
    summary.md             Turkish report (header, main table, one section per measurement)
    summary.csv            one row per measurement (machine readable)
    results.json           {"meta": ..., "results": [MeasurementResult.to_dict(False) ...]}
    report.html            self-contained (inline CSS, PNGs as base64), print-friendly A4

How a number was obtained is never lost on the way to the jury:

* simulator data (``conditions['simulated']`` / backend ``sim``) → SİMÜLASYON banner (MD,
  HTML), ``simulated`` column in summary.csv and a red footer tag on every figure;
* quick mode (``conditions['quick']``) → "HIZLI MOD - KTR'de kullanılmaz" banner, header row,
  ``quick`` column / ``meta['quick']`` and the same figure footer tag;
* a result that must not be compared with its KTR target (``MeasurementResult.blockers``,
  ``extra['verdict_note']`` from :func:`eds.measure.targets.apply_targets`, or the
  :func:`eds.measure.targets.verdict_blockers` rules) shows ``KARŞILAŞTIRILMADI`` with the
  reason instead of GEÇTİ / KALDI.

Wide raw-data tables are split into column groups (constant columns listed once) so that
the printed A4 page never cuts columns off.

CSV files are UTF-8 *with BOM* (``utf-8-sig``) so that Excel shows Turkish characters;
read them with ``encoding="utf-8-sig"``. Plotting is optional: when matplotlib is missing
or a figure fails, the failure is logged, written into the report next to the figure
title, and the rest of the report is still produced.
"""

from __future__ import annotations

import base64
import csv
import dataclasses
import html
import json
import logging
import math
import numbers
import re
import subprocess
from collections import Counter
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import numpy as np

from .. import __version__
from ..core.errors import BackendUnavailable
from ..core.paths import repo_root
from ..core.timeutil import iso_utc, now_ns, stamp
from ..core.units import fmt_hz
from .base import MeasurementResult
from .base import jsonable as _jsonable
from .targets import (
    NO_VERDICT_SUFFIX,
    TARGET_NOTE_KEY,
    TARGET_REQUIRE_KEY,
    VERDICT_NOTE_KEY,
    resolve_targets_path,
    verdict_blockers,
)

log = logging.getLogger(__name__)

DEFAULT_TITLE = "EDS RX - KTR Nicel Spesifikasyon Ölçüm Raporu"
SIM_BANNER = "SİMÜLASYON VERİSİ - donanım ölçümü değildir"
QUICK_TAG = "HIZLI MOD - KTR'de kullanılmaz"
QUICK_BANNER = f"{QUICK_TAG} (--quick: azaltılmış nokta sayısı / ölçüm süresi; hedefle karşılaştırılmaz)"
SIM_TAG = "SİMÜLASYON"
EXT_PROFILE = "ad9364_pluto_ext"
EXT_BANNER = ("SPESİFİKASYON DIŞI MOD - ad9364_pluto_ext profili kullanıldı "
              "(AD9363 resmî sınırları dışında; spesifikasyon dışı mod)")
RAW_ROWS_IN_DOC = 50
UNKNOWN = "unknown"

PASS, FAIL, NO_TARGET, SKIPPED, ERROR = "GEÇTİ", "KALDI", "HEDEF YOK", "ATLANDI", "HATA"
NOT_COMPARED = "KARŞILAŞTIRILMADI"
VERDICTS: tuple[str, ...] = (PASS, FAIL, NOT_COMPARED, NO_TARGET, SKIPPED, ERROR)
_BADGE_CLASS = {PASS: "pass", FAIL: "fail", NOT_COMPARED: "nc", NO_TARGET: "none", SKIPPED: "skip", ERROR: "err"}

SUMMARY_CSV_COLUMNS: tuple[str, ...] = (
    "spec_id", "title", "value", "unit", "target_op", "target", "passed", "status", "conditions",
    "method", "verdict", "uncertainty", "summary", "error", "t_start_utc", "duration_s",
    "device_name", "device_serial", "eds_version", "git_commit", "quick", "simulated", "verdict_note",
)

_DASH = "—"
_DB_UNITS = frozenset({"db", "dbm", "dbfs", "dbc", "dbw", "dbi", "db/hz", "dbm/hz", "dbfs/hz", "dbc/hz"})
_ANGLE_UNITS = frozenset({"°", "deg", "derece"})
_HIDDEN_EXTRA = frozenset({"traceback", TARGET_NOTE_KEY, VERDICT_NOTE_KEY, TARGET_REQUIRE_KEY})

# condition keys → Turkish labels (full table) and short labels (main table)
_COND_LABELS: dict[str, str] = {
    "fc_hz": "Merkez frekansı (fc)", "fs_hz": "Örnekleme hızı (fs)", "bw_hz": "Analog bant genişliği",
    "rbw_hz": "Çözünürlük bant genişliği (RBW)", "gain_db": "Kazanç", "gain_mode": "Kazanç modu",
    "nfft": "FFT boyu (N)", "averages": "Ortalama sayısı", "window": "Pencere",
    "temperature_c": "Sıcaklık", "backend": "Backend", "serial": "Seri no", "profile": "Cihaz profili",
    "ppm_hw": "PPM (donanım düzeltmesi)", "ppm_hw_via": "PPM düzeltme yolu", "source": "Sinyal kaynağı",
    "pfa": "Yanlış alarm olasılığı (Pfa)", "calibrated": "Güç kalibrasyonlu", "level_ref": "Seviye referansı",
    "duration_s": "Süre", "overlap": "Örtüşme", "span_hz": "Açıklık (span)", "start_hz": "Başlangıç frekansı",
    "stop_hz": "Bitiş frekansı", "step_hz": "Adım", "repeats": "Tekrar sayısı", "baseline_m": "Taban hattı",
    "quick": "Hızlı mod (--quick)", "simulated": "Simülasyon verisi",
}
_SHORT_KEYS: tuple[tuple[str, str], ...] = (
    ("fc_hz", "fc"), ("fs_hz", "fs"), ("rbw_hz", "RBW"), ("gain_db", "G"), ("averages", "ort."),
    ("source", "kaynak"), ("temperature_c", "T"),
)
_SHORT_MAX = 4
# Conditions that define each spec's number (the main table must show *these*, not a generic
# fc/fs list: e.g. the swept range of scan_speed, the stream duration of max_rate).
_SPEC_SHORT_KEYS: dict[str, tuple[tuple[str, str], ...]] = {
    "sensitivity": (("fc_hz", "fc"), ("rbw_hz", "RBW"), ("pfa", "Pfa"), ("gain_db", "G"),
                    ("trials", "deneme/seviye")),
    "dynamic_range": (("fc_hz", "fc"), ("fs_hz", "fs"), ("rbw_hz", "RBW"), ("gain_db", "G")),
    "inst_bw": (("fc_hz", "fc"), ("configs", "fs / BW"), ("rbw_hz", "RBW"), ("gain_db", "G")),
    "scan_speed": (("start_hz", "aralık"), ("fs_hz", "fs"), ("rbw_hz", "RBW"), ("averages", "ort."),
                   ("repeats", "tekrar")),
    "freq_resolution": (("fc_hz", "fc"), ("rbw_hz", "RBW"), ("window", "pencere"), ("nfft", "N")),
    "df_rms": (("f_target_hz", "f"), ("baseline_m", "d"), ("angles_deg", "θ"), ("repeats", "tekrar"),
               ("coherent", "koherent")),
    "image_rejection": (("fcs_hz", "fc"), ("fs_hz", "fs"), ("offsets_frac", "δ/fs"), ("gain_db", "G")),
    "max_rate": (("duration_s", "süre/hız"), ("buffer_size", "buffer"), ("interface", "arayüz"),
                 ("headline_mode", "mod")),
    "latency": (("headline_mode", "mod"), ("n_events", "olay"), ("transport", "taşıma"), ("fs_hz", "fs")),
}
_SPEC_SHORT_MAX = 5
_SHORT_HZ_DIGITS = {"rbw_hz": 4}
_DENSITY_SUFFIXES: tuple[tuple[str, str], ...] = (
    ("_dbm_hz", "dBm/Hz"), ("_dbfs_hz", "dBFS/Hz"), ("_dbc_hz", "dBc/Hz"), ("_db_hz", "dB/Hz"),
)
# key token → display unit (the value is already in that unit)
_TOKEN_UNITS: dict[str, str] = {
    "dbm": "dBm", "dbfs": "dBFS", "dbc": "dBc", "db": "dB", "deg": "°", "pct": "%", "ms": "ms", "us": "µs",
    "m": "m", "ppm": "ppm", "sps": "S/s", "msps": "MS/s", "ghz": "GHz", "mhz": "MHz", "khz": "kHz",
    "mb": "MB", "bins": "bin",
}
# tokens with special formatting: hz (scaled frequency), s / ns (durations), c (temperature)
_SPECIAL_TOKENS = frozenset({"hz", "s", "ns", "c"})
# statistic / qualifier tokens that may follow the unit token (``snr_db_mean``, ``ppm_estimate``)
_QUALIFIERS = frozenset({
    "mean", "avg", "std", "min", "max", "median", "rms", "p50", "p90", "p95", "p99", "est", "estimate",
    "abs", "worst", "best", "typ", "nominal", "measured", "meas", "raw", "corrected", "total", "ci95",
    "lo", "hi", "limit", "floor", "peak", "actual", "at",
})
_PER_UNITS: dict[str, str] = {"s": "s", "db": "dB", "hz": "Hz", "m": "m", "ms": "ms", "c": "°C", "deg": "°"}
_PER_RE = re.compile(r"^(.*)_per_(s|db|hz|m|ms|c|deg)$")


# ============================================================================ formatting
def _py(v: Any) -> Any:
    """numpy scalar/array → Python scalar/list (anything else unchanged)."""
    if isinstance(v, np.generic):
        return v.item()
    if isinstance(v, np.ndarray):
        return v.tolist()
    return v


def _finite(v: Any) -> bool:
    return isinstance(v, numbers.Real) and not isinstance(v, bool) and math.isfinite(float(v))


def _is_db(unit: str) -> bool:
    return unit.strip().lower() in _DB_UNITS


def _fixed(v: float, decimals: int) -> str:
    s = f"{v:.{max(0, decimals)}f}"
    return s.lstrip("-") if float(s) == 0.0 else s


def _smart_decimals(v: float, sig: int = 3, max_sig: int = 4) -> int:
    """Decimals for ≥ ``sig`` significant digits, extended up to ``max_sig`` when that
    represents the value exactly (61.44 → 2, 4.0 → 2, 18.23456 → 1, 44.467 → 1: a value
    rounded upstream to 5 digits is not shown with spurious precision)."""
    if v == 0.0:
        return sig - 1
    mag = math.floor(math.log10(abs(v)))
    for s in range(sig, max_sig + 1):
        d = s - 1 - mag
        if math.isclose(round(v, d), v, rel_tol=1e-9, abs_tol=0.0):
            return max(0, d)
    return max(0, sig - 1 - mag)


def _default_decimals(v: float, unit: str) -> int | None:
    """Fixed decimals for ``unit`` or None for scientific notation."""
    u = unit.strip()
    if _is_db(u):
        return 1
    if u in _ANGLE_UNITS:              # ≥ 2 significant digits for small angles (0.0028 °)
        d = 2 if abs(v) < 10.0 else 1
        return max(d, 1 - math.floor(math.log10(abs(v)))) if 0.0 < abs(v) < 1.0 else d
    if u == "°C":
        return 1
    if v != 0.0 and (abs(v) < 1e-3 or abs(v) >= 1e12):
        return None
    return _smart_decimals(v)


def _uncertainty_decimals(u: float) -> int:
    """GUM/PDG style: 1 significant digit, 2 when the leading digit is 1."""
    mag = math.floor(math.log10(u))
    lead = int(u / 10.0 ** mag + 1e-9)
    return max(0, (2 if lead == 1 else 1) - 1 - mag)


def _uncertainty_unit(unit: str) -> str:
    return "dB" if _is_db(unit) else unit.strip()


def _non_finite_text(value: Any) -> str:
    if _is_number(value) and math.isinf(float(value)):
        return "+∞" if float(value) > 0 else "-∞"
    return _DASH


def _is_number(v: Any) -> bool:
    return isinstance(v, numbers.Real) and not isinstance(v, bool)


def format_number(value: float | None, unit: str = "", uncertainty: float | None = None) -> str:
    """Number only (no unit) with unit-appropriate digits, e.g. ``"-101.3 ± 0.3"``."""
    if not _finite(value):
        return _non_finite_text(value)
    v = float(value)
    if _finite(uncertainty) and float(uncertainty) > 0:
        d = _uncertainty_decimals(float(uncertainty))
        return f"{_fixed(v, d)} ± {_fixed(float(uncertainty), d)}"
    d = _default_decimals(v, unit)
    return f"{v:.2e}" if d is None else _fixed(v, d)


def format_uncertainty(uncertainty: float | None, unit: str = "") -> str:
    """``"± 0.3 dB"`` (a dBm/dBFS value has its uncertainty in dB); empty when absent."""
    if not _finite(uncertainty) or float(uncertainty) <= 0:
        return ""
    u = float(uncertainty)
    uu = _uncertainty_unit(unit)
    return f"± {_fixed(u, _uncertainty_decimals(u))}" + (f" {uu}" if uu else "")


def format_value(value: float | None, unit: str = "", uncertainty: float | None = None) -> str:
    """Value + unit with sensible significant digits.

    ``-101.3 dBm``, ``18.2 MHz``, ``4.00 MS/s``, ``12.5 ms``, ``0.82 °``; with an
    uncertainty the value is rounded to the uncertainty's decimal place:
    ``-101.3 dBm ± 0.3 dB``. Missing/NaN → ``"—"``.
    """
    unit = unit.strip()
    if not _finite(value):
        txt = _non_finite_text(value)
        return f"{txt} {unit}".strip() if txt != _DASH else txt
    v = float(value)
    if _finite(uncertainty) and float(uncertainty) > 0:
        u = float(uncertainty)
        d = _uncertainty_decimals(u)
        head = f"{_fixed(v, d)} {unit}".strip()
        uu = _uncertainty_unit(unit)
        return f"{head} ± {_fixed(u, d)}" + (f" {uu}" if uu else "")
    return f"{format_number(v, unit)} {unit}".strip()


def _fmt_duration(s: float) -> str:
    a = abs(s)
    if a == 0.0 or a >= 1.0:
        return format_value(s, "s")
    if a >= 1e-3:
        return format_value(s * 1e3, "ms")
    if a >= 1e-6:
        return format_value(s * 1e6, "µs")
    return format_value(s * 1e9, "ns")


def _compact_json(v: Any, limit: int | None = None) -> str:
    s = json.dumps(_jsonable(v), ensure_ascii=False, default=str, separators=(", ", ": "))
    if limit is not None and len(s) > limit:
        return s[: limit - 1] + "…"
    return s


def _round_floats(v: Any) -> Any:
    """Display copy with floats at 6 significant digits (never used for CSV / JSON files)."""
    if isinstance(v, float):
        return float(f"{v:.6g}") if math.isfinite(v) else None
    if isinstance(v, Mapping):
        return {str(k): _round_floats(x) for k, x in v.items()}
    if isinstance(v, (list, tuple)):
        return [_round_floats(x) for x in v]
    return v


def _display_json(v: Any, limit: int | None) -> str:
    return _compact_json(_round_floats(_jsonable(v)), limit)


def _truncate(s: str, limit: int | None) -> str:
    return s if limit is None or len(s) <= limit else s[: limit - 1] + "…"


def fmt_sps(v: float, digits: int = 6) -> str:
    """Sample rate: ``4e6`` → ``"4 MS/s"`` (a sample rate is not a bandwidth: never "MHz")."""
    a = abs(v)
    for div, unit in ((1e9, "GS/s"), (1e6, "MS/s"), (1e3, "kS/s")):
        if a >= div:
            return f"{v / div:.{digits}g} {unit}"
    return f"{v:.{digits}g} S/s"


def _key_tokens(key: str) -> list[str]:
    """Lower-case tokens of a key without trailing statistic qualifiers."""
    tokens = key.lower().split("_")
    while len(tokens) > 1 and tokens[-1] in _QUALIFIERS:
        tokens.pop()
    return tokens


def _unit_token(key: str) -> str:
    """The key's unit token (``"db"``, ``"hz"``, ``"s"`` …) or ``""``."""
    tokens = _key_tokens(key)
    last = tokens[-1] if tokens else ""
    if len(tokens) == 1 and last not in ("hz", "ppm", "temperature"):
        return ""
    return last if last in _TOKEN_UNITS or last in _SPECIAL_TOKENS else ""


def _is_rate_key(k: str) -> bool:
    """``fs_hz``, ``fs_max_hz``, ``rates_hz``, ``sample_rate_hz`` … are sample rates (S/s)."""
    tokens = k.split("_")
    if len(tokens) < 2 or tokens[-1] != "hz":
        return False
    head = tokens[:-1]
    return "fs" in head or head[-1] == "rates" or head[-2:] == ["sample", "rate"]


def _has_unit(key: str) -> bool:
    k = key.lower()
    return bool(_unit_token(k) or _PER_RE.match(k) or _is_rate_key(k) or k.endswith("_hz_per_s")
                or any(k.endswith(sfx) for sfx, _ in _DENSITY_SUFFIXES))


def _is_scalar(v: Any) -> bool:
    v = _py(v)
    return v is None or isinstance(v, (bool, str)) or _is_number(v)


def _format_mapping(key: str, value: Mapping[Any, Any], limit: int | None, hz_digits: int) -> str:
    """Flat mapping → ``"k: v; k2: v2"`` with every value formatted by its own key's unit, or
    by the parent key's unit (``timing_share_pct = {"tune": 0.42}`` → ``tune: 0.420 %``)."""
    items = list(value.items())
    flat = all(_is_scalar(x) or (isinstance(_py(x), (list, tuple)) and all(_is_scalar(y) for y in _py(x)))
               for _, x in items)
    if not items or len(items) > 12 or not flat:
        return _display_json(value, limit)
    parts = []
    for k2, v2 in items:
        sub = str(k2) if _has_unit(str(k2)) else key
        parts.append(f"{k2}: {format_condition(sub, v2, limit=None, hz_digits=hz_digits)}")
    return _truncate("; ".join(parts), limit)


def format_condition(key: str, value: Any, *, limit: int | None = 300, hz_digits: int = 9,
                     unit: str | None = None) -> str:
    """Format a condition / extra metric using its key's unit suffix
    (``*_hz`` → ``915 MHz``, ``fs_hz`` → ``4 MS/s``, ``*_s`` → ``12.5 ms``, ``*_db`` →
    ``40.0 dB``, ``*_c`` → ``°C``, ``*_per_db`` → ``/dB``, ``snr_db_mean`` → ``dB`` …).
    ``hz_digits`` = significant digits of frequencies (9 keeps commanded values exact);
    ``unit`` = unit of a key that carries none in its name (e.g. from a ``<key>_unit``
    sibling). Flat mappings are formatted item by item."""
    value = _py(value)
    if value is None:
        return _DASH
    if isinstance(value, bool):
        return "evet" if value else "hayır"
    if isinstance(value, Mapping):
        return _format_mapping(key, value, limit, hz_digits)
    if isinstance(value, (list, tuple)):
        if value and all(_is_number(_py(x)) for x in value) and len(value) <= 8:
            return ", ".join(format_condition(key, x, hz_digits=hz_digits, unit=unit) for x in value)
        if value and all(isinstance(_py(x), str) for x in value) and len(value) <= 12:
            return _truncate(", ".join(str(x) for x in value), limit)
        return _display_json(value, limit)
    if isinstance(value, str):
        return _truncate(value, limit)
    if not _is_number(value):
        return str(value)
    v = float(value)
    if not math.isfinite(v):
        return _non_finite_text(v)
    k = key.lower()
    for suffix, dens in _DENSITY_SUFFIXES:          # before "_hz": "*_dbm_hz" is a density, not a frequency
        if k.endswith(suffix):
            return format_value(v, dens)
    if k.endswith("_hz_per_s"):
        return f"{fmt_hz(v, 6)}/s"
    m = _PER_RE.match("_".join(_key_tokens(k)))
    if m:                                           # "_per_db" / "_per_s" before "_db" / "_s"
        tok = m.group(1).split("_")[-1]
        num = "Hz" if tok == "hz" else ("s" if tok == "s" else _TOKEN_UNITS.get(tok, ""))
        return f"{format_number(v, num)} {num}/{_PER_UNITS[m.group(2)]}"
    if _is_rate_key(k):
        return fmt_sps(v, hz_digits)
    tok = _unit_token(k)
    if tok == "hz":
        return fmt_hz(v, hz_digits)
    if tok == "ns":
        return iso_utc(int(v)) if v > 1e15 else _fmt_duration(v * 1e-9)
    if tok == "s":
        return _fmt_duration(v)
    if tok == "c" or k == "temperature":
        return format_value(v, "°C")
    u = _TOKEN_UNITS.get(tok) or (unit or "").strip()
    if u:
        if isinstance(value, numbers.Integral) and not _is_db(u) and u not in _ANGLE_UNITS:
            return f"{int(value)} {u}"
        return format_value(v, u)
    if isinstance(value, numbers.Integral):
        return str(int(value))
    return format_number(v)


def _has_target(r: MeasurementResult) -> bool:
    return _finite(r.target) and r.target_op in ("<=", ">=")


def no_verdict_reason(r: MeasurementResult) -> str:
    """Why ``r`` is shown as KARŞILAŞTIRILMADI instead of GEÇTİ / KALDI ("" = comparable).

    Collects ``extra['verdict_note']`` (written by :func:`eds.measure.targets.apply_targets`,
    includes unmet target conditions), the measurement's own ``blockers`` and, for a
    result with a target, the :func:`eds.measure.targets.verdict_blockers` rules (quick
    mode, non-real-time simulator, no coherence, unit/calibration mismatch) — so a report
    never shows a verdict for such a number even if the targets were applied elsewhere.
    Only ``ok`` results with a finite value can be "not compared" (else ATLANDI / HATA).
    """
    if r.status != "ok" or not _finite(r.value):
        return ""
    parts: list[str] = []
    note = str(r.extra.get(VERDICT_NOTE_KEY) or "").strip()
    if note:
        parts.append(note[: -len(NO_VERDICT_SUFFIX)].strip() if note.endswith(NO_VERDICT_SUFFIX) else note)
    derived = [str(b) for b in r.blockers]
    if _has_target(r):
        from . import specs  # noqa: F401  (spec units for the unit-mismatch rule)

        derived += verdict_blockers(r)
    for b in derived:
        b = b.strip()
        if b and not any(b in p for p in parts):
            parts.append(b)
    if not parts and _has_target(r) and r.passed is None:
        parts.append("hedef değerlendirilmedi")
    return "; ".join(parts)


def verdict(r: MeasurementResult) -> str:
    """Report status word: GEÇTİ / KALDI / KARŞILAŞTIRILMADI / HEDEF YOK / ATLANDI / HATA."""
    if r.status == "skipped":
        return SKIPPED
    if r.status != "ok" or not _finite(r.value):
        return ERROR
    if no_verdict_reason(r):
        return NOT_COMPARED
    if r.passed is True:
        return PASS
    if r.passed is False:
        return FAIL
    return NO_TARGET


def is_quick(r: MeasurementResult) -> bool:
    """Result produced in quick mode (``--quick``: fewer points / shorter durations)."""
    return r.conditions.get("quick") is True


def is_simulated(r: MeasurementResult, default: bool = False) -> bool:
    """Result produced on the simulator (``conditions['simulated']``, else backend ``sim``
    of the result or of any device member; ``default`` when nothing says)."""
    flag = r.conditions.get("simulated")
    if isinstance(flag, (bool, np.bool_)):
        return bool(flag)
    backends = {str(r.conditions.get("backend") or "")}
    if r.device:
        backends |= {d["backend"] for d in _walk_devices(r.device)}
    backends.discard("")
    backends.discard("group")
    return "sim" in backends if backends else default


def _target_text(r: MeasurementResult, with_unit: bool = False) -> str:
    if not _finite(r.target) or r.target_op not in ("<=", ">="):
        return _DASH
    sym = "≤" if r.target_op == "<=" else "≥"
    num = format_number(float(r.target), r.unit)
    return f"{sym} {num}" + (f" {r.unit}" if with_unit and r.unit else "")


def _summarize_numbers(key: str, vals: Sequence[Any]) -> str:
    nums = [float(_py(v)) for v in vals]
    if len(nums) <= 3:
        return ", ".join(format_condition(key, v, hz_digits=6) for v in nums)
    return (f"{format_condition(key, min(nums), hz_digits=6)} … {format_condition(key, max(nums), hz_digits=6)}"
            f" ({len(nums)})")


def _short_value(key: str, c: Mapping[str, Any]) -> str:
    v = _py(c[key])
    if key == "start_hz" and _is_number(v) and _is_number(_py(c.get("stop_hz"))):
        return f"{fmt_hz(float(v), 6)} – {fmt_hz(float(_py(c['stop_hz'])), 6)}"
    if key == "configs" and isinstance(v, (list, tuple)) and v and all(
            isinstance(_py(x), (list, tuple)) and len(_py(x)) == 2 for x in v):
        if len(v) > 2:
            return f"{len(v)} yapılandırma"
        return ", ".join(f"{fmt_sps(float(_py(x)[0]), 4)} / {fmt_hz(float(_py(x)[1]), 4)}" for x in v)
    if isinstance(v, (list, tuple)) and v and all(_is_number(_py(x)) for x in v):
        return _summarize_numbers(key, v)
    return format_condition(key, v, limit=40, hz_digits=_SHORT_HZ_DIGITS.get(key, 6))


def short_conditions(r: MeasurementResult) -> str:
    """The conditions that define the number, for the main table: the spec's own keys
    (``scan_speed``: swept range; ``max_rate``: stream duration, buffer, interface …),
    else ``fc``/``fs``/RBW/gain (at most four), e.g. ``fc 915 MHz; fs 4 MS/s; RBW 1.465 kHz``."""
    c = r.conditions or {}
    keys = _SPEC_SHORT_KEYS.get(r.spec_id)
    if keys and any(k in c for k, _ in keys):
        parts = [f"{label} {_short_value(k, c)}" for k, label in keys if k in c]
        return "; ".join(parts[:_SPEC_SHORT_MAX])
    parts = [f"{label} {_short_value(k, c)}" for k, label in _SHORT_KEYS if k in c]
    if not parts:
        skip = {"backend", "serial", "profile", "quick", "simulated"}
        parts = [f"{k} {format_condition(k, v, limit=40, hz_digits=6)}" for k, v in c.items() if k not in skip]
    return "; ".join(parts[:_SHORT_MAX]) or _DASH


def console_table(results: Sequence[MeasurementResult]) -> str:
    """Plain-text table for the terminal (no ANSI colours, safe for logs)."""
    head = ("#", "Parametre", "Ölçülen", "Birim", "Hedef", "Durum")
    rows = [(str(i), r.title or r.spec_id, format_number(r.value, r.unit, r.uncertainty), r.unit or "",
             _target_text(r), verdict(r)) for i, r in enumerate(results, 1)]
    widths = [max(len(h), *(len(row[c]) for row in rows)) if rows else len(h) for c, h in enumerate(head)]
    right = {0, 2, 4}

    def line(cells: Sequence[str]) -> str:
        return "  ".join(c.rjust(w) if i in right else c.ljust(w)
                         for i, (c, w) in enumerate(zip(cells, widths))).rstrip()

    out = [line(head), "  ".join("-" * w for w in widths)]
    out += [line(row) for row in rows]
    counts = Counter(verdict(r) for r in results)
    out.append("")
    out.append("Toplam: " + str(len(results)) + "  |  "
               + "  ".join(f"{v}: {counts.get(v, 0)}" for v in VERDICTS))
    return "\n".join(out)


# ============================================================================ provenance
def git_revision(root: Path | None = None) -> tuple[str, bool | None]:
    """``(short_sha, dirty)`` of the checkout, or ``("unknown", None)``.

    ``dirty`` considers tracked files only (report folders are untracked by design).
    """
    root = root or repo_root()
    if not (root / ".git").exists():
        log.info("git deposu bulunamadı (%s); commit 'unknown' yazılacak", root)
        return UNKNOWN, None
    try:
        sha = subprocess.run(["git", "rev-parse", "--short", "HEAD"], cwd=root, capture_output=True,
                             text=True, timeout=10, check=True).stdout.strip()
        st = subprocess.run(["git", "status", "--porcelain", "--untracked-files=no"], cwd=root,
                            capture_output=True, text=True, timeout=10, check=True).stdout
    except (OSError, subprocess.SubprocessError) as e:
        log.warning("git commit okunamadı (%s); commit 'unknown' yazılacak", e)
        return UNKNOWN, None
    return (sha or UNKNOWN), bool(st.strip())


_DEV_FIELDS = ("name", "backend", "serial", "profile", "firmware", "driver", "hardware", "uri")


def _device_entry(d: Mapping[str, Any]) -> dict[str, str]:
    info = d.get("info") if isinstance(d.get("info"), Mapping) else d
    return {k: ("" if info.get(k) is None else str(info.get(k))) for k in _DEV_FIELDS}


def _walk_devices(d: Mapping[str, Any]) -> list[dict[str, str]]:
    out = [_device_entry(d)]
    for m in d.get("members", []) or []:
        if isinstance(m, Mapping):
            out.extend(_walk_devices(m))
    return out


def _collect_devices(results: Sequence[MeasurementResult], app: Any,
                     meta: Mapping[str, Any]) -> list[dict[str, str]]:
    """Runtime identities (meta, results) first, then the config; merged by name/serial."""
    found: list[dict[str, str]] = []
    for d in meta.get("devices", []) or []:
        if isinstance(d, Mapping):
            found.extend(_walk_devices(d))
    for r in results:
        if r.device:
            found.extend(_walk_devices(r.device))
    if app is not None:
        for dc in getattr(app, "devices", []) or []:
            args = getattr(dc, "args", {}) or {}
            found.append({"name": dc.name, "backend": dc.backend, "profile": dc.profile, "serial": "",
                          "firmware": "", "driver": str(args.get("driver", "")),
                          "hardware": "", "uri": ", ".join(f"{k}={v}" for k, v in args.items())})
    merged: list[dict[str, str]] = []
    for d in found:
        key = d["name"] or d["serial"]
        if not key:
            continue
        hit = next((m for m in merged if (m["name"] or m["serial"]) == key), None)
        if hit is None:
            merged.append(dict(d))
        else:
            for k, v in d.items():
                if v and not hit[k]:
                    hit[k] = v
    return merged


def _flags(results: Sequence[MeasurementResult], devices: Sequence[Mapping[str, str]]) -> tuple[bool, bool]:
    backends = {d.get("backend", "") for d in devices}
    profiles = {d.get("profile", "") for d in devices}
    for r in results:
        backends.add(str(r.conditions.get("backend", "")))
        profiles.add(str(r.conditions.get("profile", "")))
    sim = "sim" in backends or any(r.conditions.get("simulated") is True for r in results)
    return sim, EXT_PROFILE in profiles


@dataclass
class _FigureEntry:
    title: str
    path: str = ""              # relative to report folder
    error: str = ""


@dataclass
class _Doc:
    """Everything the writers need (computed once)."""

    title: str
    t_ns: int
    generated: str
    version: str
    commit: str
    dirty: bool | None
    config_path: str
    targets_path: str
    devices: list[dict[str, str]]
    sim: bool
    ext: bool
    user_meta: dict[str, Any]
    quick_ids: list[str] = field(default_factory=list)   # spec ids measured in quick mode
    n_results: int = 0
    figures: dict[int, list[_FigureEntry]] = field(default_factory=dict)
    tables: dict[int, str] = field(default_factory=dict)

    @property
    def commit_text(self) -> str:
        if self.commit == UNKNOWN or self.dirty is None:
            return self.commit
        return f"{self.commit} (+ yerel değişiklikler)" if self.dirty else self.commit

    @property
    def commit_tag(self) -> str:
        return f"{self.commit}-dirty" if self.dirty else self.commit

    @property
    def quick(self) -> bool:
        return bool(self.quick_ids)

    @property
    def banners(self) -> list[tuple[str, str]]:
        out = []
        if self.sim:
            out.append(("sim", SIM_BANNER))
        if self.quick:
            partial = len(self.quick_ids) < self.n_results
            out.append(("quick", QUICK_BANNER + (f" — hızlı modda ölçülenler: {', '.join(self.quick_ids)}"
                                                 if partial else "")))
        if self.ext:
            out.append(("ext", EXT_BANNER))
        return out

    @property
    def mode_text(self) -> str:
        if not self.n_results:
            return _DASH
        if not self.quick:
            return "tam (--quick kullanılmadı)"
        if len(self.quick_ids) < self.n_results:
            return f"KARIŞIK — hızlı modda: {', '.join(self.quick_ids)} ({QUICK_TAG})"
        return "HIZLI (--quick): azaltılmış nokta sayısı / ölçüm süresi — KTR'de kullanılmaz"


def figure_flags(r: MeasurementResult, doc_sim: bool = False) -> str:
    """Footer tag stamped on every figure of ``r`` (``""`` for a full hardware measurement)."""
    parts = []
    if is_simulated(r, doc_sim):
        parts.append(SIM_TAG)
    if is_quick(r):
        parts.append(QUICK_TAG)
    return " · ".join(parts)


# ============================================================================ file helpers
def _safe_name(name: str, fallback: str) -> str:
    s = re.sub(r"[^A-Za-z0-9._-]+", "_", name).strip("._")
    return s or fallback


def _unique(name: str, used: set[str]) -> str:
    cand, i = name, 2
    while cand in used:
        cand, i = f"{name}_{i}", i + 1
    used.add(cand)
    return cand


def _make_report_dir(out_dir: Path, t_ns: int) -> Path:
    out_dir.mkdir(parents=True, exist_ok=True)
    base, i = stamp(t_ns), 1
    while True:
        p = out_dir / (base if i == 1 else f"{base}-{i}")
        try:
            p.mkdir()
            return p
        except FileExistsError:
            i += 1


def _csv_cell(v: Any) -> str:
    v = _py(v)
    if v is None:
        return ""
    if isinstance(v, bool):
        return "true" if v else "false"
    if isinstance(v, numbers.Integral):
        return str(int(v))
    if isinstance(v, numbers.Real):
        f = float(v)
        return repr(f) if math.isfinite(f) else ""
    if isinstance(v, (Mapping, list, tuple)):
        return _compact_json(v)
    return str(v)


def _table_columns(rows: Sequence[Mapping[str, Any]]) -> list[str]:
    cols: list[str] = []
    seen: set[str] = set()
    for row in rows:
        for k in row:
            if str(k) not in seen:
                seen.add(str(k))
                cols.append(str(k))
    return cols


def _write_csv(path: Path, columns: Sequence[str], rows: Iterable[Sequence[str]]) -> None:
    with path.open("w", encoding="utf-8-sig", newline="") as f:
        w = csv.writer(f)
        w.writerow(columns)
        w.writerows(rows)


# ============================================================================ writers
def _render_figures(results: Sequence[MeasurementResult], rdir: Path, doc: _Doc) -> None:
    from . import plots

    used: set[str] = set()
    backend_error = ""
    for i, r in enumerate(results):
        entries: list[_FigureEntry] = []
        serial = _result_serial(r)
        date = iso_utc(r.t_start_ns) if r.t_start_ns > 0 else doc.generated
        flags = figure_flags(r, doc.sim)
        for j, fig in enumerate(r.figures):
            entry = _FigureEntry(title=fig.title or fig.name)
            entries.append(entry)
            if backend_error:
                entry.error = backend_error
                continue
            info = dict(fig.info)
            info.setdefault("version", doc.version)
            info.setdefault("date", date)
            if serial:
                info.setdefault("serial", serial)
            if flags:
                info.setdefault("flags", flags)
            stem = _unique(_safe_name(fig.name, f"{r.spec_id}_fig{j + 1}"), used)
            rel = f"figures/{stem}.png"
            try:
                plots.render(dataclasses.replace(fig, info=info), rdir / rel)
            except BackendUnavailable as e:
                backend_error = f"Grafik çizilemedi: {e}"
                entry.error = backend_error
                log.warning("rapor grafikleri atlanıyor: %s", e)
                continue
            except Exception as e:  # noqa: BLE001 - one bad figure must not kill the report
                entry.error = f"Grafik çizilemedi: {type(e).__name__}: {e}"
                log.exception("grafik çizilemedi: %s / %s", r.spec_id, fig.name)
                continue
            entry.path = rel
            r.artifacts.append(rel)
        doc.figures[i] = entries


def _write_tables(results: Sequence[MeasurementResult], rdir: Path, doc: _Doc) -> None:
    used: set[str] = set()
    for i, r in enumerate(results):
        if not r.table:
            continue
        stem = _unique(_safe_name(r.spec_id, f"result{i + 1}"), used)
        rel = f"tables/{stem}.csv"
        (rdir / "tables").mkdir(exist_ok=True)
        cols = _table_columns(r.table)
        _write_csv(rdir / rel, cols, ([_csv_cell(row.get(c)) for c in cols] for row in r.table))
        doc.tables[i] = rel
        r.artifacts.append(rel)


def _result_serial(r: MeasurementResult) -> str:
    dev = _device_entry(r.device) if r.device else {}
    return dev.get("serial", "") or str(r.conditions.get("serial", "") or "")


def _write_summary_csv(results: Sequence[MeasurementResult], path: Path, doc: _Doc) -> None:
    rows = []
    for r in results:
        dev = _device_entry(r.device) if r.device else {}
        rows.append([
            r.spec_id, r.title, _csv_cell(r.value), r.unit, r.target_op, _csv_cell(r.target),
            _csv_cell(r.passed), r.status, _compact_json(r.conditions), r.method, verdict(r),
            _csv_cell(r.uncertainty), r.summary, r.error,
            iso_utc(r.t_start_ns) if r.t_start_ns > 0 else "", _csv_cell(r.duration_s),
            dev.get("name", ""), _result_serial(r), doc.version, doc.commit_tag,
            _csv_cell(is_quick(r)), _csv_cell(is_simulated(r, doc.sim)), no_verdict_reason(r),
        ])
    _write_csv(path, SUMMARY_CSV_COLUMNS, rows)


def _report_meta(results: Sequence[MeasurementResult], doc: _Doc) -> dict[str, Any]:
    figs = [e for es in doc.figures.values() for e in es]
    starts = [r.t_start_ns for r in results if r.t_start_ns > 0]
    ends = [r.t_start_ns + int(r.duration_s * 1e9) for r in results if r.t_start_ns > 0]
    return {
        "title": doc.title, "generated_utc": doc.generated, "generated_ns": doc.t_ns,
        "eds_version": doc.version, "git_commit": doc.commit, "git_dirty": doc.dirty,
        "config_path": doc.config_path, "targets_file": doc.targets_path, "devices": doc.devices,
        "simulation": doc.sim, "quick": doc.quick, "quick_specs": list(doc.quick_ids),
        "out_of_spec_profile": doc.ext, "banners": [b for _, b in doc.banners],
        "measured_from_utc": iso_utc(min(starts)) if starts else "",
        "measured_to_utc": iso_utc(max(ends)) if ends else "",
        "verdicts": {v: sum(1 for r in results if verdict(r) == v) for v in VERDICTS},
        "figures_rendered": sum(1 for e in figs if e.path), "figures_failed": sum(1 for e in figs if e.error),
        "user_meta": doc.user_meta,
    }


def _write_json(results: Sequence[MeasurementResult], path: Path, doc: _Doc) -> None:
    items = []
    for r in results:
        d = r.to_dict(with_figures=False)
        d["verdict"] = verdict(r)
        reason = no_verdict_reason(r)
        if reason:
            d["no_verdict_reason"] = reason
        items.append(d)
    payload = {"meta": _jsonable(_report_meta(results, doc)), "results": items}
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2, default=str, allow_nan=False) + "\n",
                    encoding="utf-8")


# ---------------------------------------------------------------------------- shared rows
def _header_rows(results: Sequence[MeasurementResult], doc: _Doc) -> list[tuple[str, str]]:
    meta = _report_meta(results, doc)
    counts = meta["verdicts"]
    rows = [
        ("Rapor tarihi (UTC)", doc.generated),
        ("Ölçüm aralığı (UTC)", f"{meta['measured_from_utc']} → {meta['measured_to_utc']}"
         if meta["measured_from_utc"] else _DASH),
        ("Yazılım", f"EDS {doc.version}"),
        ("Git commit", doc.commit_text),
        ("Konfigürasyon", doc.config_path or _DASH),
        ("KTR hedef dosyası", doc.targets_path or _DASH),
        ("Ölçüm sayısı", f"{len(results)}  ("
         + ", ".join(f"{v} {counts[v]}" for v in VERDICTS if counts[v]) + ")" if results else "0"),
        ("Ölçüm modu", doc.mode_text),
        ("Veri kaynağı", "SİMÜLASYON (donanım ölçümü değil)" if doc.sim else "donanım"),
    ]
    for k, v in doc.user_meta.items():
        if k in ("devices", "git_commit", "git_dirty", "config_path"):
            continue
        rows.append((str(k), format_condition(str(k), v)))
    return rows


def _device_rows(doc: _Doc) -> list[list[str]]:
    return [[d["name"] or _DASH, d["backend"] or _DASH, d["serial"] or _DASH, d["profile"] or _DASH,
             d["firmware"] or _DASH, " / ".join(x for x in (d["driver"], d["hardware"], d["uri"]) if x) or _DASH]
            for d in doc.devices]


_DEVICE_HEAD = ["Ad", "Backend", "Seri no", "Profil", "Firmware", "Sürücü / donanım / URI"]
_MAIN_HEAD = ["#", "Parametre", "Ölçülen", "Birim", "Hedef", "Durum", "Koşullar (kısa)"]


def _main_rows(results: Sequence[MeasurementResult]) -> list[list[str]]:
    return [[str(i), r.title or r.spec_id, format_number(r.value, r.unit, r.uncertainty), r.unit or _DASH,
             _target_text(r), verdict(r), short_conditions(r)] for i, r in enumerate(results, 1)]


def _status_text(r: MeasurementResult) -> str:
    """Status word, with the reason when the result was not compared with its target."""
    reason = no_verdict_reason(r)
    return f"{NOT_COMPARED} — {reason}" if reason else verdict(r)


def _condition_label(key: str) -> str:
    return _COND_LABELS.get(key, "")


def _result_facts(r: MeasurementResult) -> list[tuple[str, str]]:
    """Ordered (label, text) facts shown at the top of each measurement section."""
    v = verdict(r)
    res = format_value(r.value, r.unit, r.uncertainty) if _finite(r.value) else "değer yok"
    tgt = _target_text(r, with_unit=True)
    facts = [("Sonuç", f"{res} — {v}" + (f" (hedef {tgt})" if tgt != _DASH else ""))]
    reason = no_verdict_reason(r)
    if reason:
        facts.append(("Karşılaştırılmama nedeni", reason))
    if is_quick(r):
        facts.append(("Mod", "hızlı (--quick): azaltılmış nokta sayısı / ölçüm süresi — KTR'de kullanılmaz"))
    note = r.extra.get(TARGET_NOTE_KEY)
    if note:
        facts.append(("Hedef notu", str(note)))
    req = r.extra.get(TARGET_REQUIRE_KEY)
    if isinstance(req, Mapping) and req:
        facts.append(("Hedef koşulları", "; ".join(f"{k} = {_py(v)!r}" if isinstance(_py(v), str) else
                                                   f"{k} = {_py(v)}" for k, v in req.items())))
    if r.summary:
        facts.append(("Özet", r.summary))
    facts.append(("Yöntem", r.method or _DASH))
    if r.t_start_ns > 0:
        facts.append(("Ölçüm zamanı (UTC)", f"{iso_utc(r.t_start_ns)} (süre {_fmt_duration(r.duration_s)})"))
    if r.device:
        d = _device_entry(r.device)
        bits = [f"backend {d['backend']}" if d["backend"] else "", f"seri {d['serial']}" if d["serial"] else "",
                f"profil {d['profile']}" if d["profile"] else "",
                f"firmware {d['firmware']}" if d["firmware"] else ""]
        facts.append(("Cihaz", f"{d['name'] or _DASH} — " + ", ".join(b for b in bits if b)))
    if r.error:
        facts.append(("Hata" if r.status == "failed" else "Not", r.error))
    if r.status == "ok" and not _finite(r.value):
        facts.append(("Hata", "Ölçüm geçerli bir değer üretmedi"))
    return facts


def _is_freq_key(key: str) -> bool:
    k = key.lower()
    return (k.endswith("_hz") and not k.endswith("_per_hz")
            and not any(k.endswith(sfx) for sfx, _ in _DENSITY_SUFFIXES))


def _raw_cell(key: str, v: Any) -> str:
    """Raw-table cell. Frequencies (``*_hz``) keep 1 Hz resolution with their unit
    (``935.401 MHz``: ``%.6g`` would print two tones 1.3 kHz apart as the same number)."""
    v = _py(v)
    if v is None:
        return ""
    if isinstance(v, bool):
        return "evet" if v else "hayır"
    if isinstance(v, numbers.Integral) and not _is_freq_key(key):
        return str(int(v))
    if isinstance(v, numbers.Real):
        f = float(v)
        if not math.isfinite(f):
            return _non_finite_text(f)
        if _is_freq_key(key):              # 1 Hz resolution (≥ 6 digits): 935.399353 MHz
            digits = min(10, max(6, math.floor(math.log10(abs(f))) + 1)) if f else 6
            return fmt_sps(f, digits) if _is_rate_key(key.lower()) else fmt_hz(f, digits)
        return f"{f:.6g}"
    if isinstance(v, (Mapping, list, tuple)):
        return _display_json(v, 80)
    return str(v)


def _unit_hint(key: str, mapping: Mapping[str, Any], r: MeasurementResult) -> str | None:
    """Unit of a key whose name carries none: a ``<key>_unit`` sibling, or the result's
    unit for the key named like the spec (``sensitivity`` in the sensitivity extras)."""
    u = mapping.get(f"{key}_unit")
    if isinstance(u, str) and u.strip():
        return u.strip()
    if key == r.spec_id and r.unit:
        return r.unit
    return None


def _kv_rows(mapping: Mapping[str, Any], r: MeasurementResult) -> list[tuple[str, str]]:
    return [(k, format_condition(k, v, unit=_unit_hint(k, mapping, r))) for k, v in mapping.items()]


# Raw tables in the A4 HTML/PDF: estimated column widths at the raw-table font (7.4 pt
# DejaVu Sans, digits 0.64 em) → columns are split into groups that fit the page width.
_RAW_BUDGET_MM = 170.0
_RAW_CHAR_MM = 1.72
_RAW_PAD_MM = 3.8
_RAW_LONG_CHARS = 28          # longer text cells wrap (and count as this many characters)


@dataclass
class _RawLayout:
    cols: list[str]
    shown: list[list[str]]                        # cell texts of the rows shown (all columns)
    groups: list[list[int]]                       # column indices per table
    constant: list[tuple[str, str]] = field(default_factory=list)   # (column, value) dropped


def _raw_col_width(head: str, cells: Iterable[str]) -> float:
    head_chars = max(len(p) for p in re.split(r"(?<=_)", head)) if head else 1
    n = max([head_chars, *(min(len(c), _RAW_LONG_CHARS) for c in cells)])
    return n * _RAW_CHAR_MM + _RAW_PAD_MM


def _raw_layout(r: MeasurementResult) -> _RawLayout:
    """Columns of the raw table split into page-width groups. When a split is needed, the
    columns that hold the same value on every row are listed once instead of repeated, and
    every group after the first repeats the first column so rows stay identifiable."""
    cols = _table_columns(r.table)
    text = [[_raw_cell(c, row.get(c)) for c in cols] for row in r.table]
    shown = text[:RAW_ROWS_IN_DOC]
    widths = [_raw_col_width(c, (row[j] for row in shown)) for j, c in enumerate(cols)]
    if sum(widths) <= _RAW_BUDGET_MM or len(cols) <= 1:
        return _RawLayout(cols, shown, [list(range(len(cols)))])
    constant: list[tuple[str, str]] = []
    keep = list(range(len(cols)))
    if len(text) > 1:
        const_idx = [j for j in range(len(cols)) if all(row[j] == text[0][j] for row in text)]
        if len(const_idx) < len(cols):
            constant = [(cols[j], text[0][j] or _DASH) for j in const_idx]
            keep = [j for j in keep if j not in const_idx]
    groups: list[list[int]] = []
    cur: list[int] = []
    used = 0.0
    first = keep[0]
    for j in keep:
        if cur and used + widths[j] > _RAW_BUDGET_MM:
            groups.append(cur)
            cur, used = ([first], widths[first]) if j != first else ([], 0.0)
        cur.append(j)
        used += widths[j]
    if cur:
        groups.append(cur)
    return _RawLayout(cols, shown, groups, constant)


# ---------------------------------------------------------------------------- markdown
def _md(s: Any) -> str:
    return str(s).replace("\\", "\\\\").replace("|", "\\|").replace("\r", "").replace("\n", "<br>")


def _md_table(head: Sequence[str], rows: Iterable[Sequence[str]]) -> list[str]:
    out = ["| " + " | ".join(_md(h) for h in head) + " |", "|" + "---|" * len(head)]
    out += ["| " + " | ".join(_md(c) for c in row) + " |" for row in rows]
    return out + [""]


def _build_markdown(results: Sequence[MeasurementResult], doc: _Doc) -> str:
    L: list[str] = [f"# {doc.title}", ""]
    for _, text in doc.banners:
        L += [f"> **UYARI: {text}**", ""]
    L += _md_table(["Alan", "Değer"], _header_rows(results, doc))
    L += ["## Cihazlar", ""]
    L += _md_table(_DEVICE_HEAD, _device_rows(doc)) if doc.devices else ["Cihaz bilgisi yok.", ""]
    L += ["## Özet tablo", ""]
    if results:
        rows = _main_rows(results)
        for row, r in zip(rows, results):
            row[5] = _status_text(r)
        L += _md_table(_MAIN_HEAD, rows)
    else:
        L += ["Ölçüm sonucu yok.", ""]
    if results:
        L += ["## Ölçümler", ""]
    for i, r in enumerate(results):
        n = i + 1
        L += [f"### {n}. {_md(r.title or r.spec_id)} (`{r.spec_id}`)", ""]
        L += [f"- **{label}:** {_md(text)}" for label, text in _result_facts(r)] + [""]
        if r.conditions:
            L += ["#### Koşullar", ""]
            L += _md_table(["Koşul", "Değer"], [
                [f"{_condition_label(k)} (`{k}`)" if _condition_label(k) else f"`{k}`", text]
                for k, text in _kv_rows(r.conditions, r)])
        extra = {k: v for k, v in r.extra.items() if k not in _HIDDEN_EXTRA}
        if extra:
            L += ["#### Ek metrikler", ""]
            L += _md_table(["Metrik", "Değer"], [[f"`{k}`", text] for k, text in _kv_rows(extra, r)])
        figs = doc.figures.get(i, [])
        if figs:
            L += ["#### Grafikler", ""]
            for j, e in enumerate(figs, 1):
                cap = f"Şekil {n}.{j} — {e.title}"
                if e.path:
                    L += [f"![{_md(cap)}]({e.path})", "", f"*{_md(cap)}*", ""]
                else:
                    L += [f"*{_md(cap)}* — {_md(e.error)}", ""]
        if r.table:
            rel = doc.tables.get(i, "")
            cols = _table_columns(r.table)
            shown = r.table[:RAW_ROWS_IN_DOC]
            L += ["#### Ham veri", "",
                  f"İlk {len(shown)} / {len(r.table)} satır; tamamı: [{rel}]({rel})", ""]
            L += _md_table(cols, [[_raw_cell(c, row.get(c)) for c in cols] for row in shown])
        tb = r.extra.get("traceback")
        if tb:
            L += ["#### Hata ayrıntısı", "", "```text", str(tb).rstrip(), "```", ""]
    L += ["---", "", f"*EDS {doc.version} · commit {doc.commit_text} · {doc.generated}*", ""]
    return "\n".join(L)


# ---------------------------------------------------------------------------- html
_CSS = """
:root{--ink:#0b0b0b;--ink2:#52514e;--muted:#8a8984;--line:#d9d8d4;--head:#f3f2ef;
--pass:#1a7f37;--fail:#cf222e;--nc:#bc4c00;--none:#6e7781;--skip:#9a6700;--err:#8250df}
*{box-sizing:border-box;-webkit-print-color-adjust:exact;print-color-adjust:exact}
@page{size:A4;margin:14mm 12mm 16mm 12mm}
html{background:#ffffff}
body{font-family:"DejaVu Sans","Segoe UI",Arial,sans-serif;color:var(--ink);background:#ffffff;
margin:0 auto;max-width:186mm;padding:10mm 4mm;font-size:10pt;line-height:1.4}
h1{font-size:17pt;margin:0 0 4mm}
h2{font-size:13pt;border-bottom:1.5px solid var(--ink);padding-bottom:1mm;margin:8mm 0 3mm}
h3{font-size:12pt;margin:0 0 2mm}
h4{font-size:10.5pt;margin:4mm 0 1.5mm;color:var(--ink2)}
h2,h3,h4{break-after:avoid}
table{border-collapse:collapse;width:100%;margin:1.5mm 0 4mm;font-size:8.6pt}
th,td{border:.6px solid var(--line);padding:1.1mm 1.6mm;text-align:left;vertical-align:top}
th{background:var(--head);font-weight:600}
thead{display:table-header-group}
tr{break-inside:avoid}
td.num{text-align:right;white-space:nowrap;font-variant-numeric:tabular-nums}
table.raw{font-size:7.4pt;margin:1mm 0 2.5mm}
table.raw th{white-space:normal;padding:.8mm 1.2mm}
td.raw{font-variant-numeric:tabular-nums;white-space:nowrap;padding:.8mm 1.2mm}
td.raw.long{white-space:normal;min-width:28mm}
.rawwrap{overflow-x:auto;max-width:100%}
.grp{margin:2mm 0 0}
.why{font-size:7.4pt;color:var(--ink2);margin-top:.6mm;line-height:1.25}
.kv td:first-child{width:34%;color:var(--ink2)}
.badge{display:inline-block;padding:.3mm 2mm;border-radius:2mm;color:#ffffff;font-weight:700;
font-size:7.8pt;white-space:nowrap;letter-spacing:.02em}
.b-pass{background:var(--pass)}.b-fail{background:var(--fail)}.b-none{background:var(--none)}
.b-skip{background:var(--skip)}.b-err{background:var(--err)}.b-nc{background:var(--nc)}
.banner{border:2px solid #cf222e;background:#fff1f0;color:#a40e26;padding:2.5mm 3mm;font-weight:700;
margin:2mm 0 4mm;font-size:11pt}
.banner.quick{border-color:#bc4c00;background:#fff1e5;color:#953800}
.banner.ext{border-color:#9a6700;background:#fff8e6;color:#7a4f00}
.facts{margin:0 0 3mm;padding:0;list-style:none}
.facts li{margin:.8mm 0}
.facts b{color:var(--ink2)}
figure{margin:3mm 0;break-inside:avoid}
figure img{width:100%;height:auto;border:.6px solid var(--line)}
figcaption{font-size:8.5pt;color:var(--ink2);margin-top:1mm}
.figerr{color:#a40e26;font-size:9pt}
.note{color:var(--ink2);font-size:8.6pt}
pre{white-space:pre-wrap;font-size:7.8pt;background:#f6f8fa;padding:2mm;border:.6px solid var(--line)}
section.meas{break-before:page;padding-top:2mm}
footer{margin-top:8mm;color:var(--muted);font-size:8pt;border-top:.6px solid var(--line);padding-top:2mm}
a{color:#1f5fae}
@media screen{body{padding:10mm 6mm}section.meas{border-top:2px solid var(--line);margin-top:8mm}}
@media print{.rawwrap{overflow:visible}}
"""


def _h(s: Any) -> str:
    return html.escape(str(s), quote=True)


def _badge(v: str) -> str:
    return f'<span class="badge b-{_BADGE_CLASS.get(v, "err")}">{_h(v)}</span>'


def _html_table(head: Sequence[str], rows: Iterable[Sequence[str]], *, cls: str = "",
                num_cols: Sequence[int] = (), raw: bool = False, escape: Sequence[bool] | None = None) -> str:
    out = [f'<table class="{cls}">' if cls else "<table>", "<thead><tr>"]
    out += [f"<th>{_h(h)}</th>" for h in head]
    out.append("</tr></thead><tbody>")
    for row in rows:
        cells = []
        for c, val in enumerate(row):
            klass = ' class="num"' if c in num_cols else (' class="raw"' if raw else "")
            text = _h(val) if escape is None or escape[c] else val
            cells.append(f"<td{klass}>{text}</td>")
        out.append("<tr>" + "".join(cells) + "</tr>")
    out.append("</tbody></table>")
    return "".join(out)


def _html_raw_tables(r: MeasurementResult) -> str:
    """Raw rows as one or more page-width tables (see :func:`_raw_layout`)."""
    lay = _raw_layout(r)
    out: list[str] = []
    if lay.constant:
        out.append('<p class="note">Tüm satırlarda aynı: '
                   + "; ".join(f"<code>{_h(c)}</code> = {_h(v)}" for c, v in lay.constant) + "</p>")
    n = len(lay.groups)
    for g, idx in enumerate(lay.groups, 1):
        if n > 1:
            out.append(f'<p class="note grp">Sütun grubu {g}/{n}'
                       + (f" (ilk sütun <code>{_h(lay.cols[idx[0]])}</code> tekrarlanır)" if g > 1 else "")
                       + "</p>")
        head = "".join(f"<th>{_h(lay.cols[j]).replace('_', '_<wbr>')}</th>" for j in idx)
        body = []
        for row in lay.shown:
            cells = "".join(f'<td class="{"raw long" if len(row[j]) > _RAW_LONG_CHARS else "raw"}">{_h(row[j])}</td>'
                            for j in idx)
            body.append(f"<tr>{cells}</tr>")
        out.append(f'<div class="rawwrap"><table class="raw"><thead><tr>{head}</tr></thead>'
                   f'<tbody>{"".join(body)}</tbody></table></div>')
    return "".join(out)


def _img_data_uri(path: Path) -> str:
    return "data:image/png;base64," + base64.b64encode(path.read_bytes()).decode("ascii")


def _build_html(results: Sequence[MeasurementResult], doc: _Doc, rdir: Path) -> str:
    P: list[str] = ["<!DOCTYPE html>", '<html lang="tr"><head><meta charset="utf-8">',
                    '<meta name="viewport" content="width=device-width, initial-scale=1">',
                    f"<title>{_h(doc.title)}</title>", f"<style>{_CSS}</style></head><body>",
                    f"<h1>{_h(doc.title)}</h1>"]
    for kind, text in doc.banners:
        P.append(f'<div class="banner {kind}">UYARI: {_h(text)}</div>')
    P.append(_html_table(["Alan", "Değer"], _header_rows(results, doc), cls="kv"))
    P.append("<h2>Cihazlar</h2>")
    P.append(_html_table(_DEVICE_HEAD, _device_rows(doc)) if doc.devices else '<p class="note">Cihaz bilgisi yok.</p>')
    P.append("<h2>Özet tablo</h2>")
    if results:
        rows = []
        for i, (row, r) in enumerate(zip(_main_rows(results), results)):
            reason = no_verdict_reason(r)
            status = _badge(verdict(r)) + (f'<div class="why">{_h(reason)}</div>' if reason else "")
            rows.append([_h(row[0]), f'<a href="#m{i + 1}">{_h(row[1])}</a>', _h(row[2]), _h(row[3]),
                         _h(row[4]), status, _h(row[6])])
        P.append(_html_table(_MAIN_HEAD, rows, num_cols=(0, 2, 4), escape=[False] * len(_MAIN_HEAD)))
    else:
        P.append('<p class="note">Ölçüm sonucu yok.</p>')
    for i, r in enumerate(results):
        n = i + 1
        P.append(f'<section class="meas" id="m{n}">')
        P.append(f"<h3>{n}. {_h(r.title or r.spec_id)} <code>{_h(r.spec_id)}</code> {_badge(verdict(r))}</h3>")
        P.append('<ul class="facts">' + "".join(f"<li><b>{_h(k)}:</b> {_h(v)}</li>" for k, v in _result_facts(r))
                 + "</ul>")
        if r.conditions:
            P.append("<h4>Koşullar</h4>")
            P.append(_html_table(["Koşul", "Anahtar", "Değer"], [
                [_condition_label(k) or k, k, text] for k, text in _kv_rows(r.conditions, r)], cls="kv"))
        extra = {k: v for k, v in r.extra.items() if k not in _HIDDEN_EXTRA}
        if extra:
            P.append("<h4>Ek metrikler</h4>")
            P.append(_html_table(["Metrik", "Değer"], _kv_rows(extra, r), cls="kv"))
        figs = doc.figures.get(i, [])
        if figs:
            P.append("<h4>Grafikler</h4>")
        for j, e in enumerate(figs, 1):
            cap = f"Şekil {n}.{j} — {e.title}"
            if e.path:
                P.append(f'<figure><img src="{_img_data_uri(rdir / e.path)}" alt="{_h(cap)}">'
                         f"<figcaption>{_h(cap)}</figcaption></figure>")
            else:
                P.append(f'<p class="figerr">{_h(cap)}: {_h(e.error)}</p>')
        if r.table:
            rel = doc.tables.get(i, "")
            shown = r.table[:RAW_ROWS_IN_DOC]
            P.append("<h4>Ham veri</h4>")
            P.append(f'<p class="note">İlk {len(shown)} / {len(r.table)} satır; tamamı: '
                     f'<a href="{_h(rel)}">{_h(rel)}</a></p>')
            P.append(_html_raw_tables(r))
        tb = r.extra.get("traceback")
        if tb:
            P.append(f"<h4>Hata ayrıntısı</h4><pre>{_h(str(tb).rstrip())}</pre>")
        P.append("</section>")
    P.append(f"<footer>EDS {_h(doc.version)} · commit {_h(doc.commit_text)} · {_h(doc.generated)}</footer>")
    P.append("</body></html>")
    return "\n".join(P)


# ============================================================================ entry point
def write_report(
    results: Iterable[MeasurementResult],
    out_dir: str | Path,
    app: Any = None,
    title: str = DEFAULT_TITLE,
    meta: Mapping[str, Any] | None = None,
) -> Path:
    """Write the full report folder ``out_dir/<stamp>/`` and return its path.

    ``app`` (:class:`~eds.core.config.AppConfig`, optional) supplies the config path, the
    targets file and configured devices. ``meta`` adds free-form header rows (operator,
    location, test setup …); the reserved keys ``devices`` (list of device dicts),
    ``git_commit``/``git_dirty`` and ``config_path`` override the detected values.
    Each result's ``artifacts`` is replaced by the files written for it (relative paths).
    """
    results = list(results)
    user_meta = dict(meta or {})
    t_ns = now_ns()
    if "git_commit" in user_meta:
        commit = str(user_meta["git_commit"]) or UNKNOWN
        dirty_raw = user_meta.get("git_dirty")
        dirty = dirty_raw if isinstance(dirty_raw, bool) else None
    else:
        commit, dirty = git_revision()
    config_path = str(user_meta.get("config_path") or (getattr(app, "source_path", "") if app is not None else ""))
    if config_path and Path(config_path).is_file():
        config_path = str(Path(config_path).resolve())
    targets_path = ""
    if app is not None:
        targets_path = str(resolve_targets_path(app.measure.targets_file))
    devices = _collect_devices(results, app, user_meta)
    sim, ext = _flags(results, devices)
    doc = _Doc(title=title, t_ns=t_ns, generated=iso_utc(t_ns), version=__version__, commit=commit,
               dirty=dirty, config_path=config_path, targets_path=targets_path, devices=devices, sim=sim,
               ext=ext, user_meta=user_meta, quick_ids=[r.spec_id for r in results if is_quick(r)],
               n_results=len(results))

    rdir = _make_report_dir(Path(out_dir), t_ns)
    for r in results:
        r.artifacts = []
    if any(r.figures for r in results):
        (rdir / "figures").mkdir(exist_ok=True)
        _render_figures(results, rdir, doc)
    _write_tables(results, rdir, doc)
    _write_json(results, rdir / "results.json", doc)
    _write_summary_csv(results, rdir / "summary.csv", doc)
    (rdir / "summary.md").write_text(_build_markdown(results, doc), encoding="utf-8")
    (rdir / "report.html").write_text(_build_html(results, doc, rdir), encoding="utf-8")
    log.info("rapor yazıldı: %s", rdir)
    return rdir
