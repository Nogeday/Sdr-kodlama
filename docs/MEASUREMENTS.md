# KTR Nicel Spesifikasyon Ölçümleri — Tanımlar, Tezgah, Prosedür, Rapor

> EDS aynı zamanda bir **ölçüm cihazıdır**: KTR'de istenen 9 nicel spesifikasyonu kendisi
> ölçer, koşullarıyla birlikte tablo + grafik + spektrum “ekran görüntüsü” olarak dışa aktarır.
> Jürinin en sık puan kırdığı nokta **koşulsuz sayı** ve **eksik nicel spec**tir; bu belge her
> ölçümün jüriye savunulabilir tanımını ve EDS'in onu **tam olarak nasıl** ürettiğini verir
> (her bölüm `src/eds/measure/specs/<spec_id>.py` ile birebir eşleşir; parametre adları ve
> varsayılanları koddan alınmıştır).
>
> Kod: `eds.measure` (çerçeve `measure/base.py`, ortak yardımcılar `measure/common.py` ve
> `measure/stimulus.py`, ölçümler `measure/specs/<spec_id>.py`, oturum `measure/session.py`,
> hedefler `measure/targets.py`, rapor `measure/report.py`). Donanım:
> [`HARDWARE_PLUTO.md`](HARDWARE_PLUTO.md). Konvansiyonlar: [`ARCHITECTURE.md`](ARCHITECTURE.md) §2.

| # | `spec_id` | Başlık (rapor) | Birim | “İyi” yönü | Gereksinim |
|---|---|---|---|---|---|
| 1 | `sensitivity` | Duyarlılık (MDS) — Pd = 0.9 + DANL/NF/MDS | dBm (kalibrasyonsuz: dBFS) | küçük | – (kaynaksız: MDS formülü) |
| 2 | `dynamic_range` | Dinamik alan — anlık + SFDR + toplam | dB | büyük | – (kaynaksız: varsayımlı) |
| 3 | `inst_bw` | Anlık bant genişliği (−3 dB, −1 dB) | MHz | büyük | – (kaynaksız: gürültü şekli) |
| 4 | `scan_speed` | Tarama hızı + adım dökümü + yeniden ziyaret | GHz/s | büyük | – |
| 5 | `freq_resolution` | Frekans çözünürlüğü — RBW + iki-ton + doğruluk | Hz | küçük | – (kaynaksız: RBW) |
| 6 | `df_rms` | DF RMS hatası | ° | küçük | 2 RX kanalı |
| 7 | `image_rejection` | İmaj bastırma (tracking açık/kapalı) | dB | büyük | kaynak |
| 8 | `max_rate` | Taşmasız maksimum örnekleme hızı | MS/s | büyük | – |
| 9 | `latency` | Uçtan uca gecikme (RF olayı → ZMQ teslimi) | ms | küçük | – (kaynaksız: yalnız Mod A, karar yok) |

---

## 0. Ortak esaslar

### 0.1 Çalıştırma

```bash
eds measure all              -c config/rx_sim.toml --quick   # donanımsız, hızlı (CI/demo; KTR'de kullanılmaz)
eds measure sensitivity      -c config/rx_pluto.toml         # tek ölçüm, gerçek Pluto (+ pluto_tx uyaran)
eds measure df_rms           -c config/dual_pluto.toml       # 2 Pluto DF (her Pluto kendi alt sürecinde)
eds measure df_rms           -c config/dual_sim.toml         # DF prosedürü simülatörde
```

* Parametre önceliği: ölçümün `default_params` < `[measure.params.<spec_id>]` (konfigürasyon,
  ör. `[measure.params.df_rms] baseline_m = 0.15`) < çağrıdaki parametreler. Hızlı modda
  (`--quick`) `quick_<anahtar>` varsa `<anahtar>` yerine o kullanılır.
* Ölçümler birbirinden **yalıtılır**: her ölçümden önce cihazlar akışı durdurup `[rx]`
  başlangıç ayarına döner ve kaynak kapatılır (`session.reset_baseline`; her adım denenir, ilk
  hata o ölçümü `failed` yapar) — bir ölçümün hız/kazanç/tracking değişikliği diğerine sızmaz.
* Gereksinimi karşılanmayan ölçüm **atlanır** (`status = "skipped"`, Türkçe neden: “Sinyal
  kaynağı gerekli”, “En az 2 RX kanalı gerekli”); hata veren ölçüm `failed` olur ve raporda iz
  (traceback) bulunur. Ctrl-C: koşan ölçüm `failed`, kalanlar `skipped`, tamamlananlarla
  “Ölçüm oturumu: YARIDA KESİLDİ” başlıklı kısmi rapor yazılır. Hiçbir ölçüm sessizce yok olmaz.
* Çıkış kodu: 0 = başarısız ölçüm yok (atlanan sayılmaz), 2 = en az bir ölçüm başarısız,
  1 = konfigürasyon hatası.

### 0.2 Seviye ve spektrum konvansiyonları (ARCHITECTURE §2)

```text
0 dBFS      = tam ölçek genlikli kompleks üstelin gücü: mean(|x|²) = 1     (I = counts/2048)
Güç spektrumu P[k] = |Σ w·x·e^{-j2πkn/N}|² / (Σw)²     → bin merkezindeki ton gücünü doğru okur
RBW         = ENBW_bins · fs / N ,  ENBW_bins = N·Σw² / (Σw)²   (Blackman-Harris: 2.004 bin)
Bant gücü   = Σ P[k] / ENBW_bins                         → scalloping'den bağımsız ton/kanal gücü
Yoğunluk    = P − 10·log10(RBW)   [dB/Hz]
dBm         = dBFS + offset(f, G)  (offset ≈ C(f) − G)
NF          = DANL[dBm/Hz] − (−173.975)                  (kT @ 290 K)
```

Örnekler (Blackman-Harris): fs = 4 MS/s, N = 4096 ⇒ RBW = **1.957 kHz** (`[dsp]` varsayılanı);
fs = 2 MS/s, N = 4096 ya da fs = 4 MS/s, N = 8192 ⇒ RBW = **978.7 Hz** (duyarlılık / dinamik
alan / frekans çözünürlüğü varsayılanları).

Ton gücü her yerde **gürültüsü düşülmüş bant gücüdür**: tonun (ölçülen görünen frekansındaki)
tepe binin ±5 bini (`TONE_HALFWIDTH_BINS`; BH ana lobu ±4 bin) toplanır, aynı bin sayısının
gürültü ortalaması çıkarılır, `ENBW_bins`'e bölünür. Gürültü ortalaması, kullanılabilir bandın
(`dsp.usable_fraction`, DC ± `dsp.dc_notch_bins` hariç) **Gamma-medyan düzeltmeli** medyanıdır
(`n_eff` ortalamalı periodogram binleri `Gamma(n_eff)` dağılır, medyan ortalamanın
`gamma_quantile(0.5, n_eff)/n_eff` katı olduğundan düzeltme yansızlaştırır).

### 0.3 dBFS → dBm (seviye referansı)

`eds.measure.stimulus.reference_level` / `common.level_reference` sırası:

1. **Kalibrasyon tablosu** (`cal/<seri>.json`, `PowerCalTable.offset_db(f, G)`) → `dBm`,
   `calibrated = true`.
