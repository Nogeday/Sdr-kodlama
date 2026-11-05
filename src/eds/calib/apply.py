"""Applying a stored calibration: spectra (frequency axis + dBm), devices (hardware PPM),
temperature check and the ``cal.result`` bus payload.

PPM modes (``[cal] ppm_mode``):

* ``off``      — nothing is corrected (raw nominal frequencies).
* ``software`` — the hardware is left alone; :func:`apply_calibration` corrects the axis.
* ``hardware`` — :func:`apply_ppm_to_device` must succeed in hardware (Pluto
  ``xo_correction`` via the thin libiio layer, or Soapy ``setFrequencyCorrection``);
  otherwise :class:`CalibrationError`.
* ``auto``     — hardware if possible, else software.

There is never a double correction: :func:`apply_calibration` only applies the residual
``(1 + e_cal) / (1 + e_hw) − 1`` between the stored total error and the correction the
hardware already applied (``IQBlock.meta['ppm_hw']``, carried into the spectrum).
"""

from __future__ import annotations

import logging
import math
import threading
from collections.abc import Mapping
from typing import Any

from ..core.errors import CalibrationError, ConfigError
from ..core.timeutil import now_ns
from ..dsp.psd import Spectrum
from .ppm import PpmEstimate, _finite, device_temperature_c, jsonable, residual_ppm
from .store import DeviceCalibration

log = logging.getLogger(__name__)

PPM_MODES: tuple[str, ...] = ("auto", "hardware", "software", "off")
CAL_KINDS: tuple[str, ...] = ("ppm", "power", "image_rejection", "coherence", "iq")
_HW_AGC_MODES = frozenset({"hw_slow", "hw_fast", "hw_hybrid"})

# Devices (by serial) already warned about a missing PPM calibration in this process: a suite
# run opens the same receiver many times (one RxNode / measurement each), one warning is enough.
_NO_PPM_WARNED: set[str] = set()
_NO_PPM_LOCK = threading.Lock()


def _device_key(device: Any, cal: DeviceCalibration | None) -> str:
    serial = str(getattr(getattr(device, "info", None), "serial", "") or "")
    if not serial and cal is not None:
        serial = str(cal.serial or "")
    return f"serial:{serial}" if serial else f"name:{getattr(device, 'name', '?')}"


def _warn_no_ppm(device: Any, cal: DeviceCalibration | None) -> None:
    """Log the missing-PPM warning once per device serial and process (later: DEBUG)."""
    key = _device_key(device, cal)
    with _NO_PPM_LOCK:
        first = key not in _NO_PPM_WARNED
        _NO_PPM_WARNED.add(key)
    log.log(logging.WARNING if first else logging.DEBUG,
            "%s: PPM kalibrasyonu yok — frekanslar düzeltilmeyecek (önce `eds cal ppm`).",
            getattr(device, "name", "?"))


def _check_mode(ppm_mode: str) -> str:
    if ppm_mode not in PPM_MODES:
        raise ConfigError(f"ppm_mode = {ppm_mode!r} geçersiz; seçenekler: {list(PPM_MODES)}")
    return ppm_mode


def _spectrum_ppm_hw(spec: Spectrum) -> float:
    """Hardware correction of the analysed channel: ``block_meta['ppm_hw_members'][channel]``
    when a multi-device block carries per-member values, else ``block_meta['ppm_hw']``."""
    bm = spec.meta.get("block_meta")
    if isinstance(bm, Mapping):
        members = bm.get("ppm_hw_members")
        ch = spec.meta.get("channel")
        if isinstance(members, (list, tuple)) and isinstance(ch, int) and 0 <= ch < len(members):
            return float(members[ch])
        if "ppm_hw" in bm:
            return float(bm["ppm_hw"])
    if "ppm_hw" in spec.meta:
        return float(spec.meta["ppm_hw"])
    return 0.0


def _spectrum_gain_mode(spec: Spectrum) -> str | None:
    bm = spec.meta.get("block_meta")
    if isinstance(bm, Mapping) and "gain_mode" in bm:
        return str(bm["gain_mode"])
    return None


