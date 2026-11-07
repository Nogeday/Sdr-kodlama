"""GUI model (no Qt): emitter tracking, BW classes, health view, latency."""

from __future__ import annotations

import math

from eds.gui.model import (
    BW_CLASSES,
    EmitterTracker,
    LatencyStats,
    SpectrumFrame,
    bw_class,
    fmt_level,
    health_view,
)

S = 1_000_000_000  # ns


def det(f, bw=10e3, peak=-50.0, snr=20.0):
    return {"f_center_hz": f, "f_peak_hz": f, "f_lo_hz": f - bw / 2, "f_hi_hz": f + bw / 2, "bw_hz": bw,
            "peak_db": peak, "band_power_db": peak - 1, "snr_db": snr, "noise_db": peak - snr, "n_bins": 5}


def frame(dets, unit="dBFS", rbw=1000.0):
    return {"device": "rx0", "unit": unit, "rbw_hz": rbw, "fc_hz": 434e6, "method": "os_cfar", "detections": dets}


def test_bw_classes_cover_all_bandwidths_in_order():
    assert bw_class(12.5e3).key == "dar"
    assert bw_class(200e3).key == "orta"
    assert bw_class(20e6).key == "genis"
    assert [c.key for c in BW_CLASSES] == ["dar", "orta", "genis"]
    assert len({c.color for c in BW_CLASSES}) == 3


def test_tracker_keeps_stable_ids_across_frames_and_small_drift():
    tr = EmitterTracker(match_hz_min=5e3)
    tr.update(frame([det(433.92e6, peak=-40), det(434.5e6, peak=-60)]), 1 * S)
    ids = {round(t.f_center_hz / 1e3): t.id for t in tr.tracks()}
    tr.update(frame([det(433.921e6, peak=-38), det(434.5005e6, peak=-62)]), 2 * S)   # 1 kHz / 0.5 kHz drift
    assert len(tr) == 2
    for t in tr.tracks():
        assert t.id == ids[round(round(t.f_center_hz / 1e3), -1)] or t.hits == 2
    a = min(tr.tracks(), key=lambda t: t.f_center_hz)
    assert a.hits == 2 and a.max_peak_db == -38 and a.first_seen_ns == 1 * S and a.last_seen_ns == 2 * S


def test_tracker_opens_new_track_for_distant_detection_and_prunes_old_ones():
    tr = EmitterTracker(match_hz_min=5e3, hold_s=10.0, stale_s=3.0)
    tr.update(frame([det(433.92e6)]), 1 * S)
    tr.update(frame([det(435.2e6)]), 2 * S)
    assert [t.id for t in tr.tracks()] == ["E001", "E002"]
    t1 = tr.get("E001")
    assert tr.is_stale(t1, 5 * S) and not tr.is_stale(tr.get("E002"), 4 * S)
    tr.update(frame([]), 12 * S)                       # E001 last seen at 1 s → older than hold_s
    assert [t.id for t in tr.tracks()] == ["E002"]


def test_tracker_assigns_each_track_at_most_once_per_frame():
    tr = EmitterTracker(match_hz_min=50e3)
    tr.update(frame([det(434.00e6)]), 1 * S)
    tr.update(frame([det(434.00e6, peak=-30), det(434.02e6, peak=-70)]), 2 * S)   # both within 50 kHz
    assert len(tr) == 2 and tr.get("E001").peak_db == -30                         # strongest kept the id


def test_uncalibrated_levels_are_marked():
    assert fmt_level(-40.04, "dBFS", calibrated=False) == "~-40.0 dBFS"
    assert fmt_level(-90.0, "dBm", calibrated=True) == "-90.0 dBm"
    assert fmt_level(float("nan"), "dBm", True) == "—"
    tr = EmitterTracker()
    tr.update(frame([det(434e6)], unit="dBm"), 1 * S)
    rows = dict(tr.tracks()[0].params(2 * S))
    assert rows["Kalibrasyon"].startswith("dBm") and rows["Modülasyon"].startswith("—")


def status(state="streaming", overflows=0, source="dma_status", drops=0):
    return {"device": {"name": "rx0", "backend": "sim"}, "state": state, "fc_hz": 434e6, "fs_hz": 4e6,
            "gain_db": [40.0], "gain_mode": "manual", "ppm": {"value": 11.7, "via": "hardware", "age_s": 3.0},
            "stream": {"blocks": 1, "samples": 1, "overflows": overflows, "timeouts": 0, "host_drops": drops,
                       "discarded_samples": 0, "throughput_sps": 3.9e6, "elapsed_s": 1.0,
                       "overflow_source": source},
            "sensors": {"temperature_c": 41.5}, "warnings": []}


