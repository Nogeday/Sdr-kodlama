"""JSON Schema (Draft 2020-12) validation of bus messages.

Schemas live in ``contracts/schemas``; each topic schema is
``allOf [envelope.schema.json] + {topic: const, data: <payload>}`` and shared definitions
are in ``common.schema.json``. ``$ref`` URIs are resolved through a local
:class:`referencing.Registry` built from those files — never over the network (a ref that
is not in the registry is an error, not a download).

``jsonschema`` is an optional dependency (``pip install jsonschema``). This module imports
without it; calling a validation function then raises
:class:`eds.core.errors.BackendUnavailable`.

Validation failures raise :class:`MessageValidationError`, which is *both* an
:class:`eds.core.errors.EDSError` (via :class:`eds.bus.envelope.BusError`) and a
``jsonschema.ValidationError`` — callers may catch either. Its ``str()`` is a short
Turkish one-liner (instance path + reason); the full ``jsonschema`` details
(``path``, ``schema_path``, ``validator``, ``instance``...) stay available as attributes.
"""

from __future__ import annotations

import json
import threading
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from ..core.errors import BackendUnavailable
from ..core.timeutil import now_ns
from .envelope import BusError, make_envelope
from .topics import (
    ENVELOPE_SCHEMA,
    UnknownTopic,
    registry,
    schema_uri,
    schemas_dir,
)

try:
    import referencing
    import referencing.exceptions
    from jsonschema import Draft202012Validator
    from jsonschema.exceptions import SchemaError, best_match
    from jsonschema.exceptions import ValidationError as _JsonschemaValidationError
    from referencing.jsonschema import DRAFT202012

    _IMPORT_ERROR: ImportError | None = None
except ImportError as _e:  # optional dependency
    _IMPORT_ERROR = _e
    _JsonschemaValidationError = None  # type: ignore[assignment,misc]

_INSTALL_HINT = (
    "JSON şema doğrulaması için 'jsonschema' paketi gerekli: pip install jsonschema "
    "(ya da pip install -e '.[validate]')"
)
_MAX_REASON_CHARS = 240

_ERROR_BASES: tuple[type, ...] = (
    (_JsonschemaValidationError, BusError) if _JsonschemaValidationError is not None else (BusError,)
)


def _format_path(path: Iterable[Any]) -> str:
    out = ""
    for p in path:
        out += f"[{p}]" if isinstance(p, int) else (f".{p}" if out else str(p))
    return out or "(zarf kökü)"


class MessageValidationError(*_ERROR_BASES):  # type: ignore[misc]
    """A message does not conform to its topic schema.

    Subclass of :class:`eds.bus.envelope.BusError` and (when installed)
    ``jsonschema.ValidationError``. Extra attributes: ``topic``, ``n_errors``,
    ``location`` (e.g. ``"data.values[3]"``) and ``summary`` (== ``str(self)``).
    """

    topic: str = ""
    n_errors: int = 0
    location: str = ""
    summary: str = ""

    @classmethod
    def from_error(cls, topic: str, err: Any, n_errors: int) -> MessageValidationError:
        exc = cls.create_from(err)
        exc.topic = topic
        exc.n_errors = n_errors
        exc.location = _format_path(err.absolute_path)
        reason = err.message
        if len(reason) > _MAX_REASON_CHARS:
            reason = reason[: _MAX_REASON_CHARS - 3] + "..."
        exc.summary = (
            f"'{topic}' mesajı şemaya uymuyor ({n_errors} hata) — {exc.location}: {reason} "
            f"[{err.validator}]"
        )
        return exc

    def __str__(self) -> str:
        return self.summary or str(getattr(self, "message", ""))


def require_backend() -> None:
    """Raise :class:`BackendUnavailable` if ``jsonschema``/``referencing`` are missing."""
    if _IMPORT_ERROR is not None:
        raise BackendUnavailable(f"{_INSTALL_HINT} ({_IMPORT_ERROR})") from _IMPORT_ERROR