2. **Mutlak kaynak** (sim, SCPI üreteç, manuel mutlak, `anchor_dbm`'li Pluto TX) → yerinde
   (in-situ) referans: bilinen güçte ton (vars. −25 dBFS civarı, `fc + 0.125·fs`) enjekte
   edilir, `offset = P_in − P_ölçülen(dBFS)`; aynı yakalamadan kaynak–alıcı referans farkı
   (`e`) de ölçülür ve sonraki tonlar görünen frekanslarına `f·(1 + e)` ile yerleştirilir.
3. Hiçbiri yoksa sonuç **dBFS** kalır, `calibrated = false`. Bu sonuç jüriye **dBm diye
   sunulmaz**; dBm hedefiyle karşılaştırılmaz (§0.7).

**Seviye belirsizliği** (`stimulus.level_uncertainty_budget`, ≈ %95, dB; açıklaması sonuca yazılır):

| Durum | Belirsizlik |
|---|---|
| `level_uncertainty_db` parametresi verilmiş | o değer |
| kalibrasyonsuz | yok (“mutlak seviye yok”) |
| kalibrasyon tablosu | ±1.0 dB (varsayılan) |
| simülatör kaynağı | 0 (tanım gereği kesin) |
| çapalı bağıl kaynak (Pluto TX + `anchor_dbm`) | `1.0 dB ⊕ 0.02 dB/dB × |P_bağıl|` (çapa ±1 dB + zayıflatıcı/sayısal ölçek ekstrapolasyonu; 90 dB altta ±1.8 dB; kutu sızıntısı hariç). Ayar bilinmiyorsa “bilinmiyor”. |
| diğer mutlak üreteç (SCPI, manuel) | ±1.0 dB (varsayılan) |

### 0.4 Uyaran kaynakları (`[measure] source`, `--source`)

| `source` | Sınıf | Güç | Not |
|---|---|---|---|
| `sim` | `SimSignalSource` | mutlak | Simülatör; gerçek değer (ground truth) bilinir → prosedür doğrulaması. RF KAPALI başlar. |
| `pluto_tx` | `ProcessSignalSource` → `SoapyTxSource` | bağıl; `anchor_dbm` ile mutlak | 2. Pluto, **kendi alt sürecinde** (`source_args.isolate = true` varsayılan; ARCHITECTURE §5). Tek ton: TX LO tonun `tone_offset_hz` (200 kHz) altında; iki ton: LO orta noktada. `set_lo_offset` ile LO ölçüm başına taşınır (§0.6). |
| `scpi` | `ScpiSignalGenerator` | mutlak | LAN üzerinden laboratuvar üreteci (TCP 5025, `*IDN?`, `:FREQ`, `:POW`, `:OUTP`, `*OPC?`, geri okuma); en doğrusu, IRR/SFDR için önerilir. |
| `manual` | `ManualSource` | mutlak/bağıl | Operatör üreteci elle ayarlar; her adımda Enter bekler (`--yes` onayları geçer). |
| `none` | – | – | `image_rejection` atlanır; diğerleri kaynaksız yedek yöntemi kullanır (yöntem metninde yazar). |

**Uyaran zamanlaması** (`common.stimulus_changed`): her uyaran değişikliğinden (aç/kapa,
frekans, seviye, tonlar) sonra `source_settle_s` kadar duvar saati beklenir, sonra tüm
cihazlarda `mark_settling()`: atılan `settle_samples` tüm RX FIFO'sunu kapsadığından her
yakalanan örnek beklemeden sonradır. `source_settle_s` = kaynağın `settle_s`'i, yoksa
`estimated_queue_latency_s × 1.25 + 5 ms` (Pluto TX: `estimated_queue_latency_s = 4 × buffer_len / fs`, 4 libiio TX çekirdek tamponu;
`rx_pluto.toml` kaynağında 8192 / 1 MS/s → 32.8 ms → 46 ms bekleme),
yoksa SCPI için 10 ms, sim/manuel için 0. Beklenen süre `conditions`'a yazılır
(`source_conditions`).

**Uyaran tavanı:** prosedürler RX girişini `profile.rx_max_input_dbm`'in (AD9363:
**−5 dBm**; mutlak maksimum ≈ +2.5 dBm, pay + düşük kazançta sıkışma) üstüne sürmez:
`stimulus.source_power_range(src, dev)` mutlak kaynağın ayarını, `relative_setting_cap` bağıl
kaynağın ayarını doğrusal bir okumadan ekstrapole ederek sınırlar; `adjust_tone` ikisini de
uygular (sensitivity, dynamic_range, inst_bw, freq_resolution, image_rejection), `df_rms`
seviyelerini tavanla kırpar, `freq_resolution` iki-ton testinde ton başına ayarı
`min(tek ton ayarı, tavan − 3 dB)` yapar (iki eşit tonun toplamı +3 dB), `latency`'nin
uyarlamalı ton seviyesi aynı iki sınırı kullanır (bağıl kaynakta tavan, düğümün yayınladığı ton
bant gücünden her okumada yeniden ekstrapole edilir). Tavan ya da kaynağın üst gücü bir aramayı
durdurursa sonuç **sınır** (≥) olarak işaretlenir. Görünmeyen tona +20 dB adım okumasızdır
(yalnız kaynak aralığı ve mutlak kaynakta tavanla sınırlı) — TX→RX yolundaki **≥ 30 dB sabit
zayıflatıcı** (HARDWARE §8) bu yüzden her durumda zorunludur.

### 0.5 Raporlanan her sonuçta

`MeasurementResult`: **değer + birim**, Türkçe tek satır **özet**, **yöntem** metni,
**koşullar** (backend, seri no, profil, fc, fs, BW, kazanç, kazanç modu, `ppm_hw`, sıcaklık,
nfft, RBW, ortalama, pencere, kaynak + bekleme süresi + ölçüme özgü; her sonuçta
`quick` ve `simulated`), **belirsizlik**, ham **tablo** satırları, **şekiller** (`FigureSpec`
→ 1280×720 PNG; `kind = "spectrum"` spektrum analizörü görünümlü ekran görüntüsü),
**hedef** karşılaştırması (`config/ktr_targets.toml` → `passed`), `blockers` (karar
engelleri) ve `extra["value_bound"]` (`"lower"` = değer alt sınır ≥, `"upper"` = üst sınır ≤).
Sonuçlar bus'ta `meas.result` olarak yayımlanabilir (`--publish`). Rapor klasörü varsayılan
olarak `reports/<zaman>/` (`[measure] out_dir`, `--out`): `report.html`, `summary.md`,
`summary.csv`, `results.json`, `tables/*.csv`, `figures/*.png`.

### 0.6 Uyaranın kendi artefaktları (Pluto TX)

Tonu bir LO etrafında sayısal sentezleyen kaynak (Pluto/HackRF TX) **LO sızıntısı** (`LO`'da)
ve **TX IQ imajı** (`2·LO − f`) üretir. `image_rejection` ve `dynamic_range` (SFDR) her
noktada `stimulus.try_lo_offset(src, hz)` ile kaynağın LO'sunu tonun **ötesine** (fc'den
uzağa) yerleştirir; aday ofsetler RX bandına katlanmış artefaktların ölçülen binlerden
(ton, RX imajı) en az `halfwidth + 2` bin uzak kalmasını sağlar. Gerçekleşen ofset ve
temizlik her noktada kaydedilir, sonunda özgün ofset geri yüklenir. `set_lo_offset`
desteklemeyen kaynak (`NotSupported`: sim, SCPI, manuel) için ofset `None`'dır (artefakt yok).

### 0.7 Hedef kararı ve “koşulsuz sayı” kuralı (`measure/targets.py`)

`config/ktr_targets.toml`: `[<spec_id>] target`, `op` (`"<="` | `">="`), `note` ve isteğe
bağlı `[<spec_id>.require]` (`<koşul>_min`, `<koşul>_max` (liste koşulunda en kötü öğe),
`<koşul>_count_min`, diğerleri eşitlik; anahtar önce `conditions`'ta, yoksa `extra`'da aranır;
ör. `rbw_hz_max = 2000`, `headline_mode = "pipeline"`). Dosyada her ölçüm için yorum satırı
olarak örnek taahhüt koşulları vardır (`scan_speed`: `span_hz_min` / `rbw_hz_max`, `max_rate`:
`headline_mode` / `duration_s_min`, `image_rejection`: `fcs_hz_count_min`, `sensitivity`:
`rbw_hz_max`). Hedef dosyası tek doğruluk kaynağıdır; bilinmeyen anahtar/işlem hatadır.

Bir sonuç hedefini korur ama **karar verilmez** (`passed = None`, rapor
**KARŞILAŞTIRILMADI** + Türkçe neden, `extra["verdict_note"]`) eğer:

