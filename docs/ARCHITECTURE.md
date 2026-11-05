# EDS RX omurgası — Mimari, Konvansiyonlar ve Modül Arayüzleri

> Bu belge **kod sözleşmesidir**. Modüller arası her çağrı burada tanımlı imzaya uyar.
> İmza değişikliği = bu belgenin aynı commit'te güncellenmesi.

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
                       eds.hal (RxDevice)
          ┌───────────────┬──────┴────────┬──────────────┐
   soapy_backend      pluto_iio (ince)   group (N×1ch)   sim (fizik tabanlı)
   (SoapyPlutoSDR)    xo_correction,      2×Pluto        CI + demo + ölçüm
                      tracking, sıcaklık                  doğrulaması
```

* **Akış ve cihaz soyutlaması SoapySDR** üzerindendir (`eds.hal.soapy_backend`).
  Pluto'ya özgü alçak seviye kontroller (PPM için `xo_correction`, BB/RF DC ve quadrature
  tracking, sıcaklık) önce Soapy API'si ile denenir; desteklenmiyorsa
  `eds.hal.pluto_iio` ince libiio katmanına düşülür. pyadi-iio **birincil arayüz değildir**.
* Kraken/bladeRF/USRP: yeni profil TOML + (gerekirse) yeni backend dosyası. Üst katmanlar
  yalnızca `RxDevice` görür (`num_channels`, `coherent`).

## 2. Konvansiyonlar (tüm modüller UYMAK ZORUNDA)

| Konu | Kural |
|---|---|
| Birimler | Frekans **Hz**, zaman **s** (`*_ns` ile biten int nanosaniye). |
| Örnekler | `complex64`, şekil `(n_ch, n)`, **tam ölçek = 1.0** (`I = counts/2048`). |
| 0 dBFS | Genliği tam ölçek olan **kompleks üstel**in gücü: `mean(|x|²) = 1.0`. |
| Kırpma | `max(|I|,|Q|) >= clip_level` (≈2047/2048). |
| PSD ölçekleme | **Güç spektrumu (spektrum analizörü)**: `P[k] = |Σ w x e^{-j..}|² / (Σw)²`. Bin merkezindeki tonun gücünü doğru okur; gürültü **RBW = ENBW·fs/N** bandındaki gücü okur. Yoğunluk `dB/Hz = P − 10log10(RBW)`. |
| Bant gücü | `band_power = Σ P[k] / ENBW_bins` (`ENBW_bins = N·Σw²/(Σw)²`). Scalloping'den bağımsız; ton gücü/kanal gücü için bunu kullanın. |
| Frekans ekseni | `np.fft.fftshift` sonrası: `f[k] = fc + (k − N/2)·fs/N`, k=0..N−1 (DC = indeks N/2). |
| dBm | `dBm = dBFS + offset(f, G)`; `offset ≈ C(f) − G` (kalibrasyon tablosu). Kalibrasyonsuz veri `unit="dBFS"`, `calibrated=false`. |
| PPM işareti | `ppm = (f_ref_gerçek − f_ref_nominal)/f_ref_nominal·1e6` (pozitif = TCXO hızlı). `e = ppm·1e-6`. AD936x'te LO ve örnekleme saati aynı referanstan türediği için **görünen RF frekansı** `f_app = f_true/(1+e)` ⇒ **düzeltme** `f_true = f_app·(1+e)`; tam `f`'ye ayarlamak için `fc_cmd = f/(1+e)`. Donanım düzeltmesi: `xo_correction = ref_clock_hz·(1+e)`. |
| Kazanç | dB, cihaz "hardwaregain" (Pluto 0..73/−3..71 banda bağlı). `soft_agc` HAL'de `manual` olarak uygulanır, yazılım AGC'si (`eds.rx.agc`) yönetir. Ölçüm kalitesinde güç için HW AGC kapalı olmalı (örnekle senkron kazanç bilgisi yok). |
| Zaman | Bus mesajları `time.time_ns()` (duvar saati); süreler `perf_counter`. `IQBlock.t_ns` = ilk örneğin tahmini host zamanı. |
| Açılar | Derece; `wrap_deg` (−180, 180]. DF azimutu: dizi eksenine dik (broadside) = 0°, saat yönünün tersi pozitif değil — **dizi ekseni boyunca 2. elemana doğru pozitif** (bkz. eds.df). |
| Hatalar | Yalnızca `eds.core.errors` alt sınıfları fırlatılır. |
| Dil | Kod/docstring İngilizce; kullanıcıya dönük metin (CLI, rapor, profil ihlalleri) Türkçe. |

## 3. Modül arayüzleri

### 3.1 `eds.core` (YAZILDI)
`types.IQBlock, StreamStats, DeviceInfo, GainRange` · `config.AppConfig` (+ bölümler) ·
`profile.DeviceProfile, load_profile` · `errors.*` · `units.*` · `timeutil.*` · `paths.*`

### 3.2 `eds.hal` (base YAZILDI)
* `base.RxDevice` — bkz. docstring. Backend'ler `_apply_*`, `_start`, `_read_raw`,
  `_stop`, `_close` uygular; isteğe bağlı `set_tracking`, `set_ppm_correction`,
  `read_sensors`, `runtime_limits`.
* `soapy_backend.SoapyRxDevice(cfg, profile)`; `enumerate_soapy(args) -> list[dict]`.
* `pluto_iio.PlutoIIO(uri)` — ince libiio katmanı:
  `get/set_xo_correction(hz)`, `set_tracking(bb_dc, rf_dc, quad)`, `get_tracking()`,
  `temperature_c()`, `rssi_db()`, `gain_control_mode` get/set, `firmware_version()`,
  `serial()`, `close()`. `iio` modülü yoksa `BackendUnavailable`.
* `group.DeviceGroup(devices: list[RxDevice])` — `RxDevice` alt sınıfı; kanalları
  birleştirir (`num_channels = Σ`), `coherent = False` (ortak referans + kalibrasyon
  olmadan). `read()` her üyeden eşzamanlı (thread) okur; herhangi bir üyede overflow →
  blok `overflow=True` (hizalama kayboldu, DF yeniden kalibre etmeli).
* `tx.SignalSource` (YAZILDI) + `tx_hw.SoapyTxSource`, `tx_hw.ScpiSignalGenerator`,
  `tx_hw.ManualSource`; `sim.source.SimSignalSource`.
* `sim` — bkz. §4.

### 3.3 `eds.dsp`
```python
windows.get_window(name: str, n: int) -> np.ndarray            # float64, periodic (DFT-even)
windows.window_info(name, n) -> WindowInfo(coherent_gain, enbw_bins, scallop_loss_db, sidelobe_db)
psd.Spectrum  # dataclass: f_hz (n,), p_lin (n,) power-spectrum scaled, fc, fs, nfft, rbw_hz,
              #   enbw_bins, window, averages, unit="dBFS", t_ns, gain_db, overflow
              #   props: p_db, noise_floor_db (median), freqs relative; methods: band_power(f_lo,f_hi)->lin,
              #   to_db(), slice(f_lo, f_hi) -> Spectrum, with_offset_db(off, unit) -> Spectrum
