"""PLACEHOLDER — to be implemented. Spec: Anlık bant genişliği [MHz]."""

from __future__ import annotations

from ..base import Measurement, MeasurementResult, register


@register
class InstBwMeasurement(Measurement):
    spec_id = "inst_bw"
    title = "Anlık bant genişliği"
    unit = "MHz"

    def run(self) -> MeasurementResult:
        raise NotImplementedError("inst_bw henüz uygulanmadı")