* ölçüm `blockers` listeledi (ör. gerçek zamanlı olmayan simülatör, yalnız taşıma modu,
  koherens yok, simülatörde DF, Mod A gecikmesi, imaj tracking'i doğrulanamadı),
* **hızlı mod** (`conditions["quick"]`) — rapor ayrıca “HIZLI MOD - KTR'de kullanılmaz”
  bandı ve her şekilde etiket taşır,
* `extra["realtime"] is False` ya da `extra["coherent"] is False`,
* birim spesifikasyonunkinden farklı (ör. kalibrasyonsuz `dBFS` duyarlılık) ya da dBm hedefi
  kalibrasyonsuz değerle karşılaşıyor,
* bir `require` koşulu sağlanmıyor ya da sonuçta kayıtlı değil,
* değer bir **sınırdır** (özette “≥”/“≤”, `extra["value_bound"]` = `"lower"`/`"upper"`) ve
  sınır kararı belirlemiyor (`targets.bound_reason`; neden: “değer alt sınırdır (≥ X) ve
  hedefin altında — sonuçsuz” vb.):

  | Sınır | `op` | Karar yok (sonuçsuz) | Aksi halde |
  |---|---|---|---|
  | alt (gerçek ≥ X) | `>=` | X < hedef | GEÇTİ (X ≥ hedef) |
  | alt (gerçek ≥ X) | `<=` | X ≤ hedef | KALDI (X > hedef) |
  | üst (gerçek ≤ X) | `<=` | X > hedef | GEÇTİ (X ≤ hedef) |
  | üst (gerçek ≤ X) | `>=` | X ≥ hedef | KALDI (X < hedef) |

  Kararı belirleyen sınır normal karşılaştırılır (ör. `inst_bw` ≥ 19.5 MHz, hedef ≥ 18 → GEÇTİ).

Simülatör sonuçları raporda “SİMÜLASYON VERİSİ” bandı ve şekil etiketiyle işaretlenir;
`ad9364_pluto_ext` profiliyle alınan sonuçlar “SPESİFİKASYON DIŞI MOD” bandı taşır.

### 0.8 Genel tuzaklar ve kodun önlemleri

| Tuzak | Etki | EDS önlemi |
|---|---|---|
| Retune sonrası bayat tamponlar | Yanlış frekans/kazançta veri | `capture()` settling örneklerini atar (`settle_buffers = kernel_buffers + 1`, gerçek sürücü tamponuna göre). |
| Uyaran gecikmesi (Pluto TX kuyruğu) | Yakalama eski uyaranı görür | `stimulus_changed`: `source_settle_s` bekle + settling işaretle (§0.4). |
| Aşırı giriş | LNA hasarı / sıkışma | `rx_max_input_dbm` tavanı (§0.4); ≥ 30 dB sabit zayıflatıcı (HARDWARE §8). |
| Kırpma / sıkışma | Sahte harmonik/IMD, yanlış güç | `iq_levels` (`clip_fraction`, kanal başına tepe); dinamik alanda 1 dB sıkışma ölçütü. |
| HW AGC açık | Örnekle senkron kazanç yok | Ölçümler `gain_mode = "manual"` kurar. |
| Sıfır-IF DC çivisi | Tonun üstüne DC biner | Tonlar DC'den uzak ofsette; `dc_notch_bins`. |
| Scalloping | Tepe bin ton gücünü ≤ 0.83 dB (BH, yarım bin) eksik okur | Ton gücü **bant gücü** ile; duyarlılıkta ton bin içinde gezdirilir. |
| PPM hatası | Frekans/bin kayması | Yerinde `e` ölçümü + `f·(1+e)` yerleşimi; kalibrasyon (`cal.ppm_mode`); koşullara `ppm_hw`. |
| Uyaran artefaktları | Sahte imaj/spur | LO yerleşimi + maskeleme (§0.6). |
| Isınma / sıcaklık | Frekans ve kazanç kayar | 10–15 dk ısınma; sıcaklık her sonuçta; kalibrasyondan 5 °C sapma uyarısı. |
| Kutu sızıntısı (Pluto→Pluto) | Kablolu seviyeden büyük sızıntı | Tezgah kontrolü: TX kablosu sonlandırılınca ton kaybolmalı (HARDWARE §7.1). |
| Overflow | Bloklar arası boşluk | `read()` asla iç boşluklu blok döndürmez; Pluto'da DMA durum yazmacı (ARCHITECTURE §6); `capture(allow_overflow=False)` yeniden dener. |

---

## 1. Duyarlılık — `sensitivity` [dBm]

**Tanım (jüri).** Belirtilen RBW, ortalama sayısı ve CFAR yanlış alarm olasılığında (Pfa),
`[detect]` ayarlı **gerçek dedektörün** tespit olasılığını **Pd = 0.9** yapan en küçük giriş
gücü:

```text
P_sens = min { P_in : Pd(P_in) ≥ 0.9 }     @ Pfa = detect.pfa (vars. 1e-6), RBW, n_avg
DANL [dBm/Hz] = N_RBW [dBm] − 10·log10(RBW)
MDS(B) [dBm]  = DANL + 10·log10(B) + SNR_min        (B ∈ mds_bandwidths: RBW, 25 kHz, 200 kHz, 1 MHz)
NF   [dB]     = DANL + 173.975                      (yalnızca kalibreli)
```

`SNR_min` (verilmezse `snr_req_db`): bin merkezli bir tonun dedektör eşiğini `α·N` Pd = 0.9
ile aştığı bin başına SNR. `α` dedektörün **gerçek** çarpanıdır (OS/CA-CFAR sonlu-M
düzeltmesi, `detect.pfa`'nın geçiş başına Bonferroni payında, `min_snr_db` tabanıyla);
ton bini `2·n_eff` serbestlik dereceli merkezi olmayan χ²'dir (`n_eff` = Welch etkin
ortalama), Patnaik (moment eşleşmeli Gamma) yaklaşımıyla çözülür.

**Tezgah.** HARDWARE §7.1: kaynak → ≥30 dB sabit + kademeli zayıflatıcı → DUT RX. Seviye
belirsizliğini azaltmak için üreteci doğru çalıştığı aralıkta tutun, kalanı sabit
zayıflatıcılarla sağlayın. Kutu sızıntısı kontrolü zorunlu.

**Prosedür (`eds measure sensitivity`).** Varsayılanlar: `fc` = `[rx] center_freq_hz`,
`fs = 2 MS/s`, `bw = fs`, **bandın en yüksek kazancı** (manual), tracking açık, `nfft = 4096`,
`averages = 8` (RBW 978.7 Hz).

1. Seviye referansı (§0.3) bu kazançta.
2. **Bölüm A — gürültü:** giriş sonlandırılır (`ctx.terminated_input()`; simülatörde tüm
   emitter'lar kapatılır, donanımda operatöre 50 Ω takması söylenir) → `noise_averages`
   (64; hızlı 32) ortalamalı spektrum → düz merkez bölgede (±`danl_fraction`/2·fs = ±0.25·fs,
   DC çentiği hariç) Gamma-medyan düzeltmeli `N_RBW` → DANL, NF, MDS(B). Aynı girişte
   `fa_trials` (40; hızlı 20) dedektör çalıştırması → **ampirik Pfa** ve tek yanlı %95 Poisson
   üst sınırı `gamma_quantile(0.95, k+1)/N_bin`.
3. **Bölüm B — Pd eğrisi** (kaynak varsa): ton `fc + offset_frac·fs` (0.15) yakınındaki bin
   merkezine, yerinde ölçülen `e` ile `f·(1+e)`'ye konur (önce `N_RBW + 40 dB`'lik prob).
   `bin_offset = "uniform"` (vars.): seviyedeki `i`. deneme tonu bin merkezinden
   `(i + ½)/n − ½` bin kaydırır (Pd, keyfi bir emitter'ın binine düştüğü yere göre
   ortalanır; BH taraklanması 0.83 dB'ye kadar); `"centre"` en iyi durumdur; manuel kaynakta
   otomatik `"centre"`. Seviye `start_snr_db` (20 dB, bin başına) SNR'den başlar (Pd hedefe
   ulaşmazsa 3 kez +6 dB yükseltilir), `coarse_step_db` (2 dB) adımlarla `Pd < pd_stop`
   (0.2) olana kadar iner, sonra geçiş bölgesi `fine_step_db` (0.5 dB) ızgarayla doldurulur
   (en fazla `max_levels` = 40 seviye). Her seviyede `trials` (20; hızlı 10) settled yakalama
   → `eds.dsp.detect.detect` (`[detect]`, kullanılabilir bant maskesi) → tepe tonun
   ±`hit_tolerance_bins` (2) içinde olan tespit = isabet. Kaynak aralığı / RX tavanı dışındaki
   seviyeler atlanır ve not edilir.
4. **Uyum:** tüm seviyelere binom en büyük olabilirlik **probit** eğrisi
   `Pd = Φ((SNR − x)/σ + Φ⁻¹(0.9))` (σ 0.2…8 dB profillenir); `x` = sonuç; %95 aralık
   profil olabilirlikten (χ²₁; Monte Carlo ile ≈ %95 kapsama doğrulanmış).
5. Duyarlılık seviyesinde ton + CFAR eşiği ekran görüntüsü.

**Değer ve belirsizlik.** Bölüm B çalıştıysa `P_sens`, aksi halde `MDS(RBW)` (özet hangisi
olduğunu yazar: “Pd eğrisi”, “MDS formülü (kaynak yok)”, “MDS formülü (Pd eğrisi
başarısız)”). Belirsizlik = %95 aralığın büyük yarısı ⊕ seviye belirsizliği (§0.3).
`|P_sens − MDS(RBW)| > mds_warn_db` (3 dB) ise uyarı. Yarım-bin en kötü durum
(`sensitivity_worst_case`, pencerenin taraklanma kaybından) ayrıca verilir. Özet Pfa'yı
“Pfa tasarım 1e-06, ölçülen ≤ x (%95, N bin)” biçiminde yazar.

**Raporlanan.** Ek: DANL (birim/Hz), NF, N_RBW, MDS(B) (`mds_<birim>_by_bw_hz` = {B [Hz]: MDS}), `snr_req_db` ve kaynağı,
`cfar_alpha_db`, `n_eff`, `pfa_pass`, ampirik Pfa ve üst sınırı, `snr_ci95_db`,
`sensitivity_ci95`, `pd_fit_sigma_db`, `pd_fit_deviance`, deneme sayıları, `in_situ_ppm`,
taraklanma kaybı, seviye belirsizliği bütçesi. Koşullar: dedektör türü/Pfa/guard/train/
os_rank/min_snr/resolutions, Pd hedefi, deneme, ton yerleşimi, seviye referansı.
Şekiller: Pd–P_in (ölçülen + Wilson çubukları + probit eğrisi, 0.9 ve %95 sınırları),
duyarlılık seviyesinde spektrum (ton + CFAR eşiği), sonlandırılmış giriş gürültü tabanı.

**Karar.** Kalibrasyonsuzsa birim `dBFS` → dBm hedefiyle karşılaştırılmaz; hızlı modda karar yok.

**Donanım notları.** Sızıntı (kablosuz yol) duyarlılığı olduğundan iyi gösterir → RF
kapalı/kablo sonlandırılmış kontrol; Pluto TX ile seviyeler çapanın 90+ dB altında →
belirsizlik ≈ ±1.8 dB (bütçe raporda); düşük seviyede üretecin kendi doğruluğu → sabit
zayıflatıcı; kazanç en yüksekte değilse NF kötüleşir.

**Örnek KTR cümlesi.**
> “EDS alıcısının duyarlılığı 935 MHz'de, RBW = 979 Hz (Blackman-Harris, N = 4096,
> fs = 2 MS/s, 8 ortalama), OS-CFAR Pfa = 10⁻⁶ (gerçekleşen ≤ ‹…›, %95) ve 73 dB kazançta,
> bin içinde düzgün dağılımlı ton konumlarıyla **Pd = 0.9 için ‹…› dBm** olarak ölçülmüştür
> (seviye başına ‹N› deneme, probit uyumu, %95 belirsizlik ±‹…› dB). Gürültü tabanından
> hesaplanan MDS ‹…› dBm, gürültü faktörü ‹…› dB'dir.”

---

## 2. Dinamik alan — `dynamic_range` [dB]

**Tanım (jüri).** Üç ayrı sayı raporlanır; başlık değeri **anlık DR**'dir. KTR'de hangisinin
kullanıldığı açıkça yazılmalıdır.

```text
DR_anlık   = P_max [dBFS] − N_RBW [dBFS]           (aynı kazanç, aynı RBW)
P_max      = min(ADC kırpması, giriş 1 dB sıkışması)   (RX tavanı/kaynak üst gücü durdurursa ≥)
SFDR [dBc] = P_taşıyıcı − P_en büyük ayrık spur      (IQ imajı DAHİL; imaj hariç değer ayrıca)
SFDR [dBFS]= 0 dBFS − P_spur
DR_toplam  = P_in,max(G_min) [dBm] − MDS_RBW(G_max) [dBm]     (kazanç yönetimli)
```

12-bit referans: ideal niceleme gürültüsü (I+Q) ≈ −74 dBFS (fs bandında); RBW'de
`−74 − 10·log10(fs/RBW)`. Gerçek taban yüksek kazançta termal, düşük kazançta ADC gürültüsüdür.

