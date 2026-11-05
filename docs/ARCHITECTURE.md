# EDS RX omurgası — Mimari, Konvansiyonlar ve Modül Arayüzleri

> Bu belge **kod sözleşmesidir**. Modüller arası her çağrı burada tanımlı imzaya uyar.
> İmza değişikliği = bu belgenin aynı commit'te güncellenmesi. §3'teki imzalar koddan
> (`inspect.signature`) alınmıştır; her satırın sonundaki açıklama tek satırlık anlamdır,
> ayrıntı modül docstring'indedir.

## 1. Katmanlar

```
             ┌──────────── eds.cli (eds ...) ────────────┐
             │                                           │
   eds.measure (KTR ölçümleri + rapor)        eds.rx.node (servis)
             │                                           │
   ┌─────────┼───────────┬───────────┬─────────────┐     │
 eds.calib  eds.df     eds.rx.sweep eds.rx.agc   eds.rx.stream ── eds.bus (ZMQ pub/sub)
   │          │           │           │             │                 │
   └──────────┴───────────┴─────── eds.dsp ─────────┘           contracts/ (JSON şema)
                              │
                       eds.hal (RxDevice, SignalSource)
     ┌───────────────┬────────┴───────┬──────────────┬───────────────┬──────────────┐
 soapy_backend   pluto_iio (ince)  group (N×1ch)  proc (süreç      sim (fizik      tx / tx_hw
 (SoapyPlutoSDR) xo_correction,    2×Pluto        yalıtımı: Pluto  tabanlı AD9363) (uyaran:
                 tracking, sıcaklık,              başına bir alt   CI + demo +     sim, Pluto TX,
                 DMA overflow yazmacı             süreç)           ölçüm doğrul.   SCPI, manuel)
```

* **Akış ve cihaz soyutlaması SoapySDR** üzerindendir (`eds.hal.soapy_backend`).
  Pluto'ya özgü alçak seviye kontroller (PPM için `xo_correction`, BB/RF DC ve quadrature
  tracking, hızlı AGC modları, sıcaklık, RX DMA overflow yazmacı) SoapyPlutoSDR'de API'si
  olmadığı için `eds.hal.pluto_iio` ince libiio katmanından yapılır; Soapy'de karşılığı olan
  (BB-DC/IQ-balance modu, `setFrequencyCorrection`, `slow_attack`) önce Soapy ile denenir.
  pyadi-iio **birincil arayüz değildir**.
* SoapyPlutoSDR süreç başına **tek** libiio bağlamı tutar; bu yüzden bir süreçte en fazla
  bir Pluto açılır, fazlası `eds.hal.proc` ile kendi alt sürecinde çalışır (§5).
* Kraken/bladeRF/USRP: yeni profil TOML + (gerekirse) yeni backend dosyası. Üst katmanlar
  yalnızca `RxDevice` görür (`num_channels`, `coherent`).

## 2. Konvansiyonlar (tüm modüller UYMAK ZORUNDA)

