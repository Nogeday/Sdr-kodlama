"""PLACEHOLDER — to be implemented. Spec: Taşma-sız maksimum örnekleme hızı [MS/s]."""

from __future__ import annotations

from ..base import Measurement, MeasurementResult, register


@register
class MaxRateMeasurement(Measurement):
    spec_id = "max_rate"
    title = "Taşma-sız maksimum örnekleme hızı"
    unit = "MS/s"

    def run(self) -> MeasurementResult:
        raise NotImplementedError("max_rate henüz uygulanmadı")