psd.welch(x: np.ndarray (n,), fs, nfft, window="blackmanharris", overlap=0.5,
          avg="mean", fc=0.0) -> Spectrum                       # x complex, avg over segments
psd.spectrum_from_block(block: IQBlock, nfft, window, overlap, avg, channel=0) -> Spectrum
psd.nfft_for_rbw(rbw_hz, fs, window) -> int                    # next pow2 with ENBW*fs/N <= rbw
peaks.interp_peak(p_db: np.ndarray, k: int) -> (delta_bins: float, peak_db: float)  # parabolic on dB
peaks.find_peaks(spec: Spectrum, n=5, min_sep_bins=3, exclude_dc_bins=0) -> list[Peak(f_hz, p_db, bin)]
peaks.tone_frequency(x, fs, fc=0.0, f_hint=None, search_hz=None) -> float   # high-precision (zero-pad + interp)
stats.gamma_quantile(p: float, k: float) -> float              # inverse regularized lower gamma (shape k, scale 1)
stats.cfar_alpha(pfa: float, n_avg: int) -> float              # threshold multiplier for averaged |X|^2 noise
levels.iq_levels(x) -> IQLevels(peak_dbfs, rms_dbfs, clip_fraction, dc_dbfs)   # x (n,) or (ch,n)
detect.Detection  # dataclass: f_center_hz, f_peak_hz, f_lo_hz, f_hi_hz, bw_hz, peak_db, band_power_db,
                  #   snr_db, noise_db, n_bins  (+ to_dict())
