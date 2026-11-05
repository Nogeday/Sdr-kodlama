"""RX chain on top of :mod:`eds.hal`: continuous streaming, software AGC, wideband sweep.

* :mod:`eds.rx.stream` — :class:`RxStreamer`: reader thread + bounded drop-oldest queue
* :mod:`eds.rx.agc`    — :class:`GainController`: software AGC for the 12-bit ADC
* :mod:`eds.rx.sweep`  — :class:`Sweeper` / :class:`SweepResult`: 325 MHz – 3.8 GHz
  step-and-stitch scanning with 20 MS/s bursts
"""

from .agc import GainController, classify_levels, gain_delta_db, validate_agc_config
from .stream import RxStreamer
from .sweep import Sweeper, SweepResult, gain_referred_unit, plan_coverage, plan_offsets

__all__ = [
    "RxStreamer",
    "GainController", "classify_levels", "gain_delta_db", "validate_agc_config",
    "Sweeper", "SweepResult", "gain_referred_unit", "plan_coverage", "plan_offsets",
]
