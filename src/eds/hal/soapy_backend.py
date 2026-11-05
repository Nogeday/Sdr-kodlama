"""SoapySDR backend — the primary stream/device abstraction (Pluto via SoapyPlutoSDR).

Every SoapySDR-supported radio (Pluto now; bladeRF/USRP/HackRF later) is opened through
:class:`SoapyRxDevice`. Pluto-only low-level controls that SoapySDR does not expose go
through the thin libiio layer :class:`eds.hal.pluto_iio.PlutoIIO` (``control_uri``), opened
together with the device, only for driver ``plutosdr`` (it establishes the ``xo_correction``
in effect and whether sample loss is detectable before anything else uses the device).

SoapyPlutoSDR facts relied upon (verified against the upstream sources; check your build
with ``SoapySDRUtil --probe="driver=plutosdr"``):

* **ONE libiio context per process.** ``PlutoSDR_Settings.cpp`` keeps
  ``static iio_context *ctx``: a second ``SoapySDR.Device`` for *any* Pluto in the same
  process silently reuses the first one's context (its ``uri`` is ignored) and destroying
  either device destroys the shared context under the other. EDS therefore refuses a second
  open SoapyPlutoSDR device per process (:func:`claim_pluto`; RX devices and the
  ``pluto_tx`` source alike, :class:`DeviceError`). Every Pluto runs in its own process
  (``[[device]] isolation``, see :mod:`eds.hal.factory`).
* **Native RX format CS16, full scale 2048.** The 12-bit ADC value arrives sign-extended in
  int16 (range [-2048, 2047]); the driver's CF32 conversion is ``counts / 2048``. EDS reads
  CS16 by default and normalises ``counts / fullScale`` so that ADC full scale == 1.0
  (``docs/ARCHITECTURE.md`` §2). ``getNativeStreamFormat`` is NOT callable as
  ``(direction, channel)`` in python3-soapysdr 0.8.1 (no typemap for ``double &fullScale``
  → ``TypeError``); SoapySDR ≥ 0.9 returns a ``NativeStreamFormat`` object
  (``.format``/``.fullScale``). Both are handled; for ``plutosdr`` the known
  ``("CS16", 2048)`` is used when the binding cannot tell.
* **Device args** read by the driver: ``uri`` (``usb:1.5.5`` / ``ip:192.168.2.1``),
  ``hostname`` and ``label`` only (anything else is ignored → warning). The only **stream
  arg** is ``bufflen`` = libiio buffer length in samples (= ``getStreamMTU``); EDS passes
  ``RxConfig.buffer_size``. Further stream args come from device args prefixed ``stream:``
  (``args = {"stream:bufflen" = "131072"}`` overrides; those keys are stripped from the
  device args). The settling window follows the *actual* buffer (``max(buffer_size, MTU)``).
* **Kernel buffers → stale samples after a retune.** libiio keeps ``kernel_buffers``
  (default 4) buffers queued; after a retune / rate / gain change the next
  ``kernel_buffers × bufflen`` samples were captured with the OLD settings. The base class
  flags them ``settling`` and :meth:`~eds.hal.base.RxDevice.capture` discards them.
* **Overflow is NEVER reported by SoapyPlutoSDR.** ``rx_streamer::recv`` returns an item
  count, 0 or ``SOAPY_SDR_TIMEOUT`` — never ``SOAPY_SDR_OVERFLOW``, never ``HAS_TIME``. EDS
  therefore polls the AXI ADC **DMA status register** of ``cf-ad9361-lpc``
  (``0x80000088``, bit 2 = overflow, write-1-to-clear; :mod:`eds.hal.pluto_iio`) through
  the thin layer after reads, at most every ``overflow_poll_s``. A set bit flags the NEXT
  block ``overflow`` (gap before it) and — because the gap sits behind the up to
  ``kernel_buffers`` buffers that were already queued when it happened — the following
  ``settle_samples`` samples ``settling``. ``overflow_source`` says what is in effect:
  ``"driver"`` (other Soapy drivers: ``SOAPY_SDR_OVERFLOW`` / hardware-timestamp jumps,
  ``detect_ts_gaps``), ``"dma_status"`` (Pluto + thin layer) or ``"none"`` (Pluto without
  the thin layer: sample loss is NOT detected; warning at stream start). It is known from the
  open on — a Pluto is never ``"driver"``: ``"dma_status"`` only if the thin layer opened at
  open can read the register, else ``"none"`` — so ``eds devices --probe`` shows the truth
  before any stream; stream start re-checks it (and clears the stale bit).
* **Stop / timeouts.** ``readStream`` holds the driver's RX mutex during
  ``iio_buffer_refill`` and ignores ``timeoutUs``; a stuck refill ends only with libiio's
  own context timeout (5 s default; SoapyPlutoSDR offers no argument to change it), and
  ``deactivateStream``/``closeStream`` wait for it. A read that returns after its stream was
  stopped is discarded (stream generation counter).
* **Selecting a Pluto by serial.** SoapyPlutoSDR enumerates ``{device, driver, uri,
  label="PlutoSDR #i <uri>"}`` — no serial — and does not filter by a ``serial`` arg (it
  opens the first context it finds). When ``args`` has ``serial`` and neither ``uri`` nor
  ``hostname``, :func:`resolve_serial_args` enumerates (driver filter only), takes each
  entry's serial from its ``serial`` key / ``serial=`` in ``label`` or else from the libiio
  scan (``iio.scan_contexts()``: ``uri → "... serial=<hw_serial>"``, what ``iio_info -s``
  prints), picks the entry matching the requested serial (case-insensitive; exact, else a
  unique prefix) and opens it by that ``uri``. No match / ambiguous prefix →
  :class:`DeviceNotFound` listing the candidates.
* **AGC:** ``setGainMode(RX, ch, true)`` selects AD9361 ``slow_attack``; ``fast_attack`` and
  ``hybrid`` need the thin layer, as do BB-DC / RF-DC / quadrature tracking (SoapyPlutoSDR
  has no DC-offset or IQ-balance API), ``xo_correction`` (hardware PPM correction), the
  overflow register and the AD9361 temperature sensor.
* **Thin layer vs. USB:** a second libiio USB context to a Pluto SoapyPlutoSDR is streaming
  from usually fails (interface busy). Set ``control_uri = "ip:192.168.2.1"`` (Pluto
  USB-Ethernet gadget; iiod serves several clients). The thin layer is used only if its
  ``hw_serial`` equals the streamed device's (else it would steer the other radio). A
  failure to open/verify it is a warning plus fallback, never a crash.
* **xo_correction is persistent until the Pluto reboots** (and may be preset by the firmware
  environment). The value in effect is read back and accounted for:
  ``ppm_hw = (xo_correction − ref)/ref·1e6`` — at open (the boot value) and after every
  write (the integer-Hz read-back, not the request; 1 Hz = 0.025 ppm). EDS restores the boot
  value on close if it changed it.
* **Runtime limits** reported by SoapyPlutoSDR are those of the AD9364 (e.g. 70 MHz–6 GHz)
  and a constant RX gain range 0..73 dB; the AD9363 profile limits (band-dependent gain
  −1..73 / −3..71 dB) are authoritative and are checked before any driver call.
"""

from __future__ import annotations

import itertools
import logging
import math
import re
import threading
import weakref
from collections.abc import Callable, Iterable, Mapping
from types import ModuleType
from typing import Any

import numpy as np

from ..core.config import DeviceConfig, RxConfig
from ..core.errors import (
    BackendUnavailable,
    ConfigError,
    DeviceError,
    DeviceNotFound,
    EDSError,
    NotSupported,
    StreamError,
)
from ..core.profile import DeviceProfile
from ..core.timeutil import mono, now_ns
from .base import RawRead, RxDevice
from .pluto_iio import PlutoIIO, scan_serials

