"""SoapySDR backend — the primary stream/device abstraction (Pluto via SoapyPlutoSDR).

Every SoapySDR-supported radio (Pluto now; bladeRF/USRP/HackRF later) is opened through
:class:`SoapyRxDevice`. Pluto-only low-level controls that SoapySDR does not expose go
through the thin libiio layer :class:`eds.hal.pluto_iio.PlutoIIO`, opened lazily and only
for driver ``plutosdr``.

SoapyPlutoSDR specifics relied upon — verify on the target machine with
``SoapySDRUtil --probe="driver=plutosdr"`` (driver builds differ):

* **Native RX format CS16, full scale 2048.** The 12-bit ADC value arrives sign-extended in
  int16 (range [-2048, 2047]); ``getNativeStreamFormat`` reports ``("CS16", 2048)``. The
  driver's CF32 conversion is ``counts / 2048``. EDS reads CS16 by default and normalises
  ``counts / fullScale`` so that ADC full scale == 1.0 (``docs/ARCHITECTURE.md`` §2).
* **Stream arg ``bufflen``** = libiio buffer length in samples (= ``getStreamMTU``). EDS
  passes ``RxConfig.buffer_size``. Further stream args come from device args prefixed
  ``stream:`` (``args = {"stream:bufflen" = "131072"}`` overrides); those keys are stripped
  from the device args.
* **Kernel buffers → stale samples after a retune.** libiio keeps ``kernel_buffers``
  (default 4) buffers queued; after a retune / rate / gain change the next
  ``kernel_buffers × bufflen`` samples were captured with the OLD settings. The base class
  flags them ``settling`` and :meth:`~eds.hal.base.RxDevice.capture` discards them.
* **Overflow = ``SOAPY_SDR_OVERFLOW`` (-4).** SoapySDR semantics: the ``readStream`` call
  reporting OVERFLOW returns no samples; the samples of the next call follow the gap.
  Some driver builds report overruns late or never — when hardware timestamps are present
  EDS also flags a forward timestamp jump as a gap (``detect_ts_gaps``), and
  ``eds measure max_rate`` cross-checks delivered vs. expected sample count.
* **Hardware timestamps are optional** (``flags & SOAPY_SDR_HAS_TIME``, ``timeNs``); when
  present they are forwarded as ``IQBlock.meta['hw_time_ns']``.
* **Pass-through device args** (untouched, see SoapyPlutoSDR docs of your build):
  ``uri`` (``usb:1.5.5`` / ``ip:192.168.2.1``), ``hostname``, ``usb_direct=1`` (libusb
  direct streaming), ``timestamp_every=<n>``, ``loopback=<n>``.
* **AGC:** ``setGainMode(RX, ch, true)`` selects AD9361 ``slow_attack``; ``fast_attack`` and
  ``hybrid`` need the thin layer, as do RF-DC tracking, ``xo_correction`` (hardware PPM
  correction) and the AD9361 temperature sensor.
* **Thin layer vs. USB:** a second libiio USB context to a Pluto SoapyPlutoSDR is streaming
  from usually fails (interface busy). Set ``control_uri = "ip:192.168.2.1"`` (Pluto
  USB-Ethernet gadget; iiod serves several clients). A failure to open the thin layer is a
  warning plus fallback, never a crash.
* **Runtime limits** reported by SoapyPlutoSDR are those of the AD9364 (e.g. 70 MHz–6 GHz);
  the AD9363 profile limits are authoritative and are checked before any driver call.
"""

from __future__ import annotations

import logging
import math
from collections.abc import Callable, Iterable, Mapping
from types import ModuleType
from typing import Any

import numpy as np

from ..core.config import DeviceConfig
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
from .pluto_iio import PlutoIIO

log = logging.getLogger(__name__)

STREAM_FORMATS: tuple[str, ...] = ("CS16", "CF32")
DEFAULT_CS16_FULL_SCALE = 2048.0
STREAM_ARG_PREFIX = "stream:"

#: EDS gain mode -> AD936x ``gain_control_mode``
AD936X_GAIN_MODES: dict[str, str] = {
    "manual": "manual", "hw_slow": "slow_attack", "hw_fast": "fast_attack", "hw_hybrid": "hybrid",
}

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


