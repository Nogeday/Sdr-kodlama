"""Faithful fake of pylibiio (the ``iio`` module of libiio 0.23–0.25).

API mirrored (as used by :mod:`eds.hal.pluto_iio`):

* ``iio.Context(uri)`` — raises ``OSError`` when the URI cannot be opened (unknown, busy).
* ``ctx.attrs`` — ``dict`` name → ``str`` (optionally objects with ``.value`` to exercise
  tolerant callers), ``ctx.devices``, ``ctx.find_device(name_or_id_or_label)`` → ``Device | None``.
* ``Device``: ``id``, ``name``, ``attrs`` (dict name → ``DeviceAttr``), ``debug_attrs``,
  ``channels``, ``find_channel(name_or_id, is_output=False)`` → ``Channel | None``,
  ``reg_read(addr) -> int`` / ``reg_write(addr, value)`` (``iio_device_reg_read/_write`` via
  the ``direct_reg_access`` debug attribute; ``OSError(ENOENT)`` on a device without it).
* ``iio.scan_contexts()`` → ``{uri: description}`` like ``iio_info -s`` (USB:
  ``"0456:b673 (Analog Devices Inc. PlutoSDR (ADALM-PLUTO)), serial=..."``).
* ``Channel``: ``id``, ``name``, ``output``, ``attrs`` (dict name → ``ChannelAttr``).
* ``Attr.value`` — property, get → ``str``; set requires ``str`` (pylibiio calls
  ``value.encode("ascii")`` → ``AttributeError`` for an int), errors → ``OSError``.

:func:`make_pluto_board` builds an ``ad9361-phy`` with the attributes of a Pluto (AD9363,
firmware v0.3x) and records every attribute write in ``board.writes`` as ``(path, value)``.
Its ``cf-ad9361-lpc`` models the AXI ADC **DMA status register** ``0x80000088``: bit 2
(``0x4``) = RX overflow, write-1-to-clear (libiio ``examples/iio_adi_xflow_check.c``).
``board.dma_overflow()`` sets the bit, i.e. "the hardware dropped samples"; hook it into the
SoapySDR fake (``fake_soapy.install(..., overflow_hook=board.dma_overflow)``) to model the real
SoapyPlutoSDR, whose ``readStream`` never reports the overflow while the register does.
"""

from __future__ import annotations

import errno
import sys
import types
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any


class _Attr:
    def __init__(self, board: FakeBoard, path: str, name: str, value: str, *, writable: bool = True,
                 validator: Callable[[str], str] | None = None):
        self._board, self._path, self._name = board, path, name
        self._value = str(value)
        self._writable = writable
        self._validator = validator

    @property
    def name(self) -> str:
        return self._name

    @property
    def filename(self) -> str:
        return self._name

    @property
    def value(self) -> str:
        if self._path in self._board.read_errors:
            raise OSError(errno.EIO, "Input/output error")
        return self._value

    @value.setter
    def value(self, v: Any) -> None:
        if not isinstance(v, str):
            raise AttributeError(f"'{type(v).__name__}' object has no attribute 'encode'")
        if not self._writable:
            raise OSError(errno.EACCES, "Permission denied")
        new = self._validator(v) if self._validator else v
        self._board.writes.append((self._path, v))
        self._value = new

    def __str__(self) -> str:
        return self._name


class DeviceAttr(_Attr):
    pass


class ChannelAttr(_Attr):
    pass


class _ValueAttr:
    """Context attribute exposed as an object with ``.value`` (tolerance test)."""

    def __init__(self, value: str):
        self.value = value


class Channel:
    def __init__(self, cid: str, output: bool, name: str | None = None):
        self._id, self._output, self._name = cid, output, name
        self._attrs: dict[str, ChannelAttr] = {}
        self.enabled = False

    @property
    def id(self) -> str:
        return self._id

    @property
    def name(self) -> str | None:
        return self._name

    @property
    def output(self) -> bool:
        return self._output

    @property
    def attrs(self) -> dict[str, ChannelAttr]:
        return self._attrs


DMA_STATUS_REG = 0x80000088
DMA_STATUS_OVERFLOW = 0x4
_W1C_REGS = frozenset({DMA_STATUS_REG})


