"""Realistic, schema-valid example payloads (the ``data`` member) for every bus topic.

Other test suites import these (``from tests.bus.examples import example``) to build
messages without re-inventing the contract. Numbers are physically consistent with the
Phase-0 rig (ADALM-Pluto / AD9363, 12-bit, 40 MHz TCXO) and the conventions of
``docs/ARCHITECTURE.md`` §2 (power-spectrum scaling, RBW = ENBW·fs/N, ppm sign, wrap_deg).

* :data:`EXAMPLES` — one canonical payload per topic (treat as read-only; use
  :func:`example` for a private deep copy).
* :data:`VARIANTS` — further valid payloads per topic (minimal / error / alternate
  hardware cases).
"""

from __future__ import annotations

import copy
import math
from typing import Any

import numpy as np

from eds.bus import topics as T
from eds.bus.envelope import make_envelope

T0_NS = 1_793_871_000_000_000_000          # 2026-11-05T09:30:00Z
C_LIGHT = 299_792_458.0
ENBW_BLACKMANHARRIS = 2.00435              # 4-term Blackman-Harris ENBW [bins]
PLUTO_SERIAL = "10447318ac0f000c1e0024004b3e85a7b1"
PPM = 12.37                                 # TCXO fast by 12.37 ppm
TEMP_C = 41.5

_rng = np.random.default_rng(20261105)


def _r(x: float, nd: int = 2) -> float:
    return float(round(float(x), nd))


# --------------------------------------------------------------------------- rx.status
def _stream(blocks: int, block: int, elapsed_s: float, **kw: int) -> dict[str, Any]:
    samples = blocks * block
    d = {
        "blocks": blocks, "samples": samples, "overflows": 0, "timeouts": 0, "host_drops": 0,
        "discarded_samples": 0,
        "throughput_sps": _r(samples / elapsed_s, 1) if elapsed_s > 0 else 0.0,
        "elapsed_s": _r(elapsed_s, 4),
    }
    d.update(kw)
    return d


RX_STATUS_EX: dict[str, Any] = {
    "device": {"name": "rx0", "backend": "soapy", "driver": "plutosdr", "serial": PLUTO_SERIAL,
               "profile": "ad9363_pluto"},
    "state": "streaming",
    "fc_hz": 915e6,
    "fs_hz": 4e6,
    "bw_hz": 4e6,
    "gain_db": [40.0],
    "gain_mode": "manual",
    "ppm": {"value": PPM, "via": "hardware", "age_s": 312.5},
    "stream": _stream(1831, 65536, 30.0021),
    "levels": {"peak_dbfs": -12.4, "rms_dbfs": -31.75, "clip_fraction": 0.0},
    "sensors": {"temperature_c": TEMP_C, "rssi_db": -62.25,
                "xo_correction_hz": _r(40e6 * (1 + PPM * 1e-6), 1), "firmware": "v0.38"},
    "warnings": [],
}

RX_STATUS_VARIANTS: list[dict[str, Any]] = [
    {   # freshly opened simulator device, nothing configured yet (required fields only)
        "device": {"name": "sim0", "backend": "sim"},
        "state": "idle", "fc_hz": 0.0, "fs_hz": 0.0, "gain_db": [0.0],
        "stream": _stream(0, 65536, 0.0),
    },
    {   # USB 2.0 bottleneck at 61.44 MS/s: overflows, no calibration, no levels yet
        "device": {"name": "rx1", "backend": "soapy", "driver": "plutosdr", "serial": "", "profile": "ad9363_pluto"},
        "state": "error", "fc_hz": 2.437e9, "fs_hz": 61.44e6, "bw_hz": 20e6,
        "gain_db": [50.0], "gain_mode": "manual",
        "ppm": {"value": None, "via": "none", "age_s": None},
        "stream": _stream(212, 65536, 1.9, overflows=37, discarded_samples=1_212_416, timeouts=1),
        "levels": {"peak_dbfs": None, "rms_dbfs": None, "clip_fraction": None},
        "sensors": {"temperature_c": None},
        "warnings": ["USB 2.0 darboğazı: 61.44 MS/s sürdürülemiyor (37 overflow)"],
    },
    {   # 2× Pluto as one non-coherent group (DF phase 0), software ppm, sweeping
        "device": {"name": "df0", "backend": "group", "profile": "ad9363_pluto"},
        "state": "sweeping", "fc_hz": 433.92e6, "fs_hz": 2.4e6, "bw_hz": 2e6,
        "gain_db": [45.0, 46.0], "gain_mode": "soft_agc",
        "ppm": {"value": -3.8, "via": "software", "age_s": 5400.0},
        "stream": _stream(80, 16384, 0.55, host_drops=2),
        "levels": {"peak_dbfs": -6.02, "rms_dbfs": -24.9, "clip_fraction": 1.2e-6},
        "sensors": {},
        "warnings": [],
    },
]


