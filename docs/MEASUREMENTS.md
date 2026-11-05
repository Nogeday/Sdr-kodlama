# KTR Nicel Spesifikasyon Ölçümleri — Tanımlar, Tezgah, Prosedür, Rapor

> EDS aynı zamanda bir **ölçüm cihazıdır**: KTR'de istenen 9 nicel spesifikasyonu kendisi
> ölçer, koşullarıyla birlikte tablo + grafik + spektrum “ekran görüntüsü” olarak dışa aktarır.
> Jürinin en sık puan kırdığı nokta **koşulsuz sayı** ve **eksik nicel spec**tir; bu belge her
> ölçümün jüriye savunulabilir tanımını ve EDS'in onu nasıl ürettiğini verir.
>
> Kod: `eds.measure` (çerçeve `measure/base.py`, ölçümler `measure/specs/<spec_id>.py`,
> oturum `measure/session.py`). Donanım: [`HARDWARE_PLUTO.md`](HARDWARE_PLUTO.md).
> Konvansiyonlar: [`ARCHITECTURE.md`](ARCHITECTURE.md) §2.

| # | `spec_id` | Spesifikasyon | Birim | “İyi” yönü |
|---|---|---|---|---|
| 1 | `sensitivity` | Duyarlılık (Pd = 0.9) + MDS + NF | dBm | küçük (daha negatif) |
| 2 | `dynamic_range` | Anlık dinamik alan + SFDR + toplam (kazanç yönetimli) | dB | büyük |
| 3 | `inst_bw` | Anlık bant genişliği (−3 dB, −1 dB) | MHz | büyük |
| 4 | `scan_speed` | Tarama hızı + adım dökümü + yeniden ziyaret süresi | GHz/s | büyük |
| 5 | `freq_resolution` | RBW + ölçülen iki-ton ayrımı + frekans doğruluğu | Hz | küçük |
| 6 | `df_rms` | Yön bulma RMS hatası | ° | küçük |
| 7 | `image_rejection` | İmaj bastırma (tracking açık/kapalı) | dB | büyük |
| 8 | `max_rate` | Taşma-sız maksimum örnekleme hızı | MS/s | büyük |
| 9 | `latency` | Uçtan uca gecikme (RF olayı → ZMQ teslimi) | ms | küçük |

---

## 0. Ortak esaslar

### 0.1 Çalıştırma

```bash
eds measure all              --config config/rx_sim.toml --quick   # donanımsız, hızlı (CI/demo)
eds measure sensitivity      --config config/rx_pluto.toml         # tek ölçüm, gerçek Pluto
eds measure df_rms           --config config/dual_pluto.toml       # 2 Pluto DF
```

* Her ölçüm `[measure.params.<spec_id>]` tablosundan parametre alır (ör.
  `[measure.params.df_rms] baseline_m = 0.15`). Öncelik: ölçümün varsayılanı < konfigürasyon
  < CLI.
* Ölçümler birbirinden **yalıtılır**: her ölçümden önce cihazlar `[rx]` başlangıç ayarına
  döndürülür ve kaynak kapatılır (`session.reset_baseline`) — bir ölçümün hız/kazanç/tracking
  değişikliği diğerine sızmaz.
* Gereksinimi karşılanmayan ölçüm **atlanır** (`status = "skipped"`, nedeni Türkçe yazılır;
  ör. “Sinyal kaynağı gerekli”, “En az 2 RX kanalı gerekli”); hata veren ölçüm `failed`
  olur, raporda iz (traceback) bulunur. Hiçbir ölçüm sessizce yok olmaz.

### 0.2 Seviye ve spektrum konvansiyonları (ARCHITECTURE §2)

```text
0 dBFS      = tam ölçek genlikli kompleks üstelin gücü: mean(|x|²) = 1     (I = counts/2048)
Güç spektrumu P[k] = |Σ w·x·e^{-j2πkn/N}|² / (Σw)²     → bin merkezindeki ton gücünü doğru okur
RBW         = ENBW_bins · fs / N ,  ENBW_bins = N·Σw² / (Σw)²   (Blackman-Harris: 2.004 bin)
Bant gücü   = Σ P[k] / ENBW_bins                         → scalloping'den bağımsız ton/kanal gücü
Yoğunluk    = P − 10·log10(RBW)   [dB/Hz]
dBm         = dBFS + offset(f, G)  (offset ≈ C(f) − G)
NF          = DANL[dBm/Hz] − (−174)  = DANL + 174   (kT @ 290 K = −173.975 dBm/Hz)
```

Örnek: fs = 4 MS/s, N = 4096, Blackman-Harris ⇒ RBW = 2.004 × 976.6 Hz = **1.957 kHz**.

### 0.3 dBFS → dBm (seviye referansı)

`eds.measure.common.level_reference` sırası:

1. **Kalibrasyon tablosu** (`cal/<seri>.json`, `PowerCalTable.offset_db(f, G)`) → `dBm`,
   `calibrated = true`.
