"""Jury report: files, traceability, numbers/units/status words, graceful degradation."""

from __future__ import annotations

import base64
import csv
import json
import logging
import re
from pathlib import Path

import numpy as np
import pytest

import eds
from eds.core.config import AppConfig, DeviceConfig
from eds.core.errors import BackendUnavailable
from eds.measure import report as report_mod
from eds.measure.base import FigureSpec, MeasurementResult, Series
from eds.measure.report import (
    EXT_BANNER,
    SIM_BANNER,
    SUMMARY_CSV_COLUMNS,
    console_table,
    format_condition,
    format_number,
    format_uncertainty,
    format_value,
    git_revision,
    short_conditions,
    verdict,
    write_report,
)
from eds.measure.targets import TARGET_NOTE_KEY, Target, apply_targets

T0_NS = 1_762_340_000_000_000_000          # 2025-11-05T10:53:20Z
FIXED_META = {"git_commit": "abc1234", "git_dirty": True, "operatör": "Ayşe Yılmaz"}


def _device(name: str = "rx0", backend: str = "sim", serial: str = "SIM-rx0",
            profile: str = "ad9363_pluto") -> dict:
    return {"info": {"name": name, "backend": backend, "driver": "", "hardware": "", "serial": serial,
                     "firmware": "sim-1" if backend == "sim" else "v0.38", "uri": "", "profile": profile,
                     "extra": {}},
            "fc_hz": 915e6, "fs_hz": 4e6, "bw_hz": 4e6, "gain_db": [40.0], "gain_mode": "manual"}


def _conditions(backend: str = "sim", profile: str = "ad9363_pluto", **kw) -> dict:
    d = {"backend": backend, "serial": "SIM-rx0", "profile": profile, "fc_hz": 915e6, "fs_hz": 4e6,
         "bw_hz": 4e6, "gain_db": [40.0], "gain_mode": "manual", "nfft": 4096, "rbw_hz": 1464.84375,
         "averages": 16, "window": "blackmanharris", "temperature_c": 35.2, "source": "sim"}
    d.update(kw)
    return d


def _spectrum_fig(name: str) -> FigureSpec:
    rng = np.random.default_rng(11)
    f = np.linspace(913.0, 917.0, 2001)
    p = -95.0 + 2.0 * rng.standard_normal(f.size)
    p[1000] = -101.3 + 60.0
    return FigureSpec(name=name, title="Spektrum — duyarlılık tonu", kind="spectrum",
                      series=[Series(x=f.tolist(), y=p.tolist(), label="dBm")], xlabel="Frekans [MHz]",
                      ylabel="Seviye [dBm]", info={"Center": "915 MHz", "Span": "4 MHz", "RBW": "1.46 kHz",
                                                   "Avg": "16", "Ref": "-30 dBm"}, ylim=(-130.0, -30.0))


