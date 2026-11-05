"""Figure rendering: every kind produces a non-trivial, deterministic 1280×720 PNG."""

from __future__ import annotations

import builtins
import dataclasses
import math
import struct
import sys
from pathlib import Path

import numpy as np
import pytest

from eds.core.errors import BackendUnavailable, MeasurementError
from eds.measure.base import FigureSpec, Series
from eds.measure.plots import HEIGHT_PX, WIDTH_PX, render

pytest.importorskip("matplotlib")

FOOTER = {"version": "0.1.0", "date": "2026-11-05T10:00:00Z", "serial": "104473dc5993000a0f0026"}


def png_size(path: Path) -> tuple[int, int]:
    data = path.read_bytes()
    assert data[:8] == b"\x89PNG\r\n\x1a\n"
    w, h = struct.unpack(">II", data[16:24])
    return w, h


def png_text_chunks(path: Path) -> dict[str, str]:
    data, pos, out = path.read_bytes(), 8, {}
    while pos < len(data):
        (n,) = struct.unpack(">I", data[pos:pos + 4])
        kind = data[pos + 4:pos + 8]
        body = data[pos + 8:pos + 8 + n]
        if kind == b"tEXt":
            k, _, v = body.partition(b"\x00")
            out[k.decode("latin-1")] = v.decode("latin-1")
        elif kind == b"iTXt":
            k, _, rest = body.partition(b"\x00")
            out[k.decode("latin-1")] = rest[2:].split(b"\x00", 2)[-1].decode("utf-8")
        pos += 12 + n
    return out


def spectrum_fig(**kw) -> FigureSpec:
    rng = np.random.default_rng(7)
    f = np.linspace(913.0, 917.0, 4001)
    p = -95.0 + 2.0 * rng.standard_normal(f.size)
    p[2000] = -20.3
    args = dict(
        name="spec_tone", title="Spektrum ekran görüntüsü — ton @ 915 MHz (çğıİöşü)", kind="spectrum",
        series=[Series(x=f.tolist(), y=p.tolist(), label="dBFS")], xlabel="Frekans [MHz]",
        ylabel="Seviye [dBFS]", hlines=[(-80.0, "CFAR eşiği")], vlines=[(916.0, "imaj")],
        markers=[(915.0, -20.3, "ton")],
        info={"Center": "915 MHz", "Span": "4 MHz", "RBW": "1.4 kHz", "Avg": "16", "Ref": "-10 dBFS",
              "Window": "blackmanharris", **FOOTER},
        ylim=(-110.0, -10.0))
    args.update(kw)
    return FigureSpec(**args)


def xy_fig() -> FigureSpec:
    x = np.arange(-110.0, -90.0, 1.0)
    pd = 1.0 / (1.0 + np.exp(-(x + 101.0)))
    return FigureSpec(
        name="pd_curve", title="Pd eğrisi — duyarlılık", kind="xy",
        series=[Series(x=x.tolist(), y=pd.tolist(), label="ölçülen", style="marker", yerr=[0.03] * x.size),
                Series(x=x.tolist(), y=(pd * 0.98).tolist(), label="model", style="line"),
                Series(x=[-108.0, -104.0, -100.0], y=[0.2, 0.4, 0.6], label="bar", style="bar",
                       yerr=[0.05, 0.05, 0.05]),
                Series(x=[-108.0, -104.0, -100.0], y=[0.1, 0.3, 0.5], label="step", style="step")],
        xlabel="Giriş gücü [dBm]", ylabel="Pd", hlines=[(0.9, "Pd = 0.9")], vlines=[(-101.3, "MDS")],
        markers=[(-101.3, 0.9, "MDS = -101.3 dBm")], info={"RBW": "1 kHz", "Pfa": "1e-6", **FOOTER},
        xlim=(-111.0, -89.0), ylim=(-0.05, 1.05))


def hist_fig() -> FigureSpec:
    rng = np.random.default_rng(3)
    return FigureSpec(name="latency_hist", title="Uçtan uca gecikme dağılımı", kind="hist",
                      series=[Series(x=[], y=rng.gamma(4.0, 3.0, 2000).tolist(), label="gecikme")],
                      xlabel="Gecikme [ms]", ylabel="Adet", vlines=[(22.0, "p95 = 22.0 ms")], info=FOOTER)