| Konu | Kural |
|---|---|
| Birimler | Frekans **Hz**, zaman **s** (`*_ns` ile biten int nanosaniye). |
| Örnekler | `complex64`, şekil `(n_ch, n)`, **tam ölçek = 1.0** (`I = counts/2048`). |
| 0 dBFS | Genliği tam ölçek olan **kompleks üstel**in gücü: `mean(|x|²) = 1.0`. |
| Kırpma | `max(|I|,|Q|) >= clip_level` (= 2047/2048, `eds.dsp.levels.DEFAULT_CLIP_LEVEL`). |
| PSD ölçekleme | **Güç spektrumu (spektrum analizörü)**: `P[k] = |Σ w x e^{-j..}|² / (Σw)²`. Bin merkezindeki tonun gücünü doğru okur; gürültü **RBW = ENBW·fs/N** bandındaki gücü okur. Yoğunluk `dB/Hz = P − 10log10(RBW)`. |
| Bant gücü | `band_power = Σ P[k] / ENBW_bins` (`ENBW_bins = N·Σw²/(Σw)²`). Scalloping'den bağımsız; ton gücü/kanal gücü için bunu kullanın. |
| Frekans ekseni | `np.fft.fftshift` sonrası: `f[k] = fc + (k − N//2)·fs/N`, k=0..N−1 (DC = indeks `N//2`; tek N için de geçerli). |
| dBm | `dBm = dBFS + offset(f, G)`; `offset ≈ C(f) − G` (kalibrasyon tablosu). Kalibrasyonsuz veri `unit="dBFS"`, `calibrated=false`. |
| PPM işareti | `ppm = (f_ref_gerçek − f_ref_nominal)/f_ref_nominal·1e6` (pozitif = TCXO hızlı). `e = ppm·1e-6`. AD936x'te LO ve örnekleme saati aynı referanstan türediği için **görünen RF frekansı** `f_app = f_true/(1+e)` ⇒ **düzeltme** `f_true = f_app·(1+e)`; tam `f`'ye ayarlamak için `fc_cmd = f/(1+e)`. Donanım düzeltmesi: `xo_correction = ref_clock_hz·(1+e)` (Pluto'da tamsayı Hz; `ppm_hw` **geri okunan** değerden hesaplanır). Ardışık düzeltmeler çarpımsal birleşir: `(1+e) = (1+e_hw)(1+e_artık)`. |
| Kazanç | dB, cihaz "hardwaregain" (Pluto banda bağlı: < 1.3 GHz −1..73, 1.3–4 GHz −3..71, profil `gain_bands`). Profil aralığı yetkilidir (SoapyPlutoSDR'nin sabit 0..73'ü yalnızca `gain_db_driver` olarak raporlanır). `soft_agc` HAL'de `manual` olarak uygulanır, yazılım AGC'si (`eds.rx.agc`) yönetir. Ölçüm kalitesinde güç için HW AGC kapalı olmalı (örnekle senkron kazanç bilgisi yok). |
| Boşluk/settling | `RxDevice.read()` **iç boşluklu blok döndürmez**: boşluk (overflow, okuma zaman aşımı) varsa blok `overflow=True` taşır ve boşluk **bloğun önündedir**. Retune/kazanç değişimi sonrası `settle_samples` örnek `settling=True`; `capture()`/`flush()` bunları atar. |
| Zaman | Bus mesajları `time.time_ns()` (duvar saati); süreler `perf_counter`. `IQBlock.t_ns` = ilk örneğin tahmini host zamanı (okumanın **dönüş** anından geriye; cihaz FIFO/USB süresi dahil değildir). |
| Açılar | Derece; `wrap_deg` (−180, 180]. DF azimutu: dizi eksenine dik (broadside) = 0°, **dizi ekseni boyunca 2. elemana doğru pozitif** (bkz. eds.df). |
| Süreç başına Pluto | Bir süreçte en fazla **bir** SoapyPlutoSDR cihazı (RX ya da `pluto_tx`) açıktır (`claim_pluto`); ikincisi `DeviceError`. Fazlası `[[device]] isolation` / `source_args.isolate` ile alt süreçte (§5). |
| Uyaran tavanı | Ölçüm prosedürleri RX girişini `DeviceProfile.rx_max_input_dbm`'in (AD9363: −5 dBm) üstüne sürmez (`eds.measure.stimulus.rx_max_input_dbm`, `source_power_range`, `relative_setting_cap`, `adjust_tone`). Bilinen istisna: `latency`'nin uyarlamalı ton seviyesi yalnız kaynağın `power_range_db`'siyle sınırlıdır (MEASUREMENTS §0.4). |
| Koşulsuz sayı yok | `MeasurementResult.blockers` (Türkçe nedenler) boş değilse, sonuç hızlı modda (`conditions["quick"]`), gerçek zamanlı olmayan simülatörde ya da birim/kalibrasyon uyuşmazlığında **hedef kararı verilmez** (`passed=None`, rapor "KARŞILAŞTIRILMADI"). `run_measurement` her sonuca `conditions["quick"]` ve `conditions["simulated"]` yazar. |
| Hatalar | Yalnızca `eds.core.errors` alt sınıfları fırlatılır (bus'ın kendi hataları da `EDSError` alt sınıfıdır). |
| Dil | Kod/docstring İngilizce; kullanıcıya dönük metin (CLI, rapor, profil ihlalleri, uyarılar) Türkçe. |

## 3. Modül arayüzleri

### 3.1 `eds.core`

```python
# types
IQBlock(samples (n_ch, n) complex64, fs, fc, t_ns, index, seq, gain_db: tuple, overflow=False,
        settling=False, clip_level=2047/2048, meta: dict)   # bitişik blok; index = ilk örneğin akış indeksi
    .n .n_channels .duration_s .t_end_ns ; .channel(ch=0) -> np.ndarray
    # meta: ppm_hw, ppm_hw_via, gain_mode, bw (+ hw_time_ns, ppm_hw_members grup için)
StreamStats(blocks, samples, overflows, timeouts, discarded_samples, settling_blocks, host_drops,
            start_mono, last_mono)  .elapsed_s .throughput_sps .to_dict()
DeviceInfo(name, backend, driver, hardware, serial, firmware, uri, profile, extra: dict[str,str]) .to_dict()
GainRange(min_db, max_db, step_db=1.0) .clamp(g)            # [min,max]'a kırp, min'e sabitli ızgaraya kuantala
# config  — öncelik: varsayılan < TOML < CLI (--set noktalı anahtar); bilinmeyen anahtar = ConfigError
DeviceConfig(name="rx0", backend="sim"|"soapy", profile="ad9363_pluto", args: dict, control_uri="" ("" = otomatik,
             "none" = kapalı), channels=[0], sim: dict, isolation="auto"|"inproc"|"subprocess")
RxConfig(center_freq_hz=915e6, sample_rate_hz=4e6, bandwidth_hz=4e6, gain_mode="manual", gain_db=40,
         buffer_size=65536, block_size=65536, settle_buffers=-1 (= kernel_buffers+1), stream_format="CS16",
         dc_tracking, rf_dc_tracking, quad_tracking=True, read_timeout_s=2.0, queue_blocks=32)
DspConfig(nfft=4096, window="blackmanharris", overlap=0.5, averages=16, avg_mode="mean",
          usable_fraction=0.8, dc_notch_bins=3)
DetectConfig(method="os_cfar"|"ca_cfar"|"fixed", pfa=1e-6, guard_bins=4, train_bins=32, os_rank=0.75,
             min_snr_db=6, merge_gap_bins=2, min_bw_bins=1, fixed_threshold=-80, exclude_dc=True,
             resolutions=[1, 16, 128])
AgcConfig(target_peak_dbfs=-10, high_peak_dbfs=-3, low_peak_dbfs=-35, max_clip_fraction=1e-5,
          attack_step_db=10, decay_step_db=3, hold_blocks=4)
SweepConfig(start_hz=325e6, stop_hz=3.8e9, sample_rate_hz=20e6, bandwidth_hz=20e6, nfft=2048, rbw_hz=0,
            averages=4, window, gain_mode="fixed"|"auto", gain_db=50, usable_fraction=0.8, dc_avoid=True,
            settle_buffers=-1, publish_max_points=8192)
BusConfig(mode="direct"|"proxy", pub_endpoint, sub_endpoint, proxy_frontend, proxy_backend, ctl_endpoints,
          validate=False, status_period_s=1.0, spectrum_rate_hz=10, spectrum_max_bins=4096, hwm=200)
CalConfig(dir="", ppm_mode="auto"|"hardware"|"software"|"off", ppm_override=nan, use_power_cal=True,
          temp_warn_delta_c=5.0)
MeasureConfig(out_dir="", targets_file="ktr_targets.toml", source="sim", source_args: dict,
              params: dict[spec_id, dict])
SimConfig(emitters: list[dict], temperature_c=35.0, seed=1234)
AppConfig(devices, rx, dsp, detect, agc, sweep, bus, cal, measure, sim, source_path) .device(name=None) .to_dict()
config_from_dict(raw, source) -> AppConfig ; apply_overrides(raw, overrides) -> dict  # "device.X" → device[0]
load_config(path=None, overrides=None) -> AppConfig
# profile
GainBand(f_min_hz, f_max_hz, gain_min_db, gain_max_db, step_db=1.0)
DeviceProfile(name, chip, ..., freq/bw/sample_rate min/max, ref_clock_hz=40e6, ref_clock_ppm=25,
              interface, interface_max_bytes_per_s, interface_practical_bytes_per_s, bytes_per_sample=4,
              kernel_buffers=4, rx_max_input_dbm=inf, default_buffer_size, candidate_sample_rates_hz,
              gain_bands, notes, extra)
    .gain_range(freq_hz=None) -> GainRange                    # banda bağlı
    .wire_rate_bytes_per_s(fs, n_channels=1) -> float
    .check_rx(center_freq_hz, sample_rate_hz, bandwidth_hz, gain_db, n_channels) -> (errors, warnings)  # Türkçe, fırlatmaz
    .validate_rx(**kw) -> warnings                            # hata varsa ProfileViolation (tüm ihlaller)
profile_from_dict(d, source) ; load_profile(name_or_path) ; list_profiles()
# errors (hepsi EDSError)
ConfigError, ProfileViolation(violations), DeviceError, DeviceNotFound, BackendUnavailable, StreamError,
StreamTimeout, NotSupported, BusError, CalibrationError, MeasurementError
# units / timeutil / paths
KT_DBM_HZ=-173.975, C_LIGHT ; db10 db20 lin10 lin20 dbm_to_watt watt_to_dbm wavelength fmt_hz wrap_deg wrap_rad
now_ns() mono() iso_utc(t_ns) stamp(t_ns) Stopwatch(totals, counts) .time(name) .mean(name)
config_dir() profiles_dir() contracts_dir() cal_dir() reports_dir()
```

### 3.2 `eds.hal`

```python
# base — RxDevice sözleşmesi (paket kökünden: RxDevice, RawRead, GAIN_MODES, open_*, enumerate_devices, get_sim_world)
GAIN_MODES = ("manual", "hw_slow", "hw_fast", "hw_hybrid")
RawRead(samples (n_ch, k) complex64, overflow_before=False, timeout=False, t_ns=0, hw_time_ns=None,
        uncertain_samples=0)          # backend _read_raw dönüşü; uncertain_samples: boşluk bu kadar örnek içinde bir yerde
RxDevice(cfg: DeviceConfig, profile: DeviceProfile)                    # soyut; backend_name
    # durum: info, stats, warnings, fc, fs, bw, gain_mode, gains, buffer_size, settle_buffers,
    #        ppm_hw, ppm_hw_via ("hardware"|"soapy"|"none")
    .num_channels .coherent .name .streaming .settle_samples (= settle_buffers × buffer_size)
    .configure(rx: RxConfig) -> list[str]          # ÖNCE tüm ayarlar profile karşı, sonra uygula; uyarılar
    .set_center_freq(hz) / .set_sample_rate(hz) / .set_bandwidth(hz) -> float   # profil kontrolü + settling işareti
    .set_gain(db, channel=None) -> float           # banda göre kırp/kuantala; gerçek kazanç
    .set_gain_mode(mode) ; .gain_range(freq_hz=None) -> GainRange   # profil ∩ runtime_limits()["gain_db"]
    .mark_settling(n_samples=None)                  # sonraki n örnek (vars. settle_samples) settling
    .start_stream(buffer_size=None) ; .stop_stream()
    .read(n=None, timeout_s=2.0) -> IQBlock         # TAM n bitişik örnek; iç boşluk yok (yukarıdaki kural)
    .flush(timeout_s=2.0) -> int                    # settling penceresi geçene kadar oku-at
    .capture(n, timeout_s=2.0, allow_overflow=True) -> IQBlock   # tek seferlik settled yakalama (akışı başlatır)
    .close() ; .describe() -> dict
    # isteğe bağlı (varsayılanları):
    .set_tracking(dc=None, rf_dc=None, quadrature=None) -> {özellik: "soapy"|"iio"|"sim"|"unsupported"}
    .set_ppm_correction(ppm) -> "hardware"|"soapy"|"none"   # ppm_hw / ppm_hw_via'yı ayarlar
    .read_sensors() -> dict ("temperature_c", "rssi_db", ...) ; .runtime_limits() -> dict
    # backend ilkelleri: _apply_sample_rate/_bandwidth/_center_freq/_gain/_gain_mode, _start, _read_raw, _stop, _close
# factory
ISOLATION_MODES = ("auto", "inproc", "subprocess")
get_sim_world(app) -> SimWorld                      # AppConfig başına tek paylaşılan sahne (config nesnesine iliştirilir)
is_pluto_config(dev) -> bool                        # soapy + driver (args ya da profil soapy_driver) == "plutosdr"
pluto_tx_in_process(app) -> bool                    # measure.source="pluto_tx" ve isolate=false
isolation_plan(app, devices=None) -> {ad: "inproc"|"subprocess"}   # §5 karar tablosu
open_device(dev, app=None) -> RxDevice              # dev.isolation="subprocess" → ProcessRxDevice; "auto" tek başına = süreç içi
open_devices(app, names=None) -> list[RxDevice]     # plana göre yerleştirir; hata olursa açılanları kapatır
open_group(app, names=None) -> RxDevice             # 1 cihaz → kendisi; çok → DeviceGroup
enumerate_devices(backend="soapy", args=None) -> list[dict]
# proc — süreç yalıtımı (§5)
ProcessRxDevice(cfg, app=None, *, opener=None) (RxDevice, backend_name="proc")  .pid .alive
    # base mantığı üst süreçte; ilkeller + set_tracking/set_ppm_correction/read_sensors/runtime_limits/describe RPC
ProcessSignalSource(target: "modül:Sınıf" | type, args=(), kwargs=None) (SignalSource)  .pid .alive
    # set_frequency/set_power/set_enabled/set_tones/set_lo_offset/describe/close iletilir; name, absolute_power,
    # max_tones, power_range_db, freq_range_hz yansıtılır; diğer public öznitelikler erişimde alt süreçten okunur
pluto_tx_isolated(args) -> bool                     # source_args["isolate"] (vars. True; bool/0/1/true/false/evet/hayır)
app_to_plain(app) / app_from_plain(d) ; target_path(t) / resolve_target(p) ; pack_error(e) / rebuild_error(rec)
RemoteTraceback(tb) ; DEFAULT_OPENER = "eds.hal.factory:open_device"
# soapy_backend
SoapyRxDevice(cfg, profile, *, stream_format="CS16"|"CF32", detect_ts_gaps=True, overflow_poll_s=0.02)
    .is_pluto .thin_uri() -> str .thin_layer() -> PlutoIIO | None    # ince katman: ilk ihtiyaçta, seri doğrulamalı
    .runtime_limits()          # plutosdr: sürücünün sabit 0..73'ü yalnız "gain_db_driver" (profil yetkili)
    .set_tracking(...)         # BB-DC / quadrature: Soapy DC-offset / IQ-balance modu varsa "soapy", yoksa (Pluto)
                               #   ince katman "iio"; RF-DC her zaman ince katman; hiçbiri → "unsupported" + uyarı
    .set_ppm_correction(ppm)   # 1) Pluto xo_correction "hardware" (ppm_hw = geri okunan tamsayı Hz'den) 2) Soapy
                               #    setFrequencyCorrection "soapy" (yalnız artığı) 3) "none"
    .read_sensors()            # Soapy sensörleri + temperature_c / rssi_db (ince katman ya da Soapy sıcaklık sensörü)
    # öznitelikler: overflow_source ("driver"|"dma_status"|"none"), dma_overflow_events, native_format(_source),
    #   stream_mtu, xo_boot_hz, xo_correction_hz, thin_serial_verified, agc_mode_actual, ts_gap_events
STREAM_FORMATS, PLUTO_DRIVER="plutosdr", PLUTO_DEVICE_ARGS={driver,uri,hostname,label,serial},
PLUTO_STREAM_ARGS={bufflen}, STREAM_ARG_PREFIX="stream:", DEFAULT_OVERFLOW_POLL_S=0.02
import_soapy() kwargs_to_dict(kw) release_device(sdr, dev) -> "deleter"|"unmake"|"dropped"
xo_ppm(xo_hz, ref_clock_hz) = (xo − ref)/ref·1e6 ; is_pluto_args(args)
claim_pluto(owner) -> token ; release_pluto(token) ; pluto_claim_for(obj, owner) -> release ; open_pluto_owners()
quarantine_pluto_device(dev)              # paylaşılan bağlamı yok etmemek için ikinci örneği asla serbest bırakma
enumerate_soapy(args=None) ; entry_serial(entry) ; fill_serials_from_libiio(found) -> (found, note)
select_by_serial(found, serial, *, name="", note="") ; resolve_serial_args(args, *, name="")  # serial → uri
unknown_pluto_args(device_args, stream_args) -> str   # SoapyPlutoSDR'nin yok saydığı argümanlar için Türkçe uyarı
parse_native_format(res) -> (format, fullScale)
# pluto_iio — ince libiio katmanı (pylibiio yoksa BackendUnavailable)
PlutoIIO(uri)  .closed .xo_boot_hz
    .get_xo_correction() -> int ; .set_xo_correction(hz:int) -> int (geri okunan)
    .rx_overflow_supported() -> bool ; .read_rx_dma_status() -> int ; .clear_rx_overflow()
    .read_clear_rx_overflow() -> bool              # cf-ad9361-lpc 0x80000088 bit 2 (W1C), §6
    .set_tracking(bb_dc=None, rf_dc=None, quad=None) -> dict ; .get_tracking() -> dict
    .gain_control_mode (get/set) ; .gain_control_modes_available() ; .hardwaregain_available() -> (min, step, max)
    .temperature_c() ; .rssi_db() ; .firmware_version() ; .serial() ; .refclk_info() (salt okunur) ; .close()
scan_serials() -> {uri: serial}           # iio.scan_contexts() ("iio_info -s")
PHY_NAME="ad9361-phy", RX_DMA_DEVICE="cf-ad9361-lpc", ADC_DMA_STATUS_REG=0x80000088, ADC_DMA_STATUS_OVERFLOW=0x4
import_iio() attr_text(v) parse_range(text) parse_number(text)
# group
DeviceGroup(devices, *, assume_coherent=False, read_margin_s=5.0) (RxDevice, backend_name="group")
    # num_channels = Σ, üye sırasıyla; coherent = assume_coherent; profil = üye profili (rx_channels = Σ,
    # arayüz sınırları × N); read(n) her üyeyi kendi kalıcı thread'inde eşzamanlı okur
    .members .worker_threads .last_skew_ns .detach()
    .set_ppm_correction(ppm | list) -> ortak yol | "mixed" ; .set_tracking(...) -> ortak yol | "mixed"
    .read_sensors() -> "<üye>.<anahtar>" ; .runtime_limits() (kesişim) ; .describe()["members"]
    # herhangi bir üyede overflow / başarısız okuma → grup bloğu overflow=True; üyenin belirsiz aralığı → settling
# tx — uyaran sözleşmesi
SignalSource (soyut): name, absolute_power, max_tones, power_range_db, freq_range_hz
    .set_frequency(hz) -> float ; .set_power(dbm) -> float (mutlak dBm @ DUT girişi | bağıl dB, 0 = maks.)
    .set_enabled(on) -> int (RF'nin etkili olduğu tahmini host time_ns) ; .set_tones(freqs, powers) (isteğe bağlı)
    .set_lo_offset(hz) -> float                  # isteğe bağlı; varsayılan NotSupported (dahili LO'su olmayan kaynak)
    .describe() ; .close() ; context manager (çıkışta RF kapalı + close)
open_source(kind: "sim"|"pluto_tx"|"scpi"|"manual"|"none", args=None, *, world=None) -> SignalSource | None
    # pluto_tx → ProcessSignalSource("eds.hal.tx_hw:SoapyTxSource") (args["isolate"]=False: süreç içi)
# tx_hw
SoapyTxSource(args=None, fs=2.4e6, tone_offset_hz=200e3, path_loss_db=0, anchor_dbm=None, buffer_len=8192,
              *, channel=0, write_timeout_s=0.1, swap_timeout_s=2.0)    # name="pluto_tx", max_tones=2
    # tek ton: LO = f − tone_offset_hz; iki ton: LO orta noktada; güç bağıl (TX kazancı + dijital ölçek),
    # anchor_dbm ile mutlak: P_dut = anchor_dbm − path_loss_db + P_rel
    .set_lo_offset(hz) -> float   # LO = f − hz (tampon döngüsüne kuantalanır, 0 < |hz| ≤ 0.4·fs), RF ton korunur
    .estimated_queue_latency_s    # libiio TX kuyruğunun boşalma süresi (ölçümlerin bekleme payı)
    # describe(): lo_hz, lo_offset_hz, tones_hz, ... ; underflows
ScpiSignalGenerator(host, port=5025, timeout_s=3.0, path_loss_db=0, *, readback=True)  .write(cmd) .query(cmd)
ManualSource(prompt=input, absolute_power=True, path_loss_db=0)     # operatör; her adımda Enter
build_tone_buffer(freqs_bb, amps, fs, n) -> (samples, actual_freqs)  # her ton tamsayı periyot (faz sürekli döngü)
quantize_tone_freq(f, fs, n) ; DIGITAL_FULL_AMPLITUDE=0.5, DIGITAL_RANGE_DB=60, MAX_BASEBAND_FRACTION=0.4
# sim — §4
SimWorld, Emitter, Scene (sim.world) ; SimRxDevice(cfg, profile, world) (sim.device) ; SimSignalSource (sim.source)
```

`SoapyRxDevice` ve `SimRxDevice` arka uca özgü öznitelikleri (`overflow_source`,
`thin_layer()`, `true_ppm` ...) `ProcessRxDevice` üzerinden **erişilemez**; değerleri yalnızca
`describe()` çıktısında görünür (§5).

### 3.3 `eds.dsp`

```python
# windows
WINDOW_NAMES = ("rect", "hann", "hamming", "blackman", "blackmanharris", "flattop") ; canonical_window_name(name)
get_window(name, n) -> np.ndarray                 # float64, periodic (DFT-even), önbellekli + salt okunur (≤ 64 MB LRU)
window_info(name, n) -> WindowInfo(coherent_gain, enbw_bins, scallop_loss_db, sidelobe_db)
bin_correlation(name, n, max_lag) -> np.ndarray   # beyaz gürültüde |X[k]|² ile |X[k+d]|²'nin korelasyonu ρ(d)
# psd
Spectrum(f_hz (n,), p_lin (n,) güç spektrumu, fc, fs, nfft, window, enbw_bins, averages, unit="dBFS", t_ns=0,
         gain_db=(), overflow=False, meta: dict)  # meta: dc_notch_bins, usable_fraction, overlap, avg, block_meta, ...
    .n_bins .df_hz .rbw_hz (= enbw_bins·df) .f_rel_hz .dc_offset_bins .p_db/.to_db() .density_db_hz
    .noise_floor_db (kullanılabilir merkez bant, DC çentiği hariç medyan) .usable_bins()
    .band_power(f_lo, f_hi) -> lin (bin yoksa MeasurementError) .band_power_db(f_lo, f_hi)
    .slice(f_lo, f_hi) -> Spectrum ; .with_offset_db(off, unit=None) -> Spectrum (skaler ya da bin başına)
    .with_freq_correction(ppm) -> Spectrum        # f, fc ve fs × (1+e); meta["ppm_applied"] birikir
    .copy()
bin_frequencies(fc, fs, nfft) ; NFFT_MIN=64, NFFT_MAX=2**22, AVG_MODES=("mean","max","min","median")
welch(x, fs, nfft, window="blackmanharris", overlap=0.5, avg="mean", fc=0.0, t_ns=0) -> Spectrum
spectrum_from_block(block, nfft, window="blackmanharris", overlap=0.5, avg="mean", channel=0) -> Spectrum
nfft_for_rbw(rbw_hz, fs, window="blackmanharris") -> int   # ENBW·fs/N ≤ rbw olan en küçük 2'nin kuvveti
# peaks
interp_peak(p_db, k) -> (delta_bins, peak_db)   # dB'de parabolik
find_peaks(spec, n=5, min_sep_bins=3, exclude_dc_bins=0, threshold_db=None) -> list[Peak(f_hz, p_db, bin, delta)]
tone_frequency(x, fs, fc=0.0, f_hint=None, search_hz=None, zero_pad=4, window="blackmanharris") -> float
# stats
regularized_gamma_p(k, x) / regularized_gamma_q(k, x) ; gamma_quantile(p, k) ; gamma_isf(q, k)
cfar_alpha(pfa, n_avg) -> float                   # bilinen ortalamalı gürültü için çarpan
ca_cfar_alpha(pfa, n_avg, n_train, *, cut: CellModel | None = None) -> float   # sonlu-M tam düzeltme
os_cfar_alpha(pfa, n_avg, n_train, k, *, cut: CellModel | None = None) -> float
os_cfar_scale(n_avg, n_train, k) ; os_rank_index(rank, n_train)
effective_averages(n_segments, overlap, window, nfft) -> float      # Welch eşdeğer bağımsız ortalama
CellModel(weights, shape) .mean ; weighted_gamma_sf(x, weights, shape)   # Σ w_i·Gamma(shape) kuyruğu (saddlepoint)
# levels
iq_levels(x, clip_level=2047/2048) -> IQLevels(peak_dbfs, rms_dbfs, clip_fraction, dc_dbfs, n) .headroom_db .crest_db
# detect — çok çözünürlüklü CFAR (ayrıntı: modül docstring)
Detection(f_center_hz, f_peak_hz, f_lo_hz, f_hi_hz, bw_hz, peak_db, band_power_db, snr_db, noise_db, n_bins) .to_dict()
detect(spec, cfg: DetectConfig, n_avg=None, valid_mask=None) -> (list[Detection], threshold_db (native geçiş))
usable_mask(n, usable_fraction, dc_notch_bins) -> np.ndarray[bool]   # notch < 0: çentik yok; 0: yalnız DC bini
default_n_avg(spec) ; effective_training_cells(window, nfft, guard_bins, train_bins) -> M_eff
estimate_noise(spec, cfg, n_avg=None, *, pfa=None) -> NoiseEstimate(noise_lin, alpha, n_eff, m_cells, m_eff, rank)
coarse_statistics(window, nfft, d, train_bins) -> (D_eff, M_eff)
coarse_cell_model(window, nfft, d, n_eff) -> CellModel      # D-bin blok ortalamasının tam dağılımı
resolution_passes(spec, cfg, n_avg=None, valid_mask=None) -> list[ResolutionPass]   # tanılama
```

`detect` özet: her bin için eğitim hücrelerinden gürültü ortalaması (OS: `k = round(os_rank·M)`
sıra istatistiği / `c_k`; CA: ortalama), çarpan pencere korelasyonunu hesaba katan `M_eff` ile
sonlu-M tam düzeltmeli; `threshold = max(α·noise, noise·10^(min_snr_db/10))`. `resolutions`
içindeki her `D > 1` için D bin güç ortalamalı kaba spektrumda ayrı geçiş (eğitim hücreleri
`Gamma(n_eff·D_eff)` ile, test hücresi `coarse_cell_model` ile tam modellenir); tüm geçişler
`pfa / n_geçiş` (Bonferroni) kullanır, geniş bant tespitler −3 dB kenarlarına (50 % noktaları)
inceltilir ve ince geçişlerin bulanık kopyaları birleştirmede elenir. DC çentiği genişliği
`spec.meta["dc_notch_bins"]` (vars. 3). Tespitler `(f_lo_hz, f_hi_hz)` sırasında döner.

### 3.4 `eds.rx`

```python
# stream
RxStreamer(device, block_size, queue_blocks=32, on_block=None, read_timeout_s=2.0, max_consecutive_timeouts=3)
    .start() ; .stop(timeout_s=None)   # uçuştaki okumayı en fazla timeout_s (vars. read_timeout_s + 1) bekler;
                                       # akışı bu streamer başlattıysa durdurur (Pluto: takılı refill libiio'nun 5 s'si)
    .get(timeout_s=1.0) -> IQBlock | None ; .drain() -> list ; .check() (okuyucu hatayla durduysa StreamError)
    .running .error .host_drops .blocks_read .timeouts .consecutive_timeouts .queued .stats
    # ayrı thread device.read(); kuyruk dolarsa EN ESKİ blok atılır (host_drops++); on_block okuyucu thread'de
# agc
GainController(device, cfg: AgcConfig, history_len=1024)
    .update(block) -> float | None     # yeni kazanç uygulandıysa döner; settling / eski kazançlı blokları yok sayar
    .settle(max_iter=8, n=16384, timeout_s=2.0) -> float ; .propose(levels, gain_db, *, one_shot=False)
    .reset() .last_levels .history .low_count .gain_db ; last_settle_status
validate_agc_config(cfg) ; classify_levels(levels, cfg) -> "attack"|"low"|"ok" ; gain_delta_db(levels, cfg, *, one_shot)
# sweep
Sweeper(device, cfg: SweepConfig, dsp: DspConfig, detect: DetectConfig,
        calibrate: Callable[[Spectrum], Spectrum] | None = None, agc: AgcConfig | None = None, *, timeout_s=2.0)
    .plan() -> list[float] ; .run(on_step=None) -> SweepResult ; .abort()   (run() MeasurementError fırlatır)
    .nfft .df_hz .rbw_hz .step_hz .usable_half_bins .dc_notch_bins .n_capture .buffer_size .n_bins .effective_averages
SweepResult(f_hz, p_db, unit, calibrated, rbw_hz, n_steps, step_hz, steps, duration_s, rate_hz_per_s,
            timing{tune_s, settle_s, capture_s, dsp_s, other_s}, detections, t_start_ns, t_end_ns, sweep_id,
            start_hz, stop_hz, df_hz, fs_hz, nfft, window, enbw_bins, averages, gain_ref_db, gain_mode,
            valid, source_step, threshold_db)
    .decimate(max_points, mode="max"|"mean"|"min") -> (f, p) ; .decimation_for(max_points) ; .to_spectrum()
    .band_power_db(f_lo, f_hi) ; .noise_floor_db ; .clipped_steps ; .to_bus_payload(device_name, max_points=8192)
plan_offsets(...) ; plan_coverage(...) ; gain_referred_unit(gain_ref_db) -> "dBFS@<G>dB"
# node
RxNode(app, device=None, publisher=None, cal=None, *, name=None, ctl_subscriber=None, on_publish=None)
    .start() ; .run(duration_s=None) ; .stop() (idempotent, her thread'den)
    .handle_command(cmd) -> ack | None ; .sweep_config(overrides=None) -> SweepConfig ; .calibrate_spectrum(spec)
    .status_payload() ; .publish_status() ; .stream_counters()
    .state ("idle"|"streaming"|"sweeping"|"error") .gain_mode .ppm_via .warnings .stats .window_samples
    .overflow_source (cihazın overflow_source özniteliği; yoksa None) .running .control_enabled .device .publisher .calibration
WindowAccumulator(n) .push(blk) .pop() -> Window | None .reset(gap=False) .available  # bitişik, örtüşmesiz pencereler
Window(samples, fs, fc, t_acq_ns, t_acq_end_ns, seq, index, gain_db, gap_before, clip_level, meta)
decimate_max(p_db, max_points) -> (p, D)
COMMANDS = ("ping", "set_center_freq", "set_gain", "set_gain_mode", "set_sample_rate", "set_bandwidth", "sweep", "stop")
NODE_GAIN_MODES = ("manual", "soft_agc", "hw_slow", "hw_fast", "hw_hybrid")
```

Düğüm: cihaz → `RxStreamer` (soft_agc'de `GainController.update` okuyucu thread'de) → settling
blokları atlanır → bitişik örnekler `welch_len(nfft, averages, overlap)` uzunluğunda örtüşmesiz
pencerelere toplanır (overflow, host drop, fc/fs/kazanç/ppm değişiminde sıfırlanır) → Welch →
kalibrasyon (`apply_calibration`, ppm ekseni + güç tablosu) → `detect` → her pencere için
`rx.detections`, en fazla `spectrum_rate_hz` ile `rx.spectrum`; `t_acq_ns = t_acq_end_ns − n/fs`.
Her `status_period_s`'de `rx.status` (cihaz `overflow_source = "none"` ise overflow sayacı
"bilinmiyor"dur). `sweep` komutu akışı durdurur, `Sweeper` çalıştırır, `rx.sweep` yayınlar,
RX ayarlarını geri yükleyip akışı yeniden başlatır (hata → `error` + yeniden deneme döngüsü).

### 3.5 `eds.calib`

```python
# ppm
PpmEstimate(ppm, std_ppm, f_measured_hz, f_ref_hz, snr_db, n, method, temperature_c=None, t_ns=0, fc_nominal_hz=0,
            fs_hz=0, ppm_residual=0, ppm_hw=0, repeats=1, extra={})  .e .to_dict() .from_dict(d) .summary_tr()
estimate_ppm_cw(device, f_ref_hz, offset_hz=100e3, n=2**18, repeats=5, gain_db=None, *, channel=0, min_snr_db=15,
                ref_clock_ppm=None, timeout_s=2.0) -> PpmEstimate
    # TOPLAM ppm döner: ölçülen artık ⊗ etkin donanım düzeltmesi (idempotent); RX ayarlarını geri yükler
estimate_ppm_from_samples(x, fs, fc_nominal, f_ref_hz, ref_clock_ppm=25.0) -> float
compose_ppm(a, b) ; residual_ppm(total, hw) ; ppm_from_apparent(f_true, f_app) ; correct_freq(f_app, ppm) -> f_true
tune_freq(f_target, ppm) -> fc_cmd ; xo_correction_hz(ppm, ref_clock_hz=40e6) ; channel_ppm_hw(device, channel, block)
preserve_rx_settings(device) (context manager) ; tone_snr_db(...) ; noise_bin_mean(spec) ; search_halfwidth_hz(...)
PPM_METHODS = ("cw", "gsm_fcch", "manual")
# fcch
estimate_ppm_gsm_fcch(device, carrier_hz (ya da ARFCN < 2048), n_bursts=10, fs=1e6, max_capture_s=5.0, *, band=None,
                      gain_db=None, channel=0, ref_clock_ppm=None, frame_s=256e-6, chunk=65536, timeout_s=2.0)
                      -> PpmEstimate                    # FCCH tonu: fc + 1625/24 kHz (+67.708 kHz)
arfcn_to_freq(arfcn, band="GSM900") -> float ; FcchBurst(...)
# power
PowerCalTable(points: [(f_hz, gain_db, offset_db)], meta=None)
    .offset_db(f, G) (dBm = dBFS + offset; bilineer, ölçülmemiş kazançta −G eğimi) .to_dbm(p, f, G)
    .points .freqs_hz .gains_at(f) .to_dict() .from_dict(d) .from_model(c0_db, freqs=None) .fit_from_measurements(rows)
measure_power_cal(device, source, freqs_hz, gains_db, p_in_dbm, n=65536, offset_hz=200e3, *, channel=0, nfft=4096,
                  min_snr_db=20, max_peak_dbfs=-1, timeout_s=2.0) -> PowerCalTable
measure_tone_dbfs(x, fs, fc, f_tone_hz, half_search_hz, nfft=4096) -> (power_dbfs, snr_db, f_peak_hz)
# store
DeviceCalibration(serial, profile="", ppm=None, power=None, image_rejection_db={}, coherence={}, notes=[], updated_ns=0)
    .is_empty .temperature_c .add_note(text) .to_dict() .from_dict(d)       # dosya "format": 1
CalibrationStore(dir=None → cal/)  .load(serial) (yoksa boş) .save(cal) -> Path (atomik) .exists(serial) .list() .path_for(serial)
# apply
apply_calibration(spec, cal, ppm_mode, ppm_hw=None) -> Spectrum   # kopya; ppm_hw None → spec.meta["block_meta"]
apply_ppm_to_device(device, cal, ppm_mode) -> "hardware"|"soapy"|"software"|"off"   # rx.status'ta "off" → "none"
check_temperature(cal, device, warn_delta_c=5.0) -> str | None
make_cal_payload(cal_kind, device, values, applied_via, *, t_ns=None, temperature_c=None, serial=None) -> dict
PPM_MODES = ("auto", "hardware", "software", "off") ; CAL_KINDS = ("ppm", "power", "image_rejection", "coherence", "iq")
```

### 3.6 `eds.df`

```python
# coherence  (konvansiyon: x1[n] ≈ x0[n − delay])
estimate_delay(x0, x1, fs=None, max_lag=None) -> (delay_samples (kesirli), peak_corr)
estimate_delay_ex(x0, x1, fs=None, max_lag=None) -> DelayEstimate(delay_samples, peak_corr, integer_lag,
                  delay_std_samples, rms_bw_norm, resolvable, n, fs)  .delay_s   # dar bantta resolvable=False
phase_offset(x0, x1) -> (phase_rad, coherence 0..1) ; align(x1, delay) ; valid_slice(n, delay, guard=8)
CoherenceReport(delay_samples, phase_deg, coherence, phase_drift_deg_per_s, phase_std_deg, n_blocks, coherent, note,
                drift_std_deg_per_s, freq_offset_hz, delay_std_samples, delay_resolvable, peak_corr, fs_hz, n,
                ref_channel, channel, t_ns, block_phases_deg, block_delays, block_coherence, criteria, pairs) .to_dict()
check_coherence(device, n=65536, blocks=8, *, ref_channel=0, channels=None, max_lag=None, drift_limit_deg_s=1.0,
                coherence_min=0.9, phase_std_max_deg=5.0, timeout_s=2.0, max_attempts=3) -> CoherenceReport
analyze_coherence(x0_blocks, x1_blocks, fs, t_s=None, ...) -> CoherenceReport    # saf hesap
# interferometer  (eleman 0 x = 0, eleman 1 x = +baseline_m)
Interferometer2(baseline_m, phase_cal_rad=0.0, delay_cal_samples=0.0, *, calibrated=None)
    .calibrate(x0, x1, f_hz, true_az_deg=0.0, *, fs=None, f_bb_hz=None, bw_hz=None, estimate_delay=True) -> dict
    .bearing(x0, x1, f_hz, f_bb_hz=None, bw_hz=None, *, fs=None) -> Bearing
    .measure_phase(...) .solutions_deg(dphi, f) .geometric_phase_rad(az, f) .d_over_lambda(f) .max_unambiguous_hz
Bearing(az_deg, ambiguous_deg (DİĞER çözümler), phase_deg, coherence, quality, f_hz, calibrated, sin_az, d_over_lambda)
    .ambiguous .solutions_deg .to_dict() .to_bus_payload(array_name, t_acq_ns=None, method="interferometer2")
# metrics
angle_errors(est, true) ; rms_angle_error(est, true) ; summarize(est, true) -> {rms, mean, std, max_abs, n}
```

### 3.7 `eds.bus`

```python
# topics (contracts/topics.json ile birebir; tests/bus/test_contracts.py)
RX_STATUS RX_SPECTRUM RX_DETECTIONS RX_SWEEP CAL_RESULT DF_BEARING MEAS_RESULT CTL_RX CTL_ACK ; ALL_TOPICS
registry() -> {topic: {schema, producer, rate, description}} ; load_contract() ; is_known(topic)
schema_path(topic) ; schemas_dir() ; envelope_schema_path() ; schema_uri(file) ; contract_path()
SCHEMA_BASE_URI = "https://eds.invalid/contracts/schemas/" ; UnknownTopic(BusError)
# envelope — zarf {topic, v, src, seq, t_ns, data}
make_envelope(topic, src, seq, data, t_ns=None) ; ENVELOPE_VERSION = 1 ; ENVELOPE_KEYS
sanitize(obj) ; encode(env) -> bytes ; encode_spliced(...) ; decode(buf) -> dict (NaN/Inf reddedilir)
check_envelope(env) ; dumps_compact(obj) ; MessageEncodeError, MessageDecodeError (BusError)
# pubsub — çerçeve: multipart [topic, json]
Publisher(endpoint, bind=True, src="rx0", validate=False, hwm=200, context=None, linger_ms=0, use_xpub=False)
    .publish(topic, data, t_ns=None) -> envelope ; .wait_for_subscribers(n=1, timeout_s=2.0, topic=None) (XPUB)
    .subscriptions() .next_seq(topic) .endpoint .closed .validating .close()
Subscriber(endpoint, topics, bind=False, exact=True, context=None, rcvhwm=1000, conflate=False, strict=False)
    .recv(timeout_s=None) -> Message | None ; .iter_messages(duration_s=None) ; .subscribe(t) .unsubscribe(t)
    .stats: SubscriberStats(received, foreign_dropped, decode_errors, seq_gaps, seq_resets, conflated) .close()
Message(topic, env, t_recv_ns)  .data .src .seq .t_ns .latency_s
run_proxy(frontend, backend, context=None, hwm=1000) ; ProxyThread(frontend, backend, ...) .start() .stop() .running
publisher_from_config(bus, src, validate=None, *, context=None, use_xpub=False)
subscriber_from_config(bus, topics, *, context=None, exact=True, conflate=False, rcvhwm=1000)
# validate (jsonschema Draft 2020-12, yerel Registry; $ref'ler göreli)
validate_message(topic, envelope) ; validate_data(topic, data, src="validate") ; validate_envelope(env)
is_valid(topic, env) ; get_validator(topic) ; check_schemas() -> list[str] ; clear_cache() ; require_backend()
MessageValidationError  # hem jsonschema.ValidationError hem BusError
```

`*_ns` alanları 2**53'ü aşar; JavaScript tüketicileri BigInt ile ayrıştırmalıdır. Ortak
`$defs` (ör. `detection`) `contracts/schemas/common.schema.json` içindedir.

### 3.8 `eds.measure`

```python
# base
SPEC_IDS = ("sensitivity", "dynamic_range", "inst_bw", "scan_speed", "freq_resolution", "df_rms",
            "image_rejection", "max_rate", "latency")
Series(x, y, label="", style="line"|"marker"|"step"|"bar", yerr=None, role=""|"pass"|"fail"|"warn"|"ref")
FigureSpec(name, title, kind="xy"|"spectrum"|"hist"|"polar", series, xlabel, ylabel, hlines, vlines, markers,
           info, logx, logy, ylim, xlim, xticks: list[(x, etiket)])
MeasurementResult(spec_id, title, value, unit, summary, method, conditions, extra, uncertainty, target, target_op,
                  passed, table, figures, status="ok"|"skipped"|"failed", error, device, t_start_ns, duration_s,
                  artifacts, blockers: list[str])
    .evaluate_target(target, op) ; .to_dict(with_figures=True) ; .bus_payload()   # meas.result
    # extra["value_bound"] = "lower" | "upper" | "" (≥ / ≤ sınır; targets.py şu an kullanmaz)
MeasurementContext(app, devices, source=None, world=None, cal=None, out_dir="", prompt, params, quick=False)
    .dut (devices[0]) .is_sim .param(spec_id, key, default) ; .terminated_input() .isolated_source() (context manager)
Measurement(ctx, **params)   # ClassVar: spec_id, title, unit, requires ⊆ {"source", "two_channels"}, default_params
    .check_requirements() -> str | None ; .new_result(value, **kw) ; .run() -> MeasurementResult
    # parametre önceliği: default_params < [measure.params.<id>] < ctx.params < çağrı; quick'te "quick_<anahtar>"
register(cls) ; MEASUREMENTS ; run_measurement(spec_id, ctx, **params) -> MeasurementResult
    # asla fırlatmaz: gereksinim yok → "skipped", istisna → "failed" (+ traceback); conditions["quick"/"simulated"]
# common
setup_rx(dev, *, fc, fs, bw, gain, gain_mode="manual", tracking=None)   # sıra: hız → BW → fc → mod → kazanç → tracking
welch_len(nfft, averages, overlap=0.5) ; capture_spectrum(dev, nfft, averages, ...) -> Spectrum
tone_power_db(spec, f_hz, halfwidth_hz=None)
source_settle_s(src) -> float          # src.settle_s | estimated_queue_latency_s×1.25 + 5 ms | scpi 10 ms | sim/manual 0
stimulus_changed(ctx, settle_s=None) -> float   # bekle, sonra tüm cihazlarda mark_settling
tone(ctx, f_hz, power) (context manager)        # kaynak aç (stimulus_changed dahil), çıkışta kapat
LevelRef(offset_db, method, calibrated, unit, setting=None)
level_reference(ctx, dev, f_hz, gain_db, *, nfft=4096, averages=8, probe_offset_hz=None, probe_dbfs=-25) -> LevelRef
    # sıra: kalibrasyon tablosu → mutlak kaynakla yerinde referans → offset 0 (dBFS, calibrated=False)
base_conditions(dev, spec=None, **extra) ; source_conditions(ctx) ; spectrum_screenshot(spec, name, title, ...) -> FigureSpec
# stimulus — spec'lerin ortak uyaran/ton yardımcıları
qparam(m, key) ; as_float / as_int / as_float_list / opt_float (Türkçe ConfigError)
rx_max_input_dbm(dev) -> float ; source_power_range(src, dev=None) -> (lo, hi)  # mutlak kaynakta RX tavanıyla kırpılı
relative_setting_cap(ctx, dev, setting, level_dbfs, ref) -> float   # bağıl kaynakta tavan (doğrusal okumadan)
input_dbm_estimate(dev, level_dbfs, ref) ; valid_bins(app, n_bins) ; noise_mean_lin/db(spec, valid=None)
locate_peak(spec, f, search_hz) ; nearest_bin(spec, f) ; tone_band_lin/db(spec, k, halfwidth_bins=5, noise_lin=0)
freq_search_hz(dev, f, bin_hz) ; known_search_hz(bin_hz) ; settled_capture(dev, n) ; nominal_setting(ctx, dev, dbfs, ref)
probe_tone(ctx, dev, f_src, setting, *, nfft, averages, window, overlap, search_hz, ...) -> ToneProbe
adjust_tone(ctx, dev, f_src, target_dbfs, *, nfft, averages, window, overlap, ref=None, setting=None, tol_db=0.5,
            max_iter=8, search_hz=None, f_expected=None) -> ToneProbe    # tavanı aşmadan hedef seviyeye
reference_level(ctx, dev, f_hz, *, nfft, averages, window, overlap, probe_frac=0.125, probe_dbfs=-25) -> LevelRef
level_uncertainty_budget(ctx, ref, override, *, setting=None) -> (u_dB | None, Türkçe açıklama)
level_uncertainty_db(ctx, ref, override, *, setting=None) ; source_anchor_dbm(src)
try_lo_offset(src, hz) -> float | None ; source_lo_offset_hz(src) ; source_artifacts(src, e=0.0) -> [(f, etiket)]
ToneProbe(f_src_hz, setting, f_app_hz, level_dbfs, noise_dbfs, levels, k, spec) .snr_db .visible .clipped .offset_db .e
# session
build_context(app, *, devices=None, quick=False, source_kind=None, source_args=None, out_dir=None, prompt=None,
              params=None, configure=True) -> MeasurementContext   # open_devices (yalıtım planı) + open_source + kalibrasyon
close_context(ctx, close_devices=True) ; reset_baseline(ctx)   # her ölçümden önce [rx]'e dön, kaynağı kapat
run_suite(app, spec_ids=None, *, ctx=None, write=True, publish=False, title=None, on_result=None, **build_kw)
    -> SuiteResult(results, report_dir, aborted) .ok    # Ctrl-C: kısmi rapor "YARIDA KESİLDİ", sonra yeniden fırlat
resolve_spec_ids(ids)
# targets — config/ktr_targets.toml: [spec] target, op ("<="|">="), note, [spec.require]
Target(target, op, note="", require=()) .passes(v) .describe(unit) ; load_targets(path=None) ; parse_targets(raw)
apply_targets(results, targets)          # yerinde; karar verilemiyorsa passed=None + extra["verdict_note"]
verdict_blockers(r, target=None) -> list[str] ; unmet_requirements(r, require) ; resolve_targets_path(path)
REASON_QUICK, REASON_NONREALTIME, REASON_NO_COHERENCE
# report / plots
write_report(results, out_dir, app=None, title=DEFAULT_TITLE, meta=None) -> Path
    # out_dir/<stamp>/: report.html, summary.md, summary.csv, results.json, tables/*.csv, figures/*.png
verdict(r) -> "GEÇTİ"|"KALDI"|"KARŞILAŞTIRILMADI"|"HEDEF YOK"|"ATLANDI"|"HATA" ; no_verdict_reason(r)
format_value(v, unit, unc) ; format_number ; format_uncertainty ; format_condition ; fmt_sps ; short_conditions(r)
console_table(results) ; is_quick(r) ; is_simulated(r) ; figure_flags(r) ; git_revision()
render(fig, path, *, dpi=128) -> Path    # 1280×720 PNG; matplotlib yalnızca burada
available() ; series_role(s) ; sa_scale(hint, top, unit)
```

### 3.9 `eds.cli` (`eds` komutu)

```text
eds [-c DOSYA] [--set ANAHTAR=DEĞER]... [--backend sim|soapy] [-v] KOMUT
  profiles                       profilleri listele
  devices [--backend] [--args k=v ...] [--probe]
  info                           etkin konfigürasyon, profil sınırları, RX doğrulaması, Pluto süreç yalıtım planı
  rx [--duration S]              RX düğümü (stderr durum satırı)
  sweep [--start HZ] [--stop HZ] [--rbw HZ] [--fs HZ] [--out F.npz] [--png F] [--publish]
  cal ppm | power | coherence    kalibrasyon (cal/<seri>.json, cal.result)
  measure [ÖLÇÜM...|all] [--quick] [--source sim|pluto_tx|scpi|manual|none] [--out DİZİN] [--publish]
          [--no-report] [--title BAŞLIK] [--yes]
  bus proxy | echo TOPIC... | send ctl.rx --cmd ... --arg k=v [--wait-ack S]
Çıkış kodları: 0 tamam, 1 konfigürasyon/kullanım hatası, 2 en az bir ölçüm başarısız, 130 Ctrl-C.
```

Sayısal seçenekler `finite_float` / `positive_float` / `nonneg_float` / `positive_int`
(`eds.cli.util`) ile doğrulanır; argparse kullanım hataları Türkçe gösterilir.

## 4. Simülatör (`eds.hal.sim`) — fizik modeli

`SimWorld` (paylaşılan RF sahnesi) + `SimRxDevice` (cihaz başına kusurlar) +
`SimSignalSource` (sahneye ton enjekte eden üreteç). Amaç: üst katmanların ve **ölçüm
prosedürlerinin** gerçek kusurlara karşı CI'da **nicel** doğrulanması. Aşağıdaki her sayı
`[[device]].sim` tablosunun varsayılanıdır ve cihaz başına değiştirilebilir (bilinmeyen anahtar
= `ConfigError`). `SimRxDevice` yalnızca tek kanallıdır (`channels = [0]`).

**Sinyal zinciri (sırasıyla):**

1. **Seviye:** `dBFS = dBm + G + gain_offset_db` (−5 dB ⇒ G = 73'te tam ölçek ≈ −68 dBm).
2. **Termal gürültü:** `−173.975 + noise_figure_db` (5 dB) dBm/Hz, aynı seviye denklemiyle;
   mutlak örnek indeksine hizalı `FRAME = 16384` örneklik frekans alanı çerçevelerinde üretilir,
   ardışık çerçeveler `FRAME_XFADE = 1024` örnek örtüşür ve güç tamamlayıcı pencerelerle
   geçişlenir (`FRAME_HOP = 15360`): çerçeve eklerinde bant dışı sızıntı yok; gürültü benzeri
   emitter'lar da aynı yolla üretilir.
3. **RX filtreleri** (her emitter katkısına, **alias'tan önceki** taban bant ofsetinde
   `f = f_RF − f_LO`; gürültüye ±fs/2 içinde):
   * analog: 3. derece Butterworth, −3 dB köşesi `analog_corner_factor · bw/2` (1.4; AD936x
     sürücüsü BB LPF'yi 1.4 × BBBW'ye kalibre eder) × TIA tek kutbu `tia_pole_factor · bw/2`
     (2.5; 0 = yok). `f = bw/2`'de analog zincir ≈ −1.2 dB'dir;
   * dijital (`digital_filter = true`): ±`dig_pass_frac`·fs (0.40) düz, ±`dig_stop_frac`·fs
     (0.60) ötesinde `dig_stop_db` (−80 dB), arada dB'de kosinüs geçiş. `k·fs + δ`'daki emitter
     δ'ya en az 80 dB bastırılarak katlanır.
   * Gerçek değer: `SimRxDevice.response_db(f_bb_hz)`. Filtre sonrası seviyesi ADC tabanının
     60 dB altında kalan katkılar (`skip_level_dbfs`) hiç sentezlenmez (LO'dan GHz uzaktaki
     emitter'lar maliyetsiz).
4. **TCXO:** `ppm` (dünyaya kaydolan ilk cihaz +11.7, diğerleri ada göre ±15 içinde);
   `e_res = (1+e)/(1+e_hw) − 1`; LO `fc·(1+e_res)`, örnekleme saati `fs·(1+e_res)`.
   `set_ppm_correction` (`hw_xo_correction = true`) `xo_correction_step_hz` adımlarıyla yazar
   (vars. **1 Hz** = 0.025 ppm, Pluto gibi; 0 = sürekli): `ppm_hw` **kuantalanmış** değerdir,
   ±0.0125 ppm'e kadar artık kalır ve `IQBlock.meta["ppm_hw"]` bunu taşır.
5. **Geometri / LO fazı:** alınan faz `2π f t + φ_e + 2π f/c·(p·u) − (2π f_LO t + θ_LO)`,
   `u = (sin aoa, cos aoa)`, anten konumu `position_m`; `cabled` emitter'da geometrik terim
   yok. `θ_LO` her retune'da ve donanım ppm değişiminde yeniden çekilir (`lo_phase_random`).
   `shared_clock` grubu: tüm üyeler ilk üyenin ppm'ini kullanır (sabit faz farkı); bağımsız
   saatlerde faz farkı `2π f (e_b − e_a)` rad/s döner. Gürültü benzeri zarflar dünya örnek
   ızgarasında (`n + time_offset_samples`) çekilir: iki cihaz aynı gerçekleşmeyi görür.
6. **RF ön uç sıkışması** (`rf_compression = true`): bileşik sinyale Rapp AM-AM (AM-PM yok),
   `p = rapp_smoothness` (2); giriş P1dB bandın kazanç tablosunda dB'de doğrusal:
   en düşük kazançta `p1db_min_gain_dbm` (0 dBm) → en yüksekte `p1db_max_gain_dbm` (−20 dBm).
   1 GHz'te ≈ 7 dB kazancın altında ön uç ADC'den önce sıkışır (−1 dB kazançta çıkış ≈ −5 dBFS'te
   doyar); orta/yüksek kazançta ADC önce kırpar. Gerçek değerler: `input_p1db_dbm(gain_db=None)`,
   `compression_db(power_dbm, gain_db=None)`; ton `level_dbfs(P) − compression_db(P)` okur.
   Sınırlar: bant dışı engelleyiciler modeli sıkıştırmaz, IMD ürünleri yeniden süzülmez.
7. **IQ dengesizliği:** `y = x + r·conj(x)`; tracking kapalı `iq_gain_db, iq_phase_deg`
   (0.4 dB, 2.5° ⇒ IRR ≈ 30 dB), açık artıklar `iq_residual_gain_db, iq_residual_phase_deg`
   (0.02 dB, 0.15° ⇒ ≈ 55 dB). Gerçek değer: `image_rejection_db()`.
8. **DC ofset:** tracking kapalı `dc_offset_dbfs` (−40), açık `dc_residual_dbfs` (−80).
9. **Spur'lar:** ADC sonrası dijital tonlar (`spurs`, vars. 1.234 MHz @ −85 dBFS; kazançtan bağımsız).
10. **ADC:** `adc_noise_lsb_rms` (0.5 LSB rms, ≈ −69.2 dBFS) beyaz geçiş gürültüsü (kuantalayıcıyı
    titretir), sonra 12-bit niceleme `rint(x·2048)` ve I/Q'nun **ayrı ayrı** sert kırpılması
    (`[−2048, 2047]`). En düşük kazançta çıkış tabanı ≈ −68 dBFS; yüksek kazançta termal baskın.
    Gerçek değer: `adc_noise_dbfs`, `noise_density_dbfs_hz(gain_db)`, `level_dbfs(power_dbm, gain_db)`.

**Ayar değişiklikleri ve bayat tamponlar:** her değişiklik (fc, fs, bw, kazanç, mod, tracking,
ppm) mutlak örnek indeksine göre kaydedilir; akış sürerken FIFO'da bekleyen örnekler ESKİ
ayarı taşır (gerçek zamanlı modda çağrıdan önce üretilenler, değilse sonraki
`kernel_buffers × buffer_size` örnek). HAL `(kernel_buffers + 1) × buffer_size` örneği settling
işaretler. **HW AGC** (`hw_*`): `buffer_size` bloğu başına −12 dBFS tepeye yaklaşan
slow-attack döngüsü (±2 dB histerezis, +3/−6 dB/blok, kırpmada −10 dB).

**Akış / USB modeli:** `realtime = false`: okuma hemen döner, zaman sanal ve deterministik,
overflow yok (zamanlama ölçümleri anlamsızdır ve engellenir). `realtime = true` (vars.): cihaz
saniyede `fs_act` örnek üretir (simülatörün kendi sentez süresi üretimi dondurur), FIFO
kapasitesi `kernel_buffers × buffer_size`, aktarım `usb_bytes_per_s` (24e6) / `bytes_per_sample`
ile sınırlı; FIFO taşarsa EN ESKİ örnekler atılır, sonraki okuma `overflow_before=True` ve dalga
biçimi doğru mutlak indeksten sürer. Sahne değişiklikleri (emitter aç/kapa) örnek başına
uygulanır: `T` anındaki geçiş, `T`'de ya da sonra üretilen ilk örnekte görünür
(`SimWorld.switch_log()` = gecikme ölçümünün gerçek değeri).

**Sahne (`SimWorld`):** emitter türleri `cw`, `noise` (`bw_hz`), `fm` (`dev_hz`, `mod_hz`),
`pulse` (`pri_s`, `pw_s`, `t0_s`), `gsm_fcch` (200 kHz GMSK benzeri gürültü + her
`period_frames` çerçevede +67.708 kHz FCCH tonu); hepsi `phase_deg` alır. API:
`add_emitter`, `remove_emitter(name, missing_ok=False)`, `replace_emitters(remove, add)` (tek
sahne değişimi), `set_enabled(name, on)` / `set_enabled_many(names, on)` → `time_ns`, `update`,
`get`, `switch_log()`, `scenes_between(t_lo, t_hi)`, `current_scene()`,
`register_device(name, shared_clock, ppm)`, `clock_ppm`, `describe()`.

**Gerçek değer yardımcıları (`SimRxDevice`):** `true_ppm`, `residual_ppm`, `sim_params`,
`position_m`, `abs_index`, `level_dbfs`, `input_p1db_dbm`, `compression_db`,
`noise_density_dbfs_hz`, `adc_noise_dbfs`, `skip_level_dbfs`, `response_db`,
`image_rejection_db`, `get_tracking`. `SimSignalSource` RF KAPALI başlar,
`set_frequency/set_power` tek ton moduna döner, `power_range_db = (−150, 10)`,
`freq_range_hz = (1 kHz, 6 GHz)`; `set_lo_offset` desteklemez (dahili LO yok).

## 5. Süreç yalıtımı (`eds.hal.proc`)

**Neden:** SoapyPlutoSDR `PlutoSDR_Settings.cpp` içinde `static iio_context *ctx` tutar:
aynı süreçte açılan ikinci Pluto `uri`'sinden bağımsız olarak ilkinin bağlamını kullanır ve
birini kapatmak diğerinin bağlamını yok eder. EDS bunu iki katmanda çözer:

* **Koruma:** `soapy_backend.claim_pluto` süreç başına tek Pluto yuvası tutar; `SoapyRxDevice`
  ve `SoapyTxSource` `SoapySDR.Device()`'tan ÖNCE yuvayı alır, ikinci istek `DeviceError`
  (Türkçe, `isolation = "subprocess"`/`"auto"` önerisiyle). Yuva kapanışta, açılış hatasında ya
  da çöp toplamada serbest kalır.
* **Yerleşim** (`factory.isolation_plan`, `isolation = "auto"`):

| Bu süreçte açılacaklar | Yerleşim |
|---|---|
| 2 Pluto `[[device]]` (2× Pluto DF grubu) | iki cihaz da kendi alt sürecinde (`ProcessRxDevice`) |
| 1 Pluto + `measure.source = "pluto_tx"` (vars.) | DUT süreç içinde; TX Pluto alt süreçte (`open_source` yalıtır) |
| 1 Pluto + `pluto_tx`, `source_args.isolate = false` | DUT alt süreçte, kaynak süreç içinde |
| tek Pluto / sim cihazları / Pluto olmayan soapy | süreç içinde |

`"inproc"` / `"subprocess"` zorlar (zorla `inproc` bir süreçte birden çok Pluto bırakırsa
günlük uyarısı). Bir sim cihazı yalnızca zorlanınca yalıtılır; o zaman alt süreç **kendi**
`SimWorld`'ünü kurar (ortak sahne, geometri ve ppm sırası kaybolur; `build_context` bu
durumda `source = "sim"`'i `ConfigError` ile reddeder).

**`ProcessRxDevice`:** `multiprocessing` *spawn* alt süreci cihazı
`factory.open_device` ile (yalıtım `inproc`'a zorlanmış, `AppConfig` düz veriden yeniden
kurulmuş) açar. `RxDevice` taban mantığı (profil doğrulaması, kazanç kırpma, settling, boşluksuz
okuma, istatistik) üst süreçte kalır; ilkeller ve isteğe bağlı özellikler RPC'dir. İki boru
(kontrol + veri), alt süreçte her biri ayrı thread: bloklayan okuma retune/`stop_stream`'i
geciktirmez. Okuma yanıtı `struct <BBxxiqqq` başlık + kanal kazançları + ham complex64 tampon
(`send_bytes`/`recv_bytes_into`, pickle yok). Alt süreç hataları aynı `eds.core.errors` sınıfı
ve mesajıyla yeniden fırlatılır (`err.child_traceback`, `__cause__ = RemoteTraceback`); EDS dışı
hata `DeviceError` olur. Alt süreç ölürse bekleyen ve sonraki çağrılar `DeviceError`
(okumada `StreamError`); `close()` alt süreci kapatıp biçer; çöp toplama / çıkışta
`multiprocessing.util.Finalize` boruları kapatır, alt süreç cihazını kapatıp çıkar. Alt süreç
SIGINT'i yok sayar (Ctrl-C yalnız üst süreçte). Bekleme sırasında kesilen çağrı boruları
senkron bırakır; kesilen okumanın örnekleri bir overflow boşluğu olur. `info` ve durum
(fc/fs/bw/kazançlar/mod/ppm/yeni uyarılar) her kontrol çağrısından sonra yansıtılır;
`backend_name = "proc"`, `info.backend` alt sürecin backend'idir. Arka uca özgü öznitelikler
(`overflow_source`, `thin_layer()`, `true_ppm` ...) yansıtılmaz; yalnızca `describe()`'da
görünür.

**`ProcessSignalSource`:** herhangi bir kaynak sınıfını (vars. `eds.hal.tx_hw:SoapyTxSource`)
alt süreçte çalıştırır; `set_*`, `set_lo_offset`, `describe`, `close` iletilir, hata türleri
korunur. Üst süreç kaybolursa alt süreç RF'yi kapatır (`set_enabled(False)`) ve kaynağı kapatır.

**Maliyet/sınırlar:** açılış bir Python yorumlayıcısı başlatır (~0.15–1 s); okuma başına bir
boru gidiş-dönüşü (onlarca µs) + iki kopya (ölçülen taşıma ~250–320 MS/s, USB 2.0'ın çok
üstünde). Yalıtılmış cihaz açan kullanıcı betiği üst düzeyini `if __name__ == "__main__":`
arkasında tutmalıdır (spawn); `eds` CLI ve pytest bunu zaten sağlar.

## 6. Örnek kaybı (overflow) algılama

| Kaynak | `overflow_source` | Ne olur |
|---|---|---|
| Soapy sürücüsü `SOAPY_SDR_OVERFLOW` / donanım zaman damgası sıçraması (`detect_ts_gaps`) | `"driver"` | Sonraki blok `overflow=True` (boşluk tam önünde). |
| Pluto + ince katman: `cf-ad9361-lpc` DMA durum yazmacı `0x80000088`, bit 2 (W1C) | `"dma_status"` | Akış başlarken temizlenir; başarılı okumalardan sonra en fazla `overflow_poll_s` (20 ms) aralıkla okunup temizlenir. Bit set → sonraki `RawRead.overflow_before=True`, `uncertain_samples = settle_samples` (kayıp o anda kuyrukta bekleyen tamponların arkasındadır): blok `overflow=True`, ardından gelen `settle_samples` örnek `settling`. Yoklama hatası → algılama uyarıyla kapanır. |
| Pluto, ince katman yok | `"none"` | SoapyPlutoSDR `readStream` overflow **bildirmez**: kayıp algılanamaz (akış başlarken uyarı); süreç içi cihazda `rx.status` `stream.overflow_source = "none"` taşır, `eds rx` sayacı "bilinmiyor" gösterir. `ProcessRxDevice` / `DeviceGroup` bu özniteliği ve üyelerin geç uyarılarını yansıtmaz. |
| Simülatör | (öznitelik yok) | USB/FIFO modeli `overflow_before` üretir (yalnızca `realtime = true`). |

Ortak kurallar (`RxDevice.read`): bildirilen her boşlukta yarım blok atılır ve blok yeniden
başlatılır (`overflow=True`, `stats.overflows++`); okuma zaman aşımı (`StreamTimeout`) da
boşluktur (kısmi örnekler `discarded_samples`'a, sonraki blok `overflow=True`, sayaç
`timeouts`). Durdurulmuş akıştan dönen okuma atılır (`StreamError`), kanal sayısı yanlış okuma
reddedilir. `DeviceGroup`'ta herhangi bir üyenin boşluğu grup bloğunu `overflow=True` yapar
(DF hizalaması kayboldu). `max_rate` ölçümü bunlara ek olarak örnek sayımıyla çapraz kontrol
yapar (MEASUREMENTS §8).
