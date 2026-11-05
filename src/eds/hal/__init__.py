"""Hardware abstraction layer.

* :mod:`eds.hal.base`          — :class:`RxDevice` contract (all backends)
* :mod:`eds.hal.factory`       — open devices from configuration (:func:`isolation_plan`:
                                 which device runs in a worker process)
* :mod:`eds.hal.proc`          — process isolation (:class:`ProcessRxDevice`,
                                 :class:`ProcessSignalSource`; one Pluto per process)
* :mod:`eds.hal.soapy_backend` — SoapySDR backend (primary; Pluto via SoapyPlutoSDR)
* :mod:`eds.hal.pluto_iio`     — thin libiio layer for Pluto-only controls
                                 (xo_correction/PPM, DC & quadrature tracking, temperature,
                                 RX DMA overflow register)
* :mod:`eds.hal.group`         — N single-channel devices as one multi-channel device
* :mod:`eds.hal.sim`           — physics-based simulator (CI, demos, measurement validation)
* :mod:`eds.hal.tx`            — signal sources for measurements (sim, Pluto TX, SCPI, manual)
"""

from .base import GAIN_MODES, RawRead, RxDevice
from .factory import enumerate_devices, get_sim_world, isolation_plan, open_device, open_devices, open_group
from .proc import ProcessRxDevice, ProcessSignalSource

__all__ = [
    "RxDevice", "RawRead", "GAIN_MODES", "open_device", "open_devices", "open_group",
    "enumerate_devices", "get_sim_world", "isolation_plan", "ProcessRxDevice", "ProcessSignalSource",
]
