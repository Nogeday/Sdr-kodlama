"""KTR quantitative targets (``config/ktr_targets.toml``) → pass/fail of measurement results.

File format (one table per spec id of :data:`eds.measure.base.SPEC_IDS`)::

    [sensitivity]
    target = -100.0          # in the measurement's unit (MeasurementResult.unit)
    op = "<="                # pass condition: measured <op> target
    note = "Pd = 0.9 @ Pfa = 1e-6, belirtilen RBW'de"

Every key is validated *before* any result is touched; a typo in a spec id or an operator
is an error (a silently ignored target on the jury report is worse than a crash). All
problems of a file are reported at once (Turkish, operator-facing).

A spec id that is absent from the file simply has no target ("HEDEF YOK" in the report).
"""

from __future__ import annotations

import math
import sys
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from ..core.errors import ConfigError
from ..core.paths import config_dir
from .base import SPEC_IDS, MeasurementResult

if sys.version_info >= (3, 11):
    import tomllib
else:  # pragma: no cover
    import tomli as tomllib

DEFAULT_TARGETS_FILE = "ktr_targets.toml"
VALID_OPS: tuple[str, ...] = ("<=", ">=")
TARGET_NOTE_KEY = "target_note"           # MeasurementResult.extra key carrying Target.note
_ALLOWED_KEYS = frozenset({"target", "op", "note"})


@dataclass(frozen=True)
class Target:
    """One KTR commitment: ``measured <op> target`` (``target`` in the result's unit)."""

    target: float
    op: str
    note: str = ""

    def __post_init__(self) -> None:
        if self.op not in VALID_OPS:
            raise ConfigError(f"Geçersiz hedef operatörü {self.op!r}; seçenekler: {list(VALID_OPS)}")
        if isinstance(self.target, bool) or not isinstance(self.target, (int, float)) \
                or not math.isfinite(float(self.target)):
            raise ConfigError(f"Hedef değeri sonlu bir sayı olmalı (geldi: {self.target!r})")

    def passes(self, value: float | None) -> bool | None:
        """``True``/``False`` for a finite value, ``None`` when nothing can be judged."""
        if value is None or not math.isfinite(float(value)):
            return None
        return float(value) <= self.target if self.op == "<=" else float(value) >= self.target

    def describe(self, unit: str = "") -> str:
        """Human readable form, e.g. ``"≤ -100 dBm"``."""
        sym = "≤" if self.op == "<=" else "≥"
        return f"{sym} {self.target:g}" + (f" {unit}" if unit else "")


def resolve_targets_path(path: str | Path | None = None) -> Path:
    """Default ``<config_dir>/ktr_targets.toml``; relative paths resolve against config_dir()."""
    if path is None or str(path) == "":
        return config_dir() / DEFAULT_TARGETS_FILE
    p = Path(path).expanduser()
    return p if p.is_absolute() else config_dir() / p


def parse_targets(raw: Mapping[str, Any], source: str = "<dict>") -> dict[str, Target]:
    """Validate a parsed TOML mapping and build ``{spec_id: Target}``.

    Raises :class:`ConfigError` listing *every* problem found in ``raw``.
    """
    problems: list[str] = []
    out: dict[str, Target] = {}
    for spec_id, entry in raw.items():
        where = f"[{spec_id}]"
        if spec_id not in SPEC_IDS:
            problems.append(f"{where}: bilinmeyen ölçüm kimliği; geçerli: {list(SPEC_IDS)}")
            continue
        if not isinstance(entry, Mapping):
            problems.append(f"{where}: tablo bekleniyordu (target/op/note), {entry!r} geldi")
            continue
        errs: list[str] = []
        unknown = sorted(set(entry) - _ALLOWED_KEYS)
        if unknown:
            errs.append(f"bilinmeyen anahtar(lar) {unknown}; geçerli: {sorted(_ALLOWED_KEYS)}")
        value = entry.get("target")
        if value is None:
            errs.append("'target' eksik")
        elif isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
            errs.append(f"'target' sonlu bir sayı olmalı, {value!r} geldi")
        op = entry.get("op")
        if op is None:
            errs.append("'op' eksik (\"<=\" ya da \">=\")")
        elif op not in VALID_OPS:
            errs.append(f"'op' = {op!r} geçersiz; seçenekler: {list(VALID_OPS)}")
        note = entry.get("note", "")
        if not isinstance(note, str):
            errs.append(f"'note' metin olmalı, {note!r} geldi")
        if errs:
            problems.extend(f"{where}: {e}" for e in errs)
        else:
            out[spec_id] = Target(target=float(value), op=str(op), note=note)
    if problems:
        raise ConfigError(f"KTR hedef dosyası geçersiz ({source}):\n  - " + "\n  - ".join(problems))
    return out


def load_targets(path: str | Path | None = None) -> dict[str, Target]:
    """Load and validate the KTR targets file → ``{spec_id: Target}``.

    ``path=None`` → ``<config_dir>/ktr_targets.toml``; relative paths are resolved against
    :func:`eds.core.paths.config_dir` (same rule as ``measure.targets_file`` in the app
    config). Raises :class:`ConfigError` when the file is missing, unparsable or invalid.
    """
    p = resolve_targets_path(path)
    if not p.is_file():
        raise ConfigError(f"KTR hedef dosyası bulunamadı: {p}")
    try:
        with p.open("rb") as f:
            raw = tomllib.load(f)
    except tomllib.TOMLDecodeError as e:
        raise ConfigError(f"KTR hedef dosyası okunamadı (TOML hatası) {p}: {e}") from e
    except OSError as e:
        raise ConfigError(f"KTR hedef dosyası açılamadı {p}: {e}") from e
    return parse_targets(raw, source=str(p))


def apply_targets(results: Iterable[MeasurementResult], targets: Mapping[str, Target]) -> None:
    """Evaluate every result against its target (in place, via ``evaluate_target``).

    The targets mapping is authoritative: a result whose spec id has no target gets
    ``target=None, target_op="", passed=None`` (so a stale target never survives a
    changed targets file). The target's ``note`` is stored in
    ``result.extra["target_note"]`` so it travels with the result (report, bus, JSON).
    """
    for r in results:
        t = targets.get(r.spec_id)
        if t is None:
            r.evaluate_target(None, "")
            r.extra.pop(TARGET_NOTE_KEY, None)
            continue
        r.evaluate_target(t.target, t.op)
        if t.note:
            r.extra[TARGET_NOTE_KEY] = t.note
        else:
            r.extra.pop(TARGET_NOTE_KEY, None)
