"""EDS — Elektronik Destek Sistemi (KTÜ TEKNOFEST Elektronik Harp takımı).

Alt paketler:
    eds.core     ortak tipler, konfigürasyon, cihaz profilleri, birim yardımcıları
    eds.hal      donanım soyutlaması (SoapySDR birincil, libiio ince katman, simülatör)
    eds.dsp      PSD, pencereler, CFAR tespit, tepe interpolasyonu, istatistik
    eds.rx       akış motoru, kazanç yönetimi (AGC), tarayıcı (sweeper), RX düğümü
    eds.calib    PPM, güç kalibrasyonu, kalibrasyon deposu
    eds.df       yön bulma (Faz-0: 2× Pluto interferometre; Faz-1: KrakenSDR)
    eds.bus      ZeroMQ pub/sub, zarf (envelope), JSON şema doğrulama
    eds.measure  KTR nicel spec ölçümleri + rapor (tablo/grafik/ekran görüntüsü)
    eds.cli      `eds` komut satırı aracı
"""

__version__ = "0.1.0"