def apply_calibration(spec: Spectrum, cal: DeviceCalibration | None, ppm_mode: str,
                      ppm_hw: float | None = None) -> Spectrum:
    """Return a calibrated copy of ``spec`` (the input is not modified).

    * **Frequency** — if ``ppm_mode != "off"`` and ``cal.ppm`` exists, the residual
      ``(1 + e_cal)/(1 + e_hw) − 1`` is applied with :meth:`Spectrum.with_freq_correction`
      (``meta['ppm_corrected'] = True``, ``meta['ppm_residual_applied']``). ``ppm_hw`` is the
      hardware correction in effect for the capture; ``None`` takes it from the spectrum
      (``meta['block_meta']['ppm_hw']`` as set by ``spectrum_from_block``), else 0.
    * **Power** — if ``cal.power`` exists the spectrum is offset by
      ``offset_db(fc, gain)`` into ``dBm`` (``meta['calibrated'] = True``); otherwise it
      stays ``dBFS`` with ``calibrated = False``. A capture taken under hardware AGC is not
      power-calibrated (gain not sample-synchronous) and is flagged in
      ``meta['cal_note']``.

    Idempotent: a spectrum already frequency-corrected / already in dBm is not corrected
    twice.
    """
    if not isinstance(spec, Spectrum):
        raise ConfigError(f"Spectrum bekleniyordu, {type(spec).__name__} geldi")
    _check_mode(ppm_mode)
    out = spec.copy()
    hw = _spectrum_ppm_hw(spec) if ppm_hw is None else _finite(ppm_hw, "ppm_hw")
    # ---- frequency
    if spec.meta.get("ppm_corrected"):
        pass                                          # already corrected upstream
    elif ppm_mode != "off" and cal is not None and cal.ppm is not None:
        res = residual_ppm(cal.ppm.ppm, hw)
        out = out.with_freq_correction(res)
        out.meta["ppm_corrected"] = True
        out.meta["ppm_residual_applied"] = res
        out.meta["ppm_total"] = cal.ppm.ppm
        out.meta["ppm_hw"] = hw
    else:
        out.meta["ppm_corrected"] = False
    # ---- power
    if spec.meta.get("calibrated") and spec.unit != "dBFS":
        return out                                    # already in absolute units
    gm = _spectrum_gain_mode(spec)
    if cal is not None and cal.power is not None:
        if gm in _HW_AGC_MODES:
            out.meta["calibrated"] = False
            out.meta["cal_note"] = (f"Donanım AGC ({gm}) açıkken güç kalibrasyonu uygulanmadı: kazanç örnekle "
                                    "senkron bilinmiyor. Ölçüm için manuel kazanç kullanın.")
            log.warning(out.meta["cal_note"])
            return out
        if spec.unit != "dBFS":
            raise CalibrationError(f"Güç kalibrasyonu yalnızca dBFS spektruma uygulanır (birim: {spec.unit})")
        if not spec.gain_db:
            raise ConfigError("Spektrumda kazanç bilgisi yok (gain_db boş): güç kalibrasyonu uygulanamaz")
        f_ref = out.fc
        off = cal.power.offset_db(f_ref, spec.gain_db[0])
        out = out.with_offset_db(off, "dBm")
        out.meta["calibrated"] = True
        out.meta["power_offset_db"] = off
    else:
        out.meta["calibrated"] = False
    return out


def apply_ppm_to_device(device: Any, cal: DeviceCalibration | None, ppm_mode: str) -> str:
    """Apply the stored PPM to ``device`` according to ``ppm_mode``.

    Returns ``"off"`` (nothing applied: mode off or no PPM calibration), ``"software"``
    (caller corrects spectra with :func:`apply_calibration`), ``"hardware"`` or ``"soapy"``
    (the device compensates; ``IQBlock.meta['ppm_hw']`` then carries the value).
    ``hardware`` mode raises :class:`CalibrationError` when the device cannot correct; a
    group whose members end up with different mechanisms (``"mixed"``) is reset to 0 and
    handled in software under ``auto``. The "no PPM calibration" warning is logged once per
    device serial and process (repeats at DEBUG level).
    """
    _check_mode(ppm_mode)
    if ppm_mode == "off":
        return "off"
    if cal is None or cal.ppm is None:
        _warn_no_ppm(device, cal)
        return "off"
    if ppm_mode == "software":
        return "software"
    ppm = float(cal.ppm.ppm)
    path = device.set_ppm_correction(ppm)
    if path in ("hardware", "soapy"):
        log.info("%s: PPM %+.4f donanımda uygulandı (%s)", device.name, ppm, path)
        return str(path)
    if path == "mixed":
        device.set_ppm_correction(0.0)
        if ppm_mode == "hardware":
            raise CalibrationError(
                f"{device.name}: PPM düzeltmesi grubun yalnızca bazı üyelerinde donanımda uygulanabildi; "
                "tüm üyeler için ince katmanı (control_uri) yapılandırın ya da ppm_mode = 'software' kullanın.")
        log.warning("%s: PPM düzeltmesi üyelerde farklı yollarla uygulandı; donanım düzeltmesi sıfırlandı, "
                    "yazılımda düzeltilecek.", device.name)
        return "software"
    if ppm_mode == "hardware":
        raise CalibrationError(
            f"{device.name}: PPM düzeltmesi donanımda uygulanamadı (sürücü/ince katman desteklemiyor). "
            "Pluto için control_uri ile libiio ince katmanını etkinleştirin ya da ppm_mode = 'software' / 'auto' "
            "kullanın.")
    log.info("%s: donanım PPM düzeltmesi yok, yazılımda uygulanacak (%+.4f ppm)", device.name, ppm)
    return "software"