**Tezgah.** §7.1. SFDR için kaynağın harmonik/spur seviyesi DUT'tan iyi olmalı (üreteç +
alçak geçiren filtre); Pluto TX'in LO sızıntısı ve TX imajı aramadan çıkarılır (§0.6), diğer
spur'ları SFDR'yi sınırlayabilir → raporda kaynak belirtilir.

**Prosedür (`eds measure dynamic_range`).** Varsayılanlar: `fs = 4 MS/s`, `bw = fs`, sabit
`gain = 40 dB`, `nfft = 8192`, `averages = 8` (RBW 978.7 Hz).

1. **N_RBW:** sabit kazançta giriş sonlandırılır → `noise_averages` (32; hızlı 16) ortalamalı
   spektrumun düz merkez bölgesinden (±0.25·fs) Gamma-medyan düzeltmeli gürültü.
   **Gürültü–kazanç eğrisi:** `gain_curve_step_db` (10; hızlı 20) adımlarla kazanç aralığında
   N_RBW(G) → `DR_anlık(G) = P_max − N_RBW(G)`.
2. **P_max araması:** ton `max(lin_start_dbfs = −70, N_RBW + 15) dBFS`'ten `lin_step_db`
   (5; hızlı 10) adımlarla yükseltilir; dur ölçütü: yakalama kırpar (`clip_fraction > 0`) ya
   da kanal başına tepe ≥ `clip_peak_dbfs` (−0.5 dBFS), **ya da** kazanç (`ton_dBFS − ayar`)
   en düşük noktaların küçük sinyal kazancından (≥ 20 dB SNR, ilk noktanın 10 dB içi)
   `compression_db` (1 dB) düşer. Son sağlam / ilk sınırlı aralık `bisect_db`'ye (0.5 dB)
   ikiye bölünür; P1dB enterpole edilir. Kaynak RX girişini `rx_max_input_dbm`'in üstüne
   sürmez; arama bu tavanda ya da kaynağın üst gücünde durursa `P_max` alt sınırdır
   (ölçüt `RX giriş sınırı` / `kaynak üst sınırı`, özet “≥”). Adımlanan noktalar giriş→çıkış
   doğrusallık eğrisidir. Kaynak yoksa `DR_anlık = 0 dBFS − N_RBW` (tam ölçekli CW varsayımı,
   yöntemde yazar).
3. **SFDR:** ton `fc + (tone_offset_frac + tone_detune_frac)·fs` (fs/8 + 0.0107·fs; harmonik
   ve imajlar çakışmasın) ve `sfdr_level_dbfs` (−3 dBFS); kendi LO'su olan kaynakta LO tonun
   ötesine yerleştirilir. Kullanılabilir bantta, taşıyıcının yakın bölgesi
   (±max(`main_lobe_bins` = 6, `close_in_hz` = 25 kHz): LO/referans faz gürültüsü eteği,
   en güçlü yakın bin `close_in_max_dbc` olarak ayrıca) ve DC çentiği dışında, komşuluk
   medyanının (±8…40 bin) ≥ `spur_contrast_db` (6 dB) üstündeki en güçlü **ayrık çizgi** baskın
   spur'dur (IQ imajı ve dijital spur'lar dahil). Spur gücü ±5 binlik gürültüsü düşülmüş bant
   gücü; gürültünün 10 dB'den az üstündeyse ya da ayrık çizgi yoksa SFDR sınırdır (≥).
   Kaynak LO sızıntısı / TX imajı aramadan maskelenir, `source_artifacts` (dBc) olarak
   “kaynak kaynaklı” verilir; IQ imajıyla çakışırsa not düşülür.
4. **DR_toplam** (`total = true`): en düşük kazançta −40 dBFS'lik yerinde prob ve aynı
   P_max aramasıyla `P_in,max(G_min)` (dBm), en yüksek kazançta `MDS = N_RBW(G_max) + SNR_min`
   (CFAR modelinden, `pd_target` 0.9). Mutlak referans yoksa: kalibrasyon tablosu ya da seviye
   modeli (`dBFS ≈ dBm + G − 5`) tahmini, `P_in,max` RX tavanıyla kırpılı, sonuçta “tahmin”
   olarak işaretlenir.

**Değer ve belirsizlik.** Değer: `DR_anlık` (dB); belirsizlik = `bisect_db` (kaynaklı ölçümde).
`extra["value_bound"] = "lower"` P_max tavan/kaynakla sınırlandıysa.

**Raporlanan.** Ek: `p_max_dbfs`, `p_max_criterion`, `p_max_input` / `p1db_input` / `clip_input` (+ birimleri
`*_unit`: dBm ya da dBFS), `compression_at_p_max_db`,
`small_signal_gain_db`, `clip_input`, SFDR dBc/dBFS, imaj hariç SFDR, baskın spur frekansı/
imaj mı/ayrık mı, `close_in_max_dbc`, `source_artifacts`, `source_lo_offset_hz`,
`dr_total_db` (+ yöntem, alt sınır mı, `p_max_gmin_criterion`, `p1db_input_gmin_dbm`,
`mds_gmax_dbm`, `snr_req_db`, `rx_max_input_dbm`), N_RBW(G) tablosu. Şekiller: taşıyıcı +
spur'lar işaretli spektrum ekran görüntüsü; giriş–çıkış doğrusallık eğrisi (kırpma/sıkışma
işaretli); gürültü/DR–kazanç eğrisi.

**Donanım notları.** ≥30 dB sabit zayıflatıcı ile Pluto TX (+5…+7 dBm) RX girişini ≈ −23 dBm'e
taşıyabilir: en düşük kazançta kırpma/sıkışma çoğunlukla **görülmez** → `DR_toplam` “≥” (kaynak
üst sınırı) çıkar; daha yüksek seviye için tavan −5 dBm'dir. I ve Q **ayrı** kırpılır: tam ölçek
CW'nin gücü 0 dBFS, gürültü benzeri sinyalin tepe/ortalama oranı 10–12 dB → OFDM için pratik DR
daha düşüktür; HW AGC kapalı olmalı.

**Örnek KTR cümlesi.**
> “40 dB sabit kazançta, fs = 4 MS/s ve 979 Hz RBW'de anlık dinamik alan **‹…› dB**
> (P_max ‹…› dBFS, ölçüt: kırpma/1 dB sıkışma), SFDR ‹…› dBc (‹…› dBFS; imaj hariç ‹…› dBc;
> taşıyıcının ±25 kHz yakını hariç) ölçülmüştür. Kazanç yönetimi (−1…73 dB) ile toplam dinamik
> alan ‹≥› ‹…› dB'dir (‹…› dBm @ −1 dB kazanç; MDS ‹…› dBm @ 73 dB).”

---

## 3. Anlık bant genişliği — `inst_bw` [MHz]

**Tanım (jüri).** Sabit giriş gücünde ölçülen alıcı genlik yanıtı `H(f)`'nin merkez
referansından 3 dB (ve 1 dB) düştüğü frekanslar arasındaki genişlik:

```text
BW_−3dB = f_üst − f_alt ,   H(f) = P_ölçülen(fc + δ) − ayar,  merkez (|δ| ≤ 0.1·fs) medyanına normalize
```

Fiziksel üst sınır: AD9363 analog BW ≤ 20 MHz ve `fs` (alias'sız bölge ±fs/2).

**Tezgah.** §7.1. Kaynağın frekansa göre düzlüğü DUT'tan iyi olmalı (değilse kaynak, referans
cihazla ölçülüp düzeltilir).

**Prosedür (`eds measure inst_bw`).** Her `(fs, bw)` konfigürasyonu için (`configs`, vars.
`[[20e6, 20e6], [10e6, 10e6], [5e6, 5e6]]`; hızlı modda `quick_configs` ya da yalnız ilki),
sabit `gain = 40 dB`, `nfft = [dsp] nfft`, `averages = 4`:

1. Ton seviyesi `tone_dbfs` (−20 dBFS) için `adjust_tone` (+ yerinde `e`).
2. Ton `±span_frac·fs` (±0.49·fs) aralığında `points` (41; hızlı 21) eşit adımla gezdirilir,
   DC'nin ±`skip_dc_bins` (2) bini atlanır; her nokta `stimulus_changed` + settled yakalama;
   ton gücü görünen frekansında ±5 binlik gürültüsü düşülmüş bant gücü.
3. Yanıt merkez noktaların (`|f| ≤ ref_frac·fs` = 0.1·fs) medyanına normalize edilir; −1/−3
   dB kenarları merkezden dışa **ilk geçişlerin** dB'de doğrusal enterpolasyonudur; her geçiş
   aralığı `refine_steps` (3; hızlı 2) ek ton noktasıyla ikiye bölünür (kenarda sıklaştırma).
