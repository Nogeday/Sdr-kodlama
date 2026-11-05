"""Tests of the measurement session (eds.measure.session.run_suite): isolation between
measurements (STR-06), partial report on interruption, target evaluation with blockers.

Ground truth: real measurements on the non-real-time simulator (inst_bw, freq_resolution:
fast) and injected failures of the baseline reset, the bus, the target evaluation and the
caller's progress hook.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

import eds.measure.session as session
from eds.core.errors import StreamError
from eds.measure.base import MeasurementResult
from eds.measure.targets import REASON_QUICK, VERDICT_NOTE_KEY

from .spec_fixtures_b import single_sim_app

TARGETS = """
[inst_bw]
target = 1.0
op = ">="

[freq_resolution]
target = 1e6
op = "<="

[sensitivity]
target = -100.0
op = "<="
"""
SPECS = ["inst_bw", "freq_resolution", "inst_bw"]


def _app(tmp_path: Path):
    tfile = tmp_path / "targets.toml"
    tfile.write_text(TARGETS, encoding="utf-8")
    app = single_sim_app(tmp_path, realtime=False, spec_id="inst_bw")
    app.measure.targets_file = str(tfile)
    return app


def _results_json(rdir: Path) -> dict:
    return json.loads((rdir / "results.json").read_text(encoding="utf-8"))


def test_reset_error_between_measurements_does_not_discard_completed_results(tmp_path, monkeypatch):
    """STR-06 (exp6): a device error while restoring the baseline before measurement k
    is that measurement's failure; the others run and the report is written."""
    real_reset = session.reset_baseline
    calls = {"n": 0}

    def flaky_reset(ctx):
        calls["n"] += 1
        if calls["n"] == 2:
            raise StreamError("USB: libiio refill hatası (-110)")
        real_reset(ctx)

    monkeypatch.setattr(session, "reset_baseline", flaky_reset)
    seen: list[str] = []
    suite = session.run_suite(_app(tmp_path), SPECS, quick=True, on_result=lambda r: seen.append(r.status))
    assert [r.status for r in suite.results] == ["ok", "failed", "ok"] == seen
    bad = suite.results[1]
    assert bad.spec_id == "freq_resolution" and bad.title and bad.unit == "Hz" and bad.value is None
    assert "Temel duruma" in bad.error and "refill" in bad.error and "çalıştırılmadı" in bad.summary
    assert bad.conditions["quick"] is True and bad.conditions["simulated"] is True
    assert suite.aborted == "" and not suite.ok
    assert suite.report_dir is not None and suite.report_dir.is_dir()
    items = _results_json(suite.report_dir)["results"]
    assert [i["status"] for i in items] == ["ok", "failed", "ok"]
    # quick results keep the target but get no verdict (MM-03/MM-04)
    ok = suite.results[0]
    assert ok.target == 1.0 and ok.passed is None and REASON_QUICK in ok.extra[VERDICT_NOTE_KEY]


def test_interrupted_suite_writes_partial_report_and_reraises(tmp_path, monkeypatch):
    real_run = session.run_measurement
    calls = {"n": 0}

    def interrupted(sid, ctx, **kw):
        calls["n"] += 1
        if calls["n"] == 2:
            raise KeyboardInterrupt
        return real_run(sid, ctx, **kw)

    monkeypatch.setattr(session, "run_measurement", interrupted)
    app = _app(tmp_path)
    with pytest.raises(KeyboardInterrupt):
        session.run_suite(app, SPECS, quick=True)
    (rdir,) = [p for p in Path(app.measure.out_dir).iterdir() if p.is_dir()]
    doc = _results_json(rdir)
    assert [(i["spec_id"], i["status"]) for i in doc["results"]] == [
        ("inst_bw", "ok"), ("freq_resolution", "failed"), ("inst_bw", "skipped")]
    assert "yarıda kesildi" in doc["results"][1]["error"]
    md = (rdir / "summary.md").read_text(encoding="utf-8")
    assert session.ABORT_META_KEY in md and "YARIDA KESİLDİ (KeyboardInterrupt) — 1/3" in md


def test_interruption_before_any_result_writes_nothing(tmp_path, monkeypatch):
    def interrupted(sid, ctx, **kw):
        raise KeyboardInterrupt

    monkeypatch.setattr(session, "run_measurement", interrupted)
    app = _app(tmp_path)
    with pytest.raises(KeyboardInterrupt):
        session.run_suite(app, ["inst_bw"], quick=True)
    out = Path(app.measure.out_dir)
    assert not out.exists() or not any(p.is_dir() for p in out.iterdir())


class _BrokenPublisher:
    closed = False

    def publish(self, topic, payload):
        raise OSError("ZMQ: adres kullanımda")

    def close(self):
        _BrokenPublisher.closed = True


def test_bus_target_and_hook_errors_are_isolated(tmp_path, monkeypatch):
    import eds.bus.pubsub as pubsub
    import eds.measure.targets as targets

    monkeypatch.setattr(pubsub, "publisher_from_config", lambda *a, **k: _BrokenPublisher())

    def broken_apply(results, tg):
        raise ValueError("bozuk hedef")

    monkeypatch.setattr(targets, "apply_targets", broken_apply)

    def hook(res: MeasurementResult) -> None:
        raise RuntimeError("ilerleme çıktısı kapandı")

    suite = session.run_suite(_app(tmp_path), ["inst_bw", "freq_resolution"], quick=True, publish=True,
                              on_result=hook, write=False)
    assert [r.status for r in suite.results] == ["ok", "ok"] and suite.ok
    for r in suite.results:
        assert "OSError" in r.extra["publish_error"]
        assert r.passed is None and "Hedef değerlendirilemedi" in r.extra[VERDICT_NOTE_KEY]
    assert _BrokenPublisher.closed and suite.report_dir is None


def test_full_mode_simulated_result_gets_verdict(tmp_path):
    """Quick results are blocked; a full-mode result of the (real-time-independent) bandwidth
    measurement on the simulator is compared with its target."""
    app = _app(tmp_path)
    suite = session.run_suite(app, ["inst_bw"], quick=False, write=False)
    (res,) = suite.results
    assert res.status == "ok" and res.conditions["quick"] is False and res.conditions["simulated"] is True
    assert res.passed is True and VERDICT_NOTE_KEY not in res.extra
