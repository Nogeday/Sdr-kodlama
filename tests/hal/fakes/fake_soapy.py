"""Faithful fake of the ``SoapySDR`` Python module (python3-soapysdr SWIG bindings, 0.8.x).

Mirrors the real binding where EDS depends on it:

* ``SoapySDR.Device(args)`` → ``Device.__new__`` → ``Device.make(args)`` (RuntimeError
  "no match" when nothing matches), ``Device.enumerate(args)`` → list of ``SoapySDRKwargs``
  (std::map proxy: ``keys()`` / ``[]`` / ``items()`` — *not* a dict),
  ``Device.unmake(dev)`` (RuntimeError "unknown device" on a second unmake) and the binding's
  ``__del__`` that calls ``unmake`` (``define_del``).
* ``readStream(stream, buffs, numElems, flags=0, timeoutUs=100000)`` /
  ``writeStream(stream, buffs, numElems, flags=0, timeNs=0, timeoutUs=100000)`` return a
  ``StreamResult`` with ``ret`` / ``flags`` / ``timeNs`` / ``chanMask``; buffers are numpy
  arrays whose dtype must match the stream format (CS16 → interleaved int16, 2·n values).
* ``getNativeStreamFormat`` returns ``[format, fullScale]``; ``getGainRange`` a ``Range``;
  ``getFrequencyRange`` / ``getSampleRateRange`` / ``getBandwidthRange`` lists of ``Range``.
* SoapyPlutoSDR behaviour: driver key "PlutoSDR", native CS16 full scale 2048, ``bufflen``
  stream arg = MTU, ``kernel_buffers × MTU`` stale (old-frequency) samples after a retune
  while streaming, CS16 tone of ``tone_counts`` counts. ``find``/``make`` IGNORE the
  ``serial`` arg (``FakeConfig.filters_serial = False``; set it for drivers such as UHD that
  filter natively), so ``make({"serial": ...})`` without ``uri`` opens the first device.
  ``getHardwareInfo`` reports the opened entry's serial (``hw_serial``, from its ``serial``
  key or the ``serial=...`` field of its libiio ``label``) and ``uri``.

Scripting: ``dev.rx_script`` / ``dev.tx_script`` (``collections.deque``) entries consumed by
the next ``readStream`` / ``writeStream``:

* ``"overflow"`` → ret -4 (``overflow_drop`` samples are skipped, i.e. really lost)
* ``"timeout"`` → ret -1 · ``"zero"`` → ret 0 · ``"underflow"`` (TX) → ret -7
* ``int`` → that return code · ``("short", k)`` → at most ``k`` samples
* ``("gap", k)`` → silently skip ``k`` samples (only visible through timestamps)

Every control call is recorded in ``dev.calls`` as ``(name, args, kwargs)``.
"""

from __future__ import annotations

import dataclasses
import sys
import threading
import time
import types
import weakref
from collections import deque
from dataclasses import dataclass, field
from typing import Any

import numpy as np

SOAPY_SDR_TX = 0
SOAPY_SDR_RX = 1

SOAPY_SDR_CF64 = "CF64"
SOAPY_SDR_CF32 = "CF32"
SOAPY_SDR_CS32 = "CS32"
SOAPY_SDR_CS16 = "CS16"
SOAPY_SDR_CS12 = "CS12"
SOAPY_SDR_CS8 = "CS8"
SOAPY_SDR_CU8 = "CU8"

SOAPY_SDR_TIMEOUT = -1
SOAPY_SDR_STREAM_ERROR = -2
SOAPY_SDR_CORRUPTION = -3
SOAPY_SDR_OVERFLOW = -4
SOAPY_SDR_NOT_SUPPORTED = -5
SOAPY_SDR_TIME_ERROR = -6
SOAPY_SDR_UNDERFLOW = -7

