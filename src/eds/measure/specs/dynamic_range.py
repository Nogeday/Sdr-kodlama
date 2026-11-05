"""PLACEHOLDER — to be implemented. Spec: Dinamik alan [dB]."""

from __future__ import annotations

from ..base import Measurement, MeasurementResult, register


@register
class DynamicRangeMeasurement(Measurement):
    spec_id = "dynamic_range"
    title = "Dinamik alan"
    unit = "dB"

    def run(self) -> MeasurementResult:
        raise NotImplementedError("dynamic_range henüz uygulanmadı")
