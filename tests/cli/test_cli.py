"""Tests of the ``eds`` command-line tool (eds.cli.main:main) on the simulator."""

from __future__ import annotations

import json
import socket
import subprocess
import sys
from pathlib import Path

import numpy as np
import pytest

from eds.bus.validate import validate_message
from eds.cli.main import main
from eds.cli.util import parse_kv, parse_literal
from eds.core.config import load_config
from eds.core.errors import ConfigError
from eds.measure.base import MeasurementResult
from eds.rx.node import RxNode

PPM_TRUE = 7.3


def free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return int(s.getsockname()[1])


def write_config(tmp_path: Path, *, realtime: bool = False, emitters: str = "", ppm_override: float | None = None,
                 fc: float = 915e6, fs: float = 2e6) -> Path:
    ovr = f"ppm_override = {ppm_override}" if ppm_override is not None else ""
    cfg = tmp_path / "eds_test.toml"
    cfg.write_text(f"""
[[device]]
name = "rx0"
backend = "sim"
profile = "ad9363_pluto"
sim = {{ ppm = {PPM_TRUE}, realtime = {str(realtime).lower()} }}

[rx]
center_freq_hz = {fc}
sample_rate_hz = {fs}
bandwidth_hz = {fs}
gain_db = 50.0
block_size = 32768
buffer_size = 32768

[dsp]
nfft = 2048
averages = 8

[sweep]
start_hz = 900e6
stop_hz = 930e6
gain_db = 50.0

[bus]
pub_endpoint = "tcp://127.0.0.1:{free_port()}"
status_period_s = 0.2

[cal]
dir = "{(tmp_path / 'cal').as_posix()}"
{ovr}

[measure]
out_dir = "{(tmp_path / 'reports').as_posix()}"
source = "sim"

[sim]
seed = 99
{emitters}
""", encoding="utf-8")
    return cfg


CW = """
[[sim.emitters]]
name = "cw1"
kind = "cw"
freq_hz = 915.3e6
power_dbm = -70.0
"""


# --------------------------------------------------------------------------- parsing helpers
def test_parse_literal_and_kv():
    assert parse_literal("2.4e9") == 2.4e9 and parse_literal("true") is True and parse_literal("[1, 2]") == [1, 2]
    assert parse_literal("sim") == "sim" and parse_literal("usb:1.5.5") == "usb:1.5.5"
    assert parse_kv("rx.gain_db=30") == ("rx.gain_db", 30)
    with pytest.raises(ConfigError):
        parse_kv("novalue")


# --------------------------------------------------------------------------- simple commands
def test_profiles_table(capsys):
    assert main(["profiles"]) == 0
    out = capsys.readouterr().out
    assert "ad9363_pluto" in out and "325 MHz – 3.8 GHz" in out and "20 MHz" in out and "usb2" in out
    assert "krakensdr" in out and "planned" in out


def test_info_defaults_and_violation(capsys):
    assert main(["info"]) == 0
    out = capsys.readouterr().out
    assert "yerleşik varsayılanlar" in out and "RBW 1.95738 kHz" in out and "Tamam" in out
    assert main(["info", "--set", "rx.center_freq_hz=5e9", "--set", "rx.sample_rate_hz=20e6"]) == 1
    cap = capsys.readouterr()
    assert "HATA: Merkez frekans 5 GHz" in cap.out and "UYARI:" in cap.out and "profil ihlali" in cap.err


def test_devices_sim_and_probe(capsys):
    assert main(["devices", "--backend", "sim"]) == 0
    assert "driver=sim" in capsys.readouterr().out
    assert main(["devices", "--backend", "sim", "--probe"]) == 0
    out = capsys.readouterr().out
    js = json.loads(out[out.index("{"):])
    assert js["describe"]["info"]["serial"] == "SIM-rx0" and js["runtime_limits"]["gain_db"] == [-1.0, 73.0]
    assert js["sensors"]["temperature_c"] == pytest.approx(35.0)


def test_devices_soapy_enumerate_and_probe_with_fake_driver(monkeypatch, capsys):
    from tests.hal.fakes import fake_iio, fake_soapy

    fake_iio.uninstall(monkeypatch)
    fake_soapy.install(monkeypatch)
    assert main(["devices", "--backend", "soapy", "--args", "driver=plutosdr", "--probe"]) == 0
    out = capsys.readouterr().out
    assert "Backend 'soapy': 1 cihaz" in out and "uri=usb:1.5.5" in out
    assert "Yoklama: dev0 (soapy, profil ad9363_pluto)" in out          # profile chosen by Soapy driver key
    js = json.loads(out[out.index("{"):])
    assert js["describe"]["info"]["serial"] == fake_soapy.PLUTO_ENTRY["serial"]
    assert js["describe"]["info"]["backend"] == "soapy"


