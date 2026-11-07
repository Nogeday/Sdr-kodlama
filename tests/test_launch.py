"""One-command launcher: processes start, publish on the proxy, and all stop cleanly."""

from __future__ import annotations

import importlib.util
import os
import socket
import subprocess
import sys
import time

import pytest

from eds.bus import topics as T
from eds.bus.pubsub import Subscriber


def free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return int(s.getsockname()[1])


def _launch(*args: str, env: dict | None = None) -> subprocess.Popen:
    return subprocess.Popen([sys.executable, "-m", "eds.launch", *args], stdout=subprocess.PIPE,
                            stderr=subprocess.STDOUT, text=True, env=env)


@pytest.mark.realtime
def test_headless_pipeline_publishes_on_proxy_and_stops_cleanly():
    fe, be = free_port(), free_port()
    p = _launch("--no-gui", "--duration", "6", "--frontend-port", str(fe), "--backend-port", str(be))
    sub = Subscriber(f"tcp://127.0.0.1:{be}", [T.RX_STATUS, T.RX_SPECTRUM])
    try:
        got = set()
        end = time.monotonic() + 15
        while time.monotonic() < end and got != {T.RX_STATUS, T.RX_SPECTRUM}:
            m = sub.recv(0.5)
            if m is not None:
                got.add(m.topic)
        assert got == {T.RX_STATUS, T.RX_SPECTRUM}, "RX node must publish through the proxy"
        out, _ = p.communicate(timeout=40)
    finally:
        sub.close()
        if p.poll() is None:
            p.kill()
    assert p.returncode == 0, out
    assert "tüm süreçler temiz kapandı" in out and "--duration 6 s doldu" in out


def test_hardware_mode_without_soapysdr_gives_clear_error():
    if importlib.util.find_spec("SoapySDR") is not None:
        pytest.skip("SoapySDR kurulu: bu test yalnız SoapySDR'siz ortam içindir")
    p = _launch("--mode", "hardware", "--no-gui")
    out, _ = p.communicate(timeout=30)
    assert p.returncode == 1 and "SoapySDR" in out and "setup.sh --hardware" in out


@pytest.mark.realtime
def test_full_chain_with_gui_offscreen_screenshot(tmp_path):
    pytest.importorskip("PySide6.QtWidgets")
    pytest.importorskip("pyqtgraph")
    shot = tmp_path / "gui.png"
    env = dict(os.environ, QT_QPA_PLATFORM="offscreen")
    p = _launch("--screenshot", str(shot), "--after", "6", "--frontend-port", str(free_port()),
                "--backend-port", str(free_port()), env=env)
    out, _ = p.communicate(timeout=90)
    assert p.returncode == 0, out
    assert "GUI penceresi kapatıldı" in out and "tüm süreçler temiz kapandı" in out
    assert shot.stat().st_size > 50_000