def polar_fig() -> FigureSpec:
    rng = np.random.default_rng(5)
    ang = np.arange(-60.0, 61.0, 10.0)
    return FigureSpec(
        name="df_polar", title="DF kestirimleri", kind="polar",
        series=[Series(x=ang.tolist(), y=(10.0 + rng.normal(0.0, 1.0, ang.size)).tolist(), label="kestirim",
                       style="marker"),
                Series(x=list(range(0, 360, 5)), y=(8.0 + 4.0 * np.cos(np.deg2rad(np.arange(0, 360, 5)))).tolist(),
                       label="desen", style="line"),
                Series(x=[0.0, 90.0, 180.0, 270.0], y=[5.0, 6.0, 7.0, 8.0], label="sektör", style="bar")],
        vlines=[(30.0, "gerçek 30°")], hlines=[(10.0, "10 dB")], markers=[(30.0, 12.0, "hedef")],
        xlabel="Azimut [°]", ylabel="Seviye [dB]", info={"Taban hattı": "0.25 m", **FOOTER})


@pytest.mark.parametrize("make", [spectrum_fig, xy_fig, hist_fig, polar_fig], ids=["spectrum", "xy", "hist", "polar"])
def test_every_kind_renders_nontrivial_png(tmp_path, make):
    fig = make()
    out = render(fig, tmp_path / fig.name)
    assert out == tmp_path / f"{fig.name}.png"
    assert out.stat().st_size > 10_000
    assert png_size(out) == (WIDTH_PX, HEIGHT_PX)
    text = png_text_chunks(out)
    assert text.get("Title") == fig.title
    assert text.get("Software", "").startswith("EDS ")
    assert not any("date" in k.lower() or "time" in k.lower() for k in text)


def test_spectrum_screen_is_dark_with_bright_trace(tmp_path):
    from matplotlib.image import imread

    img = imread(render(spectrum_fig(), tmp_path / "s.png"))[..., :3]
    bg = np.array([0x0B, 0x0F, 0x14]) / 255.0
    dark = np.all(np.abs(img - bg) < 0.02, axis=-1).mean()
    assert dark > 0.4                                        # mostly the #0b0f14 screen
    yellow = (img[..., 0] > 0.9) & (img[..., 1] > 0.75) & (img[..., 2] < 0.2)
    assert yellow.sum() > 2000                               # bright (yellow) trace present


def test_light_kinds_have_white_background(tmp_path):
    from matplotlib.image import imread

    img = imread(render(xy_fig(), tmp_path / "x.png"))[..., :3]
    assert np.all(img > 0.99, axis=-1).mean() > 0.5


def test_render_is_deterministic(tmp_path):
    a = render(spectrum_fig(), tmp_path / "a.png").read_bytes()
    b = render(spectrum_fig(), tmp_path / "b.png").read_bytes()
    assert a == b
    c = render(hist_fig(), tmp_path / "c.png").read_bytes()
    d = render(hist_fig(), tmp_path / "d.png").read_bytes()
    assert c == d


def test_footer_changes_image_only_when_keys_present(tmp_path):
    base = spectrum_fig(info={"Center": "915 MHz"})
    with_footer = spectrum_fig(info={"Center": "915 MHz", "serial": "SN-1"})
    a = render(base, tmp_path / "a.png").read_bytes()
    b = render(with_footer, tmp_path / "b.png").read_bytes()
    assert a != b


def test_no_figure_leak(tmp_path):
    import gc

    from matplotlib.figure import Figure

    def live_figures() -> int:
        gc.collect()
        return sum(1 for o in gc.get_objects() if isinstance(o, Figure))

    plt = sys.modules.get("matplotlib.pyplot")
    before = len(plt.get_fignums()) if plt else 0
    live_before = live_figures()
    render(xy_fig(), tmp_path / "x.png")
    render(polar_fig(), tmp_path / "p.png")
    assert live_figures() == live_before
    if plt:
        assert len(plt.get_fignums()) == before


