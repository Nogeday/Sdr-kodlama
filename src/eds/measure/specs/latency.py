"""PLACEHOLDER — to be implemented. Spec: Uçtan-uca gecikme [ms]."""

from __future__ import annotations

from ..base import Measurement, MeasurementResult, register


@register
class LatencyMeasurement(Measurement):
    spec_id = "latency"
    title = "Uçtan-uca gecikme"
    unit = "ms"

    def run(self) -> MeasurementResult:
        raise NotImplementedError("latency henüz uygulanmadı")
