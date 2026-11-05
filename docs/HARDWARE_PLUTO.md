# ADALM-Pluto (AD9363) — Donanım Gerçeği ve Kodun Karşılığı (Faz-0)

> Bu belge, elimizdeki **2 adet AD9363 tabanlı ADALM-Pluto** ile çalışırken uyulması gereken
> fiziksel kısıtları ve EDS kodunun **her bir kısıt için tam olarak ne yaptığını** anlatır.
> Kod sözleşmesi: [`ARCHITECTURE.md`](ARCHITECTURE.md). Ölçüm prosedürleri:
> [`MEASUREMENTS.md`](MEASUREMENTS.md). Kurulum: `scripts/install_ubuntu.sh`.

**İçindekiler**

1. [Fiziksel kısıtlar → kodun yaptığı](#1-fiziksel-kısıtlar--kodun-yaptığı)
2. [Yazılım yığını ve doğrulama komutları](#2-yazılım-yığını-ve-doğrulama-komutları)
3. [İki Pluto'yu aynı anda bağlamak (IP, URI, seri no)](#3-iki-plutoyu-aynı-anda-bağlamak-ip-uri-seri-no)
4. [Firmware sürümü](#4-firmware-sürümü)
5. [İki Pluto ile DF: gerçekler](#5-iki-pluto-ile-df-gerçekler)
6. [Spesifikasyon dışı modlar (AD9364 unlock, 2R2T)](#6-spesifikasyon-dışı-modlar-ad9364-unlock-2r2t)
7. [Tezgah bağlantı şemaları](#7-tezgah-bağlantı-şemaları)
8. [RF güvenliği](#8-rf-güvenliği)
9. [KrakenSDR / HackRF / bladeRF / USRP nasıl takılır](#9-krakensdr--hackrf--bladerf--usrp-nasıl-takılır)
10. [Sorun giderme](#10-sorun-giderme)

---

## 1. Fiziksel kısıtlar → kodun yaptığı

Resmî sınırlar `config/profiles/ad9363_pluto.toml` profilinde tutulur. **Her** ayar
(`RxDevice.configure`, `set_center_freq`, `set_sample_rate`, `set_bandwidth`) donanıma
gitmeden önce bu profile karşı doğrulanır (`DeviceProfile.check_rx`). Sürücünün bildirdiği
çalışma zamanı sınırları (SoapyPlutoSDR AD9364 aralığını raporlar!) yalnızca **kesişim**
için kullanılır; yetkili kaynak profildir.

| Parametre | Resmî (AD9363 / Pluto) | Pratik sonuç | EDS ne yapıyor |
|---|---|---|---|
| Frekans | 325 MHz – 3.8 GHz | Dışı = spesifikasyon dışı | Profil doğrulaması; ihlalde `ProfileViolation` (Türkçe, **tüm** ihlaller tek seferde). Tarama tam bu aralığı kapsar (`[sweep] start_hz/stop_hz`). |
| Analog kanal BW | 200 kHz – 20 MHz | Anlık BW ≤ 20 MHz | BW doğrulaması; `BW > fs` ise aliasing uyarısı. Anlık BW `eds measure inst_bw` ile **ölçülür**. |
| Örnekleme hızı (çip) | ≈65.1 kS/s – 61.44 MS/s | Sürdürülebilir sınır USB'dir | Profil aralığı + USB yük uyarıları (aşağıda). |
| Kanal | Pluto kartı 1R1T | 1 cihaz = 1 RX | 2 RX kanalı = **2 Pluto** + `DeviceGroup` (`num_channels = 2`, `coherent = False`). Cihaz başına `channels = [0]`. |
| ADC | 12-bit, tam ölçek ±2048 | Dinamik alan sınırlı | `I = counts/2048`, 0 dBFS tanımı, kırpma algılama (`clip_level = 2047/2048`), yazılım AGC (`eds.rx.agc`), ölçümlerde HW AGC kapalı. |
| Kazanç | Banda bağlı: <1.3 GHz −1…73 dB, 1.3–4 GHz −3…71 dB | Retune sonrası aralık değişir | `gain_range(f)` banda göre; retune'da kazanç yeni aralığa **otomatik kırpılır**; adım 1 dB'ye kuantalanır. |
| Saat | 40 MHz TCXO, ≈ ±25 ppm | 3.8 GHz'te ±95 kHz hata | PPM kalibrasyonu (`eds.calib.ppm`, GSM FCCH), donanım düzeltmesi `xo_correction`, yazılım yedeği, sıcaklık uyarısı. |
| Arayüz | USB 2.0 (480 Mbit/s) | Akışın asıl darboğazı | Profil uyarıları (pratik 24 MB/s, teorik 53.2 MB/s); `eds measure max_rate` ile **ölçülen** sürdürülebilir hız; tarama burst ile. |
| Mimari | Sıfır-IF (direct conversion) | DC çivisi + IQ imajı | AD936x DC/quadrature tracking açık; DC çevresi geçersiz bin (`dc_notch_bins`), taramada `dc_avoid`; imaj bastırma **ölçülür**. |
| Retune | libiio 4 çekirdek tamponu | Retune sonrası **bayat örnek** | `settle_buffers = kernel_buffers + 1` tampon “settling” işaretlenir; `capture()`/`flush()` atar. |

### 1.1 Frekans ve BW → profil doğrulaması

* `DeviceProfile.check_rx(...)` hataları ve uyarıları ayrı listeler; `validate_rx` hata varsa
  `ProfileViolation(violations=[...])` fırlatır. Operatör **bütün** sorunları tek mesajda görür:

  ```text
  Cihaz profili ihlali:
    - Merkez frekans 6 GHz AD9363 aralığı dışında [325 MHz – 3.8 GHz]
    - Analog kanal BW 30 MHz izinli aralık dışında [200 kHz – 20 MHz]
    - Kazanç 80 dB, 6 GHz için [-10 – 62 dB] aralığı dışında
  ```
* 325 MHz altı / 3.8 GHz üstü yalnızca bilinçli olarak seçilen `ad9364_pluto_ext` profiliyle
  mümkündür (bkz. §6). Jüri raporları **yalnızca** `ad9363_pluto` ile üretilir.

### 1.2 1R1T → iki cihaz + `DeviceGroup`

* Her Pluto ayrı bir `[[device]]` (`config/dual_pluto.toml`). `eds.hal.factory.open_group()`
  ikisini `DeviceGroup` olarak açar: `read(n)` her üyeden **eşzamanlı (thread)** okur,
  `(2, n)` blok döndürür.
* Grup `coherent = False` bildirir: iki ayrı Pluto'nun LO'su ve örnekleme saati bağımsızdır
  (bkz. §5). Herhangi bir üyede overflow → grup bloğunda `overflow = True` (hizalama
  kayboldu, DF yeniden kalibre etmeli).
* İki Pluto **aynı USB 2.0 host denetleyicisini** paylaşıyorsa toplam veri hızı önemlidir;
  `DeviceGroup.configure` bunun için uyarı üretir (2 × 4 MS/s = 32 MB/s).

### 1.3 12-bit ADC → kazanç yönetimi, kırpma algılama

* **Normalizasyon:** `I = counts/2048`; **0 dBFS** = genliği tam ölçek olan kompleks
  üstelin gücü (`mean(|x|²) = 1`). İdeal 12-bit niceleme gürültüsü (I+Q) ≈ **−74 dBFS**
  (tüm `fs` bandında). RBW'deki taban buna `−10·log10(fs/RBW)` eklenerek bulunur.
* **Kırpma:** `max(|I|,|Q|) ≥ clip_level ≈ 2047/2048`. Sert kırpma spektruma yayılan
  harmonikler/IMD üretir; bu yüzden:
  * `eds.dsp.levels.iq_levels` → `peak_dbfs, rms_dbfs, clip_fraction, dc_dbfs`,
  * yazılım AGC (`gain_mode = "soft_agc"`, `[agc]`): hedef tepe **−10 dBFS**, üst eşik
    −3 dBFS (hemen `attack_step_db = 10` dB düşür), alt eşik −35 dBFS (yavaşça
    `decay_step_db = 3` dB artır, `hold_blocks` bekle), `max_clip_fraction = 1e-5`.
    Kazanç değişimi sonrası bloklar “settling” işaretlenir ve AGC onları yok sayar.
  * Ölçümler kırpılmış blokları kullanmaz; dinamik alan ölçümü kırpma eşiğini bu ölçütle bulur.
* **Donanım AGC'si** (`hw_slow | hw_fast | hw_hybrid`) örnekle senkron kazanç bilgisi
  vermez → güç ölçümünde **kapalı** (`gain_mode = "manual"`). `hw_fast`/`hw_hybrid` ince
  libiio katmanı gerektirir; SoapySDR yalnızca `slow_attack` seçebilir.

### 1.4 40 MHz TCXO ±25 ppm → PPM kalibrasyonu

Etkisi: ±25 ppm, 935 MHz'te ±23.4 kHz, 2.4 GHz'te ±60 kHz, 3.8 GHz'te **±95 kHz**
frekans hatası (frekans ölçümü/DF frekans eşlemesi için kabul edilemez).

**İşaret konvansiyonu (ARCHITECTURE §2):**

```text
ppm = (f_ref_gerçek − f_ref_nominal) / f_ref_nominal · 1e6      (pozitif = TCXO hızlı)
e   = ppm · 1e-6
Görünen RF frekansı   f_app  = f_true / (1 + e)     (LO ve fs aynı referanstan türer)
Düzeltme              f_true = f_app · (1 + e)
Tam f'ye ayarlamak    fc_cmd = f / (1 + e)
Donanım düzeltmesi    xo_correction = ref_clock_hz · (1 + e)
```

Örnek: `ppm = +11.7` → 1 GHz'lik gerçek ton 999.988 300 MHz'te görünür (−11.7 kHz);
`xo_correction = 40 000 000 · (1 + 11.7e-6) = 40 000 468 Hz`.

**Kod ne yapıyor:**

1. **Kestirim** (`eds.calib`): bilinen bir CW referansı (`estimate_ppm_cw`; üreteç
   GPSDO/10 MHz'e kilitli olmalı) ya da bölgedeki bir **GSM BCCH**'nin FCCH tonu
   (`estimate_ppm_gsm_fcch`; ton `fc + 1625/24 kHz = +67.708 kHz`; baz istasyonu frekansı
   ±0.05 ppm doğrulukta). Sonuç her zaman **nominal referansa göre TOPLAM ppm**'dir:
   kestirim donanım düzeltmesi açıkken de yapılabilir; ölçülen artık ile uygulanan
   düzeltme birleştirilip `(1+e_toplam) = (1+e_hw)(1+e_artık)` saklanır (aynı değeri
   tekrar uygulamak artığı ~0 yapar — işlem idempotenttir).
   > 2. Pluto'nun TX'i PPM referansı **değildir** — onun TCXO'su da ±25 ppm'dir.
2. **Saklama:** `cal/<seri_no>.json` (`CalibrationStore`), sıcaklıkla birlikte.
3. **Uygulama** (`[cal] ppm_mode = "auto"`): sırayla
   * `hardware` — ince libiio katmanı ile `ad9361-phy` `xo_correction` yazılır (LO **ve**
     örnekleme saati birlikte düzelir; komut satırı karşılığı:
     `iio_attr -u ip:192.168.2.1 -d ad9361-phy xo_correction 40000468`),
   * `soapy` — `setFrequencyCorrection` (sürücü destekliyorsa),
   * `software` — `apply_calibration` ile frekans ekseni `f_true = f_app·(1+e)`.
   `IQBlock.meta["ppm_hw"]` donanımın zaten düzeltip düzeltmediğini taşır (çift düzeltme yok).
4. **Kalıcı yapmak (isteğe bağlı, operatör kararı):** EDS firmware ortamını **asla**
   değiştirmez. Kalıcı düzeltme için Pluto kabuğunda:

   ```bash
   ssh root@192.168.2.1            # varsayılan parola: analog
   fw_setenv xo_correction 40000468
   fw_printenv xo_correction       # doğrula
   reboot
   ```

   (Yeni firmware'lerde aynı alan yığın-bellek sürücüsündeki `config.txt` → `[SYSTEM]
   xo_correction` ile de ayarlanabilir; kendi sürümünüzde doğrulayın.) Açılış değeri
   nominal değilse ince katman bunu **uyarı** olarak raporlar ve ppm kestirimi yine nominale
   göre yapılır.
5. **Sıcaklık:** TCXO frekansı ısındıkça kayar. Ölçümden önce **10–15 dk ısınma**; AD9361
   sıcaklık sensörü (`read_sensors()["temperature_c"]`) her sonuca yazılır;
   kalibrasyon sıcaklığından `temp_warn_delta_c` (5 °C) fazla sapma uyarı üretir.

### 1.5 USB 2.0 → sürdürülebilir hız ölçümü, burst tarama

USB 2.0 High-Speed: 480 Mbit/s ham; bulk teorik üst sınır **53.248 MB/s**
(13 × 512 B / 125 µs). Pluto CS16 akışı örnek başına **4 bayt** (16-bit I + 16-bit Q):

| fs (MS/s) | 1 | 2.4 | 4 | 6 | 8 | 10 | 13.3 | 20 | 61.44 |
|---|---|---|---|---|---|---|---|---|---|
| MB/s | 4 | 9.6 | **16** | 24 | 32 | 40 | 53.2 | 80 | 245.8 |
| Profil uyarısı | – | – | – | – (eşik) | pratik | pratik | pratik | **teorik** | **teorik** |

* Profil (`interface_practical_bytes_per_s = 24e6`) üstünde **“overflow beklenir”**, teorik
  sınır üstünde **“overflow kesin”** uyarısı verilir (`eds measure max_rate` önerisiyle).
* Gerçek sınır makineye, USB denetleyicisine, hub'a, kabloya, libiio tampon boyuna bağlıdır
  → **ölçülür**: `eds measure max_rate` (sıfır overflow + sıfır örnek kaybı, T saniye; yalnızca
  taşıma ve DSP hattı açık iki mod). Varsayılan sürekli akış **4 MS/s** (16 MB/s, pay bırakır).
* **Tarama** 20 MS/s **burst** kullanır: her adımda retune → settle tamponları atılır → kısa
  bitişik yakalama. Adımlar arası overflow sonucu etkilemez (her adım kendi içinde bitişik).
  Bu yüzden `[sweep] sample_rate_hz = 20e6` için çıkan “teorik sınır” uyarısı beklenen
  davranıştır.
* İpuçları: her Pluto'yu **ayrı kök porta** (farklı host denetleyicisi) takın; pasif hub
  kullanmayın; zayıf portlarda Pluto'nun ikinci (PWR) micro-USB girişinden ek besleme verin;
  dizüstünde CPU frekans ölçeklemeyi `performance` yapın. SoapyPlutoSDR `usb_direct=1`
  (libusb doğrudan akış) ve daha büyük `bufflen` (`"stream:bufflen" = "131072"`) ile
  denenebilir — her değişikliği `max_rate` ile ölçün.

### 1.6 Sıfır-IF: DC ve IQ dengesizliği

* AD936x **BB DC**, **RF DC** ve **quadrature** tracking varsayılan açık (`[rx]
  dc_tracking / rf_dc_tracking / quad_tracking`). SoapySDR `setDCOffsetMode` /
  `setIQBalanceMode` ile, RF-DC tracking ince katmanla uygulanır.
* DC çevresindeki `dc_notch_bins` bin tespitte geçersizdir; tarama adımları örtüşür ve DC
  hiçbir zaman birleştirilmiş sonuca girmez (`dc_avoid = true`). Ölçüm tonları DC'den uzak
  ofsetlere konur.
* IQ dengesizliği `fc − δ`'da imaj üretir: `IRR = 10·log10((1 + 2g·cosφ + g²)/(1 − 2g·cosφ + g²))`,
  `g = 10^(ε/20)`. Tracking açık/kapalı imaj bastırma `eds measure image_rejection` ile ölçülür.

### 1.7 Retune sonrası bayat tamponlar

libiio, `kernel_buffers` (vars. 4) tamponu kuyrukta tutar: retune / hız / kazanç değişiminden
sonraki `4 × bufflen` örnek **eski ayarlarla** alınmıştır. HAL bunun +1 tampon fazlasını
(`settle_buffers = 5`) “settling” olarak işaretler; `capture()` ve `flush()` bu örnekleri
atar. 4 MS/s ve 65536'lık tamponda bu ≈ 82 ms'dir — tarama hızını doğrudan belirler
(`eds measure scan_speed` adım dökümünde “settle” kalemi).

---

## 2. Yazılım yığını ve doğrulama komutları

```text
eds (Python) ── eds.hal.soapy_backend ── SoapySDR ── SoapyPlutoSDR ── libiio (USB) ── Pluto
             └─ eds.hal.pluto_iio (ince) ──────────────────────────── libiio (ip:)  ── Pluto iiod
                xo_correction, RF-DC tracking, hızlı AGC, sıcaklık, fw_version, hw_serial
```

* Akış ve cihaz soyutlaması **SoapySDR** üzerindendir; pyadi-iio birincil arayüz değildir.
* İnce katman, SoapyPlutoSDR USB üzerinden akış yaparken ikinci bir libiio **USB** bağlamı
  genellikle açamadığı için Pluto'nun USB-Ethernet arayüzüne (`control_uri = "ip:..."`,
  iiod birden çok istemciye hizmet verir) bağlanır. Açılamazsa **uyarı + Soapy yoluna
  düşüş**; çökme yok.

Kurulum: `scripts/install_ubuntu.sh` (Ubuntu 22.04/24.04; idempotent). Doğrulama:

```bash
SoapySDRUtil --info | grep -i plutosdr            # modül yüklü mü
SoapySDRUtil --find="driver=plutosdr"             # takılı Pluto'lar (uri, label)
SoapySDRUtil --probe="driver=plutosdr,uri=usb:"   # kanal, kazanç, hız aralıkları, format (CS16, 2048)
iio_info -s                                       # libiio bağlamları: [usb:B.D.I] + serial=...
iio_info -u ip:192.168.2.1 | head                 # ağ üzerinden erişim
iio_attr -u ip:192.168.2.1 -C fw_version          # firmware sürümü
iio_attr -u ip:192.168.2.1 -d ad9361-phy xo_correction
python -c "import SoapySDR, iio; print('ok')"     # .venv içinde (system-site-packages)
```

---

## 3. İki Pluto'yu aynı anda bağlamak (IP, URI, seri no)

**Varsayılan olarak her Pluto 192.168.2.1 adresini kullanır** — iki Pluto aynı anda
bağlıyken ağ (ince katman, `ip:` URI'leri) çakışır. İkinci Pluto'nun IP'sini değiştirin:

1. Pluto B'yi **tek başına** takın; yığın-bellek sürücüsü (“PlutoSDR”) açılır.
2. `config.txt` dosyasında `[NETWORK]` bölümü:

   ```ini
   [NETWORK]
   hostname = pluto-b
   ipaddr = 192.168.3.1
   ipaddr_host = 192.168.3.10
   netmask = 255.255.255.0
   ```
3. Kaydedin ve sürücüyü **güvenli çıkarın (eject)** — Pluto ayarı flash'a yazar (LED yanıp
   söner; bu sırada kabloyu çekmeyin). Ayar kalıcıdır.
4. Doğrulayın: `ping -c1 192.168.3.1` ve `iio_info -u ip:192.168.3.1`.
5. Pluto A fabrika ayarında kalır (`192.168.2.1`). İki ağ arayüzü host'ta farklı alt ağlarda
   olur (192.168.2.10 / 192.168.3.10).

**Akış URI'si (USB) ve seri numarası:**

```text
$ iio_info -s
Available contexts:
    0: 0456:b673 (Analog Devices Inc. PlutoSDR (ADALM-PLUTO)), serial=1044730a1997... [usb:1.5.5]
    1: 0456:b673 (Analog Devices Inc. PlutoSDR (ADALM-PLUTO)), serial=104473dc5993... [usb:1.6.5]
```

* `uri = "usb:"` libiio'da “tek USB cihazı otomatik seç” demektir; **iki Pluto USB'deyken
  reddedilir** (EMLINK / “Too many links”). İki cihazlı konfigürasyonlarda tam URI yazılır.
* USB adresi (`usb:BUS.DEV.IF`) port değişince / yeniden takınca değişebilir; **seri
  numarası sabittir**. Seri numarasını kasaya etiketleyin (A/B), her oturumda
  `iio_info -s` ile URI ↔ seri eşlemesini doğrulayın. EDS açılışta okuduğu seriyi
  `DeviceInfo.serial` olarak rapora yazar ve kalibrasyonu `cal/<seri>.json`'dan yükler —
  yanlış eşleme raporda görünür.
* SoapyPlutoSDR `serial` anahtarına göre **filtrelemez**: args'a yalnızca `serial = "..."`
  yazmak yanlış cihazı sessizce açabilir. Seçim URI ile yapılır.

---

## 4. Firmware sürümü

* Okuma: `iio_attr -u ip:192.168.2.1 -C fw_version`, yığın-bellek sürücüsündeki `info.html`
  ya da EDS raporundaki cihaz bloğu (`DeviceInfo.firmware`; ince katman `firmware_version()`).
* **İki Pluto aynı firmware sürümünde olmalı** (DF'de davranış farkı istemiyoruz). Güncel
  kararlı sürümü ADI `plutosdr-fw` GitHub sürümlerinden alın.
* Güncelleme: `pluto.frm` dosyasını yığın-bellek sürücüsüne kopyalayın → güvenli çıkarın →
  LED'ler yanıp sönerken (birkaç dakika) **kabloyu çekmeyin**. Sorunlu güncellemede cihaz
  DFU moduna (USB `0456:b674`, udev kuralı bunu da kapsar) düşer; `dfu-util` ile kurtarılır.
* Firmware ortam değişkenleri (`fw_printenv`) — özellikle `xo_correction`, `attr_name`,
  `attr_val`, `mode`, referans saat seçimi — rapor koşullarına not edilmelidir.

---

## 5. İki Pluto ile DF: gerçekler

İki ayrı Pluto ile faz farkı (interferometre) DF'i **ancak** aşağıdakiler yönetilirse çalışır.
EDS'te ilgili modüller: `eds.hal.group.DeviceGroup`, `eds.df.coherence`
(`estimate_delay`, `phase_offset`, `check_coherence`), `eds.df.interferometer.Interferometer2`.

1. **Bağımsız TCXO'lar → sürekli faz kayması.** İki Pluto arasında Δppm 50 ppm'e kadar
   olabilir; 1 GHz'te bu **50 kHz** frekans farkı → faz farkı saniyede 50 000 tur döner.
   Yazılım ppm düzeltmesi sonrası bile 0.01 ppm artık → 1 GHz'te 10 Hz → **3600 °/s**.
   *Sonuç: ortak referans olmadan iki Pluto ile DF yapılamaz.*
2. **Ortak 40 MHz referans.** Pluto **Rev C/D**'de harici referans girişi (**CLK_IN**, U.FL)
   vardır. Tek bir 40 MHz TCXO/OCXO (tercihen 10 MHz GPSDO'ya kilitli) → clock buffer /
   güç bölücü → iki Pluto'nun CLK_IN'i. Harici referans seçimi bir IIO özniteliği değil,
   **firmware ortam değişkenidir** (ör. Pluto kabuğunda `fw_setenv refclk_source external` +
   yeniden başlatma; değişken adı/değerleri ve CLK_IN giriş seviyesi firmware/kart
   revizyonuna bağlıdır — kendi sürümünüzde ve kart şemasında doğrulayın). Harici
   referansta da `xo_correction` gerçek referans frekansına eşit olmalıdır. Simülatörde bu
   durum `shared_clock = "ref40"` ile modellenir (`config/dual_sim.toml`).
3. **Her retune'da rastgele LO fazı → her retune'dan sonra kalibrasyon.** Ortak referansla
   bile her Pluto'nun PLL/bölücü fazı her ayarda rastgeledir. Kalibrasyon: gürültü kaynağı
   veya sinyal üreteci → **eşit boylu, aynı tip kablolar** → 2'li güç bölücü → iki RX
   (§7.2). Ölçülen `φ_cal = angle(x1·x0*)` DF fazından çıkarılır. Kazanç, BW, tracking
   değişimi de fazı oynatabilir → aynı koşullarda kalibre edin; DF sırasında AGC kapalı ve
   iki kanalın kazancı eşit.
4. **Örnek hizalaması.** İki USB akışı farklı anlarda başlar; aradaki gecikme (tam + kesirli
   örnek) her akış başlangıcında değişir. Ortak **geniş bantlı** bir sinyalin (gürültü
   kaynağı) çapraz korelasyonu ile kestirilir (`estimate_delay`). Dar bantlı tek ton gecikme
   kestirimi için belirsizdir; gecikme tonlar arasında frekansa bağlı faz eğimi üretir
   (`Δφ = 2π·Δf_bb·Δn/fs`). **Herhangi bir overflow'da hizalama kaybolur** (grup bloğu
   `overflow = True`) → gecikme + faz yeniden kalibre edilir.
5. **Koherens kontrolü.** `check_coherence` faz kaymasını (°/s) ve koherensi (0…1) ölçer;
   `coherent = False` iken DF sonucu yayımlanmaz ve `df_rms` ölçümü bu raporu sonuçla birlikte
   yazar.
6. **Geometri.** İki eleman arası `d = λ/2` belirsizliksiz ±90° verir; 0.15 m ⇒ 1 GHz'e kadar
   belirsizliksiz (`f ≤ c/(2d)`). Azimut konvansiyonu: dizi eksenine dik (broadside) = 0°,
   dizi ekseni boyunca 2. elemana (rx1) doğru pozitif. `sin θ` doğrusal olmadığından hata
   kenarlarda `1/cos θ` ile büyür → KTR açısı tipik olarak ±60° içinde verilir.

> **Faz-1 notu:** KrakenSDR bu sorunların hepsini donanımda çözer (ortak saat, dahili gürültü
> kaynağı ile otomatik koherens kalibrasyonu). EDS'te DF kodu yalnızca `num_channels` ve
> `coherent` görür; Kraken geldiğinde DF katmanı değişmez.

---

## 6. Spesifikasyon dışı modlar (AD9364 unlock, 2R2T)

**AD9364 “unlock”** — Pluto firmware'ine çipi AD9364 gibi tanıtır (70 MHz – 6 GHz, 56 MHz BW):

```bash
ssh root@192.168.2.1
fw_setenv attr_name compatible
fw_setenv attr_val ad9364
reboot
```

* Silikon hâlâ **AD9363**'tür; 325 MHz–3.8 GHz ve 20 MHz dışı **garanti edilmez** (PLL
  kilitlenmeyebilir, NF/imaj/düzlük bozulur). EDS'te yalnızca bilinçli seçilen
  `ad9364_pluto_ext` profili ile kullanılabilir; **jüriye sunulan nicel değerler resmî
  `ad9363_pluto` profiliyle ve resmî aralıkta ölçülmelidir.**
* Geri almak: `fw_setenv attr_val ad9363a` (veya `fw_setenv attr_name` / `attr_val` boşaltılır)
  + `reboot`; firmware sürümünüzün belgelerinden doğrulayın.

**2R2T modu (Rev C/D)** — AD9363 silikonu aslında 2×2'dir; Pluto Rev C/D'de 2. RX/TX
U.FL konnektörlere çıkarılmıştır. Firmware ile 2R2T (ör. `fw_setenv mode 2r2t` ve
`attr_val ad9361`) etkinleştirilebilir. İki RX kanalı **aynı LO ve saati paylaştığı** için
tek Pluto'da koherent iki kanal verir — DF için çok değerlidir, ancak:

* resmî ADALM-Pluto spesifikasyonu (1R1T) dışıdır; 2. kanalın RF performansı belirtilmemiştir
  → ölçüp raporlayın,
* SoapyPlutoSDR sürümünüzün 2 kanalı desteklediğini `SoapySDRUtil --probe` ile doğrulayın,
* EDS'e eklemek = yeni profil (`rx_channels = 2`, `coherent_rx = true`) + `channels = [0, 1]`;
  üst katmanlar değişmez.

---

## 7. Tezgah bağlantı şemaları

### 7.1 Ölçüm tezgahı (duyarlılık, dinamik alan, anlık BW, imaj, frekans, gecikme)

```text
                     USB 2.0 (kök port 1)                    USB 2.0 (kök port 2)
          ┌───────────────────────────────── HOST ───────────────────────────────┐
          │                               Ubuntu + EDS                           │
          ▼                                                                      ▼
 ┌─────────────────┐   SMA   ┌───────────────┐   ┌──────────────────┐  SMA  ┌─────────────────┐
 │ Pluto B         │  TX ───►│ ≥30 dB SABİT  ├──►│ 0–60(90) dB      ├──────►│ RX  Pluto A     │
 │ (uyaran,        │         │ zayıflatıcı   │   │ kademeli         │       │ (DUT, rx0)      │
 │  ip:192.168.3.1)│         │ HER ZAMAN HAT │   │ zayıflatıcı      │       │ ip:192.168.2.1  │
 └─────────────────┘         └───────────────┘   └──────────────────┘       └─────────────────┘
        Pluto'ları ≥1 m ayırın / metal kutu: kutu sızıntısı kablolu sinyalden büyük olabilir.
        Kontrol: TX kablosunu 50 Ω ile sonlandırın → ton spektrumdan KAYBOLMALI.
```

`config/rx_pluto.toml` bu tezgahı tanımlar (`measure.source = "pluto_tx"`). Mutlak güç için
`anchor_dbm` (bağıl 0 dB'de DUT girişinde, kalibre güç ölçerle ölçülen güç) girilir.
Laboratuvar üreteci varsa `source = "scpi"` (daha doğru seviye, düşük faz gürültüsü).

### 7.2 DF faz/gecikme kalibrasyonu

```text
                                         kablo L (eşit boy, aynı tip/lot)
 Gürültü kaynağı veya ──►┌────────────┐ ─────────────────────────────► RX  Pluto A (rx0)
 sinyal üreteci          │ 2'li güç   │
 (RX'te ≤ −40 dBm)       │ bölücü     │ ─────────────────────────────► RX  Pluto B (rx1)
                         └────────────┘  kablo L

 40 MHz TCXO/OCXO ──► [clock buffer / bölücü] ──┬──► CLK_IN  Pluto A   (Rev C/D, U.FL)
 (opsiyonel 10 MHz GPSDO kilidi)                └──► CLK_IN  Pluto B
```

Bölücünün iki kolu arasındaki faz/genlik dengesizliğini bir kez ölçün (kolları değiştirip
tekrar) ve kalibrasyondan çıkarın.

### 7.3 DF havadan ölçüm

```text
                    hedef verici (bilinen θ, uzak alan: R ≫ 2D²/λ, en az birkaç λ)
                               \
                                \ θ  (broadside'dan rx1 yönüne pozitif)
                                 \
        Anten 0  ●────────── d = λ/2 = 0.15 m @ 1 GHz ──────────●  Anten 1        +x →
           │                                                       │
     [RF anahtar] ◄──── bölücü kolu (kalibrasyon) ────►      [RF anahtar]
           │                                                       │
      RX Pluto A (rx0)                                        RX Pluto B (rx1)
```

Anahtar her retune sonrası “kalibrasyon” konumuna alınır, `φ_cal` ölçülür, sonra “anten”
konumunda DF yapılır. Çok yollu yayılım (yansıma) DF RMS'i bozar: açık alan / emici malzeme.

---

## 8. RF güvenliği

* **ASLA TX'i doğrudan RX'e bağlamayın** (aynı Pluto'nun kendi TX→RX'i dahil). Pluto TX
  çıkışı frekansa bağlı olarak ≈ +5…+7 dBm'e ulaşabilir; AD9361/AD9363 veri sayfasındaki
  **mutlak maksimum RX giriş gücü yalnızca birkaç dBm'dir** (AD9361: +2.5 dBm tepe — kendi
  veri sayfası revizyonunuzdan doğrulayın). LNA kalıcı hasar görebilir.
* TX→RX yolunda **her zaman ≥ 30 dB sabit zayıflatıcı** bulunur (kademeli zayıflatıcı sıfıra
  alınsa bile). +7 dBm − 30 dB = −23 dBm: güvenli; yüksek kazançta kırpar (zararsız; EDS
  kırpmayı algılar ve raporlar).
* Kablo takıp çıkarmadan önce TX'i kapatın (`set_enabled(False)`); konnektörleri statik
  boşalmaya karşı koruyun (ESD bilekliği, önce gövdeye dokunun). SMA'ları tork anahtarıyla
  sıkın; gevşek konnektör seviye hatası ve faz kayması üretir.
* Antene bağlı TX yalnızca yarışma alanında ve kurallarına uygun yapılır; lisanssız yayın
  yasaktır. Tezgahta TX **daima kablolu + zayıflatıcılı** veya yapay yüklü (dummy load).
* Bilinmeyen güçlü bir kaynağı (ör. telsiz, radar) ölçerken önce yüksek zayıflatma ve en
  düşük kazançla başlayın.

---

## 9. KrakenSDR / HackRF / bladeRF / USRP nasıl takılır

Üst katmanlar (`eds.rx`, `eds.dsp`, `eds.calib`, `eds.df`, `eds.measure`, `eds.bus`) yalnızca
`RxDevice` arayüzünü görür (`num_channels`, `coherent`, `configure`, `read`, `capture`).
Yeni donanım = **profil TOML** + (gerekiyorsa) **backend dosyası**; yukarı akışta değişiklik yok.

| Donanım | Profil (`config/profiles/`) | Backend | Ne değişir |
|---|---|---|---|
| bladeRF 2.0 micro (AD9361, 2R2T koherent, USB3) | `bladerf2_micro.toml` | mevcut `soapy` (SoapyBladeRF) | `[[device]] profile = "bladerf2_micro"`, `args = { driver = "bladerf" }`, `channels = [0, 1]`. DF tek cihazda koherent (`coherent = True`). |
| USRP B210 (AD9361 2×2, USB3) | `usrp_b210.toml` | mevcut `soapy` (SoapyUHD) | `args = { driver = "uhd" }`, `channels = [0, 1]`. |
| HackRF One (TX) | `hackrf_one.toml` | mevcut `tx_hw.SoapyTxSource` | `measure.source = "pluto_tx"` yerine aynı sınıf `args = { driver = "hackrf" }` ile; 8-bit, yarı çift yönlü. |
| KrakenSDR (5 kanal koherent) | `krakensdr.toml` (`backend_hint = "heimdall"`) | **yeni** `eds/hal/heimdall_backend.py` (Heimdall DAQ akışı) | Factory'ye ve `[[device]] backend` doğrulamasına yeni ad eklenir; `num_channels = 5`, `coherent = True`. DF/ölçüm kodu değişmez. |

Profil `status = "planned"` olan cihazlar çalıştırılabilir konfigürasyonlarda kullanılmaz
(testler bunu denetler). Cihaz gelince profildeki değerler **veri sayfasıyla** doğrulanıp
`status = "supported"` yapılır ve `eds measure all` ile aynı KTR tablosu üretilir.

---

## 10. Sorun giderme

| Belirti | Olası neden | Çözüm |
|---|---|---|
| `SoapySDR cihazı açılamadı ... Bulunan cihazlar: yok` | Modül yok / udev izni / kablo | `SoapySDRUtil --info \| grep plutosdr`; `ls -l /dev/bus/usb/*/*`; `groups` içinde `plugdev`; veri taşıyan kablo. |
| `usb:` ile açılmıyor, “Too many links” | İki Pluto USB'de | Tam URI (`iio_info -s`). |
| `Permission denied` (libusb) | udev kuralı / grup | `scripts/install_ubuntu.sh`; oturumu kapat-aç; Pluto'yu yeniden tak. |
| İnce katman açılamadı uyarısı | `control_uri` yanlış / ağ arayüzü yok | `ping 192.168.2.1`; `ip addr` (USB ağ arayüzü DHCP almış mı); iki Pluto için farklı IP (§3). |
| Sürekli overflow | USB/host darboğazı | `eds measure max_rate`; ayrı kök port; hız düşür; `bufflen` büyüt; CPU `performance`. |
| Açılışta “xo_correction nominal değil” uyarısı | Firmware ortamında kalıcı düzeltme var | Bilinçli ise sorun yok (EDS nominale göre kestirir); değilse `fw_setenv xo_correction` kaldırın. |
| Frekanslar ~kHz kaymış | PPM kalibrasyonu yok/eski | GSM FCCH veya referans CW ile kalibre et; ısınma; `cal.ppm_mode`. |
| Spektrumun ortasında çivi | Sıfır-IF DC | DC tracking açık mı; `dc_notch_bins`; sinyali DC'den uzağa ayarla. |
| Retune sonrası ilk spektrum yanlış frekansta | Bayat çekirdek tamponları | Ham `read()` yerine `capture()` kullanın; `settle_buffers` ≥ `kernel_buffers + 1`. |
| Pluto açılırken bağlantı düşüyor / ModemManager | ModemManager AT komutu gönderiyor | udev kuralındaki `ID_MM_DEVICE_IGNORE` (kurulum betiği yükler). |
