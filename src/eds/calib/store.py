"""Per-device calibration storage: ``<cal_dir>/<serial>.json``.

A :class:`DeviceCalibration` collects everything measured for one physical radio (keyed by
its serial number, ``DeviceInfo.serial``): reference-clock error (:class:`PpmEstimate`),
power offset table (:class:`PowerCalTable`), image rejection per frequency, the last
coherence report of a DF channel pair and free-form operator notes.

Files are human-readable JSON (indent 2, UTF-8) with a ``format`` version. Writes are
atomic: the JSON goes to a temporary file in the same directory, is ``fsync``-ed and then
``os.replace``-d over the target — a crash or power loss never leaves a half-written
calibration behind. Unknown keys or a corrupt file are errors (never silently ignored).
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import re
import tempfile
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from ..core.errors import CalibrationError, ConfigError
from ..core.paths import cal_dir
from ..core.timeutil import iso_utc, now_ns
from .power import PowerCalTable
from .ppm import PpmEstimate, jsonable

log = logging.getLogger(__name__)

#: Version of the on-disk format written by :meth:`DeviceCalibration.to_dict`.
CAL_FORMAT_VERSION: int = 1

_SAFE = re.compile(r"[^A-Za-z0-9._-]")
_MAX_STEM = 80


@dataclass
class DeviceCalibration:
    """All calibration data of one device (see module docstring)."""

    serial: str
    profile: str = ""
    ppm: PpmEstimate | None = None
    power: PowerCalTable | None = None
    image_rejection_db: dict[str, float] = field(default_factory=dict)
    coherence: dict[str, Any] = field(default_factory=dict)
    notes: list[str] = field(default_factory=list)
    updated_ns: int = 0

    def __post_init__(self) -> None:
        if not isinstance(self.serial, str):
            raise ConfigError(f"Kalibrasyon seri numarası metin olmalı, {self.serial!r} geldi")
        if self.ppm is not None and not isinstance(self.ppm, PpmEstimate):
            raise ConfigError("DeviceCalibration.ppm bir PpmEstimate olmalı")
        if self.power is not None and not isinstance(self.power, PowerCalTable):
            raise ConfigError("DeviceCalibration.power bir PowerCalTable olmalı")
        self.image_rejection_db = {str(k): float(v) for k, v in dict(self.image_rejection_db).items()}
        self.coherence = dict(self.coherence)
        self.notes = [str(s) for s in self.notes]
        self.updated_ns = int(self.updated_ns)

    @property
    def is_empty(self) -> bool:
        """True if nothing has been calibrated yet."""
        return (self.ppm is None and self.power is None and not self.image_rejection_db
                and not self.coherence and not self.notes)

    @property
    def temperature_c(self) -> float | None:
        """Temperature at the PPM calibration (the temperature-sensitive quantity)."""
        return self.ppm.temperature_c if self.ppm is not None else None

    def add_note(self, text: str) -> None:
        """Append a timestamped operator/process note."""
        self.notes.append(f"{iso_utc()} {text}")

    def to_dict(self) -> dict[str, Any]:
        return {
            "format": CAL_FORMAT_VERSION,
            "serial": self.serial,
            "profile": self.profile,
            "ppm": self.ppm.to_dict() if self.ppm is not None else None,
            "power": self.power.to_dict() if self.power is not None else None,
            "image_rejection_db": jsonable(self.image_rejection_db),
            "coherence": jsonable(self.coherence),
            "notes": list(self.notes),
            "updated_ns": int(self.updated_ns),
        }

    @classmethod
    def from_dict(cls, d: Mapping[str, Any]) -> DeviceCalibration:
        if not isinstance(d, Mapping):
            raise CalibrationError(f"Kalibrasyon kaydı bir tablo olmalı, {type(d).__name__} geldi")
        known = {"format", "serial", "profile", "ppm", "power", "image_rejection_db", "coherence", "notes",
                 "updated_ns"}
        unknown = set(d) - known
        if unknown:
            raise CalibrationError(f"Kalibrasyon kaydında bilinmeyen alan(lar): {sorted(unknown)}")
        fmt = d.get("format", CAL_FORMAT_VERSION)
        if fmt != CAL_FORMAT_VERSION:
            raise CalibrationError(
                f"Kalibrasyon dosya biçimi {fmt!r} desteklenmiyor (beklenen {CAL_FORMAT_VERSION}); EDS'i güncelleyin.")
        if not isinstance(d.get("serial"), str):
            raise CalibrationError("Kalibrasyon kaydında 'serial' metni eksik")
        irr = d.get("image_rejection_db") or {}
        coh = d.get("coherence") or {}
        notes = d.get("notes") or []
        if not isinstance(irr, Mapping) or not isinstance(coh, Mapping) or not isinstance(notes, list):
            raise CalibrationError("Kalibrasyon kaydında image_rejection_db/coherence tablo, notes liste olmalı")
        try:
            irr_f = {str(k): float(v) for k, v in irr.items() if v is not None}
            return cls(
                serial=d["serial"], profile=str(d.get("profile") or ""),
                ppm=PpmEstimate.from_dict(d["ppm"]) if d.get("ppm") is not None else None,
                power=PowerCalTable.from_dict(d["power"]) if d.get("power") is not None else None,
                image_rejection_db=irr_f, coherence=dict(coh), notes=[str(s) for s in notes],
                updated_ns=int(d.get("updated_ns") or 0),
            )
        except (TypeError, ValueError, ConfigError) as e:
            raise CalibrationError(f"Kalibrasyon kaydı geçersiz: {e}") from e


class CalibrationStore:
    """Directory of calibration files, one per device serial (``dir=None`` → ``cal/``)."""

    SUFFIX = ".json"

    def __init__(self, dir: str | Path | None = None):
        self.dir = Path(dir).expanduser() if dir else cal_dir()

    def path_for(self, serial: str) -> Path:
        """File of ``serial``. Characters outside ``[A-Za-z0-9._-]`` become ``_``; when the
        name had to be changed (or is very long) a short hash of the original serial is
        appended so different serials never share a file."""
        if not isinstance(serial, str) or not serial.strip():
            raise ConfigError("Kalibrasyon için boş olmayan bir seri numarası gerekli")
        stem = _SAFE.sub("_", serial.strip()).strip(".")
        if stem != serial or not stem or len(stem) > _MAX_STEM:
            digest = hashlib.sha1(serial.encode("utf-8")).hexdigest()[:8]
            stem = f"{stem[:_MAX_STEM] or 'device'}-{digest}"
        return self.dir / f"{stem}{self.SUFFIX}"

    def exists(self, serial: str) -> bool:
        return self.path_for(serial).is_file()

    def _read(self, path: Path) -> DeviceCalibration:
        try:
            text = path.read_text(encoding="utf-8")
        except OSError as e:
            raise CalibrationError(f"Kalibrasyon dosyası okunamadı: {path}: {e}") from e
        try:
            data = json.loads(text)
        except json.JSONDecodeError as e:
            raise CalibrationError(f"Kalibrasyon dosyası bozuk (JSON): {path}: {e}") from e
        try:
            return DeviceCalibration.from_dict(data)
        except CalibrationError as e:
            raise CalibrationError(f"{path}: {e}") from e

    def load(self, serial: str) -> DeviceCalibration:
        """Calibration of ``serial``; an empty :class:`DeviceCalibration` if none is stored."""
        path = self.path_for(serial)
        if not path.is_file():
            log.info("'%s' için kalibrasyon dosyası yok (%s): kalibrasyonsuz çalışılacak", serial, path)
            return DeviceCalibration(serial=serial)
        cal = self._read(path)
        if cal.serial != serial:
            raise CalibrationError(
                f"{path}: dosyadaki seri numarası '{cal.serial}', istenen '{serial}' ile uyuşmuyor")
        return cal

    def save(self, cal: DeviceCalibration) -> Path:
        """Atomically write ``cal`` (``updated_ns`` is stamped with the current time)."""
        if not isinstance(cal, DeviceCalibration):
            raise ConfigError(f"DeviceCalibration bekleniyordu, {type(cal).__name__} geldi")
        path = self.path_for(cal.serial)
        cal.updated_ns = now_ns()
        try:
            text = json.dumps(cal.to_dict(), indent=2, ensure_ascii=False, allow_nan=False) + "\n"
        except ValueError as e:
            raise CalibrationError(f"Kalibrasyon JSON'a çevrilemedi: {e}") from e
        try:
            self.dir.mkdir(parents=True, exist_ok=True)
            fd, tmp = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=self.dir)
        except OSError as e:
            raise CalibrationError(f"Kalibrasyon dizini kullanılamıyor: {self.dir}: {e}") from e
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                f.write(text)
                f.flush()
                os.fsync(f.fileno())
            os.replace(tmp, path)
        except BaseException as e:
            try:
                os.unlink(tmp)
            except FileNotFoundError:
                pass
            except OSError as e2:
                log.warning("Geçici kalibrasyon dosyası silinemedi: %s: %s", tmp, e2)
            if isinstance(e, OSError):
                raise CalibrationError(f"Kalibrasyon dosyası yazılamadı: {path}: {e}") from e
            raise
        self._fsync_dir()
        log.info("Kalibrasyon kaydedildi: %s", path)
        return path

    def _fsync_dir(self) -> None:
        """Persist the rename itself (POSIX); not available on every platform."""
        if os.name != "posix":
            return
        try:
            fd = os.open(self.dir, os.O_RDONLY)
        except OSError as e:
            log.debug("Kalibrasyon dizini fsync için açılamadı: %s", e)
            return
        try:
            os.fsync(fd)
        except OSError as e:
            log.debug("Kalibrasyon dizini fsync edilemedi: %s", e)
        finally:
            os.close(fd)

    def list(self) -> list[str]:
        """Serials of all readable calibration files (sorted). Unreadable files are logged."""
        if not self.dir.is_dir():
            return []
        out: list[str] = []
        for p in sorted(self.dir.glob(f"*{self.SUFFIX}")):
            if p.name.startswith("."):
                continue
            try:
                out.append(self._read(p).serial)
            except CalibrationError as e:
                log.warning("Kalibrasyon dosyası atlandı: %s", e)
        return sorted(out)
