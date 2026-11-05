"""``eds cal ppm | power | coherence`` — calibration commands (results go to the
calibration store ``[cal] dir`` and, on request, to the bus as ``cal.result``)."""

from __future__ import annotations

import argparse
import dataclasses
import math
import sys
from typing import Any

from ..bus.topics import CAL_RESULT
from ..calib.apply import apply_ppm_to_device, make_cal_payload
from ..calib.ppm import PpmEstimate
from ..calib.store import CalibrationStore, DeviceCalibration
from ..core.config import AppConfig
from ..core.errors import CalibrationError
from ..core.units import fmt_hz
from .cmd_bus import one_shot_publish
from .util import load_app, print_table


def load_calibration(app: AppConfig, dev: Any) -> DeviceCalibration:
    """Stored calibration of ``dev`` (by serial) with a finite ``[cal] ppm_override`` applied."""
    serial = str(dev.info.serial or "")
    if serial:
        cal = CalibrationStore(app.cal.dir or None).load(serial)
    else:
        print(f"UYARI: {dev.name}: seri numarası yok — kalibrasyon deposu kullanılamıyor", file=sys.stderr)
        cal = DeviceCalibration(serial="")
    override = float(app.cal.ppm_override)
    if math.isfinite(override):
        cal = dataclasses.replace(cal, ppm=PpmEstimate(
            ppm=override, std_ppm=math.nan, f_measured_hz=math.nan, f_ref_hz=math.nan, snr_db=math.nan, n=0,
            method="manual", extra={"source": "cal.ppm_override"}))
    return cal


def _store_for(app: AppConfig) -> CalibrationStore:
    return CalibrationStore(app.cal.dir or None)


def _require_serial(dev: Any) -> str:
    serial = str(dev.info.serial or "")
    if not serial:
        raise CalibrationError(f"{dev.name}: seri numarası okunamadı — kalibrasyon kaydedilemez (--no-save kullanın)")
    return serial


# =============================================================================== ppm
def cmd_cal_ppm(args: argparse.Namespace) -> int:
    from ..calib.fcch import estimate_ppm_gsm_fcch
    from ..calib.ppm import estimate_ppm_cw
    from ..hal.factory import open_device

    app = load_app(args)
    dev = open_device(app.devices[0], app)
    try:
        for w in dev.configure(app.rx):
            print(f"UYARI: {w}", file=sys.stderr)
        if args.ref_hz is not None:
            print(f"CW referans {fmt_hz(args.ref_hz)} ile PPM kalibrasyonu ({dev.name})…", file=sys.stderr)
            est = estimate_ppm_cw(dev, float(args.ref_hz), repeats=int(args.repeats or 5))
        else:
            if args.gsm_arfcn is not None:
                carrier: float | int = int(args.gsm_arfcn)
                band: str | None = args.band
            else:
                carrier, band = float(args.gsm_freq), None
            print(f"GSM FCCH ile PPM kalibrasyonu (taşıyıcı {carrier}) ({dev.name})…", file=sys.stderr)
            est = estimate_ppm_gsm_fcch(dev, carrier, n_bursts=max(int(args.repeats or 10), 3), band=band)
        print(est.summary_tr())
        print(f"  Görünen referans frekansı {fmt_hz(est.f_measured_hz, 10)} (gerçek {fmt_hz(est.f_ref_hz, 10)}); "
              f"donanım düzeltmesi {est.ppm_hw:+.4f} ppm, ölçülen artık {est.ppm_residual:+.4f} ppm; "
              f"{est.repeats} kestirim × {est.n} örnek")
        via = "off"
        cal = load_calibration(app, dev)
        cal = dataclasses.replace(cal, ppm=est, profile=dev.profile.name)
        if not args.no_save:
            serial = _require_serial(dev)
            stored = _store_for(app).load(serial)
            stored.ppm = est
            stored.profile = dev.profile.name
            stored.add_note(f"PPM {est.method}: {est.ppm:+.4f} ppm")
            path = _store_for(app).save(stored)
            print(f"Kaydedildi: {path}")
            via = "store"
        if args.apply:
            via = apply_ppm_to_device(dev, cal, app.cal.ppm_mode)
            print(f"Uygulandı: {via} (ppm_mode {app.cal.ppm_mode})")
        if args.publish:
            one_shot_publish(app, dev.name, CAL_RESULT, make_cal_payload("ppm", dev, est, via))
    finally:
        dev.close()
    return 0


