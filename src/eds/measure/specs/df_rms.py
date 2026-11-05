"""PLACEHOLDER — to be implemented. Spec: DF RMS hatası [°]."""

from __future__ import annotations

from ..base import Measurement, MeasurementResult, register


@register
class DfRmsMeasurement(Measurement):
    spec_id = "df_rms"
    title = "DF RMS hatası"
    unit = "°"

    def run(self) -> MeasurementResult:
        raise NotImplementedError("df_rms henüz uygulanmadı")
