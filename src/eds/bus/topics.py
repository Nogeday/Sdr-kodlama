"""Topic names of the EDS ZeroMQ bus and access to the topic registry.

``contracts/topics.json`` is the single source of truth for topic names; the constants
below mirror it one-to-one (``tests/bus/test_contracts.py`` enforces equality in both
directions). Every message travels as a ZMQ multipart ``[topic (utf-8), json (utf-8)]``
where the JSON is an envelope (``contracts/schemas/envelope.schema.json``) whose ``data``
member follows ``contracts/schemas/<topic>.schema.json``.

The schema ``$id`` URIs live under :data:`SCHEMA_BASE_URI`, a reserved ``.invalid``
domain (RFC 2606): they are *names*, never fetched from the network. Validators resolve
them through a local registry built from the files (see :mod:`eds.bus.validate`).
"""

from __future__ import annotations

import copy
import json
import os
import threading
from pathlib import Path
from typing import Any

from ..core.paths import contracts_dir
from .envelope import BusError

RX_STATUS = "rx.status"
RX_SPECTRUM = "rx.spectrum"
RX_DETECTIONS = "rx.detections"
RX_SWEEP = "rx.sweep"
CAL_RESULT = "cal.result"
DF_BEARING = "df.bearing"
MEAS_RESULT = "meas.result"
CTL_RX = "ctl.rx"
CTL_ACK = "ctl.ack"

ALL_TOPICS: tuple[str, ...] = (
    RX_STATUS, RX_SPECTRUM, RX_DETECTIONS, RX_SWEEP, CAL_RESULT, DF_BEARING, MEAS_RESULT,
    CTL_RX, CTL_ACK,
)

TOPICS_FILE = "topics.json"
SCHEMAS_SUBDIR = "schemas"
ENVELOPE_SCHEMA = "envelope.schema.json"
COMMON_SCHEMA = "common.schema.json"
SCHEMA_BASE_URI = "https://eds.invalid/contracts/schemas/"


class UnknownTopic(BusError):
    """The topic is not registered in ``contracts/topics.json``."""


_cache_lock = threading.Lock()
_cache: dict[Path, tuple[tuple[int, int], dict[str, Any]]] = {}


def contract_path() -> Path:
    """Path of ``contracts/topics.json`` (honours ``EDS_CONTRACTS_DIR``)."""
    return contracts_dir() / TOPICS_FILE


def schemas_dir() -> Path:
    """Directory holding the JSON Schema files (``contracts/schemas``)."""
    return contracts_dir() / SCHEMAS_SUBDIR


def _read_contract(path: Path) -> dict[str, Any]:
    try:
        raw = path.read_text(encoding="utf-8")
    except OSError as e:
        raise BusError(f"Topic kayıt dosyası okunamadı: {path} ({e})") from e
    try:
        doc = json.loads(raw)
    except ValueError as e:
        raise BusError(f"Topic kayıt dosyası geçerli JSON değil: {path} ({e})") from e
    if not isinstance(doc, dict) or not isinstance(doc.get("topics"), dict):
        raise BusError(f"Topic kayıt dosyasında 'topics' nesnesi yok: {path}")
    for name, entry in doc["topics"].items():
        if not isinstance(entry, dict) or not isinstance(entry.get("schema"), str) or not entry["schema"]:
            raise BusError(f"'{name}' topic kaydında 'schema' alanı eksik/geçersiz: {path}")
    return doc


def load_contract() -> dict[str, Any]:
    """Full content of ``contracts/topics.json`` (deep copy; cached until the file changes)."""
    path = contract_path()
    try:
        st = os.stat(path)
    except OSError as e:
        raise BusError(f"Topic kayıt dosyası bulunamadı: {path} ({e})") from e
    stamp = (st.st_mtime_ns, st.st_size)
    with _cache_lock:
        hit = _cache.get(path)
        if hit is None or hit[0] != stamp:
            hit = (stamp, _read_contract(path))
            _cache[path] = hit
        return copy.deepcopy(hit[1])


def registry() -> dict[str, dict[str, Any]]:
    """Topic name -> registry entry (``schema``, ``producer``, ``rate``, ``description``)."""
    return load_contract()["topics"]


def is_known(topic: str) -> bool:
    return topic in registry()


def schema_path(topic: str) -> Path:
    """Path of the JSON Schema file of ``topic`` (raises :class:`UnknownTopic`)."""
    entry = registry().get(topic)
    if entry is None:
        raise UnknownTopic(f"Bilinmeyen topic: {topic!r} — kayıtlı olanlar: {sorted(registry())}")
    return schemas_dir() / entry["schema"]


def envelope_schema_path() -> Path:
    name = load_contract().get("envelope", ENVELOPE_SCHEMA)
    return schemas_dir() / name


def schema_uri(file_name: str) -> str:
    """Canonical ``$id`` of the schema file ``file_name``."""
    return SCHEMA_BASE_URI + file_name
