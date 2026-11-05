"""Message envelope of the EDS bus and its JSON wire encoding.

Every message is a ZMQ multipart ``[topic (utf-8), payload (utf-8 JSON)]``; the payload is::

    {"topic": "<== topic frame>", "v": 1, "src": "rx0", "seq": 17,
     "t_ns": 1793865600123456789, "data": {...}}

* ``seq`` counts per ``(src, topic)`` from 0. Gaps seen by a subscriber mean messages were
  dropped (PUB high-water mark) — :class:`eds.bus.pubsub.Subscriber` counts them.
* ``t_ns`` is ``time.time_ns()`` taken immediately before the message is sent (wall clock,
  comparable across processes on one host: end-to-end latency = ``t_recv_ns - t_ns``).
* ``*_ns`` integers exceed 2**53. Python keeps them exact; JavaScript consumers must parse
  them as ``BigInt`` (or only use ``t_ns / 1e9`` seconds as a float).
* JSON has no NaN/Infinity: :func:`sanitize` maps non-finite floats to ``null`` and numpy
  scalars/arrays to plain Python values; encoding uses ``allow_nan=False`` so nothing
  non-standard can reach a consumer.
"""

from __future__ import annotations

import json
import math
from collections.abc import Mapping
from typing import Any

import numpy as np

from ..core.errors import BusError, EDSError  # noqa: F401 (re-exported)
from ..core.timeutil import now_ns

ENVELOPE_VERSION = 1
ENVELOPE_KEYS: tuple[str, ...] = ("topic", "v", "src", "seq", "t_ns", "data")


class MessageEncodeError(BusError):
    """A value cannot be represented in the JSON wire format."""


class MessageDecodeError(BusError):
    """A received payload is not a valid v1 envelope."""


# --------------------------------------------------------------------------- sanitize
def _key(k: Any) -> str:
    """JSON object key, mirroring ``json.dumps`` key coercion (plus numpy scalars)."""
    if isinstance(k, str):
        return str(k)
    if isinstance(k, (bool, np.bool_)):
        return "true" if k else "false"
    if isinstance(k, (int, np.integer)):
        return str(int(k))
    if isinstance(k, (float, np.floating)):
        f = float(k)
        if not math.isfinite(f):
            raise MessageEncodeError(f"Sonlu olmayan sayı JSON nesne anahtarı olamaz: {k!r}")
        return float.__repr__(f)
    if k is None:
        return "null"
    raise MessageEncodeError(f"JSON nesne anahtarı olarak kullanılamayan tür: {type(k).__name__}")


def _array(a: np.ndarray) -> Any:
    kind = a.dtype.kind
    if kind == "f":
        finite = np.isfinite(a)
        if finite.all():
            return a.tolist()
        obj = a.astype(object)
        obj[~finite] = None
        return obj.tolist()
    if kind in "iub":
        return a.tolist()
    if kind == "U":
        return a.tolist()
    if kind == "O":
        return [sanitize(v) for v in a.tolist()]
    if kind == "c":
        raise MessageEncodeError(
            "Kompleks dizi JSON'a çevrilemez; gerçek ve sanal kısımları ayrı alanlarda gönderin"
        )
    raise MessageEncodeError(f"JSON'a çevrilemeyen numpy dizi tipi: {a.dtype}")


def sanitize(obj: Any) -> Any:
    """Return a JSON-safe deep copy of ``obj``.

    * ``float`` / numpy floating NaN or ±inf -> ``None``
    * numpy scalars -> Python ``bool`` / ``int`` / ``float`` / ``str``
    * numpy arrays -> (nested) lists, tuples -> lists, sets -> sorted lists
    * mapping keys -> ``str`` (same coercion as ``json.dumps``)

    Complex numbers, bytes and arbitrary objects raise :class:`MessageEncodeError` rather
    than being stringified silently.
    """
    t = type(obj)
    if obj is None or t is str or t is bool or t is int:
        return obj
    if t is float:
        return obj if math.isfinite(obj) else None
    if isinstance(obj, Mapping):
        return {_key(k): sanitize(v) for k, v in obj.items()}
    if isinstance(obj, (list, tuple)):
        return [sanitize(v) for v in obj]
    if isinstance(obj, np.ndarray):
        return _array(obj)
    if isinstance(obj, (bool, np.bool_)):
        return bool(obj)
    if isinstance(obj, (int, np.integer)):
        return int(obj)
    if isinstance(obj, (float, np.floating)):
        f = float(obj)
        return f if math.isfinite(f) else None
    if isinstance(obj, (str, np.str_)):
        return str(obj)
    if isinstance(obj, (set, frozenset)):
        items = [sanitize(v) for v in obj]
        try:
            return sorted(items)
        except TypeError:
            return sorted(items, key=repr)
    if isinstance(obj, (complex, np.complexfloating)):
        raise MessageEncodeError(
            f"Kompleks sayı JSON'a çevrilemez ({obj!r}); gerçek ve sanal kısımları ayrı gönderin"
        )
    raise MessageEncodeError(f"JSON'a çevrilemeyen tür: {t.__module__}.{t.__qualname__}")