4. Düzlük = ±`flat_frac`·fs (0.4) içindeki tepe-tepe dalgalanma.
5. **Kaynak yoksa yedek yöntem:** giriş sonlandırılıp bandın en yüksek kazancında
   `noise_averages` (64; hızlı 32) ortalamalı spektrum, DC çentiği enterpolasyonla doldurulur,
   `smooth_frac·N` (0.01) binlik kayan ortalama, merkez medyanına normalize; yöntem alanı
   **“gürültü şekli (yaklaşık, düşük güven)”** yazar (ADC'nin düz tabanı etekleri maskeler).

**Değer.** Konfigürasyonlar içinde **en büyük −3 dB genişliği** (MHz), profilin `bw_max_hz`'i
(20 MHz) ile sınırlanır (not düşülür). Bir −3 dB geçişi açıklıkta bulunamazsa değer alt
sınırdır (≥, `value_bound = "lower"`).

**Raporlanan.** Ek: −1 dB genişliği, düzlük, kenar frekansları, konfigürasyon başına sonuçlar,
yöntem, `in_situ_ppm`, profil sınırı uygulandı mı. Şekiller: normalize yanıt eğrisi (−1/−3 dB
çizgileri; tek konfigürasyonda da açıklamada ölçülen −3 dB genişliği), konfigürasyon başına bant
kenarı spektrum ekran görüntüsü: enterpole −3 dB kenarları dikey çizgi (“−3 dB kenarı”), işaretçi
kenara en yakın ölçülen ton noktasıdır ve kendi normalize yanıtını yazar (“en yakın ton noktası
(−x.x dB)”), bilgi kutusunda −3 dB genişliği.

**Donanım notları.** 20 MS/s'de RX bayat tampon penceresi Pluto TX kuyruk gecikmesinden çok
kısadır → her nokta kaynağın bekleme süresini bekler (yavaş ama doğru). Analog BW > fs ise −3
dB genişliği fs ile sınırlanır (rapor `fs` ve `bw`'yi yazar). Simülatörde AD936x analog köşesi
1.4 × BW/2 + TIA kutbu modellendiğinden 20 MHz ayarda ≈ 16.9 MHz −3 dB (dijital filtre
±0.4·fs düz) ölçülür.

**Örnek KTR cümlesi.**
> “fs = 20 MS/s ve 20 MHz analog BW ayarında ölçülen anlık bant genişliği **‹…› MHz (−3 dB)**,
> ‹…› MHz (−1 dB) olup ±0.4·fs içindeki düzlük ‹…› dB tepe-tepedir (ton adımlama, ‹…› nokta +
> kenar başına 3 ikiye bölme).”

---

## 4. Tarama hızı — `scan_speed` [GHz/s]

**Tanım (jüri).**

```text
S = (f_stop − f_start) / T_tarama                       [GHz/s]
T_tarama = Σ_adımlar (t_tune + t_settle + t_capture + t_dsp)   (duvar saati, perf_counter)
T_yeniden_ziyaret(325 MHz–3.8 GHz) = tam resmî bant / S + ardışık taramalar arası boşluk
```

**Tezgah.** Kablo gerekmez (anten veya sonlandırıcı); host ve USB koşulları rapora yazılır.

**Prosedür (`eds measure scan_speed`).**

1. Aralık: `[sweep] start_hz/stop_hz` (hızlı mod: 325–825 MHz).
2. Konfigürasyonlar `configs` (vars. `{fs 20 MS/s, RBW 50 kHz}` = **referans**,
   `{20 MS/s, 10 kHz}`, `{10 MS/s, 10 kHz}`; isteğe bağlı `averages`, `nfft`, `bw`; hızlı modda
   yalnız referans). Her biri için `[sweep]` geçersiz kılınır (BW = fs, profil sınırlarında) ve
   `Sweeper` `repeats` (2; hızlı 1) tam taramayı ardışık koşar; `[dsp]`/`[detect]` ve DUT'un
   kayıtlı kalibrasyonu RX düğümündekiyle aynıdır. Profilin reddettiği konfigürasyon atlanır
   (`skipped_configs`).
3. Her `SweepResult`: duvar saati süresi + tune/settle/capture/dsp toplamları (son birleştirme +
   CFAR “dsp”). Etiketler **gerçek** sweeper RBW/N ile yazılır (ör. istenen 50 kHz → 39.15 kHz,
   N = 1024), istenen RBW `rbw_requested_hz` koşulundadır.

**Değer.** Referans konfigürasyonun taramalarının **medyan** hızı. Ek: konfigürasyon başına hız
(en kötüsü dahil), adım başına ortalama ms dökümü (+ “other”), zaman bileşenlerinin toplamı /
süre oranı, adım sayısı ve boyu, tespit sayısı, taramalar arası boşluk, tam resmî bandın
yeniden ziyaret süresi (açıklık küçükse ölçülen hızla ekstrapole).

**Karar engeli.** Gerçek zamanlı olmayan simülatörde (`sim.realtime = false`) zamanlama
anlamsızdır → `REASON_NONREALTIME` blocker.

**Raporlanan.** Şekiller: adım dökümü (konfigürasyon başına yığılmış çubuk; x ekseninde
konfigürasyon etiketi: fs, gerçek RBW / N, ortalama; çubuk üstünde ms/adım ve GHz/s), taramanın
max-hold spektrum ekran görüntüsü (en güçlü tespitler işaretli).

**Donanım notları.** Pluto'da adım süresine retune maliyeti hâkimdir: USB üzerinden libiio
öznitelik yazımı, PLL kilidi ve her retune'dan sonra atılması gereken bayat tamponlar
(`(kernel_buffers + 1) × buffer_size` örnek = “settle”). Tarama hızı RBW ve ortalamaya
bağlıdır (yakalama ∝ N·n_avg/fs) — RBW belirtilmeden sayı anlamsız; yeniden ziyaret süresinden
kısa sinyaller kaçabilir (POI) — rapor bunu yazar. AD9361 fastlock profilleri gelecekteki
iyileştirmedir.

**Örnek KTR cümlesi.**
> “325 MHz–3.8 GHz bandı, 20 MS/s burst, 39.1 kHz RBW (N = 1024) ve 4 ortalama ile **‹…› GHz/s**
> hızla taranmaktadır (medyan, ‹…› tarama; tam tarama ‹…› s, ‹…› adım; adım başına tune ‹…› ms,
> settle ‹…› ms, yakalama ‹…› ms, işleme ‹…› ms). Aynı frekans en geç ‹…› s'de bir yeniden gözlenir.”

---

## 5. Frekans çözünürlüğü — `freq_resolution` [Hz]

**Tanım (jüri).**

```text
RBW     = ENBW_bins · fs / N                        (hesap; Blackman-Harris 2.004 bin)
Δf_min  = eşit güçlü iki tonun arasında ≥ 3 dB çukur görülen en küçük ayrım   (ölçülen, başlık)
e_f     = f_ölçülen − f_gerçek   [Hz, ppm]   — ham ve kayıtlı kalibrasyon ppm'i ile düzeltilmiş
```

**Tezgah.** İki-ton: `max_tones ≥ 2` kaynak (simülatör, Pluto TX) veya iki üreteç + güç
birleştirici. Frekans doğruluğu: **referansa (GPSDO/10 MHz) kilitli** üreteç.

**Prosedür (`eds measure freq_resolution`).** Varsayılanlar: `fs = 2 MS/s`, `bw = fs`,
`gain = 40 dB`, `nfft/window/averages/overlap = [dsp]`, `zero_pad = 8`.

1. RBW hesaplanır; tek tonun −3 dB çizgi genişliği ×8 sıfır dolgulu Welch spektrumunda
   enterpolasyonla ölçülür (tonun binler arasındaki konumundan bağımsız).
2. **İki-ton:** tonlar `fc + center_frac·fs ± Δf/2` (0.2·fs; yerinde `e` ile görünen
   frekansa), ton başına `tone_dbfs` (−20 dBFS) için ayarlanan tek ton ayarı; iki tonun toplamı
   (+3 dB) RX tavanını aşmasın diye ton başına ayar `min(tek ton ayarı, tavan − 3 dB)`
   (`two_tone_setting_*`, `two_tone_backoff_db`). Her ayrımda `captures` (4; hızlı 2) bağımsız
   settled yakalamanın sıfır dolgulu spektrumları ortalanır (farklı göreli fazlar → evre-dışı
   ortalama); her gerçek frekansın ±RBW içinde birer yerel tepe ve aralarında ≥ `dip_db`
   (3 dB) çukur aranır. Δf, `sep_min_rbw … sep_max_rbw` × RBW (0.5…6) aralığında `sep_points`
   (12) noktalı geometrik dizi; üstündeki her ayrımın ayrıştığı en küçük ayrım çevrelenir ve
   `bisect_steps` (3) geometrik ikiye bölmeyle inceltilir.
3. **Frekans doğruluğu:** `fc + accuracy_frac[1]·fs` (−0.1·fs) tonunun frekansı
   `tone_frequency` ile `accuracy_n` (2¹⁸) örnekte ölçülür: ham hata ve **kayıtlı kalibrasyon
   ppm'i** (`ctx.cal.ppm`, bağımsız elde edilmiş) ile düzeltilmiş hata (= operasyonel
   doğruluk). Aynı kaynaktan `fc + accuracy_frac[0]·fs` (0.15·fs) tonuyla yerinde ppm de
   kestirilir; ancak kaynak–alıcı referans oranını paylaştığı için onunla düzeltilmiş hata
   **öz-tutarlılıktır** (yalnız kestirici gürültüsü), doğruluk olarak sunulmaz
   (`freq_error_self_consistency_hz`). `reference_locked` (otomatik: simülatör evet,
   `pluto_tx` hayır — kendi TCXO'su ±25 ppm; diğerleri bilinmiyor): kilitli kaynak yoksa ham
   fark yalnızca **bağıl** referans farkıdır, mutlak doğruluk iddia edilmez.

**Değer ve belirsizlik.** İki-ton ölçüldüyse en küçük doğrulanmış ayrım; belirsizlik = son
çevreleme aralığı. En küçük denenen ayrım bile ayrıştıysa değer üst sınırdır
(≤, `value_bound = "upper"`). Çoklu ton kaynağı yoksa değer hesaplanan RBW'dir (yöntem yazar).

**Raporlanan.** Ek: RBW, `enbw_bins`, bin genişliği, tek ton çizgi genişliği, ayrım/RBW oranı,
ayrımda çukur, en büyük ayrışmayan ayrım, frekans hatası (Hz, ppm) ham / kalibrasyonlu /
öz-tutarlılık, `reference_locked`, `accuracy_verified`. Şekiller: çukur derinliği–ayrım eğrisi
(ölçülen + pencerenin analitik eş güçlü/eşevresiz eğrisi, 3 dB ölçütü; y ekseni veriye göre),
ayrışan ve ayrışmayan iki-ton yakın plan ekran görüntüleri, tek ton ve gürültü spektrumları.

**Tuzaklar.** Ortalama çözünürlüğü artırmaz (yalnızca varyansı düşürür); pencere RBW–yan lob
dengesini belirler (rapor pencereyi yazar); ppm hatası çözünürlüğü değil **doğruluğu** bozar —
iki ayrı sayı; ısınma kayması.

**Örnek KTR cümlesi.**
> “fs = 2 MS/s, N = 4096, Blackman-Harris pencerede RBW 979 Hz'dir; eşit güçlü iki ton
> **‹…› Hz** (‹…›×RBW) ayrımda ≥ 3 dB çukurla ayrıştırılmıştır (ayrım başına 4 yakalama).
> GPSDO kilitli ‹…› MHz referansında frekans hatası ham ‹…› Hz (‹…› ppm), kayıtlı kalibrasyon
> ppm'i sonrası ‹…› Hz'dir.”

---

## 6. DF RMS hatası — `df_rms` [°]

**Tanım (jüri).** Bilinen M açıda (her biri K tekrar), koherens kontrolü ve faz kalibrasyonundan
**sonra**:

```text
RMS = sqrt( (1/(M·K)) · Σ wrap(θ̂_i − θ_i)² )        wrap: (−180°, 180°]
İnterferometre: Δφ = 2π·d·sin(θ)/λ + φ_cal  ⇒  θ̂ = asin( λ·(Δφ − φ_cal) / (2π·d) )
```

Ayrıca **sapma** (ortalama hata), std ve **en büyük |hata|**. Belirsizliksiz bölge `d ≤ λ/2`;
0.15 m ⇒ 1 GHz'e kadar.

**Tezgah.** HARDWARE §7.2 (kalibrasyon: bölücü + eşit kablolar, ortak 40 MHz referans) ve §7.3
(havadan: λ/2 aralıklı iki anten, uzak alanda bilinen açılarda verici; döner tabla). Simülatör:
`config/dual_sim.toml`.

**Prosedür (`eds measure df_rms`).** Dizi: oturumun ilk iki cihazından `DeviceGroup` (2× Pluto,
her biri kendi alt sürecinde) ya da ≥ 2 koherent kanallı tek DUT. Varsayılanlar: `fc = [rx]`,
`fs = 2 MS/s`, `baseline_m = λ/2`, aynı manuel kazanç (`[rx] gain_db`), `n = 65536`.
Grup akışı bir kez başlatılır ve sona kadar **kesintisiz** sürer (üyelerin örnek hizası
kalibrasyondan son açıya kadar korunur); operatör beklerken akış arka planda okunup atılır.

1. **Koherens:** ortak sinyal `fc + pilot_offset_frac·fs` (0.1) her iki kanala (simülatör:
   sahneye `cabled` emitter; donanım: operatör bölücü beslemeli kaynağı bağlar) →
   `check_coherence` (`coherence_blocks` = 8): faz kayması, koherens, faz kararlılığı.
   Koherent değilse ölçüm **devam eder**, özet “KOHERENS YOK - ortak 40 MHz referans gerekli”
   taşır ve `REASON_NO_COHERENCE` blocker eklenir (karar yok).
2. **Faz kalibrasyonu** hedef frekansta (`fc + target_offset_frac·fs` = 0.2·fs):
   `Interferometer2.calibrate` (gerçek azimut 0°) → `φ_cal`. Ölçüm frekansında kalibre etmek
   bilinmeyen akış gecikmesinin `2π·f_bb·d/fs` fazını yok eder (CW pilot gecikmeyi çözemez;
   başka frekansta kalibrasyon her kerterize `2π·Δf_bb·d/fs` bırakır). Pilot fazı ile `φ_cal`
   farkı gecikme göstergesi olarak raporlanır.
3. **Açılar:** vars. −60…+60°, 10° adım (hızlı 20°; ±85° üstü reddedilir), her açıda
   `repeats` (5; hızlı 2) × `n` örnek → tonun taban bant frekansı her yakalamada bulunur, FFT
   bant maskesiyle `Interferometer2.bearing()`. Simülatör: `cabled = false` emitter
   `aoa_deg`'den; donanım: operatör diziyi döner tablada çevirir.
4. **Overflow** (hizalama kaybı) → bir sonraki yakalamadan önce yeniden faz kalibrasyonu
   (açı başına en fazla `max_recalibrations` = 3).
5. `df.metrics.summarize` → değer = RMS. Belirsizlik açıları bağımsız birim sayar:
   `u = std_m(ortalama_k hata²) / (2·RMS·√M)`.

**SNR.** Her SNR belirtilen bantta (`snr_bw_hz` = 25 kHz, dar bant kanal) ve FFT-bin / örnek
başına değerleriyle raporlanır. Simülatörde havadan ton `sim_snr_db` (10 dB, 25 kHz'te; 2 MS/s'de
örnek başına ≈ −9 dB) seviyesine bir referans yakalamadan ayarlanır (`sim_power_dbm` verilirse
sabit seviye), kablolu kalibrasyon sinyali güçlü kalır (`sim_cal_power_dbm` = −60 dBm); hızlı
mod dışında açı taraması `snr_sweep_db` (−10, 0, 10, 20, 30 dB) için tekrarlanır → RMS–SNR eğrisi
(`df_rms_vs_snr`; RMS ∝ 1/√SNR). Donanımda kaynak ayarı `source_power_dbm` (−40). Tüm seviyeler
`rx_max_input_dbm` ile sınırlıdır.

**Simülatör uyarısı / karar.** Simülatör YALNIZ termal gürültüyü modeller (çok yollu yayılım,
anten karşılıklı kuplajı/eleman örüntüleri, anten/kablo faz uyumsuzluğu, LO faz kayması yok):
değer gerçek bir dizinin hatasının **alt sınırıdır**; özet ve yöntem bu metinle başlar ve
`BLOCKER_SIM` eklenir (hedef kararı yok).

**Raporlanan.** Ek: sapma, std, max |hata|, frekans, `d/λ`, SNR (25 kHz / bin / örnek),
koherens raporu, kalibrasyon günlüğü (yeniden kalibrasyonlar), gecikme göstergesi, SNR taraması.
Tablo: açı başına gerçek/kestirim/hata. Şekiller: kestirim–gerçek (ideal doğru), hata–açı,
polar gösterim, RMS–SNR (simülatör, hızlı mod dışı), havadan ton spektrum ekran görüntüsü.

**Tuzaklar.** Ortak referans yoksa faz sürekli döner; her retune'da LO fazı rastgele →
kalibrasyon; örnek kayması → aynı frekansta kalibrasyon; çok yollu yayılım ve kuplaj; ±90°'ye
yakın hata `1/cos θ` ile büyür → tarama ±60°; `d > λ/2` ise belirsizlik.

**Örnek KTR cümlesi.**
> “İki elemanlı interferometre (d = 0.15 m = λ/2 @ 1 GHz, ortak 40 MHz referans, ölçüm
> frekansında bölücü ile faz kalibrasyonu) ile ‹…› GHz'te −60°…+60° aralığında 10° adımla (açı
> başına 5 ölçüm, SNR ‹…› dB / 25 kHz) ölçülen yön bulma RMS hatası **‹…›°**, sapma ‹…›°, en büyük
> hata ‹…›°'dir.”

---

## 7. İmaj bastırma — `image_rejection` [dB]

**Tanım (jüri).** `fc + δ`'daki tek tona karşı `fc − δ`'da oluşan imajın bastırılması:

```text
IRR(fc, δ) = P(fc + δ) − P(fc − δ)    [dB]
Kuram: IRR = 10·log10( (1 + 2g·cosφ + g²) / (1 − 2g·cosφ + g²) ),  g = 10^(ε/20)
       (ε: IQ kazanç dengesizliği dB, φ: faz dengesizliği; 0.4 dB / 2.5° ⇒ ≈ 30 dB)
```

Başlık değeri: **quadrature tracking AÇIK** (operasyonel durum) ve `|δ| ≤ worst_frac·fs`
(0.4) içindeki **en kötü** IRR, tüm merkez frekanslar üzerinden; tracking kapalı değerler ayrıca.

**Tezgah.** §7.1. Kaynak zorunludur (`requires = {"source"}`); SCPI üreteç önerilir.

**Prosedür (`eds measure image_rejection`).** Varsayılanlar: `fcs` (vars. `[fc]`), `fs = 4 MS/s`,
`bw = fs`, `gain = 30 dB`, `nfft/averages = [dsp]`, `offsets_frac = [0.05, 0.1, 0.2, 0.3, 0.4]`
(her iki işaretle), `tone_dbfs = −10`.

1. Her `fc` için tracking durumu (önce ON, sonra OFF) `set_tracking(quadrature=...)` ile kurulur;
   dönen yol kontrol edilir: OFF gerçekten uygulanamıyorsa (ince katmansız Pluto:
   `"unsupported"`) OFF noktaları **atlanır** (not + ek yok); ON bile uygulanamıyor/
   doğrulanamıyorsa sonuca blocker eklenir (karar yok). Her tracking değişikliğinden sonra
   `tracking_settle_s` (0.5 s) örnek atılır (yakınsama).
2. Her `δ` için ton yerinde `e` ile görünen `fc + δ`'ya konur; kendi LO'su olan kaynağın LO
   ofseti noktaya göre seçilir (§0.6; seçilen ofset ve temizlik kaydedilir).
   `P(fc + δ)`: tepe çevresinde ±`halfwidth_bins` (5) gürültüsü düşülmüş bant gücü;
   `P(fc − δ)`: tepe binin **tam ayna** bininde aynısı.
3. İmajın ham bant gücü gürültü + `bound_margin_db`'nin (6 dB) altındaysa nokta, ton
   `bound_boost_dbfs`'e (−3 dBFS, kırpmasız, RX tavanıyla sınırlı) yükseltilerek tekrar ölçülür;
   hâlâ gürültüdeyse IRR alt sınırdır (`P(+δ) − P_ham(−δ)`, işaretli).
4. Sonda tracking her durumda ON'a döner. `save = true`: `fc` başına en kötü ON değeri
   `cal.image_rejection_db`'ye yazılır ve kalibrasyon deposuna kaydedilir.

**Değer.** En kötü ON IRR (en kötü nokta alt sınırsa “≥”, `value_bound = "lower"`).
**Raporlanan.** Ek: tracking kapalı en kötü IRR, iyileşme, `fc × δ × tracking` tablosu, alt
sınır işaretleri, `tracking_paths`, nokta başına `source_lo_offset_hz` / `source_artifacts_clear`.
Şekiller: IRR–ofset (her fc, iki tracking durumu), ton + imaj işaretli spektrum ekran görüntüsü.

**Donanım notları.** Kaynağın kendi imajı: Pluto TX'in IQ dengesizliği de `2·LO − f`'de imaj
üretir; varsayılan 200 kHz LO ofsetinde bu RX imajına neredeyse çakışabilir (ör. fs = 4 MS/s,
δ = +0.05·fs'de ~100 Hz) — LO yerleşimi bu yüzden her noktada yapılır (simülatörde −40 dBc
TX imajıyla yerleştirmesiz ölçüm IRR'yi ≈ 40 dB'de kaynak sınırlı bırakır). İnce katman (`control_uri`) yoksa tracking OFF ölçülemez. Küçük δ DC
çentiğine yaklaşır; imajın gürültüde kalması alt sınır verir.

**Örnek KTR cümlesi.**
> “Quadrature tracking açıkken ‹…› merkez frekansında (±0.05…0.4·fs ofset, 30 dB kazanç,
> ‹kaynak›) ölçülen en kötü imaj bastırma **‹…› dB**'dir; tracking kapalıyken ‹…› dB'ye düşer.”

---

## 8. Taşmasız maksimum örnekleme hızı — `max_rate` [MS/s]

**Tanım (jüri).**

```text
fs_max = max { fs : T saniyelik SÜREKLİ akışta overflow = 0, okuma zaman aşımı = 0,
                    (pipeline) host kuyruğu düşmesi = 0, işleme birikimi sınırda,
                    VE örnek sayımında kayıp ≤ izin }
Veri hızı = fs · bytes_per_sample  [MB/s]   (CS16: 4 B; USB 2.0 teorik bulk 53.248 MB/s)
```

İki mod: **`pipeline`** — **başlık (sistem düzeyi, “DSP hattı açık”)**: DUT üzerinde gerçek
`RxNode` (okuyucu thread + host kuyruğu → Welch → CFAR → özel bir ZMQ ucunda `rx.detections` /
`rx.spectrum` yayını; `[rx]` blok/kuyruk, `[dsp]`/`[detect]` ayarlarıyla); **`transport`** —
oku-at (USB + libiio + host; ayar/tanı değeri). Yalnız taşıma ölçülürse
`BLOCKER_TRANSPORT_ONLY` (karar yok).

**Tezgah.** DUT tek başına (anten/sonlandırıcı). Host modeli, USB denetleyicisi, port, hub,
kablo, `buffer_size` rapora yazılır.

**Prosedür (`eds measure max_rate`).**

1. Aday hızlar: `profile.candidate_sample_rates_hz` (AD9363: 0.521…61.44 MS/s), profil ve
   sürücü sınırları içinde (dışındakiler atlanır); hızlı mod: 1, 2.4, 4, 5, 6, 8 MS/s.
   Önce pipeline taranır; ilk başarısızlığın altında geçtiği her hız **çıkarımla** taşıma
   geçişidir (aynı cihaz/taşıma, daha ağır tüketici).
2. **Taşıma:** akış durdur, `set_sample_rate`, BW = hız (profil sınırlarında; ihlal → nokta
   atlanır), akışı `buffer_size` ile başlat; settle penceresi atılır, FIFO birikimi boşaltılır
   (üretimi BEKLEMEK zorunda kalan bir okumaya kadar), sonra `duration_s` (5 s; hızlı 0.6 s)
   sürekli okuma. İlk başarısızlık işaretinden sonra pencere `fail_window_s`'e (1 s; hızlı 0.3)
   kısaltılır.
3. **Pipeline:** hızda yeni bir RxNode (`pipeline_transport` tcp|inproc); ilk kare +
   `pipeline_warmup_s` (0.2 s) sonra düğüm ve cihaz sayaçlarının anlık görüntüsü, `duration_s`
   çalışma, tekrar anlık görüntü. Overflow/zaman aşımı/host düşmesine ek olarak işleme
   yetişmeli: sonda okunmuş ama analiz edilmemiş örnekler (host kuyruğu + biriktirici) host
   kuyruğunun yarısı + bir Welch penceresinden az olmalı. Gerçek zamanlı olmayan simülatörde
   pipeline ölçülemez (nedeniyle atlanır) ve sonuç `REASON_NONREALTIME` ile engellenir.
4. **Örnek sayımı çapraz kontrolü** (bazı sürücüler overflow'u geç/hiç bildirmez; Pluto'da HAL
   DMA durum yazmacını okur, `conditions["overflow_source"]`). `L = beklenen − teslim edilen`,
   eldeki en iyi saatle:
   * `hw_timestamp` — cihaz zaman damgaları (izin: 1 örnek + 1 ppm);
   * `device_counter` — cihazın mutlak örnek sayacı (simülatör): taşımada kesin; pipeline'da
     ±1 blok;
   * `wall_clock` — taşımada `g_i = D_i − fs·(t_i − t0)`'ın **üst zarfı** (üretimi bekleyen
     okuma “tampon tamam + aktarım” anında döner); pencere sonrası FIFO, iki ardışık okuma
     üretim hızında olana kadar (`Δg ≤ 0.1·buffer`) boşaltılır, böylece sonda kuyrukta kalan
     birikim kayıp sayılmaz: `L = max g(pencere) − max g(boşaltma)`. İzin:
     `max(buffer/2, ref_clock_ppm·beklenen + wall_clock_jitter_s·fs)`; sessizce düşen tampon
     bütündür (`buffer` örnek) — izin bir tampona ulaşırsa sonuç tek kayıp tamponun
     çözülemediğini yazar;
   * `wall_clock_snapshot` — sayaçsız pipeline: anlık görüntüler arası `fs × Δt`, izin
     `(kernel_buffers + 2) × buffer` + saat (yalnız kaba kayıp; tek sessiz tampon taşıma modu ve
     overflow bayrağıyla aranır).
