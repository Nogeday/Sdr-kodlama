"""Jury report: files, traceability, numbers/units/status words, graceful degradation."""

from __future__ import annotations

import base64
import csv
import html
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
    NOT_COMPARED,
    QUICK_BANNER,
    QUICK_TAG,
    SIM_BANNER,
    SIM_TAG,
    SUMMARY_CSV_COLUMNS,
    _raw_col_width,
    _raw_layout,
    console_table,
    figure_flags,
    format_condition,
    format_number,
    format_uncertainty,
    format_value,
    git_revision,
    no_verdict_reason,
    short_conditions,
    verdict,
    write_report,
)
from eds.measure.targets import REASON_QUICK, TARGET_NOTE_KEY, Target, apply_targets

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
        "| 1 | Duyarlılık (MDS) | -101.3 ± 0.3 | dBm | ≤ -100.0 | GEÇTİ | fc 915 MHz; RBW 1.465 kHz; Pfa 1.00e-06; G 40.0 dB |")
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
    assert meta["verdicts"] == {"GEÇTİ": 2, "KALDI": 1, "KARŞILAŞTIRILMADI": 0, "HEDEF YOK": 1, "ATLANDI": 1,
                                "HATA": 2}
    assert meta["quick"] is False and meta["quick_specs"] == []
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
    assert format_condition("cal", {"a": float("nan")}) == "a: —"
    assert format_condition("tracking", np.bool_(False)) == "hayır"
    assert format_condition("gain_db", np.array([40.0, 38.0])) == "40.0 dB, 38.0 dB"
    assert format_condition("fc_hz", np.float32(915e6)) == "915 MHz"


def test_verdict_and_short_conditions():
    ok = MeasurementResult(spec_id="sensitivity", title="t", value=1.0, unit="dBm")
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
    assert lines[-1] == "Toplam: 7  |  GEÇTİ: 2  KALDI: 1  KARŞILAŞTIRILMADI: 0  HEDEF YOK: 1  ATLANDI: 1  HATA: 2"
    assert "\x1b[" not in text


# ------------------------------------------------------------------------- quick / simulation / no verdict
def _png_text(path: Path) -> dict[str, str]:
    import struct

    data, pos, out = path.read_bytes(), 8, {}
    while pos < len(data):
        (n,) = struct.unpack(">I", data[pos:pos + 4])
        kind, body = data[pos + 4:pos + 8], data[pos + 8:pos + 8 + n]
        if kind == b"tEXt":
            k, _, v = body.partition(b"\x00")
            out[k.decode("latin-1")] = v.decode("latin-1")
        elif kind == b"iTXt":
            k, _, rest = body.partition(b"\x00")
            out[k.decode("latin-1")] = rest[2:].split(b"\x00", 2)[-1].decode("utf-8")
        pos += 12 + n
    return out


def _quick_results(simulated: bool = True) -> list[MeasurementResult]:
    """Results as run_measurement produces them in ``--quick`` mode (conditions quick/simulated)."""
    results = make_results(backend="sim" if simulated else "soapy")
    for r in results:
        r.conditions["quick"] = True
        r.conditions["simulated"] = simulated
    apply_targets(results, {"sensitivity": Target(-100.0, "<="), "dynamic_range": Target(60.0, ">="),
                            "image_rejection": Target(40.0, ">=")})
    return results


@pytest.fixture(scope="module")
def quick_report(tmp_path_factory):
    pytest.importorskip("matplotlib")
    results = _quick_results()
    for r in results[1:]:
        r.figures = []
    rdir = write_report(results, tmp_path_factory.mktemp("quick"), meta={"git_commit": "abc1234"})
    return rdir, results


def test_quick_mode_banner_header_csv_json(quick_report):
    """JURY-01 / MM-03: a --quick report says so everywhere the jury or a later reader looks."""
    rdir, results = quick_report
    md = (rdir / "summary.md").read_text(encoding="utf-8")
    page = (rdir / "report.html").read_text(encoding="utf-8")
    assert QUICK_BANNER.startswith("HIZLI MOD - KTR'de kullanılmaz")
    assert f"> **UYARI: {QUICK_BANNER}**" in md and f"> **UYARI: {SIM_BANNER}**" in md
    assert '<div class="banner quick">UYARI: ' + html.escape(QUICK_BANNER) + "</div>" in page
    assert re.search(r"\| Ölçüm modu \| HIZLI \(--quick\)", md)
    assert "| Veri kaynağı | SİMÜLASYON (donanım ölçümü değil) |" in md
    rows = _read_csv(rdir / "summary.csv")
    assert {"quick", "simulated", "verdict_note"} <= set(rows[0])
    assert all(r["quick"] == "true" and r["simulated"] == "true" for r in rows)
    meta = json.loads((rdir / "results.json").read_text(encoding="utf-8"))["meta"]
    assert meta["quick"] is True and meta["quick_specs"] == [r.spec_id for r in results]
    assert QUICK_BANNER in meta["banners"]


