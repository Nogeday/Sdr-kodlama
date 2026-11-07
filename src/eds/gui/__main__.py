"""``python -m eds.gui`` — open the operator console."""

import sys


def run() -> int:
    try:
        from .app import main
    except ImportError as e:   # PySide6 / pyqtgraph missing, or Qt system libraries missing
        print(f"HATA: GUI başlatılamadı: {e}\n"
              "GUI bağımlılıkları: pip install -e \".[gui]\"   (ya da ./setup.sh --sim)\n"
              "Linux'ta Qt sistem kütüphaneleri eksikse: sudo apt install libegl1 libxkbcommon0 libxcb-cursor0",
              file=sys.stderr)
        return 1
    return main()


if __name__ == "__main__":
    sys.exit(run())