class Device:
    def __init__(self, did: str, name: str, label: str | None = None, board: FakeBoard | None = None,
                 regs: dict[int, int] | None = None):
        self._id, self._name, self._label = did, name, label
        self._attrs: dict[str, DeviceAttr] = {}
        self._debug_attrs: dict[str, DeviceAttr] = {}
        self._channels: list[Channel] = []
        self._board = board
        self._regs = regs                       # None: no direct_reg_access debug attribute

    def reg_read(self, reg: int) -> int:
        if self._regs is None:
            raise OSError(errno.ENOENT, "No such file or directory")
        b = self._board
        if b is not None:
            if b.reg_error is not None:
                raise OSError(*b.reg_error)
            b.reg_log.append(("r", self._name, int(reg), None))
        return int(self._regs.get(int(reg), 0)) & 0xFFFFFFFF

    def reg_write(self, reg: int, value: int) -> None:
        if self._regs is None:
            raise OSError(errno.ENOENT, "No such file or directory")
        b = self._board
        if b is not None:
            if b.reg_error is not None:
                raise OSError(*b.reg_error)
            b.reg_log.append(("w", self._name, int(reg), int(value)))
        reg, value = int(reg), int(value) & 0xFFFFFFFF
        if reg in _W1C_REGS:
            self._regs[reg] = self._regs.get(reg, 0) & ~value
        else:
            self._regs[reg] = value

    @property
    def id(self) -> str:
        return self._id

    @property
    def name(self) -> str:
        return self._name

    @property
    def label(self) -> str | None:
        return self._label

    @property
    def attrs(self) -> dict[str, DeviceAttr]:
        return self._attrs

    @property
    def debug_attrs(self) -> dict[str, DeviceAttr]:
        return self._debug_attrs

    @property
    def channels(self) -> list[Channel]:
        return list(self._channels)

    def find_channel(self, name_or_id: str, is_output: bool = False) -> Channel | None:
        for ch in self._channels:
            if ch.output == bool(is_output) and name_or_id in (ch.id, ch.name):
                return ch
        return None


@dataclass
class FakeBoard:
    uri: str
    ctx_attrs: dict[str, str] = field(default_factory=dict)
    devices: list[Device] = field(default_factory=list)
    writes: list[tuple[str, str]] = field(default_factory=list)
    read_errors: set[str] = field(default_factory=set)
    open_error: tuple[int, str] | None = None
    ctx_attr_objects: bool = False
    opened: int = 0
    rx_regs: dict[int, int] = field(default_factory=dict)        # cf-ad9361-lpc core registers
    reg_log: list[tuple[str, str, int, int | None]] = field(default_factory=list)
    reg_error: tuple[int, str] | None = None                     # every reg_read/reg_write fails
    dma_overflows: int = 0

    def device(self, name: str) -> Device:
        return next(d for d in self.devices if d.name == name)

    def dma_overflow(self) -> None:
        """The RX DMA dropped samples: set the sticky overflow bit (as the AXI ADC core does)."""
        self.dma_overflows += 1
        self.rx_regs[DMA_STATUS_REG] = self.rx_regs.get(DMA_STATUS_REG, 0) | DMA_STATUS_OVERFLOW

    @property
    def rx_overflow_bit(self) -> bool:
        return bool(self.rx_regs.get(DMA_STATUS_REG, 0) & DMA_STATUS_OVERFLOW)

    def serial(self) -> str:
        return self.ctx_attrs.get("hw_serial") or self.ctx_attrs.get("usb,serial", "")

    def scan_description(self) -> str:
        """Context description as ``iio_info -s`` / ``iio.scan_contexts()`` show it."""
        ser = self.serial()
        if self.uri.startswith("usb:"):
            return f"0456:b673 (Analog Devices Inc. PlutoSDR (ADALM-PLUTO)), serial={ser}"
        host = self.uri.split(":", 1)[-1]
        return f"{host} ({self.ctx_attrs.get('hw_model', 'PlutoSDR')}), serial={ser}"

    def attr(self, path: str) -> _Attr:
        """``"ad9361-phy/xo_correction"`` or ``"ad9361-phy/in/voltage0/rssi"``."""
        parts = path.split("/")
        dev = self.device(parts[0])
        if len(parts) == 2:
            return dev.attrs[parts[1]]
        ch = dev.find_channel(parts[2], parts[1] == "out")
        assert ch is not None, path
        return ch.attrs[parts[3]]

    def value(self, path: str) -> str:
        return self.attr(path)._value