def test_degenerate_inputs_still_render(tmp_path):
    empty = FigureSpec(name="empty", title="Boş", kind="xy", series=[])
    nan_series = FigureSpec(name="nan", title="NaN", kind="xy",
                            series=[Series(x=[1.0, 2.0, 3.0], y=[1.0, float("nan"), None])])  # type: ignore[list-item]
    const_hist = FigureSpec(name="const", title="Sabit", kind="hist", series=[Series(x=[], y=[5.0] * 20)])
    empty_hist = FigureSpec(name="eh", title="Boş hist", kind="hist", series=[Series(x=[], y=[])])
    no_data_spec = FigureSpec(name="es", title="Boş spektrum", kind="spectrum", series=[])
    log_xy = FigureSpec(name="log", title="Log", kind="xy", logx=True, logy=True,
                        series=[Series(x=[0.5, 1, 2, 4, 8], y=[1, 2, 5, 20, 400], style="bar")])
    long_title = FigureSpec(name="long", title="Çok uzun başlık " * 12, kind="xy",
                            series=[Series(x=[0, 1], y=[0, 1])])
    for fig in (empty, nan_series, const_hist, empty_hist, no_data_spec, log_xy, long_title):
        out = render(fig, tmp_path / f"{fig.name}.png")
        assert png_size(out) == (WIDTH_PX, HEIGHT_PX)


def test_polar_options_and_suffix(tmp_path):
    fig = polar_fig()
    fig.info.update({"theta_zero": "E", "theta_direction": "ccw"})
    out = render(fig, tmp_path / "sub" / "polar.v2")
    assert out.name == "polar.v2.png" and out.is_file()


@pytest.mark.parametrize("fig, match", [
    (FigureSpec(name="k", title="t", kind="pie"), "Bilinmeyen grafik türü"),
    (FigureSpec(name="s", title="t", series=[Series(x=[1], y=[1], style="area")]), "bilinmeyen stil"),
    (FigureSpec(name="l", title="t", series=[Series(x=[1, 2], y=[1])]), "uzunlukları farklı"),
    (FigureSpec(name="e", title="t", series=[Series(x=[1, 2], y=[1, 2], yerr=[1])]), "yerr"),
    (FigureSpec(name="p", title="t", kind="polar", info={"theta_zero": "X"}), "theta_zero"),
    (FigureSpec(name="y", title="t", ylim=(0.0, float("nan"))), "ylim"),
])
def test_invalid_specs_raise_measurement_error(tmp_path, fig, match):
    with pytest.raises(MeasurementError, match=match):
        render(fig, tmp_path / "x.png")


def test_dpi_range_and_text_scale(tmp_path):
    with pytest.raises(MeasurementError):
        render(xy_fig(), tmp_path / "x.png", dpi=300)
    out = render(xy_fig(), tmp_path / "y.png", dpi=100)
    assert png_size(out) == (WIDTH_PX, HEIGHT_PX)


def test_missing_matplotlib_raises_backend_unavailable(tmp_path, monkeypatch):
    real_import = builtins.__import__

    def fake_import(name, *args, **kwargs):
        if name == "matplotlib" or name.startswith("matplotlib."):
            raise ImportError("No module named 'matplotlib'")
        return real_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", fake_import)
    with pytest.raises(BackendUnavailable, match="pip install matplotlib"):
        render(xy_fig(), tmp_path / "x.png")
    from eds.measure import plots

    assert plots.available() is False


# ----------------------------------------------------------------------------- spectrum instrument scales
from eds.measure import plots  # noqa: E402
from eds.measure.plots import ROLE_COLORS, SA_DB_PER_DIV, sa_scale, series_role  # noqa: E402


@pytest.mark.parametrize(("hint", "top", "unit", "expected"), [
    ((-120.0, 10.0), -3.0, "dBFS", (0.0, 20.0, 8)),         # SFDR: never above full scale, 160 dB deep
    ((-100.0, -0.0), -11.6, "dBFS@50dB", (0.0, 10.0, 10)),   # sweep: no "-0"
    ((-190.0, -90.0), -101.3, "dBm", (-90.0, 10.0, 10)),     # absolute units are not capped
    ((-50.0, 10.0), -14.8, "dBFS", (0.0, 10.0, 8)),          # 60 dB zoom → 8 × 10 dB
    ((-40.0, 0.0), -3.0, "dBFS", (0.0, 5.0, 8)),
    (None, -23.4, "dBFS", (-10.0, 10.0, 10)),                # no hint: Ref 5..15 dB above the peak
    (None, 2.5, "dBFS", (10.0, 10.0, 10)),                   # trace above 0 dBFS stays on screen
    ((-12.5, 0.0), -0.4, "dB", (0.0, 2.0, 8)),
])
def test_sa_scale_uses_instrument_values(hint, top, unit, expected):
    ref, div, n = sa_scale(hint, top, unit)
    assert (ref, div, n) == expected
    assert div in SA_DB_PER_DIV and n in (8, 10)
    assert math.copysign(1.0, ref) > 0 or ref < 0               # never -0.0
    assert ref >= top and (ref - div * n) <= (hint[0] if hint else ref - 100.0)


