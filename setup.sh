#!/usr/bin/env bash
# =============================================================================
# EDS — tek komutla kurulum (Ubuntu 22.04 / 24.04). İdempotent: tekrar çalıştırmak güvenlidir.
#
#   ./setup.sh --sim        Simülasyon + GUI. SDR sürücüsü KURULMAZ. Takımın çoğu bunu kullanır (~5 dk).
#   ./setup.sh --hardware   Yukarıdakiler + SDR yığını (libiio, SoapySDR, SoapyPlutoSDR, udev) ve
#                           sonunda Pluto'yu SoapySDRUtil --find ile doğrular.
#
# Seçenekler: --venv DİZİN (vars. <repo>/.venv)   --no-system (apt adımlarını atla)   --help
# Hangi paket neden kuruluyor: DEPENDENCIES.md
# =============================================================================
set -Eeuo pipefail

REPO_ROOT="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
VENV_DIR="${EDS_VENV_DIR:-${REPO_ROOT}/.venv}"
MODE=""
DO_SYSTEM=1
STEP=0
TOTAL=5

if [[ -t 1 ]]; then
    C_I=$'\033[1;34m'; C_OK=$'\033[1;32m'; C_W=$'\033[1;33m'; C_E=$'\033[1;31m'; C_0=$'\033[0m'
else
    C_I=""; C_OK=""; C_W=""; C_E=""; C_0=""
fi
step() { STEP=$((STEP + 1)); printf '\n%s[%d/%d]%s %s\n' "${C_I}" "${STEP}" "${TOTAL}" "${C_0}" "$*"; }
ok()   { printf '  %s✔%s %s\n' "${C_OK}" "${C_0}" "$*"; }
warn() { printf '  %s!%s %s\n' "${C_W}" "${C_0}" "$*" >&2; }
die()  { printf '\n%sHATA:%s %s\n' "${C_E}" "${C_0}" "$*" >&2; exit 1; }
trap 'die "beklenmeyen hata (satır ${LINENO}). Ayrıntı için çıktıyı yukarı kaydırın."' ERR

usage() {
    sed -n '3,11p' "${BASH_SOURCE[0]}" | sed 's/^# \{0,1\}//'
}

while [[ $# -gt 0 ]]; do
    case "$1" in
        --sim) MODE="sim" ;;
        --hardware|--hw) MODE="hardware" ;;
        --venv) shift; [[ $# -gt 0 ]] || die "--venv bir dizin ister"; VENV_DIR="$1" ;;
        --no-system) DO_SYSTEM=0 ;;
        -h|--help) usage; exit 0 ;;
        *) usage; die "bilinmeyen seçenek: $1" ;;
    esac
    shift
done
[[ -n "${MODE}" ]] || { usage; die "mod seçin: --sim ya da --hardware"; }
[[ "${MODE}" == "hardware" ]] && TOTAL=6

SUDO=()
if [[ ${EUID} -ne 0 ]]; then SUDO=(sudo); fi

