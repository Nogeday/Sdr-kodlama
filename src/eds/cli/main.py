"""``eds`` command-line tool (entry point ``eds.cli.main:main``; also ``python -m eds``).

Global options (accepted before or after the subcommand)::

    -c/--config PATH     TOML configuration (none: built-in defaults = one simulated device)
    --set KEY=VALUE      dotted-key override, value parsed as a TOML literal (repeatable)
    --backend sim|soapy  backend of every configured device
    -v/--verbose         more log output (-vv: debug) and tracebacks on errors

Exit codes: 0 success, 1 error (configuration / device / bus — Turkish message on stderr),
2 a measurement (``measure``) or a command acknowledgement (``bus send``) failed, 130 Ctrl-C.
"""

from __future__ import annotations

import argparse
import contextlib
import logging
import os
import sys
import traceback
from collections.abc import Callable, Sequence

from .. import __version__
from ..core.errors import EDSError

log = logging.getLogger("eds.cli")

EXIT_OK, EXIT_ERROR, EXIT_FAILED, EXIT_INTERRUPTED = 0, 1, 2, 130
_HANDLER_FLAG = "_eds_cli_handler"

DESCRIPTION = """\
EDS — KTÜ TEKNOFEST Elektronik Harp takımı Elektronik Destek sistemi.
RX düğümü, geniş bant tarama, kalibrasyon, KTR nicel spesifikasyon ölçümleri ve ZeroMQ bus araçları.

Örnekler:
  eds profiles
  eds info -c config/rx_pluto.toml
  eds rx -c config/rx_sim.toml --duration 10
  eds sweep --start 400e6 --stop 500e6 --png tarama.png --out tarama.npz
  eds measure all -c config/rx_sim.toml --quick
  eds cal ppm --gsm-arfcn 1 -c config/rx_pluto.toml
  eds bus echo rx.detections
  eds bus send ctl.rx --cmd set_center_freq --arg hz=2.4e9 --wait-ack 2
"""


class _Parser(argparse.ArgumentParser):
    """ArgumentParser with Turkish usage errors and exit code 1 for them."""

    def __init__(self, *a, **kw):
        kw.setdefault("add_help", False)
        kw.setdefault("formatter_class", argparse.RawDescriptionHelpFormatter)
        super().__init__(*a, **kw)
        self._positionals.title = "konumsal argümanlar"
        self._optionals.title = "seçenekler"
        self.add_argument("-h", "--help", action="help", default=argparse.SUPPRESS,
                          help="bu yardım metnini göster ve çık")

    def error(self, message: str) -> None:  # type: ignore[override]
        self.print_usage(sys.stderr)
        self.exit(EXIT_ERROR, f"{self.prog}: hata: {translate_usage_error(message)}\n")


#: argparse (English) usage-error fragments → Turkish (operator-facing text is Turkish).
_USAGE_TR: tuple[tuple[str, str], ...] = (
    ("the following arguments are required: ", "şu argümanlar gerekli: "),
    ("unrecognized arguments: ", "tanınmayan argümanlar: "),
    ("one of the arguments ", "şu argümanlardan biri gerekli: "),
    (" is required", ""),
    (": not allowed with argument ", ": şu argümanla birlikte kullanılamaz: "),
    ("invalid choice: ", "geçersiz seçim: "),
    ("(choose from ", "(seçenekler: "),
    ("expected at least one argument", "en az bir değer bekleniyordu"),
    ("expected one argument", "bir değer bekleniyordu"),
    ("invalid int value: ", "geçersiz tamsayı: "),
    ("invalid float value: ", "geçersiz sayı: "),
    ("argument ", "argüman "),
)


def translate_usage_error(message: str) -> str:
    """Turkish rendering of the common argparse usage-error messages (unknown ones unchanged)."""
    for en, tr in _USAGE_TR:
        message = message.replace(en, tr)
    return message


