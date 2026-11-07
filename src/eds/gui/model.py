"""GUI data model — pure Python (no Qt), so it is unit-testable without a display.

The operator GUI is only a *subscriber* of the ZeroMQ bus. This module turns bus messages
into what the panels draw:

* :class:`EmitterTracker` — ``rx.detections`` gives anonymous per-frame detections; the
  tracker associates them over time by frequency and keeps a stable ID (``E001`` …), first /
  last seen time, hit count and the maximum level, i.e. an emitter list an operator can sort.
* :func:`bw_class` — colour code by **occupied bandwidth class**. The repository has no
  modulation classifier yet (no ``classify.result`` producer), so the GUI does not pretend to
  know analog/digital or the modulation: those columns show "—". Bandwidth is measured, so it
  is an honest, consistent signal-type colour key.
* :func:`health_view` — ``rx.status`` → status-bar view (state colour, overflow alarm, …).
* :class:`LatencyStats` — host-side pipeline latency ``t_recv − t_acq_end`` (same-host
  wall clocks; across machines the clock offset adds in).
"""

from __future__ import annotations

import math
from collections import deque
from dataclasses import dataclass, field
from typing import Any

# ----------------------------------------------------------------------------- BW classes


@dataclass(frozen=True)
class BwClass:
    key: str
    label: str          # Turkish legend text
    max_hz: float       # upper bound (exclusive)
    color: str          # hex colour used in table, spectrum overlay and legend


BW_CLASSES: tuple[BwClass, ...] = (
    BwClass("dar", "Dar bant  < 25 kHz", 25e3, "#35d0ff"),
    BwClass("orta", "Orta  25 kHz – 1 MHz", 1e6, "#ffb020"),
    BwClass("genis", "Geniş bant  ≥ 1 MHz", math.inf, "#ff4fa0"),
)


def bw_class(bw_hz: float) -> BwClass:
    for c in BW_CLASSES:
        if bw_hz < c.max_hz:
            return c
    return BW_CLASSES[-1]


# ----------------------------------------------------------------------------- formatting


def fmt_freq_mhz(hz: float | None, digits: int = 4) -> str:
    return "—" if hz is None or not math.isfinite(hz) else f"{hz / 1e6:.{digits}f}"


def fmt_bw(hz: float | None) -> str:
    if hz is None or not math.isfinite(hz):
        return "—"
    return f"{hz / 1e6:.2f} MHz" if hz >= 1e6 else f"{hz / 1e3:.1f} kHz"


def fmt_level(db: float | None, unit: str, calibrated: bool) -> str:
    """Level with unit; uncalibrated (relative) levels carry a leading '~'."""
    if db is None or not math.isfinite(db):
        return "—"
    return f"{db:.1f} {unit}" if calibrated else f"~{db:.1f} {unit}"


# ----------------------------------------------------------------------------- tracking