detect.detect(spec: Spectrum, cfg: DetectConfig, n_avg: int | None = None,
              valid_mask: np.ndarray | None = None) -> tuple[list[Detection], np.ndarray threshold_db]
detect.usable_mask(n, usable_fraction, dc_notch_bins) -> np.ndarray[bool]
```

### 3.4 `eds.rx`
```python
stream.RxStreamer(device: RxDevice, block_size: int, queue_blocks=32, on_block=None)
    .start(); .stop(); .get(timeout) -> IQBlock | None; .stats -> StreamStats (host_drops dahil)
    # ayrı thread device.read() yapar; kuyruk dolarsa EN ESKİ blok atılır, host_drops++
agc.GainController(device: RxDevice, cfg: AgcConfig)
    .update(block: IQBlock) -> float | None      # yeni kazanç uygulandıysa döner; settling bloklarını yok sayar
    .settle(max_iter=8, n=16384) -> float         # tek seferlik: hedef headroom'a gelene kadar
sweep.Sweeper(device: RxDevice, cfg: SweepConfig, dsp: DspConfig, detect: DetectConfig,
              cal: DeviceCalibration | None = None, agc: AgcConfig | None = None)
    .plan() -> list[float]                        # adım merkez frekansları
    .run(on_step=None) -> SweepResult
SweepResult  # f_hz, p_db (birleştirilmiş), unit, calibrated, rbw_hz, steps, duration_s,
             # rate_hz_per_s, timing{tune,settle,capture,dsp} (s), detections, t_start_ns, t_end_ns
             # .decimate(max_points, mode="max") -> (f, p)
node.RxNode(app: AppConfig, device: RxDevice | None = None, publisher=None)
    .run(duration_s=None); .stop(); .handle_command(cmd: dict) -> dict(ack)
```

### 3.5 `eds.calib`
```python
ppm.PpmEstimate  # ppm, std_ppm, f_measured_hz, f_ref_hz, snr_db, n, method, temperature_c, t_ns
ppm.estimate_ppm_cw(device, f_ref_hz, offset_hz=100e3, n=2**18, repeats=5) -> PpmEstimate
ppm.estimate_ppm_from_samples(x, fs, fc_nominal, f_ref_hz) -> float (ppm)
ppm.correct_freq(f_app_hz, ppm) -> f_true ; ppm.tune_freq(f_target_hz, ppm) -> fc_cmd
fcch.estimate_ppm_gsm_fcch(device, arfcn_or_freq_hz, ...) -> PpmEstimate   # FCCH ton: fc + 1625/24 kHz
power.PowerCalTable(points: list[(f_hz, gain_db, offset_db)])
    .offset_db(f_hz, gain_db) -> float            # dBm = dBFS + offset ; bilineer, gain ekseninde
                                                  # ölçülmemiş yerde -G eğimiyle ekstrapolasyon
store.DeviceCalibration  # serial, ppm: PpmEstimate|None, power: PowerCalTable|None,
                         # image_rejection_db: dict, coherence: dict, updated_ns; to_dict/from_dict
