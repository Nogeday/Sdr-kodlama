"""JSON Schema validation: every topic accepts the shared examples and rejects contract
violations (missing/extra fields, wrong types, enums, physical ranges, envelope errors)."""

from __future__ import annotations

import copy
import importlib.util
import json
import subprocess
import sys
from collections.abc import Callable
from typing import Any

import numpy as np
import pytest

from eds.bus import topics as T
from eds.bus.envelope import BusError, decode, encode, make_envelope
from eds.core.errors import BackendUnavailable, EDSError
from tests.bus import examples as E

jsonschema = pytest.importorskip("jsonschema")
from eds.bus import validate as V  # noqa: E402  (needs jsonschema)


def _data_schema(topic: str) -> dict[str, Any]:
    return json.loads(T.schema_path(topic).read_text(encoding="utf-8"))["properties"]["data"]


def _valid(topic: str, data: dict[str, Any]) -> None:
    V.validate_data(topic, data)


def _invalid(topic: str, data: dict[str, Any]) -> V.MessageValidationError:
    with pytest.raises(V.MessageValidationError) as ei:
        V.validate_data(topic, data)
    return ei.value


# --------------------------------------------------------------------------- positives
@pytest.mark.parametrize("topic,payload", E.all_examples(),
                         ids=[f"{t}-{i}" for i, (t, _) in enumerate(E.all_examples())])
def test_examples_are_valid(topic, payload):
    _valid(topic, payload)
    env = make_envelope(topic, "rx0", 3, payload)
    V.validate_message(topic, env)
    assert V.is_valid(topic, env)
    # the wire round trip keeps it valid and identical
    back = decode(encode(env))
    assert back == env
    V.validate_message(topic, back)


def test_every_topic_has_examples():
    assert set(E.EXAMPLES) == set(T.ALL_TOPICS) == set(E.VARIANTS)
    for t in T.ALL_TOPICS:
        assert len(E.variants(t)) >= 2


def test_example_returns_private_copy():
    a = E.example(T.RX_SPECTRUM)
    a["values"][0] = 999.0
    assert E.example(T.RX_SPECTRUM)["values"][0] != 999.0


def test_example_envelope():
    env = E.example_envelope(T.RX_STATUS, src="rx1", seq=5)
    assert env["topic"] == T.RX_STATUS and env["src"] == "rx1" and env["seq"] == 5
    V.validate_message(T.RX_STATUS, env)