log = logging.getLogger(__name__)

STREAM_FORMATS: tuple[str, ...] = ("CS16", "CF32")
DEFAULT_CS16_FULL_SCALE = 2048.0
STREAM_ARG_PREFIX = "stream:"
PLUTO_DRIVER = "plutosdr"
#: Device args SoapyPlutoSDR reads (``serial`` is resolved to ``uri`` by EDS).
PLUTO_DEVICE_ARGS = frozenset({"driver", "uri", "hostname", "label", "serial"})
#: Stream args SoapyPlutoSDR reads.
PLUTO_STREAM_ARGS = frozenset({"bufflen"})
#: Default minimum interval between two polls of the Pluto RX DMA status register.
DEFAULT_OVERFLOW_POLL_S = 0.02
#: Plausibility bound for an ``xo_correction`` read from the hardware info (AD9361: ±200 ppm).
_XO_MAX_PPM = 1000.0

#: EDS gain mode -> AD936x ``gain_control_mode``
AD936X_GAIN_MODES: dict[str, str] = {
    "manual": "manual", "hw_slow": "slow_attack", "hw_fast": "fast_attack", "hw_hybrid": "hybrid",
}

_LABEL_SERIAL_RE = re.compile(r"serial=([0-9A-Za-z]+)")

SOAPY_INSTALL_HINT = (
    "SoapySDR Python modülü bulunamadı. Kurulum: sudo apt install python3-soapysdr soapysdr-tools; "
    "Pluto için SoapyPlutoSDR sürücüsünü derleyin (bkz. scripts/install_ubuntu.sh). "
    'Doğrulama: SoapySDRUtil --find ve SoapySDRUtil --probe="driver=plutosdr".'
)


# ---------------------------------------------------------------------------- helpers
def import_soapy() -> ModuleType:
    """Import the SoapySDR SWIG module lazily; :class:`BackendUnavailable` if missing."""
    try:
        import SoapySDR  # type: ignore[import-not-found]
    except ImportError as e:
        raise BackendUnavailable(f"{SOAPY_INSTALL_HINT} ({e})") from e
    return SoapySDR


def kwargs_to_dict(kw: Any) -> dict[str, str]:
    """``SoapySDRKwargs`` (SWIG std::map proxy: ``keys()`` + ``[]``) or mapping → ``dict``."""
    if kw is None:
        return {}
    if isinstance(kw, Mapping):
        return {str(k): str(v) for k, v in kw.items()}
    return {str(k): str(kw[k]) for k in list(kw.keys())}


def release_device(sdr: ModuleType, dev: Any) -> str:
    """Release a ``SoapySDR.Device`` exactly once; returns the mechanism used.

    The python3-soapysdr binding defines ``Device.__del__`` → ``Device.unmake(self)``; an
    explicit ``unmake`` on top of it would unmake twice (``unknown device`` error from the
    factory). So: if the class has its own deleter, the caller just drops its reference
    ("deleter"); otherwise ``Device.unmake`` is called explicitly ("unmake"); else the
    reference is dropped and garbage collection releases it ("dropped").
    """
    if getattr(type(dev), "__del__", None) is not None:
        return "deleter"
    unmake = getattr(getattr(sdr, "Device", None), "unmake", None)
    if callable(unmake):
        unmake(dev)
        return "unmake"
    return "dropped"


def _compose_ppm(a: float, b: float) -> float:
    """``(1+e) = (1+e_a)(1+e_b)`` in ppm (same formula as :func:`eds.calib.ppm.compose_ppm`)."""
    return a + b + a * b * 1e-6


def _residual_ppm(total: float, hw: float) -> float:
    """``(1+e_res) = (1+e)/(1+e_hw)`` in ppm (same as :func:`eds.calib.ppm.residual_ppm`)."""
    return (total - hw) / (1.0 + hw * 1e-6)


def xo_ppm(xo_hz: float, ref_clock_hz: float) -> float:
    """PPM correction an AD936x ``xo_correction`` value applies: ``(xo − ref)/ref·1e6``."""
    return (float(xo_hz) - float(ref_clock_hz)) * 1e6 / float(ref_clock_hz)


# ---------------------------------------------------------------------------- one Pluto per process
_pluto_lock = threading.Lock()
_pluto_owners: dict[int, str] = {}
_pluto_seq = itertools.count(1)
#: Devices that must never be released: a second SoapyPlutoSDR instance that slipped past the
#: argument check shares the static context; destroying it would free the first one's context.
_pluto_quarantine: list[Any] = []


def is_pluto_args(args: Mapping[str, str]) -> bool:
    """True if the Soapy device args select the SoapyPlutoSDR driver."""
    return str(args.get("driver", "")).strip().lower() == PLUTO_DRIVER


def open_pluto_owners() -> list[str]:
    """Names of the SoapyPlutoSDR devices currently open in this process (at most one)."""
    with _pluto_lock:
        return list(_pluto_owners.values())


def claim_pluto(owner: str) -> int:
    """Reserve this process's single SoapyPlutoSDR slot for ``owner``; returns a token.

    SoapyPlutoSDR keeps ONE static libiio context per process, so a second instance would
    silently drive the first Pluto and destroy its context when closed. Raises
    :class:`DeviceError` (Turkish, with the remedy) if the slot is taken.
    """
    with _pluto_lock:
        if _pluto_owners:
            others = ", ".join(sorted(_pluto_owners.values()))
            raise DeviceError(
                f"{owner}: bu süreçte zaten açık bir ADALM-Pluto var ({others}). SoapyPlutoSDR süreç başına "
                "TEK libiio bağlamı kullanır: ikinci Pluto ilkinin bağlamını sessizce paylaşır (yanlış cihazı "
                "sürer) ve kapanınca ortak bağlamı yok eder. Her Pluto kendi sürecinde çalışmalıdır: ilgili "
                "[[device]] için isolation = \"subprocess\" yazın (varsayılan \"auto\" ile cihaz fabrikası bunu "
                "otomatik yapar); pluto_tx uyaranı da ayrı bir süreçte çalışmalıdır.")
        token = next(_pluto_seq)
        _pluto_owners[token] = str(owner)
        return token


def release_pluto(token: int) -> None:
    """Free a slot taken by :func:`claim_pluto` (idempotent)."""
    with _pluto_lock:
        _pluto_owners.pop(int(token), None)


def pluto_claim_for(obj: Any, owner: str) -> Callable[[], Any]:
    """:func:`claim_pluto` tied to ``obj``'s lifetime: the returned callable releases the slot
    (idempotent) and runs automatically if ``obj`` is garbage-collected without closing."""
    return weakref.finalize(obj, release_pluto, claim_pluto(owner))


def quarantine_pluto_device(dev: Any) -> None:
    """Keep a SoapyPlutoSDR instance alive forever instead of destroying the shared context."""
    _pluto_quarantine.append(dev)


def enumerate_soapy(args: Mapping[str, str] | None = None) -> list[dict[str, str]]:
    """``SoapySDR.Device.enumerate(args)`` as a list of plain ``dict[str, str]``."""
    sdr = import_soapy()
    filt = {str(k): str(v) for k, v in (args or {}).items()}
    try:
        found = sdr.Device.enumerate(filt)
    except Exception as e:
        raise DeviceError(f"SoapySDR cihaz taraması başarısız (args={filt}): {e}") from e
    return [kwargs_to_dict(k) for k in found]