2. **Mutlak kaynak** (sim, SCPI üreteç, `anchor_dbm`'li Pluto TX) → yerinde (in-situ)
   referans: bilinen güçte ton enjekte edilir, `offset = P_in − P_ölçülen(dBFS)`.
3. Hiçbiri yoksa sonuç **dBFS** kalır, `calibrated = false` olarak işaretlenir. Bu sonuç
   jüriye **dBm diye sunulmaz**.

### 0.4 Uyaran kaynakları (`[measure] source`)

| `source` | Sınıf | Güç | Not |
|---|---|---|---|
| `sim` | `SimSignalSource` | mutlak | Simülatör; gerçek değer (ground truth) bilinir → prosedür doğrulaması. |
| `pluto_tx` | `SoapyTxSource` | bağıl; `anchor_dbm` ile mutlak | 2. Pluto. TX LO tonun `tone_offset_hz` altında (LO sızıntısı tonla çakışmaz). |
| `scpi` | `ScpiSignalGenerator` | mutlak | LAN üzerinden laboratuvar üreteci (TCP 5025); en doğrusu. |
| `manual` | `ManualSource` | mutlak/bağıl | Operatör üreteci elle ayarlar; her adımda Enter bekler. |
| `none` | – | – | Kaynak gerektiren ölçümler atlanır ya da kaynaksız yedek yöntem kullanılır. |

### 0.5 Raporlanan her sonuçta

`MeasurementResult`: **değer + birim**, Türkçe tek satır **özet**, **yöntem** metni,
**koşullar** (backend, seri no, profil, fc, fs, BW, kazanç, kazanç modu, ppm donanım
düzeltmesi, sıcaklık, nfft, RBW, ortalama, pencere + ölçüme özgü), **belirsizlik**, ham
**tablo** satırları, **şekiller** (`FigureSpec` → PNG; `kind = "spectrum"` jüriye dönük
spektrum analizörü görünümlü ekran görüntüsü), **hedef** karşılaştırması
(`config/ktr_targets.toml` → `passed`). Sonuçlar ayrıca bus'ta `meas.result` olarak
yayımlanabilir. Rapor klasörü varsayılan olarak `reports/` altındadır (`[measure] out_dir`).

### 0.6 Genel tuzaklar ve kodun önlemleri

| Tuzak | Etki | EDS önlemi |
|---|---|---|
| Retune sonrası bayat tamponlar | Yanlış frekans/kazançta veri | `capture()` settling örneklerini atar (`settle_buffers = kernel_buffers + 1`). |
| Kırpma | Sahte harmonik/IMD, yanlış güç | `iq_levels.clip_fraction` kontrolü; kırpılmış blok kullanılmaz. |
| HW AGC açık | Örnekle senkron kazanç yok | Ölçümler `gain_mode = "manual"` kurar. |
| Sıfır-IF DC çivisi | Tonun üstüne DC biner | Tonlar DC'den uzak ofsette; `dc_notch_bins`. |
| Scalloping | Ton gücü ±0.8 dB (BH) hatalı | Ton gücü **bant gücü** ile (`tone_power_db`). |
| PPM hatası | Frekans/bin kayması | Kalibrasyon (`cal.ppm_mode`); koşullara `ppm_hw` yazılır. |
| Isınma / sıcaklık | Frekans ve kazanç kayar | 10–15 dk ısınma; sıcaklık her sonuçta; kalibrasyondan 5 °C sapma uyarısı. |
| Kutu sızıntısı (Pluto→Pluto) | Kablolu seviyeden büyük sızıntı | Tezgah kontrolü: TX kablosu sonlandırılınca ton kaybolmalı (HARDWARE §7.1). |
| Overflow | Bloklar arası boşluk | `read()` asla iç boşluklu blok döndürmez; `capture(allow_overflow=False)` yeniden dener. |

---

## 1. Duyarlılık — `sensitivity` [dBm]

**Tanım (jüri).** Belirtilen RBW, ortalama sayısı ve CFAR yanlış alarm olasılığında (Pfa),
tespit olasılığını **Pd = 0.9** yapan en küçük giriş gücü:

```text
P_sens = min { P_in : Pd(P_in) ≥ 0.9 }     @ Pfa = detect.pfa (vars. 1e-6), RBW, n_avg
```

Gürültü tabanından hesaplanan çapraz kontrol (MDS) ve gürültü faktörü:

```text
DANL [dBm/Hz] = N_RBW [dBm] − 10·log10(RBW)
MDS  [dBm]    = DANL + 10·log10(B) + SNR_min        (B = tespit bant genişliği = RBW)
NF   [dB]     = DANL + 174
```

`SNR_min`, `n_avg` ortalamalı periodogram gürültüsünün Gamma dağılımı (`eds.dsp.stats`:
`cfar_alpha(pfa, n_avg)`, `gamma_quantile`) ile Pd = 0.9 için gereken SNR'dir.
Örnek hesap (NF = 5 dB, RBW = 1.957 kHz): DANL = −169 dBm/Hz → RBW'de gürültü −136.1 dBm.

**Tezgah.** HARDWARE §7.1: kaynak → ≥30 dB sabit + kademeli zayıflatıcı → DUT RX. Seviye
belirsizliğini azaltmak için üreteci doğru çalıştığı aralıkta tutun, kalanı sabit
zayıflatıcılarla sağlayın. Kutu sızıntısı kontrolü zorunlu.

**Prosedür (`eds measure sensitivity`).**

1. DUT: ölçüm frekansı, fs, nfft (RBW), **en yüksek kazanç** (banda göre 73/71 dB),
   manual kazanç, tracking açık.
2. Seviye referansı (§0.3) belirlenir.
3. **Gürültü:** giriş sonlandırılır (`ctx.terminated_input()`; simülatörde tüm emitter'lar
   kapatılır, donanımda operatöre 50 Ω takması söylenir) → medyan taban `N_RBW`, DANL, NF.
   Kaynak kapalıyken dedektörün **ampirik yanlış alarm oranı** sayılır.
4. **Pd eğrisi:** ton, bin merkezine ve DC/bilinen spur'lardan uzağa konur; güç SNR ≈ 20 dB
   seviyesinden aşağı adımlanır (normal 1 dB, `--quick` daha kaba). Her seviyede N bağımsız,
   settled yakalama → aynı CFAR dedektörü (`eds.dsp.detect.detect`, `[detect]` ayarları) →
   tonu kapsayan tespit = isabet. `Pd = isabet / N`.
5. `Pd = 0.9` geçişi komşu seviyeler arasında (dB ekseninde) enterpolasyonla bulunur;
   belirsizlik = binom güven aralığı + seviye referansı belirsizliği.
6. MDS formülüyle çapraz kontrol (fark büyükse rapora uyarı).

**Raporlanan.** Değer: `P_sens` (dBm; kalibrasyonsuzsa dBFS + `calibrated = false`).
Ek: DANL (dBm/Hz), NF (dB), N_RBW, MDS, `SNR_min`, ampirik Pfa, deneme sayısı.
Koşullar: fc, fs, RBW, pencere, n_avg, kazanç, CFAR türü/Pfa/guard/train, seviye referansı.
Şekiller: Pd–P_in eğrisi (0.9 çizgisi), duyarlılık seviyesinde spektrum ekran görüntüsü
(ton + CFAR eşiği).

**Tuzaklar.** Sızıntı (kablosuz yol) duyarlılığı olduğundan iyi gösterir → RF kapalı/kablo
sonlandırılmış kontrol; düşük seviyede üretecin kendi doğruluğu → sabit zayıflatıcı;
DC/spur çakışması → ton ofseti; kazanç en yüksekte değilse NF kötüleşir; `Pd` için az deneme
→ geniş güven aralığı (rapor N'yi yazar).

**Örnek KTR cümlesi.**
> “EDS alıcısının duyarlılığı 935 MHz'de, RBW = 1.96 kHz (Blackman-Harris, N = 4096,
> fs = 4 MS/s, 16 ortalama), OS-CFAR Pfa = 10⁻⁶ ve 73 dB kazançta **Pd = 0.9 için ‹…› dBm**
> olarak ölçülmüştür (seviye başına ‹N› deneme, kalibre kaynak, belirsizlik ±‹…› dB).
> Gürültü tabanından hesaplanan MDS ‹…› dBm, gürültü faktörü ‹…› dB'dir.”

---

## 2. Dinamik alan — `dynamic_range` [dB]

**Tanım (jüri).** Üç ayrı sayı raporlanır; KTR'de hangisinin kullanıldığı açıkça yazılmalıdır.

```text
DR_anlık   = P_max,kırpmasız [dBFS] − N_RBW [dBFS]            (aynı kazanç, aynı RBW)
SFDR [dBc] = P_taşıyıcı − P_en büyük spur                     (DC hariç; imaj dahil ve hariç ayrı)
SFDR [dBFS]= 0 dBFS − P_en büyük spur [dBFS]                   (taşıyıcı kırpmanın hemen altında)
DR_toplam  = P_in,max(G_min, kırpmasız) [dBm] − P_sens(G_max) [dBm]   (kazanç yönetimli)
```

12-bit referans: ideal niceleme gürültüsü (I+Q) ≈ −74 dBFS (fs bandında); RBW'de
`−74 − 10·log10(fs/RBW)` (4 MS/s, 1.96 kHz: ≈ −107 dBFS). Gerçek taban yüksek kazançta
termal gürültü, düşük kazançta ADC gürültüsüdür.

**Tezgah.** §7.1. SFDR için kaynağın harmonik/spur seviyesi DUT'tan iyi olmalı (üreteç +
alçak geçiren filtre). Pluto TX'in kendi spur'ları SFDR'yi sınırlayabilir → raporda kaynak
belirtilir.

