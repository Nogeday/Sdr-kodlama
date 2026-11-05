"""KTR targets: TOML loading/validation and pass/fail evaluation."""

from __future__ import annotations

import math
from pathlib import Path

import pytest

from eds.core.errors import ConfigError
from eds.measure.base import SPEC_IDS, MeasurementResult
from eds.measure.targets import (
    TARGET_NOTE_KEY,
    Target,
    apply_targets,
    load_targets,
    parse_targets,
    resolve_targets_path,
)

GOOD = """
[sensitivity]
target = -100.0
op = "<="
note = "Pd = 0.9 @ Pfa = 1e-6"

[dynamic_range]
target = 60
op = ">="

[max_rate]
target = 4.0
op = ">="
note = "USB 2.0, sıfır overflow"
"""


def _write(tmp_path: Path, text: str, name: str = "t.toml") -> Path:
    p = tmp_path / name
    p.write_text(text, encoding="utf-8")
    return p


def _res(spec_id: str, value: float | None, status: str = "ok", unit: str | None = None) -> MeasurementResult:
    from eds.measure import specs  # noqa: F401  (populate registry)
    from eds.measure.base import MEASUREMENTS

    return MeasurementResult(spec_id=spec_id, title=spec_id, value=value,
                             unit=unit if unit is not None else MEASUREMENTS[spec_id].unit, status=status)


def test_load_targets_parses_values_ops_and_notes(tmp_path):
    t = load_targets(_write(tmp_path, GOOD))
    assert set(t) == {"sensitivity", "dynamic_range", "max_rate"}
    assert t["sensitivity"] == Target(-100.0, "<=", "Pd = 0.9 @ Pfa = 1e-6")
    assert t["dynamic_range"].target == 60.0 and isinstance(t["dynamic_range"].target, float)
    assert t["dynamic_range"].note == ""
    assert t["max_rate"].note == "USB 2.0, sıfır overflow"


def test_relative_path_resolves_against_config_dir(tmp_path, monkeypatch):
    monkeypatch.setenv("EDS_CONFIG_DIR", str(tmp_path))
    _write(tmp_path, GOOD, "ktr_targets.toml")
    sub = tmp_path / "alt"
    sub.mkdir()
    _write(sub, '[latency]\ntarget = 100\nop = "<="\n', "lat.toml")
    assert resolve_targets_path(None) == tmp_path / "ktr_targets.toml"
    assert resolve_targets_path("") == tmp_path / "ktr_targets.toml"
    assert set(load_targets()) == {"sensitivity", "dynamic_range", "max_rate"}
    assert set(load_targets("alt/lat.toml")) == {"latency"}
    absolute = _write(tmp_path, '[inst_bw]\ntarget = 18\nop = ">="\n', "abs.toml")
    assert resolve_targets_path(absolute) == absolute


def test_repository_default_file_is_valid_if_present():
    p = resolve_targets_path(None)
    if not p.is_file():
        pytest.skip("config/ktr_targets.toml henüz yok")
    t = load_targets()
    assert set(t) <= set(SPEC_IDS)
    assert all(v.op in ("<=", ">=") and math.isfinite(v.target) for v in t.values())


def test_missing_file_and_bad_toml_raise_config_error(tmp_path):
    with pytest.raises(ConfigError, match="bulunamadı"):
        load_targets(tmp_path / "yok.toml")
    with pytest.raises(ConfigError, match="TOML"):
        load_targets(_write(tmp_path, "[sensitivity\ntarget = 1"))


def test_all_validation_problems_reported_at_once(tmp_path):
    bad = """
[sensitivty]
target = -100
op = "<="

[dynamic_range]
target = "60"
op = "=>"

[inst_bw]
op = ">="
unit = "MHz"

[latency]
target = 100
note = 5

[df_rms]
target = true
op = "<="
"""
    with pytest.raises(ConfigError) as ei:
        load_targets(_write(tmp_path, bad))
    msg = str(ei.value)
    assert "[sensitivty]: bilinmeyen ölçüm kimliği" in msg
    assert "[dynamic_range]: 'target' sonlu bir sayı olmalı" in msg
    assert "[dynamic_range]: 'op' = '=>' geçersiz" in msg
    assert "[inst_bw]: bilinmeyen anahtar(lar) ['unit']" in msg
    assert "[inst_bw]: 'target' eksik" in msg
    assert "[latency]: 'op' eksik" in msg
    assert "[latency]: 'note' metin olmalı" in msg
    assert "[df_rms]: 'target' sonlu bir sayı olmalı" in msg


