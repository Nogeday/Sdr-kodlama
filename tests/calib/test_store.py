"""eds.calib.store: DeviceCalibration round trip, file naming, atomic writes, error handling."""

from __future__ import annotations

import json
import logging
import os

import pytest

from eds.calib.power import PowerCalTable
from eds.calib.ppm import PpmEstimate
from eds.calib.store import CAL_FORMAT_VERSION, CalibrationStore, DeviceCalibration
from eds.core.errors import CalibrationError, ConfigError


def _full(serial: str = "104473dc599300131300ea00d5b6a7c4b8") -> DeviceCalibration:
    ppm = PpmEstimate(ppm=11.7, std_ppm=0.004, f_measured_hz=999_988_300.1, f_ref_hz=1e9, snr_db=52.0, n=262144,
                      method="cw", temperature_c=38.5, t_ns=1_700_000_000_000_000_000, fc_nominal_hz=999.9e6,
                      fs_hz=1e6, ppm_residual=11.7, ppm_hw=0.0, repeats=5, extra={"rbw_hz": 488.3})
    power = PowerCalTable([(433.92e6, 40.0, -71.8), (915e6, 40.0, -73.2), (915e6, 60.0, -93.1)],
                          meta={"source": "scpi"})
    return DeviceCalibration(serial=serial, profile="ad9363_pluto", ppm=ppm, power=power,
                             image_rejection_db={"915000000": 41.2, "2437000000": 36.8},
                             coherence={"delay_samples": 0.37, "phase_deg": -47.2, "coherent": True},
                             notes=["kurulum A"])


def test_round_trip_through_store(tmp_path):
    store = CalibrationStore(tmp_path / "cal")
    cal = _full()
    path = store.save(cal)
    assert path.parent == tmp_path / "cal" and path.name == f"{cal.serial}.json"
    assert cal.updated_ns > 0
    raw = json.loads(path.read_text(encoding="utf-8"))
    assert raw["format"] == CAL_FORMAT_VERSION and raw["ppm"]["ppm"] == 11.7
    assert path.read_text(encoding="utf-8").startswith("{\n  ")            # indent 2, human readable
    back = store.load(cal.serial)
    assert back.to_dict() == cal.to_dict()
    assert back.power.offset_db(915e6, 50.0) == pytest.approx(cal.power.offset_db(915e6, 50.0))
    assert back.temperature_c == 38.5
    assert store.exists(cal.serial)


def test_missing_file_gives_empty_calibration(tmp_path):
    cal = CalibrationStore(tmp_path).load("SIM-rx0")
    assert cal.serial == "SIM-rx0" and cal.is_empty and cal.ppm is None and cal.power is None
    assert cal.temperature_c is None


def test_default_directory_follows_env(tmp_path, monkeypatch):
    monkeypatch.setenv("EDS_CAL_DIR", str(tmp_path / "envcal"))
    store = CalibrationStore()
    assert store.dir == (tmp_path / "envcal").resolve()
    store.save(DeviceCalibration(serial="SIM-A"))
    assert (tmp_path / "envcal" / "SIM-A.json").is_file()


def test_filenames_are_sanitized_and_unique(tmp_path):
    store = CalibrationStore(tmp_path)
    p1 = store.path_for("usb:1.5.5")
    assert ":" not in p1.name and p1.name.startswith("usb_1.5.5-") and p1.suffix == ".json"
    assert store.path_for("a/b") != store.path_for("a_b")
    assert store.path_for("SIM-rx0").name == "SIM-rx0.json"
    assert store.path_for("../../etc/passwd").parent == tmp_path
    assert len(store.path_for("x" * 300).name) < 100
    for bad in ("", "   "):
        with pytest.raises(ConfigError):
            store.path_for(bad)


def test_atomic_write_keeps_old_file_on_failure(tmp_path, monkeypatch):
    store = CalibrationStore(tmp_path)
    cal = _full("SN1")
    path = store.save(cal)
    before = path.read_text(encoding="utf-8")

    def boom(fd):
        raise OSError(28, "No space left on device")

    monkeypatch.setattr(os, "fsync", boom)
    cal.notes.append("yeni not")
    with pytest.raises(CalibrationError, match="yazılamadı"):
        store.save(cal)
    assert path.read_text(encoding="utf-8") == before                      # old content intact
    assert sorted(p.name for p in tmp_path.iterdir()) == ["SN1.json"]       # no temporary left behind


def test_corrupt_unknown_and_mismatched_files_raise(tmp_path, caplog):
    store = CalibrationStore(tmp_path)
    store.save(_full("GOOD"))
    store.path_for("BROKEN").write_text("{not json", encoding="utf-8")
    with pytest.raises(CalibrationError, match="bozuk"):
        store.load("BROKEN")
    d = _full("UNK").to_dict()
    d["surprise"] = 1
    store.path_for("UNK").write_text(json.dumps(d), encoding="utf-8")
    with pytest.raises(CalibrationError, match="bilinmeyen"):
        store.load("UNK")
    store.path_for("OTHER").write_text(json.dumps(_full("SOMEONE").to_dict()), encoding="utf-8")
    with pytest.raises(CalibrationError, match="uyuşmuyor"):
        store.load("OTHER")
    d = _full("FMT").to_dict()
    d["format"] = 99
    store.path_for("FMT").write_text(json.dumps(d), encoding="utf-8")
    with pytest.raises(CalibrationError, match="biçimi"):
        store.load("FMT")
    with caplog.at_level(logging.WARNING, logger="eds.calib.store"):
        serials = store.list()
    assert serials == ["GOOD", "SOMEONE"]                                   # unreadable ones skipped …
    assert "atlandı" in caplog.text                                         # … but reported


def test_list_empty_and_notes(tmp_path):
    assert CalibrationStore(tmp_path / "none").list() == []
    cal = DeviceCalibration(serial="X")
    cal.add_note("ppm yenilendi")
    assert cal.notes[0].endswith("ppm yenilendi") and not cal.is_empty
    with pytest.raises(ConfigError):
        DeviceCalibration(serial="X", ppm={"ppm": 1.0})                     # type: ignore[arg-type]
