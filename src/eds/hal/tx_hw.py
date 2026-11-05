"""Hardware stimulus sources for the measurement suite (see :mod:`eds.hal.tx`).

* :class:`SoapyTxSource`        — 2nd Pluto (later HackRF) TX over SoapySDR: CW / two-tone
  from a precomputed buffer, fed continuously by a background thread.
* :class:`ScpiSignalGenerator`  — lab generator over raw-socket SCPI (TCP 5025).
* :class:`ManualSource`         — asks the operator (Turkish prompts) to set a generator.

Tone generation (:func:`build_tone_buffer`): every tone gets an **integer number of cycles**
in the buffer (``f = k·fs/N``), so wrapping the buffer is phase-continuous and the spectrum
is a clean line, not a spread of wrap-around discontinuities. The TX LO sits
``tone_offset_hz`` below a single tone (LO leakage / DC never coincides with the tone);
for two tones it sits at their midpoint (no odd-order IMD product lands on it).

Power on the Pluto TX is *relative* (0 dB = maximum TX gain, digital amplitude 0.5 so the
DAC and PA stay linear) and realised as **TX gain first, then digital scaling** for the
remainder. With ``anchor_dbm`` (input power measured at the DUT reference plane at relative
0 dB, path loss included) the source becomes ``absolute_power``:
``P_dut = anchor_dbm − path_loss_db + P_rel`` (``path_loss_db`` = loss added *after* the
anchor measurement, default 0).
"""

from __future__ import annotations

import logging
import math
import socket
import threading
from collections.abc import Callable, Mapping, Sequence
from typing import Any

import numpy as np

from ..core.errors import (
    ConfigError,
    DeviceError,
    DeviceNotFound,
    EDSError,
    MeasurementError,
    NotSupported,
    StreamError,
)
from ..core.timeutil import now_ns
from ..core.units import fmt_hz
from .soapy_backend import import_soapy, release_device
from .tx import SignalSource

log = logging.getLogger(__name__)

DIGITAL_FULL_AMPLITUDE = 0.5     # peak |x| of the (summed) digital tone at relative 0 dB
DIGITAL_RANGE_DB = 60.0          # deepest digital attenuation used below minimum TX gain
MAX_BASEBAND_FRACTION = 0.4      # |f_bb| <= 0.4·fs (TX interpolation filter passband)


# ---------------------------------------------------------------------------- tone buffer
def quantize_tone_freq(f_hz: float, fs: float, n: int) -> float:
    """Nearest frequency with an integer number of cycles in ``n`` samples at ``fs``."""
    return round(float(f_hz) * n / fs) * fs / n


def build_tone_buffer(freqs_bb: Sequence[float], amps: Sequence[float], fs: float,
                      n: int) -> tuple[np.ndarray, list[float]]:
    """Sum of complex tones, each with an integer number of cycles in ``n`` samples.

    Returns ``(samples complex64 (n,), actual_freqs_hz)``. ``x[k + n] == x[k]`` exactly, so
    playing the buffer in a loop is phase-continuous. Peak ``|x| <= Σ amps``.
    """
    n = int(n)
    fs = float(fs)
    if n < 2 or fs <= 0:
        raise ConfigError(f"Ton tamponu: n >= 2 ve fs > 0 olmalı (n={n}, fs={fs})")
    if len(freqs_bb) != len(amps) or not freqs_bb:
        raise ConfigError(f"Ton tamponu: frekans ve genlik listeleri aynı uzunlukta ve boş olmamalı "
                          f"({len(freqs_bb)} / {len(amps)})")
    k = np.arange(n, dtype=np.int64)
    x = np.zeros(n, dtype=np.complex128)
    actual: list[float] = []
    for f, a in zip(freqs_bb, amps):
        cycles = int(round(float(f) * n / fs))
        if 2 * abs(cycles) >= n:
            raise ConfigError(f"Ton {fmt_hz(float(f))} Nyquist sınırının (±{fmt_hz(fs / 2)}) dışında")
        if not (math.isfinite(float(a)) and float(a) >= 0):
            raise ConfigError(f"Ton genliği negatif/geçersiz: {a!r}")
        # integer phase index keeps the argument exact for long buffers
        x += float(a) * np.exp(2j * np.pi * ((cycles * k) % n) / n)
        actual.append(cycles * fs / n)
    return x.astype(np.complex64), actual