store.CalibrationStore(dir) .load(serial) -> DeviceCalibration ; .save(cal)
apply.apply_calibration(spec: Spectrum, cal, ppm_mode) -> Spectrum   # f düzeltmesi + dBm
apply.apply_ppm_to_device(device, cal, ppm_mode) -> str               # "hardware"|"soapy"|"software"|"off"
```

### 3.6 `eds.df`
```python
coherence.estimate_delay(x0, x1, fs, max_lag) -> (delay_samples: float (fractional), peak_corr: float)
coherence.phase_offset(x0, x1) -> (phase_rad, coherence 0..1)
coherence.CoherenceReport  # delay_samples, phase_deg, coherence, phase_drift_deg_per_s, coherent: bool
coherence.check_coherence(device_group, n=65536, blocks=8) -> CoherenceReport
interferometer.Interferometer2(baseline_m, phase_cal_rad=0.0)
    .bearing(x0, x1, f_hz) -> Bearing(az_deg, ambiguous: list[float], phase_deg, quality)
metrics.rms_angle_error(est_deg, true_deg) -> float  # wrap'li
```

### 3.7 `eds.bus`
```python
topics.RX_STATUS ... CTL_ACK (str sabitleri) ; topics.registry() -> dict (contracts/topics.json)
envelope.make_envelope(topic, src, seq, data) -> dict ; envelope.ENVELOPE_VERSION = 1
pubsub.Publisher(endpoint, bind=True, validate=False, src="rx0", hwm=200)
    .publish(topic, data) -> dict(envelope) ; .close()
pubsub.Subscriber(endpoint, topics: list[str], bind=False, exact=True)
    .recv(timeout_s) -> Message | None   # Message(topic, env: dict, t_recv_ns)
pubsub.run_proxy(frontend, backend)      # XSUB/XPUB
pubsub.publisher_from_config(bus_cfg, src) ; subscriber_from_config(bus_cfg, topics)
validate.validate_message(topic, envelope) -> None (raises jsonschema ValidationError)
```

### 3.8 `eds.measure` (base YAZILDI) — bkz. `measure/base.py`; ölçümler `measure/specs/*.py`,
rapor `measure/report.py` (`write_report(results, out_dir) -> Path`), grafikler
`measure/plots.py` (`render(fig: FigureSpec, path)`), hedefler `measure/targets.py`.

## 4. Simülatör (`eds.hal.sim`) — fizik modeli

`SimWorld` (paylaşılan RF sahnesi) + `SimRxDevice` (cihaz başına kusurlar). Amaç: üst
katmanların ve **ölçüm prosedürlerinin** gerçek kusurlara karşı CI'da doğrulanması.

Seviye modeli: `dBFS = dBm + G + gain_offset_db` (varsayılan offset −5 dB ⇒ G=73'te
tam ölçek ≈ −68 dBm). Termal gürültü `−174 + NF` dBm/Hz (NF vars. 5 dB), analog filtre
(BW ayarına göre 3. derece Butterworth genlik yanıtı) gürültüye ve sinyallere uygulanır,
12-bit niceleme + sert kırpma (±2048), DC ofset, IQ dengesizliği (kazanç ε dB, faz φ°;
quadrature tracking açıkken artık değerler), sabit spur'lar, TCXO ppm (LO ve fs aynı
oranda), LO fazı her retune'da rastgele, retune sonrası `kernel_buffers×buffer_size`
örnek **eski frekanstan** (bayat tampon), gerçek zamanlı USB modeli
(`usb_bytes_per_s`; cihaz FIFO kapasitesi `kernel_buffers×buffer_size`, dolunca
overflow), çok cihazlı sahnede anten konumu (`position_m`) ve emitter AoA'sı ile geometrik
faz, `shared_clock` grubu (aynı ppm + sabit faz farkı) vs bağımsız saat (faz kayması).
