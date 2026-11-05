"""KTR quantitative targets (``config/ktr_targets.toml``) → pass/fail of measurement results.

File format (one table per spec id of :data:`eds.measure.base.SPEC_IDS`)::

    [sensitivity]
    target = -100.0          # in the measurement's unit (MeasurementResult.unit)
    op = "<="                # pass condition: measured <op> target
    note = "Pd = 0.9 @ Pfa = 1e-6, belirtilen RBW'de"

    [sensitivity.require]    # optional: conditions under which the target was committed
    rbw_hz_max = 2000.0      # <name>_max: result.conditions[name] <= value (every element of a list)
    gain_db_min = 70.0       # <name>_min: result.conditions[name] >= value
    # <name>_count_min = 4   # number of elements of a list condition (e.g. measured frequencies)
    # <name> = "pipeline"    # anything else: equality (string / bool / number)

Every key is validated *before* any result is touched; a typo in a spec id or an operator
is an error (a silently ignored target on the jury report is worse than a crash). All
problems of a file are reported at once (Turkish, operator-facing).

A spec id that is absent from the file simply has no target ("HEDEF YOK" in the report).

No verdict ("koşulsuz sayı" rule of docs/MEASUREMENTS.md): a result keeps its target but
gets ``passed = None`` and a Turkish reason in ``extra['verdict_note']`` when

* the measurement itself listed :attr:`MeasurementResult.blockers`,
* it was produced in quick mode (``conditions['quick']``),
* it comes from a non-real-time simulator (``extra['realtime'] is False``) or from channels
  that are not coherent (``extra['coherent'] is False``),
* its unit differs from the spec's unit, or a dBm target meets an uncalibrated value,
* a ``require`` condition of the target is not met or not recorded by the result.
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
VERDICT_NOTE_KEY = "verdict_note"         # why no pass/fail verdict was given (unit / calibration)
TARGET_REQUIRE_KEY = "target_require"     # MeasurementResult.extra key carrying Target.require
_ALLOWED_KEYS = frozenset({"target", "op", "note", "require"})
_NUMERIC_SUFFIXES: tuple[str, ...] = ("_count_min", "_min", "_max")

#: Shared blocker texts (measurements add them to ``MeasurementResult.blockers``; the same
#: text derived here from ``extra`` / ``conditions`` is not repeated in the verdict note).
REASON_QUICK = "hızlı mod (--quick): azaltılmış nokta/süre"
REASON_NONREALTIME = "gerçek zamanlı olmayan simülatör: ölçülen zamanlama fiziksel olarak anlamsız"
REASON_NO_COHERENCE = "kanallar koherent değil (ortak referans yok)"
NO_VERDICT_SUFFIX = " — hedefle karşılaştırılmadı"


@dataclass(frozen=True)
class Target:
    """One KTR commitment: ``measured <op> target`` (``target`` in the result's unit)."""

    target: float
    op: str
    note: str = ""
    #: ``((key, value), ...)`` conditions the target was committed under (see module doc).
    require: tuple[tuple[str, Any], ...] = ()

    def __post_init__(self) -> None:
        if self.op not in VALID_OPS:
            raise ConfigError(f"Geçersiz hedef operatörü {self.op!r}; seçenekler: {list(VALID_OPS)}")
        if isinstance(self.target, bool) or not isinstance(self.target, (int, float)) \
                or not math.isfinite(float(self.target)):
            raise ConfigError(f"Hedef değeri sonlu bir sayı olmalı (geldi: {self.target!r})")
        if isinstance(self.require, Mapping):
            object.__setattr__(self, "require", tuple(self.require.items()))
        errs = _require_errors(dict(self.require))
        if errs:
            raise ConfigError("Geçersiz hedef koşulu: " + "; ".join(errs))

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
        req = entry.get("require", {})
        if not isinstance(req, Mapping):
            errs.append(f"'require' tablo olmalı ([{spec_id}.require]), {req!r} geldi")
        else:
            errs.extend(f"'require': {e}" for e in _require_errors(req))
        if errs:
            problems.extend(f"{where}: {e}" for e in errs)
        else:
            out[spec_id] = Target(target=float(value), op=str(op), note=note, require=tuple(req.items()))
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
    ``result.extra["target_note"]`` and its ``require`` conditions in
    ``result.extra["target_require"]`` so they travel with the result (report, bus, JSON).
    A result that must not be compared (see :func:`verdict_blockers`) keeps the target
    but gets ``passed = None`` and the reasons in ``result.extra["verdict_note"]``.
    """
    for r in results:
        t = targets.get(r.spec_id)
        if t is None:
            r.evaluate_target(None, "")
            for k in (TARGET_NOTE_KEY, TARGET_REQUIRE_KEY, VERDICT_NOTE_KEY):
                r.extra.pop(k, None)
            continue
        r.evaluate_target(t.target, t.op)
        if t.note:
            r.extra[TARGET_NOTE_KEY] = t.note
        else:
            r.extra.pop(TARGET_NOTE_KEY, None)
        if t.require:
            r.extra[TARGET_REQUIRE_KEY] = dict(t.require)
        else:
            r.extra.pop(TARGET_REQUIRE_KEY, None)
        reasons = verdict_blockers(r, t) if r.status == "ok" else []
        if reasons:
            r.passed = None
            r.extra[VERDICT_NOTE_KEY] = "; ".join(reasons) + NO_VERDICT_SUFFIX
        else:
            r.extra.pop(VERDICT_NOTE_KEY, None)


def verdict_blockers(r: MeasurementResult, target: Target | None = None) -> list[str]:
    """Reasons (TR) why ``r.value`` must NOT be compared with its KTR target (empty = comparable).

    * the measurement's own :attr:`MeasurementResult.blockers`;
    * quick mode, a non-real-time simulator, channels that are not coherent (derived from
      ``conditions`` / ``extra`` for results that did not list them as blockers);
    * a unit different from the spec's canonical unit (e.g. dBFS against a dBm target), or
      an uncalibrated result (``extra['calibrated'] is False``) against a dBm target;
    * ``target.require`` conditions that the result does not meet or does not record.
    """
    from .base import MEASUREMENTS

    reasons: list[str] = [str(b) for b in r.blockers if str(b).strip()]

    def add(reason: str) -> None:
        if reason not in reasons:
            reasons.append(reason)

    if r.conditions.get("quick") is True:
        add(REASON_QUICK)
    if r.extra.get("realtime") is False:
        add(REASON_NONREALTIME)
    if r.extra.get("coherent") is False:
        add(REASON_NO_COHERENCE)
    cls = MEASUREMENTS.get(r.spec_id)
    if cls is not None and r.unit and cls.unit and r.unit != cls.unit:
        add(f"Birim uyuşmuyor ({r.unit} ≠ hedef birimi {cls.unit})")
    elif r.extra.get("calibrated") is False and cls is not None and cls.unit in ("dBm",):
        add("Kalibrasyonsuz ölçüm; mutlak (dBm) hedef")
    if target is not None:
        for reason in unmet_requirements(r, target.require):
            add(reason)
    return reasons


def _verdict_blocker(r: MeasurementResult) -> str:
    """Backward-compatible single-string form of :func:`verdict_blockers` (no ``require``)."""
    reasons = verdict_blockers(r)
    return "; ".join(reasons) + NO_VERDICT_SUFFIX if reasons else ""


# ------------------------------------------------------------------ target conditions
def _split_require_key(key: str) -> tuple[str, str]:
    """``"rbw_hz_max"`` → ``("rbw_hz", "max")``; ``"n_fc_count_min"`` → ``("n_fc", "count")``;
    anything else is an equality condition ``(key, "eq")``."""
    for suffix in _NUMERIC_SUFFIXES:
        if key.endswith(suffix) and len(key) > len(suffix):
            return key[: -len(suffix)], {"_count_min": "count", "_min": "min", "_max": "max"}[suffix]
    return key, "eq"


def _is_number(v: Any) -> bool:
    return not isinstance(v, bool) and isinstance(v, (int, float)) and math.isfinite(float(v))


def _require_errors(req: Mapping[str, Any]) -> list[str]:
    errs: list[str] = []
    for key, want in req.items():
        if not isinstance(key, str) or not key:
            errs.append(f"geçersiz koşul adı {key!r}")
            continue
        _, kind = _split_require_key(key)
        if kind == "eq":
            if not (isinstance(want, (str, bool)) or _is_number(want)):
                errs.append(f"{key} = {want!r}: metin, true/false ya da sonlu sayı olmalı")
        elif not _is_number(want):
            errs.append(f"{key} = {want!r}: sonlu bir sayı olmalı")
    return errs


_MISSING = object()


def _lookup(r: MeasurementResult, name: str) -> Any:
    if name in r.conditions:
        return r.conditions[name]
    return r.extra.get(name, _MISSING)


def unmet_requirements(r: MeasurementResult, require: Iterable[tuple[str, Any]]) -> list[str]:
    """Turkish reasons for every ``require`` condition the result does not satisfy (a
    condition the result does not record cannot be verified and counts as unmet)."""
    out: list[str] = []
    for key, want in require:
        name, kind = (key, "eq") if (key in r.conditions or key in r.extra) else _split_require_key(key)
        have = _lookup(r, name)
        if have is _MISSING or have is None:
            out.append(f"hedef koşulu doğrulanamadı: '{name}' sonuçta kayıtlı değil ({key} = {want!r})")
            continue
        if kind == "count":
            n = len(have) if isinstance(have, (list, tuple)) else 1
            if n < want:
                out.append(f"hedef koşulu sağlanmadı: {name} sayısı {n} < {want:g}")
            continue
        if kind in ("min", "max"):
            vals = list(have) if isinstance(have, (list, tuple)) else [have]
            if not vals or not all(_is_number(v) for v in vals):
                out.append(f"hedef koşulu doğrulanamadı: {name} = {have!r} sayı değil ({key} = {want:g})")
                continue
            worst = min(vals) if kind == "min" else max(vals)
            bad = worst < want if kind == "min" else worst > want
            if bad:
                sym = "≥" if kind == "min" else "≤"
                out.append(f"hedef koşulu sağlanmadı: {name} = {worst:g} (gerekli {sym} {want:g})")
            continue
        if isinstance(want, bool) or isinstance(have, bool):
            same = isinstance(have, bool) and isinstance(want, bool) and have is want
        elif _is_number(want) and _is_number(have):
            same = math.isclose(float(have), float(want), rel_tol=1e-9, abs_tol=0.0)
        else:
            same = str(have) == str(want)
        if not same:
            out.append(f"hedef koşulu sağlanmadı: {name} = {have!r} (gerekli {want!r})")
    return out
