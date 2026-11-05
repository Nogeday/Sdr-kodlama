# EDS — Elektronik Destek Sistemi · RX omurgası

KTÜ TEKNOFEST **Elektronik Harp** takımının Elektronik Destek (ED) sisteminin alıcı (RX)
omurgası. Faz-0 donanımı **2× ADALM-Pluto (AD9363)**; nihai mimaride DF için **KrakenSDR**
(5 kanal koherent) ve TX için **HackRF** gelecek. Bu kod atılacak "Pluto kodu" değildir:
donanım soyutlaması, mesajlaşma ve klasör yapısı yeni donanım geldiğinde yeniden yazım
gerektirmeyecek şekilde kurulmuştur.

Sistem aynı zamanda **kendi ölçüm cihazıdır**: KTR'nin istediği 9 nicel spesifikasyonu
(duyarlılık, dinamik alan, anlık BW, tarama hızı, frekans çözünürlüğü, DF RMS, imaj
bastırma, taşma-sız max örnekleme hızı, uçtan-uca gecikme) kendisi ölçer ve **tablo + grafik
+ spektrum analizörü tarzı ekran görüntüsü** olarak raporlar (`eds measure all`).

```
$ eds -c config/rx_sim.toml measure all --quick
#  Parametre                              Ölçülen  Birim     Hedef  Durum
1  Duyarlılık (MDS)                  -131.8 ± 0.9  dBm    ≤ -100.0  KARŞILAŞTIRILMADI
2  Dinamik alan                       100.5 ± 0.5  dB       ≥ 60.0  KARŞILAŞTIRILMADI
3  Anlık bant genişliği                      16.9  MHz      ≥ 18.0  KARŞILAŞTIRILMADI
4  Tarama hızı                              0.512  GHz/s    ≥ 5.00  KARŞILAŞTIRILMADI
5  Frekans çözünürlüğü                  1352 ± 38  Hz      ≤ 10000  KARŞILAŞTIRILMADI
6  DF RMS hatası                                —  °        ≤ 5.00  ATLANDI
7  İmaj bastırma                             55.1  dB       ≥ 40.0  KARŞILAŞTIRILMADI
8  Taşmasız maksimum örnekleme hızı          6.00  MS/s     ≥ 4.00  KARŞILAŞTIRILMADI
9  Uçtan uca gecikme                         39.6  ms        ≤ 100  KARŞILAŞTIRILMADI

Toplam: 9  |  GEÇTİ: 0  KALDI: 0  KARŞILAŞTIRILMADI: 8  HEDEF YOK: 0  ATLANDI: 1  HATA: 0
Rapor: reports/20261105-201723   (report.html, summary.md/.csv, results.json, tables/, figures/*.png)
```
> Yukarıdaki değerler **simülatörden** ve **hızlı moddan** gelir: rapor ikisini de bant ve şekil
> etiketiyle belirtir, hızlı mod sonuçları hedefle **karşılaştırılmaz** (KARŞILAŞTIRILMADI);
> `df_rms` tek cihazlı `rx_sim.toml`'da 2 kanal olmadığı için atlanır (`config/dual_sim.toml`
> ile koşar). Hedef kararı için `--quick` olmadan ve donanımda ölçün. Hedefler
> `config/ktr_targets.toml` içindeki **örnek** değerlerdir — takımın KTR değerleriyle güncelleyin.

---

## 1. Hızlı başlangıç (Ubuntu 22.04 / 24.04)

```bash
git clone https://github.com/Nogeday/Sdr-kodlama.git && cd Sdr-kodlama
./scripts/install_ubuntu.sh          # apt: libiio, SoapySDR, python3-soapysdr/libiio; SoapyPlutoSDR derlemesi;
                                     # udev kuralı; .venv (--system-site-packages) + pip install -e ".[dev]"
source .venv/bin/activate

make test                            # ~1600 test, donanım gerekmez (fizik tabanlı AD9363 simülatörü)
make demo                            # simülatörde 9 KTR ölçümü (hızlı) → reports/<zaman>/report.html
```

