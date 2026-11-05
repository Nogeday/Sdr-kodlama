"""Direction finding.

Phase 0: two-element phase interferometer on 2× Pluto (``DeviceGroup``; shared 40 MHz
reference required). Phase 1: KrakenSDR (5 coherent channels) — the DF code only sees
``RxDevice.num_channels`` / ``coherent``, so it does not change.

* :mod:`eds.df.coherence`      — :func:`estimate_delay`, :func:`phase_offset`, :func:`align`,
  :class:`CoherenceReport`, :func:`check_coherence`
* :mod:`eds.df.interferometer` — :class:`Interferometer2`, :class:`Bearing`
* :mod:`eds.df.metrics`        — :func:`rms_angle_error`, :func:`summarize`

Azimuth convention: broadside = 0°, positive along the array axis toward element 1
(docs/ARCHITECTURE.md section 2).
"""

from .coherence import (
    COHERENCE_MIN,
    DRIFT_LIMIT_DEG_S,
    PHASE_STD_MAX_DEG,
    CoherenceReport,
    DelayEstimate,
    align,
    analyze_coherence,
    check_coherence,
    estimate_delay,
    estimate_delay_ex,
    phase_offset,
    valid_slice,
)
from .interferometer import Bearing, Interferometer2
from .metrics import angle_errors, rms_angle_error, summarize

__all__ = [
    "estimate_delay", "estimate_delay_ex", "DelayEstimate", "phase_offset", "align", "valid_slice",
    "CoherenceReport", "analyze_coherence", "check_coherence",
    "DRIFT_LIMIT_DEG_S", "COHERENCE_MIN", "PHASE_STD_MAX_DEG",
    "Interferometer2", "Bearing",
    "rms_angle_error", "summarize", "angle_errors",
]
