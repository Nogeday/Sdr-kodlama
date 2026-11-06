"""The example programs in ``examples/`` (documented in docs/KULLANIM.md §8) run against a real
in-process :class:`~eds.rx.node.RxNode` on the simulator, so they cannot silently rot when the
bus API, the topic schemas or the shipped ``config/rx_sim.toml`` change."""

from __future__ import annotations

import ast
import importlib.util
import socket
import subprocess
import sys
from pathlib import Path
from types import ModuleType

import pytest

from eds.core.config import load_config
from eds.rx.node import RxNode

ROOT = Path(__file__).resolve().parents[2]
EXAMPLES = ROOT / "examples"
SIM_CONFIG = ROOT / "config" / "rx_sim.toml"


def free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return int(s.getsockname()[1])


def load_example(name: str) -> ModuleType:
    """Import ``examples/<name>.py`` (the folder is not a package) without running ``main``."""
    spec = importlib.util.spec_from_file_location(f"eds_example_{name}", EXAMPLES / f"{name}.py")
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


@pytest.fixture
def node(tmp_path):
    """RX node of the shipped simulator config on private ports (calibration store in tmp).

    Yields ``(node, publish endpoint, [3 control endpoints])``: the node connects to every control
    endpoint; each commander binds its own one (a just-closed TCP port cannot be re-bound at once).
    """
    pub = f"tcp://127.0.0.1:{free_port()}"
    ctls = [f"tcp://127.0.0.1:{free_port()}" for _ in range(3)]
    app = load_config(SIM_CONFIG, {
        "bus.pub_endpoint": pub, "bus.sub_endpoint": pub, "bus.ctl_endpoints": ctls,
        "bus.status_period_s": 0.2, "bus.validate": True,          # every published message is schema-checked
        "cal.dir": str(tmp_path / "cal"),
        "device.sim.realtime": False,                              # CPU-speed independent
    })
    n = RxNode(app)
    n.start()
    try:
        yield n, pub, ctls
    finally:
        n.stop()


def test_bus_dinleyici_prints_detections_and_status(node, capsys):
    rx, pub, _ = node
    mod = load_example("bus_dinleyici")
    assert mod.main(["--endpoint", pub, "--duration", "20", "--count", "3", "--min-snr", "20"]) == 0
    out = capsys.readouterr().out.splitlines()
    dets = [x for x in out if " TESPİT " in x]
    assert len(dets) == 3 and all(x.split()[1] == "rx0" for x in dets)
    # GSM900 BCCH of the simulated scene (935.2 MHz ± 100 kHz, -60 dBm, SNR >> 20 dB)
    assert any(935.0 < float(x.split()[3]) < 935.4 for x in dets), dets
    assert all("SNR" in x and "dBFS" in x for x in dets)
    # status lines (every bus.status_period_s): stop at the first one
    class Done(Exception):
        pass

    lines: list[str] = []

    def out(line: str) -> None:
        lines.append(line)
        if " DURUM " in line:
            raise Done

    with pytest.raises(Done):
        mod.listen(pub, duration_s=20, min_snr_db=1e9, out=out)
    assert "streaming" in lines[-1] and "fc 935.000 MHz" in lines[-1] and "G 50 dB" in lines[-1], lines[-1]
    assert rx.running


def test_bus_dinleyici_endpoint_from_config(tmp_path):
    mod = load_example("bus_dinleyici")
    assert mod.endpoint_from_config(str(SIM_CONFIG)) == "tcp://127.0.0.1:5555" == mod.DEFAULT_ENDPOINT
    cfg = tmp_path / "proxy.toml"
    cfg.write_text('[bus]\nmode = "proxy"\nproxy_backend = "tcp://127.0.0.1:6001"\n', encoding="utf-8")
    assert mod.endpoint_from_config(str(cfg)) == "tcp://127.0.0.1:6001"


def test_bus_dinleyici_no_publisher_returns_1(capsys):
    mod = load_example("bus_dinleyici")
    rc = mod.main(["--endpoint", f"tcp://127.0.0.1:{free_port()}", "--duration", "0.3"])
    assert rc == 1 and "Hiç tespit alınmadı" in capsys.readouterr().err