# --------------------------------------------------------------------------- envelope
def _is_int(x: Any) -> bool:
    return isinstance(x, (int, np.integer)) and not isinstance(x, (bool, np.bool_))


def check_envelope(env: Any, *, error: type[BusError] = MessageDecodeError) -> None:
    """Structural check of a v1 envelope (no jsonschema needed). Raises ``error``."""
    if not isinstance(env, dict):
        raise error(f"Zarf bir JSON nesnesi olmalı (gelen: {type(env).__name__})")
    keys = set(env)
    missing = [k for k in ENVELOPE_KEYS if k not in keys]
    extra = sorted(keys - set(ENVELOPE_KEYS))
    if missing or extra:
        raise error(f"Zarf alanları hatalı — eksik: {missing}, fazla: {extra}")
    v = env["v"]
    if not _is_int(v) or v != ENVELOPE_VERSION:
        raise error(f"Desteklenmeyen zarf sürümü: {v!r} (beklenen {ENVELOPE_VERSION})")
    if not isinstance(env["topic"], str) or not env["topic"]:
        raise error("Zarf 'topic' alanı boş olmayan metin olmalı")
    if not isinstance(env["src"], str) or not env["src"]:
        raise error("Zarf 'src' alanı boş olmayan metin olmalı")
    if not _is_int(env["seq"]) or env["seq"] < 0:
        raise error(f"Zarf 'seq' alanı negatif olmayan tamsayı olmalı (gelen: {env['seq']!r})")
    if not _is_int(env["t_ns"]) or env["t_ns"] < 0:
        raise error(f"Zarf 't_ns' alanı negatif olmayan tamsayı olmalı (gelen: {env['t_ns']!r})")
    if not isinstance(env["data"], dict):
        raise error(f"Zarf 'data' alanı JSON nesnesi olmalı (gelen: {type(env['data']).__name__})")


def make_envelope(
    topic: str, src: str, seq: int, data: Mapping[str, Any], t_ns: int | None = None
) -> dict[str, Any]:
    """Build a v1 envelope; ``data`` is sanitized, ``t_ns`` defaults to ``time.time_ns()``."""
    if not isinstance(topic, str) or not topic:
        raise MessageEncodeError("topic boş olmayan metin olmalı")
    if not isinstance(src, str) or not src:
        raise MessageEncodeError("src boş olmayan metin olmalı")
    if not _is_int(seq) or seq < 0:
        raise MessageEncodeError(f"seq negatif olmayan tamsayı olmalı (gelen: {seq!r})")
    if t_ns is not None and (not _is_int(t_ns) or t_ns < 0):
        raise MessageEncodeError(f"t_ns negatif olmayan tamsayı olmalı (gelen: {t_ns!r})")
    if not isinstance(data, Mapping):
        raise MessageEncodeError(f"data bir nesne (dict) olmalı (gelen: {type(data).__name__})")
    return {
        "topic": topic,
        "v": ENVELOPE_VERSION,
        "src": src,
        "seq": int(seq),
        "t_ns": now_ns() if t_ns is None else int(t_ns),
        "data": sanitize(data),
    }


def dumps_compact(obj: Any) -> str:
    """Compact, strict JSON of an already sanitized object."""
    try:
        return json.dumps(obj, ensure_ascii=False, allow_nan=False, separators=(",", ":"))
    except (TypeError, ValueError) as e:
        raise MessageEncodeError(f"JSON kodlama hatası: {e}") from e


def encode(env: Mapping[str, Any]) -> bytes:
    """Envelope -> compact UTF-8 JSON bytes (sanitized, ``allow_nan=False``)."""
    if not isinstance(env, Mapping):
        raise MessageEncodeError(f"Zarf bir nesne (dict) olmalı (gelen: {type(env).__name__})")
    clean = sanitize(env)
    check_envelope(clean, error=MessageEncodeError)
    return dumps_compact({k: clean[k] for k in ENVELOPE_KEYS}).encode("utf-8")


def encode_spliced(topic: str, src: str, seq: int, t_ns: int, data_json: str) -> bytes:
    """Byte-identical to :func:`encode` for an envelope whose ``data`` was already encoded
    with :func:`dumps_compact`. Lets the publisher take ``t_ns`` *after* the (potentially slow)
    encoding of a large payload, so latency measurements are not biased by it."""
    return (
        '{"topic":' + dumps_compact(topic) + ',"v":' + str(ENVELOPE_VERSION) + ',"src":' + dumps_compact(src)
        + ',"seq":' + str(int(seq)) + ',"t_ns":' + str(int(t_ns)) + ',"data":' + data_json + "}"
    ).encode("utf-8")


def _reject_constant(name: str) -> Any:
    raise ValueError(f"JSON'da izin verilmeyen sabit: {name}")


def decode(buf: bytes | bytearray | memoryview | str) -> dict[str, Any]:
    """Wire payload -> envelope dict. Rejects NaN/Infinity tokens and non-v1 envelopes."""
    try:
        raw = buf if isinstance(buf, str) else bytes(buf)
        env = json.loads(raw, parse_constant=_reject_constant)
    except (ValueError, TypeError) as e:
        raise MessageDecodeError(f"Geçersiz JSON yükü: {e}") from e
    check_envelope(env, error=MessageDecodeError)
    return env
