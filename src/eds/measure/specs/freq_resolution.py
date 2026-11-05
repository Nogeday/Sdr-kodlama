"""PLACEHOLDER — to be implemented. Spec: Frekans çözünürlüğü [Hz]."""

from __future__ import annotations

from ..base import Measurement, MeasurementResult, register


@register
class FreqResolutionMeasurement(Measurement):
    spec_id = "freq_resolution"
    title = "Frekans çözünürlüğü"
    unit = "Hz"

    def run(self) -> MeasurementResult:
        raise NotImplementedError("freq_resolution henüz uygulanmadı")