def check_temperature(cal: DeviceCalibration | None, device: Any, warn_delta_c: float = 5.0) -> str | None:
    """Turkish warning if the device is more than ``warn_delta_c`` away from the temperature
    of the PPM calibration (TCXO drift), else ``None`` (also when either is unknown)."""
    warn_delta_c = _finite(warn_delta_c, "warn_delta_c")
    if cal is None or cal.ppm is None or cal.ppm.temperature_c is None:
        return None
    t_now = device_temperature_c(device)
    if t_now is None:
        log.debug("%s: güncel sıcaklık okunamadı, sıcaklık kontrolü atlandı", getattr(device, "name", "?"))
        return None
    t_cal = float(cal.ppm.temperature_c)
    delta = t_now - t_cal
    if abs(delta) <= warn_delta_c:
        return None
    msg = (f"{getattr(device, 'name', '?')}: sıcaklık PPM kalibrasyonundan {delta:+.1f} °C farklı "
           f"(kalibrasyon {t_cal:.1f} °C, şimdi {t_now:.1f} °C; eşik ±{warn_delta_c:g} °C). TCXO sıcaklıkla "
           "kayar: cihaz ısındıktan sonra PPM kalibrasyonunu yenileyin.")
    log.warning(msg)
    return msg


def make_cal_payload(cal_kind: str, device: Any, values: Any, applied_via: str, *, t_ns: int | None = None,
                     temperature_c: float | None = None, serial: str | None = None) -> dict[str, Any]:
    """``cal.result`` bus payload (contracts/schemas/cal.result.schema.json).

    ``device`` is an :class:`~eds.hal.base.RxDevice` (name/serial taken from it) or a device
    name. ``values`` is a dict or an object with ``to_dict()`` (:class:`PpmEstimate`,
    :class:`PowerCalTable`, ``CoherenceReport``, ...). ``t_ns`` / ``temperature_c`` default
    to the values recorded in a :class:`PpmEstimate` (else now / the device sensor).
    """
    if cal_kind not in CAL_KINDS:
        raise ConfigError(f"Kalibrasyon türü {cal_kind!r} geçersiz; seçenekler: {list(CAL_KINDS)}")
    if not isinstance(applied_via, str) or not applied_via:
        raise ConfigError("applied_via boş olmayan bir metin olmalı (hardware | soapy | software | off | store)")
    if isinstance(device, str):
        name, ser = device, serial
    else:
        name = str(getattr(device, "name", ""))
        ser = serial if serial is not None else str(getattr(getattr(device, "info", None), "serial", "") or "")
    if not name:
        raise ConfigError("Kalibrasyon mesajı için cihaz adı gerekli")
    if hasattr(values, "to_dict"):
        vals = values.to_dict()
    elif isinstance(values, Mapping):
        vals = dict(values)
    else:
        raise ConfigError(f"values bir sözlük ya da to_dict() sağlayan nesne olmalı, {type(values).__name__} geldi")
    if t_ns is None:
        t_ns = values.t_ns if isinstance(values, PpmEstimate) and values.t_ns > 0 else now_ns()
    if temperature_c is None and isinstance(values, PpmEstimate):
        temperature_c = values.temperature_c
    if temperature_c is None and not isinstance(device, str):
        temperature_c = device_temperature_c(device)
    temp = None
    if temperature_c is not None and math.isfinite(float(temperature_c)):
        temp = float(temperature_c)
    data: dict[str, Any] = {
        "device": name, "kind": cal_kind, "values": jsonable(vals), "applied_via": applied_via,
        "t_ns": int(t_ns), "temperature_c": temp,
    }
    if ser:
        data["serial"] = ser
    return data