# ---------------------------------------------------------------------------- Soapy TX
class SoapyTxSource(SignalSource):
    """CW / two-tone stimulus from a SoapySDR TX channel (Pluto by default)."""

    name = "pluto_tx"
    max_tones = 2

    def __init__(self, args: Mapping[str, str] | None = None, fs: float = 2.4e6,
                 tone_offset_hz: float = 200e3, path_loss_db: float = 0.0,
                 anchor_dbm: float | None = None, buffer_len: int = 8192, *,
                 channel: int = 0, write_timeout_s: float = 0.1, swap_timeout_s: float = 2.0):
        fs = float(fs)
        buffer_len = int(buffer_len)
        if fs <= 0:
            raise ConfigError(f"TX örnekleme hızı pozitif olmalı (geldi: {fs})")
        if buffer_len < 64:
            raise ConfigError(f"TX buffer_len en az 64 olmalı (geldi: {buffer_len})")
        if tone_offset_hz == 0 or abs(tone_offset_hz) > MAX_BASEBAND_FRACTION * fs:
            raise ConfigError(f"tone_offset_hz sıfır olamaz ve |offset| <= {MAX_BASEBAND_FRACTION}·fs olmalı "
                              f"(geldi: {tone_offset_hz}, fs={fs})")
        self._sdr = import_soapy()
        self.args = {str(k): str(v) for k, v in (args if args is not None else {"driver": "plutosdr"}).items()}
        self.channel = int(channel)
        self.tone_offset_hz = float(tone_offset_hz)
        self.path_loss_db = float(path_loss_db)
        self.anchor_dbm = None if anchor_dbm is None else float(anchor_dbm)
        self.absolute_power = self.anchor_dbm is not None
        self.buffer_len = buffer_len
        self._write_timeout_us = int(float(write_timeout_s) * 1e6)
        self._swap_timeout_s = float(swap_timeout_s)
        self._TX = int(self._sdr.SOAPY_SDR_TX)
        self._stream: Any = None
        self._thread: threading.Thread | None = None
        self._stop_evt = threading.Event()
        self._pending: tuple[np.ndarray, threading.Event] | None = None
        self._pending_lock = threading.Lock()
        self._swap_t_ns = 0
        self._error: EDSError | None = None
        self._closed = False
        self.underflows = 0
        self.write_timeouts = 0
        self.samples_written = 0
        self.lo_hz: float | None = None
        self.tones_hz: list[float] = []          # actual RF tone frequencies
        self._bb_freqs: list[float] = []
        self._levels_rel: list[float] = []       # requested per-tone relative levels
        self.tone_levels_rel_db: list[float] = []  # realised per-tone relative levels
        self.digital_amps: list[float] = []
        self.gain_db = 0.0
        self.enabled = False

        from .soapy_backend import resolve_serial_args

        self.args = resolve_serial_args(self.args, name=self.name)
        try:
            self._dev: Any = self._sdr.Device(dict(self.args))
        except Exception as e:
            raise DeviceNotFound(f"TX cihazı açılamadı (args={self.args}): {e}. Kontrol: SoapySDRUtil --find") from e
        try:
            self._setup(fs)
        except Exception as e:
            self._teardown()
            if isinstance(e, EDSError):
                raise
            raise DeviceError(f"TX kurulumu başarısız (args={self.args}): {e}") from e

    # ------------------------------------------------------------------ setup / teardown
    def _setup(self, fs: float) -> None:
        d, tx, ch = self._dev, self._TX, self.channel
        d.setSampleRate(tx, ch, fs)
        self.fs = float(d.getSampleRate(tx, ch))
        try:
            d.setBandwidth(tx, ch, max(self.fs, 200e3))
        except Exception as e:  # analog TX filter is optional; the driver default is wide enough
            log.warning("TX setBandwidth başarısız (sürücü varsayılanı kullanılıyor): %s", e)
        gr = d.getGainRange(tx, ch)
        self._gmin, self._gmax = float(gr.minimum()), float(gr.maximum())
        try:
            self._gstep = float(gr.step())
        except Exception as e:  # Range without step() in very old bindings
            log.debug("TX gain step bilinmiyor: %s", e)
            self._gstep = 0.0
        if self._gmax <= self._gmin:
            raise DeviceError(f"TX kazanç aralığı geçersiz: [{self._gmin}, {self._gmax}]")
        try:
            fr = list(d.getFrequencyRange(tx, ch))
            self.freq_range_hz = (min(float(r.minimum()) for r in fr), max(float(r.maximum()) for r in fr))
        except Exception as e:
            log.debug("TX getFrequencyRange kullanılamadı: %s", e)
            self.freq_range_hz = SignalSource.freq_range_hz
        self._update_power_range()
        self.gain_db = float(self._set_tx_gain(self._gmin))     # safe start: minimum gain, zeros
        self._zero = np.zeros(self.buffer_len, dtype=np.complex64)
        self._tone = self._zero
        self._stream = d.setupStream(tx, self._sdr.SOAPY_SDR_CF32, [ch], {"bufflen": str(self.buffer_len)})
        try:
            mtu = int(d.getStreamMTU(self._stream))
        except Exception as e:
            log.debug("TX getStreamMTU kullanılamadı: %s", e)
            mtu = 0
        self._chunk = max(1, min(self.buffer_len, mtu if mtu > 0 else self.buffer_len))
        ret = d.activateStream(self._stream)
        if isinstance(ret, (int, np.integer)) and int(ret) != 0:
            raise StreamError(f"TX activateStream hata kodu {int(ret)} ({self._sdr.errToStr(int(ret))})")
        self._levels_rel = [self._gmin - self._gmax]
        self._thread = threading.Thread(target=self._writer, name="eds-tx-writer", daemon=True)
        self._thread.start()

    def _update_power_range(self) -> None:
        lo = self._gmin - self._gmax - DIGITAL_RANGE_DB
        off = self._abs_offset()
        self.power_range_db = (lo + off, 0.0 + off)

    def _abs_offset(self) -> float:
        return (self.anchor_dbm - self.path_loss_db) if self.anchor_dbm is not None else 0.0

    def _teardown(self) -> None:
        self._stop_evt.set()
        t = self._thread
        if t is not None and t.is_alive():
            t.join(timeout=max(2.0, 4 * self._write_timeout_us / 1e6))
            if t.is_alive():
                log.warning("TX yazıcı thread'i 2 s içinde durmadı; akış yine de kapatılıyor")
        self._thread = None
        d, stream = getattr(self, "_dev", None), self._stream
        self._stream = None
        if d is not None and stream is not None:
            try:
                d.deactivateStream(stream)
            except Exception as e:
                log.warning("TX deactivateStream başarısız: %s", e)
            try:
                d.closeStream(stream)
            except Exception as e:
                log.warning("TX closeStream başarısız: %s", e)
        if d is not None:
            self._dev = None
            try:
                release_device(self._sdr, d)
            except Exception as e:
                log.warning("TX cihazı bırakılamadı: %s", e)
            del d

    # ------------------------------------------------------------------ writer thread
    def _take_pending(self) -> tuple[np.ndarray, threading.Event] | None:
        with self._pending_lock:
            p, self._pending = self._pending, None
            return p

    def _writer(self) -> None:
        sdr, d, stream = self._sdr, self._dev, self._stream
        cur = self._zero
        pos = 0
        timeout_code, underflow_code = int(sdr.SOAPY_SDR_TIMEOUT), int(sdr.SOAPY_SDR_UNDERFLOW)
        try:
            while not self._stop_evt.is_set():
                pend = self._take_pending()
                if pend is not None:
                    cur = pend[0]
                    pos %= len(cur)
                    self._swap_t_ns = now_ns()
                    pend[1].set()
                k = min(self._chunk, len(cur) - pos)
                sr = d.writeStream(stream, [cur[pos:pos + k]], k, timeoutUs=self._write_timeout_us)
                ret = int(sr.ret)
                if ret > 0:
                    pos = (pos + ret) % len(cur)
                    self.samples_written += ret
                elif ret == underflow_code:
                    self.underflows += 1
                elif ret == timeout_code or ret == 0:
                    self.write_timeouts += 1
                else:
                    self._error = StreamError(f"TX writeStream hata kodu {ret} ({sdr.errToStr(ret)})")
                    break
        except Exception as e:
            self._error = StreamError(f"TX writeStream istisnası: {e!r}")
        finally:
            pend = self._take_pending()
            if pend is not None:
                pend[1].set()
            if self._error is not None:
                log.error("TX yazıcı durdu: %s", self._error)

    def _check(self) -> None:
        if self._closed:
            raise StreamError("TX kaynağı kapalı")
        if self._error is not None:
            raise StreamError(f"TX akışı hatalı: {self._error}")

    def _swap(self, buf: np.ndarray) -> int:
        """Hand ``buf`` to the writer; returns host time when its first chunk was submitted."""
        self._check()
        t = self._thread
        if t is None or not t.is_alive():
            raise StreamError("TX yazıcı thread'i çalışmıyor")
        ev = threading.Event()
        with self._pending_lock:
            self._pending = (buf, ev)
        if not ev.wait(self._swap_timeout_s):
            raise StreamError(f"TX yazıcı {self._swap_timeout_s:.1f} s içinde yeni tamponu almadı")
        self._check()
        return self._swap_t_ns

    # ------------------------------------------------------------------ levels / tones
    def _set_tx_gain(self, g: float) -> float:
        d = self._dev
        d.setGain(self._TX, self.channel, float(min(max(g, self._gmin), self._gmax)))
        return float(d.getGain(self._TX, self.channel))

    def _to_rel(self, level: float) -> float:
        level = float(level)
        if not math.isfinite(level):
            raise ConfigError(f"TX seviyesi sonlu bir sayı olmalı (geldi: {level!r})")
        return level - self._abs_offset()

    def _apply_levels(self) -> None:
        """TX gain from the strongest requirement, digital amplitudes for the rest."""
        p = np.asarray(self._levels_rel, dtype=float)
        g_star = 20.0 * math.log10(float(np.sum(10.0 ** (p / 20.0))))      # needed hw level (rel)
        g_target = min(max(g_star, self._gmin - self._gmax), 0.0)
        want = self._gmax + g_target
        g_act = self._set_tx_gain(want)
        if g_act < want - 1e-9 and g_act < self._gmax:
            # driver rounded down (e.g. integer dB): go one step up, the digital stage attenuates the rest
            step = self._gstep if self._gstep > 0 else 1.0
            g_act = self._set_tx_gain(g_act + step * math.ceil((want - g_act) / step - 1e-9))
        self.gain_db = g_act
        G = g_act - self._gmax
        amps = DIGITAL_FULL_AMPLITUDE * 10.0 ** ((p - G) / 20.0)
        total = float(np.sum(amps))
        if total > DIGITAL_FULL_AMPLITUDE:        # gain rounded down / request above maximum
            amps *= DIGITAL_FULL_AMPLITUDE / total
        amps = np.maximum(amps, DIGITAL_FULL_AMPLITUDE * 10.0 ** (-DIGITAL_RANGE_DB / 20.0))
        if float(np.sum(amps)) > DIGITAL_FULL_AMPLITUDE:
            amps *= DIGITAL_FULL_AMPLITUDE / float(np.sum(amps))
        self.digital_amps = [float(a) for a in amps]
        self.tone_levels_rel_db = [float(G + 20.0 * math.log10(a / DIGITAL_FULL_AMPLITUDE)) for a in amps]
        if self._bb_freqs:
            self._tone, _ = build_tone_buffer(self._bb_freqs, self.digital_amps, self.fs, self.buffer_len)
            if self.enabled:
                self._swap(self._tone)

    def _tune(self, lo: float, bb: list[float]) -> None:
        d = self._dev
        lo_min, lo_max = self.freq_range_hz
        if not (lo_min <= lo <= lo_max):
            raise ConfigError(f"TX LO {fmt_hz(lo)} cihaz aralığı dışında [{fmt_hz(lo_min)} – {fmt_hz(lo_max)}]")
        d.setFrequency(self._TX, self.channel, float(lo))
        self.lo_hz = float(d.getFrequency(self._TX, self.channel))
        self._bb_freqs = bb
        self.tones_hz = [self.lo_hz + f for f in bb]

    def set_frequency(self, hz: float) -> float:
        """Single CW tone at ``hz`` (LO = hz − tone_offset). Returns the actual RF frequency."""
        self._check()
        f_bb = quantize_tone_freq(self.tone_offset_hz, self.fs, self.buffer_len)
        level = max(self._levels_rel) if self._levels_rel else self._gmin - self._gmax
        try:
            self._tune(float(hz) - f_bb, [f_bb])
        except EDSError:
            raise
        except Exception as e:
            raise DeviceError(f"TX frekansı {fmt_hz(float(hz))} ayarlanamadı: {e}") from e
        self._levels_rel = [level]
        self._apply_levels_safe()
        return self.tones_hz[0]

    def set_tones(self, freqs_hz: list[float], powers: list[float]) -> None:
        """1–2 tones (two-tone IMD): LO at the midpoint, amplitude split digitally.

        ``powers`` per tone, same unit as :meth:`set_power`. Actual RF frequencies in
        ``tones_hz``, actual levels in ``tone_levels_rel_db``.
        """
        self._check()
        if not 1 <= len(freqs_hz) <= self.max_tones or len(freqs_hz) != len(powers):
            raise NotSupported(f"{self.name}: 1–{self.max_tones} ton desteklenir; frekans/güç sayısı eşit olmalı "
                               f"({len(freqs_hz)} / {len(powers)})")
        if len(freqs_hz) == 1:
            self.set_frequency(freqs_hz[0])
            self.set_power(powers[0])
            return
        f1, f2 = (float(f) for f in freqs_hz)
        half = quantize_tone_freq(abs(f2 - f1) / 2.0, self.fs, self.buffer_len)
        if half <= 0 or half > MAX_BASEBAND_FRACTION * self.fs:
            raise ConfigError(f"İki ton aralığı ({fmt_hz(abs(f2 - f1))}) TX bandına sığmıyor / çok küçük "
                              f"(fs = {fmt_hz(self.fs)})")
        sign = 1.0 if f2 >= f1 else -1.0
        try:
            self._tune((f1 + f2) / 2.0, [-sign * half, sign * half])
        except EDSError:
            raise
        except Exception as e:
            raise DeviceError(f"TX iki ton ayarlanamadı: {e}") from e
        self._levels_rel = [self._to_rel(p) for p in powers]
        self._apply_levels_safe()

    def set_power(self, dbm: float) -> float:
        """Set every active tone to ``dbm`` (absolute if ``absolute_power`` else relative dB,
        0 = maximum). Returns the realised level of the first tone in the same unit."""
        self._check()
        rel = self._to_rel(dbm)
        if rel > 0:
            log.warning("TX seviyesi %.2f dB bağıl istenmiş; en fazla 0 dB (maksimum) uygulanır", rel)
        n_tones = max(1, len(self._bb_freqs))
        self._levels_rel = [rel] * n_tones
        self._apply_levels_safe()
        return self.tone_levels_rel_db[0] + self._abs_offset()

    def _apply_levels_safe(self) -> None:
        try:
            self._apply_levels()
        except EDSError:
            raise
        except Exception as e:
            raise DeviceError(f"TX seviyesi ayarlanamadı: {e}") from e

    def set_enabled(self, on: bool) -> int:
        """Tone (on) vs zeros (off). Returns host time when the new buffer's first chunk was
        handed to the driver (the driver/kernel queue latency comes on top)."""
        if self._closed:
            if on:
                raise StreamError("TX kaynağı kapalı")
            return now_ns()
        if on and not self._bb_freqs:
            raise ConfigError("TX: önce set_frequency()/set_tones() ile frekans ayarlayın")
        t = self._swap(self._tone if on else self._zero)
        self.enabled = bool(on)
        return t

    # ------------------------------------------------------------------ info / lifecycle
    @property
    def estimated_queue_latency_s(self) -> float:
        """Rough upper bound of the driver queue (4 kernel buffers of ``buffer_len``)."""
        return 4.0 * self.buffer_len / self.fs

    def describe(self) -> dict[str, Any]:
        return {
            "name": self.name, "absolute_power": self.absolute_power, "args": dict(self.args),
            "fs_hz": self.fs, "lo_hz": self.lo_hz, "tones_hz": list(self.tones_hz),
            "tone_offset_hz": self.tone_offset_hz, "gain_db": self.gain_db,
            "gain_range_db": [self._gmin, self._gmax], "digital_amps": list(self.digital_amps),
            "tone_levels_rel_db": list(self.tone_levels_rel_db), "anchor_dbm": self.anchor_dbm,
            "path_loss_db": self.path_loss_db, "power_range_db": list(self.power_range_db),
            "enabled": self.enabled, "underflows": self.underflows, "write_timeouts": self.write_timeouts,
            "samples_written": self.samples_written, "buffer_len": self.buffer_len,
            "estimated_queue_latency_s": self.estimated_queue_latency_s,
            "error": str(self._error) if self._error else "",
        }

    def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        self._teardown()


