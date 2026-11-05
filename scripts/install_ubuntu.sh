#!/usr/bin/env bash
# =============================================================================
# EDS (KTÜ EH — Elektronik Destek RX omurgası) — Ubuntu 22.04 / 24.04 kurulum betiği
# =============================================================================
# İdempotent: tekrar çalıştırmak güvenlidir; yalnızca eksik olanı kurar/günceller.
#
#   1. apt paketleri (libiio, libad9361, SoapySDR, Python bağlamaları, ZeroMQ, derleme araçları)
#   2. SoapyPlutoSDR sürücüsü — "SoapySDRUtil --info" plutosdr göstermiyorsa kaynaktan derlenir
#   3. udev kuralı (ADI 0456:b673 / 0456:b674) + udevadm reload, kullanıcı → plugdev grubu
#   4. .venv (--system-site-packages: sistemdeki SoapySDR/iio bağlamaları görünür) +
#      pip install -e ".[dev]"
#   5. Son kontroller: SoapySDRUtil --find="driver=plutosdr", iio_info -s,
#      python -c "import SoapySDR, iio"
#
# Kullanım:  scripts/install_ubuntu.sh [--no-build] [--no-venv] [--help]
# Ortam değişkenleri (isteğe bağlı):
#   EDS_VENV_DIR          sanal ortam dizini            (vars: <repo>/.venv)
#   EDS_BUILD_DIR         kaynak derleme dizini         (vars: ~/.cache/eds-build)
#   SOAPY_PLUTOSDR_REPO   SoapyPlutoSDR git deposu      (vars: https://github.com/pothosware/SoapyPlutoSDR)
#   SOAPY_PLUTOSDR_REF    dal/etiket/commit             (vars: master)
# =============================================================================
# SC2310 (dosya geneli): pkg_installed / soapy_has_plutosdr / venv_sees_system_site bilinçli olarak
# if/|| içinde kullanılan yüklem (predicate) fonksiyonlarıdır; içlerinde set -e'ye güvenilmez.
# shellcheck disable=SC2310
set -Eeuo pipefail

SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd -- "${SCRIPT_DIR}/.." && pwd)"
RULES_NAME="53-adi-plutosdr-usb.rules"
RULES_SRC="${SCRIPT_DIR}/${RULES_NAME}"
RULES_DST="/etc/udev/rules.d/${RULES_NAME}"
SYSTEM_PY="/usr/bin/python3"
SOAPY_PLUTO_REPO="${SOAPY_PLUTOSDR_REPO:-https://github.com/pothosware/SoapyPlutoSDR}"
SOAPY_PLUTO_REF="${SOAPY_PLUTOSDR_REF:-master}"

APT_PACKAGES=(
    python3-venv python3-pip
    libiio-dev libiio-utils python3-libiio libad9361-dev
    soapysdr-tools libsoapysdr-dev python3-soapysdr
    libzmq3-dev
    cmake g++ make git pkg-config ca-certificates
    libusb-1.0-0-dev            # SoapyPlutoSDR: taramada Pluto VID:PID ön kontrolü (HAS_LIBUSB1); "usb_direct" diye bir argüman YOK
)

DO_BUILD=1
DO_VENV=1
FAILED_CHECKS=0

# ----------------------------------------------------------------------------- çıktı
if [[ -t 1 ]]; then
    C_INFO=$'\033[1;34m'; C_OK=$'\033[1;32m'; C_WARN=$'\033[1;33m'; C_ERR=$'\033[1;31m'; C_OFF=$'\033[0m'
else
    C_INFO=""; C_OK=""; C_WARN=""; C_ERR=""; C_OFF=""
fi
info() { printf '%s[EDS]%s %s\n' "${C_INFO}" "${C_OFF}" "$*"; }
ok()   { printf '%s[ OK]%s %s\n' "${C_OK}" "${C_OFF}" "$*"; }
warn() { printf '%s[UYARI]%s %s\n' "${C_WARN}" "${C_OFF}" "$*" >&2; }
die()  { printf '%s[HATA]%s %s\n' "${C_ERR}" "${C_OFF}" "$*" >&2; exit 1; }

on_error() {
    local rc=$?
    printf '%s[HATA]%s Satır %s: "%s" başarısız (çıkış kodu %s). Kurulum yarıda kaldı; sorunu giderip betiği yeniden çalıştırın (idempotent).\n' \
        "${C_ERR}" "${C_OFF}" "${BASH_LINENO[0]}" "${BASH_COMMAND}" "${rc}" >&2
    exit "${rc}"
}
trap on_error ERR