def test_spectrum_tick_labels_and_x_range():
    assert plots._fmt_tick(-0.0) == "0" and plots._fmt_tick(-160.0) == "-160" and plots._fmt_tick(0.25) == "0.25"
    fig = spectrum_fig(info={"Center": "915 MHz", "Span": "4 MHz"})
    fx = np.array([913.0, 916.998047])                           # fftshift grid: +fs/2 excluded
    assert plots._sa_x_range(fig, fx, 1e6) == (913.0, 917.0)     # Start/Stop = Center ∓ Span/2
    fig = spectrum_fig(info={"Center": "915 MHz", "Span": "40 MHz"})
    assert plots._sa_x_range(fig, fx, 1e6) == (913.0, 916.998047)  # Span not matching the data: data extent
    assert plots._parse_hz("1.00049927 GHz") == pytest.approx(1.00049927e9) and plots._parse_hz("auto") is None


def _sfdr_fig(markers: list[tuple[float, float, str]]) -> FigureSpec:
    rng = np.random.default_rng(1)
    f = 935.0 + (np.arange(4096) - 2048) * 4.0 / 4096
    p = -104.0 + 2.0 * rng.standard_normal(f.size)
    p[2525], p[1440] = -3.0, -58.1
    return FigureSpec(name="sfdr", title="SFDR", kind="spectrum", series=[Series(x=f.tolist(), y=p.tolist(), label="dBFS")],
                      xlabel="Frekans [MHz]", ylabel="Seviye [dBFS]", markers=markers,
                      info={"Center": "935 MHz", "Span": "4 MHz", "RBW": "1 kHz", "Ref": "10 dBFS"}, ylim=(-120.0, 10.0))


def test_marker_readout_never_hides_a_marker(tmp_path):
    """JURY-05: the carrier marker near the top right stays visible (readout is a table strip)."""
    from matplotlib.image import imread

    f = 935.0 + (np.arange(4096) - 2048) * 4.0 / 4096
    one = imread(render(_sfdr_fig([(f[1440], -58.1, "imaj")]), tmp_path / "a.png"))[..., :3]
    two = imread(render(_sfdr_fig([(f[2525], -3.0, "taşıyıcı"), (f[1440], -58.1, "imaj")]), tmp_path / "b.png"))[..., :3]
    white = [int(np.all(img > 0.995, axis=-1).sum()) for img in (one, two)]   # diamond fills are pure white
    assert white[0] > 50 and white[1] >= 1.8 * white[0]


# ----------------------------------------------------------------------------- semantic colours
@pytest.mark.parametrize(("label", "role", "shown"), [
    ("GEÇTİ", "pass", "GEÇTİ"), ("GEÇTİ (DSP hattından çıkarım) — yalnız taşıma", "pass", None),
    ("KALDI — yalnız taşıma", "fail", None), ("kaldı", "fail", None), ("Kırpma", "warn", None),
    ("≥ 1 dB sıkışma", "warn", None), ("kayıp örnek — yalnız taşıma", "", None), ("Pd", "", None),
    ("ölçülen {fail}", "fail", "ölçülen"), ("{ref} ideal", "ref", "ideal"),
])
def test_series_role_from_label(label, role, shown):
    assert series_role(Series(x=[], y=[], label=label)) == (role, label if shown is None else shown)


def test_series_role_attribute_wins():
    s = Series(x=[], y=[], label="GEÇTİ")
    s.role = "warn"                     # forward compatible with an optional Series.role field
    assert series_role(s)[0] == "warn"