# ---------------------------------------------------------------------------- SCPI
class ScpiSignalGenerator(SignalSource):
    """Lab RF generator over raw-socket SCPI (``host:5025``).

    Commands: ``*IDN?``, ``:FREQ <Hz>``, ``:POW <dBm>`` (generator level = desired DUT level
    + ``path_loss_db``), ``:OUTP ON|OFF``; every setting is synchronised with ``*OPC?`` and,
    with ``readback=True``, verified with ``:FREQ?`` / ``:POW?``.
    """

    name = "scpi"
    absolute_power = True
    max_tones = 1

    def __init__(self, host: str, port: int = 5025, timeout_s: float = 3.0, path_loss_db: float = 0.0, *,
                 readback: bool = True):
        self.host, self.port = str(host), int(port)
        self.timeout_s = float(timeout_s)
        self.path_loss_db = float(path_loss_db)
        self.readback = bool(readback)
        self._lock = threading.Lock()
        self._rbuf = b""
        try:
            self._sock: socket.socket | None = socket.create_connection((self.host, self.port), timeout=self.timeout_s)
        except OSError as e:
            raise DeviceNotFound(f"SCPI üreteç bağlantısı kurulamadı ({self.host}:{self.port}): {e}") from e
        self._sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
        try:
            self.idn = self.query("*IDN?")
        except EDSError:
            self.close()
            raise
        if not self.idn:
            self.close()
            raise DeviceError(f"SCPI üreteç ({self.host}:{self.port}) *IDN? sorgusuna boş yanıt verdi")
        self.enabled = False
        self.frequency_hz: float | None = None
        self.level_dbm: float | None = None

    # ------------------------------------------------------------------ transport
    def _require(self) -> socket.socket:
        if self._sock is None:
            raise DeviceError(f"SCPI bağlantısı kapalı ({self.host}:{self.port})")
        return self._sock

    def write(self, cmd: str) -> None:
        sock = self._require()
        try:
            sock.sendall((cmd.strip() + "\n").encode("ascii"))
        except OSError as e:
            raise DeviceError(f"SCPI yazma hatası ({cmd!r}): {e}") from e

    def _readline(self, cmd: str) -> str:
        sock = self._require()
        while b"\n" not in self._rbuf:
            try:
                chunk = sock.recv(4096)
            except TimeoutError as e:
                raise DeviceError(f"SCPI yanıtı {self.timeout_s:.1f} s içinde gelmedi ({cmd!r})") from e
            except OSError as e:
                raise DeviceError(f"SCPI okuma hatası ({cmd!r}): {e}") from e
            if not chunk:
                raise DeviceError(f"SCPI bağlantısı karşı tarafça kapatıldı ({cmd!r})")
            self._rbuf += chunk
        line, self._rbuf = self._rbuf.split(b"\n", 1)
        return line.decode("ascii", errors="replace").strip()

    def query(self, cmd: str) -> str:
        with self._lock:
            self.write(cmd)
            return self._readline(cmd)

    def _opc(self, after: str) -> None:
        r = self.query("*OPC?")
        if r.lstrip("+") != "1":
            raise DeviceError(f"SCPI *OPC? beklenmeyen yanıt {r!r} ({after} sonrası)")

    def _query_float(self, cmd: str) -> float:
        r = self.query(cmd)
        try:
            return float(r.split(",")[0])
        except ValueError as e:
            raise DeviceError(f"SCPI {cmd} sayısal yanıt vermedi: {r!r}") from e

    # ------------------------------------------------------------------ SignalSource
    def set_frequency(self, hz: float) -> float:
        cmd = f":FREQ {float(hz):.3f}"
        with self._lock:
            self.write(cmd)
        self._opc(cmd)
        self.frequency_hz = self._query_float(":FREQ?") if self.readback else float(hz)
        return self.frequency_hz

    def set_power(self, dbm: float) -> float:
        level = float(dbm) + self.path_loss_db
        cmd = f":POW {level:.2f}"
        with self._lock:
            self.write(cmd)
        self._opc(cmd)
        gen = self._query_float(":POW?") if self.readback else level
        self.level_dbm = gen - self.path_loss_db
        return self.level_dbm

    def set_enabled(self, on: bool) -> int:
        cmd = f":OUTP {'ON' if on else 'OFF'}"
        with self._lock:
            self.write(cmd)
        self._opc(cmd)
        t = now_ns()
        self.enabled = bool(on)
        return t

    def describe(self) -> dict[str, Any]:
        return {"name": self.name, "absolute_power": True, "idn": self.idn, "host": self.host,
                "port": self.port, "path_loss_db": self.path_loss_db, "frequency_hz": self.frequency_hz,
                "level_dbm": self.level_dbm, "enabled": self.enabled}

    def close(self) -> None:
        sock, self._sock = self._sock, None
        if sock is not None:
            try:
                sock.close()
            except OSError as e:
                log.warning("SCPI soketi kapatılamadı: %s", e)

    def __exit__(self, *exc: object) -> None:
        try:
            if self._sock is not None:
                self.set_enabled(False)
        finally:
            self.close()