@dataclass
class Track:
    id: str
    f_center_hz: float
    f_peak_hz: float
    f_lo_hz: float
    f_hi_hz: float
    bw_hz: float
    peak_db: float
    band_power_db: float
    snr_db: float
    noise_db: float
    n_bins: int
    unit: str
    calibrated: bool
    first_seen_ns: int
    last_seen_ns: int
    hits: int = 1
    max_peak_db: float = -math.inf
    rbw_hz: float = float("nan")
    fc_hz: float = float("nan")
    device: str = ""
    method: str = ""
    image_of: str | None = None          # id of the strong emitter this one mirrors (zero-IF IQ image)
    image_delta_db: float = float("nan")

    @property
    def bw_class(self) -> BwClass:
        return bw_class(self.bw_hz)

    def age_s(self, now_ns: int) -> float:
        return max(0.0, (now_ns - self.last_seen_ns) / 1e9)

    def params(self, now_ns: int) -> list[tuple[str, str]]:
        """Every known parameter of this emitter, as (label, value-with-unit) rows."""
        cal = self.calibrated
        return [
            ("Kimlik", self.id),
            ("Merkez frekans", f"{self.f_center_hz / 1e6:.6f} MHz"),
            ("Tepe frekansı", f"{self.f_peak_hz / 1e6:.6f} MHz"),
            ("Alt kenar", f"{self.f_lo_hz / 1e6:.6f} MHz"),
            ("Üst kenar", f"{self.f_hi_hz / 1e6:.6f} MHz"),
            ("Bant genişliği", fmt_bw(self.bw_hz)),
            ("BW sınıfı", self.bw_class.label),
            ("Tepe seviyesi", fmt_level(self.peak_db, self.unit, cal)),
            ("Bant gücü", fmt_level(self.band_power_db, self.unit, cal)),
            ("En yüksek tepe", fmt_level(self.max_peak_db, self.unit, cal)),
            ("Gürültü tabanı", fmt_level(self.noise_db, self.unit, cal)),
            ("SNR", f"{self.snr_db:.1f} dB"),
            ("Bin sayısı", str(self.n_bins)),
            ("RBW", f"{self.rbw_hz:.0f} Hz" if math.isfinite(self.rbw_hz) else "—"),
            ("Merkez (LO)", fmt_freq_mhz(self.fc_hz, 6) + " MHz" if math.isfinite(self.fc_hz) else "—"),
            ("Görülme sayısı", str(self.hits)),
            ("İlk görülme", f"{(now_ns - self.first_seen_ns) / 1e9:.1f} s önce"),
            ("Son görülme", f"{self.age_s(now_ns):.1f} s önce"),
            ("Kalibrasyon", "dBm (kalibreli)" if cal else "kalibrasyonsuz (göreli)"),
            ("Tespit yöntemi", self.method or "—"),
            ("Cihaz", self.device or "—"),
            ("IQ imajı şüphesi", (f"evet: {self.image_of} emiterinin LO'ya göre aynası, {self.image_delta_db:.0f} dB "
                                  "daha zayıf (sıfır-IF artefaktı olabilir)") if self.image_of else "yok"),
            ("Modülasyon", "— (sınıflandırıcı yok)"),
        ]


def merge_overlapping(dets: list[dict[str, Any]], min_overlap: float = 0.5) -> list[dict[str, Any]]:
    """Merge detections of ONE frame that cover mostly the same band.

    Two detections whose ``[f_lo, f_hi]`` intervals overlap by at least ``min_overlap`` of the
    narrower one are the same emitter seen twice (e.g. native + coarse CFAR pass, or an FM
    spectrum split in two): keep the stronger one with the union of both bands. A narrow carrier
    inside a much wider signal overlaps by 100 % of *its* width but is kept separate when the
    detector reported it on purpose — so only near-equal widths (ratio ≤ 3) are merged.
    """
    out: list[dict[str, Any]] = []
    for d in sorted(dets, key=lambda x: float(x.get("f_lo_hz", x["f_center_hz"]))):
        lo, hi = float(d.get("f_lo_hz", d["f_center_hz"])), float(d.get("f_hi_hz", d["f_center_hz"]))
        if out:
            p = out[-1]
            plo, phi = float(p["f_lo_hz"]), float(p["f_hi_hz"])
            ov = min(hi, phi) - max(lo, plo)
            w_small, w_big = min(hi - lo, phi - plo), max(hi - lo, phi - plo)
            if ov > 0 and w_small > 0 and ov >= min_overlap * w_small and w_big <= 3.0 * w_small:
                keep = dict(p if float(p.get("peak_db", -math.inf)) >= float(d.get("peak_db", -math.inf)) else d)
                keep["f_lo_hz"], keep["f_hi_hz"] = min(lo, plo), max(hi, phi)
                keep["bw_hz"] = keep["f_hi_hz"] - keep["f_lo_hz"]
                keep["f_center_hz"] = 0.5 * (keep["f_lo_hz"] + keep["f_hi_hz"])
                out[-1] = keep
                continue
        out.append(dict(d, f_lo_hz=lo, f_hi_hz=hi))
    return out