5. **Arama:** bir mod `stop_after_failures` (2) ardışık başarısızlıkta durur. `fs_max` = ilk
   başarısızlığın altında geçen en yüksek hız (başarısızlığın üstündeki geçiş “monoton değil”
   olarak raporlanır, değer olarak asla); `refine` (hızlı modda kapalı) ile son geçiş–ilk
   başarısızlık aralığı `refine_steps` (3) kez ikiye bölünür (1 kS/s ızgara).
6. **Ayar matrisi:** `buffer_sizes = [...]` taşıma aramasını her tampon boyu için tekrarlar
   (`extra["matrix"]`); Pluto'da tampon boyu (akış argümanı `bufflen`) sürücünün okuduğu tek akış
   ayarıdır. SoapyPlutoSDR cihaz argümanlarından yalnız `uri`, `hostname`, `label` okunur
   (`serial` EDS'te `uri`'ye çözülür); `usb_direct`, `timeout`, `loopback`, `timestamp_every` gibi
   diğerleri **yok sayılır**: ölçüm koşulu değildir, `conditions["device_args_ignored"]`'da
   listelenir ve özette yazılır; `conditions["device_args"]` yalnız etkin argümanlardır. Sürücünün
   yalnız açılışta okuduğu argümanlar (başka backend) çalışan ölçümle değiştirilemez: her
   `[[device]].args` varyantı için ayrı koşun.

