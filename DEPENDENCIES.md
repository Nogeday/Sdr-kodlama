# Bağımlılıklar — neyi, neden, hangi modda kuruyoruz

Kurulumu zorlaştıran tek şey **SDR sürücüleridir** (libiio, SoapySDR, SoapyPlutoSDR). Bu yüzden
iki mod vardır:

| Mod | Komut | Ne kurulur | Ne zaman |
|---|---|---|---|
| **Simülasyon** | `./setup.sh --sim` | Yalnız pip paketleri (+ Qt'nin pencere açmak için istediği birkaç sistem kütüphanesi) | Takımın çoğu: geliştirme, GUI, test, KTR ölçüm prosedürlerinin denenmesi |
| **Donanım** | `./setup.sh --hardware` | Yukarıdakiler + SDR yığını (apt) + udev kuralı | Pluto'nun takılı olduğu bilgisayar (yarışma/tezgâh makinesi) |

Simülasyon modunda SoapySDR/libiio **hiç yoktur**: kod onları yalnızca donanım açılırken içe aktarır
(lazy import). GUI de bunlara ihtiyaç duymaz, çünkü yalnızca ZeroMQ bus'ını dinler.

## Python paketleri (pip — her iki mod)

| Paket | Neden | Grup |
|---|---|---|
| `numpy` | Bütün sinyal işleme (Welch PSD, CFAR, simülatör) | çekirdek |
| `pyzmq` | ZeroMQ bus: RX düğümü ↔ GUI ↔ diğer modüller | çekirdek |
| `PySide6` | Operatör GUI'sinin Qt 6 arayüzü (pencere, tablo, panel) | `gui` |
| `pyqtgraph` | Gerçek zamanlı spektrum ve waterfall çizimi (matplotlib bu iş için çok yavaş) | `gui` |
| `matplotlib` | KTR raporundaki statik grafikler / spektrum "ekran görüntüleri" (PNG) | `report`, `dev` |
| `jsonschema` | Bus mesajlarının JSON şemasına göre doğrulanması (testler, `bus.validate = true`) | `dev`, `validate` |
| `pytest` | Test paketi (~1700 test, simülatörde) | `dev` |
| `ruff` | Kod stili denetimi (`make lint`) | `dev` |

`./setup.sh` bunları `pip install -e ".[gui,dev]"` ile kurar. Elle: `pip install -e ".[gui]"` (yalnız
çalıştırmak), `pip install -e ".[gui,dev]"` (geliştirmek). GUI'siz bir makine (ör. yalnız hattı
çalıştıran Jetson) için `pip install -e .` yeterlidir.

## Sistem paketleri — simülasyon modu (apt, Linux)

| Paket | Neden |
|---|---|
| `python3-venv`, `python3-pip` | Sanal ortam (`.venv`) oluşturmak |
| `libegl1`, `libgl1`, `libxkbcommon0`, `libfontconfig1`, `libdbus-1-3`, `libxcb-cursor0` | PySide6'nın (Qt 6) Linux'ta pencere açmak için istediği çalışma zamanı kütüphaneleri. Masaüstü Ubuntu'da çoğu zaten vardır; `libxcb-cursor0` sık eksik olur ("Could not load the Qt platform plugin xcb" hatası). |

## Sistem paketleri — donanım modu (apt + kaynak, `scripts/install_ubuntu.sh`)

| Paket | Neden |
|---|---|
| `libiio-dev`, `libiio-utils`, `python3-libiio` | Pluto ile konuşan kütüphane. EDS'nin ince katmanı (`eds.hal.pluto_iio`) bununla `xo_correction` (PPM), DC/IQ tracking, sıcaklık ve RX DMA taşma kaydını okur. `iio_info` gibi araçlar da buradan gelir. |
| `libad9361-dev` | SoapyPlutoSDR'nin AD9361 örnekleme hızı / FIR ayarları için kullandığı yardımcı kütüphane |
| `soapysdr-tools`, `libsoapysdr-dev`, `python3-soapysdr` | Ortak SDR arayüzü (SoapySDR) + `SoapySDRUtil` + Python bağlamaları. Akış ve cihaz soyutlaması bunun üzerinden; Kraken/HackRF/bladeRF geldiğinde aynı arayüz. |
| **SoapyPlutoSDR** | SoapySDR'nin Pluto sürücüsü (`driver=plutosdr`). Ubuntu deposunda yoksa betik `github.com/pothosware/SoapyPlutoSDR`'den derler. |
| `libusb-1.0-0-dev`, `cmake`, `g++`, `make`, `git`, `pkg-config` | SoapyPlutoSDR'yi kaynaktan derlemek için |
| `libzmq3-dev` | ZeroMQ C kütüphanesi (pyzmq çoğu zaman kendi kopyasını getirir; C/C++ modülleri için) |
| udev kuralı `scripts/53-adi-plutosdr-usb.rules` + `plugdev` grubu | Pluto'ya root olmadan USB erişimi; ModemManager'ın Pluto'yu kurcalamasını engeller |

Donanım modunda sanal ortam `--system-site-packages` ile oluşturulur, çünkü SoapySDR ve libiio'nun
Python bağlamaları pip'ten değil apt'ten gelir.

## Bilinçli olarak KURULMAYANLAR

| Paket | Neden kurulmuyor |
|---|---|
| `pyadi-iio` | Mimari karar: akış ve cihaz soyutlaması **SoapySDR** üzerinden, Pluto'ya özel kontroller ise libiio'nun kendi Python bağlamasıyla (`import iio`) ince bir katmanda. pyadi-iio aynı işi ikinci bir yoldan yapardı; kod onu hiç kullanmıyor. |
| `scipy` | Çalışma zamanında gerekmiyor (bütün DSP numpy ile). Testler varsa çapraz kontrol için kullanır, yoksa o testleri atlar. |

## Alternatif: conda (Linux ve Windows)

`environment.yml` aynı SDR yığınını derleme gerektirmeden kurar (SoapySDR 0.8.1 + SoapyPlutoSDR 0.2.4
+ libiio/pylibiio 0.26, sürümler bilerek sabit). GUI için ek olarak: `pip install -e ".[gui]"`.
Ayrıntı: [`docs/KURULUM.md`](docs/KURULUM.md).