def _int_range_validator(lo: int, hi: int) -> Callable[[str], str]:
    def check(v: str) -> str:
        try:
            x = int(v.strip())
        except ValueError as e:
            raise OSError(errno.EINVAL, "Invalid argument") from e
        if not lo <= x <= hi:
            raise OSError(errno.EINVAL, "Invalid argument")
        return str(x)
    return check


def _choice_validator(choices: tuple[str, ...]) -> Callable[[str], str]:
    def check(v: str) -> str:
        if v.strip() not in choices:
            raise OSError(errno.EINVAL, "Invalid argument")
        return v.strip()
    return check


def make_pluto_board(uri: str = "ip:192.168.2.1", *, xo_correction: int = 40_000_000, has_xo: bool = True,
                     fw_version: str = "v0.38", hw_serial: str = "104473dc5993000917000e00c83b2e5e35",
                     temp_mode: str = "input", temp_milli: float = 36250.0, rssi: str = "105.25 dB",
                     gain_modes: str = "manual fast_attack slow_attack hybrid",
                     hardwaregain_available: str = "[-3 1 71]", ctx_attr_objects: bool = False,
                     open_error: tuple[int, str] | None = None, include_serial: bool = True,
                     dma_regs: bool = True) -> FakeBoard:
    """A Pluto (AD9363) as libiio exposes it. ``temp_mode``: "input" | "raw" | "none".
    ``dma_regs=False``: ``cf-ad9361-lpc`` without ``direct_reg_access`` (no overflow register)."""
    b = FakeBoard(uri=uri, open_error=open_error, ctx_attr_objects=ctx_attr_objects)
    b.ctx_attrs = {
        "hw_model": "Analog Devices PlutoSDR Rev.B (Z7010-AD9363A)", "hw_model_variant": "0",
        "fw_version": fw_version, "ad9361-phy,xo_correction": str(xo_correction),
        "ad9361-phy,model": "ad9363a", "local,kernel": "5.15.0-175774-g3ae3f6d0b1e7", "uri": uri,
    }
    if include_serial:
        b.ctx_attrs["hw_serial"] = hw_serial
    else:
        b.ctx_attrs["usb,serial"] = hw_serial
    phy = Device("iio:device0", "ad9361-phy")
    P = "ad9361-phy"
    if has_xo:
        phy._attrs["xo_correction"] = DeviceAttr(b, f"{P}/xo_correction", "xo_correction", str(xo_correction),
                                                 validator=_int_range_validator(39_992_000, 40_008_000))
        phy._attrs["xo_correction_available"] = DeviceAttr(b, f"{P}/xo_correction_available",
                                                           "xo_correction_available", "[39992000 1 40008000]",
                                                           writable=False)
    phy._attrs["ensm_mode"] = DeviceAttr(b, f"{P}/ensm_mode", "ensm_mode", "fdd",
                                         validator=_choice_validator(("sleep", "alert", "fdd", "pinctrl")))
    rx = Channel("voltage0", False)
    base = f"{P}/in/voltage0"
    bool_v = _choice_validator(("0", "1"))
    modes = tuple(gain_modes.split())
    for name, val, writable, val_fn in (
        ("hardwaregain", "71.000000 dB", True, None),
        ("hardwaregain_available", hardwaregain_available, False, None),
        ("gain_control_mode", "slow_attack", True, _choice_validator(modes)),
        ("gain_control_mode_available", gain_modes, False, None),
        ("rssi", rssi, False, None),
        ("bb_dc_offset_tracking_en", "1", True, bool_v),
        ("rf_dc_offset_tracking_en", "1", True, bool_v),
        ("quadrature_tracking_en", "1", True, bool_v),
        ("rf_bandwidth", "18000000", True, None),
        ("sampling_frequency", "30720000", True, None),
        ("rf_port_select", "A_BALANCED", True, None),
    ):
        rx._attrs[name] = ChannelAttr(b, f"{base}/{name}", name, val, writable=writable, validator=val_fn)
    tx = Channel("voltage0", True)
    tx._attrs["hardwaregain"] = ChannelAttr(b, f"{P}/out/voltage0/hardwaregain", "hardwaregain", "-10.000000 dB")
    lo = Channel("altvoltage0", True, name="RX_LO")
    lo._attrs["frequency"] = ChannelAttr(b, f"{P}/out/altvoltage0/frequency", "frequency", "2400000000")
    phy._channels = [rx, tx, lo]
    if temp_mode != "none":
        temp = Channel("temp0", False)
        tb = f"{P}/in/temp0"
        if temp_mode == "input":
            temp._attrs["input"] = ChannelAttr(b, f"{tb}/input", "input", f"{temp_milli:.0f}", writable=False)
        else:   # raw/scale/offset (m°C = (raw + offset) * scale)
            temp._attrs["raw"] = ChannelAttr(b, f"{tb}/raw", "raw", "1000", writable=False)
            temp._attrs["offset"] = ChannelAttr(b, f"{tb}/offset", "offset", "250", writable=False)
            temp._attrs["scale"] = ChannelAttr(b, f"{tb}/scale", "scale", f"{temp_milli / 1250.0:.6f}", writable=False)
        phy._channels.append(temp)
    rxdev = Device("iio:device3", "cf-ad9361-lpc", board=b, regs=b.rx_regs if dma_regs else None)
    rxdev._channels = [Channel("voltage0", False), Channel("voltage1", False)]
    if dma_regs:
        rxdev._debug_attrs["direct_reg_access"] = DeviceAttr(b, "cf-ad9361-lpc/direct_reg_access",
                                                              "direct_reg_access", "0x0")
    txdev = Device("iio:device2", "cf-ad9361-dds-core-lpc")
    xadc = Device("iio:device1", "xadc")
    b.devices = [phy, xadc, txdev, rxdev]
    return b