Donanımla:
```bash
SoapySDRUtil --find="driver=plutosdr"          # Pluto(lar)ı gör (uri, label; seri no GÖSTERMEZ)
iio_info -s                                    # libiio bağlamları: URI ↔ seri numarası eşlemesi
eds devices --backend soapy --probe            # aç, çalışma zamanı sınırları ve sensörleri yazdır
eds info -c config/rx_pluto.toml               # konfigürasyonu profile karşı doğrula
eds cal ppm -c config/rx_pluto.toml --gsm-arfcn 62     # TCXO ppm (GSM FCCH) → cal/<seri>.json
eds measure max_rate -c config/rx_pluto.toml   # USB 2.0 sürdürülebilir örnekleme hızını ÖLÇ
eds rx -c config/rx_pluto.toml                 # RX düğümü: ZMQ'ya spektrum/tespit/durum yayınla
```
Ayrıntılar: [`docs/HARDWARE_PLUTO.md`](docs/HARDWARE_PLUTO.md) (tezgâh bağlantıları, iki Pluto,
süreç başına tek Pluto, seri no ile seçim, harici referans, xo_correction, overflow algılama,
Pluto TX artefaktları, güvenlik).

## 2. Donanım kısıtları → kodun cevabı

| Kısıt (AD9363 / Pluto) | Kod ne yapıyor |
|---|---|
| 325 MHz – 3.8 GHz, ≤ 20 MHz BW | `config/profiles/ad9363_pluto.toml`; her ayar donanıma gitmeden **profile karşı doğrulanır** (`ProfileViolation`, tüm ihlaller Türkçe listelenir). AD9364 "unlock" yalnızca bilinçli seçilen ayrı profil. |
| 1R1T, 2 cihaz = 2 kanal | `DeviceGroup` iki Pluto'yu tek çok kanallı cihaz gibi okur; `coherent=False` (ortak referans + kalibrasyon olmadan). SoapyPlutoSDR süreç başına tek libiio bağlamı tuttuğundan her fazla Pluto (DF grubu, `pluto_tx` uyaranı) kendi alt sürecinde çalışır (`[[device]] isolation`). |
| 12-bit ADC | Kırpma tespiti (`iq_levels`), yazılım AGC (`soft_agc`), taramada otomatik kazanç, banda bağlı kazanç tablosu (≤1.3 GHz: −1…73 dB, üstü −3…71 dB). |
| 40 MHz TCXO ±25 ppm | CW veya **GSM FCCH** ile ppm kestirimi; donanımda `xo_correction` (LO **ve** fs birlikte düzelir), olmazsa yazılım düzeltmesi; sıcaklık farkı uyarısı. |
| USB 2.0 | Sürdürülebilir hız **ölçülür** (`max_rate`, DSP hattı açıkken); tarama 20 MS/s **burst** kullanır (USB sınırı adımlar arasında geçerli değil); overflow'da blok içi boşluk asla oluşmaz. SoapyPlutoSDR overflow bildirmediği için örnek kaybı ince katmandan RX DMA durum yazmacıyla algılanır. |
| Sıfır-IF | DC/RF-DC/quadrature tracking; DC çevresi geçersiz bin; taramada `dc_avoid` (%50 örtüşme); imaj bastırma ölçülür. |
| Retune sonrası bayat tampon | `settle_buffers` ile işaretlenir ve atılır (`RxDevice.capture/flush`). |

## 3. Mimari

```
                 eds (CLI)  ·  eds.rx.node (servis)  ·  eds.measure (KTR ölçümleri + rapor)
                                     │
   eds.calib (ppm, FCCH, güç)   eds.df (koherens, interferometre)   eds.rx (stream, AGC, sweep)
                                     │
                         eds.dsp (Welch PSD, çok çözünürlüklü CFAR, tepe)
                                     │
                      eds.hal.RxDevice  ──  eds.bus (ZeroMQ pub/sub + JSON şema)
        ┌─────────────────┬──────────┴─────────┬──────────────┬──────────────────┐
  soapy_backend      pluto_iio (ince)      group (N×1ch)    proc (süreç       sim (fizik tabanlı AD9363)
  (SoapyPlutoSDR)    xo_correction,         2× Pluto         yalıtımı: Pluto   CI · demo · ölçüm
                     tracking, sıcaklık,                     başına süreç)     prosedürü doğrulama
                     DMA overflow yazmacı
```
* **Akış ve cihaz soyutlaması SoapySDR**'dir. Pluto'ya özel kontroller önce Soapy API'si
  ile denenir, yoksa ince libiio katmanına düşülür; pyadi-iio birincil arayüz değildir.