def test_quick_results_are_not_compared_and_show_the_reason(quick_report):
    """MM-04 (report side): a blocked result is KARŞILAŞTIRILMADI + reason, never HEDEF YOK/GEÇTİ."""
    rdir, results = quick_report
    md = (rdir / "summary.md").read_text(encoding="utf-8")
    page = (rdir / "report.html").read_text(encoding="utf-8")
    assert verdict(results[0]) == NOT_COMPARED and results[0].passed is None
    assert REASON_QUICK in no_verdict_reason(results[0])
    row = next(ln for ln in md.splitlines() if ln.startswith("| 1 | Duyarlılık (MDS)"))
    assert f"| {NOT_COMPARED} — {REASON_QUICK} |" in row
    assert '<span class="badge b-nc">KARŞILAŞTIRILMADI</span><div class="why">' in page
    sec = md.split("### 1. ")[1].split("### 2.")[0]
    assert f"- **Sonuç:** -101.3 dBm ± 0.3 dB — {NOT_COMPARED} (hedef ≤ -100.0 dBm)" in sec
    assert f"- **Karşılaştırılmama nedeni:** {REASON_QUICK}" in sec
    assert "verdict_note" not in sec                                         # shown as a fact only
    rows = _read_csv(rdir / "summary.csv")
    assert rows[0]["verdict"] == NOT_COMPARED and rows[0]["passed"] == "" and REASON_QUICK in rows[0]["verdict_note"]
    items = json.loads((rdir / "results.json").read_text(encoding="utf-8"))["results"]
    assert items[0]["verdict"] == NOT_COMPARED and REASON_QUICK in items[0]["no_verdict_reason"]
    assert rows[2]["verdict"] == "HEDEF YOK"            # quick but no target: nothing to compare
    assert rows[3]["verdict"] == "ATLANDI" and rows[4]["verdict"] == "HATA"


def test_quick_and_simulation_reach_the_figures(quick_report):
    """JURY-15 / MM-03: every PNG of a simulated quick result carries both tags."""
    rdir, results = quick_report
    flags = _png_text(rdir / "figures" / "sensitivity_screenshot.png").get("Description", "")
    assert SIM_TAG in flags and QUICK_TAG in flags
    assert figure_flags(results[0]) == f"{SIM_TAG} · {QUICK_TAG}"


def test_hardware_full_run_has_no_tags(tmp_path):
    pytest.importorskip("matplotlib")
    results = make_results(backend="soapy")
    for r in results:
        r.conditions["quick"] = False
        r.conditions["simulated"] = False
        r.figures = r.figures[:1]
    rdir = write_report(results, tmp_path, meta={"git_commit": "x"})
    assert "Description" not in _png_text(rdir / "figures" / "sensitivity_screenshot.png")
    rows = _read_csv(rdir / "summary.csv")
    assert rows[0]["quick"] == "false" and rows[0]["simulated"] == "false" and rows[0]["verdict"] == "GEÇTİ"
    md = (rdir / "summary.md").read_text(encoding="utf-8")
    assert "HIZLI MOD" not in md and "| Ölçüm modu | tam (--quick kullanılmadı) |" in md


def test_partial_quick_banner_lists_specs(tmp_path):
    results = _no_figures(make_results())
    results[1].conditions["quick"] = True
    rdir = write_report(results, tmp_path, meta={"git_commit": "x"})
    md = (rdir / "summary.md").read_text(encoding="utf-8")
    assert f"{QUICK_BANNER} — hızlı modda ölçülenler: dynamic_range" in md
    assert "| Ölçüm modu | KARIŞIK — hızlı modda: dynamic_range" in md