def test_exit_codes_for_bad_config_and_usage(tmp_path, capsys):
    assert main(["-c", str(tmp_path / "yok.toml"), "info"]) == 1
    assert "HATA: Konfigürasyon dosyası yok" in capsys.readouterr().err
    assert main(["info", "--set", "rx.bogus=1"]) == 1
    assert "bilinmeyen anahtar" in capsys.readouterr().err
    bad = tmp_path / "bad.toml"
    bad.write_text("[rx\ncenter_freq_hz = 1", encoding="utf-8")
    assert main(["info", "-c", str(bad)]) == 1
    assert "TOML sözdizimi hatası" in capsys.readouterr().err
    assert main(["yok-boyle-komut"]) == 1
    assert "hata:" in capsys.readouterr().err
    assert main(["measure", "olmayan_olcum", "--no-report"]) == 1
    assert "Bilinmeyen ölçüm" in capsys.readouterr().err
    assert main(["--help"]) == 0
    assert "genel seçenekler" in capsys.readouterr().out


def test_global_options_after_subcommand_and_backend_override(tmp_path, capsys):
    cfg = write_config(tmp_path)
    assert main(["info", "-c", str(cfg), "--set", "rx.gain_db=33", "--backend", "soapy"]) == 0
    out = capsys.readouterr().out
    assert "kazanç manual 33 dB" in out and "backend=soapy" in out


def test_python_dash_m_eds():
    r = subprocess.run([sys.executable, "-m", "eds", "profiles"], capture_output=True, text=True, timeout=120)
    assert r.returncode == 0 and "ad9363_pluto" in r.stdout


# --------------------------------------------------------------------------- sweep
def test_sweep_small_span_png_and_npz(tmp_path, capsys):
    cfg = write_config(tmp_path, emitters=CW, ppm_override=PPM_TRUE)
    png, npz = tmp_path / "s.png", tmp_path / "s.npz"
    assert main(["-c", str(cfg), "sweep", "--start", "905e6", "--stop", "925e6", "--png", str(png),
                 "--out", str(npz)]) == 0
    out = capsys.readouterr().out
    assert "GHz/s" in out and "tespit" in out
    assert png.read_bytes()[:8] == b"\x89PNG\r\n\x1a\n" and png.stat().st_size > 20_000
    d = np.load(npz)
    f, p = d["f_hz"], d["p_db"]
    meta = json.loads(str(d["meta"].item()))
    assert f[0] == pytest.approx(905e6) and f[-1] <= 925e6 + 1 and np.all(np.diff(f) > 0) and p.shape == f.shape
    assert meta["ppm_via"] == "hardware" and meta["start_hz"] == 905e6 and meta["unit"].startswith("dBFS")
    # the CW is found at its TRUE frequency (hardware ppm correction) within one bin
    near = [x for x in meta["detections"] if abs(x["f_peak_hz"] - 915.3e6) < 2 * meta["rbw_hz"]]
    assert near and abs(near[0]["f_peak_hz"] - 915.3e6) < meta["df_hz"]
    k = int(np.argmax(p))
    assert abs(f[k] - 915.3e6) <= meta["df_hz"]


# --------------------------------------------------------------------------- calibration
def test_cal_ppm_cw_reference_ground_truth(tmp_path, capsys):
    cfg = write_config(tmp_path, emitters="""
[[sim.emitters]]
name = "ref"
kind = "cw"
freq_hz = 900e6
power_dbm = -60.0
""")
    assert main(["-c", str(cfg), "cal", "ppm", "--ref-hz", "900e6", "--repeats", "3", "--apply"]) == 0
    out = capsys.readouterr().out
    assert "Kaydedildi:" in out and "Uygulandı: hardware" in out
    stored = json.loads((tmp_path / "cal" / "SIM-rx0.json").read_text(encoding="utf-8"))
    assert stored["ppm"]["ppm"] == pytest.approx(PPM_TRUE, abs=0.02)        # sim ground truth: +7.3 ppm
    assert stored["ppm"]["method"] == "cw" and stored["profile"] == "ad9363_pluto"
    # a node started afterwards applies it in hardware
    app = load_config(cfg)
    from eds.bus.pubsub import Publisher

    with Publisher("inproc://eds-cli-cal-node", src="rx0") as pub:
        node = RxNode(app, publisher=pub)
        try:
            assert node.ppm_via == "hardware" and node.device.ppm_hw == pytest.approx(PPM_TRUE, abs=0.02)
        finally:
            node.stop()