SOAPY_SDR_END_BURST = 1 << 1
SOAPY_SDR_HAS_TIME = 1 << 2
SOAPY_SDR_END_ABRUPT = 1 << 3
SOAPY_SDR_ONE_PACKET = 1 << 4
SOAPY_SDR_MORE_FRAGMENTS = 1 << 5
SOAPY_SDR_WAIT_TRIGGER = 1 << 6

SOAPY_SDR_FATAL, SOAPY_SDR_CRITICAL, SOAPY_SDR_ERROR, SOAPY_SDR_WARNING = 1, 2, 3, 4
SOAPY_SDR_NOTICE, SOAPY_SDR_INFO, SOAPY_SDR_DEBUG, SOAPY_SDR_TRACE, SOAPY_SDR_SSI = 5, 6, 7, 8, 9

_ERR_STR = {
    SOAPY_SDR_TIMEOUT: "TIMEOUT", SOAPY_SDR_STREAM_ERROR: "STREAM_ERROR",
    SOAPY_SDR_CORRUPTION: "CORRUPTION", SOAPY_SDR_OVERFLOW: "OVERFLOW",
    SOAPY_SDR_NOT_SUPPORTED: "NOT_SUPPORTED", SOAPY_SDR_TIME_ERROR: "TIME_ERROR",
    SOAPY_SDR_UNDERFLOW: "UNDERFLOW",
}


def errToStr(code: int) -> str:  # noqa: N802 - real binding name
    return _ERR_STR.get(int(code), "UNKNOWN")


class Range:
    """``SoapySDR.Range(minimum, maximum, step=0.0)`` with accessor *methods*."""

    def __init__(self, minimum: float = 0.0, maximum: float = 0.0, step: float = 0.0):
        self._min, self._max, self._step = float(minimum), float(maximum), float(step)

    def minimum(self) -> float:
        return self._min

    def maximum(self) -> float:
        return self._max

    def step(self) -> float:
        return self._step

    def __str__(self) -> str:
        return f"{self._min:g}, {self._max:g}" + (f", {self._step:g}" if self._step else "")


class SoapySDRKwargs:
    """SWIG ``std::map<std::string, std::string>`` proxy (deliberately *not* a dict)."""

    def __init__(self, d: Any = None):
        self._d: dict[str, str] = {}
        if d is not None:
            for k in (d.keys() if hasattr(d, "keys") else d):
                self._d[str(k)] = str(d[k])

    def keys(self) -> list[str]:
        return list(self._d.keys())

    def values(self) -> list[str]:
        return list(self._d.values())

    def items(self) -> list[tuple[str, str]]:
        return list(self._d.items())

    def has_key(self, k: str) -> bool:
        return k in self._d

    def asdict(self) -> dict[str, str]:
        return dict(self._d)

    def __getitem__(self, k: str) -> str:
        return self._d[k]

    def __setitem__(self, k: str, v: str) -> None:
        self._d[str(k)] = str(v)

    def __contains__(self, k: object) -> bool:
        return k in self._d

    def __iter__(self):
        return iter(list(self._d))

    def __len__(self) -> int:
        return len(self._d)

    def __str__(self) -> str:
        return "{" + ", ".join(f"{k}={v}" for k, v in self._d.items()) + "}"


class StreamResult:
    def __init__(self, ret: int = 0, flags: int = 0, timeNs: int = 0, chanMask: int = 0):  # noqa: N803
        self.ret, self.flags, self.timeNs, self.chanMask = int(ret), int(flags), int(timeNs), int(chanMask)

    def __str__(self) -> str:
        return f"ret={self.ret}, flags={self.flags}, timeNs={self.timeNs}"


PLUTO_ENTRY = {
    "device": "PlutoSDR", "driver": "plutosdr", "label": "PlutoSDR #0 usb:1.5.5",
    "uri": "usb:1.5.5", "serial": "104473dc5993000917000e00c83b2e5e35",
}