def enumerate_soapy(args: Mapping[str, str] | None = None) -> list[dict[str, str]]:
    """``SoapySDR.Device.enumerate(args)`` as a list of plain ``dict[str, str]``."""
    sdr = import_soapy()
    filt = {str(k): str(v) for k, v in (args or {}).items()}
    try:
        found = sdr.Device.enumerate(filt)
    except Exception as e:
        raise DeviceError(f"SoapySDR cihaz taraması başarısız (args={filt}): {e}") from e
    return [kwargs_to_dict(k) for k in found]


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


# ---------------------------------------------------------------------------- device
class SoapyRxDevice(RxDevice):
    """RX device opened through SoapySDR (``cfg.args`` = Soapy device kwargs).

    ``stream_format`` ("CS16" default | "CF32") is the host stream format
    (``RxConfig.stream_format``); ``detect_ts_gaps`` flags forward jumps of hardware
    timestamps as overflow (only when the driver sets ``SOAPY_SDR_HAS_TIME``).
    """

    backend_name = "soapy"

    def __init__(self, cfg: DeviceConfig, profile: DeviceProfile, *, stream_format: str = "CS16",
                 detect_ts_gaps: bool = True):
        super().__init__(cfg, profile)
        fmt = str(stream_format).upper()
        if fmt not in STREAM_FORMATS:
            raise ConfigError(f"stream_format {stream_format!r} geçersiz; seçenekler: {list(STREAM_FORMATS)}")
        if not cfg.channels:
            raise ConfigError(f"{cfg.name}: en az bir RX kanalı gerekli (channels boş)")
        self.stream_format: str = fmt
        self.detect_ts_gaps = bool(detect_ts_gaps)
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

        # stream / runtime state
        self._stream: Any = None
        self._bufs: list[np.ndarray] = []
        self._cap = 0
        self._scale = DEFAULT_CS16_FULL_SCALE
        self._limits: dict[str, tuple[float, float]] | None = None
        self._ts_expect: int | None = None
        self._zero_since: float | None = None
        self.ts_gap_events = 0
        self.stream_mtu = 0
        self.native_format: tuple[str, float] = ("", 0.0)
        self.agc_mode_actual = "manual"
        self.xo_correction_hz: int | None = None
        self._soapy_ppm_applied = 0.0
        self._thin_obj: PlutoIIO | None = None
        self._thin_tried = False
        self._hwinfo: dict[str, str] = {}

        try:
            self._dev: Any = self._sdr.Device(dict(self.device_args))
        except Exception as e:
            raise DeviceNotFound(self._not_found_message(e)) from e
        try:
            self._fill_info()
        except Exception as e:
            try:
                self._close()
            except EDSError as close_err:
                log.warning("%s: açılış hatası sonrası kapatma da başarısız: %s", self.name, close_err)
            raise DeviceError(f"{self.name}: cihaz kimliği okunamadı (getDriverKey/getHardwareKey): {e}") from e

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

    @property
    def is_pluto(self) -> bool:
        return "plutosdr" in (self.device_args.get("driver", "").lower(), self.info.driver.lower())

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
        """The Pluto thin libiio layer, opened lazily on first need (None if unavailable)."""
        if self._thin_obj is not None:
            return self._thin_obj
        if self._thin_tried or self._dev is None:
            return None
        self._thin_tried = True
        if not self.is_pluto:
            return None
        if (self.cfg.control_uri or "").strip().lower() == "none":
            return None
        uri = self.thin_uri()
        if not uri:
            self._warn("Pluto ince libiio katmanı için URI yok (control_uri / args.uri / args.hostname); "
                       "xo_correction, RF-DC tracking, hızlı AGC ve AD9361 sıcaklığı kullanılamaz.")
            return None
        try:
            thin = PlutoIIO(uri)
        except EDSError as e:
            hint = (' USB üzerinden SoapyPlutoSDR akışıyla aynı anda ikinci bir libiio USB bağlamı genellikle '
                    'açılamaz; control_uri = "ip:192.168.2.1" kullanın.') if uri.startswith("usb:") else ""
            self._warn(f"Pluto ince katmanı ({uri}) açılamadı: {e}.{hint} Soapy yoluna düşülüyor.")
            return None
        self._thin_obj = thin
        self.info.extra["control_uri"] = uri
        if thin.xo_boot_hz is not None:
            self.info.extra["xo_correction_boot_hz"] = str(thin.xo_boot_hz)
            if thin.xo_boot_hz != int(round(self.profile.ref_clock_hz)):
                self._warn(f"xo_correction açılışta {thin.xo_boot_hz} Hz (nominal {self.profile.ref_clock_hz:.0f} Hz "
                           "değil; firmware ortamında ayarlı olabilir). PPM kestirimi nominale göre yapılmalı: "
                           "ölçümden önce set_ppm_correction(0) çağrılır.")
        return thin

    # ------------------------------------------------------------------ limits
    def runtime_limits(self) -> dict[str, Any]:
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
            lim["gain_db"] = r
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
        d = self._require()
        try:
            res = d.getNativeStreamFormat(self._RX, ch)
        except Exception as e:
            log.debug("%s: getNativeStreamFormat başarısız: %s", self.name, e)
            return "", 0.0
        if isinstance(res, str):
            return res, 0.0
        seq = list(res)
        fmt = str(seq[0]) if seq else ""
        full = float(seq[1]) if len(seq) > 1 else 0.0
        return fmt, full

    def _start(self, buffer_size: int) -> None:
        d = self._require()
        sdr = self._sdr
        chans = list(self.cfg.channels)
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
        self._stream, self._bufs, self._cap, self._scale = stream, bufs, cap, float(scale)
        self.stream_mtu = mtu
        self._ts_expect = None
        self._zero_since = None

    def _close_stream_quietly(self, stream: Any, reason: str) -> None:
        """Best-effort ``closeStream`` while another error is already being raised."""
        try:
            self._dev.closeStream(stream)
        except Exception as e:
            log.warning("%s: closeStream (%s) da başarısız: %s", self.name, reason, e)

    def _read_raw(self, n: int, timeout_s: float) -> RawRead:
        stream, d = self._stream, self._dev
        if stream is None or d is None:
            raise StreamError(f"{self.name}: akış başlatılmadı")
        sdr = self._sdr
        k_req = max(1, min(int(n), self._cap))
        try:
            sr = d.readStream(stream, self._bufs, k_req, timeoutUs=int(max(float(timeout_s), 0.0) * 1e6))
        except Exception as e:
            raise StreamError(f"{self.name}: readStream istisnası: {e}") from e
        t_ns = now_ns()
        ret = int(sr.ret)
        nch = len(self._bufs)
        if ret > 0:
            k = min(ret, k_req)
            out = np.empty((nch, k), dtype=np.complex64)
            if self.stream_format == "CS16":
                inv = np.float32(1.0 / self._scale)
                for i, b in enumerate(self._bufs):
                    np.multiply(b[: 2 * k].astype(np.float32).view(np.complex64), inv, out=out[i])
            else:
                for i, b in enumerate(self._bufs):
                    out[i] = b[:k]
            flags = int(sr.flags)
            hw = int(sr.timeNs) if flags & int(sdr.SOAPY_SDR_HAS_TIME) else None
            gap = self._timestamp_gap(hw, k)
            self._zero_since = None
            return RawRead(samples=out, overflow_before=gap, t_ns=t_ns, hw_time_ns=hw)
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
        self._stream, self._bufs, self._cap = None, [], 0
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
        if thin is not None:
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
        After a hardware change fc and fs are re-programmed (the driver recomputes its PLLs
        with the new reference) and the following samples are flagged settling.
        """
        ppm = float(ppm)
        if not math.isfinite(ppm):
            raise ConfigError(f"ppm sonlu bir sayı olmalı (geldi: {ppm!r})")
        with self._lock:
            d = self._require()
            rx = self._RX
            via = "none"
            thin = self.thin_layer()
            if thin is not None:
                hz = int(round(self.profile.ref_clock_hz * (1.0 + ppm * 1e-6)))
                try:
                    self.xo_correction_hz = thin.set_xo_correction(hz)
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
                for ch in self.cfg.channels:
                    self._call("setFrequencyCorrection", d.setFrequencyCorrection, rx, ch, ppm)
                self._soapy_ppm_applied = ppm
                via = "soapy"
                if self.xo_correction_hz is not None and self.xo_correction_hz != int(round(self.profile.ref_clock_hz)):
                    self._warn(f"Önceki xo_correction ({self.xo_correction_hz} Hz) geri alınamadı; PPM iki kez "
                               "uygulanmış olabilir — cihazı yeniden açın.")
            self.ppm_hw = ppm if via != "none" else 0.0
            self.ppm_hw_via = via
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
            "stream_mtu": self.stream_mtu, "control_uri": self.thin_uri() if self.is_pluto else "",
            "thin_layer": self._thin_obj is not None, "agc_mode_actual": self.agc_mode_actual,
            "ts_gap_events": self.ts_gap_events, "xo_correction_hz": self.xo_correction_hz,
            "warnings": list(self.warnings),
        })
        return d