def test_cal_power_against_sim_level_model(tmp_path, capsys):
    cfg = write_config(tmp_path)
    assert main(["-c", str(cfg), "cal", "power", "--freqs", "915e6", "--gains", "30", "50", "--p-in", "-60"]) == 0
    capsys.readouterr()
    stored = json.loads((tmp_path / "cal" / "SIM-rx0.json").read_text(encoding="utf-8"))
    pts = {round(g): off for f, g, off in stored["power"]["points"]}
    # sim level equation dBFS = dBm + G - 5  ⇒  offset (dBm = dBFS + offset) = 5 - G
    assert pts[30] == pytest.approx(5.0 - 30.0, abs=0.5) and pts[50] == pytest.approx(5.0 - 50.0, abs=0.5)


def _dual_config(tmp_path: Path, *, shared: bool) -> Path:
    clock_a = 'ppm = 5.0, shared_clock = "ref"' if shared else "ppm = 5.0"
    clock_b = 'shared_clock = "ref"' if shared else "ppm = 5.0001"        # 1e-4 ppm: ~36 °/s at 1 GHz
    cfg = tmp_path / "dual.toml"
    cfg.write_text(f"""
[[device]]
name = "rx0"
backend = "sim"
sim = {{ {clock_a}, realtime = false, serial = "SIM-A" }}
[[device]]
name = "rx1"
backend = "sim"
sim = {{ {clock_b}, realtime = false, serial = "SIM-B", position_m = [0.15, 0.0] }}
[rx]
center_freq_hz = 999.5e6
sample_rate_hz = 2e6
bandwidth_hz = 2e6
gain_db = 40.0
[cal]
dir = "{(tmp_path / 'cal').as_posix()}"
[[sim.emitters]]
name = "pilot"
kind = "noise"
freq_hz = 999.7e6
bw_hz = 200e3
power_dbm = -60.0
cabled = true
""", encoding="utf-8")
    return cfg


@pytest.mark.parametrize("blocks", ["4", "8"])
def test_cal_coherence_shared_clock_pair(tmp_path, capsys, blocks):
    # 4 blocks of 16384: the noise-pilot drift spread (once ~1 °/s rms, as large as the ±1 °/s
    # limit) is ~0.2 °/s with one common delay, and the limit is widened to 3σ when needed
    cfg = _dual_config(tmp_path, shared=True)
    assert main(["-c", str(cfg), "cal", "coherence", "--n", "16384", "--blocks", blocks, "--no-publish"]) == 0
    cap = capsys.readouterr()
    assert "KOHERENT" in cap.out and "DEĞİL" not in cap.out and "DEĞİL" not in cap.err
    files = list((tmp_path / "cal").glob("SIM-A_SIM-B-*.json"))
    assert len(files) == 1
    coh = json.loads(files[0].read_text(encoding="utf-8"))["coherence"]
    assert coh["coherent"] is True and coh["coherence"] > 0.9


def test_cal_coherence_not_coherent_exits_3_and_still_saves(tmp_path, capsys):
    cfg = _dual_config(tmp_path, shared=False)
    rc = main(["-c", str(cfg), "cal", "coherence", "--n", "16384", "--blocks", "4", "--no-publish"])
    cap = capsys.readouterr()
    assert rc == 3
    assert "KOHERENT DEĞİL" in cap.out and "faz kayması" in cap.out
    assert "çıkış kodu 3" in cap.err and "DF" in cap.err
    files = list((tmp_path / "cal").glob("SIM-A_SIM-B-*.json"))
    assert len(files) == 1                                   # the failed check is still recorded
    assert json.loads(files[0].read_text(encoding="utf-8"))["coherence"]["coherent"] is False
    # a usage/config error stays 1, not 3
    assert main(["-c", str(cfg), "cal", "coherence", "--n", "100", "--no-save", "--no-publish"]) == 1
    capsys.readouterr()
    assert main(["cal", "coherence", "--help"]) == 0
    out = capsys.readouterr().out
    assert "Çıkış kodları" in out and "3  kanallar koherent DEĞİL" in out