def test_non_table_entry_and_nonfinite_target_rejected():
    with pytest.raises(ConfigError, match="tablo bekleniyordu"):
        parse_targets({"sensitivity": 5})
    with pytest.raises(ConfigError, match="sonlu"):
        parse_targets({"sensitivity": {"target": float("nan"), "op": "<="}})
    with pytest.raises(ConfigError):
        Target(1.0, "==")
    with pytest.raises(ConfigError):
        Target(float("inf"), "<=")


@pytest.mark.parametrize(("op", "target", "value", "expected"), [
    ("<=", -100.0, -101.3, True),
    ("<=", -100.0, -100.0, True),      # boundary passes
    ("<=", -100.0, -99.9, False),
    (">=", 60.0, 60.0, True),
    (">=", 60.0, 72.5, True),
    (">=", 60.0, 59.99, False),
    ("<=", -100.0, None, None),
    (">=", 60.0, float("nan"), None),
])
def test_target_evaluation(op, target, value, expected):
    t = {"dynamic_range": Target(target, op)}
    r = _res("dynamic_range", value)
    apply_targets([r], t)
    assert r.passed is expected
    assert r.target == target and r.target_op == op
    assert t["dynamic_range"].passes(value) is expected


def test_apply_targets_is_authoritative_and_stores_note(tmp_path):
    targets = load_targets(_write(tmp_path, GOOD))
    sens = _res("sensitivity", -103.0)
    rate = _res("max_rate", 3.0)
    lat = _res("latency", 12.0)
    lat.evaluate_target(1.0, "<=")             # stale target from an earlier file
    lat.extra[TARGET_NOTE_KEY] = "eski"
    skipped = _res("dynamic_range", None, status="skipped")
    apply_targets([sens, rate, lat, skipped], targets)
    assert sens.passed is True and sens.extra[TARGET_NOTE_KEY] == "Pd = 0.9 @ Pfa = 1e-6"
    assert rate.passed is False and rate.target == 4.0 and rate.target_op == ">="
    assert lat.target is None and lat.target_op == "" and lat.passed is None
    assert TARGET_NOTE_KEY not in lat.extra
    assert skipped.passed is None and skipped.target == 60.0 and TARGET_NOTE_KEY not in skipped.extra


def test_target_describe():
    assert Target(-100.0, "<=").describe("dBm") == "≤ -100 dBm"
    assert Target(4.0, ">=").describe() == "≥ 4"


def test_uncalibrated_or_wrong_unit_gets_no_verdict(tmp_path):
    from eds.measure.targets import VERDICT_NOTE_KEY

    targets = load_targets(_write(tmp_path, GOOD))
    dbfs = _res("sensitivity", -130.0, unit="dBFS")          # uncalibrated: dBFS vs dBm target
    uncal = _res("sensitivity", -130.0)
    uncal.extra["calibrated"] = False
    ok = _res("sensitivity", -103.0)
    apply_targets([dbfs, uncal, ok], targets)
    assert dbfs.passed is None and "Birim" in dbfs.extra[VERDICT_NOTE_KEY]
    assert uncal.passed is None and "Kalibrasyonsuz" in uncal.extra[VERDICT_NOTE_KEY]
    assert ok.passed is True and VERDICT_NOTE_KEY not in ok.extra


# ----------------------------------------------------------------------------- verdict blockers
def test_results_flagged_not_comparable_get_no_verdict(tmp_path):
    """MM-04: quick mode, non-real-time simulator, no coherence and the measurement's own
    blockers keep the target but never give GEÇTİ/KALDI."""
    from eds.measure.targets import REASON_NO_COHERENCE, REASON_NONREALTIME, REASON_QUICK, VERDICT_NOTE_KEY

    t = {"max_rate": Target(4.0, ">="), "latency": Target(100.0, "<="), "df_rms": Target(5.0, "<=")}
    quick = _res("max_rate", 6.0)
    quick.conditions["quick"] = True
    nonrt = _res("latency", 300.0)
    nonrt.extra["realtime"] = False
    incoherent = _res("df_rms", 1.0)
    incoherent.extra["coherent"] = False
    own = _res("df_rms", 1.0)
    own.blockers = [REASON_NO_COHERENCE, "simülasyon: yalnız termal gürültü"]
    own.extra["coherent"] = False                              # same reason: listed once
    full = _res("max_rate", 6.0)
    full.conditions.update(quick=False, simulated=True)
    full.extra["realtime"] = True
    apply_targets([quick, nonrt, incoherent, own, full], t)
    for r, word in ((quick, REASON_QUICK), (nonrt, REASON_NONREALTIME), (incoherent, REASON_NO_COHERENCE)):
        assert r.passed is None and r.target is not None and word in r.extra[VERDICT_NOTE_KEY]
        assert r.extra[VERDICT_NOTE_KEY].endswith("hedefle karşılaştırılmadı")
    assert own.passed is None and own.extra[VERDICT_NOTE_KEY].count(REASON_NO_COHERENCE) == 1
    assert "termal" in own.extra[VERDICT_NOTE_KEY]
    assert full.passed is True and VERDICT_NOTE_KEY not in full.extra
    # a failed / skipped result gets no verdict note (nothing to compare)
    failed = _res("latency", None, status="failed")
    failed.conditions["quick"] = True
    apply_targets([failed], t)
    assert failed.passed is None and VERDICT_NOTE_KEY not in failed.extra
    # re-evaluation with a fresh (comparable) result clears a stale note
    quick.conditions["quick"] = False
    apply_targets([quick], t)
    assert quick.passed is True and VERDICT_NOTE_KEY not in quick.extra