def make_results(backend: str = "sim", profile: str = "ad9363_pluto") -> list[MeasurementResult]:
    rng = np.random.default_rng(42)
    dev = _device(backend=backend, profile=profile)
    p_in = np.arange(-115.0, -95.0, 0.5)
    sens_table = [{"p_in_dbm": p, "pd": float(1.0 / (1.0 + np.exp(-(p + 101.5)))), "n_trials": np.int64(200),
                   "detected": p > -101.3, "note": "a|b"} for p in np.linspace(-115, -95, 120)]
    sens = MeasurementResult(
        spec_id="sensitivity", title="Duyarlılık (MDS)", value=-101.3, unit="dBm", uncertainty=0.3,
        summary="Pd = 0.9 için giriş gücü -101.3 dBm", method="Kaynak gücü 0.5 dB adımlarla; CFAR Pd ölçümü",
        conditions=_conditions(backend, profile, pfa=1e-6, level_ref="kaynak referanslı",
                               calibrated=True, capture_s=0.0125),
        extra={"noise_floor_dbm_hz": -169.2, "nf_db": 4.8, "points": 40}, table=sens_table,
        figures=[_spectrum_fig("sensitivity_screenshot"),
                 FigureSpec(name="sensitivity_pd_curve", title="Pd eğrisi", kind="xy",
                            series=[Series(x=p_in.tolist(), y=(1 / (1 + np.exp(-(p_in + 101.5)))).tolist(),
                                           label="Pd", style="marker")],
                            xlabel="Giriş [dBm]", ylabel="Pd", hlines=[(0.9, "Pd = 0.9")],
                            vlines=[(-101.3, "MDS")])],
        device=dev, t_start_ns=T0_NS, duration_s=12.34)
    dr = MeasurementResult(
        spec_id="dynamic_range", title="Dinamik alan", value=55.04, unit="dB", method="İki ton",
        conditions=_conditions(backend, profile), device=dev, t_start_ns=T0_NS + 20_000_000_000, duration_s=3.0,
        figures=[FigureSpec(name="dynamic_range_hist", title="Gürültü dağılımı", kind="hist",
                            series=[Series(x=[], y=rng.normal(-95.0, 2.0, 800).tolist(), label="gürültü")],
                            xlabel="Seviye [dBFS]")])
    ibw = MeasurementResult(spec_id="inst_bw", title="Anlık bant genişliği", value=18.2, unit="MHz",
                            method="Geçiş bandı -3 dB", conditions=_conditions(backend, profile), device=dev,
                            t_start_ns=T0_NS + 30_000_000_000, duration_s=1.5)
    df = MeasurementResult(spec_id="df_rms", title="DF RMS hatası", value=None, unit="°", status="skipped",
                           error="En az 2 RX kanalı gerekli", summary="Atlandı: En az 2 RX kanalı gerekli",
                           device=dev, t_start_ns=T0_NS + 40_000_000_000)
    lat = MeasurementResult(spec_id="latency", title="Uçtan uca gecikme", value=None, unit="ms",
                            status="failed", error="RuntimeError: boom", summary="Başarısız: boom",
                            extra={"traceback": "Traceback (most recent call last):\n  ...\nRuntimeError: boom"},
                            device=dev, t_start_ns=T0_NS + 50_000_000_000, duration_s=0.2)
    rate = MeasurementResult(spec_id="max_rate", title="Taşmasız max örnekleme hızı", value=float("nan"),
                             unit="MS/s", conditions=_conditions(backend, profile), device=dev,
                             t_start_ns=T0_NS + 60_000_000_000, duration_s=5.0)
    img = MeasurementResult(
        spec_id="image_rejection", title="İmaj bastırma", value=42.0, unit="dB",
        conditions=_conditions(backend, profile), device=dev, t_start_ns=T0_NS + 70_000_000_000, duration_s=4.0,
        figures=[FigureSpec(name="image_rejection_polar", title="Kutupsal", kind="polar",
                            series=[Series(x=list(range(0, 360, 15)), y=[40.0 + (i % 5) for i in range(24)],
                                           label="IRR")], vlines=[(45.0, "45°")]),
                 FigureSpec(name="image_rejection_broken", title="Bozuk grafik", kind="pie")])
    apply_targets([sens, dr, ibw, df, lat, rate, img], {
        "sensitivity": Target(-100.0, "<=", "Pd = 0.9 @ Pfa = 1e-6"),
        "dynamic_range": Target(60.0, ">="), "df_rms": Target(5.0, "<="), "latency": Target(100.0, "<="),
        "max_rate": Target(4.0, ">="), "image_rejection": Target(40.0, ">="),
    })
    return [sens, dr, ibw, df, lat, rate, img]


@pytest.fixture(scope="module")
def full_report(tmp_path_factory):
    pytest.importorskip("matplotlib")
    out = tmp_path_factory.mktemp("reports")
    results = make_results()
    app = AppConfig(devices=[DeviceConfig(name="rx0", backend="sim", profile="ad9363_pluto")])
    app.source_path = "/etc/eds/rx_sim.toml"
    rdir = write_report(results, out, app=app, meta=FIXED_META)
    return rdir, results


def _read_csv(path: Path) -> list[dict[str, str]]:
    with path.open(encoding="utf-8-sig", newline="") as f:
        return list(csv.DictReader(f))


# ------------------------------------------------------------------------- full report
def test_report_folder_layout(full_report):
    rdir, results = full_report
    assert re.fullmatch(r"\d{8}-\d{6}(-\d+)?", rdir.name)
    for name in ("summary.md", "summary.csv", "results.json", "report.html"):
        assert (rdir / name).is_file() and (rdir / name).stat().st_size > 200
    pngs = sorted(p.name for p in (rdir / "figures").glob("*.png"))
    assert pngs == ["dynamic_range_hist.png", "image_rejection_polar.png", "sensitivity_pd_curve.png",
                    "sensitivity_screenshot.png"]
    assert all((rdir / "figures" / p).stat().st_size > 10_000 for p in pngs)
    assert (rdir / "tables" / "sensitivity.csv").is_file()
    assert sorted(p.name for p in (rdir / "tables").iterdir()) == ["sensitivity.csv"]


