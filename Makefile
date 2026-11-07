# EDS — kısa komutlar. "make help" hepsini insan diliyle listeler.
# .venv varsa onun Python'u kullanılır (./setup.sh oluşturur), yoksa sistemdeki python3.
PY ?= $(if $(wildcard .venv/bin/python),.venv/bin/python,python3)

.PHONY: help setup setup-hw run-sim run gui test test-fast lint demo report-sim install clean

help:
	@echo ""
	@echo "EDS — Elektronik Destek RX omurgası · kısa komutlar"
	@echo ""
	@echo "  Kurulum"
	@echo "    make setup       Simülasyon + GUI kur (yalnız pip; SDR sürücüsü yok, ~5 dk)"
	@echo "    make setup-hw    Yukarıdakiler + Pluto sürücüleri (libiio, SoapySDR, SoapyPlutoSDR, udev)"
	@echo ""
	@echo "  Çalıştırma (tek komut: bus proxy + RX düğümü + GUI; Ctrl-C hepsini kapatır)"
	@echo "    make run-sim     Simülatörle çalıştır — donanım gerekmez"
	@echo "    make run         Gerçek Pluto ile çalıştır (config/rx_pluto.toml)"
	@echo "    make gui         Yalnız operatör GUI'sini aç (çalışan bir hatta bağlanır)"
	@echo ""
	@echo "  Geliştirme"
	@echo "    make test        Bütün testler (simülatör; donanım gerekmez, ~3 dk)"
	@echo "    make test-fast   Zamanlamaya duyarlı (realtime) testler hariç"
	@echo "    make lint        Kod stili denetimi (ruff)"
	@echo ""
	@echo "  Ölçüm (KTR)"
	@echo "    make demo        Simülatörde 9 KTR ölçümünün hızlı raporu (reports/)"
	@echo "    make report-sim  Simülatörde tam KTR raporu (~5 dk)"
	@echo ""
	@echo "  Kullanılan Python: $(PY)"
	@echo ""

setup:
	./setup.sh --sim

setup-hw:
	./setup.sh --hardware

run-sim:
	$(PY) -m eds.launch --mode sim

run:
	$(PY) -m eds.launch --mode hardware

gui:
	$(PY) -m eds.gui --connect tcp://127.0.0.1:5551

test:
	$(PY) -m pytest

test-fast:
	$(PY) -m pytest -m "not realtime"

lint:
	$(PY) -m ruff check src tests examples run.py

demo:
	$(PY) -m eds measure all --config config/rx_sim.toml --quick

report-sim:
	$(PY) -m eds measure all --config config/rx_sim.toml

install:
	$(PY) -m pip install -e ".[gui,dev]"

clean:
	rm -rf .pytest_cache .ruff_cache build dist src/*.egg-info
	find . -name __pycache__ -type d -prune -exec rm -rf {} +