@dataclass
class FakeConfig:
    devices: list[dict[str, str]] = field(default_factory=lambda: [dict(PLUTO_ENTRY)])
    driver_key: str = "PlutoSDR"
    hardware_key: str = "ADALM-PLUTO"
    hardware_info: dict[str, str] = field(default_factory=lambda: {
        "hw_model": "Analog Devices PlutoSDR Rev.B (Z7010-AD9363A)", "hw_serial": PLUTO_ENTRY["serial"],
        "fw_version": "v0.38", "uri": "usb:1.5.5", "ad9361-phy,model": "ad9363a",
    })
    num_channels: int = 1
    native_format: str = SOAPY_SDR_CS16
    native_full_scale: float = 2048.0
    cs16_full_scale: float = 2048.0          # counts of full scale in the driver's CS16 output
    stream_formats: tuple[str, ...] = (SOAPY_SDR_CS8, SOAPY_SDR_CS12, SOAPY_SDR_CS16, SOAPY_SDR_CF32)
    default_mtu: int = 16384
    max_per_read: int | None = None
    kernel_buffers: int = 4
    tone_hz: float = 100e3
    tone_counts: float = 1024.0
    stale_counts: float = 300.0
    channel_phase_rad: float = 0.5
    overflow_drop: int = 1000
    has_time: bool = False
    time0_ns: int = 1_000_000_000
    freq_ranges: tuple[tuple[float, float], ...] = ((70e6, 6e9),)
    tx_freq_ranges: tuple[tuple[float, float], ...] = ((46.875e6, 6e9),)
    rate_ranges: tuple[tuple[float, float], ...] = ((65105.0, 61.44e6),)
    rates_list: tuple[float, ...] = (1e6, 2.4e6, 4e6, 10e6, 20e6)
    bw_ranges: tuple[tuple[float, float], ...] = ((200e3, 56e6),)
    bw_list: tuple[float, ...] = (200e3, 56e6)
    rx_gain_range: tuple[float, float] = (0.0, 73.0)
    tx_gain_range: tuple[float, float] = (0.0, 89.0)
    has_dc_offset_mode: bool = True
    has_iq_balance_mode: bool = False
    has_frequency_correction: bool = False
    missing_methods: tuple[str, ...] = ()
    define_del: bool = True
    sensors: dict[str, str] = field(default_factory=lambda: {"xadc_temp0": "41.5", "temp0": "35.25"})
    failing_sensors: tuple[str, ...] = ()
    activate_ret: int = 0
    make_error: str = ""
    tx_pacing_s: float = 0.0005
    tx_record_limit: int = 1_000_000
    filters_serial: bool = False            # SoapyPlutoSDR ignores 'serial'; UHD-like drivers filter


@dataclass
class FakeState:
    config: FakeConfig
    live: set[int] = field(default_factory=set)
    made: int = 0
    unmade: list[str] = field(default_factory=list)
    double_unmake: int = 0
    instances: list[weakref.ref] = field(default_factory=list)

    def last(self) -> Any:
        for ref in reversed(self.instances):
            obj = ref()
            if obj is not None:
                return obj
        return None


class _Stream:
    """Opaque ``SoapySDR::Stream*`` stand-in."""

    def __init__(self, direction: int, fmt: str, channels: list[int], args: dict[str, str], mtu: int):
        self.direction, self.format, self.channels, self.args, self.mtu = direction, fmt, channels, args, mtu
        self.active = False
        self.closed = False


def _parse_args(args: Any) -> dict[str, str]:
    if args is None:
        return {}
    if isinstance(args, str):
        out = {}
        for part in args.split(","):
            if "=" in part:
                k, v = part.split("=", 1)
                out[k.strip()] = v.strip()
        return out
    keys = args.keys() if hasattr(args, "keys") else []
    return {str(k): str(args[k]) for k in keys}


_MATCH_KEYS = ("driver", "serial", "uri", "label")


def entry_serial(entry: dict[str, str]) -> str:
    """Serial of an entry: ``serial`` key, else ``serial=...`` inside the libiio ``label``."""
    if entry.get("serial"):
        return entry["serial"]
    label = entry.get("label", "")
    i = label.find("serial=")
    return label[i + 7:].split()[0].rstrip(",)") if i >= 0 else ""


