"""``eds profiles | devices | info | rx | sweep`` — hardware-facing commands."""

from __future__ import annotations

import argparse
import dataclasses
import json
import math
import sys
from pathlib import Path
from typing import Any

import numpy as np

from .. import __version__
from ..bus.envelope import sanitize
from ..core.config import AppConfig, DeviceConfig
from ..core.errors import ConfigError
from ..core.profile import DeviceProfile, list_profiles, load_profile
from ..core.timeutil import iso_utc, now_ns
from ..core.units import fmt_hz
from .util import compact_json, load_app, parse_kv_list, print_table


# =============================================================================== profiles
def _fmt_range(lo: float, hi: float) -> str:
    if not math.isfinite(hi):
        return f"≥ {fmt_hz(lo)}"
    return f"{fmt_hz(lo)} – {fmt_hz(hi)}"


def cmd_profiles(args: argparse.Namespace) -> int:
    rows = []
    for name in list_profiles():
        p = load_profile(name)
        rows.append([p.name, p.chip, p.status, _fmt_range(p.freq_min_hz, p.freq_max_hz),
                     fmt_hz(p.bw_max_hz) if math.isfinite(p.bw_max_hz) else "—",
                     f"{p.rx_channels} RX / {p.tx_channels} TX", p.interface])
    if not rows:
        raise ConfigError("Hiç cihaz profili bulunamadı (config/profiles/*.toml)")
    print_table(["Profil", "Çip", "Durum", "Frekans aralığı", "Maks. BW", "Kanallar", "Arayüz"], rows)
    return 0


# =============================================================================== devices
def _profile_for_driver(driver: str, app: AppConfig) -> str:
    """Profile for an enumerated Soapy device: the configured one if its driver matches,
    else the first *supported* profile declaring that Soapy driver."""
    configured = app.devices[0].profile
    try:
        if load_profile(configured).soapy_driver == driver:
            return configured
    except ConfigError:
        pass                                    # configured profile unknown: search below
    for name in list_profiles():
        p = load_profile(name)
        if p.soapy_driver == driver and p.status == "supported":
            return name
    return configured


def _probe_configs(entries: list[dict[str, str]], backend: str, app: AppConfig) -> list[DeviceConfig]:
    out: list[DeviceConfig] = []
    for i, e in enumerate(entries):
        if backend == "sim":
            base = app.devices[i] if i < len(app.devices) else app.devices[0]
            out.append(DeviceConfig(name=base.name if i < len(app.devices) else f"sim{i}", backend="sim",
                                    profile=base.profile, channels=list(base.channels), sim=dict(base.sim)))
        else:
            out.append(DeviceConfig(name=f"dev{i}", backend="soapy",
                                    profile=_profile_for_driver(e.get("driver", ""), app),
                                    args={str(k): str(v) for k, v in e.items()}, control_uri="none"))
    return out


def cmd_devices(args: argparse.Namespace) -> int:
    from ..hal.factory import enumerate_devices, open_device

    app = load_app(args)
    backends = [args.backend] if args.backend else sorted({d.backend for d in app.devices})
    filt = parse_kv_list(args.args, "--args", literal=False)
    total = 0
    for backend in backends:
        entries = enumerate_devices(backend, filt)
        print(f"Backend '{backend}': {len(entries)} cihaz")
        for i, e in enumerate(entries):
            print(f"  [{i}] " + ", ".join(f"{k}={v}" for k, v in e.items()))
        total += len(entries)
        if not args.probe:
            continue
        for dc in _probe_configs(entries, backend, app):
            print(f"\n--- Yoklama: {dc.name} ({dc.backend}, profil {dc.profile}) ---")
            dev = open_device(dc, app)
            try:
                info = {"describe": dev.describe(), "runtime_limits": dev.runtime_limits(),
                        "sensors": dev.read_sensors()}
            finally:
                dev.close()
            print(json.dumps(sanitize(info), indent=2, ensure_ascii=False))
    if total == 0:
        print("Hiç cihaz bulunamadı (USB bağlantısı, udev kuralı ve SoapySDR modüllerini kontrol edin: "
              "SoapySDRUtil --find)", file=sys.stderr)
        return 1
    return 0