# --------------------------------------------------------------------------- rx.spectrum
SPEC_FC, SPEC_FS, SPEC_NFFT, SPEC_AVG, SPEC_DEC = 915e6, 4e6, 4096, 16, 4
SPEC_BIN_HZ = SPEC_FS / SPEC_NFFT                                   # 976.5625 Hz
SPEC_RBW = ENBW_BLACKMANHARRIS * SPEC_BIN_HZ                         # ≈ 1957.4 Hz
SPEC_STEP = SPEC_DEC * SPEC_BIN_HZ                                   # 3906.25 Hz
SPEC_F0 = SPEC_FC - SPEC_FS / 2                                      # f[0] = fc - fs/2
SPEC_POINTS = math.ceil(SPEC_NFFT / SPEC_DEC)                        # 1024
SPEC_NOISE = -95.02                                                  # dBFS in RBW
TONE_HZ, TONE_DBFS = 915.2e6, -42.31
LORA_HZ, LORA_BW, LORA_DBFS = 914.0e6, 125e3, -80.0                  # density per RBW
SPEC_T_ACQ = T0_NS + 500_000_000
_samples = int(SPEC_NFFT * (1 + (SPEC_AVG - 1) * 0.5))               # 34816 samples, 50 % overlap
SPEC_T_END = SPEC_T_ACQ + int(round(_samples / SPEC_FS * 1e9))       # +8.704 ms
SPEC_T_PROC = SPEC_T_END + 2_150_000