def test_health_view_states_and_overflow_alarm():
    assert health_view(None, None, 0).level == "none"
    ok = health_view(status(), 10 * S, 10 * S + S // 2)
    assert ok.connected and ok.level == "ok" and not ok.overflow_alarm and ok.throughput_text == "3.90 MS/s"
    assert ok.ppm_text.startswith("+11.70 ppm") and ok.temperature_text == "41.5 °C"
    bad = health_view(status(overflows=3), 10 * S, 10 * S)
    assert bad.overflow_alarm and bad.level == "warn" and bad.overflow_text == "3"
    unknown = health_view(status(overflows=0, source="none"), 10 * S, 10 * S)
    assert not unknown.overflow_alarm and "bilinmiyor" in unknown.overflow_text
    stale = health_view(status(), 10 * S, 20 * S)
    assert not stale.connected and stale.level == "bad"
    assert health_view(status(state="error"), 10 * S, 10 * S).level == "bad"


def test_latency_stats_and_spectrum_frame():
    ls = LatencyStats()
    assert math.isnan(ls.median())
    for ms in (10, 20, 30, 40, 100):
        ls.add(1_000 * S + ms * 1_000_000, 1_000 * S)
    assert ls.median() == 30 and ls.p95() == 100
    f = SpectrumFrame.from_data({"f_start_hz": 432e6, "f_step_hz": 2e3, "values": [-80.0] * 2000, "unit": "dBFS",
                                 "calibrated": False, "ppm_corrected": True, "fc_hz": 434e6, "fs_hz": 4e6,
                                 "rbw_hz": 1957.0, "noise_floor": -80.0, "gain_db": [40.0], "decimation": 2,
                                 "t_acq_end_ns": 5, "overflow": False})
    assert f.n == 2000 and "kalibrasyonsuz" in f.cal_mode and f.geometry_key() == (432e6, 2e3, 2000)


def test_overlapping_detections_of_one_frame_become_one_emitter():
    from eds.gui.model import merge_overlapping

    a = det(434.081e6, bw=58.6e3, peak=-77.4)
    b = det(434.092e6, bw=60.5e3, peak=-76.8)                 # same image signal, reported twice
    carrier = det(432.95e6, bw=2e3, peak=-40.0)
    wide = det(432.95e6, bw=1.5e6, peak=-72.0)                 # carrier inside wideband: keep both
    merged = merge_overlapping([a, b, carrier, wide])
    assert len(merged) == 3
    m = [d for d in merged if abs(d["f_center_hz"] - 434.0865e6) < 10e3][0]
    assert m["peak_db"] == -76.8 and m["f_lo_hz"] == a["f_lo_hz"] and m["f_hi_hz"] == b["f_hi_hz"]
    tr = EmitterTracker()
    tr.update(frame([a, b]), 1 * S)
    assert len(tr) == 1


def test_mirror_of_a_strong_emitter_is_flagged_as_possible_iq_image():
    tr = EmitterTracker()
    fc = 434.0e6
    strong = det(433.9158e6, bw=60e3, peak=-23.6)
    ghost = det(434.0842e6, bw=60e3, peak=-78.3)       # 2·fc − f, 55 dB weaker
    real = det(434.4991e6, bw=126e3, peak=-46.0)       # not at any mirror
    data = frame([strong, ghost, real])
    data["fc_hz"] = fc
    tr.update(data, 1 * S)
    by_f = {round(t.f_center_hz / 1e3): t for t in tr.tracks()}
    g, s_, r = by_f[434084], by_f[433916], by_f[434499]
    assert g.image_of == s_.id and round(g.image_delta_db) == 55
    assert s_.image_of is None and r.image_of is None
    assert dict(g.params(2 * S))["IQ imajı şüphesi"].startswith("evet")
    # a mirror that is not much weaker is NOT flagged (could be a real emitter)
    tr2 = EmitterTracker()
    d2 = frame([det(433.9158e6, peak=-40.0), det(434.0842e6, peak=-50.0)])
    d2["fc_hz"] = fc
    tr2.update(d2, 1 * S)
    assert all(t.image_of is None for t in tr2.tracks())


def test_wideband_signal_near_a_mirror_is_not_called_an_image():
    """Regression: a 1.3 MHz video band overlapping the mirror of a 7 kHz carrier is real."""
    tr = EmitterTracker()
    fc = 434.0e6
    carrier = det(435.2449e6, bw=6.8e3, peak=-21.3)          # mirror at 432.755 MHz
    video = det(433.0665e6, bw=1.3e6, peak=-51.9)             # spans 432.42–433.72 MHz
    data = frame([carrier, video])
    data["fc_hz"] = fc
    tr.update(data, 1 * S)
    assert all(t.image_of is None for t in tr.tracks())