# --------------------------------------------------------------------------- measure
@pytest.mark.realtime
def test_measure_latency_quick_realtime_writes_report(tmp_path, capsys):
    cfg = write_config(tmp_path, realtime=True, emitters=CW)
    out_dir = tmp_path / "rapor"
    rc = main(["-c", str(cfg), "measure", "latency", "--quick", "--out", str(out_dir), "--title", "Test raporu"])
    cap = capsys.readouterr()
    assert rc == 0, cap.err
    line = next(x for x in cap.out.splitlines() if x.startswith("Rapor: "))
    rdir = Path(line.split("Rapor: ", 1)[1].strip())
    assert rdir.is_dir() and rdir.parent == out_dir
    assert (rdir / "results.json").is_file() and (rdir / "summary.md").is_file() and (rdir / "report.html").is_file()
    res = json.loads((rdir / "results.json").read_text(encoding="utf-8"))["results"][0]
    assert res["spec_id"] == "latency" and res["status"] == "ok" and res["unit"] == "ms"
    assert 0 < res["value"] < 1000 and res["extra"]["mode_b_detected"] == res["extra"]["mode_b_events"] == 6
    assert "Uçtan uca gecikme" in cap.out
    assert len(list((rdir / "figures").glob("*.png"))) == 4


@pytest.mark.parametrize("statuses, code", [(["ok", "skipped"], 0), (["ok", "failed", "skipped"], 2)])
def test_measure_exit_code_skipped_vs_failed(monkeypatch, capsys, statuses, code):
    """Exit code contract: skipped measurements are not failures; any failed one → 2."""
    import eds.measure.session as session

    seen = {}

    def fake_run_suite(app, ids, **kw):
        seen.update(kw, ids=ids)
        res = [MeasurementResult(spec_id=s, title=s, value=1.0 if st == "ok" else None, unit="ms", status=st)
               for s, st in zip(("latency", "max_rate", "df_rms"), statuses)]
        return session.SuiteResult(results=res, report_dir=None)

    monkeypatch.setattr(session, "run_suite", fake_run_suite)
    assert main(["measure", "latency", "max_rate", "--quick", "--no-report", "--source", "none", "--yes"]) == code
    out = capsys.readouterr().out
    assert "ATLANDI" in out and "Rapor:" not in out
    assert seen["ids"] == ["latency", "max_rate"] and seen["quick"] is True and seen["write"] is False
    assert seen["source_kind"] == "none" and seen["prompt"] is None          # all-sim config: no operator prompt


# --------------------------------------------------------------------------- bus
def test_bus_send_and_echo_round_trip_with_in_thread_node(tmp_path, capsys):
    pub_ep = f"tcp://127.0.0.1:{free_port()}"
    ctl1, ctl2 = (f"tcp://127.0.0.1:{free_port()}" for _ in range(2))
    cfg = write_config(tmp_path, emitters=CW)
    app = load_config(cfg, {"bus.pub_endpoint": pub_ep, "bus.ctl_endpoints": [ctl1, ctl2],
                            "bus.validate": True})
    node = RxNode(app)
    node.start()
    common = ["-c", str(cfg), "--set", f'bus.sub_endpoint="{pub_ep}"', "--set", f'bus.ctl_endpoints=["{ctl1}"]']
    try:
        rc = main([*common, "bus", "send", "ctl.rx", "--cmd", "set_center_freq", "--arg", "hz=916e6",
                   "--target", "rx0", "--wait-ack", "10", "--req-id", "cli-1"])
        out = capsys.readouterr().out
        assert rc == 0, out
        assert "Gönderildi: ctl.rx set_center_freq → rx0 (req_id cli-1)" in out
        assert "cli-1 ← rx0: set_center_freq OK" in out and '"fc_hz":916000000.0' in out
        assert node.device.fc == 916e6
        # broadcast ('*'): every ack within the wait window is collected; a failed one → exit 2
        rc = main([*common, "bus", "send", "ctl.rx", "--cmd", "set_center_freq", "--arg", "hz=5e9",
                   "--endpoint", ctl2, "--wait-ack", "1.5"])
        out = capsys.readouterr().out
        assert rc == 2 and "HATA: Cihaz profili ihlali" in out and node.device.fc == 916e6
        rc = main(["bus", "echo", "rx.status", "rx.detections", "--endpoint", pub_ep, "--count", "4",
                   "--timeout", "20"])
        out = capsys.readouterr().out
        assert rc == 0
        lines = [x for x in out.splitlines() if " rx." in x]
        assert len(lines) == 4 and all("rx0#" in x for x in lines)
        rc = main(["bus", "echo", "rx.status", "--endpoint", pub_ep, "--count", "1", "--timeout", "20", "--raw"])
        out = capsys.readouterr().out.strip().splitlines()[-1]
        env = json.loads(out)
        validate_message("rx.status", env)
        assert rc == 0 and env["data"]["fc_hz"] == 916e6
    finally:
        node.stop()
    rc = main(["bus", "echo", "rx.status", "--endpoint", pub_ep, "--count", "1", "--timeout", "0.3"])
    assert rc == 1 and "Zaman aşımı: 0/1" in capsys.readouterr().err


