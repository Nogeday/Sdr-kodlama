"""Thin libiio layer for ADALM-Pluto (AD936x) controls that SoapySDR does not expose.

Streaming and the device abstraction stay on SoapySDR (:mod:`eds.hal.soapy_backend`).
This module only touches a handful of ``ad9361-phy`` IIO attributes:

* ``xo_correction`` (device attr) — hardware PPM correction of the 40 MHz reference;
  the AD9361 driver derives LO *and* sample clock from it.
* input ``voltage0``: ``bb_dc_offset_tracking_en``, ``rf_dc_offset_tracking_en``,
  ``quadrature_tracking_en`` ("1"/"0"), ``gain_control_mode`` (+ ``_available``),
  ``hardwaregain_available`` ("[min step max]"), ``rssi`` ("105.25 dB").
* input ``temp0``: ``input`` (milli-°C) or ``raw``/``scale``/``offset``.
* context attrs: ``fw_version``, ``hw_serial`` (Pluto firmware) / ``usb,serial``.

It uses the pylibiio API exactly as shipped with libiio 0.23–0.25 (``import iio``):
``iio.Context(uri)``, ``ctx.find_device(name)`` → ``Device | None``,
``dev.attrs[name].value`` (get/set **str**), ``dev.find_channel(id, is_output)`` →
``Channel | None``, ``chan.attrs[name].value``, ``ctx.attrs`` (name → str; objects with
``.value`` are tolerated as well). Attribute I/O errors surface as ``OSError``.

USB note: a second libiio *USB* context to a Pluto that SoapyPlutoSDR is already streaming
from usually fails (interface busy). Use the network URI of the Pluto's USB-Ethernet
gadget (``ip:192.168.2.1``) for this layer — iiod serves several clients at once.

The external reference input (Rev C/D ``CLK_IN``) is selected by the Pluto *firmware
environment*, not by an IIO attribute. This module NEVER modifies the firmware environment;
:meth:`PlutoIIO.refclk_info` only reports what is known.
"""

from __future__ import annotations

import logging
import re
import threading
from types import ModuleType
from typing import Any

from ..core.errors import BackendUnavailable, ConfigError, DeviceError, DeviceNotFound, NotSupported

log = logging.getLogger(__name__)

PHY_NAME = "ad9361-phy"
RX_CHANNEL = "voltage0"
TEMP_CHANNEL = "temp0"
GAIN_CONTROL_MODES = ("manual", "slow_attack", "fast_attack", "hybrid")
TRACKING_ATTRS: dict[str, str] = {
    "bb_dc": "bb_dc_offset_tracking_en",
    "rf_dc": "rf_dc_offset_tracking_en",
    "quad": "quadrature_tracking_en",
}

IIO_INSTALL_HINT = (
    "pylibiio ('iio' Python modülü) bulunamadı. Kurulum: sudo apt install python3-libiio "
    "(ya da libiio'yu -DPYTHON_BINDINGS=ON ile derleyin; bkz. scripts/install_ubuntu.sh)."
)

EXTERNAL_REF_NOTE = (
    "Harici referans (Pluto Rev C/D CLK_IN) bir IIO özniteliği değildir; Pluto firmware ortam "
    "değişkeni ile seçilir (ör. Pluto kabuğunda `fw_setenv refclk_source external` + yeniden "
    "başlatma; değişken adı/değerleri firmware sürümüne bağlıdır — kendi sürümünüzde doğrulayın). "
    "EDS firmware ortamını ASLA değiştirmez. Harici referansta da frekansı `xo_correction` "
    "gerçek referans frekansına eşit olmalıdır."
)

_RANGE_RE = re.compile(r"^\s*\[\s*([-+0-9.eE]+)\s+([-+0-9.eE]+)\s+([-+0-9.eE]+)\s*\]\s*$")
_NUM_RE = re.compile(r"[-+]?\d+(?:\.\d*)?(?:[eE][-+]?\d+)?")


def import_iio() -> ModuleType:
    """Import pylibiio lazily; :class:`BackendUnavailable` with an install hint if missing."""
    try:
        import iio  # type: ignore[import-not-found]
    except ImportError as e:
        raise BackendUnavailable(f"{IIO_INSTALL_HINT} ({e})") from e
    return iio


def attr_text(v: Any) -> str:
    """Value of a ``ctx.attrs`` entry: ``str`` in pylibiio, or an object with ``.value``."""
    if isinstance(v, str):
        return v.strip()
    if hasattr(v, "value"):
        return str(v.value).strip()
    return str(v).strip()


def parse_range(text: str) -> tuple[float, float, float]:
    """Parse the IIO ``"[min step max]"`` format, e.g. ``"[-3 1 71]"`` → ``(-3.0, 1.0, 71.0)``."""
    m = _RANGE_RE.match(text)
    if not m:
        raise DeviceError(f"IIO aralık biçimi çözümlenemedi: {text!r} (beklenen '[min adım max]')")
    lo, step, hi = (float(g) for g in m.groups())
    return lo, step, hi


