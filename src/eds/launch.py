"""One-command launcher: bus proxy + RX node + operator GUI, stopped together with Ctrl-C.

    python -m eds.launch                       # = --mode sim (config/sim_gui.toml), GUI açık
    python -m eds.launch --mode hardware       # gerçek Pluto (config/rx_pluto.toml)
    python -m eds.launch --no-gui --listen-all # yalnız hat (ör. Jetson); GUI başka makineden bağlanır
    python -m eds.launch --gui-only --connect tcp://<jetson-ip>:5551

Topology (every process is separate, so the GUI can never slow the pipeline down)::

    RX düğümü ──PUB──► [XSUB :5550  proxy  XPUB :5551] ──SUB──► GUI, eds bus echo, kendi programlarınız
                                         ▲
               ctl.rx komutları (eds bus send) ─┘    (düğüm ctl.rx'i proxy'nin arka ucundan dinler)

The proxy lets several producers (RX node now; DF / measurement nodes later) and several
subscribers share two well-known ports. Children are started in their own process groups:
Ctrl-C reaches only the launcher, which then stops GUI → RX node → proxy in that order
(SIGINT = graceful on POSIX; terminate on Windows) and kills whatever does not exit in time.
"""

from __future__ import annotations

import argparse
import importlib.util
import os
import signal
import subprocess
import sys
import time
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path

from .core.paths import config_dir

MODE_CONFIG = {"sim": "sim_gui.toml", "hardware": "rx_pluto.toml"}


@dataclass
class Child:
    name: str
    proc: subprocess.Popen
    critical: bool = True          # its exit stops everything


def _child_env() -> dict[str, str]:
    """Children import the same ``eds`` as the launcher, also when run from a source tree."""
    env = dict(os.environ)
    src = str(Path(__file__).resolve().parents[1])
    env["PYTHONPATH"] = src + (os.pathsep + env["PYTHONPATH"] if env.get("PYTHONPATH") else "")
    env.setdefault("PYTHONUNBUFFERED", "1")
    return env


def _spawn(name: str, args: list[str], critical: bool = True) -> Child:
    kw: dict = {"env": _child_env()}
    if os.name == "posix":
        kw["start_new_session"] = True             # terminal Ctrl-C goes to the launcher only
    else:  # pragma: no cover - Windows
        kw["creationflags"] = subprocess.CREATE_NEW_PROCESS_GROUP
    print(f"[launch] başlatılıyor: {name}: {' '.join(args)}", flush=True)
    return Child(name, subprocess.Popen([sys.executable, *args], **kw), critical)


def _stop(child: Child, timeout_s: float) -> None:
    p = child.proc
    if p.poll() is not None:
        return
    try:
        if os.name == "posix":
            p.send_signal(signal.SIGINT)           # graceful: KeyboardInterrupt / Qt quit
        else:  # pragma: no cover - Windows
            p.terminate()
        p.wait(timeout_s)
    except subprocess.TimeoutExpired:
        print(f"[launch] {child.name} {timeout_s:.0f} s içinde kapanmadı → zorla sonlandırılıyor", flush=True)
        p.kill()
        p.wait(5)


def _has(mod: str) -> bool:
    try:
        return importlib.util.find_spec(mod) is not None
    except (ImportError, ValueError):
        return False


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="python -m eds.launch",
        description="EDS'yi tek komutla başlat: bus proxy + RX düğümü + operatör GUI. Ctrl-C hepsini kapatır.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="Örnekler:\n"
               "  python -m eds.launch                         # simülasyon + GUI\n"
               "  python -m eds.launch --mode hardware          # Pluto + GUI\n"
               "  python -m eds.launch --no-gui --listen-all    # yalnız hat (uzak GUI için)\n"
               "  python -m eds.launch --gui-only --connect tcp://192.168.1.20:5551\n")
    p.add_argument("--mode", choices=("sim", "hardware"), default="sim",
                   help="sim = simülatör (SoapySDR gerekmez); hardware = gerçek Pluto (vars.: sim)")
    p.add_argument("-c", "--config", metavar="DOSYA",
                   help="RX konfigürasyonu (vars.: sim → config/sim_gui.toml, hardware → config/rx_pluto.toml)")
    p.add_argument("--set", action="append", default=[], metavar="ANAHTAR=DEĞER",
                   help="RX düğümüne iletilecek konfigürasyon geçersiz kılma (tekrarlanabilir)")
    g = p.add_mutually_exclusive_group()
    g.add_argument("--no-gui", action="store_true", help="GUI'yi açma (yalnız proxy + RX düğümü)")
    g.add_argument("--gui-only", action="store_true", help="yalnız GUI; çalışan bir hatta --connect ile bağlan")
    p.add_argument("--connect", metavar="UÇ", help="--gui-only için abone uç noktası (vars.: tcp://127.0.0.1:<backend>)")
    p.add_argument("--frontend-port", type=int, default=5550, help="proxy yayıncı portu (vars.: 5550)")
    p.add_argument("--backend-port", type=int, default=5551, help="proxy abone portu (vars.: 5551)")
    p.add_argument("--listen-all", action="store_true",
                   help="proxy portlarını tüm ağ arayüzlerinde aç (başka makinedeki GUI için)")
    p.add_argument("--duration", type=float, default=0.0, help="bu kadar saniye sonra her şeyi kapat (0 = Ctrl-C'ye kadar)")
    p.add_argument("--screenshot", metavar="PNG", help="GUI bu süre sonunda ekran görüntüsü alıp kapanır (belge/test)")
    p.add_argument("--after", type=float, default=10.0, help="--screenshot bekleme süresi [s] (vars.: 10)")
    return p


