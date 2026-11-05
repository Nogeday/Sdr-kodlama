"""Shared helpers of the ``eds`` command-line tool: config loading with CLI overrides,
``KEY=VALUE`` parsing, plain-text tables and operator prompts."""

from __future__ import annotations

import argparse
import json
import math
import sys
from collections.abc import Callable, Iterable, Sequence
from typing import Any, TextIO

from ..core.config import AppConfig, load_config
from ..core.errors import ConfigError, MeasurementError

if sys.version_info >= (3, 11):
    import tomllib
else:  # pragma: no cover
    import tomli as tomllib


def parse_literal(text: str) -> Any:
    """``VALUE`` of ``--set KEY=VALUE`` / ``--arg k=v``: a TOML literal (``2.4e9``, ``true``,
    ``[1, 2]``, ``"a b"``, ``{x = 1}``), otherwise the raw string (``sim``, ``usb:1.5.5``)."""
    try:
        return tomllib.loads(f"v = {text}")["v"]
    except tomllib.TOMLDecodeError:
        return text


# ----------------------------------------------------------------------------- argparse types
def _number(text: str) -> float:
    try:
        v = float(text)
    except ValueError:
        raise argparse.ArgumentTypeError(f"sayı bekleniyordu, {text!r} geldi") from None
    if not math.isfinite(v):
        raise argparse.ArgumentTypeError(f"sonlu bir sayı olmalı, {text!r} geldi")
    return v


def finite_float(text: str) -> float:
    """argparse type: a finite number (``nan`` / ``inf`` rejected with a Turkish message)."""
    return _number(text)


def positive_float(text: str) -> float:
    """argparse type: a finite number > 0 (frequencies, rates, RBW)."""
    v = _number(text)
    if v <= 0:
        raise argparse.ArgumentTypeError(f"pozitif bir sayı olmalı, {text!r} geldi")
    return v


def nonneg_float(text: str) -> float:
    """argparse type: a finite number >= 0 (durations, timeouts)."""
    v = _number(text)
    if v < 0:
        raise argparse.ArgumentTypeError(f"negatif olmayan bir sayı olmalı, {text!r} geldi")
    return v


def positive_int(text: str) -> int:
    """argparse type: an integer >= 1 (counts, repeats, block sizes)."""
    try:
        v = int(text)
    except ValueError:
        raise argparse.ArgumentTypeError(f"tamsayı bekleniyordu, {text!r} geldi") from None
    if v < 1:
        raise argparse.ArgumentTypeError(f"en az 1 olmalı, {text!r} geldi")
    return v


def nonfinite_paths(obj: Any, path: str = "") -> list[str]:
    """Dotted paths of the non-finite floats (NaN, ±inf) inside ``obj`` — JSON cannot carry
    them (the bus would send ``null``)."""
    if isinstance(obj, float):
        return [] if math.isfinite(obj) else [path or "(değer)"]
    if isinstance(obj, dict):
        return [p for k, v in obj.items() for p in nonfinite_paths(v, f"{path}.{k}" if path else str(k))]
    if isinstance(obj, (list, tuple)):
        return [p for i, v in enumerate(obj) for p in nonfinite_paths(v, f"{path}[{i}]")]
    return []


def parse_kv(item: str, what: str = "--set", literal: bool = True) -> tuple[str, Any]:
    """``"key=value"`` → ``(key, value)`` (value parsed with :func:`parse_literal` if ``literal``)."""
    if "=" not in item:
        raise ConfigError(f"{what} '{item}': ANAHTAR=DEĞER biçiminde olmalı")
    key, value = item.split("=", 1)
    key = key.strip()
    if not key:
        raise ConfigError(f"{what} '{item}': anahtar boş olamaz")
    return key, (parse_literal(value.strip()) if literal else value.strip())


def parse_kv_list(items: Iterable[str] | None, what: str = "--set", literal: bool = True) -> dict[str, Any]:
    out: dict[str, Any] = {}
    for item in items or []:
        k, v = parse_kv(item, what, literal)
        out[k] = v
    return out


def load_app(args: Any) -> AppConfig:
    """Effective configuration: built-in defaults (one simulated device) < ``-c FILE`` <
    ``--set`` overrides < ``--backend`` (every device)."""
    overrides = parse_kv_list(getattr(args, "set", None) or [])
    app = load_config(getattr(args, "config", None), overrides)
    backend = getattr(args, "backend", None)
    if backend:
        for d in app.devices:
            d.backend = backend
    return app


def fmt_value(v: Any, digits: int = 4) -> str:
    """Compact human formatting of a scalar (``—`` for None / non-finite)."""
    if v is None:
        return "—"
    if isinstance(v, bool):
        return "evet" if v else "hayır"
    if isinstance(v, float):
        if not math.isfinite(v):
            return "—"
        return f"{v:.{digits}g}"
    return str(v)


def print_table(headers: Sequence[str], rows: Iterable[Sequence[Any]], file: TextIO | None = None,
                right: Iterable[int] = ()) -> None:
    """Plain aligned text table (no ANSI colours)."""
    out = file if file is not None else sys.stdout
    srows = [[str(c) for c in r] for r in rows]
    widths = [max([len(h)] + [len(r[i]) for r in srows if i < len(r)]) for i, h in enumerate(headers)]
    ralign = set(right)

    def line(cells: Sequence[str]) -> str:
        return "  ".join(c.rjust(w) if i in ralign else c.ljust(w) for i, (c, w) in enumerate(zip(cells, widths))).rstrip()

    print(line(list(headers)), file=out)
    print("  ".join("-" * w for w in widths), file=out)
    for r in srows:
        print(line(r + [""] * (len(headers) - len(r))), file=out)


def compact_json(obj: Any, limit: int | None = None) -> str:
    s = json.dumps(obj, ensure_ascii=False, separators=(",", ":"), default=str)
    if limit is not None and len(s) > limit:
        s = s[: max(limit - 1, 0)] + "…"
    return s


def make_prompt(auto_yes: bool, stream_in: Callable[[str], str] = input,
                stream_out: TextIO | None = None) -> Callable[[str], None]:
    """Operator prompt for hardware measurements: shows the instruction and waits for Enter
    (``--yes``: shows it and continues). A closed stdin is an error, never a silent skip."""
    out = stream_out if stream_out is not None else sys.stderr

    def prompt(msg: str) -> None:
        if auto_yes:
            print(f"[OPERATÖR] {msg} (--yes: otomatik devam)", file=out)
            return
        try:
            stream_in(f"[OPERATÖR] {msg} ")
        except EOFError as e:
            raise MeasurementError(
                "Operatör girişi alınamadı (stdin kapalı); etkileşimsiz çalıştırmada --yes kullanın") from e

    return prompt