IMAGE_MIN_DELTA_DB = 20.0   # a mirror-frequency track this much weaker is flagged as a possible IQ image
IMAGE_MAX_BW_RATIO = 2.5    # ... and only if its bandwidth is similar to the strong track's


class EmitterTracker:
    """Associates per-frame detections into persistent emitters.

    A detection joins the nearest existing track whose centre is within
    ``max(match_hz_min, 3·RBW, ½·max(BW_track, BW_det))``; each track takes at most one
    detection per frame (strongest first). Unmatched detections open new tracks. Tracks not
    seen for ``hold_s`` are removed; ``stale_s`` marks them greyed ("eski") before that.
    """

    def __init__(self, match_hz_min: float = 5e3, hold_s: float = 15.0, stale_s: float = 3.0,
                 max_tracks: int = 500):
        self.match_hz_min = float(match_hz_min)
        self.hold_s = float(hold_s)
        self.stale_s = float(stale_s)
        self.max_tracks = int(max_tracks)
        self._tracks: dict[str, Track] = {}
        self._next = 1
        self.frames = 0

    def reset(self) -> None:
        self._tracks.clear()

    def _new_id(self) -> str:
        tid = f"E{self._next:03d}"
        self._next += 1
        return tid

    def update(self, data: dict[str, Any], t_ns: int) -> None:
        """Feed one ``rx.detections`` payload (``data``) received at host time ``t_ns``."""
        self.frames += 1
        unit = str(data.get("unit", "dBFS"))
        calibrated = unit == "dBm"
        rbw = float(data.get("rbw_hz", float("nan")))
        fc = float(data.get("fc_hz", float("nan")))
        dets = sorted(merge_overlapping(list(data.get("detections") or [])),
                      key=lambda d: -float(d.get("peak_db", -math.inf)))
        used: set[str] = set()
        for d in dets:
            fcen = float(d["f_center_hz"])
            bw = float(d.get("bw_hz", 0.0))
            best, best_df = None, math.inf
            for t in self._tracks.values():
                if t.id in used:
                    continue
                tol = max(self.match_hz_min, 3.0 * rbw if math.isfinite(rbw) else 0.0, 0.5 * max(t.bw_hz, bw))
                df = abs(t.f_center_hz - fcen)
                if df <= tol and df < best_df:
                    best, best_df = t, df
            vals = {
                "f_center_hz": fcen, "f_peak_hz": float(d.get("f_peak_hz", fcen)),
                "f_lo_hz": float(d.get("f_lo_hz", fcen - bw / 2)), "f_hi_hz": float(d.get("f_hi_hz", fcen + bw / 2)),
                "bw_hz": bw, "peak_db": float(d.get("peak_db", math.nan)),
                "band_power_db": float(d.get("band_power_db", math.nan)), "snr_db": float(d.get("snr_db", math.nan)),
                "noise_db": float(d.get("noise_db", math.nan)), "n_bins": int(d.get("n_bins", 0)),
            }
            if best is None:
                if len(self._tracks) >= self.max_tracks:
                    continue
                t = Track(id=self._new_id(), unit=unit, calibrated=calibrated, first_seen_ns=t_ns,
                          last_seen_ns=t_ns, max_peak_db=vals["peak_db"], rbw_hz=rbw, fc_hz=fc,
                          device=str(data.get("device", "")), method=str(data.get("method", "")), **vals)
                self._tracks[t.id] = t
                used.add(t.id)
            else:
                for k, v in vals.items():
                    setattr(best, k, v)
                best.unit, best.calibrated, best.rbw_hz, best.fc_hz = unit, calibrated, rbw, fc
                best.last_seen_ns = t_ns
                best.hits += 1
                if math.isfinite(vals["peak_db"]):
                    best.max_peak_db = max(best.max_peak_db, vals["peak_db"])
                used.add(best.id)
        self.prune(t_ns)
        self._flag_images(fc, rbw)

    def _flag_images(self, fc: float, rbw: float) -> None:
        """Mark tracks that sit at the LO mirror ``2·fc − f`` of a much stronger track.

        A zero-IF receiver (AD936x) leaks a weak copy of every strong signal at the mirror
        frequency (finite image rejection, ~55 dB with quadrature tracking, ~30 dB without).
        The GUI only *flags* it (``image_of``); it never hides a detection.
        """
        if not math.isfinite(fc):
            return
        tracks = list(self._tracks.values())
        for t in tracks:
            t.image_of, t.image_delta_db = None, float("nan")
            if not math.isfinite(t.peak_db):
                continue
            best_delta = IMAGE_MIN_DELTA_DB
            for o in tracks:
                if o is t or not math.isfinite(o.peak_db) or abs(o.fc_hz - t.fc_hz) > 1.0:
                    continue
                # an IQ image is a mirrored copy: same occupied bandwidth as its source
                floor = max(2.0 * rbw if math.isfinite(rbw) else 0.0, 1.0)
                bw_t, bw_o = max(t.bw_hz, floor), max(o.bw_hz, floor)
                if max(bw_t, bw_o) > IMAGE_MAX_BW_RATIO * min(bw_t, bw_o):
                    continue
                mirror = 2.0 * fc - o.f_center_hz
                tol = max(self.match_hz_min, 3.0 * rbw if math.isfinite(rbw) else 0.0, 0.5 * min(bw_t, bw_o))
                delta = o.peak_db - t.peak_db
                if abs(t.f_center_hz - mirror) <= tol and delta >= best_delta:
                    t.image_of, t.image_delta_db, best_delta = o.id, delta, delta

    def prune(self, now_ns: int) -> None:
        dead = [k for k, t in self._tracks.items() if t.age_s(now_ns) > self.hold_s]
        for k in dead:
            del self._tracks[k]

    def is_stale(self, t: Track, now_ns: int) -> bool:
        return t.age_s(now_ns) > self.stale_s

    def tracks(self) -> list[Track]:
        return sorted(self._tracks.values(), key=lambda t: t.f_center_hz)

    def get(self, tid: str) -> Track | None:
        return self._tracks.get(tid)

    def __len__(self) -> int:
        return len(self._tracks)


