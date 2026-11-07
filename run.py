#!/usr/bin/env python3
"""EDS'yi tek komutla çalıştır (python -m eds.launch kısayolu).

    python run.py                    # simülasyon: bus proxy + RX düğümü + operatör GUI
    python run.py --mode hardware    # gerçek Pluto
    python run.py --help             # tüm seçenekler

Ctrl-C ya da GUI penceresini kapatmak bütün süreçleri temiz kapatır.
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent / "src"))   # kaynak ağacından da çalışsın

from eds.launch import main  # noqa: E402

raise SystemExit(main())