# --------------------------------------------------------------------------- registry
@dataclass
class _SchemaSet:
    directory: Path
    stamp: tuple[tuple[str, int, int], ...]
    registry: Any                                  # referencing.Registry
    schemas: dict[str, dict[str, Any]]             # file name -> contents
    validators: dict[str, Any] = field(default_factory=dict)


_lock = threading.Lock()
_sets: dict[Path, _SchemaSet] = {}


def _dir_stamp(directory: Path) -> tuple[tuple[str, int, int], ...]:
    try:
        files = sorted(directory.glob("*.schema.json"))
        return tuple((p.name, p.stat().st_mtime_ns, p.stat().st_size) for p in files)
    except OSError as e:
        raise BusError(f"Şema klasörü okunamadı: {directory} ({e})") from e


def load_schema_files(directory: Path | None = None) -> dict[str, dict[str, Any]]:
    """All ``*.schema.json`` files of ``directory`` (default ``contracts/schemas``)."""
    d = schemas_dir() if directory is None else Path(directory)
    if not d.is_dir():
        raise BusError(f"Şema klasörü bulunamadı: {d}")
    out: dict[str, dict[str, Any]] = {}
    for p in sorted(d.glob("*.schema.json")):
        try:
            doc = json.loads(p.read_text(encoding="utf-8"))
        except (OSError, ValueError) as e:
            raise BusError(f"Şema dosyası okunamadı/geçersiz JSON: {p} ({e})") from e
        if not isinstance(doc, dict):
            raise BusError(f"Şema dosyası bir JSON nesnesi değil: {p}")
        expected = schema_uri(p.name)
        sid = doc.get("$id", expected)
        if sid != expected:
            raise BusError(f"Şema $id uyuşmazlığı: {p.name} için '{sid}' (beklenen '{expected}')")
        out[p.name] = doc
    return out


def build_registry(schemas: Mapping[str, Mapping[str, Any]]) -> Any:
    """``referencing.Registry`` holding every schema under its canonical ``$id``.

    The registry has no ``retrieve`` callable, so an unknown ``$ref`` raises instead of
    being fetched from the network.
    """
    require_backend()
    resources = [
        (schema_uri(name), DRAFT202012.create_resource(dict(doc))) for name, doc in schemas.items()
    ]
    return referencing.Registry().with_resources(resources).crawl()


def _schema_set() -> _SchemaSet:
    d = schemas_dir()
    stamp = _dir_stamp(d)
    with _lock:
        s = _sets.get(d)
        if s is None or s.stamp != stamp:
            schemas = load_schema_files(d)
            s = _SchemaSet(directory=d, stamp=stamp, registry=build_registry(schemas), schemas=schemas)
            _sets[d] = s
        return s


def clear_cache() -> None:
    """Forget cached registries/validators (schemas are re-read on next use)."""
    with _lock:
        _sets.clear()


def _validator_for(schema_file: str) -> Any:
    require_backend()
    s = _schema_set()
    with _lock:
        v = s.validators.get(schema_file)
        if v is None:
            doc = s.schemas.get(schema_file)
            if doc is None:
                raise BusError(f"Şema dosyası yok: {s.directory / schema_file}")
            v = Draft202012Validator(doc, registry=s.registry)
            s.validators[schema_file] = v
        return v


def get_validator(topic: str) -> Any:
    """Cached ``Draft202012Validator`` for the full message (envelope + data) of ``topic``."""
    require_backend()
    entry = registry().get(topic)
    if entry is None:
        raise UnknownTopic(f"Bilinmeyen topic: {topic!r} — kayıtlı olanlar: {sorted(registry())}")
    return _validator_for(entry["schema"])


# --------------------------------------------------------------------------- validation
def _raise_first(topic: str, validator: Any, instance: Any) -> None:
    try:
        errors = list(validator.iter_errors(instance))
    except referencing.exceptions.Unresolvable as e:
        raise BusError(f"'{topic}' şemasında çözülemeyen $ref: {e}") from e
    if errors:
        best = best_match(errors)
        raise MessageValidationError.from_error(topic, best, len(errors))


