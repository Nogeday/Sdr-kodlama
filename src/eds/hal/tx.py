"""Signal sources (stimulus) used by the measurement suite.

A measurement such as sensitivity, dynamic range or image rejection needs a controllable
tone at the receiver input. Implementations:

* ``eds.hal.sim.source.SimSignalSource`` — injects an emitter into the simulated world.
* ``eds.hal.tx_hw.SoapyTxSource``         — 2nd Pluto (later HackRF) TX → attenuators → RX.
* ``eds.hal.tx_hw.ScpiSignalGenerator``   — lab generator over SCPI/TCP (port 5025).
* ``eds.hal.tx_hw.ManualSource``          — prompts the operator to set a generator by hand.

Power semantics: if ``absolute_power`` is True, ``set_power(dbm)`` is the power *at the
receiver input connector* (cable/attenuator losses included via ``path_loss_db``). If
False (e.g. uncalibrated Pluto TX), ``set_power`` is *relative* dB where 0 = the source's
maximum; measurements must then anchor absolute level with a calibrated RX reading.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from typing import Any

from ..core.errors import ConfigError, NotSupported


class SignalSource(ABC):
    name: str = "source"
    absolute_power: bool = False
    max_tones: int = 1
    power_range_db: tuple[float, float] = (-140.0, 0.0)
    freq_range_hz: tuple[float, float] = (0.0, 6e9)

    @abstractmethod
    def set_frequency(self, hz: float) -> float:
        """Set CW tone frequency (RF, Hz). Returns the actual frequency."""

    @abstractmethod
    def set_power(self, dbm: float) -> float:
        """Set level (absolute dBm at DUT input, or relative dB — see module doc)."""

    @abstractmethod
    def set_enabled(self, on: bool) -> int:
        """RF on/off. Returns best estimate of host ``time.time_ns()`` when it took effect."""

    def set_tones(self, freqs_hz: list[float], powers: list[float]) -> None:
        """Multi-tone output (e.g. two-tone IMD). Optional."""
        raise NotSupported(f"{self.name}: çoklu ton desteklenmiyor")

    def set_lo_offset(self, hz: float) -> float:
        """Place the source's own LO ``hz`` away from the tone (optional).

        Sources that synthesize the tone digitally around an LO (Pluto/HackRF TX) carry an LO
        leakage line and a TX IQ image at ``LO − (f − LO)``. Measurements choose the offset so
        these artifacts never land on the bins they measure. Returns the offset in effect.
        """
        raise NotSupported(f"{self.name}: LO ofseti ayarlanamaz (dahili LO'su olmayan kaynak)")

    def describe(self) -> dict[str, Any]:
        return {"name": self.name, "absolute_power": self.absolute_power}

    def close(self) -> None:  # noqa: B027 - optional hook
        pass

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        try:
            self.set_enabled(False)
        finally:
            self.close()


def open_source(kind: str, args: dict[str, Any] | None = None, *, world: Any = None) -> SignalSource | None:
    """Factory used by the CLI / measurement runner.

    kind: "sim" (needs ``world``), "pluto_tx", "scpi", "manual", "none".
    """
    args = dict(args or {})
    if kind == "none":
        return None
    if kind == "sim":
        from .sim.source import SimSignalSource

        if world is None:
            raise ConfigError("sim kaynağı için SimWorld gerekli (measure.source = sim yalnızca sim backend ile)")
        return SimSignalSource(world, **args)
    if kind == "pluto_tx":
        from .tx_hw import SoapyTxSource

        return SoapyTxSource(**args)
    if kind == "scpi":
        from .tx_hw import ScpiSignalGenerator

        return ScpiSignalGenerator(**args)
    if kind == "manual":
        from .tx_hw import ManualSource

        return ManualSource(**args)
    raise ConfigError(f"Bilinmeyen sinyal kaynağı türü: {kind!r} (sim | pluto_tx | scpi | manual | none)")