**Raporlanan.** Değer: pipeline `fs_max` (MS/s; `extra["headline_mode"]`), özetin ilk cümlesi
modu adlandırır (“Sistem düzeyi (DSP hattı açık: RxNode …)”); taşıma değeri, MB/s, USB 2.0
teorik/pratik sınıra oranı, hız başına sayaçlar (`lost_samples`, `allowed_lost_samples`,
muhasebe yöntemi). Şekiller: overflow/kayıp – fs, MB/s – fs (USB2 sınır çizgileri), tampon
matrisi (varsa), en yüksek geçen hızda spektrum ekran görüntüsü. Gerçek zamanlı simülatörde özet, taşıma sınırının yapılandırılmış USB
modelinden (`usb_bytes_per_s`) geldiğini yazar.

**Karar engeli (overflow).** `conditions["overflow_source"]` cihazın kendi beyanıdır
(`dma_status` / `driver` / `none`; beyan yoksa boş). Gerçek donanımda `none` (Pluto'da ince
katman `control_uri` yok) ise örnek kaybı cihazca bildirilmez, yalnız sayımla aranır:
`BLOCKER_NO_OVERFLOW` (“örnek kaybı algılanamaz: overflow sayacı bildirilmiyor …”), özette
yazılır, hedef kararı verilmez. Şekillerde GEÇTİ/KALDI yeşil/kırmızı, sayım izni ve gerekli veri
hızı gri (referans), overflow/zaman aşımı/host drop olayları amber (`Series.role`).