apt_install() {   # yalnızca eksik paketleri kurar
    local missing=() p
    for p in "$@"; do
        dpkg -s "${p}" >/dev/null 2>&1 || missing+=("${p}")
    done
    if [[ ${#missing[@]} -eq 0 ]]; then ok "sistem paketleri zaten kurulu: $*"; return 0; fi
    if [[ ${DO_SYSTEM} -eq 0 ]]; then
        warn "eksik sistem paketleri (--no-system verildi, kurulmadı): ${missing[*]}"
        warn "elle kurun: sudo apt install ${missing[*]}"
        return 0
    fi
    command -v apt-get >/dev/null 2>&1 || die "apt-get yok (Ubuntu/Debian değil?). Şu paketleri kurun: ${missing[*]}"
    echo "  apt: ${missing[*]}"
    "${SUDO[@]}" apt-get update -qq
    DEBIAN_FRONTEND=noninteractive "${SUDO[@]}" apt-get install -y -qq "${missing[@]}"
    ok "kuruldu: ${missing[*]}"
}

# -----------------------------------------------------------------------------------------
step "Python kontrolü (≥ 3.10 gerekir)"
command -v python3 >/dev/null 2>&1 || die "python3 bulunamadı: sudo apt install python3"
python3 -c 'import sys; sys.exit(0 if sys.version_info >= (3, 10) else 1)' \
    || die "Python $(python3 -V 2>&1) çok eski; 3.10 ya da üstü gerekir (Ubuntu 22.04+)."
ok "$(python3 -V)"

step "Sistem kütüphaneleri (sanal ortam + Qt pencere sistemi)"
# python3-venv: sanal ortam; libegl1/libgl1/libxkbcommon0/libfontconfig1/libdbus-1-3/libxcb-cursor0:
# PySide6'nın (Qt 6) Linux'ta pencere açmak için istediği çalışma zamanı kütüphaneleri.
apt_install python3-venv python3-pip libegl1 libgl1 libxkbcommon0 libfontconfig1 libdbus-1-3 libxcb-cursor0

if [[ "${MODE}" == "hardware" ]]; then
    step "SDR yığını: libiio, SoapySDR, SoapyPlutoSDR, Python bağlamaları, udev kuralı"
    # Mevcut ve incelenmiş betik: apt paketleri; SoapyPlutoSDR apt'te yoksa kaynaktan derler;
    # udev kuralı + plugdev grubu. Sanal ortamı burada (aşağıda) kendimiz kuruyoruz.
    if [[ ${DO_SYSTEM} -eq 1 ]]; then
        "${REPO_ROOT}/scripts/install_ubuntu.sh" --no-venv
    else
        warn "--no-system: SDR yığını kurulmadı (scripts/install_ubuntu.sh --no-venv ile kurun)"
    fi
fi

step "Sanal ortam: ${VENV_DIR}"
VENV_ARGS=()
if [[ "${MODE}" == "hardware" ]]; then
    VENV_ARGS+=(--system-site-packages)   # apt ile gelen SoapySDR / iio Python bağlamaları görünsün
fi
if [[ -x "${VENV_DIR}/bin/python" ]]; then
    ok "mevcut sanal ortam kullanılıyor"
    if [[ "${MODE}" == "hardware" ]] && ! grep -qi '^include-system-site-packages *= *true' "${VENV_DIR}/pyvenv.cfg"; then
        sed -i 's/^include-system-site-packages *=.*/include-system-site-packages = true/I' "${VENV_DIR}/pyvenv.cfg"
        ok "sistem site-packages erişimi açıldı (SoapySDR/iio bağlamaları için)"
    fi
else
    python3 -m venv "${VENV_ARGS[@]}" "${VENV_DIR}"
    ok "oluşturuldu"
fi
PY="${VENV_DIR}/bin/python"

step "Python paketleri (pip): eds + GUI (PySide6, pyqtgraph) + test araçları"
"${PY}" -m pip install -q --upgrade pip
"${PY}" -m pip install -q -e "${REPO_ROOT}[gui,dev]"
ok "eds $("${PY}" -c 'import eds; print(eds.__version__)') düzenlenebilir modda kuruldu"

step "Doğrulama"
"${PY}" -c "import eds, numpy, zmq; print('  eds', eds.__version__, '| numpy', numpy.__version__, '| pyzmq', zmq.__version__)"
"${PY}" -c "import PySide6, pyqtgraph; print('  PySide6', PySide6.__version__, '| pyqtgraph', pyqtgraph.__version__)"
SHOT="$(mktemp -t eds-gui-XXXXXX.png)"
if QT_QPA_PLATFORM=offscreen "${PY}" -m eds.gui --connect tcp://127.0.0.1:1 --screenshot "${SHOT}" --after 0.5 >/dev/null 2>&1; then
    ok "GUI açılıyor (ekransız deneme)"
else
    die "GUI açılamadı. Qt kütüphaneleri eksik olabilir: sudo apt install libegl1 libxkbcommon0 libxcb-cursor0"
fi
rm -f "${SHOT}"
"${VENV_DIR}/bin/eds" profiles >/dev/null && ok "eds komutu çalışıyor"

if [[ "${MODE}" == "hardware" ]]; then
    "${PY}" -c "import SoapySDR, iio" 2>/dev/null \
        || die "SoapySDR / iio Python bağlamaları sanal ortamdan görünmüyor. 'scripts/install_ubuntu.sh' çıktısını kontrol edin."
    ok "SoapySDR ve libiio Python bağlamaları içe aktarılıyor"
    SoapySDRUtil --info 2>/dev/null | grep -q "plutosdr" \
        || die "SoapySDR 'plutosdr' sürücüsünü bulamıyor (SoapyPlutoSDR kurulmamış). scripts/install_ubuntu.sh'yi tekrar çalıştırın."
    ok "SoapyPlutoSDR sürücüsü yüklü"
    if SoapySDRUtil --find="driver=plutosdr" 2>/dev/null | grep -q "driver *= *plutosdr"; then
        ok "Pluto bulundu:"
        SoapySDRUtil --find="driver=plutosdr" 2>/dev/null | grep -E "uri|label|serial" | sed 's/^/      /'
    else
        printf '\n%sHATA: Pluto bulunamadı.%s Kurulum tamam ama cihaz görünmüyor. Kontrol edin:\n' "${C_E}" "${C_0}" >&2
        printf '  1) Pluto takılı mı, kablo VERİ kablosu mu (yalnız şarj kabloları çalışmaz)?\n' >&2
        printf '  2) lsusb çıktısında 0456:b673 var mı?\n' >&2
        printf '  3) Bu kurulumdan sonra oturumu kapatıp açtınız mı? (plugdev grubu ancak böyle geçerli olur)\n' >&2
        printf '  4) Sonra tekrar: SoapySDRUtil --find="driver=plutosdr"\n' >&2
        printf '  Donanımsız denemek için: %s -m eds.launch --mode sim\n' "${PY}" >&2
        exit 2
    fi
fi

printf '\n%sKurulum tamam.%s Şunu çalıştırın:\n' "${C_OK}" "${C_0}"
printf '  source %s/bin/activate\n' "${VENV_DIR}"
if [[ "${MODE}" == "hardware" ]]; then
    printf '  python run.py --mode hardware     # Pluto + operatör GUI  (ya da: make run)\n'
else
    printf '  python run.py                     # simülasyon + operatör GUI  (ya da: make run-sim)\n'
fi
printf '  make help                         # diğer komutlar\n'