usage() {
    cat <<EOF
Kullanım: $(basename "$0") [--no-build] [--no-venv] [--help]

EDS geliştirme/ölçüm ortamını Ubuntu 22.04 / 24.04 üzerinde kurar (idempotent).

Seçenekler:
  --no-build   SoapyPlutoSDR'yi kaynaktan derleme (yalnızca kontrol et, eksikse uyar)
  --no-venv    .venv oluşturma / pip install yapma (sistem python3 ile kontrol edilir)
  -h, --help   bu yardımı göster

Ortam değişkenleri: EDS_VENV_DIR, EDS_BUILD_DIR, SOAPY_PLUTOSDR_REPO, SOAPY_PLUTOSDR_REF
EOF
}

# ----------------------------------------------------------------------------- argümanlar
while (($#)); do
    case "$1" in
        --no-build) DO_BUILD=0 ;;
        --no-venv) DO_VENV=0 ;;
        -h | --help) usage; exit 0 ;;
        *) usage >&2; die "Bilinmeyen seçenek: $1" ;;
    esac
    shift
done

# ----------------------------------------------------------------------------- kullanıcı / sudo
if [[ ${EUID} -eq 0 ]]; then
    SUDO=()
    TARGET_USER="${SUDO_USER:-root}"
else
    command -v sudo >/dev/null 2>&1 || die "sudo bulunamadı; betiği root olarak ya da sudo yetkili bir kullanıcıyla çalıştırın."
    SUDO=(sudo)
    TARGET_USER="$(id -un)"
fi
TARGET_HOME="$(getent passwd "${TARGET_USER}" | cut -d: -f6)"
[[ -n ${TARGET_HOME} ]] || die "'${TARGET_USER}' kullanıcısının ev dizini bulunamadı."
VENV_DIR="${EDS_VENV_DIR:-${REPO_ROOT}/.venv}"
BUILD_ROOT="${EDS_BUILD_DIR:-${TARGET_HOME}/.cache/eds-build}"

# Komutu hedef (root olmayan) kullanıcı olarak çalıştır: .venv ve derleme dizini root'a ait olmasın.
as_user() {
    if [[ ${EUID} -eq 0 && ${TARGET_USER} != root ]]; then
        sudo -u "${TARGET_USER}" -H -- "$@"
    else
        "$@"
    fi
}

# ----------------------------------------------------------------------------- 0. ön kontrol
preflight() {
    [[ -r /etc/os-release ]] || die "/etc/os-release okunamadı; desteklenen sistemler: Ubuntu 22.04 / 24.04."
    # shellcheck disable=SC1091
    . /etc/os-release
    if [[ ${ID:-} != ubuntu ]]; then
        warn "Bu dağıtım '${ID:-?}'. Betik Ubuntu 22.04/24.04 için yazıldı; paket adları farklı olabilir."
    elif [[ ${VERSION_ID:-} != 22.04 && ${VERSION_ID:-} != 24.04 ]]; then
        warn "Ubuntu ${VERSION_ID:-?} test edilmedi (desteklenen: 22.04, 24.04). Devam ediliyor."
    else
        ok "Ubuntu ${VERSION_ID} algılandı."
    fi
    command -v apt-get >/dev/null 2>&1 || die "apt-get yok; Debian/Ubuntu tabanlı bir sistem gerekli."
    [[ -f ${RULES_SRC} ]] || die "udev kuralı bulunamadı: ${RULES_SRC}"
    [[ -f ${REPO_ROOT}/pyproject.toml ]] || die "Depo kökü bulunamadı (pyproject.toml yok): ${REPO_ROOT}"
}

# ----------------------------------------------------------------------------- 1. apt
# NOT: "cmd | grep -q" kalıbı pipefail altında kullanılmaz — grep erken çıkınca cmd SIGPIPE alır
# ve boru hattı "başarısız" görünür. Çıktı önce değişkene alınır.
pkg_installed() {
    [[ "$(dpkg-query -W -f='${Status}' "$1" 2>/dev/null || true)" == "install ok installed" ]]
}