def entry_serial(entry: Mapping[str, str]) -> str:
    """Serial of an enumeration entry: its ``serial`` key, else ``serial=...`` in ``label``;
    ``""`` if neither is present (SoapyPlutoSDR entries carry no serial — see
    :func:`fill_serials_from_libiio`)."""
    ser = str(entry.get("serial", "")).strip()
    if ser:
        return ser
    m = _LABEL_SERIAL_RE.search(str(entry.get("label", "")))
    return m.group(1) if m else ""


def fill_serials_from_libiio(found: list[dict[str, str]]) -> tuple[list[dict[str, str]], str]:
    """Add a ``serial`` key from the libiio scan (``uri → serial``) to entries that lack one.

    Returns ``(entries, note)``; ``note`` explains why the scan was unavailable ("" if it ran
    or was not needed).
    """
    if all(entry_serial(e) or not e.get("uri") for e in found):
        return found, ""
    try:
        serials = scan_serials()
    except EDSError as e:
        return found, f"Seri numaraları libiio taramasından okunamadı ({e})."
    out = []
    for e in found:
        uri = e.get("uri", "")
        if not entry_serial(e) and uri in serials:
            e = {**e, "serial": serials[uri]}
        out.append(e)
    return out, ""


def _describe_entries(entries: Iterable[Mapping[str, str]]) -> str:
    parts = []
    for e in entries:
        label = str(e.get("label", "")).strip()
        parts.append(f"serial={entry_serial(e) or '?'} uri={e.get('uri', '') or '?'}" + (f" ({label})" if label else ""))
    return "; ".join(parts) or "yok"


def select_by_serial(found: list[dict[str, str]], serial: str, *, name: str = "", note: str = "") -> dict[str, str]:
    """The enumeration entry whose serial matches ``serial`` (see module docstring).

    Case-insensitive; an exact match wins, otherwise ``serial`` may be a prefix that matches
    exactly one device (entries with the same ``uri`` count once). No match / ambiguous ->
    :class:`DeviceNotFound` listing the candidates (Turkish); ``note`` is appended.
    """
    who = f"{name}: " if name else ""
    want = str(serial).strip().lower()
    if not want:
        raise ConfigError(f"{who}args.serial boş olamaz")
    keyed = [(entry_serial(e).lower(), e) for e in found]
    pool = [e for s, e in keyed if s and s == want] or [e for s, e in keyed if s and s.startswith(want)]
    distinct: dict[str, dict[str, str]] = {}
    for e in pool:
        distinct.setdefault(e.get("uri", "") or entry_serial(e).lower(), e)
    if not distinct:
        hint = ""
        if any(not s for s, _ in keyed):
            hint = (" SoapyPlutoSDR taramasında seri numarası yoktur; EDS seri ↔ URI eşlemesini libiio "
                    "taramasından (python3-libiio, `iio_info -s`) alır.")
        raise DeviceNotFound(
            f"{who}seri numarası '{serial}' ile eşleşen SoapySDR cihazı yok. "
            f"Bulunan cihazlar: {_describe_entries(found)}.{hint}{(' ' + note) if note else ''} "
            "Kontrol: SoapySDRUtil --find, iio_info -s")
    if len(distinct) > 1:
        raise DeviceNotFound(
            f"{who}seri numarası '{serial}' birden çok cihazla eşleşiyor: {_describe_entries(distinct.values())}. "
            "Daha uzun bir seri numarası ya da args.uri verin.")
    return dict(next(iter(distinct.values())))


def resolve_serial_args(args: Mapping[str, str], *, name: str = "") -> dict[str, str]:
    """Device args with ``serial`` resolved to the matching entry's ``uri`` (see module doc).

    Unchanged copy when ``serial`` is absent or ``uri``/``hostname`` already address the
    device. Otherwise enumerates with the ``driver`` filter only, completes missing serials
    from the libiio scan (:func:`fill_serials_from_libiio`), selects the entry with
    :func:`select_by_serial`, sets ``uri`` (if the entry has one) and replaces ``serial`` by
    the entry's full serial.
    """
    out = {str(k): str(v) for k, v in args.items()}
    if "serial" not in out or "uri" in out or "hostname" in out:
        return out
    filt = {"driver": out["driver"]} if "driver" in out else {}
    found, note = fill_serials_from_libiio(enumerate_soapy(filt))
    entry = select_by_serial(found, out["serial"], name=name, note=note)
    full = entry_serial(entry)
    if full:
        out["serial"] = full
    if entry.get("uri"):
        out["uri"] = entry["uri"]
    log.info("%sseri %s -> uri %s", f"{name}: " if name else "", out["serial"], out.get("uri", "(sürücü seçer)"))
    return out


def unknown_pluto_args(device_args: Mapping[str, str], stream_args: Mapping[str, str]) -> str:
    """Turkish warning listing args SoapyPlutoSDR ignores ("" if none)."""
    dev = sorted(k for k in device_args if k not in PLUTO_DEVICE_ARGS)
    st = sorted(f"{STREAM_ARG_PREFIX}{k}" for k in stream_args if k not in PLUTO_STREAM_ARGS)
    bad = dev + st
    if not bad:
        return ""
    return (f"SoapyPlutoSDR şu argümanları tanımaz ve yok sayar: {bad}. Sürücünün okuduğu cihaz argümanları "
            "yalnızca uri, hostname, label (serial EDS tarafından URI'ye çözülür); akış argümanı yalnızca "
            "bufflen (\"stream:bufflen\"). Bu ayarlar ölçüm koşulu DEĞİLDİR.")


def _range_tuple(value: Any) -> tuple[float, float] | None:
    """``Range`` or iterable of ``Range`` (RangeList) → ``(min, max)``; None if empty."""
    if value is None:
        return None
    if hasattr(value, "minimum") and hasattr(value, "maximum"):
        return float(value.minimum()), float(value.maximum())
    los, his = [], []
    for r in value:
        los.append(float(r.minimum()))
        his.append(float(r.maximum()))
    if not los:
        return None
    return min(los), max(his)


def _list_tuple(values: Iterable[float] | None) -> tuple[float, float] | None:
    vals = [float(v) for v in (values or [])]
    return (min(vals), max(vals)) if vals else None


def _num_or_str(v: Any) -> float | str:
    s = str(v).strip()
    try:
        return float(s)
    except ValueError:
        return s


def parse_native_format(res: Any) -> tuple[str, float]:
    """``getNativeStreamFormat`` result → ``(format, fullScale)`` for every binding flavour:
    SoapySDR ≥ 0.9 ``NativeStreamFormat`` (``.format``/``.fullScale``), a bare format string
    (fullScale unknown = 0.0) or a ``[format, fullScale]`` sequence."""
    if res is None:
        return "", 0.0
    if isinstance(res, str):
        return res, 0.0
    fmt = getattr(res, "format", None)
    if fmt is not None and hasattr(res, "fullScale"):
        return str(fmt), float(res.fullScale)
    seq = list(res)
    return (str(seq[0]) if seq else ""), (float(seq[1]) if len(seq) > 1 else 0.0)