* **Yeni donanım = yeni profil TOML (+ gerekirse yeni backend dosyası).** KrakenSDR,
  HackRF, bladeRF 2.0, USRP B210 profilleri `status = "planned"` olarak hazır; üst katmanlar
  yalnızca `RxDevice` (`num_channels`, `coherent`) görür.
* Konvansiyonlar (PSD ölçekleme, dBFS tanımı, ppm işareti, birimler) ve modül arayüzleri:
  [`docs/ARCHITECTURE.md`](docs/ARCHITECTURE.md).

## 4. ZeroMQ topic'leri

Tek doğruluk kaynağı: [`contracts/topics.json`](contracts/topics.json) + `contracts/schemas/*.schema.json`
(JSON Schema 2020-12). Çerçeve: multipart `[topic, json]`; her mesaj zarf taşır:
`{topic, v, src, seq, t_ns, data}`.

| Topic | Üretici | İçerik |
|---|---|---|
| `rx.status` | rx | frekans/hız/kazanç, overflow/drop sayaçları, sıcaklık, ppm, headroom |
| `rx.spectrum` | rx | PSD karesi (dBm/dBFS), RBW, zaman damgaları (`t_acq_ns`, `t_acq_end_ns`, `t_proc_ns`) |
| `rx.detections` | rx | CFAR tespitleri (merkez/tepe frekansı, BW, güç, SNR) |
| `rx.sweep` | rx | geniş bant tarama (max-hold desimasyonlu) + tespitler + tarama hızı |
| `cal.result` | rx / calib | ppm, güç, imaj, koherens kalibrasyon sonuçları |
| `df.bearing` | df | kerteriz (Faz-0: 2 elemanlı interferometre) |
| `meas.result` | measure | KTR ölçüm sonucu (değer, birim, koşullar, hedef, geçti/kaldı) |
| `ctl.rx` / `ctl.ack` | gui → rx / rx | komut (frekans, kazanç, hız, BW, tarama, durdur) ve yanıtı |

Topic adları değişecekse yalnızca `contracts/topics.json` ve `src/eds/bus/topics.py`
değişir; `tests/bus/test_contracts.py` ikisinin birebir eşleştiğini doğrular.
Araçlar: `eds bus proxy` (XSUB/XPUB), `eds bus echo rx.detections`,
`eds bus send ctl.rx --cmd set_center_freq --arg hz=2.4e9 --wait-ack 2`.

## 5. KTR ölçümleri (`eds measure ...`)

