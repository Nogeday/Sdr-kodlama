"""EDS message bus: ZeroMQ pub/sub, the v1 envelope and JSON Schema contracts.

* :mod:`eds.bus.topics`   — topic constants (mirror of ``contracts/topics.json``)
* :mod:`eds.bus.envelope` — envelope build / sanitize / encode / decode
* :mod:`eds.bus.pubsub`   — :class:`Publisher`, :class:`Subscriber`, proxy, config helpers
* :mod:`eds.bus.validate` — Draft 2020-12 validation (optional ``jsonschema``); its names
  are re-exported lazily so ``import eds.bus`` never imports ``jsonschema``.
"""

from __future__ import annotations

from typing import Any

from .envelope import (
    ENVELOPE_KEYS,
    ENVELOPE_VERSION,
    BusError,
    MessageDecodeError,
    MessageEncodeError,
    decode,
    encode,
    make_envelope,
    sanitize,
)
from .pubsub import (
    Message,
    ProxyThread,
    Publisher,
    Subscriber,
    SubscriberStats,
    publisher_from_config,
    run_proxy,
    subscriber_from_config,
)
from .topics import (
    ALL_TOPICS,
    CAL_RESULT,
    CTL_ACK,
    CTL_RX,
    DF_BEARING,
    MEAS_RESULT,
    RX_DETECTIONS,
    RX_SPECTRUM,
    RX_STATUS,
    RX_SWEEP,
    UnknownTopic,
    registry,
    schema_path,
)

_LAZY_VALIDATE = ("validate_message", "validate_data", "validate_envelope", "MessageValidationError")

__all__ = [
    "ALL_TOPICS", "RX_STATUS", "RX_SPECTRUM", "RX_DETECTIONS", "RX_SWEEP", "CAL_RESULT",
    "DF_BEARING", "MEAS_RESULT", "CTL_RX", "CTL_ACK", "registry", "schema_path", "UnknownTopic",
    "ENVELOPE_VERSION", "ENVELOPE_KEYS", "make_envelope", "sanitize", "encode", "decode",
    "BusError", "MessageEncodeError", "MessageDecodeError",
    "Publisher", "Subscriber", "SubscriberStats", "Message", "ProxyThread", "run_proxy",
    "publisher_from_config", "subscriber_from_config",
    *_LAZY_VALIDATE,
]


def __getattr__(name: str) -> Any:
    if name in _LAZY_VALIDATE:
        from . import validate

        return getattr(validate, name)
    raise AttributeError(f"module 'eds.bus' has no attribute {name!r}")