def _add_globals(p: argparse.ArgumentParser, top: bool, skip: Sequence[str] = ()) -> None:
    """Global options; on subparsers their default is SUPPRESS so a value given before the
    subcommand is not overwritten."""
    d = (lambda v: v) if top else (lambda v: argparse.SUPPRESS)
    g = p.add_argument_group("genel seçenekler")
    g.add_argument("-c", "--config", metavar="DOSYA", default=d(None),
                   help="konfigürasyon dosyası (TOML); verilmezse yerleşik varsayılanlar (tek simülatör cihazı)")
    g.add_argument("--set", metavar="ANAHTAR=DEĞER", action="append", default=d([]),
                   help="konfigürasyon değerini geçersiz kıl (noktalı anahtar, değer TOML sözdizimi; ör. "
                        "--set rx.center_freq_hz=2.4e9 --set device.sim.realtime=false); tekrarlanabilir")
    if "backend" not in skip:
        g.add_argument("--backend", choices=("sim", "soapy"), default=d(None),
                       help="tüm cihazların backend'ini geçersiz kıl")
    g.add_argument("-v", "--verbose", action="count", default=d(0),
                   help="ayrıntılı günlük (-vv: hata ayıklama) ve hatalarda iz (traceback)")


def _sub(subs: argparse._SubParsersAction, name: str, help_: str, *, skip: Sequence[str] = (),
         description: str | None = None) -> argparse.ArgumentParser:
    p = subs.add_parser(name, help=help_, description=description or help_)
    _add_globals(p, top=False, skip=skip)
    return p