# ----------------------------------------------------------------------------- health


@dataclass
class HealthView:
    connected: bool
    state: str                      # streaming | sweeping | idle | error | — (no data)
    level: str                      # ok | warn | bad | none  → colour of the state lamp
    device: str = ""
    fc_hz: float = float("nan")
    fs_hz: float = float("nan")
    gain_text: str = "—"
    overflow_text: str = "—"
    overflow_alarm: bool = False
    throughput_text: str = "—"
    ppm_text: str = "—"
    temperature_text: str = "—"
    warnings: list[str] = field(default_factory=list)
    age_s: float = math.inf


def health_view(status: dict[str, Any] | None, t_recv_ns: int | None, now_ns: int,
                timeout_s: float = 3.0) -> HealthView:
    """``rx.status`` data → status bar. ``None`` (never received) → disconnected view."""
    if status is None or t_recv_ns is None:
        return HealthView(connected=False, state="—", level="none")
    age = (now_ns - t_recv_ns) / 1e9
    connected = age <= timeout_s
    state = str(status.get("state", "—"))
    level = {"streaming": "ok", "sweeping": "warn", "idle": "warn", "error": "bad"}.get(state, "warn")
    st = status.get("stream") or {}
    ovf = int(st.get("overflows", 0))
    src = str(st.get("overflow_source", "") or "")
    if src == "none":
        ovf_text, alarm = "bilinmiyor (sürücü raporlamaz)", False
    else:
        ovf_text, alarm = str(ovf), ovf > 0
    drops = int(st.get("host_drops", 0))
    if drops:
        ovf_text += f" · host drop {drops}"
        alarm = True
    if alarm and level == "ok":
        level = "warn"
    if not connected:
        level = "bad"
    gains = status.get("gain_db") or []
    gain_text = ("/".join(f"{g:g}" for g in gains) + " dB" if gains else "—") + f" ({status.get('gain_mode', '—')})"
    thr = float(st.get("throughput_sps", 0.0))
    ppm = status.get("ppm") or {}
    pv = ppm.get("value")
    ppm_text = (f"{pv:+.2f} ppm ({ppm.get('via', '—')})" if isinstance(pv, (int, float)) else
                f"yok ({ppm.get('via', 'none')})")
    sensors = status.get("sensors") or {}
    temp = sensors.get("temperature_c")
    dev = status.get("device") or {}
    return HealthView(
        connected=connected, state=state, level=level, device=str(dev.get("name", "")),
        fc_hz=float(status.get("fc_hz", math.nan)), fs_hz=float(status.get("fs_hz", math.nan)),
        gain_text=gain_text, overflow_text=ovf_text, overflow_alarm=alarm,
        throughput_text=f"{thr / 1e6:.2f} MS/s" if thr > 0 else "—",
        ppm_text=ppm_text, temperature_text=f"{temp:.1f} °C" if isinstance(temp, (int, float)) else "—",
        warnings=list(status.get("warnings") or []), age_s=age,
    )


