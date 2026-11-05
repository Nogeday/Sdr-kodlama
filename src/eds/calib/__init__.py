"""Calibration: reference-clock PPM (CW reference or GSM FCCH), absolute power (dBFS → dBm),
per-device storage and application to spectra / devices.

* :mod:`eds.calib.ppm`   — :class:`PpmEstimate`, :func:`estimate_ppm_cw`,
  :func:`estimate_ppm_from_samples`, :func:`correct_freq`, :func:`tune_freq`
* :mod:`eds.calib.fcch`  — :func:`estimate_ppm_gsm_fcch`, :func:`arfcn_to_freq`
* :mod:`eds.calib.power` — :class:`PowerCalTable`, :func:`measure_power_cal`
* :mod:`eds.calib.store` — :class:`DeviceCalibration`, :class:`CalibrationStore`
* :mod:`eds.calib.apply` — :func:`apply_calibration`, :func:`apply_ppm_to_device`,
  :func:`check_temperature`, :func:`make_cal_payload`

PPM sign convention: docs/ARCHITECTURE.md section 2 (positive = TCXO fast,
``f_true = f_app (1 + e)``).
"""

from .apply import (
    CAL_KINDS,
    PPM_MODES,
    apply_calibration,
    apply_ppm_to_device,
    check_temperature,
    make_cal_payload,
)
from .fcch import (
    GSM_FCCH_BURST_S,
    GSM_FCCH_OFFSET_HZ,
    GSM_FRAME_S,
    FcchBurst,
    arfcn_to_freq,
    estimate_ppm_gsm_fcch,
)
from .power import PowerCalTable, measure_power_cal, measure_tone_dbfs
from .ppm import (
    PpmEstimate,
    channel_ppm_hw,
    compose_ppm,
    correct_freq,
    device_temperature_c,
    estimate_ppm_cw,
    estimate_ppm_from_samples,
    ppm_from_apparent,
    preserve_rx_settings,
    residual_ppm,
    tune_freq,
    xo_correction_hz,
)
from .store import CAL_FORMAT_VERSION, CalibrationStore, DeviceCalibration

__all__ = [
    # ppm
    "PpmEstimate", "estimate_ppm_cw", "estimate_ppm_from_samples", "correct_freq", "tune_freq",
    "compose_ppm", "residual_ppm", "ppm_from_apparent", "xo_correction_hz", "channel_ppm_hw", "device_temperature_c",
    "preserve_rx_settings",
    # fcch
    "estimate_ppm_gsm_fcch", "arfcn_to_freq", "FcchBurst", "GSM_FCCH_OFFSET_HZ", "GSM_FCCH_BURST_S", "GSM_FRAME_S",
    # power
    "PowerCalTable", "measure_power_cal", "measure_tone_dbfs",
    # store
    "DeviceCalibration", "CalibrationStore", "CAL_FORMAT_VERSION",
    # apply
    "apply_calibration", "apply_ppm_to_device", "check_temperature", "make_cal_payload", "PPM_MODES", "CAL_KINDS",
]