def build_parser() -> argparse.ArgumentParser:
    from ..measure.base import SPEC_IDS
    from ..rx.node import COMMANDS
    from . import cmd_bus, cmd_cal, cmd_hw, cmd_measure
    from .util import finite_float, nonneg_float, positive_float, positive_int

    p = _Parser(prog="eds", description=DESCRIPTION)
    p.add_argument("--version", action="version", version=f"eds {__version__}",
                   help="sürüm bilgisini göster ve çık")
    _add_globals(p, top=True)
    subs = p.add_subparsers(dest="command", metavar="KOMUT", title="komutlar", parser_class=_Parser)
    subs.required = True

    # ---------------------------------------------------------------- profiles / devices / info
    sp = _sub(subs, "profiles", "cihaz profillerini listele (çip, durum, frekans aralığı, BW, kanal, arayüz)")
    sp.set_defaults(func=cmd_hw.cmd_profiles)

    sp = _sub(subs, "devices", "bağlı cihazları listele (--probe: aç ve özellikleri/sensörleri yazdır)",
              skip=("backend",))
    sp.add_argument("--backend", choices=("soapy", "sim"), default=argparse.SUPPRESS,
                    help="taranacak backend (varsayılan: konfigürasyondaki cihazların backend'leri)")
    sp.add_argument("--args", nargs="*", metavar="ANAHTAR=DEĞER", default=[],
                    help="SoapySDR tarama filtresi (ör. driver=plutosdr)")
    sp.add_argument("--probe", action="store_true",
                    help="her cihazı aç; describe(), runtime_limits(), read_sensors() çıktısını yazdır")
    sp.set_defaults(func=cmd_hw.cmd_devices)

    sp = _sub(subs, "info", "etkin konfigürasyon özeti, profil sınırları ve RX ayarlarının doğrulaması")
    sp.set_defaults(func=cmd_hw.cmd_info)

    # ---------------------------------------------------------------- rx / sweep
    sp = _sub(subs, "rx", "RX düğümünü çalıştır (akış → PSD → CFAR → ZeroMQ); stderr'de periyodik durum satırı")
    sp.add_argument("--duration", type=nonneg_float, metavar="S", default=None,
                    help="çalışma süresi [s] (varsayılan: Ctrl-C ya da 'stop' komutuna kadar)")
    sp.set_defaults(func=cmd_hw.cmd_rx)

    sp = _sub(subs, "sweep", "tek geniş bant tarama (hız + tespit tablosu, isteğe bağlı npz/PNG/bus)")
    sp.add_argument("--start", type=positive_float, metavar="HZ",
                    help="başlangıç frekansı [Hz] (varsayılan sweep.start_hz)")
    sp.add_argument("--stop", type=positive_float, metavar="HZ", help="bitiş frekansı [Hz] (varsayılan sweep.stop_hz)")
    sp.add_argument("--rbw", type=positive_float, metavar="HZ",
                    help="çözünürlük bant genişliği [Hz] (nfft'yi belirler)")
    sp.add_argument("--fs", type=positive_float, metavar="HZ", help="burst örnekleme hızı [S/s]")
    sp.add_argument("--out", metavar="DOSYA.npz", help="sonucu kaydet (f_hz, p_db, meta JSON)")
    sp.add_argument("--png", metavar="DOSYA", help="spektrum analizörü ekran görüntüsü (PNG)")
    sp.add_argument("--publish", action="store_true", help="sonucu rx.sweep olarak bus'a yayınla")
    sp.set_defaults(func=cmd_hw.cmd_sweep)

    # ---------------------------------------------------------------- cal
    cal = _sub(subs, "cal", "kalibrasyon (ppm, güç, koherens)")
    cal_subs = cal.add_subparsers(dest="cal_command", metavar="ALT_KOMUT", title="kalibrasyon komutları",
                                  parser_class=_Parser)
    cal_subs.required = True
    sp = _sub(cal_subs, "ppm", "referans saat (TCXO) ppm kalibrasyonu: CW referans ya da GSM FCCH")
    src = sp.add_mutually_exclusive_group(required=True)
    src.add_argument("--ref-hz", type=positive_float, metavar="F",
                     help="bilinen frekanslı CW referans [Hz] (GPSDO kilitli)")
    src.add_argument("--gsm-freq", type=positive_float, metavar="F", help="GSM BCCH taşıyıcı frekansı [Hz] (FCCH)")
    src.add_argument("--gsm-arfcn", type=int, metavar="N", help="GSM BCCH ARFCN numarası (FCCH)")
    sp.add_argument("--band", choices=("GSM900", "DCS1800"), default=None,
                    help="ARFCN bandı (varsayılan: numaradan otomatik)")
    sp.add_argument("--repeats", type=positive_int, metavar="N", default=None,
                    help="CW: tekrar sayısı (vars. 5); FCCH: burst sayısı (vars. 10, en az 3)")
    sp.add_argument("--no-save", action="store_true", help="kalibrasyon deposuna kaydetme")
    sp.add_argument("--apply", action="store_true", help="sonucu cihaza uygula (cal.ppm_mode'a göre)")
    sp.add_argument("--publish", action="store_true", help="cal.result mesajı yayınla")
    sp.set_defaults(func=cmd_cal.cmd_cal_ppm)

    sp = _sub(cal_subs, "power", "mutlak güç kalibrasyonu (dBFS → dBm ofset tablosu, kalibre kaynakla)")
    sp.add_argument("--freqs", type=positive_float, nargs="+", required=True, metavar="HZ",
                    help="ölçüm frekansları [Hz]")
    sp.add_argument("--gains", type=finite_float, nargs="+", required=True, metavar="DB", help="RX kazançları [dB]")
    sp.add_argument("--p-in", type=finite_float, nargs="+", required=True, metavar="DBM",
                    help="RX girişindeki kaynak gücü [dBm] (tek değer ya da kazanç başına bir değer)")
    sp.add_argument("--no-save", action="store_true", help="kalibrasyon deposuna kaydetme")
    sp.add_argument("--publish", action="store_true", help="cal.result mesajı yayınla")
    sp.set_defaults(func=cmd_cal.cmd_cal_power)

    sp = _sub(cal_subs, "coherence", "çok kanallı grubun (2× Pluto) koherens kontrolü; depoya kaydet, cal.result yayınla")
    sp.add_argument("--n", type=positive_int, default=65536, metavar="N", help="blok başına örnek (vars. 65536)")
    sp.add_argument("--blocks", type=positive_int, default=8, metavar="N", help="blok sayısı (vars. 8)")
    sp.add_argument("--no-save", action="store_true", help="kalibrasyon deposuna kaydetme")
    sp.add_argument("--no-publish", action="store_true", help="cal.result yayınlama")
    sp.set_defaults(func=cmd_cal.cmd_cal_coherence)

    # ---------------------------------------------------------------- measure
    sp = _sub(subs, "measure", "KTR nicel spesifikasyon ölçümleri + rapor (tablo, grafik, ekran görüntüsü)",
              description="KTR ölçümleri: " + ", ".join(SPEC_IDS) + ". Çıkış kodu: 0 = başarısız ölçüm yok "
                          "(atlanan sayılmaz), 2 = en az bir ölçüm başarısız, 1 = konfigürasyon hatası.")
    sp.add_argument("specs", nargs="*", metavar="ÖLÇÜM", help="ölçüm kimlikleri ya da 'all' (varsayılan: all)")
    sp.add_argument("--quick", action="store_true", help="hızlı mod (daha az nokta / kısa süre; CI, demo)")
    sp.add_argument("--source", choices=("sim", "pluto_tx", "scpi", "manual", "none"), default=None,
                    help="sinyal kaynağı türü (varsayılan measure.source)")
    sp.add_argument("--out", metavar="DİZİN", default=None, help="rapor kök dizini (varsayılan measure.out_dir)")
    sp.add_argument("--publish", action="store_true", help="her sonucu meas.result olarak yayınla")
    sp.add_argument("--no-report", action="store_true", help="rapor klasörü yazma")
    sp.add_argument("--title", metavar="BAŞLIK", default=None, help="rapor başlığı")
    sp.add_argument("--yes", action="store_true", help="operatör onaylarını otomatik geç (donanım)")
    sp.set_defaults(func=cmd_measure.cmd_measure)

    # ---------------------------------------------------------------- bus
    bus = _sub(subs, "bus", "ZeroMQ bus araçları (proxy, dinleme, komut gönderme)")
    bus_subs = bus.add_subparsers(dest="bus_command", metavar="ALT_KOMUT", title="bus komutları",
                                  parser_class=_Parser)
    bus_subs.required = True
    sp = _sub(bus_subs, "proxy", "XSUB/XPUB aracı (proxy) çalıştır", skip=("backend",))
    sp.add_argument("--frontend", metavar="UÇ", default=None, help="yayıncıların bağlandığı uç (vars. bus.proxy_frontend)")
    sp.add_argument("--backend", dest="backend_ep", metavar="UÇ", default=None,
                    help="abonelerin bağlandığı uç (vars. bus.proxy_backend)")
    sp.add_argument("--duration", type=nonneg_float, metavar="S", default=None,
                    help="çalışma süresi [s] (vars. Ctrl-C)")
    sp.set_defaults(func=cmd_bus.cmd_bus_proxy)

    sp = _sub(bus_subs, "echo", "bus mesajlarını dinle ve her birini tek satır yazdır")
    sp.add_argument("topics", nargs="*", metavar="TOPIC", help="dinlenecek topic'ler (varsayılan: hepsi)")
    sp.add_argument("--endpoint", metavar="UÇ", default=None,
                    help="bağlanılacak uç (vars. direct: bus.sub_endpoint, proxy: bus.proxy_backend)")
    sp.add_argument("--count", type=positive_int, metavar="N", default=None, help="N mesajdan sonra çık")
    sp.add_argument("--timeout", type=nonneg_float, metavar="S", default=None, help="en fazla S saniye dinle")
    sp.add_argument("--raw", action="store_true", help="zarfın tamamını JSON olarak yazdır")
    sp.set_defaults(func=cmd_bus.cmd_bus_echo)

    sp = _sub(bus_subs, "send", "RX düğümlerine ctl.rx komutu gönder (isteğe bağlı ctl.ack bekle)")
    sp.add_argument("topic", choices=("ctl.rx",), metavar="TOPIC", help="ctl.rx")
    sp.add_argument("--cmd", required=True, choices=COMMANDS, metavar="KOMUT",
                    help="komut: " + ", ".join(COMMANDS))
    sp.add_argument("--arg", action="append", default=[], metavar="ANAHTAR=DEĞER",
                    help="komut argümanı (değer TOML sözdizimi; ör. --arg hz=2.4e9); tekrarlanabilir")
    sp.add_argument("--target", default="*", metavar="AD", help="hedef cihaz adı ya da '*' (vars. '*')")
    sp.add_argument("--wait-ack", type=nonneg_float, default=0.0, metavar="S",
                    help="ctl.ack için en fazla S saniye bekle")
    sp.add_argument("--endpoint", metavar="UÇ", default=None,
                    help="komut uç noktası (direct: bind, vars. bus.ctl_endpoints[0]; proxy: bus.proxy_frontend)")
    sp.add_argument("--ack-endpoint", metavar="UÇ", default=None,
                    help="ctl.ack dinleme ucu (vars. direct: bus.sub_endpoint, proxy: bus.proxy_backend)")
    sp.add_argument("--sub-timeout", type=nonneg_float, default=3.0, metavar="S",
                    help="komutu dinleyen düğümü bekleme süresi [s] (vars. 3)")
    sp.add_argument("--req-id", default=None, metavar="ID", help="istek kimliği (vars. rastgele)")
    sp.add_argument("--src", default="cli", metavar="AD", help="zarf 'src' alanı (vars. cli)")
    sp.set_defaults(func=cmd_bus.cmd_bus_send)
    return p