def test_artifacts_recorded_relative_and_existing(full_report):
    rdir, results = full_report
    sens, img = results[0], results[6]
    assert sens.artifacts == ["figures/sensitivity_screenshot.png", "figures/sensitivity_pd_curve.png",
                              "tables/sensitivity.csv"]
    assert img.artifacts == ["figures/image_rejection_polar.png"]       # broken figure not recorded
    assert "serial" not in sens.figures[0].info and "version" not in sens.figures[0].info  # spec not mutated
    for r in results:
        for a in r.artifacts:
            assert (rdir / a).is_file()


def test_markdown_header_traceability(full_report):
    rdir, _ = full_report
    md = (rdir / "summary.md").read_text(encoding="utf-8")
    assert md.startswith("# EDS RX - KTR Nicel Spesifikasyon Ölçüm Raporu")
    assert f"> **UYARI: {SIM_BANNER}**" in md
    assert re.search(r"\| Rapor tarihi \(UTC\) \| \d{4}-\d\d-\d\dT\d\d:\d\d:\d\dZ \|", md)
    assert f"| Yazılım | EDS {eds.__version__} |" in md
    assert "| Git commit | abc1234 (+ yerel değişiklikler) |" in md
    assert "| Konfigürasyon | /etc/eds/rx_sim.toml |" in md
    assert "ktr_targets.toml" in md
    assert "| operatör | Ayşe Yılmaz |" in md
    assert "2025-11-05T10:53:20Z" in md                                   # measurement time
    assert "| rx0 | sim | SIM-rx0 | ad9363_pluto | sim-1 |" in md          # device table


def test_markdown_main_table_values_units_status(full_report):
    rdir, _ = full_report
    md = (rdir / "summary.md").read_text(encoding="utf-8")
    assert "| # | Parametre | Ölçülen | Birim | Hedef | Durum | Koşullar (kısa) |" in md
    rows = {m.group(1): m.group(0) for m in re.finditer(r"^\| \d \| ([^|]+) \|.*$", md, re.M)}
    assert rows["Duyarlılık (MDS)"].startswith(
        "| 1 | Duyarlılık (MDS) | -101.3 ± 0.3 | dBm | ≤ -100.0 | GEÇTİ | fc 915 MHz; fs 4 MHz; RBW 1.46484 kHz; G 40.0 dB |")
    assert rows["Dinamik alan"].startswith("| 2 | Dinamik alan | 55.0 | dB | ≥ 60.0 | KALDI |")
    assert rows["Anlık bant genişliği"].startswith("| 3 | Anlık bant genişliği | 18.2 | MHz | — | HEDEF YOK |")
    assert rows["DF RMS hatası"].startswith("| 4 | DF RMS hatası | — | ° | ≤ 5.00 | ATLANDI |")
    assert rows["Uçtan uca gecikme"].startswith("| 5 | Uçtan uca gecikme | — | ms | ≤ 100 | HATA |")
    assert rows["Taşmasız max örnekleme hızı"].startswith("| 6 | Taşmasız max örnekleme hızı | — | MS/s | ≥ 4.00 | HATA |")
    assert rows["İmaj bastırma"].startswith("| 7 | İmaj bastırma | 42.0 | dB | ≥ 40.0 | GEÇTİ |")