def validate_message(topic: str, envelope: Mapping[str, Any]) -> None:
    """Validate a complete envelope against the schema of ``topic``.

    Raises :class:`MessageValidationError` (also a ``jsonschema.ValidationError``) on the
    first (most relevant) error, :class:`UnknownTopic` for unregistered topics and
    :class:`BackendUnavailable` when ``jsonschema`` is not installed. An envelope whose
    ``topic`` field differs from ``topic`` is rejected (``const`` in the topic schema).
    """
    _raise_first(topic, get_validator(topic), envelope)


def validate_data(topic: str, data: Mapping[str, Any], src: str = "validate") -> None:
    """Validate a payload by wrapping it in a fresh envelope (``seq=0``, ``t_ns=now``)."""
    validate_message(topic, make_envelope(topic, src, 0, data, t_ns=now_ns()))


def validate_envelope(envelope: Mapping[str, Any]) -> None:
    """Validate only the envelope layer (any topic, ``data`` unchecked)."""
    topic = envelope.get("topic") if isinstance(envelope, Mapping) else None
    _raise_first(str(topic), _validator_for(ENVELOPE_SCHEMA), envelope)


def is_valid(topic: str, envelope: Mapping[str, Any]) -> bool:
    """True if ``envelope`` is valid for ``topic`` (unknown topic -> :class:`UnknownTopic`)."""
    return bool(get_validator(topic).is_valid(envelope))


def check_schemas() -> list[str]:
    """Self-check of ``contracts/``: returns a list of problems (Turkish), empty if OK.

    Checks that every schema file is a valid Draft 2020-12 schema with its canonical
    ``$id``, every registered topic has a schema file whose ``topic`` is ``const`` to the
    topic name, and every ``$ref`` in every file resolves through the local registry.
    """
    require_backend()
    problems: list[str] = []
    try:
        schemas = load_schema_files()
    except BusError as e:
        return [str(e)]
    for name, doc in schemas.items():
        if doc.get("$schema") != "https://json-schema.org/draft/2020-12/schema":
            problems.append(f"{name}: $schema Draft 2020-12 değil ({doc.get('$schema')!r})")
        try:
            Draft202012Validator.check_schema(doc)
        except SchemaError as e:
            problems.append(f"{name}: geçersiz şema — {e.message}")
    if ENVELOPE_SCHEMA not in schemas:
        problems.append(f"{ENVELOPE_SCHEMA} bulunamadı")
    for topic, entry in registry().items():
        doc = schemas.get(entry["schema"])
        if doc is None:
            problems.append(f"{topic}: şema dosyası yok ({entry['schema']})")
            continue
        const = doc.get("properties", {}).get("topic", {}).get("const")
        if const != topic:
            problems.append(f"{topic}: şemadaki topic const değeri {const!r}")
        refs = [r.get("$ref") for r in doc.get("allOf", []) if isinstance(r, dict)]
        if ENVELOPE_SCHEMA not in refs:
            problems.append(f"{topic}: allOf içinde {ENVELOPE_SCHEMA} $ref'i yok")
    reg = build_registry(schemas)
    for name in schemas:
        resolver = reg.resolver(base_uri=schema_uri(name))
        for ref in _iter_refs(schemas[name]):
            try:
                resolver.lookup(ref)
            except referencing.exceptions.Unresolvable as e:
                problems.append(f"{name}: çözülemeyen $ref {ref!r} ({e})")
    return problems


def _iter_refs(node: Any) -> Iterable[str]:
    if isinstance(node, dict):
        for k, v in node.items():
            if k == "$ref" and isinstance(v, str):
                yield v
            else:
                yield from _iter_refs(v)
    elif isinstance(node, list):
        for v in node:
            yield from _iter_refs(v)