def _setup_logging(verbosity: int) -> tuple[logging.Handler, int]:
    """Attach a stderr handler to the ``eds`` logger for this invocation. Returns the
    handler and the previous logger level (both restored by :func:`main`)."""
    logger = logging.getLogger("eds")
    for h in list(logger.handlers):
        if getattr(h, _HANDLER_FLAG, False):
            logger.removeHandler(h)
    handler = logging.StreamHandler(sys.stderr)
    setattr(handler, _HANDLER_FLAG, True)
    handler.setFormatter(logging.Formatter("%(levelname)s %(name)s: %(message)s"))
    level = logging.WARNING if verbosity <= 0 else (logging.INFO if verbosity == 1 else logging.DEBUG)
    handler.setLevel(level)
    old = logger.level
    logger.addHandler(handler)
    logger.setLevel(level)
    return handler, old


def main(argv: Sequence[str] | None = None) -> int:
    """Run the ``eds`` CLI; returns the process exit code."""
    parser = build_parser()
    try:
        args = parser.parse_args(list(argv) if argv is not None else None)
    except SystemExit as e:              # --help / --version / usage error
        return int(e.code) if isinstance(e.code, int) else (EXIT_OK if e.code is None else EXIT_ERROR)
    verbose = int(getattr(args, "verbose", 0) or 0)
    handler, old_level = _setup_logging(verbose)
    func: Callable[[argparse.Namespace], int] = args.func
    try:
        return int(func(args))
    except KeyboardInterrupt:
        print("İptal edildi (Ctrl-C).", file=sys.stderr)
        return EXIT_INTERRUPTED
    except BrokenPipeError:              # output piped into e.g. `head`: stop quietly
        with contextlib.suppress(OSError, ValueError):   # stdout without a real descriptor
            devnull = os.open(os.devnull, os.O_WRONLY)
            os.dup2(devnull, sys.stdout.fileno())
            os.close(devnull)
        return EXIT_OK
    except EDSError as e:
        print(f"HATA: {e}", file=sys.stderr)
        if verbose:
            traceback.print_exc(file=sys.stderr)
        return EXIT_ERROR
    except Exception as e:  # a bug: always show where it happened
        print(f"Beklenmeyen hata ({type(e).__name__}): {e}", file=sys.stderr)
        traceback.print_exc(file=sys.stderr)
        return EXIT_ERROR
    finally:
        with contextlib.suppress(BrokenPipeError):
            sys.stdout.flush()
        logger = logging.getLogger("eds")
        logger.removeHandler(handler)
        logger.setLevel(old_level)
        handler.close()


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