# =============================================================================== info
def _profile_lines(p: DeviceProfile, fc: float) -> list[str]:
    gr = p.gain_range(fc)
    return [
        f"  Profil {p.name} ({p.chip}, {p.status}): {p.description}".rstrip(": "),
        f"    Frekans {_fmt_range(p.freq_min_hz, p.freq_max_hz)}; analog BW {_fmt_range(p.bw_min_hz, p.bw_max_hz)}; "
        f"fs {p.sample_rate_min_hz / 1e6:.4g} – {p.sample_rate_max_hz / 1e6:.4g} MS/s",
        f"    Kazanç @ {fmt_hz(fc)}: {gr.min_db:g} … {gr.max_db:g} dB (adım {gr.step_db:g} dB); "
        f"kanallar {p.rx_channels} RX / {p.tx_channels} TX; ADC {p.adc_bits} bit",
        f"    Referans {fmt_hz(p.ref_clock_hz)} ±{p.ref_clock_ppm:g} ppm; arayüz {p.interface} "
        f"(teorik {p.interface_max_bytes_per_s / 1e6:.1f} MB/s, pratik ~{p.interface_practical_bytes_per_s / 1e6:.0f} MB/s); "
        f"retune sonrası bayat tampon {p.kernel_buffers}",
    ]


def cmd_info(args: argparse.Namespace) -> int:
    from ..dsp.windows import window_info
    from ..measure.common import welch_len

    app = load_app(args)
    rx, dsp, det, sw, bus, cal, me = app.rx, app.dsp, app.detect, app.sweep, app.bus, app.cal, app.measure
    print(f"EDS {__version__} — etkin konfigürasyon")
    print(f"Kaynak: {app.source_path if app.source_path != '<defaults>' else 'yerleşik varsayılanlar (tek sim cihazı)'}")
    print("Cihazlar:")
    for d in app.devices:
        extra = f"  args={compact_json(d.args)}" if d.args else ""
        sim = f"  sim={compact_json(d.sim)}" if d.sim and d.backend == "sim" else ""
        ctl = f"  control_uri={d.control_uri}" if d.control_uri else ""
        iso = f"  isolation={d.isolation}" if d.isolation != "auto" else ""
        print(f"  {d.name}: backend={d.backend}  profil={d.profile}  kanallar={d.channels}{extra}{ctl}{iso}{sim}")
    if any(d.backend == "soapy" for d in app.devices) or me.source == "pluto_tx":
        from ..hal.factory import isolation_plan, pluto_tx_in_process

        plan = ", ".join(f"{n}={m}" for n, m in isolation_plan(app).items())
        src = ""
        if me.source == "pluto_tx":
            src = "; pluto_tx kaynağı " + ("bu süreçte (source_args.isolate = false)" if pluto_tx_in_process(app)
                                           else "ayrı süreçte")
        print(f"Süreç yalıtımı (birlikte açılınca; SoapyPlutoSDR süreç başına tek Pluto): {plan}{src}")
    rbw = window_info(dsp.window, int(dsp.nfft)).enbw_bins * rx.sample_rate_hz / dsp.nfft
    n_win = welch_len(int(dsp.nfft), int(dsp.averages), float(dsp.overlap))
    gain = f"{rx.gain_db:g} dB" if rx.gain_mode in ("manual", "soft_agc") else "donanım AGC"
    print(f"RX: fc {fmt_hz(rx.center_freq_hz)}, fs {rx.sample_rate_hz / 1e6:.4g} MS/s, BW {fmt_hz(rx.bandwidth_hz)}, "
          f"kazanç {rx.gain_mode} {gain}, blok {rx.block_size}, tampon {rx.buffer_size}, kuyruk {rx.queue_blocks}, "
          f"{rx.stream_format}, tracking dc/rf_dc/quad = {rx.dc_tracking}/{rx.rf_dc_tracking}/{rx.quad_tracking}")
    print(f"DSP: nfft {dsp.nfft}, {dsp.window}, örtüşme {dsp.overlap:g}, {dsp.averages} ort. ({dsp.avg_mode}) → "
          f"RBW {fmt_hz(rbw)}, analiz penceresi {n_win} örnek = {n_win / rx.sample_rate_hz * 1e3:.2f} ms; "
          f"kullanılabilir bant %{100 * dsp.usable_fraction:g}, DC çentiği ±{dsp.dc_notch_bins} bin")
    print(f"Tespit: {det.method}, Pfa {det.pfa:g}, koruma {det.guard_bins}, eğitim {det.train_bins}, "
          f"min SNR {det.min_snr_db:g} dB, birleştirme {det.merge_gap_bins} bin")
    sw_rbw = (sw.rbw_hz if sw.rbw_hz > 0 else window_info(sw.window, int(sw.nfft)).enbw_bins * sw.sample_rate_hz / sw.nfft)
    print(f"Tarama: {fmt_hz(sw.start_hz)} – {fmt_hz(sw.stop_hz)}, fs {sw.sample_rate_hz / 1e6:.4g} MS/s, "
          f"RBW ≈ {fmt_hz(sw_rbw)}, {sw.averages} ort., kazanç {sw.gain_mode} {sw.gain_db:g} dB, "
          f"DC kaçınma {'açık' if sw.dc_avoid else 'kapalı'}")
    if bus.mode == "direct":
        eps = f"yayın {bus.pub_endpoint} (bind), abone {bus.sub_endpoint}, kontrol {bus.ctl_endpoints or 'kapalı'}"
    else:
        eps = f"proxy frontend {bus.proxy_frontend}, backend {bus.proxy_backend}"
    print(f"Bus: {bus.mode}: {eps}; şema doğrulama {'açık' if bus.validate else 'kapalı'}; durum {bus.status_period_s:g} s, "
          f"spektrum ≤ {bus.spectrum_rate_hz:g} Hz / ≤ {bus.spectrum_max_bins} nokta")
    ovr = f"{cal.ppm_override:+.4f}" if math.isfinite(cal.ppm_override) else "—"
    print(f"Kalibrasyon: dizin {cal.dir or '<repo>/cal'}, ppm_mode {cal.ppm_mode}, ppm_override {ovr}, "
          f"güç kalibrasyonu {'açık' if cal.use_power_cal else 'kapalı'}, sıcaklık uyarısı ±{cal.temp_warn_delta_c:g} °C")
    print(f"Ölçüm: kaynak {me.source}, çıktı {me.out_dir or '<repo>/reports'}, hedefler {me.targets_file}")
    n_err = 0
    print("Profil sınırları ve RX ayarlarının doğrulaması:")
    for d in app.devices:
        p = load_profile(d.profile)
        for line in _profile_lines(p, rx.center_freq_hz):
            print(line)
        errs, warns = p.check_rx(
            center_freq_hz=rx.center_freq_hz, sample_rate_hz=rx.sample_rate_hz, bandwidth_hz=rx.bandwidth_hz,
            gain_db=rx.gain_db if rx.gain_mode in ("manual", "soft_agc") else None, n_channels=len(d.channels))
        for e in errs:
            print(f"    HATA: {e}")
        for w in warns:
            print(f"    UYARI: {w}")
        if not errs and not warns:
            print("    Tamam: RX ayarları profil sınırları içinde.")
        n_err += len(errs)
    if n_err:
        print(f"{n_err} profil ihlali — bu konfigürasyonla RX başlatılamaz.", file=sys.stderr)
        return 1
    return 0