| spec_id | Ölçülen | Yöntem (özet) |
|---|---|---|
| `sensitivity` | dBm | Pd–giriş gücü eğrisi (Pd = 0.9 @ CFAR Pfa, RBW) + DANL/NF/MDS(B) |
| `dynamic_range` | dB | anlık DR (min(kırpma, 1 dB sıkışma) − RBW gürültüsü), SFDR (imaj dâhil, kaynak artefaktları hariç), kazanç yönetimli toplam DR |
| `inst_bw` | MHz | ton adımlama ile −3/−1 dB geçirme bandı (kaynak yoksa gürültü şekli) |
| `scan_speed` | GHz/s | gerçek tarayıcı ile süpürülen aralık / süre; tune/settle/yakalama/işleme dökümü |
| `freq_resolution` | Hz | RBW = ENBW·fs/N ve ölçülen en küçük ayrılabilir iki-ton aralığı; frekans doğruluğu (ham / kayıtlı kalibrasyon ppm'i ile; referansa kilitli kaynakla) |
| `df_rms` | ° | koherens kontrolü + kablolu faz kalibrasyonu + açı taraması, wrap'li RMS |
| `image_rejection` | dB | P(+Δ) − P(−Δ), quadrature tracking açık/kapalı |
| `max_rate` | MS/s | sıfır overflow + sıfır örnek kaybı ile en yüksek sürekli hız (başlık: DSP hattı açık, gerçek RxNode; ayrıca yalnız taşıma) |
| `latency` | ms | RF olayı → ZMQ teslimi p50/p95 (p99 N ≥ 100) + boru hattı dökümü; kaynaksız yalnız host tarafı (Mod A) |

Tanımlar, tezgâh kurulumu ve KTR'de nasıl yazılacağı: [`docs/MEASUREMENTS.md`](docs/MEASUREMENTS.md).
Rapor klasörü: `report.html` (tek dosya, A4 yazdırılabilir), `summary.md`, `summary.csv`,
`results.json`, `tables/*.csv`, `figures/*.png`. Simülatör verisi, hızlı mod ve spesifikasyon
dışı profil raporda banner (ve şekil etiketi) ile işaretlenir. Koşulsuz sayıya karar verilmez:
hızlı mod, gerçek zamanlı olmayan simülatör, koherent olmayan kanallar, ölçümün kendi karar
engelleri (`blockers`) ve birim/kalibrasyon uyuşmazlığı (ör. dBFS değer, dBm hedef) durumunda
sonuç **KARŞILAŞTIRILMADI** olur (nedeniyle birlikte).

Sinyal kaynakları (`measure.source`): `sim`, `pluto_tx` (2. Pluto TX, kendi alt sürecinde →
≥30 dB zayıflatıcı → RX), `scpi` (laboratuvar üreteci, TCP 5025), `manual` (operatör), `none`.
Ölçümler RX girişini profilin `rx_max_input_dbm` (AD9363: −5 dBm) tavanının üstüne sürmez
(`latency`'nin uyarlamalı tonu hariç); TX→RX yolunda ≥ 30 dB sabit zayıflatıcı yine de zorunludur.

## 6. Klasör yapısı

```
contracts/          topic kayıt defteri + JSON şemaları (dil bağımsız sözleşme)
config/             çalıştırılabilir konfigürasyonlar (rx_sim, rx_pluto, dual_pluto, dual_sim),
  profiles/         cihaz profilleri (AD9363 resmî sınırlar; planlanan Kraken/HackRF/...)
  ktr_targets.toml  KTR hedefleri (rapor "Hedef/Durum" sütunları)
src/eds/
  core/             tipler (IQBlock), konfigürasyon, profiller, hatalar, birimler
  hal/              RxDevice; soapy_backend, pluto_iio, group, proc (süreç yalıtımı), tx/tx_hw, sim/
  dsp/              pencereler, Welch PSD, tepe, istatistik (gamma), CFAR, seviyeler
  rx/               stream (okuyucu iş parçacığı), agc, sweep, node (servis)
  calib/            ppm (CW, GSM FCCH), güç tablosu, kalibrasyon deposu, uygulama
  df/               koherens, 2 elemanlı interferometre, metrikler
  bus/              topics, zarf, pub/sub/proxy, şema doğrulama
  measure/          çerçeve, oturum, uyaran yardımcıları, specs/ (9 ölçüm), rapor, grafikler
  cli/              `eds` komutu
cal/                cihaz başına kalibrasyon dosyaları (cal/<seri>.json)
reports/            ölçüm raporları
scripts/            install_ubuntu.sh, udev kuralı
docs/               ARCHITECTURE.md, HARDWARE_PLUTO.md, MEASUREMENTS.md
tests/              pytest (simülatör + gerçek API'leri taklit eden SoapySDR/libiio sahteleri)
```

## 7. Geliştirme

```bash
make test         # tüm testler        make test-fast   # realtime testler hariç
make lint         # ruff               eds -v ...       # ayrıntılı günlük + traceback
```
Kurallar: birimler Hz/s/dB; örnekler `complex64`, tam ölçek = 1.0; yalnızca
`eds.core.errors` istisnaları; kullanıcıya dönük metinler Türkçe, kod İngilizce.

## 8. Yol haritası

* **Faz-1 DF:** KrakenSDR (Heimdall DAQ) backend'i → `RxDevice(num_channels=5, coherent=True)`;
  `eds.df`'e MUSIC/Bartlett; `df_rms` ölçümü değişmeden çalışır.
* **TX:** HackRF profili hazır; `SoapyTxSource` HackRF ile de çalışır (aynı Soapy API).
* **Tarama hızı:** AD9361 fastlock profilleri ile retune süresinin kısaltılması.
* Donanımda henüz koşulmamış yollar (SoapySDR/libiio) gerçek API'leri taklit eden sahtelerle
  test edilmiştir; ilk donanım oturumunda `eds devices --probe` ve `eds measure max_rate`
  ile doğrulanmalıdır.