install_apt_packages() {
    local missing=() pkg
    for pkg in "${APT_PACKAGES[@]}"; do
        pkg_installed "${pkg}" || missing+=("${pkg}")
    done
    if ((${#missing[@]} == 0)); then
        ok "apt paketleri zaten kurulu (${#APT_PACKAGES[@]} paket)."
        return
    fi
    info "Eksik apt paketleri kuruluyor: ${missing[*]}"
    "${SUDO[@]}" apt-get update -q
    local unavailable=()
    for pkg in "${missing[@]}"; do
        local policy
        policy="$(apt-cache policy "${pkg}" 2>/dev/null || true)"
        if ! grep -q "Candidate: [^(]" <<<"${policy}"; then
            unavailable+=("${pkg}")
        fi
    done
    if ((${#unavailable[@]})); then
        die "Depoda bulunamayan paket(ler): ${unavailable[*]}. 'universe' deposu etkin mi? (sudo add-apt-repository universe && sudo apt-get update)"
    fi
    "${SUDO[@]}" env DEBIAN_FRONTEND=noninteractive apt-get install -y --no-install-recommends "${missing[@]}"
    ok "apt paketleri kuruldu."
}

# ----------------------------------------------------------------------------- 2. SoapyPlutoSDR
soapy_has_plutosdr() {
    command -v SoapySDRUtil >/dev/null 2>&1 || return 1
    local out
    out="$(SoapySDRUtil --info 2>/dev/null || true)"
    grep -qiw "plutosdr" <<<"${out}"
}

build_soapy_plutosdr() {
    if soapy_has_plutosdr; then
        ok "SoapyPlutoSDR modülü zaten yüklü (SoapySDRUtil --info → plutosdr)."
        return
    fi
    if ((DO_BUILD == 0)); then
        warn "SoapyPlutoSDR modülü YOK ve --no-build verildi. Pluto SoapySDR üzerinden açılamaz."
        return
    fi
    local src="${BUILD_ROOT}/SoapyPlutoSDR"
    info "SoapyPlutoSDR kaynaktan derleniyor (${SOAPY_PLUTO_REPO} @ ${SOAPY_PLUTO_REF}) → ${src}"
    as_user mkdir -p "${BUILD_ROOT}"
    if [[ ! -d ${src}/.git ]]; then
        as_user git init -q "${src}"
        as_user git -C "${src}" remote add origin "${SOAPY_PLUTO_REPO}"
    else
        as_user git -C "${src}" remote set-url origin "${SOAPY_PLUTO_REPO}"
    fi
    as_user git -C "${src}" fetch -q --depth 1 origin "${SOAPY_PLUTO_REF}"
    as_user git -C "${src}" checkout -q --detach FETCH_HEAD
    local rev jobs
    rev="$(as_user git -C "${src}" rev-parse --short HEAD)"
    jobs="$(nproc)"
    info "SoapyPlutoSDR commit: ${rev}"
    as_user cmake -S "${src}" -B "${src}/build" -DCMAKE_BUILD_TYPE=Release
    as_user cmake --build "${src}/build" -j "${jobs}"
    "${SUDO[@]}" cmake --install "${src}/build"
    "${SUDO[@]}" ldconfig
    if soapy_has_plutosdr; then
        ok "SoapyPlutoSDR kuruldu."
    else
        SoapySDRUtil --info 2>&1 | sed -n '1,40p' >&2 || true
        die "SoapyPlutoSDR derlendi fakat SoapySDR modülü bulamıyor. Modül yolunu kontrol edin (yukarıdaki 'Search path'); gerekirse SOAPY_SDR_PLUGIN_PATH=/usr/local/lib/SoapySDR/modules0.8 ayarlayın."
    fi
}

# ----------------------------------------------------------------------------- 3. udev + plugdev
install_udev_rule() {
    if [[ -f ${RULES_DST} ]] && cmp -s "${RULES_SRC}" "${RULES_DST}"; then
        ok "udev kuralı güncel: ${RULES_DST}"
    else
        info "udev kuralı kuruluyor: ${RULES_DST}"
        "${SUDO[@]}" install -m 0644 "${RULES_SRC}" "${RULES_DST}"
    fi
    if command -v udevadm >/dev/null 2>&1 && [[ -d /run/udev ]]; then
        "${SUDO[@]}" udevadm control --reload-rules
        "${SUDO[@]}" udevadm trigger --subsystem-match=usb --attr-match=idVendor=0456 \
            || warn "udevadm trigger başarısız; Pluto'yu çıkarıp yeniden takın."
        ok "udev kuralları yeniden yüklendi (takılı Pluto'yu bir kez çıkarıp takmanız önerilir)."
    else
        warn "udev çalışmıyor (konteyner?) — kural kopyalandı, yeniden başlatma sonrası etkin olur."
    fi
}

ensure_plugdev() {
    if [[ ${TARGET_USER} == root ]]; then
        info "root kullanıcısı: plugdev grubu gerekmez."
        return
    fi
    if ! getent group plugdev >/dev/null 2>&1; then
        "${SUDO[@]}" groupadd --system plugdev
    fi
    local groups
    groups="$(id -nG "${TARGET_USER}")"
    if [[ " ${groups} " == *" plugdev "* ]]; then
        ok "'${TARGET_USER}' zaten plugdev grubunda."
    else
        "${SUDO[@]}" usermod -aG plugdev "${TARGET_USER}"
        warn "'${TARGET_USER}' plugdev grubuna eklendi — etkin olması için OTURUMU KAPATIP AÇIN (veya: newgrp plugdev)."
    fi
}

# ----------------------------------------------------------------------------- 4. venv + pip
venv_sees_system_site() {
    [[ -f ${VENV_DIR}/pyvenv.cfg ]] && grep -Eq '^include-system-site-packages *= *true' "${VENV_DIR}/pyvenv.cfg"
}

setup_venv() {
    if ((DO_VENV == 0)); then
        info "--no-venv: sanal ortam adımı atlandı."
        return
    fi
    [[ -x ${SYSTEM_PY} ]] || die "${SYSTEM_PY} yok (python3 paketi kurulu mu?)."
    if [[ -x ${VENV_DIR}/bin/python ]] && venv_sees_system_site; then
        ok "Sanal ortam mevcut: ${VENV_DIR}"
    else
        if [[ -d ${VENV_DIR} ]]; then
            warn "${VENV_DIR} sistem paketlerini görmüyor; --system-site-packages ile güncelleniyor (kurulu paketler korunur)."
        else
            info "Sanal ortam oluşturuluyor: ${VENV_DIR} (--system-site-packages)"
        fi
        # Sistem python3'ü ŞART: apt'nin python3-soapysdr / python3-libiio bağlamaları onun sürümü için derlenmiştir.
        as_user "${SYSTEM_PY}" -m venv --system-site-packages "${VENV_DIR}"
    fi
    venv_sees_system_site || die "${VENV_DIR}/pyvenv.cfg include-system-site-packages = true değil."
    info "pip güncelleniyor ve EDS düzenlenebilir modda kuruluyor: pip install -e \".[dev]\""
    as_user "${VENV_DIR}/bin/python" -m pip install -q --upgrade pip
    as_user "${VENV_DIR}/bin/python" -m pip install -q -e "${REPO_ROOT}[dev]"
    ok "EDS kuruldu (${VENV_DIR})."
}

# ----------------------------------------------------------------------------- 5. son kontroller
check() {
    # check "<açıklama>" komut...   → başarısızlık sayılır ama betik devam eder
    local what="$1"
    shift
    if "$@"; then
        ok "${what}"
    else
        warn "${what}: BAŞARISIZ"
        FAILED_CHECKS=$((FAILED_CHECKS + 1))
    fi
}

final_checks() {
    local py="${SYSTEM_PY}"
    if ((DO_VENV)); then
        py="${VENV_DIR}/bin/python"
    fi
    info "Son kontroller (${py})"
    check "Python: import SoapySDR, iio" \
        "${py}" -c 'import SoapySDR, iio; print("    SoapySDR API", SoapySDR.getAPIVersion(), "| libiio", getattr(iio, "version", "?"))'
    if ((DO_VENV)); then
        check "Python: import eds (+ numpy, zmq)" \
            "${py}" -c 'import eds, numpy, zmq; print("    eds", getattr(eds, "__version__", "?"), "| numpy", numpy.__version__, "| pyzmq", zmq.__version__)'
    fi
    check "SoapySDR plutosdr modülü (SoapySDRUtil --info)" soapy_has_plutosdr

    info "Takılı Pluto'lar — SoapySDRUtil --find=\"driver=plutosdr\":"
    local out
    out="$(timeout 30 SoapySDRUtil --find="driver=plutosdr" 2>&1 || true)"
    printf '%s\n' "${out}" | sed 's/^/    /'
    if grep -q "uri" <<<"${out}"; then
        ok "SoapySDR en az bir Pluto buldu."
    else
        warn "SoapySDR Pluto bulamadı: cihaz takılı değil, udev izni yok (plugdev + yeniden oturum) ya da USB kablosu yalnızca güç taşıyor."
    fi

    info "libiio bağlamları — iio_info -s:"
    out="$(timeout 30 iio_info -s 2>&1 || true)"
    printf '%s\n' "${out}" | sed 's/^/    /'
    if grep -q "PlutoSDR" <<<"${out}"; then
        ok "libiio Pluto'yu görüyor. Her satırdaki [usb:B.D.I] URI'si ve serial=... değeri config/*.toml için gereklidir."
    else
        warn "libiio Pluto görmüyor (USB). Ağ üzerinden deneyin: iio_info -u ip:192.168.2.1"
    fi
}

# ----------------------------------------------------------------------------- ana akış
main() {
    info "EDS kurulumu — depo: ${REPO_ROOT} | kullanıcı: ${TARGET_USER}"
    preflight
    install_apt_packages
    build_soapy_plutosdr
    install_udev_rule
    ensure_plugdev
    setup_venv
    final_checks
    echo
    if ((FAILED_CHECKS)); then
        warn "${FAILED_CHECKS} kontrol başarısız. Ayrıntılar yukarıda; docs/HARDWARE_PLUTO.md 'Sorun giderme'."
        exit 2
    fi
    ok "Kurulum tamam."
    if ((DO_VENV)); then
        info "Etkinleştirme: source ${VENV_DIR}/bin/activate"
    fi
    info "Donanımsız deneme : eds measure all --config config/rx_sim.toml --quick"
    info "Pluto ile         : eds measure max_rate --config config/rx_pluto.toml"
}

main "$@"