# ---------------------------------------------------------------------------- manual
class ManualSource(SignalSource):
    """The operator sets a generator by hand; every step waits for Enter.

    The operator may type the value actually set (Hz / dBm, ``,`` or ``.`` decimal) instead
    of just pressing Enter; that value is then returned as the actual setting.
    """

    name = "manual"
    max_tones = 1
    max_attempts = 5

    def __init__(self, prompt: Callable[[str], str] = input, absolute_power: bool = True,
                 path_loss_db: float = 0.0):
        self._prompt = prompt
        self.absolute_power = bool(absolute_power)
        self.path_loss_db = float(path_loss_db)
        self.frequency_hz: float | None = None
        self.level: float | None = None
        self.enabled = False

    def _ask(self, msg: str) -> str:
        try:
            return str(self._prompt(msg))
        except EOFError as e:
            raise MeasurementError("Operatör girişi alınamadı (etkileşimsiz oturum?) — ManualSource kullanılamaz") from e

    def _ask_number(self, msg: str, default: float) -> float:
        text = msg
        for _ in range(self.max_attempts):
            reply = self._ask(text).strip().replace(",", ".")
            if not reply:
                return float(default)
            try:
                return float(reply)
            except ValueError:
                text = f"Geçersiz sayı: {reply!r}. {msg}"
        raise MeasurementError(f"{self.max_attempts} denemede geçerli değer girilmedi")

    def set_frequency(self, hz: float) -> float:
        hz = float(hz)
        msg = (f"[EDS] Sinyal üretecinin frekansını {fmt_hz(hz, 10)} ({hz:.0f} Hz) yapın. Hazır olunca Enter'a "
               "basın (farklı bir değer ayarladıysanız Hz cinsinden yazın): ")
        self.frequency_hz = self._ask_number(msg, hz)
        return self.frequency_hz

    def set_power(self, dbm: float) -> float:
        dbm = float(dbm)
        if self.absolute_power:
            gen = dbm + self.path_loss_db
            msg = (f"[EDS] Üreteç seviyesini {gen:.2f} dBm yapın (alıcı girişinde {dbm:.2f} dBm; yol kaybı "
                   f"{self.path_loss_db:.2f} dB). Hazır olunca Enter'a basın (farklıysa ayarlanan dBm değerini yazın): ")
            self.level = self._ask_number(msg, gen) - self.path_loss_db
        else:
            msg = (f"[EDS] Kaynak seviyesini bağıl {dbm:.2f} dB yapın (0 dB = en yüksek). Hazır olunca Enter'a "
                   "basın (farklıysa ayarlanan değeri yazın): ")
            self.level = self._ask_number(msg, dbm)
        return self.level

    def set_enabled(self, on: bool) -> int:
        state = "AÇIN" if on else "KAPATIN"
        self._ask(f"[EDS] Üretecin RF çıkışını {state}. Hazır olunca Enter'a basın: ")
        self.enabled = bool(on)
        return now_ns()

    def describe(self) -> dict[str, Any]:
        return {"name": self.name, "absolute_power": self.absolute_power, "path_loss_db": self.path_loss_db,
                "frequency_hz": self.frequency_hz, "level": self.level, "enabled": self.enabled}