# =============================================================================== power
def cmd_cal_power(args: argparse.Namespace) -> int:
    from ..calib.power import measure_power_cal
    from ..hal.factory import get_sim_world, open_device
    from ..hal.tx import open_source

    app = load_app(args)
    dev = open_device(app.devices[0], app)
    src = None
    try:
        for w in dev.configure(app.rx):
            print(f"UYARI: {w}", file=sys.stderr)
        world = get_sim_world(app) if dev.backend_name == "sim" else None
        src = open_source(app.measure.source, app.measure.source_args, world=world)
        if src is None:
            raise CalibrationError("Güç kalibrasyonu sinyal kaynağı gerektirir (measure.source = sim | scpi | pluto_tx "
                                   "| manual)")
        p_in: float | list[float] = float(args.p_in[0]) if len(args.p_in) == 1 else [float(v) for v in args.p_in]
        print(f"Güç kalibrasyonu: {len(args.freqs)} frekans × {len(args.gains)} kazanç, kaynak {src.name}…",
              file=sys.stderr)
        table = measure_power_cal(dev, src, [float(f) for f in args.freqs], [float(g) for g in args.gains], p_in)
        rows = [[f"{f / 1e6:.3f}", f"{g:g}", f"{o:+.2f}"] for f, g, o in table.points]
        print_table(["Frekans [MHz]", "Kazanç [dB]", "Ofset [dB] (dBm = dBFS + ofset)"], rows, right=range(3))
        via = "off"
        if not args.no_save:
            serial = _require_serial(dev)
            store = _store_for(app)
            stored = store.load(serial)
            stored.power = table
            stored.profile = dev.profile.name
            stored.add_note(f"Güç kalibrasyonu: {len(table.points)} nokta")
            print(f"Kaydedildi: {store.save(stored)}")
            via = "store"
        if args.publish:
            one_shot_publish(app, dev.name, CAL_RESULT, make_cal_payload("power", dev, table, via))
    finally:
        if src is not None:
            try:
                src.set_enabled(False)
            finally:
                src.close()
        dev.close()
    return 0


# =============================================================================== coherence
def cmd_cal_coherence(args: argparse.Namespace) -> int:
    from ..df.coherence import check_coherence
    from ..hal.factory import open_group

    app = load_app(args)
    grp = open_group(app)
    try:
        grp.configure(app.rx)
        rep = check_coherence(grp, n=int(args.n), blocks=int(args.blocks))
        name, serial = grp.name, str(grp.info.serial or "")
        print(f"Koherens ({name}, {grp.num_channels} kanal): {'KOHERENT' if rep.coherent else 'KOHERENT DEĞİL'}")
        print(f"  gecikme {rep.delay_samples:+.3f} örnek, faz {rep.phase_deg:+.2f}° (σ {rep.phase_std_deg:.2f}°), "
              f"koherens {rep.coherence:.4f}, faz kayması {rep.phase_drift_deg_per_s:+.3f} °/s, "
              f"frekans ofseti {rep.freq_offset_hz:+.3f} Hz, {rep.n_blocks} blok × {rep.n} örnek")
        if rep.note:
            print(f"  Not: {rep.note}")
        via = "off"
        if not args.no_save:
            if not serial:
                raise CalibrationError(f"{name}: seri numarası okunamadı — koherens raporu kaydedilemez (--no-save)")
            store = _store_for(app)
            stored = store.load(serial)
            stored.coherence = rep.to_dict()
            stored.add_note(f"Koherens: {'koherent' if rep.coherent else 'koherent değil'}, faz {rep.phase_deg:+.2f}°")
            print(f"Kaydedildi: {store.save(stored)}")
            via = "store"
        if not args.no_publish:
            one_shot_publish(app, name, CAL_RESULT, make_cal_payload("coherence", grp, rep, via))
    finally:
        grp.close()
    return 0