def test_markdown_sections(full_report):
    rdir, _ = full_report
    md = (rdir / "summary.md").read_text(encoding="utf-8")
    sec = md.split("### 1. Duyarlılık (MDS) (`sensitivity`)")[1].split("### 2.")[0]
    assert "- **Sonuç:** -101.3 dBm ± 0.3 dB — GEÇTİ (hedef ≤ -100.0 dBm)" in sec
    assert "- **Hedef notu:** Pd = 0.9 @ Pfa = 1e-6" in sec
    assert "- **Yöntem:** Kaynak gücü 0.5 dB adımlarla; CFAR Pd ölçümü" in sec
    assert "- **Ölçüm zamanı (UTC):** 2025-11-05T10:53:20Z (süre 12.34 s)" in sec
    assert "seri SIM-rx0" in sec
    assert "| Merkez frekansı (fc) (`fc_hz`) | 915 MHz |" in sec
    assert "| Sıcaklık (`temperature_c`) | 35.2 °C |" in sec
    assert "| Yanlış alarm olasılığı (Pfa) (`pfa`) | 1.00e-06 |" in sec
    assert "| `capture_s` | 12.5 ms |" in sec
    assert "| Güç kalibrasyonlu (`calibrated`) | evet |" in sec
    assert "#### Ek metrikler" in sec and "| `noise_floor_dbm_hz` | -169.2 dBm/Hz |" in sec
    assert "| `nf_db` | 4.8 dB |" in sec
    assert TARGET_NOTE_KEY not in sec                                        # shown as "Hedef notu" only
    assert "![Şekil 1.1 — Spektrum — duyarlılık tonu](figures/sensitivity_screenshot.png)" in sec
    assert "(figures/sensitivity_pd_curve.png)" in sec
    assert "İlk 50 / 120 satır; tamamı: [tables/sensitivity.csv](tables/sensitivity.csv)" in sec
    assert "| p_in_dbm | pd | n_trials | detected | note |" in sec
    raw_rows = [ln for ln in sec.splitlines() if re.match(r"^\| -?\d", ln)]
    assert len(raw_rows) == 50
    assert raw_rows[0].startswith("| -115 | ") and raw_rows[0].endswith("| 200 | hayır | a\\|b |")
    failed = md.split("### 5. Uçtan uca gecikme")[1].split("### 6.")[0]
    assert "- **Hata:** RuntimeError: boom" in failed and "```text" in failed
    skipped = md.split("### 4. DF RMS hatası")[1].split("### 5.")[0]
    assert "- **Not:** En az 2 RX kanalı gerekli" in skipped
    nan_sec = md.split("### 6. ")[1].split("### 7.")[0]
    assert "Ölçüm geçerli bir değer üretmedi" in nan_sec and "- **Sonuç:** değer yok — HATA" in nan_sec
    broken = md.split("### 7. ")[1]
    assert "*Şekil 7.2 — Bozuk grafik* — Grafik çizilemedi: MeasurementError" in broken


def test_summary_csv_parses(full_report):
    rdir, results = full_report
    rows = _read_csv(rdir / "summary.csv")
    assert tuple(rows[0].keys()) == SUMMARY_CSV_COLUMNS
    assert [r["spec_id"] for r in rows] == [r.spec_id for r in results]
    sens = rows[0]
    assert float(sens["value"]) == -101.3 and sens["unit"] == "dBm"
    assert sens["target_op"] == "<=" and float(sens["target"]) == -100.0
    assert sens["passed"] == "true" and sens["status"] == "ok" and sens["verdict"] == "GEÇTİ"
    assert json.loads(sens["conditions"])["fc_hz"] == 915e6
    assert sens["method"].startswith("Kaynak gücü") and sens["uncertainty"] == "0.3"
    assert sens["device_serial"] == "SIM-rx0" and sens["git_commit"] == "abc1234-dirty"
    assert sens["eds_version"] == eds.__version__ and sens["t_start_utc"] == "2025-11-05T10:53:20Z"
    assert rows[1]["passed"] == "false" and rows[1]["verdict"] == "KALDI"
    assert rows[2]["passed"] == "" and rows[2]["target"] == "" and rows[2]["verdict"] == "HEDEF YOK"
    assert rows[3]["value"] == "" and rows[3]["status"] == "skipped" and rows[3]["verdict"] == "ATLANDI"
    assert rows[4]["status"] == "failed" and rows[4]["error"] == "RuntimeError: boom"
    assert rows[5]["value"] == "" and rows[5]["verdict"] == "HATA"                # NaN → empty


def test_table_csv_has_all_rows(full_report):
    rdir, results = full_report
    rows = _read_csv(rdir / "tables" / "sensitivity.csv")
    assert len(rows) == 120
    assert float(rows[0]["p_in_dbm"]) == -115.0 and rows[0]["detected"] == "false"
    assert float(rows[-1]["pd"]) == pytest.approx(results[0].table[-1]["pd"], rel=1e-15)
    assert rows[5]["note"] == "a|b"