# ----------------------------------------------------------------------------- latency


class LatencyStats:
    """Rolling host-side latency [ms] of detections: ``t_recv − t_acq_end`` (same host clock)."""

    def __init__(self, maxlen: int = 200):
        self._v: deque[float] = deque(maxlen=maxlen)

    def add(self, t_recv_ns: int, t_acq_end_ns: int | None) -> None:
        if t_acq_end_ns:
            ms = (t_recv_ns - int(t_acq_end_ns)) / 1e6
            if math.isfinite(ms) and -1e4 < ms < 1e6:
                self._v.append(ms)

    def median(self) -> float:
        if not self._v:
            return math.nan
        s = sorted(self._v)
        return s[len(s) // 2]

    def p95(self) -> float:
        if not self._v:
            return math.nan
        s = sorted(self._v)
        return s[min(len(s) - 1, int(round(0.95 * (len(s) - 1))))]

    def __len__(self) -> int:
        return len(self._v)


# ----------------------------------------------------------------------------- spectrum frame


@dataclass
class SpectrumFrame:
    """One ``rx.spectrum`` message, ready to draw (frequencies in Hz, values in ``unit``)."""

    f_start_hz: float
    f_step_hz: float
    values: list[float]
    unit: str
    calibrated: bool
    ppm_corrected: bool
    fc_hz: float
    fs_hz: float
    rbw_hz: float
    noise_floor: float
    gain_db: list[float]
    decimation: int
    t_acq_end_ns: int
    overflow: bool

    @classmethod
    def from_data(cls, d: dict[str, Any]) -> SpectrumFrame:
        unit = str(d.get("unit", "dBFS"))
        return cls(
            f_start_hz=float(d["f_start_hz"]), f_step_hz=float(d["f_step_hz"]),
            values=list(d["values"]), unit=unit, calibrated=bool(d.get("calibrated", unit == "dBm")),
            ppm_corrected=bool(d.get("ppm_corrected", False)), fc_hz=float(d.get("fc_hz", math.nan)),
            fs_hz=float(d.get("fs_hz", math.nan)), rbw_hz=float(d.get("rbw_hz", math.nan)),
            noise_floor=float(d.get("noise_floor", math.nan)), gain_db=list(d.get("gain_db") or []),
            decimation=int(d.get("decimation", 1)), t_acq_end_ns=int(d.get("t_acq_end_ns", 0)),
            overflow=bool(d.get("overflow", False)),
        )

    @property
    def n(self) -> int:
        return len(self.values)

    @property
    def cal_mode(self) -> str:
        if self.calibrated:
            return "dBm · ppm düzeltmeli" if self.ppm_corrected else "dBm · ppm yok"
        return "dBFS · kalibrasyonsuz" + (" · ppm düzeltmeli" if self.ppm_corrected else "")

    def geometry_key(self) -> tuple[float, float, int]:
        """Changes when the frequency axis changes (retune / new rate) → reset waterfall."""
        return (round(self.f_start_hz, 1), round(self.f_step_hz, 6), self.n)
