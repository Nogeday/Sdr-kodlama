"""Unit helpers. Conventions (see docs/CONVENTIONS.md):

* All frequencies in **Hz**, all times in **seconds** unless the name ends in ``_ns``.
* Power in **dB** quantities: ``dBFS`` (relative to ADC full scale), ``dBm`` (absolute,
  requires power calibration), ``dBc`` (relative to a carrier), ``dB`` (ratio).
* 0 dBFS == power of a *complex* exponential whose magnitude equals full scale, i.e.
  ``|x| == 1.0`` in the normalized complex64 sample domain used everywhere in EDS.
* ``-174 dBm/Hz`` == thermal noise density kT at 290 K.
"""

from __future__ import annotations

import numpy as np

KT_DBM_HZ: float = -173.975  # 10*log10(k*290*1000)  (≈ -174 dBm/Hz)
C_LIGHT: float = 299_792_458.0

_TINY = 1e-30


def db10(x):
    """Power ratio -> dB.  Works on scalars and arrays; never returns -inf."""
    return 10.0 * np.log10(np.maximum(np.asarray(x, dtype=float), _TINY))


def db20(x):
    """Amplitude ratio -> dB."""
    return 20.0 * np.log10(np.maximum(np.abs(np.asarray(x, dtype=float)), _TINY))


def lin10(db):
    """dB -> power ratio."""
    return 10.0 ** (np.asarray(db, dtype=float) / 10.0)


def lin20(db):
    """dB -> amplitude ratio."""
    return 10.0 ** (np.asarray(db, dtype=float) / 20.0)


def dbm_to_watt(dbm):
    return 1e-3 * lin10(dbm)


def watt_to_dbm(w):
    return db10(np.asarray(w, dtype=float) / 1e-3)


def wavelength(freq_hz: float) -> float:
    return C_LIGHT / float(freq_hz)


def fmt_hz(hz: float, digits: int = 6) -> str:
    """Human friendly frequency string: 915000000 -> '915 MHz'."""
    a = abs(hz)
    for div, unit in ((1e9, "GHz"), (1e6, "MHz"), (1e3, "kHz")):
        if a >= div:
            return f"{hz / div:.{digits}g} {unit}"
    return f"{hz:.{digits}g} Hz"


def wrap_deg(a):
    """Wrap angle(s) in degrees to (-180, 180]."""
    a = np.asarray(a, dtype=float)
    w = (a + 180.0) % 360.0 - 180.0
    return np.where(w == -180.0, 180.0, w)


def wrap_rad(a):
    """Wrap angle(s) in radians to (-pi, pi]."""
    a = np.asarray(a, dtype=float)
    w = (a + np.pi) % (2 * np.pi) - np.pi
    return np.where(w == -np.pi, np.pi, w)
