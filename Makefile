PY ?= python3

.PHONY: help install test test-fast lint demo report-sim clean

help:
	@echo "make install     - editable kurulum (+dev bağımlılıkları)"
	@echo "make test        - tüm testler (simülatör; donanım gerekmez)"
	@echo "make test-fast   - gerçek zamanlı (realtime) testler hariç"
	@echo "make lint        - ruff"
	@echo "make demo        - simülatörde 9 KTR ölçümünün hızlı (quick) raporu"
	@echo "make report-sim  - simülatörde tam rapor"

install:
	$(PY) -m pip install -e ".[dev]"

test:
	$(PY) -m pytest

test-fast:
	$(PY) -m pytest -m "not realtime"

lint:
	ruff check src tests

demo:
	eds measure all --config config/rx_sim.toml --quick

report-sim:
	eds measure all --config config/rx_sim.toml

clean:
	rm -rf .pytest_cache .ruff_cache build dist src/*.egg-info
	find . -name __pycache__ -type d -prune -exec rm -rf {} +