def test_results_json(full_report):
    rdir, results = full_report
    data = json.loads((rdir / "results.json").read_text(encoding="utf-8"))
    meta = data["meta"]
    assert meta["simulation"] is True and meta["out_of_spec_profile"] is False
    assert meta["eds_version"] == eds.__version__ and meta["git_commit"] == "abc1234" and meta["git_dirty"] is True
    assert meta["config_path"] == "/etc/eds/rx_sim.toml"
    assert meta["devices"][0]["serial"] == "SIM-rx0"
    assert meta["verdicts"] == {"GEÇTİ": 2, "KALDI": 1, "HEDEF YOK": 1, "ATLANDI": 1, "HATA": 2}
    assert meta["figures_rendered"] == 4 and meta["figures_failed"] == 1
    assert meta["measured_from_utc"] == "2025-11-05T10:53:20Z"
    assert meta["user_meta"]["operatör"] == "Ayşe Yılmaz"
    items = data["results"]
    assert len(items) == len(results) and all("figures" not in d for d in items)
    assert items[0]["value"] == -101.3 and items[0]["passed"] is True and items[0]["verdict"] == "GEÇTİ"
    assert items[5]["value"] is None                                          # NaN sanitized
    assert items[0]["artifacts"] == results[0].artifacts
    assert len(items[0]["table"]) == 120


def test_html_self_contained_with_images_badges_and_banner(full_report):
    rdir, _ = full_report
    page = (rdir / "report.html").read_text(encoding="utf-8")
    assert page.startswith("<!DOCTYPE html>") and '<html lang="tr">' in page
    assert "@page{size:A4" in page and "<style>" in page
    assert not re.search(r'<(link|script)\b', page)
    assert not re.search(r'src="(?!data:)', page)                             # no external resources
    uris = re.findall(r'src="data:image/png;base64,([A-Za-z0-9+/=]+)"', page)
    assert len(uris) == 4
    assert all(base64.b64decode(u)[:8] == b"\x89PNG\r\n\x1a\n" for u in uris)
    assert f'<div class="banner sim">UYARI: {SIM_BANNER}</div>' in page
    assert "spesifikasyon dışı" not in page.lower()
    for cls, word in (("pass", "GEÇTİ"), ("fail", "KALDI"), ("none", "HEDEF YOK"), ("skip", "ATLANDI"),
                      ("err", "HATA")):
        assert f'<span class="badge b-{cls}">{word}</span>' in page
    assert "-101.3 ± 0.3" in page and "Ayşe Yılmaz" in page and "abc1234" in page
    assert "a|b" in page and "&lt;" not in page.split("<body>")[0]


# ------------------------------------------------------------------------- variants
def _no_figures(results: list[MeasurementResult]) -> list[MeasurementResult]:
    for r in results:
        r.figures = []
    return results


def test_ext_profile_banner_and_no_sim_banner_on_hardware(tmp_path):
    results = _no_figures(make_results(backend="soapy", profile="ad9364_pluto_ext"))
    rdir = write_report(results, tmp_path, meta={"git_commit": "abc1234"})
    md = (rdir / "summary.md").read_text(encoding="utf-8")
    page = (rdir / "report.html").read_text(encoding="utf-8")
    assert SIM_BANNER not in md and SIM_BANNER not in page
    assert EXT_BANNER in md and '<div class="banner ext">' in page
    assert "spesifikasyon dışı mod" in page
    meta = json.loads((rdir / "results.json").read_text(encoding="utf-8"))["meta"]
    assert meta["simulation"] is False and meta["out_of_spec_profile"] is True
    assert not (rdir / "figures").exists()
    assert "| Git commit | abc1234 |" in md


def test_missing_matplotlib_degrades_gracefully(tmp_path, monkeypatch, caplog):
    from eds.measure import plots

    calls = []

    def unavailable(fig, path, **kw):
        calls.append(fig.name)
        raise BackendUnavailable("Grafik çizimi için matplotlib gerekli: pip install matplotlib")

    monkeypatch.setattr(plots, "render", unavailable)
    results = make_results()
    with caplog.at_level(logging.WARNING, logger="eds.measure.report"):
        rdir = write_report(results, tmp_path, meta=FIXED_META)
    assert calls == ["sensitivity_screenshot"]                                # gave up after the first
    assert any("matplotlib" in rec.getMessage() for rec in caplog.records)
    md = (rdir / "summary.md").read_text(encoding="utf-8")
    assert md.count("Grafik çizilemedi: Grafik çizimi için matplotlib gerekli") == 5
    assert "base64" not in (rdir / "report.html").read_text(encoding="utf-8")
    assert all(not a.startswith("figures/") for r in results for a in r.artifacts)
    assert _read_csv(rdir / "summary.csv")[0]["verdict"] == "GEÇTİ"


