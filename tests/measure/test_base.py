"""eds.measure.base: declarative figure fields (Series.role, FigureSpec.xticks) and the
serialisation of results (report JSON, bus payload)."""

from __future__ import annotations

import json

from eds.measure.base import FigureSpec, MeasurementResult, Series


def test_series_role_defaults_to_categorical_and_is_serialised():
    s = Series(x=[1.0], y=[2.0])
    assert s.role == ""
    r = Series(x=[1.0], y=[2.0], label="GEÇTİ", style="bar", role="pass")
    fig = FigureSpec(name="f", title="t", series=[s, r])
    res = MeasurementResult(spec_id="max_rate", title="Taşmasız maksimum örnekleme hızı", value=6.0, unit="MS/s",
                            figures=[fig])
    d = res.to_dict()
    assert [x["role"] for x in d["figures"][0]["series"]] == ["", "pass"]
    json.dumps(d)                                               # JSON-safe


def test_figure_xticks_default_empty_and_independent():
    a = FigureSpec(name="a", title="A")
    b = FigureSpec(name="b", title="B")
    assert a.xticks == [] and a.xticks is not b.xticks          # no shared mutable default
    c = FigureSpec(name="c", title="C", kind="xy", series=[Series(x=[0.0, 1.0], y=[3.0, 4.0], style="bar")],
                   xticks=[(0.0, "Mod A"), (1.0, "Mod B")])
    d = MeasurementResult(spec_id="latency", title="Uçtan uca gecikme", value=1.0, unit="ms",
                          figures=[c]).to_dict()
    assert d["figures"][0]["xticks"] == [[0.0, "Mod A"], [1.0, "Mod B"]]


def test_without_figures_drops_them():
    res = MeasurementResult(spec_id="latency", title="Uçtan uca gecikme", value=1.0, unit="ms",
                            figures=[FigureSpec(name="a", title="A")])
    assert "figures" not in res.to_dict(with_figures=False)
