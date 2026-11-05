"""PLACEHOLDER — to be implemented. Spec: Duyarlılık (MDS) [dBm]."""

from __future__ import annotations

from ..base import Measurement, MeasurementResult, register


@register
class SensitivityMeasurement(Measurement):
    spec_id = "sensitivity"
    title = "Duyarlılık (MDS)"
    unit = "dBm"

    def run(self) -> MeasurementResult:
        raise NotImplementedError("sensitivity henüz uygulanmadı")