def test_bus_send_without_listener_fails(tmp_path, capsys):
    ep = f"tcp://127.0.0.1:{free_port()}"
    rc = main(["--set", f'bus.ctl_endpoints=["{ep}"]', "bus", "send", "ctl.rx", "--cmd", "ping",
               "--sub-timeout", "0.3"])
    assert rc == 1 and "dinleyen RX düğümü bulunamadı" in capsys.readouterr().err
    assert main(["bus", "send", "ctl.rx", "--cmd", "ping"]) == 1           # direct mode, no ctl endpoint
    assert "ctl_endpoints boş" in capsys.readouterr().err


def test_bus_proxy_runs_for_duration(capsys):
    fe, be = (f"tcp://127.0.0.1:{free_port()}" for _ in range(2))
    assert main(["bus", "proxy", "--frontend", fe, "--backend", be, "--duration", "0.3"]) == 0
    assert "Bus proxy çalışıyor" in capsys.readouterr().err


# --------------------------------------------------------------------------- rx
def test_rx_command_prints_status_lines(tmp_path, capsys):
    cfg = write_config(tmp_path, emitters=CW)
    assert main(["-c", str(cfg), "rx", "--duration", "0.8"]) == 0
    err = capsys.readouterr().err
    status = [x for x in err.splitlines() if x.startswith("[rx0] ")]
    assert len(status) >= 2 and "fc 915 MHz" in status[-1] and "streaming" in status[-1]
    assert "RX düğümü durdu:" in err


# --------------------------------------------------------------------------- input validation / review fixes
@pytest.mark.parametrize("argv, fragment", [
    (["rx", "--duration", "nan"], "argüman --duration: sonlu bir sayı olmalı"),
    (["rx", "--duration", "-1"], "negatif olmayan"),
    (["sweep", "--rbw", "-5"], "argüman --rbw: pozitif bir sayı olmalı"),
    (["sweep", "--start", "inf"], "sonlu bir sayı olmalı"),
    (["cal", "ppm", "--ref-hz", "915e6", "--repeats", "0"], "argüman --repeats: en az 1 olmalı"),
    (["cal", "coherence", "--n", "abc"], "tamsayı bekleniyordu"),
    (["bus", "echo", "--timeout", "nan"], "sonlu bir sayı olmalı"),
    (["bus", "send", "ctl.rx", "--cmd", "ping", "--wait-ack", "-2"], "negatif olmayan"),
    (["bus", "send", "ctl.rx"], "şu argümanlar gerekli: --cmd"),
    (["cal", "ppm"], "şu argümanlardan biri gerekli"),
    (["bus", "send", "ctl.rx", "--cmd", "reboot"], "geçersiz seçim: 'reboot' (seçenekler:"),
])
def test_bad_numeric_options_are_usage_errors_in_turkish(capsys, argv, fragment):
    assert main(argv) == 1
    err = capsys.readouterr().err
    assert fragment in err and "hata:" in err


def test_bus_send_rejects_non_finite_args_and_bad_ids(capsys):
    ep = f"tcp://127.0.0.1:{free_port()}"
    assert main(["bus", "send", "ctl.rx", "--cmd", "set_center_freq", "--arg", "hz=nan", "--endpoint", ep]) == 1
    assert "sonlu olmayan sayı" in capsys.readouterr().err
    assert main(["bus", "send", "ctl.rx", "--cmd", "ping", "--target", "", "--endpoint", ep]) == 1
    assert "--target boş olamaz" in capsys.readouterr().err
    assert main(["bus", "send", "ctl.rx", "--cmd", "ping", "--req-id", "x" * 129, "--endpoint", ep]) == 1
    assert "--req-id" in capsys.readouterr().err