def main(argv: Sequence[str] | None = None) -> int:
    a = build_parser().parse_args(list(argv) if argv is not None else None)
    host = "0.0.0.0" if a.listen_all else "127.0.0.1"
    fe_bind, be_bind = f"tcp://{host}:{a.frontend_port}", f"tcp://{host}:{a.backend_port}"
    fe_conn, be_conn = f"tcp://127.0.0.1:{a.frontend_port}", f"tcp://127.0.0.1:{a.backend_port}"
    want_gui = not a.no_gui
    want_pipeline = not a.gui_only

    if want_gui and not (_has("PySide6") and _has("pyqtgraph")):
        print("HATA: GUI bağımlılıkları kurulu değil (PySide6, pyqtgraph).\n"
              "  Kurulum: ./setup.sh --sim   ya da   pip install -e \".[gui]\"\n"
              "  GUI'siz çalıştırmak için: python -m eds.launch --no-gui", file=sys.stderr)
        return 1
    cfg = a.config or str(config_dir() / MODE_CONFIG[a.mode])
    if want_pipeline:
        if not Path(cfg).is_file():
            print(f"HATA: konfigürasyon dosyası yok: {cfg}", file=sys.stderr)
            return 1
        if a.mode == "hardware" and not _has("SoapySDR"):
            print("HATA: donanım modu SoapySDR Python bağlamalarını gerektirir (bulunamadı).\n"
                  "  Kurulum: ./setup.sh --hardware   (ya da conda: environment.yml, bkz. docs/KURULUM.md)\n"
                  "  Donanımsız denemek için: python -m eds.launch --mode sim", file=sys.stderr)
            return 1

    children: list[Child] = []
    stop_reason = ""
    stopping = False

    def _on_signal(signum, _frame) -> None:
        nonlocal stopping, stop_reason
        stopping, stop_reason = True, f"sinyal {signal.Signals(signum).name}"

    signal.signal(signal.SIGINT, _on_signal)
    if hasattr(signal, "SIGTERM"):
        signal.signal(signal.SIGTERM, _on_signal)

    rc = 0
    try:
        if want_pipeline:
            children.append(_spawn("bus proxy", ["-m", "eds", "bus", "proxy", "--frontend", fe_bind,
                                                 "--backend", be_bind]))
            time.sleep(0.5)
            node_args = ["-m", "eds", "-c", cfg, "--set", 'bus.mode="proxy"',
                         "--set", f'bus.proxy_frontend="{fe_conn}"', "--set", f'bus.proxy_backend="{be_conn}"']
            for kv in a.set:
                node_args += ["--set", kv]
            if a.mode == "sim":
                node_args += ["--backend", "sim"]
            children.append(_spawn("RX düğümü", node_args + ["rx"]))
        if want_gui:
            ep = a.connect or be_conn
            gui_args = ["-m", "eds.gui", "--connect", ep]
            if a.screenshot:
                gui_args += ["--screenshot", a.screenshot, "--after", str(a.after)]
            children.append(_spawn("GUI", gui_args))

        print("[launch] çalışıyor. Durdurmak için Ctrl-C" + (" ya da GUI penceresini kapatın." if want_gui else "."),
              flush=True)
        if want_pipeline:
            print(f"[launch] bus: yayıncılar → {fe_conn} · aboneler → {be_conn}"
                  + (f" (ağdan: tcp://<bu-makine-ip>:{a.backend_port})" if a.listen_all else ""), flush=True)
            print("[launch] komut örneği: eds --set bus.mode=\"proxy\" bus send ctl.rx --cmd set_center_freq "
                  "--arg hz=434.5e6 --wait-ack 2", flush=True)
        t_end = time.monotonic() + a.duration if a.duration > 0 else None
        while not stopping:
            for ch in children:
                code = ch.proc.poll()
                if code is not None:
                    stopping = True
                    if ch.name == "GUI" and (code == 0 or a.screenshot):
                        stop_reason = "GUI penceresi kapatıldı"
                    else:
                        stop_reason = f"{ch.name} beklenmedik şekilde durdu (çıkış kodu {code})"
                        rc = 1
                    break
            if t_end is not None and time.monotonic() >= t_end:
                stopping, stop_reason = True, f"--duration {a.duration:g} s doldu"
            time.sleep(0.2)
    finally:
        print(f"[launch] kapatılıyor ({stop_reason or 'çıkış'})…", flush=True)
        for ch in reversed(children):             # GUI → RX düğümü → proxy
            _stop(ch, timeout_s=8.0)
        alive = [c.name for c in children if c.proc.poll() is None]
        if alive:
            print(f"[launch] UYARI: hâlâ çalışan süreç(ler): {alive}", flush=True)
            rc = rc or 1
        else:
            print("[launch] tüm süreçler temiz kapandı.", flush=True)
    return rc


if __name__ == "__main__":
    raise SystemExit(main())