def test_unique_folders_in_same_second(tmp_path, monkeypatch):
    monkeypatch.setattr(report_mod, "now_ns", lambda: T0_NS)
    a = write_report([], tmp_path, meta={"git_commit": "x"})
    b = write_report([], tmp_path, meta={"git_commit": "x"})
    assert a != b and b.name == f"{a.name}-2"
    md = (a / "summary.md").read_text(encoding="utf-8")
    assert "Ölçüm sonucu yok." in md and "2025-11-05T10:53:20Z" in md
    assert json.loads((a / "results.json").read_text(encoding="utf-8"))["results"] == []
    assert _read_csv(a / "summary.csv") == []


def test_rewriting_report_replaces_artifacts(tmp_path):
    results = _no_figures(make_results())[:1]
    write_report(results, tmp_path / "a", meta={"git_commit": "x"})
    write_report(results, tmp_path / "b", meta={"git_commit": "x"})
    assert results[0].artifacts == ["tables/sensitivity.csv"]


def test_config_path_resolved_to_absolute(tmp_path, monkeypatch):
    cfg = tmp_path / "rx.toml"
    cfg.write_text("", encoding="utf-8")
    monkeypatch.chdir(tmp_path)
    app = AppConfig()
    app.source_path = "rx.toml"
    rdir = write_report([], tmp_path / "out", app=app, meta={"git_commit": "x"})
    md = (rdir / "summary.md").read_text(encoding="utf-8")
    assert f"| Konfigürasyon | {cfg.resolve()} |" in md
    assert SIM_BANNER in md                                           # configured sim device → banner


def test_device_group_members_listed(tmp_path):
    group = _device(name="grp", backend="group", serial="")
    group["members"] = [_device("rx0", "soapy", "SN-A"), _device("rx1", "soapy", "SN-B")]
    r = MeasurementResult(spec_id="df_rms", title="DF RMS", value=2.5, unit="°", device=group)
    rdir = write_report([r], tmp_path, meta={"git_commit": "x"})
    md = (rdir / "summary.md").read_text(encoding="utf-8")
    assert "| rx0 | soapy | SN-A |" in md and "| rx1 | soapy | SN-B |" in md
    assert SIM_BANNER not in md


def test_git_revision():
    sha, dirty = git_revision()
    assert sha == "unknown" or re.fullmatch(r"[0-9a-f]{4,40}", sha)
    assert dirty in (True, False, None)


def test_git_revision_outside_repo(tmp_path):
    assert git_revision(tmp_path) == ("unknown", None)


# ------------------------------------------------------------------------- formatting
@pytest.mark.parametrize(("value", "unit", "unc", "expected"), [
    (-101.3, "dBm", None, "-101.3 dBm"),
    (-101.34, "dBm", None, "-101.3 dBm"),
    (18.2, "MHz", None, "18.2 MHz"),
    (18.23456, "MHz", None, "18.2 MHz"),
    (4, "MS/s", None, "4.00 MS/s"),
    (61.44, "MS/s", None, "61.44 MS/s"),
    (0.5208333, "MS/s", None, "0.521 MS/s"),
    (12.5, "ms", None, "12.5 ms"),
    (12.3456, "ms", None, "12.3 ms"),
    (0.82, "°", None, "0.82 °"),
    (12.345, "°", None, "12.3 °"),
    (60.0, "dB", None, "60.0 dB"),
    (2441.40625, "Hz", None, "2441 Hz"),
    (10000.0, "Hz", None, "10000 Hz"),
    (5.2, "GHz/s", None, "5.20 GHz/s"),
    (-0.04, "dB", None, "0.0 dB"),
    (1e-6, "", None, "1.00e-06"),
    (-101.3, "dBm", 0.3, "-101.3 dBm ± 0.3 dB"),
    (-101.27, "dBm", 0.15, "-101.27 dBm ± 0.15 dB"),
    (18.234, "MHz", 2.0, "18 MHz ± 2 MHz"),
    (0.82, "°", 0.05, "0.82 ° ± 0.05 °"),
    (0.82, "", 0.3, "0.8 ± 0.3"),
    (None, "dBm", None, "—"),
    (float("nan"), "dB", 0.3, "—"),
    (float("inf"), "dB", None, "+∞ dB"),
])
def test_format_value(value, unit, unc, expected):
    assert format_value(value, unit, unc) == expected