def test_spectrum_example_is_physically_consistent():
    s = E.example(T.RX_SPECTRUM)
    assert len(s["values"]) == -(-s["n_bins"] // s["decimation"])
    assert s["f_step_hz"] == pytest.approx(s["decimation"] * s["fs_hz"] / s["nfft"])
    assert s["rbw_hz"] == pytest.approx(E.ENBW_BLACKMANHARRIS * s["fs_hz"] / s["nfft"], abs=1e-3)
    assert s["f_start_hz"] == s["fc_hz"] - s["fs_hz"] / 2
    assert s["t_acq_ns"] < s["t_acq_end_ns"] < s["t_proc_ns"]
    k = int((E.TONE_HZ - s["f_start_hz"]) // s["f_step_hz"])
    assert max(s["values"]) == s["values"][k] == E.TONE_DBFS
    assert np.median(s["values"]) == pytest.approx(s["noise_floor"] + 1.0, abs=1.0)


def test_sweep_example_is_physically_consistent():
    s = E.example(T.RX_SWEEP)
    span = s["f_stop_hz"] - s["f_start_hz"]
    assert s["rate_hz_per_s"] == pytest.approx(span / s["duration_s"], rel=1e-6)
    assert (s["t_end_ns"] - s["t_start_ns"]) / 1e9 == pytest.approx(s["duration_s"], abs=1e-6)
    assert sum(s["timing"].values()) <= s["duration_s"]
    assert s["n_steps"] * s["step_hz"] >= span
    n_bins = round(span / (20e6 / 2048))
    assert len(s["values"]) == -(-n_bins // s["decimation"]) and len(s["values"]) <= 8192
    last = s["f_point_start_hz"] + (len(s["values"]) - 1) * s["f_point_step_hz"]
    assert s["f_start_hz"] <= last <= s["f_stop_hz"]


def test_df_example_matches_interferometer_geometry():
    d = E.example(T.DF_BEARING)
    lam = E.C_LIGHT / d["f_hz"]
    phase = 360.0 * 0.16 / lam * np.sin(np.radians(d["az_deg"]))
    assert phase == pytest.approx(d["phase_deg"], abs=0.02)
    assert d["ambiguous_deg"] == []                      # d < λ/2 at 915 MHz
    amb = E.DF_BEARING_VARIANTS[0]
    assert len(amb["ambiguous_deg"]) == 1                # d ≈ 1.3 λ at 2.437 GHz


def test_ppm_example_follows_sign_convention():
    c = E.example(T.CAL_RESULT)["values"]
    # fast TCXO (ppm > 0) -> apparent frequency low; f_true = f_app * (1 + e)
    assert c["ppm"] > 0 and c["f_measured_hz"] < c["f_ref_hz"]
    assert c["f_measured_hz"] * (1 + c["ppm"] * 1e-6) == pytest.approx(c["f_ref_hz"], abs=0.01)


# --------------------------------------------------------------------------- required fields
def _required_cases() -> list[tuple[str, str]]:
    return [(t, k) for t in T.ALL_TOPICS for k in _data_schema(t)["required"]]


@pytest.mark.parametrize("topic,key", _required_cases(), ids=[f"{t}-{k}" for t, k in _required_cases()])
def test_missing_required_field_rejected(topic, key):
    data = E.example(topic)
    del data[key]
    err = _invalid(topic, data)
    assert err.validator == "required"
    assert key in str(err)


@pytest.mark.parametrize("topic", T.ALL_TOPICS)
def test_unknown_payload_field_rejected(topic):
    data = E.example(topic)
    data["unexpected_field"] = 1
    err = _invalid(topic, data)
    assert err.validator == "additionalProperties"


@pytest.mark.parametrize("topic", T.ALL_TOPICS)
def test_optional_fields_may_be_omitted(topic):
    sch = _data_schema(topic)
    data = {k: v for k, v in E.example(topic).items() if k in sch["required"]}
    if topic == T.CTL_ACK:
        data["ok"] = True
    _valid(topic, data)


# --------------------------------------------------------------------------- targeted negatives
def _set(path: str, value: Any) -> Callable[[dict], None]:
    def f(d: dict) -> None:
        *head, last = path.split(".")
        node: Any = d
        for p in head:
            node = node[int(p)] if p.isdigit() else node[p]
        if last.isdigit():
            node[int(last)] = value
        else:
            node[last] = value
    return f


def _del(path: str) -> Callable[[dict], None]:
    def f(d: dict) -> None:
        *head, last = path.split(".")
        node: Any = d
        for p in head:
            node = node[int(p)] if p.isdigit() else node[p]
        del node[last]
    return f


NEGATIVE: list[tuple[str, str, Callable[[dict], None], str]] = [
    # rx.status
    (T.RX_STATUS, "device.backend eksik", _del("device.backend"), "required"),
    (T.RX_STATUS, "device fazla alan", _set("device.hardware", "pluto"), "additionalProperties"),
    (T.RX_STATUS, "device.name boş", _set("device.name", ""), "minLength"),
    (T.RX_STATUS, "state enum", _set("state", "running"), "enum"),
    (T.RX_STATUS, "fc_hz metin", _set("fc_hz", "915e6"), "type"),
    (T.RX_STATUS, "fs_hz negatif", _set("fs_hz", -1.0), "minimum"),
    (T.RX_STATUS, "gain_db boş", _set("gain_db", []), "minItems"),
    (T.RX_STATUS, "gain_db eleman metin", _set("gain_db", ["40"]), "type"),
    (T.RX_STATUS, "gain_db dizi değil", _set("gain_db", 40.0), "type"),
    (T.RX_STATUS, "ppm.via enum", _set("ppm.via", "gps"), "enum"),
    (T.RX_STATUS, "ppm.age_s negatif", _set("ppm.age_s", -1.0), "minimum"),
    (T.RX_STATUS, "ppm.value metin", _set("ppm.value", "12"), "type"),
    (T.RX_STATUS, "stream.overflows eksik", _del("stream.overflows"), "required"),
    (T.RX_STATUS, "stream.overflows negatif", _set("stream.overflows", -1), "minimum"),
    (T.RX_STATUS, "stream.blocks kesirli", _set("stream.blocks", 1.5), "type"),
    (T.RX_STATUS, "stream fazla alan", _set("stream.start_mono", 1.0), "additionalProperties"),
    (T.RX_STATUS, "clip_fraction > 1", _set("levels.clip_fraction", 1.5), "maximum"),
    (T.RX_STATUS, "sensors dizi değeri", _set("sensors.temperature_c", [41.5]), "type"),
    (T.RX_STATUS, "sensors bool değeri", _set("sensors.ok", True), "type"),
    (T.RX_STATUS, "warnings eleman sayı", _set("warnings", [1]), "type"),
    # rx.spectrum
    (T.RX_SPECTRUM, "values boş", _set("values", []), "minItems"),
    (T.RX_SPECTRUM, "values içinde null (NaN)", _set("values.3", None), "type"),
    (T.RX_SPECTRUM, "decimation 0", _set("decimation", 0), "minimum"),
    (T.RX_SPECTRUM, "nfft 0", _set("nfft", 0), "minimum"),
    (T.RX_SPECTRUM, "averages kesirli", _set("averages", 2.5), "type"),
    (T.RX_SPECTRUM, "unit enum", _set("unit", "dBW"), "enum"),
    (T.RX_SPECTRUM, "gain-referred birim spektrumda yok", _set("unit", "dBFS@40dB"), "enum"),
    (T.RX_SPECTRUM, "calibrated metin", _set("calibrated", "yes"), "type"),
    (T.RX_SPECTRUM, "overflow int", _set("overflow", 0), "type"),
    (T.RX_SPECTRUM, "f_step_hz 0", _set("f_step_hz", 0.0), "exclusiveMinimum"),
    (T.RX_SPECTRUM, "rbw_hz 0", _set("rbw_hz", 0.0), "exclusiveMinimum"),
    (T.RX_SPECTRUM, "fs_hz 0", _set("fs_hz", 0.0), "exclusiveMinimum"),
    (T.RX_SPECTRUM, "block_seq negatif", _set("block_seq", -1), "minimum"),
    (T.RX_SPECTRUM, "t_acq_ns metin", _set("t_acq_ns", "1793871000500000000"), "type"),
    (T.RX_SPECTRUM, "device boş", _set("device", ""), "minLength"),
    (T.RX_SPECTRUM, "noise_floor null", _set("noise_floor", None), "type"),
    # rx.detections
    (T.RX_DETECTIONS, "tespit n_bins eksik", _del("detections.0.n_bins"), "required"),
    (T.RX_DETECTIONS, "tespit fazla alan", _set("detections.0.bin", 12), "additionalProperties"),
    (T.RX_DETECTIONS, "tespit n_bins 0", _set("detections.1.n_bins", 0), "minimum"),
    (T.RX_DETECTIONS, "tespit bw negatif", _set("detections.0.bw_hz", -1.0), "minimum"),
    (T.RX_DETECTIONS, "tespit snr null", _set("detections.0.snr_db", None), "type"),
    (T.RX_DETECTIONS, "pfa 0", _set("pfa", 0.0), "exclusiveMinimum"),
    (T.RX_DETECTIONS, "pfa > 1", _set("pfa", 1.5), "maximum"),
    (T.RX_DETECTIONS, "method boş", _set("method", ""), "minLength"),
    (T.RX_DETECTIONS, "detections nesne", _set("detections", {}), "type"),
    # rx.sweep
    (T.RX_SWEEP, "timing.dsp_s eksik", _del("timing.dsp_s"), "required"),
    (T.RX_SWEEP, "timing fazla alan", _set("timing.agc_s", 0.1), "additionalProperties"),
    (T.RX_SWEEP, "timing.tune_s negatif", _set("timing.tune_s", -0.1), "minimum"),
    (T.RX_SWEEP, "n_steps 0", _set("n_steps", 0), "minimum"),
    (T.RX_SWEEP, "decimation 0", _set("decimation", 0), "minimum"),
    (T.RX_SWEEP, "f_point_step_hz 0", _set("f_point_step_hz", 0.0), "exclusiveMinimum"),
    (T.RX_SWEEP, "tespit eksik alan", _del("detections.0.peak_db"), "required"),
    (T.RX_SWEEP, "values boş", _set("values", []), "minItems"),
    (T.RX_SWEEP, "rate negatif", _set("rate_hz_per_s", -1.0), "minimum"),
    (T.RX_SWEEP, "sweep_id kesirli", _set("sweep_id", 1.5), "type"),
    # cal.result
    (T.CAL_RESULT, "kind enum", _set("kind", "temperature"), "enum"),
    (T.CAL_RESULT, "values dizi", _set("values", [12.37]), "type"),
    (T.CAL_RESULT, "temperature metin", _set("temperature_c", "41.5"), "type"),
    (T.CAL_RESULT, "t_ns negatif", _set("t_ns", -1), "minimum"),
    (T.CAL_RESULT, "device boş", _set("device", ""), "minLength"),
    # df.bearing
    (T.DF_BEARING, "az > 180", _set("az_deg", 181.0), "maximum"),
    (T.DF_BEARING, "az < -180", _set("az_deg", -181.0), "minimum"),
    (T.DF_BEARING, "belirsiz açı aralık dışı", _set("ambiguous_deg", [200.0]), "maximum"),
    (T.DF_BEARING, "phase 270", _set("phase_deg", 270.0), "maximum"),
    (T.DF_BEARING, "coherence > 1", _set("coherence", 1.2), "maximum"),
    (T.DF_BEARING, "quality < 0", _set("quality", -0.1), "minimum"),
    (T.DF_BEARING, "f_hz 0", _set("f_hz", 0.0), "exclusiveMinimum"),
    (T.DF_BEARING, "calibrated metin", _set("calibrated", "true"), "type"),
    (T.DF_BEARING, "array boş", _set("array", ""), "minLength"),
    # meas.result
    (T.MEAS_RESULT, "spec_id enum", _set("spec_id", "noise_figure"), "enum"),
    (T.MEAS_RESULT, "status enum", _set("status", "done"), "enum"),
    (T.MEAS_RESULT, "passed metin", _set("passed", "yes"), "type"),
    (T.MEAS_RESULT, "target_op enum", _set("target_op", "<"), "enum"),
    (T.MEAS_RESULT, "uncertainty negatif", _set("uncertainty", -1.0), "minimum"),
    (T.MEAS_RESULT, "value metin", _set("value", "-101.3"), "type"),
    (T.MEAS_RESULT, "title boş", _set("title", ""), "minLength"),
    (T.MEAS_RESULT, "conditions dizi", _set("conditions", []), "type"),
    (T.MEAS_RESULT, "duration negatif", _set("duration_s", -0.5), "minimum"),
    # ctl.rx
    (T.CTL_RX, "cmd enum", _set("cmd", "reboot"), "enum"),
    (T.CTL_RX, "req_id boş", _set("req_id", ""), "minLength"),
    (T.CTL_RX, "target boş", _set("target", ""), "minLength"),
    (T.CTL_RX, "args dizi", _set("args", [2.437e9]), "type"),
    (T.CTL_RX, "req_id sayı", _set("req_id", 17), "type"),
    # ctl.ack
    (T.CTL_ACK, "ok metin", _set("ok", "true"), "type"),
    (T.CTL_ACK, "result dizi", _set("result", []), "type"),
    (T.CTL_ACK, "error sayı", _set("error", 3), "type"),
]


@pytest.mark.parametrize("topic,desc,mutate,keyword", NEGATIVE,
                         ids=[f"{t}-{d}" for t, d, _, _ in NEGATIVE])
def test_contract_violation_rejected(topic, desc, mutate, keyword):
    data = E.example(topic)
    mutate(data)
    err = _invalid(topic, data)
    assert err.validator == keyword, f"{desc}: {err}"
    assert isinstance(err, EDSError) and isinstance(err, jsonschema.ValidationError)


@pytest.mark.parametrize("unit", ["dBm", "dBFS", "dBFS@50dB", "dBFS@50.0dB", "dBFS@-1.5dB", "dBFS@+3dB",
                                  "dBFS@0dB", "dBFS@73dB"])
def test_sweep_unit_pattern_accepts(unit):
    data = E.example(T.RX_SWEEP)
    data["unit"] = unit
    _valid(T.RX_SWEEP, data)


@pytest.mark.parametrize("unit", ["", "dbfs", "dBW", "dBFS@dB", "dBm@50dB", "dBFS@50", "dBFS@50 dB",
                                  "dBFS@5O dB", "dBFS @50dB", "dBFS@50dBm", "dBFS@1e2dB", "dBFS@.5dB"])
def test_sweep_unit_pattern_rejects(unit):
    data = E.example(T.RX_SWEEP)
    data["unit"] = unit
    assert _invalid(T.RX_SWEEP, data).validator == "pattern"


def test_failed_ack_requires_error_text():
    base = {"req_id": "r1", "target": "rx0", "cmd": "set_gain", "ok": False}
    _invalid(T.CTL_ACK, base)                                    # no error at all
    _invalid(T.CTL_ACK, {**base, "error": None})
    _invalid(T.CTL_ACK, {**base, "error": ""})
    _valid(T.CTL_ACK, {**base, "error": "Kazanç aralık dışı: 80 dB > 71 dB"})
    _valid(T.CTL_ACK, {"req_id": "r1", "ok": True, "error": None, "result": None})
    _valid(T.CTL_ACK, {"req_id": "", "ok": False, "cmd": "", "error": "Bozuk komut: req_id yok"})


def test_ctl_rx_accepts_every_command():
    for cmd in ("ping", "set_center_freq", "set_gain", "set_gain_mode", "set_sample_rate",
                "set_bandwidth", "sweep", "stop"):
        _valid(T.CTL_RX, {"req_id": "x", "target": "*", "cmd": cmd, "args": {}})


def test_nullable_fields_accept_null():
    st = E.example(T.RX_STATUS)
    st["ppm"] = {"value": None, "via": "none", "age_s": None}
    st["levels"] = {"peak_dbfs": None, "rms_dbfs": None, "clip_fraction": None}
    st["sensors"] = {"temperature_c": None, "fw": "v0.38", "rssi_db": -60}
    _valid(T.RX_STATUS, st)
    m = E.example(T.MEAS_RESULT)
    m.update(value=None, passed=None, target=None, uncertainty=None)
    _valid(T.MEAS_RESULT, m)
    c = E.example(T.CAL_RESULT)
    c["temperature_c"] = None
    _valid(T.CAL_RESULT, c)


def test_integers_accepted_where_numbers_expected():
    d = E.example(T.RX_SPECTRUM)
    d.update(fc_hz=915_000_000, fs_hz=4_000_000, values=[-95, -94, -40])
    _valid(T.RX_SPECTRUM, d)


# --------------------------------------------------------------------------- envelope layer
ENVELOPE_NEGATIVE: list[tuple[str, Callable[[dict], None], str]] = [
    ("v 2", _set("v", 2), "const"),
    ("v metin", _set("v", "1"), "type"),
    ("seq negatif", _set("seq", -1), "minimum"),
    ("seq kesirli", _set("seq", 1.5), "type"),
    ("seq bool", _set("seq", True), "type"),
    ("src boş", _set("src", ""), "minLength"),
    ("src çok uzun", _set("src", "x" * 65), "maxLength"),
    ("fazla zarf alanı", _set("meta", {}), "additionalProperties"),
    ("t_ns eksik", _del("t_ns"), "required"),
    ("t_ns negatif", _set("t_ns", -5), "minimum"),
    ("t_ns metin", _set("t_ns", "now"), "type"),
    ("data dizi", _set("data", []), "type"),
    ("topic büyük harf", _set("topic", "RX.STATUS"), "const"),
]


@pytest.mark.parametrize("desc,mutate,keyword", ENVELOPE_NEGATIVE, ids=[d for d, _, _ in ENVELOPE_NEGATIVE])
def test_envelope_violation_rejected(desc, mutate, keyword):
    env = E.example_envelope(T.RX_STATUS)
    mutate(env)
    with pytest.raises(V.MessageValidationError) as ei:
        V.validate_message(T.RX_STATUS, env)
    assert ei.value.validator == keyword, f"{desc}: {ei.value}"


def test_envelope_only_validation():
    env = make_envelope("future.topic", "gui", 0, {"anything": [1, 2]})
    V.validate_envelope(env)
    env["v"] = 2
    with pytest.raises(V.MessageValidationError):
        V.validate_envelope(env)
    with pytest.raises(V.MessageValidationError):
        V.validate_envelope({**make_envelope("noDots", "gui", 0, {})})


@pytest.mark.parametrize("topic", T.ALL_TOPICS)
def test_wrong_envelope_topic_rejected(topic):
    other = T.CTL_ACK if topic != T.CTL_ACK else T.CTL_RX
    env = make_envelope(other, "rx0", 0, E.example(topic))       # topic field says 'other'
    with pytest.raises(V.MessageValidationError) as ei:
        V.validate_message(topic, env)
    assert ei.value.validator == "const"
    assert ei.value.location == "topic"


def test_payload_of_other_topic_rejected():
    env = make_envelope(T.RX_STATUS, "rx0", 0, E.example(T.RX_SPECTRUM))
    with pytest.raises(V.MessageValidationError):
        V.validate_message(T.RX_STATUS, env)


def test_big_int_timestamps_valid_and_preserved():
    big = 2**64 + 12345                                           # far beyond 2**53
    data = E.example(T.RX_SPECTRUM)
    data.update(t_acq_ns=big, t_acq_end_ns=big + 8_704_000, t_proc_ns=big + 10_854_001)
    env = make_envelope(T.RX_SPECTRUM, "rx0", 2**60, data, t_ns=big + 11_000_003)
    V.validate_message(T.RX_SPECTRUM, env)
    back = decode(encode(env))
    assert back["t_ns"] == big + 11_000_003 and type(back["t_ns"]) is int
    assert back["data"]["t_proc_ns"] == big + 10_854_001
    assert back["seq"] == 2**60
    assert float(big) != big                                       # a float64 would have lost it


def test_unknown_topic():
    with pytest.raises(T.UnknownTopic):
        V.validate_data("rx.unknown", {})
    with pytest.raises(T.UnknownTopic):
        V.is_valid("rx.unknown", {})


# --------------------------------------------------------------------------- error object
def test_validation_error_is_concise_and_dual_typed():
    data = E.example(T.RX_SPECTRUM)
    data["values"][3] = "abc"
    err = _invalid(T.RX_SPECTRUM, data)
    assert isinstance(err, BusError) and isinstance(err, EDSError)
    assert isinstance(err, jsonschema.ValidationError)
    s = str(err)
    assert "data.values[3]" in s and "rx.spectrum" in s and "şemaya uymuyor" in s
    assert len(s) < 400                                          # no 1024-value dump
    assert err.location == "data.values[3]"
    assert list(err.absolute_path) == ["data", "values", 3]
    assert err.n_errors == 1 and err.topic == T.RX_SPECTRUM
    # catching by the jsonschema type (as documented in ARCHITECTURE §3.7) works too
    with pytest.raises(jsonschema.ValidationError):
        V.validate_data(T.RX_SPECTRUM, data)


def test_validation_error_counts_all_errors():
    data = E.example(T.RX_STATUS)
    data["state"] = "bogus"
    data["fs_hz"] = -1
    del data["stream"]
    err = _invalid(T.RX_STATUS, data)
    assert err.n_errors == 3


def test_long_reason_is_truncated():
    data = E.example(T.RX_SPECTRUM)
    data["values"] = {"k" * 10: "v" * 5000}
    err = _invalid(T.RX_SPECTRUM, data)
    assert len(str(err)) < 400 and "..." in str(err)


# --------------------------------------------------------------------------- integration with producers
def test_measurement_result_bus_payload_validates():
    from eds.measure.base import SPEC_IDS, MeasurementResult

    assert set(_data_schema(T.MEAS_RESULT)["properties"]["spec_id"]["enum"]) == set(SPEC_IDS)
    r = MeasurementResult(
        spec_id="sensitivity", title="Duyarlılık (MDS)", value=np.float64(-101.3), unit="dBm",
        summary="MDS = -101.3 dBm", method="Pd eğrisi", uncertainty=np.float64(np.nan),
        conditions={"fc_hz": np.float64(915e6), "nfft": np.int64(4096), "gains": np.array([70.0]),
                    "ok": np.bool_(True), "nf": float("inf")},
        extra={"pd": np.array([0.1, 0.5, np.nan])},
        device={"name": "rx0", "backend": "sim"}, t_start_ns=E.T0_NS, duration_s=12.5,
    )
    r.evaluate_target(-95.0, "<=")
    payload = r.bus_payload()
    _valid(T.MEAS_RESULT, payload)
    assert payload["passed"] is True and payload["uncertainty"] is None
    env = make_envelope(T.MEAS_RESULT, "measure", 0, payload)
    assert env["data"]["extra"]["pd"] == [0.1, 0.5, None]
    V.validate_message(T.MEAS_RESULT, decode(encode(env)))

    for sid in SPEC_IDS:
        skipped = MeasurementResult(spec_id=sid, title="X", value=None, unit="", status="skipped",
                                    error="kaynak yok", summary="Atlandı: kaynak yok")
        _valid(T.MEAS_RESULT, skipped.bus_payload())
    failed = MeasurementResult(spec_id="latency", title="Gecikme", value=None, unit="ms", status="failed",
                               extra={"traceback": "Traceback ..."})
    failed.evaluate_target(100.0, ">=")
    _valid(T.MEAS_RESULT, failed.bus_payload())


def test_dsp_detection_to_dict_validates():
    detect = pytest.importorskip("eds.dsp.detect")
    det = detect.Detection(
        f_center_hz=np.float64(915.2e6), f_peak_hz=np.float64(915.20004e6),
        f_lo_hz=np.float64(915.1966e6), f_hi_hz=np.float64(915.2034e6), bw_hz=np.float64(6835.9),
        peak_db=np.float64(-42.3), band_power_db=np.float64(-42.27), snr_db=np.float64(52.8),
        noise_db=np.float64(-95.1), n_bins=np.int64(7),
    )
    data = E.example(T.RX_DETECTIONS)
    data["detections"] = [det.to_dict()]
    _valid(T.RX_DETECTIONS, data)
    sweep = E.example(T.RX_SWEEP)
    sweep["detections"] = [det.to_dict(), det.to_dict()]
    _valid(T.RX_SWEEP, sweep)


def test_status_schema_is_satisfiable_from_core_types():
    from dataclasses import fields

    from eds.core.types import DeviceInfo, StreamStats

    props = _data_schema(T.RX_STATUS)["properties"]
    assert set(props["stream"]["required"]) <= set(StreamStats().to_dict())
    assert set(props["device"]["properties"]) <= {f.name for f in fields(DeviceInfo)}
    stats = StreamStats(blocks=3, samples=3 * 65536, start_mono=1.0, last_mono=1.05)
    st = E.example(T.RX_STATUS)
    st["stream"] = {k: v for k, v in stats.to_dict().items() if k in props["stream"]["required"]}
    _valid(T.RX_STATUS, st)


# --------------------------------------------------------------------------- backend handling
def test_cached_validator_reused():
    assert V.get_validator(T.RX_STATUS) is V.get_validator(T.RX_STATUS)
    V.clear_cache()
    v1 = V.get_validator(T.RX_STATUS)
    assert v1 is V.get_validator(T.RX_STATUS)


def test_missing_jsonschema_raises_backend_unavailable(monkeypatch):
    """Fresh copy of eds.bus.validate imported while jsonschema/referencing are absent."""
    for name in list(sys.modules):
        if name == "jsonschema" or name.startswith(("jsonschema.", "referencing")):
            monkeypatch.delitem(sys.modules, name)
    monkeypatch.setitem(sys.modules, "jsonschema", None)
    monkeypatch.setitem(sys.modules, "referencing", None)
    spec = importlib.util.spec_from_file_location("eds.bus._validate_nojs", V.__file__)
    mod = importlib.util.module_from_spec(spec)
    monkeypatch.setitem(sys.modules, "eds.bus._validate_nojs", mod)
    spec.loader.exec_module(mod)
    for call in (lambda: mod.validate_data(T.RX_STATUS, E.example(T.RX_STATUS)),
                 lambda: mod.validate_message(T.RX_STATUS, E.example_envelope(T.RX_STATUS)),
                 mod.check_schemas, mod.require_backend):
        with pytest.raises(BackendUnavailable, match="pip install jsonschema"):
            call()
    assert issubclass(mod.MessageValidationError, BusError)

    import eds.bus
    from eds.bus.pubsub import Publisher

    monkeypatch.setitem(sys.modules, "eds.bus.validate", mod)
    monkeypatch.setattr(eds.bus, "validate", mod)
    with pytest.raises(BackendUnavailable):
        Publisher("inproc://nojs-validate", validate=True)


def test_import_eds_bus_does_not_import_jsonschema():
    code = ("import sys, eds.bus; assert 'jsonschema' not in sys.modules; "
            "import eds.bus as b; b.validate_message; assert 'jsonschema' in sys.modules")
    subprocess.run([sys.executable, "-c", code], check=True, timeout=30)


def test_examples_untouched_by_validation():
    before = copy.deepcopy(E.EXAMPLES)
    for t, p in E.EXAMPLES.items():
        V.validate_data(t, p)
    assert E.EXAMPLES == before