def test_komut_gonder_retunes_node_and_reports_rejection(node, capsys):
    rx, pub, ctls = node
    mod = load_example("komut_gonder")
    rc = mod.main(["433.92e6", "--ctl-endpoint", ctls[0], "--ack-endpoint", pub, "--target", "rx0",
                   "--timeout", "10"])
    out = capsys.readouterr().out
    assert rc == 0, out
    assert out.startswith("OK    rx0: set_center_freq") and "'fc_hz': 433920000.0" in out
    assert rx.device.fc == pytest.approx(433.92e6)
    # outside the AD9363 profile: the node answers ok = false, keeps its frequency, exit code 2
    rc = mod.main(["5e9", "--ctl-endpoint", ctls[1], "--ack-endpoint", pub, "--timeout", "10"])
    out = capsys.readouterr().out
    assert rc == 2 and out.startswith("HATA  rx0: set_center_freq reddedildi") and "profil" in out
    assert rx.device.fc == pytest.approx(433.92e6)


def test_komut_gonder_commander_stays_open_for_many_commands(node):
    """How a GUI uses it: one channel, several commands, each answered by its own ack."""
    rx, pub, ctls = node
    mod = load_example("komut_gonder")
    with mod.Commander(ctls[2], pub) as kanal:
        acks = kanal.send("set_gain", {"db": 30}, timeout_s=10)
        assert len(acks) == 1 and acks[0]["ok"] and acks[0]["result"]["gain_db"] == [30.0]
        acks = kanal.send("set_center_freq", {"hz": 2.437e9}, target="rx0", timeout_s=10)
        assert acks[0]["ok"] and acks[0]["target"] == "rx0" and rx.device.fc == pytest.approx(2.437e9)
        acks = kanal.send("set_center_freq", {"hz": 1e9}, target="baska_dugum", timeout_s=0.5)
        assert acks == [] and rx.device.fc == pytest.approx(2.437e9)      # not addressed to rx0: ignored
        acks = kanal.send("ping", timeout_s=10)
        assert acks[0]["ok"] and acks[0]["result"]["fc_hz"] == pytest.approx(2.437e9)


def test_komut_gonder_without_node_fails_fast(capsys):
    mod = load_example("komut_gonder")
    rc = mod.main(["433.92e6", "--ctl-endpoint", f"tcp://127.0.0.1:{free_port()}",
                   "--ack-endpoint", f"tcp://127.0.0.1:{free_port()}", "--timeout", "0.3"])
    assert rc == 1 and "dinleyen RX düğümü yok" in capsys.readouterr().err


def test_komut_gonder_endpoints_from_config(tmp_path):
    mod = load_example("komut_gonder")
    with pytest.raises(SystemExit, match="ctl_endpoints boş"):
        mod.endpoints_from_config(str(SIM_CONFIG))          # shipped config: remote control off
    cfg = tmp_path / "ctl.toml"
    cfg.write_text('[bus]\nctl_endpoints = ["tcp://127.0.0.1:6002"]\n', encoding="utf-8")
    assert mod.endpoints_from_config(str(cfg)) == ("tcp://127.0.0.1:6002", "tcp://127.0.0.1:5555", True)
    cfg.write_text('[bus]\nmode = "proxy"\n', encoding="utf-8")
    assert mod.endpoints_from_config(str(cfg)) == ("tcp://127.0.0.1:5550", "tcp://127.0.0.1:5551", False)


def test_saf_zmq_dinleyici_needs_only_pyzmq(node):
    _, pub, _ = node
    path = EXAMPLES / "saf_zmq_dinleyici.py"
    imported = set()
    for n in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
        if isinstance(n, ast.Import):
            imported |= {a.name.split(".")[0] for a in n.names}
        elif isinstance(n, ast.ImportFrom):
            imported.add((n.module or "").split(".")[0])
    assert imported == {"json", "sys", "zmq"}, imported         # the reference for non-Python teammates
    res = subprocess.run([sys.executable, str(path), pub, "3"], capture_output=True, text=True,
                         encoding="utf-8", timeout=60, cwd=str(ROOT))
    assert res.returncode == 0, res.stderr
    lines = res.stdout.strip().splitlines()
    assert len(lines) == 3 and all(x.startswith("rx0 seq=") and "fc=935.000 MHz" in x for x in lines)


@pytest.mark.parametrize("name", ["bus_dinleyici", "komut_gonder"])
def test_examples_help_runs_as_script(name):
    res = subprocess.run([sys.executable, str(EXAMPLES / f"{name}.py"), "--help"], capture_output=True,
                         text=True, encoding="utf-8", timeout=60, cwd=str(ROOT))
    assert res.returncode == 0 and "usage:" in res.stdout
