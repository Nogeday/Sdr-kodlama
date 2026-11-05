"""PLACEHOLDER — to be implemented. Spec: İmaj bastırma [dB]."""

from __future__ import annotations

from ..base import Measurement, MeasurementResult, register


@register
class ImageRejectionMeasurement(Measurement):
    spec_id = "image_rejection"
    title = "İmaj bastırma"
    unit = "dB"

    def run(self) -> MeasurementResult:
        raise NotImplementedError("image_rejection henüz uygulanmadı")