def test_blockers_without_target_and_without_apply_targets():
    """``blockers`` alone, and the targets rules even if apply_targets was never run."""
    r = MeasurementResult(spec_id="inst_bw", title="t", value=18.0, unit="MHz",
                          blockers=["kanallar koherent değil (ortak referans yok)"])
    assert verdict(r) == NOT_COMPARED and "koherent" in no_verdict_reason(r)
    q = MeasurementResult(spec_id="dynamic_range", title="t", value=70.0, unit="dB", conditions={"quick": True})
    q.evaluate_target(60.0, ">=")                       # passed=True, but quick → no verdict in the report
    assert q.passed is True and verdict(q) == NOT_COMPARED and REASON_QUICK in no_verdict_reason(q)
    u = MeasurementResult(spec_id="sensitivity", title="t", value=-40.0, unit="dBFS")
    u.evaluate_target(-100.0, "<=")
    assert verdict(u) == NOT_COMPARED and "Birim" in no_verdict_reason(u)
    skipped = MeasurementResult(spec_id="x", title="t", value=None, unit="", status="skipped", blockers=["b"])
    assert verdict(skipped) == "ATLANDI" and no_verdict_reason(skipped) == ""


# ------------------------------------------------------------------------- conditions / units
def test_short_conditions_are_spec_specific():
    """JURY-04: the main table shows the conditions that define each number."""
    scan = MeasurementResult(spec_id="scan_speed", title="t", value=0.5, unit="GHz/s", conditions={
        "fs_hz": 20e6, "start_hz": 325e6, "stop_hz": 825e6, "rbw_hz": 39147.51832455171, "averages": 4,
        "repeats": 1, "gain_db": 50.0})
    assert short_conditions(scan) == "aralık 325 MHz – 825 MHz; fs 20 MS/s; RBW 39.15 kHz; ort. 4; tekrar 1"
    rate = MeasurementResult(spec_id="max_rate", title="t", value=6.0, unit="MS/s", conditions={
        "fc_hz": 935e6, "fs_hz": 6e6, "duration_s": 0.6, "buffer_size": 65536, "interface": "usb2",
        "headline_mode": "pipeline"})
    assert short_conditions(rate) == "süre/hız 600 ms; buffer 65536; arayüz usb2; mod pipeline"
    lat = MeasurementResult(spec_id="latency", title="t", value=35.9, unit="ms", conditions={
        "headline_mode": "B", "n_events": 6, "transport": "tcp", "fs_hz": 4e6})
    assert short_conditions(lat) == "mod B; olay 6; taşıma tcp; fs 4 MS/s"
    df = MeasurementResult(spec_id="df_rms", title="t", value=0.5, unit="°", conditions={
        "fc_hz": 999.5e6, "f_target_hz": 999.9e6, "baseline_m": 0.15, "angles_deg": [-60, -40, -20, 0, 20, 40, 60],
        "repeats": 2, "coherent": True})
    assert short_conditions(df) == "f 999.9 MHz; d 0.150 m; θ -60.0 ° … 60.0 ° (7); tekrar 2; koherent evet"
    ibw = MeasurementResult(spec_id="inst_bw", title="t", value=16.9, unit="MHz", conditions={
        "fc_hz": 935e6, "configs": [[20e6, 20e6]], "rbw_hz": 9786.88, "gain_db": [40.0]})
    assert short_conditions(ibw) == "fc 935 MHz; fs / BW 20 MS/s / 20 MHz; RBW 9.787 kHz; G 40.0 dB"
    img = MeasurementResult(spec_id="image_rejection", title="t", value=55.0, unit="dB", conditions={
        "fcs_hz": [935e6], "fs_hz": 4e6, "offsets_frac": [0.05, 0.1, 0.2, 0.3, 0.4], "gain_db": [30.0]})
    assert short_conditions(img) == "fc 935 MHz; fs 4 MS/s; δ/fs 0.0500 … 0.400 (5); G 30.0 dB"


def test_unit_formatting_rules():
    """JURY-11: degrees keep significant digits, per-unit keys, qualifiers, nested dicts."""
    assert format_condition("rms_deg", 0.0028) == "0.0028 °"
    assert format_condition("max_abs_deg", 0.0123) == "0.012 °"
    assert format_condition("pd_slope_per_db", 0.6) == "0.600 /dB"
    assert format_condition("phase_drift_deg_per_s", -0.00978) == "-0.0098 °/s"
    assert format_condition("mb_per_s_at_max", 24.0) == "24.0 MB/s"
    assert format_condition("snr_db_mean", 86.6) == "86.6 dB"
    assert format_condition("ppm_estimate", 11.7) == "11.7 ppm"
    assert format_condition("fs_hz", 4e6) == "4 MS/s"
    assert format_condition("rates_hz", [1e6, 2.4e6]) == "1 MS/s, 2.4 MS/s"
    assert format_condition("spectrum_rate_hz", 10.0) == "10 Hz"                 # a frame rate, not fs
    assert format_condition("halfwidth_bins", 5) == "5 bin"
    assert format_condition("timing_share_pct", {"tune": 0.4182692535174094, "settle": 74.27}) == \
        "tune: 0.418 %; settle: 74.27 %"
    assert format_condition("mode_a_breakdown_mean_ms", {"processing_ms": 14.066, "publish_ms": 0.0842}) == \
        "processing_ms: 14.1 ms; publish_ms: 0.0842 ms"
    assert format_condition("best_config", {"fs_hz": 2e7, "bw_hz": 2e7}) == "fs_hz: 20 MS/s; bw_hz: 20 MHz"
    assert format_condition("modes", ["transport", "pipeline"]) == "transport, pipeline"
    assert format_condition("configs", [{"x": 0.123456789}]) == '[{"x": 0.123457}]'
    assert format_condition("clip_input", -35.57, unit="dBm") == "-35.6 dBm"
    assert format_value(44.467, "ms") == "44.5 ms"               # no spurious 5th digit (latency p95)
    assert format_value(0.0028, "°", 0.0005) == "0.0028 ° ± 0.0005 °"