def test_format_helpers():
    assert format_number(-101.3, "dBm", 0.3) == "-101.3 ± 0.3"
    assert format_number(None, "dB") == "—"
    assert format_uncertainty(0.3, "dBm") == "± 0.3 dB"
    assert format_uncertainty(0.3) == "± 0.3"
    assert format_uncertainty(None, "dB") == ""
    assert format_condition("fc_hz", 2.4e9) == "2.4 GHz"
    assert format_condition("rbw_hz", 1464.84375) == "1.46484375 kHz"
    assert format_condition("gain_db", [40.0, 38.0]) == "40.0 dB, 38.0 dB"
    assert format_condition("duration_s", 0.0125) == "12.5 ms"
    assert format_condition("settle_s", 2.5e-5) == "25.0 µs"
    assert format_condition("t_start_ns", T0_NS) == "2025-11-05T10:53:20Z"
    assert format_condition("rate_hz_per_s", 5.2e9) == "5.2 GHz/s"
    assert format_condition("nfft", np.int64(4096)) == "4096"
    assert format_condition("tracking", True) == "evet"
    assert format_condition("x", None) == "—"
    assert format_condition("ppm", 12.34567) == "12.3 ppm"
    assert format_condition("noise_dbfs_hz", -150.04) == "-150.0 dBFS/Hz"
    assert format_condition("cal", {"a": float("nan")}) == '{"a": null}'
    assert format_condition("tracking", np.bool_(False)) == "hayır"
    assert format_condition("gain_db", np.array([40.0, 38.0])) == "40.0 dB, 38.0 dB"
    assert format_condition("fc_hz", np.float32(915e6)) == "915 MHz"


def test_verdict_and_short_conditions():
    ok = MeasurementResult(spec_id="sensitivity", title="t", value=1.0, unit="dB")
    assert verdict(ok) == "HEDEF YOK"
    ok.evaluate_target(0.0, ">=")
    assert verdict(ok) == "GEÇTİ"
    ok.evaluate_target(2.0, ">=")
    assert verdict(ok) == "KALDI"
    assert verdict(MeasurementResult(spec_id="x", title="t", value=None, unit="", status="skipped")) == "ATLANDI"
    assert verdict(MeasurementResult(spec_id="x", title="t", value=3.0, unit="", status="failed")) == "HATA"
    r = MeasurementResult(spec_id="x", title="t", value=1.0, unit="dB",
                          conditions={"serial": "S", "custom_hz": 1e3, "mode": "a"})
    assert short_conditions(r) == "custom_hz 1 kHz; mode a"
    assert short_conditions(MeasurementResult(spec_id="x", title="t", value=1.0, unit="")) == "—"


def test_console_table():
    results = _no_figures(make_results())
    text = console_table(results)
    lines = text.splitlines()
    assert lines[0].split() == ["#", "Parametre", "Ölçülen", "Birim", "Hedef", "Durum"]
    assert set(lines[1].replace(" ", "")) == {"-"}
    assert "Duyarlılık (MDS)" in lines[2] and "-101.3 ± 0.3" in lines[2] and lines[2].endswith("GEÇTİ")
    assert lines[3].endswith("KALDI") and lines[4].endswith("HEDEF YOK")
    assert lines[5].endswith("ATLANDI") and lines[6].endswith("HATA")
    col = lines[0].index("Durum")
    assert all(ln[col:].split()[0] in {"GEÇTİ", "KALDI", "HEDEF", "ATLANDI", "HATA"} for ln in lines[2:9])
    assert lines[-1] == "Toplam: 7  |  GEÇTİ: 2  KALDI: 1  HEDEF YOK: 1  ATLANDI: 1  HATA: 2"
    assert "\x1b[" not in text
