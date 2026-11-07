"""EDS operator console (GUI): a ZeroMQ bus subscriber — PySide6 + pyqtgraph.

Install the extra dependencies with ``pip install -e ".[gui]"`` (no SoapySDR/libiio needed).
Run with ``python -m eds.gui`` or, together with the pipeline, ``python -m eds.launch``.
The Qt-free parts (:mod:`eds.gui.model`, :mod:`eds.gui.busclient`) import without PySide6.
"""