**Donanım notları.** Pluto'da ince katman (`control_uri`) yoksa `overflow_source = "none"`:
overflow algılanamaz, kayıp yalnız sayımla yakalanır (karar engeli, yukarıda). SoapyPlutoSDR yalnızca `uri`,
`hostname`, `label` ve akış argümanı `bufflen`'i okur; `usb_direct`, `timestamp_every`,
`loopback`, `timeout` gibi argümanlar **yok sayılır** (EDS uyarır) — ölçüm koşulu değildir.
KTR değeri için `duration_s = 60` önerilir (kısa T aralıklı overflow'u gizler); paylaşılan USB
denetleyicisi (2 Pluto) toplam yük; dizüstü güç tasarrufu/CPU ölçekleme; VM/WSL USB geçişi
güvenilmez; Python GIL/tüketici yavaşlığı **host drop** üretir (taşıma değil) — ayrı sayılır.

**Örnek KTR cümlesi.**
> “ADALM-Pluto + ‹host› (USB 2.0, ayrı kök port, DMA durum yazmacıyla overflow izleme) ile 60 s
> sürekli akışta sıfır overflow ve sıfır örnek kaybıyla sürdürülebilen en yüksek örnekleme hızı,
> tam DSP hattı (RxNode: Welch + CFAR + ZMQ yayını) açıkken **‹…› MS/s** (‹…› MB/s, USB 2.0
> teorik sınırın %‹…›'i), yalnız taşımada ‹…› MS/s'dir.”

---

## 9. Uçtan uca gecikme — `latency` [ms]

**Tanım (jüri).**

```text
L_i = t_teslim,i − t_RF,i
  t_RF     : RF olayının alıcı girişinde başladığı host duvar saati anı (kaynağa göre, aşağıda)
  t_teslim : olayı içeren ilk rx.detections mesajının ZMQ abonesine ulaştığı an (t_recv_ns)
İstatistik: p50, p95 (başlık), max; p99 yalnız N ≥ 100; p95'in %95 güven aralığı yalnız N ≥ 72
```

**Tezgah.** §7.1; ölçüm gerçek yarışma hattını koşar: DUT üzerinde `RxNode` (akış → Welch →
CFAR → yayın) özel bir uçta (`transport = "tcp"`: `tcp://127.0.0.1:<boş port>`, dağıtımla aynı
soket yolu; `"inproc"`: yalnız süreç içi) ve tüketiciyi oynayan bir `Subscriber`.

**Prosedür (`eds measure latency`).**

* **Mod B — RF olayı gecikmesi** (kaynak varsa; KTR değeri). Ton `fc + offset_frac·fs`
  (0.15·fs), seviyesi hattın kendi raporladığı SNR'den uyarlanır (`snr_db` = 30, en az
  `min_snr_db` = 25; mutlak/bağıl kaynak fark etmez) ve RX girişini `rx_max_input_dbm`'in üstüne
  sürmez (mutlak kaynak: `source_power_range`; bağıl kaynak: `relative_setting_cap`, düğümün
  yayınladığı ton bant gücünden — kalibrasyonlu düğümde dBm, değilse nominal seviye modeli). Ayar
  birimiyle kaydedilir: `tone_power_dbm` (mutlak) / `tone_power_rel_db` (bağıl), üst sınırı
  `tone_power_max_*`. `n_events` (100; hızlı 6) kez: kaynak
  kapalı → sessiz kare (kapamadan sonra başlayan, ton yakınında tespit içermeyen pencere;
  `quiet_timeout_s`) → rastgele bekleme (`hold_max_s`, 0 = 2 blok süresi; blok sınırlarıyla
  korelasyonu kırar, `seed` ile tekrarlanabilir) → kaynak açık → penceresi en erken olası
  RF-açık anında/sonrasında biten ve tonun ±max(2·RBW, 5 kHz) (düğüm referansı düzeltmiyorsa
  + TCXO toleransı) içinde tespit taşıyan ilk `rx.detections` (`event_timeout_s`) →
  `L = t_recv − t_RF`.
* **RF-açık anı `t_RF`** (kaynak çağrısı `t_before` / `t_after` ile çevrelenir, `t_ret` dönüş):
  * simülatör kaynağı — sahne geçişi birebir (`t_ret`), ±0;
  * akışlı TX (`estimated_queue_latency_s` = Q, Pluto TX) — ton kuyruktaki örneklerden sonra
    başlar: `t_RF = t_ret + Q` ± `Q/4`;
  * diğer (SCPI `:OUTP ON` + `*OPC?`, manuel) — `[t_before, t_after]` ortası ± yarı genişlik.
  Düzeltme, belirsizliği ve kaynak türü kaydedilir; belirsizlik p95'in %50'sinden büyükse
  blocker (sayı alıcıdan çok kaynağı anlatır).
* **Mod A — host tarafı işleme gecikmesi** (her zaman; uçtan uca DEĞİL): her `rx.detections`
  için `L_A = t_recv − t_acq_end` = işleme (`t_proc − t_acq_end`: kuyruk + DSP) + yayın
  (`env.t_ns − t_proc`) + taşıma (`t_recv − env.t_ns`, ZMQ). `t_acq_end` okumanın **döndüğü**
  host anından türetildiği için son örneğin cihaz FIFO'sunda ve USB'de geçirdiği süre dahil
  değildir: Mod A RF-teslim gecikmesinin alt sınırıdır; raporda bir tampon aktarımı
  (`buffer_size · bytes_per_sample / interface_practical_bytes_per_s`) bilinen minimum olarak
  eklenir (`mode_a_p95_plus_transfer_ms`). `duration_s` (5 s; hızlı 1.5 s) toplanır.

**Değer ve belirsizlik.** Mod B p95 (kaynak yoksa Mod A p95 + `BLOCKER_MODE_A`, karar yok).
Belirsizlik: p95 güven aralığının yarı genişliği (N ≥ 72) + RF-açık zamanlama belirsizliği;
N < 72'de özet ve yöntem “p95 için %95 güven aralığı hesaplanamadı (N = … < 72)” yazar.
Gerçek zamanlı olmayan simülatörde `REASON_NONREALTIME` blocker.

**Raporlanan.** Ek: p50/p95/p99/max/ortalama/std (her iki mod), olay sayısı, zaman aşımları,
`source_timing` (tür, düzeltme, belirsizlik), Mod A dökümü, `usb_transfer_min_ms`, blok/kuyruk/fs/
`spectrum_rate_hz`. Tablo: olay başına `t_rf_ns`, `t_rf_unc_ms`, gecikme. Şekiller: Mod B ve Mod A
histogram/CDF (`kind = "hist"`, p50/p95(/p99) çizgileri), mod başına tek yığılmış döküm çubuğu
(tepesi ortalama gecikme; x ekseninde mod ve örnek sayısı). Değer yuvarlanmadan saklanır (rapor
basamakları biçimler).

**Donanım notları.** Kuramsal alt sınır blok süresidir (65536 / 4 MS/s = 16.4 ms; ortalama
≈ 1.5 blok + işleme) → `block_size` gecikme–verim dengesi; Pluto TX kuyruğu (`4 × buffer_len / fs`)
düzeltilir ama ±%25 belirsizliği sonuca girer; manuel kaynak karar vermeye yetmez;
`spectrum_rate_hz` kısıtı; ZMQ HWM aşımı mesaj düşürür; Python GC duraklamaları kuyruğu büyütür;
EDS bus'ta yalnız duvar saati kullanır.

**Örnek KTR cümlesi.**
> “4 MS/s, 65536 örneklik blok ve 10 Hz spektrum hızında, RF olayından tespit mesajının ZMQ
> abonesine teslimine kadar ölçülen uçtan uca gecikme ‹N ≥ 100› olayda p50 ‹…› ms, **p95 ‹…› ms**
> (%95 GA ‹…›–‹…› ms), p99 ‹…› ms, en kötü ‹…› ms'dir (RF-açık anı: ‹kaynak›, ±‹…› ms; host tarafı
> işleme p95 ‹…› ms).”

---

## 10. KTR tablosu şablonu

Raporun özet tablosu bu biçimdedir; **her satırda koşullar** yazılıdır (koşullar varsayılan
parametrelerle):

| Spesifikasyon | Ölçülen | Hedef (KTR) | Durum | Koşullar | Yöntem |
|---|---|---|---|---|---|
| Duyarlılık | ‹…› dBm | ≤ −100 dBm | GEÇTİ/KALDI | 935 MHz, fs 2 MS/s, RBW 979 Hz, Pfa 1e-6, Pd 0.9, G = 73 dB | Pd eğrisi (probit), CFAR |
| Dinamik alan | ‹…› dB | ≥ 60 dB | | G = 40 dB, fs 4 MS/s, RBW 979 Hz | min(kırpma, P1dB) − N_RBW |
| Anlık BW | ‹…› MHz | ≥ 18 MHz | | fs 20 MS/s, BW 20 MHz | ton adımlama, −3 dB |
| Tarama hızı | ‹…› GHz/s | ≥ 5 GHz/s | | 325 MHz–3.8 GHz, 20 MS/s, RBW 39.1 kHz | duvar saati, medyan |
| Frekans çözünürlüğü | ‹…› Hz | ≤ 10 kHz | | fs 2 MS/s, N 4096, BH | iki-ton ≥ 3 dB çukur |
| DF RMS | ‹…›° | ≤ 5° | | d = λ/2, ±60°, ortak referans | interferometre |
| İmaj bastırma | ‹…› dB | ≥ 40 dB | | ‹fc listesi›, ±0.05…0.4·fs, tracking açık | P(+δ) − P(−δ) |
| Maks. örnekleme hızı | ‹…› MS/s | ≥ 4 MS/s | | 60 s, DSP hattı açık | overflow + sayım |
| Gecikme (p95) | ‹…› ms | ≤ 100 ms | | 4 MS/s, blok 65536, N ≥ 100 | RF→ZMQ (Mod B) |

Durum sütunu: **GEÇTİ / KALDI / KARŞILAŞTIRILMADI** (karar engeli, §0.7) / **HEDEF YOK** /
**ATLANDI** / **HATA**. Hedef değerler `config/ktr_targets.toml` dosyasındadır ve **örnektir** —
takımın KTR'sindeki sayılarla değiştirilmelidir. Hedefin taahhüt edildiği koşullar
`[<spec_id>.require]` ile yazılırsa sağlanmayan koşulda karar verilmez.
