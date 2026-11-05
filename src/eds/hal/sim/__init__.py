"""Physics-based simulator backend (``backend = "sim"``).

* :class:`SimWorld`        shared RF scene (emitters, on/off timeline, device registry)
* :class:`Emitter`         one emitter (cw | noise | fm | pulse | gsm_fcch)
* :class:`SimRxDevice`     AD9363/Pluto receiver model (``RxDevice``) — see its module docstring
* :class:`SimSignalSource` signal generator that injects tones into the world
"""

from .device import SimRxDevice
from .source import SimSignalSource
from .world import Emitter, SimWorld

__all__ = ["SimWorld", "Emitter", "SimRxDevice", "SimSignalSource"]