# =============================================================================== rx
#: Overflow counter text when the driver cannot detect sample loss (``overflow_source = "none"``).
OVERFLOW_UNKNOWN = "bilinmiyor (sürücü raporlamaz)"


def _fmt_gains(gains: Any) -> str:
    if isinstance(gains, (list, tuple)) and gains:
        return "/".join(f"{float(g):g}" for g in gains)
    return str(gains)


def _overflows_text(stream: dict[str, Any]) -> str:
    """Overflow counter for the operator: unknown (not 0) when the driver cannot detect loss."""
    if stream.get("overflow_source") == "none":
        return OVERFLOW_UNKNOWN
    return str(stream.get("overflows", 0))


def _status_line(name: str, d: dict[str, Any], node: Any) -> str:
    st = d.get("stream", {})
    ppm = d.get("ppm") or {}
    sensors = d.get("sensors") or {}
    temp = sensors.get("temperature_c")
    ppm_txt = f"{ppm['value']:+.3f} ({ppm.get('via')})" if isinstance(ppm.get("value"), (int, float)) else \
        f"— ({ppm.get('via', 'none')})"
    stats = node.stats if node is not None else {}
    parts = [
        f"[{name}] {d.get('state')}", f"fc {fmt_hz(float(d.get('fc_hz', 0.0)))}",
        f"fs {float(d.get('fs_hz', 0.0)) / 1e6:.4g} MS/s", f"G {_fmt_gains(d.get('gain_db'))} dB ({d.get('gain_mode')})",
        f"kare {stats.get('frames', 0)}", f"tespit {stats.get('detections', 0)}",
        f"blok {st.get('blocks', 0)}", f"ovf {_overflows_text(st)}", f"drop {st.get('host_drops', 0)}",
        f"{float(st.get('throughput_sps', 0.0)) / 1e6:.3g} MS/s", f"ppm {ppm_txt}",
    ]
    if isinstance(temp, (int, float)):
        parts.append(f"T {temp:.1f} °C")
    warns = d.get("warnings") or []
    if warns:
        parts.append(f"uyarı {len(warns)}")
    return "  ".join(parts)