def make_module(boards: list[FakeBoard] | None = None, *,
                scan_extra: dict[str, str] | None = None) -> types.ModuleType:
    """Fresh fake ``iio`` module serving ``boards`` by URI (``module.boards``).

    ``scan_contexts()`` lists every board plus ``scan_extra`` (``{uri: description}``, e.g. a
    USB Pluto that is visible to the scan but cannot be opened as a context here).
    """
    registry = {b.uri: b for b in (boards or [])}
    extra = dict(scan_extra or {})
    mod = types.ModuleType("iio")
    mod.scan_calls = 0

    def scan_contexts() -> dict[str, str]:
        mod.scan_calls += 1
        found = {uri: b.scan_description() for uri, b in registry.items()}
        found.update(extra)
        return found

    class Context:
        def __init__(self, _context: Any = None):
            if not isinstance(_context, str):
                raise OSError(errno.ENOENT, "No such file or directory")
            board = registry.get(_context)
            if board is None:
                raise OSError(errno.ENOENT, "No such file or directory")
            if board.open_error is not None:
                raise OSError(*board.open_error)
            board.opened += 1
            self._board = board
            self._attrs: dict[str, Any] = {
                k: (_ValueAttr(v) if board.ctx_attr_objects else v) for k, v in board.ctx_attrs.items()
            }
            self.name = _context.split(":", 1)[0]
            self.description = "fake libiio context"
            self.xml = "<context/>"
            self.version = (0, 25, "fake")

        @property
        def attrs(self) -> dict[str, Any]:
            return self._attrs

        @property
        def devices(self) -> list[Device]:
            return list(self._board.devices)

        def find_device(self, name_or_id_or_label: str) -> Device | None:
            for d in self._board.devices:
                if name_or_id_or_label in (d.name, d.id, d.label):
                    return d
            return None

    mod.Context = Context
    mod.scan_contexts = scan_contexts
    mod.Device = Device
    mod.Channel = Channel
    mod.DeviceAttr = DeviceAttr
    mod.ChannelAttr = ChannelAttr
    mod.boards = registry
    mod.version = (0, 25, "fake")
    return mod


def install(monkeypatch: Any, *boards: FakeBoard, scan_extra: dict[str, str] | None = None) -> types.ModuleType:
    """Install a fake ``iio`` module serving ``boards`` (undone by ``monkeypatch``)."""
    mod = make_module(list(boards), scan_extra=scan_extra)
    monkeypatch.setitem(sys.modules, "iio", mod)
    return mod


def uninstall(monkeypatch: Any) -> None:
    """Make ``import iio`` raise ImportError."""
    monkeypatch.setitem(sys.modules, "iio", None)