def test_bus_echo_rejects_unknown_topic(capsys):
    assert main(["bus", "echo", "rx.bogus", "--timeout", "0.1"]) == 1
    err = capsys.readouterr().err
    assert "Bilinmeyen topic" in err and "rx.status" in err


def test_ack_summary_omits_null_result():
    from eds.bus.pubsub import Message
    from eds.cli.cmd_bus import summarize

    env = {"topic": "ctl.ack", "v": 1, "src": "rx0", "seq": 0, "t_ns": 1,
           "data": {"req_id": "r", "target": "rx0", "cmd": "set_gain", "ok": False, "error": "Kazanç aralık dışı",
                    "result": None}}
    s = summarize(Message("ctl.ack", env, 2))
    assert s.endswith("HATA: Kazanç aralık dışı") and "null" not in s
    env["data"].update(ok=True, error=None, result={"gain_db": [30.0]})
    assert summarize(Message("ctl.ack", env, 2)).endswith('OK {"gain_db":[30.0]}')


def test_status_line_shows_unknown_overflows_when_driver_cannot_detect():
    from eds.cli.cmd_hw import OVERFLOW_UNKNOWN, _status_line

    d = {"state": "streaming", "fc_hz": 915e6, "fs_hz": 4e6, "gain_db": [40.0, 41.5], "gain_mode": "manual",
         "stream": {"blocks": 3, "overflows": 0, "host_drops": 0, "throughput_sps": 4e6, "overflow_source": "none"},
         "ppm": {"value": None, "via": "none"}}
    line = _status_line("rx0", d, None)
    assert f"ovf {OVERFLOW_UNKNOWN}" in line and "G 40/41.5 dB" in line
    d["stream"]["overflow_source"] = "dma_status"
    assert "ovf 0 " in _status_line("rx0", d, None)


def test_info_shows_pluto_process_isolation(capsys):
    repo = Path(__file__).resolve().parents[2]
    assert main(["info", "-c", str(repo / "config" / "dual_pluto.toml")]) in (0, 1)
    out = capsys.readouterr().out
    assert "Süreç yalıtımı" in out and "rx0=subprocess, rx1=subprocess" in out


def test_cal_coherence_switches_off_over_the_air_emitters_on_sim(tmp_path, capsys):
    """The coherence check is a cabled setup: an OTA emitter in the scene (geometric phase
    difference) must not make a shared-clock pair look non-coherent; it is restored after."""
    cfg = tmp_path / "dual_ota.toml"
    cfg.write_text(f"""
[[device]]
name = "rx0"
backend = "sim"
sim = {{ ppm = 5.0, shared_clock = "ref", realtime = false, serial = "SIM-A" }}
[[device]]
name = "rx1"
backend = "sim"
sim = {{ shared_clock = "ref", realtime = false, serial = "SIM-B", position_m = [0.15, 0.0] }}
[rx]
center_freq_hz = 999.5e6
sample_rate_hz = 2e6
bandwidth_hz = 2e6
gain_db = 40.0
[cal]
dir = "{(tmp_path / 'cal').as_posix()}"
[[sim.emitters]]
name = "pilot"
kind = "cw"
freq_hz = 998.9e6
power_dbm = -55.0
cabled = true
[[sim.emitters]]
name = "ota"
kind = "cw"
freq_hz = 1.0e9
power_dbm = -60.0
aoa_deg = 25.0
""", encoding="utf-8")
    import eds.cli.cmd_cal as cmd_cal
    from eds.hal.factory import get_sim_world

    seen = {}
    orig = cmd_cal._cabled_setup

    def spy(app):
        seen["world"] = get_sim_world(app)
        return orig(app)

    import unittest.mock as um

    with um.patch.object(cmd_cal, "_cabled_setup", spy):
        assert main(["-c", str(cfg), "cal", "coherence", "--n", "16384", "--blocks", "4", "--no-save",
                     "--no-publish"]) == 0
    cap = capsys.readouterr()
    assert "KOHERENT" in cap.out and "DEĞİL" not in cap.out
    assert "havadan gelen 1 emitter (ota)" in cap.err
    assert seen["world"].get("ota").enabled is True                       # scene restored