def cmd_rx(args: argparse.Namespace) -> int:
    from ..bus.topics import RX_STATUS
    from ..rx.node import RxNode

    app = load_app(args)
    holder: list[Any] = []
    shown: set[str] = set()

    def hook(topic: str, env: dict[str, Any]) -> None:
        if topic != RX_STATUS:
            return
        data = env["data"]
        for w in data.get("warnings") or []:          # each new warning once, then only the count
            if w not in shown:
                shown.add(w)
                print(f"UYARI: {w}", file=sys.stderr)
        print(_status_line(env["src"], data, holder[0] if holder else None), file=sys.stderr, flush=True)

    node = RxNode(app, on_publish=hook)
    holder.append(node)
    dur = "Ctrl-C ile durdurun" if args.duration is None else f"{args.duration:g} s"
    print(f"RX düğümü '{node.name}' çalışıyor ({dur}); bus {app.bus.mode} "
          f"{app.bus.pub_endpoint if app.bus.mode == 'direct' else app.bus.proxy_frontend}", file=sys.stderr)
    try:
        node.run(args.duration)
    finally:
        node.stop()                                   # idempotent; also if run() raised before start
    s = node.stats
    stream = dict(s["stream"])
    if node.overflow_source is not None:
        stream["overflow_source"] = node.overflow_source
    print(f"RX düğümü durdu: {s['frames']} kare, {s['detections']} tespit, {s['spectrum_msgs']} spektrum, "
          f"{s['status_msgs']} durum mesajı; overflow {_overflows_text(stream)}, host drop {stream['host_drops']}, "
          f"yayın hatası {s['publish_errors']}", file=sys.stderr)
    return 0


# =============================================================================== sweep
def _sweep_figure(res: Any, title: str, serial: str, ppm_via: str) -> Any:
    from ..measure.base import FigureSpec, Series

    f, p = res.decimate(4001, "max")
    span = res.stop_hz - res.start_hz
    scale, unit = (1e9, "GHz") if res.stop_hz >= 1e9 and span >= 500e6 else (1e6, "MHz")
    top = float(np.max(p)) if p.size else 0.0
    ref = 10.0 * math.ceil((top + 5.0) / 10.0)
    dets = sorted(res.detections, key=lambda d: d.peak_db, reverse=True)[:5]
    markers = [(d.f_peak_hz / scale, d.peak_db, f"SNR {d.snr_db:.1f} dB") for d in dets]
    info = {
        "Ref": f"{ref:.0f} {res.unit}", "RBW": fmt_hz(res.rbw_hz), "Avg": str(res.averages),
        "Span": fmt_hz(span), "Center": fmt_hz(0.5 * (res.start_hz + res.stop_hz)),
        "Adım": str(res.n_steps), "Süre": f"{res.duration_s:.3f} s", "Hız": f"{res.rate_hz_per_s / 1e9:.3g} GHz/s",
        "Start": fmt_hz(res.start_hz, 9), "Stop": fmt_hz(res.stop_hz, 9),
        "version": __version__, "date": iso_utc(now_ns()), "serial": serial,
    }
    return FigureSpec(name="sweep", title=f"{title} — {res.window}, ppm {ppm_via}", kind="spectrum",
                      series=[Series(x=(f / scale).tolist(), y=p.tolist(), label=res.unit)],
                      xlabel=f"Frekans [{unit}]", ylabel=f"Seviye [{res.unit}]", markers=markers, info=info,
                      xlim=(res.start_hz / scale, res.stop_hz / scale), ylim=(ref - 100.0, ref))


def _sweep_meta(res: Any, dev_desc: dict[str, Any], ppm_via: str) -> dict[str, Any]:
    return sanitize({
        "eds_version": __version__, "t_start_ns": res.t_start_ns, "t_end_ns": res.t_end_ns, "sweep_id": res.sweep_id,
        "start_hz": res.start_hz, "stop_hz": res.stop_hz, "rbw_hz": res.rbw_hz, "df_hz": res.df_hz,
        "fs_hz": res.fs_hz, "nfft": res.nfft, "window": res.window, "enbw_bins": res.enbw_bins,
        "averages": res.averages, "unit": res.unit, "calibrated": res.calibrated, "gain_ref_db": res.gain_ref_db,
        "gain_mode": res.gain_mode, "n_steps": res.n_steps, "step_hz": res.step_hz, "duration_s": res.duration_s,
        "rate_hz_per_s": res.rate_hz_per_s, "timing": res.timing, "ppm_via": ppm_via,
        "detections": [d.to_dict() for d in res.detections], "device": dev_desc,
    })