REQ = """
[scan_speed]
target = 5.0
op = ">="
note = "325 MHz–3.8 GHz"

[scan_speed.require]
span_hz_min = 3.475e9
rbw_hz_max = 20e3
fs_hz = 20e6

[image_rejection]
target = 40.0
op = ">="

[image_rejection.require]
fcs_hz_count_min = 4
tracking = true

[max_rate]
target = 4.0
op = ">="

[max_rate.require]
headline_mode = "pipeline"
duration_s_min = 60
"""


def test_target_conditions_are_parsed_and_checked(tmp_path):
    from eds.measure.targets import TARGET_REQUIRE_KEY, VERDICT_NOTE_KEY

    t = load_targets(_write(tmp_path, REQ))
    assert dict(t["scan_speed"].require) == {"span_hz_min": 3.475e9, "rbw_hz_max": 20e3, "fs_hz": 20e6}
    quick_scan = _res("scan_speed", 7.0)                       # 325–825 MHz, RBW 39 kHz
    quick_scan.conditions.update(span_hz=500e6, rbw_hz=39.06e3, fs_hz=20e6)
    full_scan = _res("scan_speed", 7.0)
    full_scan.conditions.update(span_hz=3.475e9, rbw_hz=19.5e3, fs_hz=20e6)
    one_fc = _res("image_rejection", 45.0)
    one_fc.conditions.update(fcs_hz=[915e6], tracking=True)
    four_fc = _res("image_rejection", 45.0)
    four_fc.conditions.update(fcs_hz=[0.4e9, 1e9, 2e9, 3e9], tracking=True)
    transport = _res("max_rate", 6.0)
    transport.conditions.update(headline_mode="transport", duration_s=60.0)
    no_record = _res("max_rate", 6.0)                          # conditions not recorded → cannot verify
    apply_targets([quick_scan, full_scan, one_fc, four_fc, transport, no_record], t)
    assert quick_scan.passed is None
    note = quick_scan.extra[VERDICT_NOTE_KEY]
    assert "span_hz = 5e+08 (gerekli ≥ 3.475e+09)" in note and "rbw_hz = 39060 (gerekli ≤ 20000)" in note
    assert "fs_hz" not in note
    assert full_scan.passed is True and full_scan.extra[TARGET_REQUIRE_KEY]["rbw_hz_max"] == 20e3
    assert one_fc.passed is None and "fcs_hz sayısı 1 < 4" in one_fc.extra[VERDICT_NOTE_KEY]
    assert four_fc.passed is True
    assert transport.passed is None and "headline_mode = 'transport' (gerekli 'pipeline')" in \
        transport.extra[VERDICT_NOTE_KEY]
    assert no_record.passed is None and "kayıtlı değil" in no_record.extra[VERDICT_NOTE_KEY]


def test_bad_target_conditions_are_rejected(tmp_path):
    bad = """
[scan_speed]
target = 5.0
op = ">="
require = 3

[max_rate]
target = 4.0
op = ">="
[max_rate.require]
duration_s_min = "uzun"
mode = [1, 2]
"""
    with pytest.raises(ConfigError) as ei:
        load_targets(_write(tmp_path, bad))
    msg = str(ei.value)
    assert "[scan_speed]: 'require' tablo olmalı" in msg
    assert "duration_s_min = 'uzun': sonlu bir sayı olmalı" in msg
    assert "mode = [1, 2]: metin, true/false ya da sonlu sayı olmalı" in msg
    with pytest.raises(ConfigError, match="hedef koşulu"):
        Target(1.0, "<=", require={"rbw_hz_max": "x"})
    assert Target(1.0, "<=", require={"rbw_hz_max": 2e3}).require == (("rbw_hz_max", 2e3),)