**Prosedür (`eds measure dynamic_range`).**

1. Sabit kazançta giriş sonlandırılır → `N_RBW`.
2. Ton gücü artırılarak kırpma başlangıcı bulunur (`clip_fraction > agc.max_clip_fraction`);
   kırpmasız en büyük ton gücü `P_max` (dBFS) → `DR_anlık`.
3. Ton kırpmanın ≈1 dB altında tutulur, spektrumda DC ve taşıyıcı ana lobu hariç en büyük
   bileşen aranır → SFDR (dBc, dBFS); imajın payı ayrıca verilir.
4. Toplam: en düşük kazançta kırpmasız en büyük giriş (dBm) − en yüksek kazançta duyarlılık
   (ya da MDS). Kaynağın üst gücü yetmezse alt sınır (“≥”) olarak raporlanır.

**Raporlanan.** Değer: `DR_anlık` (dB). Ek: SFDR dBc/dBFS, en büyük spur frekansı, `DR_toplam`,
P_max, N_RBW, kullanılan kazançlar. Şekiller: taşıyıcı + spur'lar işaretli spektrum ekran
görüntüsü; giriş–çıkış (P_in → dBFS) doğrusallık eğrisi, kırpma noktası işaretli.

**Tuzaklar.** RBW belirtilmeden DR anlamsızdır (rapor RBW'yi yazar); I ve Q **ayrı** kırpılır:
tam ölçek CW'nin gücü 0 dBFS, gürültü benzeri sinyalin tepe/ortalama oranı 10–12 dB → OFDM için
pratik DR daha düşüktür (not düşülür); DC ve imaj spur sayılırsa SFDR düşer — rapor hangisini
saydığını yazar; HW AGC kapalı olmalı.

**Örnek KTR cümlesi.**
> “50 dB sabit kazançta ve 1.96 kHz RBW'de anlık dinamik alan **‹…› dB**, SFDR ‹…› dBc
> (‹…› dBFS) ölçülmüştür. Kazanç yönetimi (−1…73 dB) ile toplam dinamik alan ‹…› dB'dir
> (‹…› dBm kırpmasız giriş @ −1 dB kazanç; ‹…› dBm duyarlılık @ 73 dB).”

---

## 3. Anlık bant genişliği — `inst_bw` [MHz]

**Tanım (jüri).** Sabit giriş gücünde ölçülen alıcı genlik yanıtı `H(f)`'nin referans
seviyesinden 3 dB (ve 1 dB) düştüğü frekanslar arasındaki genişlik:

```text
BW_−3dB = f_üst − f_alt ,   H(f_üst) = H(f_alt) = H_ref − 3 dB
H(f) = P_ölçülen(fc + δ) − P_giriş   (δ: taban bant ofseti, bant gücü ile)
```

Fiziksel üst sınır: AD9363 analog BW ≤ 20 MHz ve `fs` (alias'sız bölge ±fs/2).

**Tezgah.** §7.1 (kaynak ↔ DUT). Kaynağın frekansa göre düzlüğü DUT'tan iyi olmalı (değilse
kaynak, referans cihazla ölçülüp düzeltilir).

**Prosedür (`eds measure inst_bw`).**

1. DUT ölçüm hızında (ör. fs = 20 MS/s, BW = 20 MHz) ve sabit kazançta kurulur.
2. Ton `−fs/2…+fs/2` aralığında adımlanır (kenarlarda sıklaştırılmış); her adımda settled
   yakalama, ton gücü **bant gücü** ile; DC çevresi ve tonun kendi imajı ölçüme katılmaz.
3. Yanıt referansa normalize edilir; −1 dB ve −3 dB geçişleri doğrusal enterpolasyonla bulunur;
   geçiş bandında dalgalanma (ripple) hesaplanır.
4. **Kaynak yoksa yedek yöntem:** giriş sonlandırılıp en yüksek kazançta termal gürültünün
   spektral şekli (analog LPF + dijital filtreler) ölçülür; düzleştirilmiş şeklin −3 dB
   genişliği raporlanır ve yöntem alanında **“kaynaksız (gürültü şekli) — düşük güven”**
   yazar (ADC'nin düz tabanı kenarlarda şekli maskeleyebilir).

**Raporlanan.** Değer: −3 dB genişliği (MHz). Ek: −1 dB genişliği, düzlük (dB), kenar
frekansları, yöntem (ton/gürültü). Şekiller: yanıt eğrisi (−1/−3 dB çizgileri), bant kenarı
spektrum ekran görüntüsü.

**Tuzaklar.** DC çentiği bölgesinde ton görünmez → atlanır; ±fs/2 dışı tonlar alias olur →
yalnızca ±fs/2 içinde adım; analog BW > fs ise −3 dB genişliği fs ile sınırlanır (rapor
`fs` ve `bw` ayarını yazar); PPM düzeltilmezse ton beklenen binde değildir → ton frekansı
ölçülerek izlenir.

**Örnek KTR cümlesi.**
> “fs = 20 MS/s ve 20 MHz analog BW ayarında ölçülen anlık bant genişliği **‹…› MHz (−3 dB)**,
> ‹…› MHz (−1 dB) olup geçiş bandı düzlüğü ±‹…› dB'dir (ton adımlama, ‹…› nokta).”

---

## 4. Tarama hızı — `scan_speed` [GHz/s]

**Tanım (jüri).**

```text
S = (f_stop − f_start) / T_tarama                       [GHz/s]
T_tarama = Σ_adımlar (t_tune + t_settle + t_capture + t_dsp)   (duvar saati)
Δf_adım  ≈ usable_fraction · fs  (dc_avoid örtüşmesi düşülür)
T_yeniden_ziyaret = aynı frekansın iki gözlemi arası süre ≈ T_tarama (+ taramalar arası boşluk)
```

325 MHz–3.8 GHz (3.475 GHz) açıklık, `[sweep]` ayarları (20 MS/s burst, RBW).

**Tezgah.** Kablo gerekmez (anten veya sonlandırıcı); host ve USB koşulları rapora yazılır.

**Prosedür (`eds measure scan_speed`).**

1. `Sweeper(device, cfg.sweep, ...)` ile adım planı (`plan()`) çıkarılır.
2. Bir veya daha çok tam tarama `run()` edilir; her adımda `Stopwatch` (perf_counter) ile
   **tune / settle / capture / dsp** süreleri toplanır.
3. `S` taramaların ortalaması (ve en kötüsü) olarak hesaplanır; adım başına ortalamalar ve
   yüzde dağılımı çıkarılır; tespit sayısı da raporlanır (tespit dahil hız).

**Raporlanan.** Değer: S (GHz/s). Ek: T_tarama, adım sayısı, Δf_adım, adım başına ms döküm
(tune/settle/capture/dsp), yeniden ziyaret süresi, RBW, ortalama sayısı. Şekiller: adım
dökümü (yığılmış çubuk), taramanın max-hold spektrum ekran görüntüsü (tespitler işaretli).

**Tuzaklar.** Tarama hızı RBW ve ortalamaya bağlıdır (yakalama süresi ∝ N·n_avg/fs) — RBW
belirtilmeden sayı anlamsız; settle süresi `(kernel_buffers + 1) × buffer_size / fs`
olduğundan tampon boyu hızı doğrudan belirler; LO retune süresi yalnızca PLL kilidi değil,
sürücü/USB öznitelik yazma gecikmesini de içerir; adımlar arası overflow sonucu etkilemez
(burst); yeniden ziyaret süresinden kısa sinyaller kaçabilir (POI) — rapor bunu yazar.

**Örnek KTR cümlesi.**
> “325 MHz–3.8 GHz bandı, 20 MS/s burst, 19.6 kHz RBW ve 4 ortalama ile **‹…› GHz/s** hızla
> taranmaktadır (tam tarama ‹…› s, ‹…› adım; adım başına ortalama tune ‹…› ms, settle ‹…› ms,
> yakalama ‹…› ms, işleme ‹…› ms). Aynı frekans en geç ‹…› s'de bir yeniden gözlenir.”

---

## 5. Frekans çözünürlüğü — `freq_resolution` [Hz]

**Tanım (jüri).**

```text
RBW     = ENBW_bins · fs / N                        (hesap; Blackman-Harris 2.004 bin)
Δf_min  = eşit güçlü iki tonun arasında ≥ 3 dB çukur görülen en küçük ayrım   (ölçülen)
e_f     = f_ölçülen − f_gerçek   [Hz] ve [ppm]   — ppm düzeltmesi ÖNCESİ ve SONRASI
```

**Tezgah.** İki-ton: iki tonlu kaynak (simülatör, Pluto TX `max_tones = 2`) veya iki üreteç +
güç birleştirici. Frekans doğruluğu: referansa kilitli üreteç veya GSM FCCH.

**Prosedür (`eds measure freq_resolution`).**

1. RBW, konfigürasyondan hesaplanır.
2. Eşit güçlü iki ton, ayrım birkaç RBW'den RBW'nin altına doğru adımlanarak verilir; her
   adımda spektrumda iki tepe ve aradaki çukur derinliği ölçülür; ≥ 3 dB çukur ölçütünün
   sağlandığı en küçük ayrım enterpolasyonla bulunur. Tonların göreli fazının çukura etkisi
   için birden çok yakalama kullanılır.
3. İki-ton kaynağı yoksa yalnızca RBW (hesap) raporlanır ve yöntem alanı bunu belirtir.
4. Frekans doğruluğu: bilinen frekanstaki ton yüksek çözünürlüklü kestirimle
   (`tone_frequency`: sıfır dolgu + enterpolasyon) ölçülür; PPM düzeltmesi önce kapalı
   (ham), sonra açık hesaplanır.

**Raporlanan.** Değer: ölçülen Δf_min (iki-ton varsa), aksi halde RBW. Ek: RBW, pencere,
N, fs, frekans hatası (Hz, ppm) düzeltme öncesi/sonrası, kullanılan ppm. Şekiller: ayrım
eşiğindeki iki-ton spektrum ekran görüntüsü; çukur derinliği–ayrım eğrisi.

**Tuzaklar.** Ortalama çözünürlüğü artırmaz (yalnızca varyansı düşürür); pencere seçimi
RBW–yan lob dengesini belirler (rapor pencereyi yazar); ppm hatası çözünürlüğü değil
**doğruluğu** bozar — iki ayrı sayı olarak raporlanır; ısınma kayması.

**Örnek KTR cümlesi.**
> “fs = 4 MS/s, N = 4096, Blackman-Harris pencerede RBW 1.96 kHz'dir; eşit güçlü iki ton
> **‹…› Hz** ayrımda ≥ 3 dB çukurla ayrıştırılmıştır. 935.2677 MHz referansında frekans hatası
> PPM düzeltmesi öncesi ‹…› kHz (‹…› ppm), sonrası ‹…› Hz (‹…› ppm)'dir.”

---

## 6. DF RMS hatası — `df_rms` [°]

**Tanım (jüri).** Bilinen M açıda (her biri K tekrar), faz kalibrasyonu ve koherens
kontrolünden **sonra**:

```text
RMS = sqrt( (1/M) · Σ wrap(θ̂_i − θ_i)² )        wrap: (−180°, 180°]
İnterferometre: Δφ = 2π·d·sin(θ)/λ + φ_cal  ⇒  θ̂ = asin( λ·(Δφ − φ_cal) / (2π·d) )
```

Ayrıca **sapma** (ortalama hata) ve **en büyük |hata|** raporlanır. Belirsizliksiz bölge
`d ≤ λ/2`; 0.15 m ⇒ 1 GHz'e kadar.

**Tezgah.** HARDWARE §7.2 (kalibrasyon: bölücü + eşit kablolar, ortak 40 MHz referans) ve §7.3
(havadan: λ/2 aralıklı iki anten, uzak alanda bilinen açılarda verici; döner tabla veya ölçülü
konumlar; açık alan/emici). Simülatör: `config/dual_sim.toml` (gerçek açı birebir bilinir).

**Prosedür (`eds measure df_rms`).**

1. İki kanal açılır (`DeviceGroup` veya koherent çok kanallı cihaz); AGC kapalı, kazançlar eşit.
2. **Koherens kontrolü** (`check_coherence`): faz kayması (°/s) ve koherens eşiği
   sağlanmazsa ölçüm **koşulsuz sayı üretmez** — neden raporlanır (ör. “ortak referans yok”).
3. **Kalibrasyon** (her retune sonrası): kablolu referansla gecikme (`estimate_delay`,
   çapraz korelasyon) ve faz ofseti `φ_cal` (`phase_offset`).
4. Açı taraması (ör. −60°…+60°): her açıda K kez `Interferometer2.bearing()` →
   `θ̂`; hata `wrap(θ̂ − θ)`; SNR ve koherens kaydedilir.
5. Overflow (hizalama kaybı) görülürse 3. adım tekrarlanır.

**Raporlanan.** Değer: RMS (°). Ek: sapma, max |hata|, frekans, `d/λ`, SNR, koherens metrikleri,
kalibrasyon yöntemi. Tablo: açı başına gerçek/kestirim/hata. Şekiller: kestirim–gerçek
(ideal doğru ile), hata–açı, polar gösterim.

**Tuzaklar.** Ortak referans yoksa faz sürekli döner; her retune'da LO fazı rastgele →
kalibrasyon; örnek kayması → çapraz korelasyon; çok yollu yayılım ve anten karşılıklı
kuplajı; kablo boyu/fazı farkı (aynı frekansta kalibrasyon yok eder); ±90°'ye yakın açılarda
hata `1/cos θ` ile büyür → tarama ±60° ile sınırlanır ve rapor aralığı yazar; `d > λ/2` ise
belirsizlik; DC/imaj → dar bant faz çıkarımı DC'den uzak binde.

**Örnek KTR cümlesi.**
> “İki elemanlı interferometre (d = 0.15 m = λ/2 @ 1 GHz, ortak 40 MHz referans, her retune
> sonrası bölücü ile faz kalibrasyonu) ile 1.000 GHz'te −60°…+60° aralığında 10° adımla
> (açı başına ‹K› ölçüm, SNR ‹…› dB) ölçülen yön bulma RMS hatası **‹…›°**, sapma ‹…›°,
> en büyük hata ‹…›°'dir.”

---

## 7. İmaj bastırma — `image_rejection` [dB]

**Tanım (jüri).** `fc + δ`'daki tek tona karşı `fc − δ`'da oluşan imajın bastırılması:

```text
IRR(fc, δ) = P(fc + δ) − P(fc − δ)    [dB]
Kuram: IRR = 10·log10( (1 + 2g·cosφ + g²) / (1 − 2g·cosφ + g²) ),  g = 10^(ε/20)
       (ε: IQ kazanç dengesizliği dB, φ: faz dengesizliği; 0.4 dB / 2.5° ⇒ ≈ 30 dB)
```

Bant boyunca birkaç `fc` (ör. 400 MHz, 1 GHz, 2.4 GHz, 3.5 GHz) ve birkaç `δ` (±0.1…0.4·fs);
başlık değeri **en kötü** (en küçük) IRR, **quadrature tracking açık** (operasyonel durum);
tracking kapalı değerler ayrıca.

**Tezgah.** §7.1.

**Prosedür (`eds measure image_rejection`).**

1. Tracking durumu `set_tracking(quadrature=...)` ile ayarlanır; tracking açıldıktan sonra
   yakınsama için beklenir.
2. Her `fc` ve `δ` için ton `fc + δ`'ya verilir; `+δ` ve `−δ`'daki güç **bant gücü** ile
   ölçülür; işaret değiştirilerek tekrarlanır.
3. İmaj gürültü tabanındaysa IRR **alt sınır** (“≥ ‹…› dB”) olarak işaretlenir (ton gücü
   kırpmasız en yükseğe yaklaştırılarak sınır yükseltilir).

**Raporlanan.** Değer: en kötü IRR (tracking açık). Ek: tracking kapalı en kötü IRR, iyileşme,
`fc × δ` tablosu, alt sınır işaretleri. Şekiller: IRR–ofset (her fc, iki tracking durumu),
ton + imaj işaretli spektrum ekran görüntüsü.

**Tuzaklar.** Kaynağın kendi imajı: Pluto TX'in IQ dengesizliği de imaj üretir — TX LO tonun
`tone_offset_hz` altında olduğundan TX imajı `ton − 2·tone_offset`'te oluşur, RX imaj
frekansına (`fc − δ`) denk getirilmez; DC (`δ` küçükse DC çentiğine yakın); imajın gürültüde
kalması (alt sınır); tracking yakınsama süresi.

**Örnek KTR cümlesi.**
> “Quadrature tracking açıkken 400 MHz–3.5 GHz aralığında (4 merkez frekansı, ±0.1…0.4·fs
> ofset) ölçülen en kötü imaj bastırma **‹…› dB**'dir; tracking kapalıyken ‹…› dB'ye düşer.”

---

## 8. Taşma-sız maksimum örnekleme hızı — `max_rate` [MS/s]

**Tanım (jüri).**

```text
fs_max = max { fs ∈ aday hızlar :  T saniyelik SÜREKLİ akışta
               overflow olayı = 0  VE  teslim edilen örnek = fs·T (kayıp yok) }
Veri hızı = fs · 4 B  [MB/s]   (CS16; USB 2.0 teorik bulk 53.248 MB/s)
```

İki mod: **yalnızca taşıma** (oku-at: USB + libiio + host) ve **DSP hattı açık** (akış +
Welch + CFAR + bus yayını; sistem düzeyi değer). KTR'de sistem düzeyi değer kullanılmalıdır;
rapor ikisini de verir.

**Tezgah.** DUT tek başına (anten/sonlandırıcı). Host modeli, USB denetleyicisi, port, hub,
kablo, `buffer_size`, `usb_direct` rapora yazılır.

**Prosedür (`eds measure max_rate`).**

1. Aday hızlar `profile.candidate_sample_rates_hz` (AD9363: 0.521…61.44 MS/s) artan sırada.
2. Her hızda: yapılandır, settle, T saniye (normal ≥ 10 s; `--quick` kısa) sürekli akış;
   sayaçlar: `overflows`, `timeouts`, `discarded_samples`, `host_drops`.
3. **Örnek sayımı çapraz kontrolü:** teslim edilen örnek sayısı `fs × geçen süre` ile
   karşılaştırılır (bazı sürücüler overflow'u geç/hiç bildirmez); donanım zaman damgası
   varsa ileri sıçramalar da boşluk sayılır.
4. Geçen en yüksek hız (ve altındaki hızların da geçmesi) `fs_max`'tır; tekrarlarla doğrulanır.

**Raporlanan.** Değer: `fs_max` (MS/s). Ek: MB/s, USB2 teorik/pratik sınıra oranı, iki modun
değerleri, hız başına sayaçlar. Şekiller: overflow sayısı ve MB/s – fs (USB2 sınır çizgileri).

**Tuzaklar.** Kısa T aralıklı overflow'u gizler; paylaşılan USB denetleyicisi (2 Pluto) toplam
yük; dizüstü güç tasarrufu/CPU ölçekleme; sanal makine/WSL USB geçişi güvenilmez; Python GIL
ve tüketici yavaşlığı **host drop** üretir (taşıma değil) — rapor ayrı sayar.

**Örnek KTR cümlesi.**
> “ADALM-Pluto + ‹host› (USB 2.0, ayrı kök port) ile 60 s sürekli akışta sıfır overflow ve sıfır
> örnek kaybıyla sürdürülebilen en yüksek örnekleme hızı yalnızca taşımada ‹…› MS/s
> (‹…› MB/s), tam DSP hattı açıkken **‹…› MS/s**'dir (USB 2.0 teorik sınırın %‹…›'i).”

---

## 9. Uçtan uca gecikme — `latency` [ms]

**Tanım (jüri).**

```text
L_i = t_teslim,i − t_RF,i
  t_RF     : RF olayının alıcı girişinde başladığı host duvar saati anı
             (kaynak set_enabled(True) dönüş anı; simülatörde sahne geçiş anı birebir)
  t_teslim : olayı içeren ilk rx.detections mesajının ZMQ abonesine ulaştığı an (t_recv_ns)
İstatistik: p50, p95, p99, max   (N olay)
```

Döküm: **edinim** (blok dolumu = `block_size/fs` + FIFO/USB), **kuyruk**, **DSP** (Welch +
CFAR), **serileştirme + ZMQ**. Zaman damgaları: `IQBlock.t_ns`, mesaj `t_ns`, `t_recv_ns`.
Tüm zaman damgaları aynı host'ta `time.time_ns()` (duvar saati); süreler `perf_counter`.

**Tezgah.** §7.1; RX düğümü (`eds.rx.node.RxNode`) yayın yapar, ölçüm bir abone açar.

**Prosedür (`eds measure latency`).**

1. RX düğümü ve abone çalıştırılır; abone bağlantısı ısınır (ZMQ “slow joiner”).
2. N kez: rastgele bekleme (blok sınırlarıyla korelasyonu kırmak için) → kaynak açılır
   (`t_RF`) → tonu içeren ilk tespit beklenir (`t_teslim`) → kaynak kapatılır → tespitin
   kaybolması beklenir.
3. İstatistikler ve döküm hesaplanır; zaman aşımına uğrayan olaylar ayrıca sayılır.

**Raporlanan.** Değer: önerilen başlık **p95** (rapor p50/p95/p99/max hepsini verir). Ek: döküm
(ms), olay sayısı, zaman aşımları, `block_size`, fs, `spectrum_rate_hz`. Şekiller: gecikme
histogramı/CDF, döküm çubuğu.

**Tuzaklar.** Kuramsal alt sınır blok süresidir (65536 / 4 MS/s = 16.4 ms; ortalama ≈ 1.5 blok
+ işleme) → `block_size` gecikme–verim dengesi; donanımda kaynağın kendi gecikmesi (Pluto TX
tamponu `buffer_len/fs`, SCPI üreteç ms mertebesi) ölçüme biner → rapor kaynak türünü yazar;
`spectrum_rate_hz` kısıtı; ZMQ HWM aşımı mesaj düşürür; Python GC duraklamaları p99'u büyütür;
saat alanlarını karıştırmak (duvar saati vs monotonik) yanlış sonuç verir — EDS bus'ta yalnız
duvar saati kullanır.

**Örnek KTR cümlesi.**
> “4 MS/s, 65536 örneklik blok ve 10 Hz spektrum hızında, RF olayından tespit mesajının ZMQ
> abonesine teslimine kadar ölçülen uçtan uca gecikme ‹N› olayda p50 ‹…› ms, **p95 ‹…› ms**,
> p99 ‹…› ms, en kötü ‹…› ms'dir (edinim ‹…› ms, işleme ‹…› ms, iletim ‹…› ms).”

---

## 10. KTR tablosu şablonu

Raporun özet tablosu bu biçimdedir; **her satırda koşullar** yazılıdır:

| Spesifikasyon | Ölçülen | Hedef (KTR) | Durum | Koşullar | Yöntem |
|---|---|---|---|---|---|
| Duyarlılık | ‹…› dBm | ≤ −100 dBm | ✓/✗ | 935 MHz, RBW 1.96 kHz, Pfa 1e-6, Pd 0.9, G = 73 dB | CW adımlama, CFAR |
| Dinamik alan | ‹…› dB | ≥ 60 dB | | G = 50 dB, RBW 1.96 kHz | kırpma + taban |
| Anlık BW | ‹…› MHz | ≥ 18 MHz | | fs 20 MS/s, BW 20 MHz | ton adımlama, −3 dB |
| Tarama hızı | ‹…› GHz/s | ≥ 5 GHz/s | | 325 MHz–3.8 GHz, RBW 19.6 kHz | duvar saati |
| Frekans çözünürlüğü | ‹…› Hz | ≤ 10 kHz | | fs 4 MS/s, N 4096, BH | iki-ton ≥3 dB çukur |
| DF RMS | ‹…›° | ≤ 5° | | 1 GHz, d = λ/2, ±60° | interferometre |
| İmaj bastırma | ‹…› dB | ≥ 40 dB | | 4 fc, ±0.1…0.4 fs, tracking açık | P(+δ) − P(−δ) |
| Maks. örnekleme hızı | ‹…› MS/s | ≥ 4 MS/s | | 60 s, DSP açık | overflow + sayım |
| Gecikme (p95) | ‹…› ms | ≤ 100 ms | | 4 MS/s, blok 65536 | RF→ZMQ |

Hedef değerler `config/ktr_targets.toml` dosyasındadır ve **örnektir** — takımın KTR'sindeki
sayılarla değiştirilmelidir. Hedefin taahhüt edildiği koşullar ile ölçüm koşulları aynı olmalıdır.