def _matches(entry: dict[str, str], filt: dict[str, str], filters_serial: bool = True) -> bool:
    for k in _MATCH_KEYS:
        if k == "serial" and not filters_serial:
            continue
        if k in filt:
            a, b = entry.get(k, ""), filt[k]
            if (a.lower() != b.lower()) if k == "driver" else (a != b):
                return False
    if "hostname" in filt and entry.get("uri", "") not in ("", f"ip:{filt['hostname']}"):
        return False
    return True


def make_module(config: FakeConfig | None = None, **overrides: Any) -> types.ModuleType:
    """A fresh fake ``SoapySDR`` module with its own state (``module.state``)."""
    cfg = dataclasses.replace(config or FakeConfig(), **overrides)
    state = FakeState(cfg)
    mod = types.ModuleType("SoapySDR")

    class Device:
        def __new__(cls, *args: Any, **kwargs: Any):
            return cls.make(*args, **kwargs)

        def __init__(self, *args: Any, **kwargs: Any):
            """Real proxy objects come fully built from ``make``; nothing to do here."""

        # ---------------------------------------------------------------- factory
        @staticmethod
        def enumerate(args: Any = None) -> list[SoapySDRKwargs]:
            filt = _parse_args(args)
            return [SoapySDRKwargs(e) for e in cfg.devices if _matches(e, filt, cfg.filters_serial)]

        @classmethod
        def make(cls, args: Any = None) -> Device:
            a = _parse_args(args)
            if cfg.make_error:
                raise RuntimeError(cfg.make_error)
            entry = next((e for e in cfg.devices if _matches(e, a, cfg.filters_serial)), None)
            if entry is None:
                raise RuntimeError("SoapySDR::Device::make() no match")
            obj = object.__new__(cls)
            obj._fake_init(a, entry)
            state.live.add(id(obj))
            state.made += 1
            state.instances.append(weakref.ref(obj))
            return obj

        @staticmethod
        def unmake(dev: Any) -> None:
            if id(dev) not in state.live:
                state.double_unmake += 1
                raise RuntimeError("SoapySDR::Device::unmake() unknown device")
            state.live.discard(id(dev))
            state.unmade.append(dev.entry.get("label", ""))

        # ---------------------------------------------------------------- internals
        def _fake_init(self, args: dict[str, str], entry: dict[str, str]) -> None:
            self.args = dict(args)
            self.entry = dict(entry)
            self.calls: list[tuple[str, tuple, dict]] = []
            self.rx_script: deque[Any] = deque()
            self.tx_script: deque[Any] = deque()
            self.rx_zero_mode = False
            self.settings: dict[tuple[int, int], dict[str, Any]] = {}
            for d in (SOAPY_SDR_RX, SOAPY_SDR_TX):
                for ch in range(cfg.num_channels):
                    self.settings[(d, ch)] = {
                        "rate": 2.4e6, "bw": 2e6, "freq": 2.4e9, "gain": 0.0, "gain_mode": False,
                        "dc_mode": True, "iq_mode": False, "fcorr": 0.0,
                    }
            self.streams: list[_Stream] = []
            self.rx_counter = 0
            self.stale_until = -1
            self.tx_written: list[np.ndarray] = []
            self.tx_recorded = 0
            self.tx_calls = 0
            self.tx_lock = threading.Lock()
            self.read_calls = 0

        def _rec(self, name: str, *args: Any, **kwargs: Any) -> None:
            self.calls.append((name, args, kwargs))

        def called(self, name: str) -> list[tuple[tuple, dict]]:
            return [(a, k) for n, a, k in self.calls if n == name]

        def _ch(self, direction: int, channel: int) -> dict[str, Any]:
            key = (int(direction), int(channel))
            if key not in self.settings:
                raise RuntimeError(f"invalid channel {channel} for direction {direction}")
            return self.settings[key]

        def _active_rx(self) -> list[_Stream]:
            return [s for s in self.streams if s.direction == SOAPY_SDR_RX and s.active and not s.closed]

        # ---------------------------------------------------------------- identity
        def getDriverKey(self) -> str:  # noqa: N802
            self._rec("getDriverKey")
            return cfg.driver_key

        def getHardwareKey(self) -> str:  # noqa: N802
            self._rec("getHardwareKey")
            return cfg.hardware_key

        def getHardwareInfo(self) -> SoapySDRKwargs:  # noqa: N802
            self._rec("getHardwareInfo")
            info = dict(cfg.hardware_info)
            ser = entry_serial(self.entry)
            if ser and "hw_serial" in info:
                info["hw_serial"] = ser
            if self.entry.get("uri") and "uri" in info:
                info["uri"] = self.entry["uri"]
            return SoapySDRKwargs(info)

        def __str__(self) -> str:
            return f"{cfg.driver_key}:{cfg.hardware_key}"

        # ---------------------------------------------------------------- ranges
        def getFrequencyRange(self, direction: int, channel: int, *name: str) -> list[Range]:  # noqa: N802
            self._ch(direction, channel)
            rs = cfg.freq_ranges if direction == SOAPY_SDR_RX else cfg.tx_freq_ranges
            return [Range(lo, hi) for lo, hi in rs]

        def getSampleRateRange(self, direction: int, channel: int) -> list[Range]:  # noqa: N802
            self._ch(direction, channel)
            return [Range(lo, hi) for lo, hi in cfg.rate_ranges]

        def listSampleRates(self, direction: int, channel: int) -> list[float]:  # noqa: N802
            self._ch(direction, channel)
            return list(cfg.rates_list)

        def getBandwidthRange(self, direction: int, channel: int) -> list[Range]:  # noqa: N802
            self._ch(direction, channel)
            return [Range(lo, hi) for lo, hi in cfg.bw_ranges]

        def listBandwidths(self, direction: int, channel: int) -> list[float]:  # noqa: N802
            self._ch(direction, channel)
            return list(cfg.bw_list)

        def getGainRange(self, direction: int, channel: int, *name: str) -> Range:  # noqa: N802
            self._ch(direction, channel)
            lo, hi = cfg.rx_gain_range if direction == SOAPY_SDR_RX else cfg.tx_gain_range
            return Range(lo, hi, 1.0)

        # ---------------------------------------------------------------- settings
        def setSampleRate(self, direction: int, channel: int, rate: float) -> None:  # noqa: N802
            self._rec("setSampleRate", direction, channel, rate)
            self._ch(direction, channel)["rate"] = float(rate)

        def getSampleRate(self, direction: int, channel: int) -> float:  # noqa: N802
            return float(self._ch(direction, channel)["rate"])

        def setBandwidth(self, direction: int, channel: int, bw: float) -> None:  # noqa: N802
            self._rec("setBandwidth", direction, channel, bw)
            self._ch(direction, channel)["bw"] = float(bw)

        def getBandwidth(self, direction: int, channel: int) -> float:  # noqa: N802
            return float(self._ch(direction, channel)["bw"])

        def setFrequency(self, direction: int, channel: int, *args: Any) -> None:  # noqa: N802
            # overloads: (dir, ch, freq[, args]) and (dir, ch, name, freq[, args])
            self._rec("setFrequency", direction, channel, *args)
            freq = args[1] if args and isinstance(args[0], str) else args[0]
            self._ch(direction, channel)["freq"] = float(freq)
            if direction == SOAPY_SDR_RX:
                for s in self._active_rx():
                    self.stale_until = self.rx_counter + cfg.kernel_buffers * s.mtu

        def getFrequency(self, direction: int, channel: int, *name: str) -> float:  # noqa: N802
            return float(self._ch(direction, channel)["freq"])

        def setGain(self, direction: int, channel: int, *args: Any) -> None:  # noqa: N802
            self._rec("setGain", direction, channel, *args)
            value = args[1] if args and isinstance(args[0], str) else args[0]
            self._ch(direction, channel)["gain"] = float(int(value))   # SoapyPlutoSDR: (long long) value

        def getGain(self, direction: int, channel: int, *name: str) -> float:  # noqa: N802
            return float(self._ch(direction, channel)["gain"])

        def hasGainMode(self, direction: int, channel: int) -> bool:  # noqa: N802
            return True

        def setGainMode(self, direction: int, channel: int, automatic: bool) -> None:  # noqa: N802
            self._rec("setGainMode", direction, channel, automatic)
            self._ch(direction, channel)["gain_mode"] = bool(automatic)

        def getGainMode(self, direction: int, channel: int) -> bool:  # noqa: N802
            return bool(self._ch(direction, channel)["gain_mode"])

        def hasDCOffsetMode(self, direction: int, channel: int) -> bool:  # noqa: N802
            return cfg.has_dc_offset_mode

        def setDCOffsetMode(self, direction: int, channel: int, automatic: bool) -> None:  # noqa: N802
            self._rec("setDCOffsetMode", direction, channel, automatic)
            if not cfg.has_dc_offset_mode:
                raise RuntimeError("setDCOffsetMode not supported")
            self._ch(direction, channel)["dc_mode"] = bool(automatic)

        def getDCOffsetMode(self, direction: int, channel: int) -> bool:  # noqa: N802
            return bool(self._ch(direction, channel)["dc_mode"])

        def hasIQBalanceMode(self, direction: int, channel: int) -> bool:  # noqa: N802
            return cfg.has_iq_balance_mode

        def setIQBalanceMode(self, direction: int, channel: int, automatic: bool) -> None:  # noqa: N802
            self._rec("setIQBalanceMode", direction, channel, automatic)
            if not cfg.has_iq_balance_mode:
                raise RuntimeError("setIQBalanceMode not supported")
            self._ch(direction, channel)["iq_mode"] = bool(automatic)

        def getIQBalanceMode(self, direction: int, channel: int) -> bool:  # noqa: N802
            return bool(self._ch(direction, channel)["iq_mode"])

        def hasFrequencyCorrection(self, direction: int, channel: int) -> bool:  # noqa: N802
            return cfg.has_frequency_correction

        def setFrequencyCorrection(self, direction: int, channel: int, value: float) -> None:  # noqa: N802
            self._rec("setFrequencyCorrection", direction, channel, value)
            if not cfg.has_frequency_correction:
                raise RuntimeError("setFrequencyCorrection not supported")
            self._ch(direction, channel)["fcorr"] = float(value)

        def getFrequencyCorrection(self, direction: int, channel: int) -> float:  # noqa: N802
            return float(self._ch(direction, channel)["fcorr"])

        # ---------------------------------------------------------------- sensors
        def listSensors(self, *args: Any) -> list[str]:  # noqa: N802
            return [] if args else list(cfg.sensors)

        def readSensor(self, *args: Any) -> str:  # noqa: N802
            key = str(args[-1])
            if key in cfg.failing_sensors or key not in cfg.sensors:
                raise RuntimeError(f"readSensor({key}) failed")
            return cfg.sensors[key]

        # ---------------------------------------------------------------- streams
        def getStreamFormats(self, direction: int, channel: int) -> list[str]:  # noqa: N802
            return list(cfg.stream_formats)

        def getNativeStreamFormat(self, direction: int, channel: int) -> list[Any]:  # noqa: N802
            self._ch(direction, channel)
            return [cfg.native_format, cfg.native_full_scale]

        def setupStream(self, direction: int, format: str, channels: Any = None, args: Any = None) -> _Stream:  # noqa: N802, A002
            chans = [int(c) for c in (channels if channels is not None else [0])] or [0]
            a = _parse_args(args)
            self._rec("setupStream", direction, format, chans, a)
            if format not in cfg.stream_formats:
                raise RuntimeError(f"setupStream invalid format '{format}'")
            for c in chans:
                self._ch(direction, c)
            mtu = int(a.get("bufflen", cfg.default_mtu))
            s = _Stream(int(direction), format, chans, a, mtu)
            self.streams.append(s)
            return s

        def getStreamMTU(self, stream: _Stream) -> int:  # noqa: N802
            return stream.mtu

        def activateStream(self, stream: _Stream, flags: int = 0, timeNs: int = 0, numElems: int = 0) -> int:  # noqa: N802, N803
            self._rec("activateStream", flags, timeNs, numElems)
            if cfg.activate_ret != 0:
                return cfg.activate_ret
            stream.active = True
            return 0

        def deactivateStream(self, stream: _Stream, flags: int = 0, timeNs: int = 0) -> int:  # noqa: N802, N803
            self._rec("deactivateStream", flags, timeNs)
            stream.active = False
            return 0

        def closeStream(self, stream: _Stream) -> None:  # noqa: N802
            self._rec("closeStream")
            stream.closed = True
            stream.active = False

        def _rx_counts(self, chan_pos: int, start: int, k: int, rate: float) -> np.ndarray:
            idx = np.arange(start, start + k, dtype=np.float64)
            amp = np.where(idx < self.stale_until, cfg.stale_counts, cfg.tone_counts)
            ph = 2.0 * np.pi * cfg.tone_hz * idx / rate + chan_pos * cfg.channel_phase_rad
            return amp * np.exp(1j * ph)

        def readStream(self, stream: _Stream, buffs: list[Any], numElems: int, flags: int = 0,  # noqa: N802, N803
                       timeoutUs: int = 100000) -> StreamResult:  # noqa: N803
            self.read_calls += 1
            self._rec("readStream", int(numElems), flags=flags, timeoutUs=timeoutUs)
            if stream.direction != SOAPY_SDR_RX or not stream.active or stream.closed:
                return StreamResult(ret=SOAPY_SDR_STREAM_ERROR)
            if len(buffs) != len(stream.channels):
                raise ValueError(f"readStream: {len(buffs)} buffers for {len(stream.channels)} channels")
            if self.rx_zero_mode:
                return StreamResult(ret=0)
            limit = int(numElems)
            ev = self.rx_script.popleft() if self.rx_script else None
            if ev == "timeout":
                return StreamResult(ret=SOAPY_SDR_TIMEOUT)
            if ev == "overflow":
                self.rx_counter += cfg.overflow_drop
                return StreamResult(ret=SOAPY_SDR_OVERFLOW)
            if ev == "zero":
                return StreamResult(ret=0)
            if isinstance(ev, int):
                return StreamResult(ret=ev)
            if isinstance(ev, tuple) and ev[0] == "gap":
                self.rx_counter += int(ev[1])
            if isinstance(ev, tuple) and ev[0] == "short":
                limit = min(limit, int(ev[1]))
            k = min(limit, stream.mtu, cfg.max_per_read or limit)
            rate = self._ch(SOAPY_SDR_RX, stream.channels[0])["rate"]
            for pos, buf in enumerate(buffs):
                counts = np.round(self._rx_counts(pos, self.rx_counter, k, rate))
                fsc = cfg.cs16_full_scale
                re = np.clip(counts.real, -fsc, fsc - 1)
                im = np.clip(counts.imag, -fsc, fsc - 1)
                if stream.format == SOAPY_SDR_CS16:
                    if buf.dtype != np.int16 or buf.size < 2 * k:
                        raise TypeError(f"CS16 buffer must be int16 with >= {2 * k} values (got {buf.dtype}, {buf.size})")
                    buf[0:2 * k:2] = re.astype(np.int16)
                    buf[1:2 * k:2] = im.astype(np.int16)
                elif stream.format == SOAPY_SDR_CF32:
                    if buf.dtype != np.complex64 or buf.size < k:
                        raise TypeError(f"CF32 buffer must be complex64 with >= {k} values (got {buf.dtype}, {buf.size})")
                    buf[:k] = ((re + 1j * im) / fsc).astype(np.complex64)
                else:
                    raise TypeError(f"fake readStream: unsupported format {stream.format}")
            t_ns = cfg.time0_ns + int(round(self.rx_counter * 1e9 / rate))
            self.rx_counter += k
            if cfg.has_time:
                return StreamResult(ret=k, flags=SOAPY_SDR_HAS_TIME, timeNs=t_ns)
            return StreamResult(ret=k)

        def writeStream(self, stream: _Stream, buffs: list[Any], numElems: int, flags: int = 0,  # noqa: N802
                        timeNs: int = 0, timeoutUs: int = 100000) -> StreamResult:  # noqa: N803
            self.tx_calls += 1
            if stream.direction != SOAPY_SDR_TX or not stream.active or stream.closed:
                return StreamResult(ret=SOAPY_SDR_STREAM_ERROR)
            ev = self.tx_script.popleft() if self.tx_script else None
            if ev == "underflow":
                return StreamResult(ret=SOAPY_SDR_UNDERFLOW)
            if ev == "timeout":
                return StreamResult(ret=SOAPY_SDR_TIMEOUT)
            if isinstance(ev, int):
                return StreamResult(ret=ev)
            buf = buffs[0]
            k = min(int(numElems), stream.mtu)
            if buf.dtype != np.complex64 or buf.size < k:
                raise TypeError(f"CF32 TX buffer must be complex64 with >= {k} values (got {buf.dtype}, {buf.size})")
            with self.tx_lock:
                if self.tx_recorded < cfg.tx_record_limit:
                    self.tx_written.append(np.array(buf[:k], copy=True))
                    self.tx_recorded += k
            if cfg.tx_pacing_s:
                time.sleep(cfg.tx_pacing_s)
            return StreamResult(ret=k)

        def readStreamStatus(self, stream: _Stream, chanMask: int = 0, flags: int = 0,  # noqa: N802, N803
                             timeoutUs: int = 100000) -> StreamResult:  # noqa: N803
            return StreamResult(ret=SOAPY_SDR_NOT_SUPPORTED)

        def tx_samples(self) -> np.ndarray:
            with self.tx_lock:
                parts = list(self.tx_written)
            return np.concatenate(parts) if parts else np.zeros(0, np.complex64)

    if cfg.define_del:
        def __del__(self: Any) -> None:  # the real binding: "call unmake from custom deleter"
            try:
                Device.unmake(self)
            except RuntimeError:
                pass   # counted in state.double_unmake (real binding: "Exception ignored in __del__")

        Device.__del__ = __del__  # type: ignore[attr-defined]
    for name in cfg.missing_methods:
        delattr(Device, name)

    for k, v in globals().items():
        if k.startswith("SOAPY_SDR_"):
            setattr(mod, k, v)
    mod.errToStr = errToStr
    mod.Range = Range
    mod.SoapySDRKwargs = SoapySDRKwargs
    mod.StreamResult = StreamResult
    mod.Device = Device
    mod.state = state
    mod.setLogLevel = lambda level: None
    mod.getAPIVersion = lambda: "0.8.0"
    mod.getABIVersion = lambda: "0.8"
    return mod


def install(monkeypatch: Any, config: FakeConfig | None = None, **overrides: Any) -> types.ModuleType:
    """Install a fresh fake as ``sys.modules["SoapySDR"]`` (undone by ``monkeypatch``)."""
    mod = make_module(config, **overrides)
    monkeypatch.setitem(sys.modules, "SoapySDR", mod)
    return mod


def uninstall(monkeypatch: Any) -> None:
    """Make ``import SoapySDR`` raise ImportError (module not installed)."""
    monkeypatch.setitem(sys.modules, "SoapySDR", None)
