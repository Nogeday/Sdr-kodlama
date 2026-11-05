"""Time helpers.

Two clocks are used, never mixed:

* ``now_ns()``   — wall clock (``time.time_ns``). Comparable *across processes on the same
  host*; used in every bus message (``t_ns``, ``t_acq_ns``) and for end-to-end latency.
* ``mono()``     — monotonic seconds (``time.perf_counter``). Used for durations/rates
  inside one process (sweep timing, throughput, timeouts).
"""

from __future__ import annotations

import time
from contextlib import contextmanager
from dataclasses import dataclass, field


def now_ns() -> int:
    return time.time_ns()


def mono() -> float:
    return time.perf_counter()


def iso_utc(t_ns: int | None = None) -> str:
    """ISO-8601 UTC string (second resolution) for reports/filenames."""
    t = (now_ns() if t_ns is None else t_ns) / 1e9
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(t))


def stamp(t_ns: int | None = None) -> str:
    """Filesystem-safe local timestamp, e.g. 20261105-142233."""
    t = (now_ns() if t_ns is None else t_ns) / 1e9
    return time.strftime("%Y%m%d-%H%M%S", time.localtime(t))


@dataclass
class Stopwatch:
    """Accumulating named timers: ``with sw.time("tune"): ...`` then ``sw.totals``."""

    totals: dict[str, float] = field(default_factory=dict)
    counts: dict[str, int] = field(default_factory=dict)

    @contextmanager
    def time(self, name: str):
        t0 = mono()
        try:
            yield
        finally:
            dt = mono() - t0
            self.totals[name] = self.totals.get(name, 0.0) + dt
            self.counts[name] = self.counts.get(name, 0) + 1

    def mean(self, name: str) -> float:
        n = self.counts.get(name, 0)
        return self.totals.get(name, 0.0) / n if n else 0.0