def test_unit_hints_in_report_tables(tmp_path):
    r = MeasurementResult(spec_id="sensitivity", title="Duyarlılık", value=-131.8, unit="dBm",
                          extra={"sensitivity": -131.785, "clip_input": -35.576, "clip_input_unit": "dBm",
                                 "rms_deg": 0.0028})
    md = (write_report([r], tmp_path, meta={"git_commit": "x"}) / "summary.md").read_text(encoding="utf-8")
    assert "| `sensitivity` | -131.8 dBm |" in md and "| `clip_input` | -35.6 dBm |" in md
    assert "| `rms_deg` | 0.0028 ° |" in md


# ------------------------------------------------------------------------- raw tables
def test_raw_frequency_cells_keep_their_digits(tmp_path):
    """JURY-12: two tones 1.3 kHz apart at 935 MHz must not print as the same number."""
    r = MeasurementResult(spec_id="freq_resolution", title="t", value=1315.0, unit="Hz",
                          table=[{"df_hz": 1300.0, "f_a_hz": 935399350.0, "f_b_hz": 935400650.0,
                                  "noise_dbm_hz": -150.25}])
    md = (write_report([r], tmp_path, meta={"git_commit": "x"}) / "summary.md").read_text(encoding="utf-8")
    assert "| 1.3 kHz | 935.39935 MHz | 935.40065 MHz | -150.25 |" in md


def _wide_result(n_cols: int = 28, n_rows: int = 12) -> MeasurementResult:
    rows = []
    for i in range(n_rows):
        row = {"mode": "pipeline", "rate_msps": 1.0 + i, "fs_hz": (1.0 + i) * 1e6, "buffer_size": 65536}
        row.update({f"discarded_samples_{k}": 1_000_000 + 37 * i * k for k in range(n_cols - 5)})
        row["note"] = "kayıpsız; overflow yok; zaman aşımı yok" if i % 2 else ""
        rows.append(row)
    return MeasurementResult(spec_id="max_rate", title="t", value=6.0, unit="MS/s", table=rows)


def test_wide_raw_tables_are_split_to_fit_a4():
    """JURY-03: no raw table wider than the A4 text column; constants listed once; the first
    column repeated in every group."""
    r = _wide_result()
    lay = _raw_layout(r)
    assert len(lay.groups) > 1
    assert ("buffer_size", "65536") in lay.constant
    assert all(g[0] == lay.groups[0][0] for g in lay.groups)
    for g in lay.groups:
        width = sum(_raw_col_width(lay.cols[j], (row[j] for row in lay.shown)) for j in g)
        assert width <= report_mod._RAW_BUDGET_MM
    shown_cols = {lay.cols[j] for g in lay.groups for j in g} | {c for c, _ in lay.constant}
    assert shown_cols == set(lay.cols)                           # nothing silently lost


def test_wide_raw_table_html(tmp_path):
    rdir = write_report([_wide_result()], tmp_path, meta={"git_commit": "x"})
    page = (rdir / "report.html").read_text(encoding="utf-8")
    assert page.count('<table class="raw">') == len(_raw_layout(_wide_result()).groups)
    assert '<div class="rawwrap">' in page and "Tüm satırlarda aynı:" in page and "Sütun grubu 2/" in page
    assert "discarded_<wbr>samples_<wbr>1" in page
    assert '<td class="raw long">kayıpsız; overflow yok; zaman aşımı yok</td>' in page


def test_narrow_raw_table_is_not_split():
    lay = _raw_layout(make_results()[0])
    assert lay.groups == [list(range(len(lay.cols)))] and lay.constant == []
