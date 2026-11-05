"""PLACEHOLDER — to be implemented. Spec: Tarama hızı [GHz/s]."""

from __future__ import annotations

from ..base import Measurement, MeasurementResult, register


@register
class ScanSpeedMeasurement(Measurement):
    spec_id = "scan_speed"
    title = "Tarama hızı"
    unit = "GHz/s"

    def run(self) -> MeasurementResult:
        raise NotImplementedError("scan_speed henüz uygulanmadı")