def test_pass_fail_colours_do_not_depend_on_series_order():
    """JURY-06: with transport-only trials KALDI is series 2 — it must still be red, not green."""
    series = [Series(x=[1], y=[1], label="gerekli veri hızı"), Series(x=[1], y=[1], label="GEÇTİ — yalnız taşıma"),
              Series(x=[1], y=[1], label="KALDI — yalnız taşıma", style="marker"), Series(x=[1], y=[1], label="Kırpma")]
    styles = plots._series_styles(series)
    assert [s.color for s in styles[1:]] == [ROLE_COLORS["pass"], ROLE_COLORS["fail"], ROLE_COLORS["warn"]]
    assert styles[0].color not in ROLE_COLORS.values() and styles[0].color not in ("#1baf7a", "#008300", "#e34948")
    plain = plots._series_styles([Series(x=[1], y=[1], label="a"), Series(x=[1], y=[1], label="b")])
    assert [s.color for s in plain] == [plots.PALETTE[0], plots.PALETTE[1]]     # no roles: unchanged


def test_role_colours_rendered(tmp_path):
    from matplotlib.image import imread

    fig = FigureSpec(name="thr", title="t", kind="xy", series=[
        Series(x=[1, 2, 3], y=[1, 2, 3], label="gerekli", style="line"),
        Series(x=[1, 2], y=[1, 2], label="GEÇTİ", style="marker"),
        Series(x=[3], y=[3], label="KALDI", style="marker")])
    img = (imread(render(fig, tmp_path / "t.png"))[..., :3] * 255).round().astype(int)

    def count(hex_color: str) -> int:
        rgb = np.array([int(hex_color[i:i + 2], 16) for i in (1, 3, 5)])
        return int(np.all(np.abs(img - rgb) <= 3, axis=-1).sum())

    assert count(ROLE_COLORS["pass"]) > 50 and count(ROLE_COLORS["fail"]) > 30
    assert count("#1baf7a") == 0                                 # the categorical green is not used


# ----------------------------------------------------------------------------- layout / footer flags
def test_flags_footer_and_png_description(tmp_path):
    from matplotlib.image import imread

    flags = "SİMÜLASYON · HIZLI MOD - KTR'de kullanılmaz"
    for make in (xy_fig, spectrum_fig):
        fig = make()
        fig.info["flags"] = flags
        out = render(fig, tmp_path / f"{fig.name}.png")
        assert png_text_chunks(out)["Description"] == flags
        img = imread(out)[..., :3]
        corner = img[-25:, :300]
        red = (corner[..., 0] > 0.75) & (corner[..., 1] < 0.5) & (corner[..., 2] < 0.5)
        assert red.sum() > 100                                   # bold red tag, bottom left
    assert "Description" not in png_text_chunks(render(xy_fig(), tmp_path / "plain.png"))


@pytest.mark.parametrize("kind", ["xy", "spectrum"])
def test_long_title_fits_the_canvas(tmp_path, kind):
    """JURY-07: a ~95-character title is wrapped by rendered width instead of being cut."""
    from matplotlib.image import imread

    title = "Birleştirilmiş tarama spektrumu (max-hold) — 64 adım × 7.988 MHz, 0.95 s, 0.529 GHz/s, 3 tespit"
    fig = spectrum_fig(title=title) if kind == "spectrum" else dataclasses.replace(xy_fig(), title=title)
    img = imread(render(fig, tmp_path / "t.png"))[..., :3]
    edge = img[:100, -10:]
    text_px = (edge > 0.6).any(axis=-1) if kind == "spectrum" else (edge < 0.5).any(axis=-1)
    assert text_px.sum() == 0


def test_axis_label_not_pushed_off_canvas(tmp_path):
    """JURY-07: tick labels like −0.006 must not push the y label past the left edge."""
    from matplotlib.image import imread

    ang = np.arange(-60.0, 61.0, 10.0)
    fig = FigureSpec(name="dferr", title="DF hata", kind="xy",
                     series=[Series(x=ang.tolist(), y=(0.006 * np.sin(ang / 20.0)).tolist(), style="marker")],
                     xlabel="Gerçek açı [°]", ylabel="Hata θ̂ − θ [°]")
    img = imread(render(fig, tmp_path / "e.png"))[..., :3]
    assert not (img[:, :4] < 0.6).any()