# ---------------------------------------------------------------------------- device
class SoapyRxDevice(RxDevice):
    """RX device opened through SoapySDR (``cfg.args`` = Soapy device kwargs).

    ``stream_format`` ("CS16" default | "CF32") is the host stream format
    (``RxConfig.stream_format``); ``detect_ts_gaps`` flags forward jumps of hardware
    timestamps as overflow (only when the driver sets ``SOAPY_SDR_HAS_TIME``);
    ``overflow_poll_s`` is the minimum interval between two polls of the Pluto RX DMA status
    register (0 = after every read). A ``serial`` device arg without ``uri``/``hostname`` is
    resolved to the matching device's ``uri`` (:func:`resolve_serial_args`; SoapyPlutoSDR
    itself ignores ``serial``). Only one SoapyPlutoSDR device may be open per process
    (:func:`claim_pluto`).
    """

    backend_name = "soapy"

    def __init__(self, cfg: DeviceConfig, profile: DeviceProfile, *, stream_format: str = "CS16",
                 detect_ts_gaps: bool = True, overflow_poll_s: float = DEFAULT_OVERFLOW_POLL_S):
        super().__init__(cfg, profile)
        fmt = str(stream_format).upper()
        if fmt not in STREAM_FORMATS:
            raise ConfigError(f"stream_format {stream_format!r} geçersiz; seçenekler: {list(STREAM_FORMATS)}")
        if not cfg.channels:
            raise ConfigError(f"{cfg.name}: en az bir RX kanalı gerekli (channels boş)")
        poll = float(overflow_poll_s)
        if not (math.isfinite(poll) and poll >= 0.0):
            raise ConfigError(f"overflow_poll_s >= 0 olmalı (geldi: {overflow_poll_s!r})")
        self.stream_format: str = fmt
        self.detect_ts_gaps = bool(detect_ts_gaps)
        self.overflow_poll_s = poll
        self._sdr = import_soapy()
        self._RX = int(self._sdr.SOAPY_SDR_RX)
        self.device_args: dict[str, str] = {
            str(k): str(v) for k, v in cfg.args.items() if not str(k).startswith(STREAM_ARG_PREFIX)
        }
        self.stream_args_extra: dict[str, str] = {
            str(k)[len(STREAM_ARG_PREFIX):]: str(v) for k, v in cfg.args.items()
            if str(k).startswith(STREAM_ARG_PREFIX) and len(str(k)) > len(STREAM_ARG_PREFIX)
        }
        if "driver" not in self.device_args and profile.soapy_driver:
            self.device_args["driver"] = profile.soapy_driver
        self.device_args = resolve_serial_args(self.device_args, name=self.name)

        # stream / runtime state
        self._stream: Any = None
        self._bufs: list[np.ndarray] = []
        self._cap = 0
        self._stream_gen = 0
        self._scale = DEFAULT_CS16_FULL_SCALE
        self._limits: dict[str, tuple[float, float]] | None = None
        self._ts_expect: int | None = None
        self._zero_since: float | None = None
        self.ts_gap_events = 0
        self.stream_mtu = 0
        self.native_format: tuple[str, float] = ("", 0.0)
        self.native_format_source = ""
        self.agc_mode_actual = "manual"
        # reference clock: xo_correction in effect (Pluto), Soapy frequency correction
        self.xo_correction_hz: int | None = None
        self.xo_boot_hz: int | None = None
        self._xo_ppm = 0.0
        self._xo_written = False
        self._soapy_ppm_applied = 0.0
        # thin layer + overflow detection
        self._thin_obj: PlutoIIO | None = None
        self._thin_tried = False
        self.thin_serial_verified = False
        self.overflow_source = "none" if is_pluto_args(self.device_args) else "driver"   # final value below
        self._dma_thin: PlutoIIO | None = None
        self._dma_pending = False
        self._dma_last_poll = -math.inf
        self.dma_overflow_events = 0
        self._hwinfo: dict[str, str] = {}
        self._pluto_release: Callable[[], Any] | None = None

        if is_pluto_args(self.device_args):
            self._pluto_release = pluto_claim_for(self, self.name)
            msg = unknown_pluto_args(self.device_args, self.stream_args_extra)
            if msg:
                self._warn(msg)
        try:
            self._dev: Any = self._sdr.Device(dict(self.device_args))
        except Exception as e:
            self._release_pluto_slot()
            raise DeviceNotFound(self._not_found_message(e)) from e
        try:
            self._fill_info()
        except Exception as e:
            try:
                self._close()
            except EDSError as close_err:
                log.warning("%s: açılış hatası sonrası kapatma da başarısız: %s", self.name, close_err)
            raise DeviceError(f"{self.name}: cihaz kimliği okunamadı (getDriverKey/getHardwareKey): {e}") from e
        if self._pluto_release is None and self.info.driver.lower() == PLUTO_DRIVER:
            # a Pluto opened without driver=plutosdr in the args: claim after the fact
            try:
                self._pluto_release = pluto_claim_for(self, self.name)
            except DeviceError:
                # this instance shares the static context of the Pluto already open: releasing
                # it would destroy that context too, so it is parked for the process lifetime
                dev, self._dev = self._dev, None
                quarantine_pluto_device(dev)
                raise
        if self.is_pluto:
            # opened with the device: the xo_correction in effect (ppm_hw) and the overflow
            # detection the streams will have are known from here on (a failure = warning)
            try:
                self.overflow_source = self._overflow_source_at_rest(self.thin_layer())
            except Exception as e:  # libiio errors are warnings inside thin_layer(): this is a bug
                try:
                    self._close()
                except EDSError as close_err:
                    log.warning("%s: açılış hatası sonrası kapatma da başarısız: %s", self.name, close_err)
                raise DeviceError(f"{self.name}: Pluto ince katmanı açılırken beklenmeyen hata: {e!r}") from e
        else:
            self.overflow_source = "driver"

    # ------------------------------------------------------------------ open / identity
    def _not_found_message(self, err: Exception) -> str:
        try:
            found = enumerate_soapy({"driver": self.device_args["driver"]} if "driver" in self.device_args else {})
            seen = "; ".join(f.get("label") or ",".join(f"{k}={v}" for k, v in f.items()) for f in found) or "yok"
        except EDSError as e:
            seen = f"tarama başarısız: {e}"
        return (f"{self.name}: SoapySDR cihazı açılamadı (args={self.device_args}): {err}. "
                f"Bulunan cihazlar: {seen}. Kontrol: SoapySDRUtil --find")

    def _fill_info(self) -> None:
        d = self._dev
        driver = str(d.getDriverKey())
        hardware = str(d.getHardwareKey())
        try:
            hw = kwargs_to_dict(d.getHardwareInfo())
        except Exception as e:  # optional query
            log.debug("%s: getHardwareInfo başarısız: %s", self.name, e)
            hw = {}
        self._hwinfo = hw
        host = self.device_args.get("hostname", "")
        self.info.driver = driver
        self.info.hardware = hardware
        self.info.serial = hw.get("serial") or hw.get("hw_serial") or self.device_args.get("serial", "")
        self.info.firmware = hw.get("fw_version") or hw.get("firmware", "")
        self.info.uri = self.device_args.get("uri") or (f"ip:{host}" if host else "") or hw.get("uri", "")
        self.info.extra = dict(hw)
        self.info.extra["stream_format"] = self.stream_format
        asked, actual = self.device_args.get("uri", "").strip(), str(hw.get("uri", "")).strip()
        if self.is_pluto and asked.startswith("usb:") and actual.startswith("usb:") and asked != actual:
            # SoapyPlutoSDR's static libiio context ignores 'uri' once a context exists
            self._warn(f"İstenen URI {asked} ama sürücünün bağlamı {actual}: SoapyPlutoSDR bu süreçte başka bir "
                       "Pluto'nun bağlamını kullanıyor olabilir — her Pluto ayrı süreçte açılmalı.")

    @property
    def is_pluto(self) -> bool:
        return PLUTO_DRIVER in (self.device_args.get("driver", "").lower(), self.info.driver.lower())

    def _release_pluto_slot(self) -> None:
        rel, self._pluto_release = self._pluto_release, None
        if rel is not None:
            rel()

    def _require(self) -> Any:
        if self._dev is None:
            raise DeviceError(f"{self.name}: cihaz kapalı")
        return self._dev

    def _call(self, what: str, fn: Callable[..., Any], *args: Any) -> Any:
        try:
            return fn(*args)
        except Exception as e:
            raise DeviceError(f"{self.name}: SoapySDR {what}{args} başarısız: {e}") from e

    def _warn(self, msg: str) -> None:
        if msg not in self.warnings:
            self.warnings.append(msg)
        log.warning("%s: %s", self.name, msg)

    def _all_channels(self, what: str, pred: Callable[[int], Any]) -> bool:
        """True iff ``pred(ch)`` holds for every configured channel (missing API → False)."""
        try:
            return all(bool(pred(ch)) for ch in self.cfg.channels)
        except Exception as e:
            log.debug("%s: %s sorgusu başarısız: %s", self.name, what, e)
            return False

    # ------------------------------------------------------------------ thin libiio layer
    def thin_uri(self) -> str:
        """libiio URI for the Pluto thin layer: ``control_uri`` | args ``uri`` | ``ip:<hostname>``."""
        cu = (self.cfg.control_uri or "").strip()
        if cu.lower() == "none":
            return ""
        if cu:
            return cu
        uri = self.device_args.get("uri", "").strip()
        if uri:
            return uri
        host = self.device_args.get("hostname", "").strip()
        if host:
            return f"ip:{host}"
        return self._hwinfo.get("uri", "").strip()

    def thin_layer(self) -> PlutoIIO | None:
        """The Pluto thin libiio layer, opened once — at device open for a Pluto (None if
        unavailable; the open is not retried).

        Opening it also establishes the reference-clock state: the ``xo_correction`` in
        effect (boot value) becomes ``ppm_hw`` (see module docstring).
        """
        if self._thin_obj is not None:
            return self._thin_obj
        with self._lock:
            if self._thin_obj is not None:
                return self._thin_obj
            if self._thin_tried or self._dev is None:
                return None
            self._thin_tried = True
            if not self.is_pluto:
                return None
            thin = self._open_thin()
            if thin is None:
                self._adopt_unverified_boot_xo()
                return None
            self._thin_obj = thin
            if thin.xo_boot_hz is not None:
                self.info.extra["xo_correction_boot_hz"] = str(thin.xo_boot_hz)
                self.xo_boot_hz = int(thin.xo_boot_hz)
                self._set_xo_state(self.xo_boot_hz)
                if self._xo_ppm != 0.0:
                    self.ppm_hw = _compose_ppm(self._xo_ppm, self._soapy_ppm_applied)
                    self.ppm_hw_via = "hardware"
                    self._warn(
                        f"xo_correction açılışta {thin.xo_boot_hz} Hz (nominal {self.profile.ref_clock_hz:.0f} Hz "
                        f"değil; firmware ortamı ya da önceki bir çalıştırma): donanım {self._xo_ppm:+.4f} ppm düzeltme "
                        "uyguluyor. EDS bunu ppm_hw olarak hesaba katar (kalibrasyon yine nominale göre TOPLAM ppm "
                        "saklar); EDS değeri değiştirirse kapanışta açılış değerini geri yükler.")
            return thin

    def _open_thin(self) -> PlutoIIO | None:
        if (self.cfg.control_uri or "").strip().lower() == "none":
            return None
        uri = self.thin_uri()
        if not uri:
            self._warn("Pluto ince libiio katmanı için URI yok (control_uri / args.uri / args.hostname); "
                       "xo_correction, tracking, hızlı AGC, overflow algılama ve AD9361 sıcaklığı kullanılamaz.")
            return None
        try:
            thin = PlutoIIO(uri)
        except EDSError as e:
            hint = (' USB üzerinden SoapyPlutoSDR akışıyla aynı anda ikinci bir libiio USB bağlamı genellikle '
                    'açılamaz; control_uri = "ip:192.168.2.1" kullanın.') if uri.startswith("usb:") else ""
            self._warn(f"Pluto ince katmanı ({uri}) açılamadı: {e}.{hint} Soapy yoluna düşülüyor.")
            return None
        want = (self.info.serial or "").strip()
        try:
            got = thin.serial().strip()
        except EDSError as e:
            log.debug("%s: ince katman seri numarası okunamadı: %s", self.name, e)
            got = ""
        if want and got and want.lower() != got.lower():
            try:
                thin.close()
            except EDSError as e:
                log.debug("%s: ince katman kapatılamadı: %s", self.name, e)
            self._warn(f"İnce katman ({uri}) BAŞKA bir Pluto'ya bağlı: seri {got} ≠ akış cihazının serisi {want}. "
                       "control_uri bu cihazın IP'si olmalı (iki Pluto: config.txt ile farklı IP). İnce katman "
                       "KULLANILMIYOR: xo_correction, tracking, hızlı AGC ve overflow algılama devre dışı.")
            return None
        self.thin_serial_verified = bool(want and got)
        if not self.thin_serial_verified:
            self._warn(f"İnce katmanın ({uri}) akış cihazıyla aynı Pluto olduğu seri numarasıyla doğrulanamadı "
                       f"(akış: {want or '?'}, ince katman: {got or '?'}); control_uri'yi kontrol edin.")
        self.info.extra["control_uri"] = uri
        return thin

    def _set_xo_state(self, xo_hz: int) -> None:
        self.xo_correction_hz = int(xo_hz)
        self._xo_ppm = xo_ppm(self.xo_correction_hz, self.profile.ref_clock_hz)

    def _adopt_unverified_boot_xo(self) -> None:
        """Without the thin layer the driver value cannot be read; the firmware environment's
        boot value (context attribute ``ad9361-phy,xo_correction``) is the best estimate."""
        txt = str(self._hwinfo.get("ad9361-phy,xo_correction", "")).strip()
        try:
            xo = int(round(float(txt)))
        except ValueError:
            return
        ref = float(self.profile.ref_clock_hz)
        if xo <= 0 or xo == int(round(ref)) or abs(xo_ppm(xo, ref)) > _XO_MAX_PPM:
            return
        self.xo_boot_hz = xo
        self._set_xo_state(xo)
        self.ppm_hw = _compose_ppm(self._xo_ppm, self._soapy_ppm_applied)
        self.ppm_hw_via = "hardware"
        self._warn(f"Firmware ortamına göre xo_correction = {xo} Hz ({self._xo_ppm:+.4f} ppm) açılışta uygulanmış; "
                   "ince katman olmadan doğrulanamıyor ve değiştirilemiyor. ppm_hw bu değerle hesaplanır.")

    # ------------------------------------------------------------------ configuration
    def configure(self, rx: RxConfig) -> list[str]:
        if self.is_pluto:
            self.thin_layer()      # ppm_hw must reflect the xo_correction in effect before any use
        return super().configure(rx)

    @property
    def settle_samples(self) -> int:
        """Settling window in samples, from the ACTUAL driver buffer (``stream:bufflen`` may
        make it larger than ``buffer_size``)."""
        return int(self.settle_buffers * max(int(self.buffer_size), int(getattr(self, "stream_mtu", 0) or 0)))

    # ------------------------------------------------------------------ limits
    def runtime_limits(self) -> dict[str, Any]:
        """Driver-reported limits. For ``plutosdr`` the driver's constant RX gain range
        (0..73 dB, not band-aware) is reported as ``gain_db_driver`` only, so the profile's
        band-dependent range (−1..73 / −3..71 dB) stays in effect."""
        if self._limits is not None:
            return dict(self._limits)
        d = self._dev
        if d is None:
            return {}
        ch, rx = self.cfg.channels[0], self._RX
        lim: dict[str, tuple[float, float]] = {}

        def query(name: str, fn: Callable[[], Any], conv: Callable[[Any], tuple[float, float] | None]):
            try:
                return conv(fn())
            except Exception as e:  # missing method (old binding) or driver NotSupported
                log.debug("%s: %s kullanılamadı: %s", self.name, name, e)
                return None

        r = query("getFrequencyRange", lambda: d.getFrequencyRange(rx, ch), _range_tuple)
        if r:
            lim["freq_hz"] = r
        r = (query("getSampleRateRange", lambda: d.getSampleRateRange(rx, ch), _range_tuple)
             or query("listSampleRates", lambda: d.listSampleRates(rx, ch), _list_tuple))
        if r:
            lim["sample_rate_hz"] = r
        r = (query("getBandwidthRange", lambda: d.getBandwidthRange(rx, ch), _range_tuple)
             or query("listBandwidths", lambda: d.listBandwidths(rx, ch), _list_tuple))
        if r:
            lim["bandwidth_hz"] = r
        r = query("getGainRange", lambda: d.getGainRange(rx, ch), _range_tuple)
        if r:
            lim["gain_db_driver" if self.is_pluto else "gain_db"] = r
        self._limits = lim
        return dict(lim)

    # ------------------------------------------------------------------ primitives
    def _apply_sample_rate(self, hz: float) -> float:
        d = self._require()
        for ch in self.cfg.channels:
            self._call("setSampleRate", d.setSampleRate, self._RX, ch, hz)
        self._ts_expect = None
        return float(self._call("getSampleRate", d.getSampleRate, self._RX, self.cfg.channels[0]))

    def _apply_bandwidth(self, hz: float) -> float:
        d = self._require()
        for ch in self.cfg.channels:
            self._call("setBandwidth", d.setBandwidth, self._RX, ch, hz)
        return float(self._call("getBandwidth", d.getBandwidth, self._RX, self.cfg.channels[0]))

    def _apply_center_freq(self, hz: float) -> float:
        d = self._require()
        for ch in self.cfg.channels:
            self._call("setFrequency", d.setFrequency, self._RX, ch, hz)
        return float(self._call("getFrequency", d.getFrequency, self._RX, self.cfg.channels[0]))

    def _apply_gain(self, db: float, channel: int) -> float:
        d = self._require()
        ch = self.cfg.channels[channel]
        self._call("setGain", d.setGain, self._RX, ch, db)
        return float(self._call("getGain", d.getGain, self._RX, ch))

    def _apply_gain_mode(self, mode: str) -> None:
        if mode not in AD936X_GAIN_MODES:
            raise ConfigError(f"Kazanç modu {mode!r} geçersiz; seçenekler: {list(AD936X_GAIN_MODES)}")
        d = self._require()
        automatic = mode != "manual"
        for ch in self.cfg.channels:
            self._call("setGainMode", d.setGainMode, self._RX, ch, automatic)
        target = AD936X_GAIN_MODES[mode]
        self.agc_mode_actual = target if not automatic else ("slow_attack" if self.is_pluto else "driver_auto")
        thin = self.thin_layer()
        if thin is not None:
            try:
                self.agc_mode_actual = thin.set_gain_control_mode(target)
            except EDSError as e:
                self._warn(f"AD9361 gain_control_mode = {target} ince katmanla ayarlanamadı: {e}")
        elif mode in ("hw_fast", "hw_hybrid"):
            if self.is_pluto:
                self._warn(f"{mode}: SoapySDR yalnızca AD9361 'slow_attack' AGC'sini açabilir; '{target}' için "
                           "Pluto ince katmanı (control_uri) gerekli. Şu an slow_attack etkin.")
            else:
                self._warn(f"{mode}: bu sürücüde AGC türü seçilemiyor; sürücünün otomatik kazancı etkin.")

    def _native_format(self, ch: int) -> tuple[str, float]:
        """``(format, fullScale)`` of the RX stream (see module docstring for the bindings)."""
        d = self._require()
        try:
            fmt, full = parse_native_format(d.getNativeStreamFormat(self._RX, ch))
        except Exception as e:  # 0.8.x binding: TypeError (no typemap for 'double &fullScale')
            log.debug("%s: getNativeStreamFormat kullanılamadı: %s", self.name, e)
            fmt, full = "", 0.0
        if fmt:
            self.native_format_source = "binding"
            return fmt, full
        if self.is_pluto:
            self.native_format_source = "plutosdr_default"     # SoapyPlutoSDR: RX CS16, fullScale 2048
            return "CS16", DEFAULT_CS16_FULL_SCALE
        self.native_format_source = "unknown"
        return "", 0.0

    def _start(self, buffer_size: int) -> None:
        d = self._require()
        sdr = self._sdr
        chans = list(self.cfg.channels)
        thin = self.thin_layer() if self.is_pluto else None     # before the DMA starts running
        native, full = self._native_format(chans[0])
        self.native_format = (native, full)
        if self.stream_format == "CS16":
            if native == "CS16" and full > 0:
                scale = full
            else:
                scale = DEFAULT_CS16_FULL_SCALE
                self._warn(f"Doğal akış biçimi {native or '?'} (tam ölçek {full:g}); CS16 tam ölçeği "
                           f"{DEFAULT_CS16_FULL_SCALE:g} varsayıldı — doğru ölçek için stream_format = \"CF32\" kullanın.")
            soapy_fmt = sdr.SOAPY_SDR_CS16
        else:
            scale = 1.0
            soapy_fmt = sdr.SOAPY_SDR_CF32
        args = {"bufflen": str(int(buffer_size))}
        args.update(self.stream_args_extra)
        try:
            stream = d.setupStream(self._RX, soapy_fmt, chans, args)
        except Exception as e:
            raise StreamError(f"{self.name}: setupStream({self.stream_format}, {chans}, {args}) başarısız: {e}") from e
        try:
            mtu = int(d.getStreamMTU(stream))
        except Exception as e:
            log.debug("%s: getStreamMTU başarısız: %s", self.name, e)
            mtu = 0
        cap = max(int(buffer_size), mtu, 1)
        if self.stream_format == "CS16":
            bufs = [np.zeros(2 * cap, dtype=np.int16) for _ in chans]
        else:
            bufs = [np.zeros(cap, dtype=np.complex64) for _ in chans]
        try:
            ret = d.activateStream(stream)
        except Exception as e:
            self._close_stream_quietly(stream, f"activateStream istisnası: {e}")
            raise StreamError(f"{self.name}: activateStream başarısız: {e}") from e
        if isinstance(ret, (int, np.integer)) and int(ret) != 0:
            msg = f"{self.name}: activateStream hata kodu {int(ret)} ({sdr.errToStr(int(ret))})"
            self._close_stream_quietly(stream, msg)
            raise StreamError(msg)
        self._stream_gen += 1
        self._stream, self._bufs, self._cap, self._scale = stream, bufs, cap, float(scale)
        self.stream_mtu = mtu
        self._ts_expect = None
        self._zero_since = None
        self._setup_overflow_detection(thin)

    def _overflow_source_at_rest(self, thin: PlutoIIO | None) -> str:
        """The overflow detection the next stream will have, known without streaming:
        ``"driver"`` (not a Pluto), ``"dma_status"`` (the thin layer can read the RX DMA status
        register; read only, the bit is cleared at stream start) or ``"none"``."""
        if not self.is_pluto:
            return "driver"
        if thin is None or not thin.rx_overflow_supported():
            return "none"
        try:
            thin.read_rx_dma_status()
        except EDSError as e:
            log.debug("%s: RX DMA durum yazmacı okunamadı: %s", self.name, e)
            return "none"
        return "dma_status"

    def _setup_overflow_detection(self, thin: PlutoIIO | None) -> None:
        self._dma_thin, self._dma_pending, self._dma_last_poll = None, False, -math.inf
        if not self.is_pluto:
            self.overflow_source = "driver"
            return
        self.overflow_source = "none"
        if thin is not None and thin.rx_overflow_supported():
            try:
                thin.clear_rx_overflow()            # stale bit from an earlier stream / idle time
                self._dma_thin = thin
                self.overflow_source = "dma_status"
            except EDSError as e:
                log.debug("%s: DMA durum yazmacı temizlenemedi: %s", self.name, e)
        if self.overflow_source == "none":
            self._warn("SoapyPlutoSDR örnek kaybını (overflow) RAPORLAMAZ ve ince katmandan RX DMA durum yazmacı "
                       "(cf-ad9361-lpc 0x80000088) okunamıyor: overflow sayısı BİLİNMİYOR (0 görünür), hizalama/"
                       "pencere bütünlüğü doğrulanamaz. control_uri ile ince katmanı etkinleştirin.")

    def _poll_dma_overflow(self) -> None:
        """Rate-limited poll of the RX DMA overflow bit (reader thread). A set bit flags the
        next read; a failing poll disables detection (it must never stall the stream)."""
        thin = self._dma_thin
        if thin is None:
            return
        now = mono()
        if now - self._dma_last_poll < self.overflow_poll_s:
            return
        self._dma_last_poll = now
        try:
            hit = thin.read_clear_rx_overflow()
        except EDSError as e:
            self._dma_thin = None
            self.overflow_source = "none"
            self._warn(f"RX DMA durum yazmacı okunamadı ({e}); overflow algılama KAPATILDI — örnek kaybı artık "
                       "raporlanmaz.")
            return
        if hit:
            self._dma_pending = True
            self.dma_overflow_events += 1
            log.warning("%s: RX DMA overflow (donanım örnek düşürdü; olay %d)", self.name, self.dma_overflow_events)

    def _close_stream_quietly(self, stream: Any, reason: str) -> None:
        """Best-effort ``closeStream`` while another error is already being raised."""
        try:
            self._dev.closeStream(stream)
        except Exception as e:
            log.warning("%s: closeStream (%s) da başarısız: %s", self.name, reason, e)

    def _read_raw(self, n: int, timeout_s: float) -> RawRead:
        # local snapshot: a concurrent _stop() replaces these while this read may be in flight
        stream, d, bufs, cap, gen = self._stream, self._dev, self._bufs, self._cap, self._stream_gen
        if stream is None or d is None or not bufs:
            raise StreamError(f"{self.name}: akış başlatılmadı")
        sdr = self._sdr
        k_req = max(1, min(int(n), cap))
        try:
            sr = d.readStream(stream, bufs, k_req, timeoutUs=int(max(float(timeout_s), 0.0) * 1e6))
        except Exception as e:
            if gen != self._stream_gen:
                raise StreamError(f"{self.name}: akış okuma sırasında durduruldu") from e
            raise StreamError(f"{self.name}: readStream istisnası: {e}") from e
        if gen != self._stream_gen:
            raise StreamError(f"{self.name}: akış okuma sırasında durduruldu; okunan veri atıldı")
        t_ns = now_ns()
        ret = int(sr.ret)
        nch = len(bufs)
        if ret > 0:
            k = min(ret, k_req)
            out = np.empty((nch, k), dtype=np.complex64)
            if self.stream_format == "CS16":
                inv = np.float32(1.0 / self._scale)
                for i, b in enumerate(bufs):
                    np.multiply(b[: 2 * k].astype(np.float32).view(np.complex64), inv, out=out[i])
            else:
                for i, b in enumerate(bufs):
                    out[i] = b[:k]
            flags = int(sr.flags)
            hw = int(sr.timeNs) if flags & int(sdr.SOAPY_SDR_HAS_TIME) else None
            gap = self._timestamp_gap(hw, k)
            self._zero_since = None
            dma = self._dma_pending              # detected by an earlier poll: gap before these samples
            self._dma_pending = False
            self._poll_dma_overflow()
            return RawRead(samples=out, overflow_before=gap or dma, t_ns=t_ns, hw_time_ns=hw,
                           uncertain_samples=self.settle_samples if dma else 0)
        empty = np.empty((nch, 0), dtype=np.complex64)
        if ret == int(sdr.SOAPY_SDR_TIMEOUT):
            return RawRead(samples=empty, timeout=True, t_ns=t_ns)
        if ret == int(sdr.SOAPY_SDR_OVERFLOW):
            self._ts_expect = None
            self._zero_since = None
            return RawRead(samples=empty, overflow_before=True, t_ns=t_ns)
        if ret == 0:
            # no samples, no error: keep polling, but never longer than the read timeout
            now = mono()
            if self._zero_since is None:
                self._zero_since = now
            if now - self._zero_since >= float(timeout_s):
                self._zero_since = None
                return RawRead(samples=empty, timeout=True, t_ns=t_ns)
            return RawRead(samples=empty, t_ns=t_ns)
        raise StreamError(f"{self.name}: readStream hata kodu {ret} ({sdr.errToStr(ret)})")

    def _timestamp_gap(self, hw_ns: int | None, k: int) -> bool:
        """True if the hardware timestamp jumped forward (= samples lost before this read)."""
        if hw_ns is None or not self.detect_ts_gaps or self.fs <= 0:
            self._ts_expect = None
            return False
        gap = False
        if self._ts_expect is not None:
            tol_ns = max(2e9 / self.fs, 1000.0)
            if hw_ns - self._ts_expect > tol_ns:
                gap = True
                self.ts_gap_events += 1
        self._ts_expect = hw_ns + int(round(k * 1e9 / self.fs))
        return gap

    def _stop(self) -> None:
        stream, d = self._stream, self._dev
        self._stream_gen += 1                     # an in-flight read is discarded when it returns
        self._stream, self._bufs, self._cap = None, [], 0
        self._dma_thin, self._dma_pending = None, False
        if stream is None or d is None:
            return
        errs: list[str] = []
        try:
            d.deactivateStream(stream)
        except Exception as e:
            errs.append(f"deactivateStream: {e}")
        try:
            d.closeStream(stream)
        except Exception as e:
            errs.append(f"closeStream: {e}")
        if errs:
            raise StreamError(f"{self.name}: akış düzgün kapatılamadı: " + "; ".join(errs))

    def _close(self) -> None:
        errs: list[str] = []
        thin, self._thin_obj = self._thin_obj, None
        self._dma_thin = None
        if thin is not None:
            boot = self.xo_boot_hz
            if self._xo_written and boot is not None and self.xo_correction_hz != boot:
                try:
                    self._set_xo_state(thin.set_xo_correction(boot))
                    log.info("%s: xo_correction açılış değerine (%d Hz) geri yüklendi", self.name, boot)
                except EDSError as e:
                    log.warning("%s: xo_correction açılış değerine (%d Hz) geri yüklenemedi: %s", self.name, boot, e)
            try:
                thin.close()
            except EDSError as e:
                errs.append(f"ince katman: {e}")
        dev, self._dev = self._dev, None
        if dev is not None:
            try:
                how = release_device(self._sdr, dev)
                log.debug("%s: cihaz bırakıldı (%s)", self.name, how)
            except Exception as e:
                errs.append(f"Device.unmake: {e}")
            del dev
        self._release_pluto_slot()
        if errs:
            raise DeviceError(f"{self.name}: kapatma hatası: " + "; ".join(errs))

    # ------------------------------------------------------------------ optional features
    def set_tracking(self, dc: bool | None = None, rf_dc: bool | None = None,
                     quadrature: bool | None = None) -> dict[str, str]:
        """BB-DC via Soapy DC-offset mode, quadrature via Soapy IQ-balance mode (SoapySDR ≥ 0.8),
        anything else (RF-DC always) via the Pluto thin layer; else "unsupported" + warning."""
        with self._lock:
            d = self._require()
            rx = self._RX
            if any(v is not None for v in (dc, rf_dc, quadrature)):
                self.mark_settling()           # before the change: no block escapes the window
            res: dict[str, str] = {}
            thin_req: list[tuple[str, str, bool]] = []   # (feature, thin kwarg, value)
            if dc is not None:
                if hasattr(d, "setDCOffsetMode") and self._all_channels(
                        "hasDCOffsetMode", lambda ch: d.hasDCOffsetMode(rx, ch)):
                    for ch in self.cfg.channels:
                        self._call("setDCOffsetMode", d.setDCOffsetMode, rx, ch, bool(dc))
                    res["dc"] = "soapy"
                else:
                    thin_req.append(("dc", "bb_dc", bool(dc)))
            if rf_dc is not None:
                thin_req.append(("rf_dc", "rf_dc", bool(rf_dc)))
            if quadrature is not None:
                if (hasattr(d, "hasIQBalanceMode") and hasattr(d, "setIQBalanceMode")
                        and self._all_channels("hasIQBalanceMode", lambda ch: d.hasIQBalanceMode(rx, ch))):
                    for ch in self.cfg.channels:
                        self._call("setIQBalanceMode", d.setIQBalanceMode, rx, ch, bool(quadrature))
                    res["quadrature"] = "soapy"
                else:
                    thin_req.append(("quadrature", "quad", bool(quadrature)))
            if thin_req:
                thin = self.thin_layer()
                for feature, kw, val in thin_req:
                    path = "unsupported"
                    if thin is not None:
                        try:
                            thin.set_tracking(**{kw: val})
                            path = "iio"
                        except EDSError as e:
                            self._warn(f"{feature} tracking ince katmanla ayarlanamadı: {e}")
                    if path == "unsupported":
                        state = "açık" if val else "kapalı"
                        self._warn(f"{feature} tracking ({state}) bu cihazda ayarlanamadı (SoapySDR API'si ve Pluto "
                                   "ince katmanı yok) — sürücü varsayılanı geçerli.")
                    res[feature] = path
            # order the result like the arguments
            ordered = {k: res[k] for k in ("dc", "rf_dc", "quadrature") if k in res}
            if any(p != "unsupported" for p in ordered.values()):
                self.mark_settling()
            return ordered

    def set_ppm_correction(self, ppm: float) -> str:
        """Compensate the reference error: (1) Pluto ``xo_correction`` → "hardware",
        (2) ``setFrequencyCorrection`` → "soapy", (3) "none" (software correction upstream).

        ``xo_correction = round(ref_clock_hz·(1 + ppm·1e-6))``; ``ppm = 0`` restores nominal.
        ``ppm_hw`` is what is IN EFFECT afterwards, derived from the integer-Hz read-back of
        ``xo_correction`` (steps of 0.025 ppm) composed with any Soapy correction — not the
        request — so upstream residual / total ppm stay exact. With "none" the hardware state
        is unchanged (``ppm_hw`` may still be a pre-existing ``xo_correction``). After a
        hardware change fc and fs are re-programmed (the driver recomputes its PLLs with the
        new reference) and the following samples are flagged settling.
        """
        ppm = float(ppm)
        if not math.isfinite(ppm):
            raise ConfigError(f"ppm sonlu bir sayı olmalı (geldi: {ppm!r})")
        with self._lock:
            d = self._require()
            rx = self._RX
            self.mark_settling()
            via = "none"
            thin = self.thin_layer()
            if thin is not None:
                hz = int(round(self.profile.ref_clock_hz * (1.0 + ppm * 1e-6)))
                try:
                    self._set_xo_state(thin.set_xo_correction(hz))
                    self._xo_written = True
                    via = "hardware"
                except NotSupported as e:
                    self._warn(f"xo_correction kullanılamıyor (eski firmware?): {e}; Soapy/yazılım düzeltmesine düşülüyor.")
                except EDSError as e:
                    self._warn(f"xo_correction = {hz} yazılamadı: {e}; Soapy/yazılım düzeltmesine düşülüyor.")
            if via == "hardware":
                if self._soapy_ppm_applied != 0.0:
                    for ch in self.cfg.channels:
                        self._call("setFrequencyCorrection", d.setFrequencyCorrection, rx, ch, 0.0)
                    self._soapy_ppm_applied = 0.0
                if self.fs > 0:
                    self.fs = float(self._apply_sample_rate(self.fs))
                if self.fc > 0:
                    self.fc = float(self._apply_center_freq(self.fc))
            elif hasattr(d, "setFrequencyCorrection") and self._all_channels(
                    "hasFrequencyCorrection", lambda ch: d.hasFrequencyCorrection(rx, ch)):
                corr = _residual_ppm(ppm, self._xo_ppm)    # an xo_correction in effect stays: correct the rest
                for ch in self.cfg.channels:
                    self._call("setFrequencyCorrection", d.setFrequencyCorrection, rx, ch, corr)
                self._soapy_ppm_applied = corr
                via = "soapy"
            self.ppm_hw = _compose_ppm(self._xo_ppm, self._soapy_ppm_applied)
            self.ppm_hw_via = via if via != "none" else ("hardware" if self._xo_ppm != 0.0 else "none")
            if via != "none":
                self.mark_settling()
            return via

    def read_sensors(self) -> dict[str, float | str | None]:
        """Soapy sensors (``listSensors``/``readSensor``) + thin-layer ``temperature_c`` /
        ``rssi_db``. Never raises; an unreadable sensor is reported as ``None``."""
        out: dict[str, float | str | None] = {}
        d = self._dev
        if d is None:
            return out
        try:
            keys = [str(k) for k in d.listSensors()]
        except Exception as e:
            log.debug("%s: listSensors başarısız: %s", self.name, e)
            keys = []
        for k in keys:
            try:
                out[k] = _num_or_str(d.readSensor(k))
            except Exception as e:
                log.debug("%s: readSensor(%s) başarısız: %s", self.name, k, e)
                out[k] = None
        try:
            thin = self.thin_layer()
        except Exception as e:
            log.debug("%s: ince katman açılamadı: %s", self.name, e)
            thin = None
        if thin is not None:
            for key, fn in (("temperature_c", thin.temperature_c), ("rssi_db", thin.rssi_db)):
                try:
                    out[key] = float(fn())
                except Exception as e:
                    log.debug("%s: %s okunamadı: %s", self.name, key, e)
                    out[key] = None
        if out.get("temperature_c") is None:
            # Soapy fallback: prefer the RF chip sensor over the FPGA/XADC one
            temps = sorted((k for k in keys if "temp" in k.lower() and isinstance(out.get(k), float)),
                           key=lambda k: ("xadc" in k.lower(), k))
            if temps:
                out["temperature_c"] = out[temps[0]]
        return out

    def describe(self) -> dict[str, Any]:
        d = super().describe()
        d.update({
            "stream_format": self.stream_format, "native_format": list(self.native_format),
            "native_format_source": self.native_format_source,
            "stream_mtu": self.stream_mtu, "settle_samples": self.settle_samples,
            "control_uri": self.thin_uri() if self.is_pluto else "",
            "thin_layer": self._thin_obj is not None, "thin_serial_verified": self.thin_serial_verified,
            "agc_mode_actual": self.agc_mode_actual,
            "overflow_source": self.overflow_source, "dma_overflow_events": self.dma_overflow_events,
            "ts_gap_events": self.ts_gap_events, "xo_correction_hz": self.xo_correction_hz,
            "xo_correction_boot_hz": self.xo_boot_hz, "warnings": list(self.warnings),
        })
        return d