def cmd_sweep(args: argparse.Namespace) -> int:
    from ..calib.apply import apply_calibration, apply_ppm_to_device
    from ..hal.factory import open_device
    from ..rx.sweep import Sweeper
    from .cmd_cal import load_calibration

    app = load_app(args)
    sw = app.sweep
    if args.start is not None:
        sw.start_hz = float(args.start)
    if args.stop is not None:
        sw.stop_hz = float(args.stop)
    if args.rbw is not None:
        sw.rbw_hz = float(args.rbw)
    if args.fs is not None:
        sw.sample_rate_hz = float(args.fs)
        sw.bandwidth_hz = min(float(sw.bandwidth_hz), float(args.fs))
    if args.publish and sw.publish_max_points < 1:      # fail before the sweep, not after it
        raise ConfigError(f"sweep.publish_max_points pozitif tamsayı olmalı, {sw.publish_max_points!r} geldi")
    dev = open_device(app.devices[0], app)
    try:
        for w in dev.configure(app.rx):
            print(f"UYARI: {w}", file=sys.stderr)
        cal = load_calibration(app, dev)
        via = apply_ppm_to_device(dev, cal, app.cal.ppm_mode)
        cal_spec = cal if app.cal.use_power_cal else dataclasses.replace(cal, power=None)

        def calibrate(spec: Any) -> Any:
            return apply_calibration(spec, cal_spec, app.cal.ppm_mode, ppm_hw=float(dev.ppm_hw))

        sweeper = Sweeper(dev, sw, app.dsp, app.detect, calibrate=calibrate, agc=app.agc,
                          timeout_s=float(app.rx.read_timeout_s))
        print(f"Tarama: {fmt_hz(sw.start_hz)} – {fmt_hz(sw.stop_hz)}, {len(sweeper.plan())} adım × "
              f"{fmt_hz(sweeper.step_hz)}, fs {sw.sample_rate_hz / 1e6:.4g} MS/s, RBW {fmt_hz(sweeper.rbw_hz)}, "
              f"ppm {via}", file=sys.stderr)
        res = sweeper.run()
        desc = dev.describe()
        serial = str(dev.info.serial)
        name = dev.name
    finally:
        dev.close()
    t = res.timing
    print(f"Tarama süresi {res.duration_s:.3f} s → hız {res.rate_hz_per_s / 1e9:.4g} GHz/s "
          f"({res.n_steps} adım; tune {t['tune_s']:.3f} s, settle {t['settle_s']:.3f} s, yakalama {t['capture_s']:.3f} s, "
          f"DSP {t['dsp_s']:.3f} s); birim {res.unit}, gürültü tabanı {res.noise_floor_db:.1f} {res.unit}")
    rows = [[i + 1, f"{d.f_peak_hz / 1e6:.6f}", f"{d.bw_hz / 1e3:.1f}", f"{d.peak_db:.1f}", f"{d.band_power_db:.1f}",
             f"{d.snr_db:.1f}"] for i, d in enumerate(sorted(res.detections, key=lambda d: d.f_peak_hz))]
    print(f"{len(rows)} tespit:")
    if rows:
        print_table(["#", "Tepe [MHz]", "BW [kHz]", f"Tepe [{res.unit}]", f"Bant gücü [{res.unit}]", "SNR [dB]"],
                    rows, right=range(6))
    if args.out:
        out = Path(args.out)
        if out.suffix.lower() != ".npz":
            out = out.with_name(out.name + ".npz")
        out.parent.mkdir(parents=True, exist_ok=True)
        np.savez(out, f_hz=res.f_hz, p_db=res.p_db, meta=np.array(json.dumps(_sweep_meta(res, desc, via),
                                                                              ensure_ascii=False)))
        print(f"Veri: {out}")
    if args.png:
        from ..measure.plots import render

        title = f"EDS tarama {fmt_hz(res.start_hz)} – {fmt_hz(res.stop_hz)} ({name})"
        path = render(_sweep_figure(res, title, serial, via), args.png)
        print(f"Ekran görüntüsü: {path}")
    if args.publish:
        from ..bus.topics import RX_SWEEP
        from .cmd_bus import one_shot_publish

        one_shot_publish(app, name, RX_SWEEP, res.to_bus_payload(name, int(sw.publish_max_points)))
    return 0