def _spectrum_values() -> list[float]:
    f = SPEC_F0 + np.arange(SPEC_POINTS) * SPEC_STEP
    off = np.abs(f - SPEC_FC) / (SPEC_FS / 2)
    rolloff = -2.0 * off ** 6                                        # analog filter edge
    # max-hold of 4 averaged bins sits ~1 dB above the median floor
    v = SPEC_NOISE + 1.0 + rolloff + _rng.normal(0.0, 0.6, SPEC_POINTS)
    lora = np.abs(f - LORA_HZ) <= LORA_BW / 2
    v[lora] = LORA_DBFS + 1.0 + _rng.normal(0.0, 0.6, int(lora.sum()))
    v[int(round((SPEC_FC - SPEC_F0) / SPEC_STEP))] = -71.4         # zero-IF DC residue
    v[int((TONE_HZ - SPEC_F0) // SPEC_STEP)] = TONE_DBFS
    return [_r(x) for x in v]


RX_SPECTRUM_EX: dict[str, Any] = {
    "device": "rx0", "block_seq": 4571,
    "t_acq_ns": SPEC_T_ACQ, "t_acq_end_ns": SPEC_T_END, "t_proc_ns": SPEC_T_PROC,
    "fc_hz": SPEC_FC, "fs_hz": SPEC_FS, "rbw_hz": _r(SPEC_RBW, 3),
    "nfft": SPEC_NFFT, "averages": SPEC_AVG, "window": "blackmanharris",
    "f_start_hz": SPEC_F0, "f_step_hz": SPEC_STEP, "n_bins": SPEC_NFFT, "decimation": SPEC_DEC,
    "unit": "dBFS", "calibrated": False, "ppm_corrected": True, "gain_db": [40.0],
    "noise_floor": SPEC_NOISE, "values": _spectrum_values(), "overflow": False,
}

RX_SPECTRUM_VARIANTS: list[dict[str, Any]] = [
    {   # power-calibrated (dBm), undecimated 1024-bin frame at 2.437 GHz, after an overflow
        **copy.deepcopy(RX_SPECTRUM_EX),
        "fc_hz": 2.437e9, "fs_hz": 10e6, "nfft": 1024, "n_bins": 1024, "decimation": 1,
        "rbw_hz": _r(ENBW_BLACKMANHARRIS * 10e6 / 1024, 3), "f_start_hz": 2.432e9,
        "f_step_hz": 10e6 / 1024, "unit": "dBm", "calibrated": True, "ppm_corrected": False,
        "gain_db": [50.0], "noise_floor": -118.6,
        "values": [_r(-117.6 + x) for x in _rng.normal(0.0, 0.6, 1024)], "overflow": True,
    },
]


# --------------------------------------------------------------------------- detections
def _det(f_center: float, f_peak: float, n_bins: int, bin_hz: float, peak: float,
         band_power: float, noise: float) -> dict[str, Any]:
    half = n_bins * bin_hz / 2
    return {
        "f_center_hz": _r(f_center, 1), "f_peak_hz": _r(f_peak, 1),
        "f_lo_hz": _r(f_center - half, 1), "f_hi_hz": _r(f_center + half, 1),
        "bw_hz": _r(n_bins * bin_hz, 1), "peak_db": _r(peak), "band_power_db": _r(band_power),
        "snr_db": _r(peak - noise), "noise_db": _r(noise), "n_bins": n_bins,
    }


_lora_band_power = LORA_DBFS + 10 * math.log10((LORA_BW / SPEC_BIN_HZ) / ENBW_BLACKMANHARRIS)

RX_DETECTIONS_EX: dict[str, Any] = {
    "device": "rx0", "block_seq": 4571,
    "t_acq_ns": SPEC_T_ACQ, "t_acq_end_ns": SPEC_T_END, "t_proc_ns": SPEC_T_PROC + 310_000,
    "fc_hz": SPEC_FC, "rbw_hz": _r(SPEC_RBW, 3), "unit": "dBFS", "method": "os_cfar", "pfa": 1e-6,
    "detections": [
        _det(TONE_HZ + 12.4, TONE_HZ + 37.1, 7, SPEC_BIN_HZ, TONE_DBFS, TONE_DBFS + 0.03, -95.1),
        _det(LORA_HZ + 210.0, LORA_HZ + 31_250.0, 128, SPEC_BIN_HZ, LORA_DBFS + 2.2,
             _lora_band_power, -95.1),
    ],
}

RX_DETECTIONS_VARIANTS: list[dict[str, Any]] = [
    {**copy.deepcopy(RX_DETECTIONS_EX), "detections": [], "block_seq": 4572},   # empty frame
    {**copy.deepcopy(RX_DETECTIONS_EX), "unit": "dBm", "method": "ca_cfar", "pfa": 1e-4},
]


# --------------------------------------------------------------------------- rx.sweep
def _sweep(f_start: float, f_stop: float, fs: float, nfft: int, max_points: int, unit: str,
           calibrated: bool, sweep_id: int, t_start_ns: int, noise: float,
           signals: list[tuple[float, float, float]], timing: dict[str, float],
           detections: list[dict[str, Any]], usable: float = 0.8) -> dict[str, Any]:
    step = usable * fs
    n_steps = math.ceil((f_stop - f_start) / step)
    bin_hz = fs / nfft
    n_bins = int(round((f_stop - f_start) / bin_hz))
    dec = max(1, math.ceil(n_bins / max_points))
    n_pts = math.ceil(n_bins / dec)
    f = f_start + np.arange(n_pts) * dec * bin_hz
    v = noise + _rng.normal(0.0, 0.7, n_pts)
    for fc, bw, level in signals:
        m = np.abs(f - fc) <= max(bw / 2, dec * bin_hz / 2)
        v[m] = level + _rng.normal(0.0, 0.7, int(m.sum()))
    duration = _r(sum(timing.values()) + 0.06, 3)
    return {
        "device": "rx0", "sweep_id": sweep_id, "t_start_ns": t_start_ns,
        "t_end_ns": t_start_ns + int(round(duration * 1e9)),
        "f_start_hz": f_start, "f_stop_hz": f_stop,
        "rbw_hz": _r(ENBW_BLACKMANHARRIS * bin_hz, 3), "n_steps": n_steps, "step_hz": step,
        "duration_s": duration, "rate_hz_per_s": _r((f_stop - f_start) / duration, 1),
        "unit": unit, "calibrated": calibrated, "decimation": dec,
        "f_point_start_hz": f_start, "f_point_step_hz": dec * bin_hz,
        "values": [_r(x) for x in v], "detections": detections, "timing": timing,
    }


_SWEEP_BIN = 20e6 / 2048
RX_SWEEP_EX: dict[str, Any] = _sweep(
    325e6, 3.8e9, 20e6, 2048, 8192, "dBm", True, 37, T0_NS + 10_000_000_000, -121.0,
    signals=[(433.92e6, 50e3, -71.5), (947.4e6, 200e3, -58.3), (1842.5e6, 20e6, -77.0),
             (2437e6, 20e6, -66.2), (3500e6, 100e6, -86.5)],
    timing={"tune_s": 0.654, "settle_s": 3.575, "capture_s": 0.121, "dsp_s": 0.208},
    detections=[
        _det(433.92e6, 433.9201e6, 6, _SWEEP_BIN, -71.5, -71.9, -121.0),
        _det(947.4e6 + 850.0, 947.4e6 + 9765.6, 21, _SWEEP_BIN, -58.3, -50.4, -121.0),
        _det(1842.5e6, 1838.1e6, 2048, _SWEEP_BIN, -75.8, -54.9, -121.0),
        _det(2437e6, 2431.6e6, 2048, _SWEEP_BIN, -64.9, -44.1, -121.0),
    ],
)

RX_SWEEP_VARIANTS: list[dict[str, Any]] = [
    # uncalibrated, gain-referred ISM 2.4 GHz sweep with no detection
    _sweep(2.4e9, 2.4835e9, 20e6, 2048, 8192, "dBFS@50dB", False, 38, T0_NS + 20_000_000_000,
           -96.0, signals=[], timing={"tune_s": 0.018, "settle_s": 0.098, "capture_s": 0.004,
                                      "dsp_s": 0.011}, detections=[]),
    _sweep(2.4e9, 2.4835e9, 20e6, 2048, 8192, "dBFS@-1.5dB", False, 39, T0_NS + 21_000_000_000,
           -60.0, signals=[], timing={"tune_s": 0.018, "settle_s": 0.098, "capture_s": 0.004,
                                      "dsp_s": 0.011}, detections=[]),
    _sweep(325e6, 341e6, 20e6, 2048, 8192, "dBFS", False, 40, T0_NS + 22_000_000_000,
           -90.0, signals=[], timing={"tune_s": 0.003, "settle_s": 0.016, "capture_s": 0.001,
                                      "dsp_s": 0.002}, detections=[]),
]


# --------------------------------------------------------------------------- cal.result
_F_REF = 915.2e6
_f_meas = _F_REF / (1 + PPM * 1e-6)          # apparent frequency of a fast TCXO

CAL_RESULT_EX: dict[str, Any] = {
    "device": "rx0", "serial": PLUTO_SERIAL, "kind": "ppm",
    "values": {"ppm": PPM, "std_ppm": 0.04, "f_measured_hz": _r(_f_meas, 2), "f_ref_hz": _F_REF,
               "snr_db": 48.7, "n": 262144, "repeats": 5, "method": "cw"},
    "applied_via": "hardware", "t_ns": T0_NS - 312_500_000_000, "temperature_c": TEMP_C,
}

CAL_RESULT_VARIANTS: list[dict[str, Any]] = [
    {"device": "rx0", "serial": PLUTO_SERIAL, "kind": "power",
     "values": {"points": [[433.92e6, 40.0, -71.8], [915e6, 40.0, -73.2], [2437e6, 40.0, -70.9],
                           [915e6, 60.0, -93.1]], "ref_dbm": -50.0, "source": "sim"},
     "applied_via": "store", "t_ns": T0_NS - 3_600_000_000_000, "temperature_c": 39.0},
    {"device": "rx0", "kind": "image_rejection",
     "values": {"freqs_hz": [915e6, 2437e6], "irr_db": [41.2, 36.8], "quad_tracking": True},
     "t_ns": T0_NS},
    {"device": "df0", "kind": "coherence",
     "values": {"delay_samples": 0.37, "phase_deg": -47.2, "coherence": 0.993,
                "phase_drift_deg_per_s": 1.8, "coherent": False},
     "applied_via": "software", "t_ns": T0_NS, "temperature_c": None},
    {"device": "rx1", "kind": "iq", "values": {"gain_imbalance_db": 0.12, "phase_deg": 0.9},
     "applied_via": "hardware", "t_ns": T0_NS},
]


# --------------------------------------------------------------------------- df.bearing
def _wrap(a: float) -> float:
    w = (a + 180.0) % 360.0 - 180.0
    return 180.0 if w == -180.0 else w


def _interferometer(f_hz: float, d_m: float, az_true: float) -> tuple[float, list[float]]:
    """Measured phase [deg] and every azimuth consistent with it (two-element array)."""
    d_lam = d_m * f_hz / C_LIGHT
    phase = _wrap(360.0 * d_lam * math.sin(math.radians(az_true)))
    sols = []
    for k in range(-int(d_lam) - 1, int(d_lam) + 2):
        s = (phase / 360.0 + k) / d_lam
        if -1.0 <= s <= 1.0:
            sols.append(_r(math.degrees(math.asin(s))))
    return _r(phase), sorted(sols)


_ph915, _sol915 = _interferometer(915.2e6, 0.16, 23.4)
_ph2437, _sol2437 = _interferometer(2437e6, 0.16, 23.4)

DF_BEARING_EX: dict[str, Any] = {
    "array": "pluto2x_d0.16m", "method": "interferometer2", "f_hz": 915.2e6,
    "az_deg": _sol915[0], "ambiguous_deg": _sol915[1:], "phase_deg": _ph915,
    "coherence": 0.97, "quality": 0.93, "calibrated": True, "t_acq_ns": SPEC_T_ACQ,
}

DF_BEARING_VARIANTS: list[dict[str, Any]] = [
    {   # baseline > λ/2 at 2.437 GHz: grating-lobe ambiguity, no phase calibration yet
        "array": "pluto2x_d0.16m", "method": "interferometer2", "f_hz": 2437e6,
        "az_deg": _sol2437[0], "ambiguous_deg": _sol2437[1:], "phase_deg": _ph2437,
        "coherence": 0.81, "quality": 0.41, "calibrated": False, "t_acq_ns": SPEC_T_ACQ,
    },
    {   # phase 1: KrakenSDR 5-element UCA, MUSIC, full circle
        "array": "kraken_uca5_r0.25m", "method": "music", "f_hz": 433.92e6, "az_deg": -131.6,
        "ambiguous_deg": [], "coherence": 0.998, "quality": 0.88, "calibrated": True,
        "t_acq_ns": SPEC_T_ACQ,
    },
    {"array": "pluto2x_d0.16m", "method": "interferometer2", "f_hz": 868.3e6, "az_deg": 180.0,
     "calibrated": False},
]


# --------------------------------------------------------------------------- meas.result
MEAS_RESULT_EX: dict[str, Any] = {
    "spec_id": "sensitivity", "title": "Duyarlılık (MDS)", "value": -101.3, "unit": "dBm",
    "summary": "MDS = -101.3 dBm @ 915 MHz (RBW 1.96 kHz, SNR 10 dB, Pd 0.9)",
    "status": "ok", "passed": True, "target": -95.0, "target_op": "<=", "uncertainty": 1.0,
    "conditions": {"fc_hz": 915e6, "fs_hz": 4e6, "rbw_hz": _r(SPEC_RBW, 3), "gain_db": 70.0,
                   "nfft": 4096, "averages": 16, "window": "blackmanharris", "snr_db": 10.0,
                   "pd": 0.9, "pfa": 1e-6, "source": "sim", "temperature_c": TEMP_C},
    "extra": {"noise_floor_dbm": -111.4, "nf_db": 5.3,
              "pd_curve": {"p_dbm": [-106.0, -104.0, -102.0, -100.0], "pd": [0.12, 0.47, 0.86, 0.99]}},
    "t_start_ns": T0_NS + 60_000_000_000, "duration_s": 12.84, "device": "rx0",
}

MEAS_RESULT_VARIANTS: list[dict[str, Any]] = [
    {"spec_id": "df_rms", "title": "Yön bulma RMS hatası", "value": None, "unit": "°",
     "summary": "Atlandı: En az 2 RX kanalı gerekli (2× Pluto veya koherent çok kanallı cihaz)",
     "status": "skipped", "passed": None, "target": 5.0, "target_op": "<=", "uncertainty": None,
     "conditions": {}, "extra": {}, "t_start_ns": T0_NS, "duration_s": 0.0, "device": "rx0"},
    {"spec_id": "max_rate", "title": "Taşmasız azami örnekleme hızı", "value": 3.84e6, "unit": "S/s",
     "summary": "3.84 MS/s'de 10 s boyunca 0 overflow (5 MS/s'de 3 overflow)", "status": "ok",
     "passed": None, "target": None, "target_op": "", "uncertainty": None,
     "conditions": {"duration_s": 10.0, "buffer_size": 65536, "interface": "usb2"},
     "extra": {"tried": [[3.84e6, 0], [5e6, 3]]}, "t_start_ns": T0_NS, "duration_s": 74.2,
     "device": "rx0"},
    {"spec_id": "latency", "title": "Uçtan uca gecikme", "value": None, "unit": "ms",
     "summary": "Başarısız: StreamTimeout: örnek gelmedi", "status": "failed",
     "extra": {"traceback": "Traceback (most recent call last): ..."}},
    {"spec_id": "inst_bw", "title": "Anlık bant genişliği", "value": 16.0, "unit": "MHz",
     "status": "ok"},
]


# --------------------------------------------------------------------------- ctl.*
CTL_RX_EX: dict[str, Any] = {
    "req_id": "gui-7f3a-0001", "target": "rx0", "cmd": "set_center_freq", "args": {"hz": 2.437e9},
}

CTL_RX_VARIANTS: list[dict[str, Any]] = [
    {"req_id": "cli-1", "target": "*", "cmd": "ping"},
    {"req_id": "cli-2", "target": "rx0", "cmd": "set_gain", "args": {"db": 30.0, "channel": 0}},
    {"req_id": "cli-3", "target": "rx0", "cmd": "set_gain_mode", "args": {"mode": "manual"}},
    {"req_id": "cli-4", "target": "rx1", "cmd": "set_sample_rate", "args": {"hz": 3.84e6}},
    {"req_id": "cli-5", "target": "rx1", "cmd": "set_bandwidth", "args": {"hz": 4e6}},
    {"req_id": "cli-6", "target": "rx0", "cmd": "sweep",
     "args": {"start_hz": 325e6, "stop_hz": 3.8e9, "gain_db": 50.0}},
    {"req_id": "cli-7", "target": "*", "cmd": "stop", "args": {}},
]

CTL_ACK_EX: dict[str, Any] = {
    "req_id": "gui-7f3a-0001", "target": "rx0", "cmd": "set_center_freq", "ok": True,
    "error": None, "result": {"fc_hz": 2.437e9, "warnings": []},
}

CTL_ACK_VARIANTS: list[dict[str, Any]] = [
    {"req_id": "cli-1", "ok": True},
    {"req_id": "cli-4", "target": "rx1", "cmd": "set_sample_rate", "ok": False,
     "error": "Cihaz profili ihlali:\n  - örnekleme hızı 80 MS/s > profil üst sınırı 61.44 MS/s",
     "result": None},
    {"req_id": "cli-9", "target": "rx0", "cmd": "reboot", "ok": False,
     "error": "Bilinmeyen komut: 'reboot'"},
]


# --------------------------------------------------------------------------- registry
EXAMPLES: dict[str, dict[str, Any]] = {
    T.RX_STATUS: RX_STATUS_EX,
    T.RX_SPECTRUM: RX_SPECTRUM_EX,
    T.RX_DETECTIONS: RX_DETECTIONS_EX,
    T.RX_SWEEP: RX_SWEEP_EX,
    T.CAL_RESULT: CAL_RESULT_EX,
    T.DF_BEARING: DF_BEARING_EX,
    T.MEAS_RESULT: MEAS_RESULT_EX,
    T.CTL_RX: CTL_RX_EX,
    T.CTL_ACK: CTL_ACK_EX,
}

VARIANTS: dict[str, list[dict[str, Any]]] = {
    T.RX_STATUS: RX_STATUS_VARIANTS,
    T.RX_SPECTRUM: RX_SPECTRUM_VARIANTS,
    T.RX_DETECTIONS: RX_DETECTIONS_VARIANTS,
    T.RX_SWEEP: RX_SWEEP_VARIANTS,
    T.CAL_RESULT: CAL_RESULT_VARIANTS,
    T.DF_BEARING: DF_BEARING_VARIANTS,
    T.MEAS_RESULT: MEAS_RESULT_VARIANTS,
    T.CTL_RX: CTL_RX_VARIANTS,
    T.CTL_ACK: CTL_ACK_VARIANTS,
}


def example(topic: str) -> dict[str, Any]:
    """Deep copy of the canonical valid payload of ``topic``."""
    return copy.deepcopy(EXAMPLES[topic])


def variants(topic: str) -> list[dict[str, Any]]:
    """Deep copies of every valid payload of ``topic`` (canonical first)."""
    return [example(topic)] + [copy.deepcopy(v) for v in VARIANTS[topic]]


def all_examples() -> list[tuple[str, dict[str, Any]]]:
    """``(topic, payload)`` for every canonical example and variant."""
    return [(t, p) for t in EXAMPLES for p in variants(t)]


def example_envelope(topic: str, src: str = "rx0", seq: int = 0, t_ns: int | None = None) -> dict[str, Any]:
    """Complete envelope around the canonical payload (``t_ns`` defaults to a fixed time)."""
    return make_envelope(topic, src, seq, example(topic), t_ns=T0_NS + 1 if t_ns is None else t_ns)