def parse_number(text: str) -> float:
    """First number in an attribute value such as ``"105.25 dB"`` or ``"71.000000 dB"``."""
    m = _NUM_RE.search(text)
    if not m:
        raise DeviceError(f"IIO değerinde sayı yok: {text!r}")
    return float(m.group(0))


class PlutoIIO:
    """Minimal ``ad9361-phy`` control over one libiio context (thread-safe)."""

    def __init__(self, uri: str):
        uri = str(uri).strip()
        if not uri or uri.lower() == "none":
            raise ConfigError("PlutoIIO: libiio URI boş (ör. 'ip:192.168.2.1' ya da 'usb:1.5.5')")
        iio = import_iio()
        self.uri = uri
        self._lock = threading.RLock()
        try:
            ctx = iio.Context(uri)
        except (OSError, RuntimeError, ValueError) as e:
            raise DeviceNotFound(f"libiio bağlamı açılamadı ({uri}): {e}") from e
        phy = ctx.find_device(PHY_NAME)
        if phy is None:
            raise DeviceNotFound(f"{uri}: '{PHY_NAME}' IIO cihazı yok (Pluto/AD936x değil mi?)")
        self._ctx: Any = ctx
        self._phy: Any = phy
        self.xo_boot_hz: int | None = None
        try:
            self.xo_boot_hz = self.get_xo_correction()
        except NotSupported as e:
            log.info("%s: %s", uri, e)

    # ------------------------------------------------------------------ helpers
    @property
    def closed(self) -> bool:
        return self._phy is None

    def _require_phy(self) -> Any:
        if self._phy is None:
            raise DeviceError(f"PlutoIIO ({self.uri}) kapalı")
        return self._phy

    def _phy_attr(self, name: str) -> Any:
        attrs = self._require_phy().attrs
        if name not in attrs:
            raise NotSupported(f"{PHY_NAME}: '{name}' özniteliği yok (eski firmware/sürücü?)")
        return attrs[name]

    def _channel(self, chan_id: str, output: bool = False) -> Any:
        ch = self._require_phy().find_channel(chan_id, output)
        if ch is None:
            kind = "çıkış" if output else "giriş"
            raise NotSupported(f"{PHY_NAME}: {kind} kanalı '{chan_id}' yok")
        return ch

    def _chan_attr(self, chan_id: str, name: str, output: bool = False) -> Any:
        attrs = self._channel(chan_id, output).attrs
        if name not in attrs:
            raise NotSupported(f"{PHY_NAME}/{chan_id}: '{name}' özniteliği yok (eski firmware/sürücü?)")
        return attrs[name]

    def _read(self, attr: Any, what: str) -> str:
        try:
            return str(attr.value).strip()
        except OSError as e:
            raise DeviceError(f"{self.uri}: {what} okunamadı: {e}") from e

    def _write(self, attr: Any, value: str, what: str) -> None:
        try:
            attr.value = str(value)
        except OSError as e:
            raise DeviceError(f"{self.uri}: {what} = {value!r} yazılamadı: {e}") from e

    def _ctx_attr(self, *names: str) -> str:
        if self._ctx is None:
            raise DeviceError(f"PlutoIIO ({self.uri}) kapalı")
        attrs = self._ctx.attrs
        for n in names:
            if n in attrs:
                txt = attr_text(attrs[n])
                if txt:
                    return txt
        return ""

    # ------------------------------------------------------------------ reference clock
    def get_xo_correction(self) -> int:
        """Current ``xo_correction`` in Hz (reference frequency the driver assumes)."""
        with self._lock:
            txt = self._read(self._phy_attr("xo_correction"), "xo_correction")
        return int(round(parse_number(txt)))

    def set_xo_correction(self, hz: int) -> int:
        """Write ``xo_correction`` (integer Hz). Returns the value read back.

        The new reference only affects LO / sample clock once they are re-programmed —
        callers (``SoapyRxDevice.set_ppm_correction``) re-apply fc and fs afterwards.
        """
        value = int(round(float(hz)))
        if value <= 0:
            raise ConfigError(f"xo_correction pozitif olmalı (geldi: {hz!r})")
        with self._lock:
            self._write(self._phy_attr("xo_correction"), str(value), "xo_correction")
            return self.get_xo_correction()

    def refclk_info(self) -> dict[str, Any]:
        """What is known about the reference clock. Read-only (never touches firmware env)."""
        info: dict[str, Any] = {"uri": self.uri, "external_ref_note": EXTERNAL_REF_NOTE}
        try:
            info["xo_correction_hz"] = self.get_xo_correction()
        except NotSupported:
            info["xo_correction_hz"] = None
        info["xo_correction_boot_hz"] = self.xo_boot_hz
        with self._lock:
            attrs = self._require_phy().attrs
            info["xo_correction_available"] = (
                self._read(attrs["xo_correction_available"], "xo_correction_available")
                if "xo_correction_available" in attrs else None
            )
            env_xo = self._ctx_attr("ad9361-phy,xo_correction")
        info["xo_correction_context_attr"] = env_xo or None
        info["fw_version"] = self.firmware_version()
        return info

    # ------------------------------------------------------------------ tracking
    def set_tracking(self, bb_dc: bool | None = None, rf_dc: bool | None = None,
                     quad: bool | None = None) -> dict[str, bool]:
        """Enable/disable AD936x BB-DC / RF-DC / quadrature tracking (``None`` = unchanged)."""
        req = {"bb_dc": bb_dc, "rf_dc": rf_dc, "quad": quad}
        with self._lock:
            for key, val in req.items():
                if val is None:
                    continue
                attr = self._chan_attr(RX_CHANNEL, TRACKING_ATTRS[key])
                self._write(attr, "1" if val else "0", TRACKING_ATTRS[key])
            return self.get_tracking()

    def get_tracking(self) -> dict[str, bool]:
        """Current tracking state for every tracking attribute the driver exposes."""
        out: dict[str, bool] = {}
        with self._lock:
            attrs = self._channel(RX_CHANNEL).attrs
            for key, name in TRACKING_ATTRS.items():
                if name in attrs:
                    out[key] = int(parse_number(self._read(attrs[name], name))) != 0
        return out

    # ------------------------------------------------------------------ gain control
    def gain_control_modes_available(self) -> tuple[str, ...]:
        with self._lock:
            attr = self._chan_attr(RX_CHANNEL, "gain_control_mode_available")
            return tuple(self._read(attr, "gain_control_mode_available").split())

    def get_gain_control_mode(self) -> str:
        with self._lock:
            return self._read(self._chan_attr(RX_CHANNEL, "gain_control_mode"), "gain_control_mode")

    def set_gain_control_mode(self, mode: str) -> str:
        """Set AD936x RX AGC mode (manual | slow_attack | fast_attack | hybrid)."""
        mode = str(mode).strip()
        with self._lock:
            avail = self.gain_control_modes_available()
            if mode not in avail:
                raise ConfigError(f"gain_control_mode {mode!r} geçersiz; cihazın desteklediği: {list(avail)}")
            self._write(self._chan_attr(RX_CHANNEL, "gain_control_mode"), mode, "gain_control_mode")
            return self.get_gain_control_mode()

    @property
    def gain_control_mode(self) -> str:
        return self.get_gain_control_mode()

    @gain_control_mode.setter
    def gain_control_mode(self, mode: str) -> None:
        self.set_gain_control_mode(mode)

    def hardwaregain_available(self) -> tuple[float, float, float]:
        """RX ``hardwaregain_available`` as ``(min, step, max)`` dB at the current LO band."""
        with self._lock:
            txt = self._read(self._chan_attr(RX_CHANNEL, "hardwaregain_available"), "hardwaregain_available")
        return parse_range(txt)

    # ------------------------------------------------------------------ sensors / identity
    def temperature_c(self) -> float:
        """AD936x die temperature in °C (IIO ``temp0``: ``input`` m°C, or (raw+offset)·scale m°C)."""
        with self._lock:
            ch = self._channel(TEMP_CHANNEL)
            attrs = ch.attrs
            if "input" in attrs:
                return parse_number(self._read(attrs["input"], "temp0 input")) / 1000.0
            if "raw" in attrs:
                raw = parse_number(self._read(attrs["raw"], "temp0 raw"))
                scale = parse_number(self._read(attrs["scale"], "temp0 scale")) if "scale" in attrs else 1.0
                offset = parse_number(self._read(attrs["offset"], "temp0 offset")) if "offset" in attrs else 0.0
                return (raw + offset) * scale / 1000.0
        raise NotSupported(f"{PHY_NAME}/{TEMP_CHANNEL}: 'input' ya da 'raw' özniteliği yok")

    def rssi_db(self) -> float:
        """RX ``rssi`` as reported by the driver (e.g. ``"105.25 dB"`` → 105.25; larger = weaker)."""
        with self._lock:
            return parse_number(self._read(self._chan_attr(RX_CHANNEL, "rssi"), "rssi"))

    def firmware_version(self) -> str:
        """Pluto firmware version (context attr ``fw_version``), "" if not exposed."""
        with self._lock:
            return self._ctx_attr("fw_version")

    def serial(self) -> str:
        """Unique serial (``hw_serial`` on Pluto firmware, else ``usb,serial``), "" if unknown."""
        with self._lock:
            return self._ctx_attr("hw_serial", "usb,serial")

    # ------------------------------------------------------------------ lifecycle
    def close(self) -> None:
        """Drop the context (pylibiio destroys it when the last reference goes away)."""
        with self._lock:
            self._phy = None
            self._ctx = None

    def __enter__(self) -> PlutoIIO:
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    def __repr__(self) -> str:
        state = "closed" if self.closed else "open"
        return f"PlutoIIO(uri={self.uri!r}, {state})"
