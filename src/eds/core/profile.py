"""Device profiles: the *official* physical limits of a radio, loaded from
``config/profiles/<name>.toml``.

Why profiles exist: the code must manage the hardware's real constraints instead of
ignoring them (AD9363: 325 MHz–3.8 GHz, ≤20 MHz BW, 12-bit, ±25 ppm TCXO, USB 2.0).
Every configure call is validated against the profile *before* touching the hardware,
and the effective limits are ``profile ∩ what the driver reports at runtime``.

Adding KrakenSDR / HackRF / bladeRF / USRP = adding a TOML file, not rewriting code.
"""

from __future__ import annotations

import math
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .errors import ConfigError, ProfileViolation
from .paths import profiles_dir
from .types import GainRange
from .units import fmt_hz

if sys.version_info >= (3, 11):
    import tomllib
else:  # pragma: no cover
    import tomli as tomllib


@dataclass(frozen=True, slots=True)
class GainBand:
    """Gain range valid for RF frequencies in [f_min_hz, f_max_hz)."""

    f_min_hz: float
    f_max_hz: float
    gain_min_db: float
    gain_max_db: float
    step_db: float = 1.0


@dataclass(frozen=True)
class DeviceProfile:
    name: str
    chip: str
    description: str = ""
    status: str = "supported"              # "supported" | "planned"
    backend_hint: str = "soapy"             # "soapy" | "heimdall" | ...
    soapy_driver: str = ""                  # e.g. "plutosdr"
    rx_channels: int = 1
    tx_channels: int = 0
    coherent_rx: bool = False               # channels of ONE device share LO/clock
    adc_bits: int = 12
    full_scale_counts: float = 2048.0
    freq_min_hz: float = 0.0
    freq_max_hz: float = math.inf
    bw_min_hz: float = 0.0
    bw_max_hz: float = math.inf
    sample_rate_min_hz: float = 0.0
    sample_rate_max_hz: float = math.inf
    ref_clock_hz: float = 40e6
    ref_clock_ppm: float = 25.0
    interface: str = "usb2"
    interface_max_bytes_per_s: float = 53.248e6      # USB2 HS bulk theoretical
    interface_practical_bytes_per_s: float = 32e6    # warning threshold; measure with max_rate
    bytes_per_sample: int = 4               # on-wire bytes per complex sample per channel
    kernel_buffers: int = 4                 # stale buffers after retune (libiio default)
    default_buffer_size: int = 65536
    candidate_sample_rates_hz: tuple[float, ...] = ()
    gain_bands: tuple[GainBand, ...] = ()
    notes: tuple[str, ...] = ()
    extra: dict[str, Any] = field(default_factory=dict)

    # ------------------------------------------------------------------ queries
    def gain_range(self, freq_hz: float | None = None) -> GainRange:
        """Gain range at ``freq_hz`` (band-dependent on AD936x)."""
        if not self.gain_bands:
            return GainRange(0.0, 0.0, 1.0)
        if freq_hz is None:
            b0 = self.gain_bands[0]
            return GainRange(b0.gain_min_db, b0.gain_max_db, b0.step_db)
        for b in self.gain_bands:
            if b.f_min_hz <= freq_hz < b.f_max_hz:
                return GainRange(b.gain_min_db, b.gain_max_db, b.step_db)
        # outside every band: use the closest one
        b = min(self.gain_bands, key=lambda b: min(abs(freq_hz - b.f_min_hz), abs(freq_hz - b.f_max_hz)))
        return GainRange(b.gain_min_db, b.gain_max_db, b.step_db)

    def wire_rate_bytes_per_s(self, sample_rate_hz: float, n_channels: int = 1) -> float:
        return sample_rate_hz * self.bytes_per_sample * n_channels

    # ------------------------------------------------------------------ validation
    def check_rx(
        self,
        center_freq_hz: float | None = None,
        sample_rate_hz: float | None = None,
        bandwidth_hz: float | None = None,
        gain_db: float | None = None,
        n_channels: int = 1,
    ) -> tuple[list[str], list[str]]:
        """Return ``(errors, warnings)`` as Turkish one-liners. Does not raise."""
        err: list[str] = []
        warn: list[str] = []
        if center_freq_hz is not None and not (self.freq_min_hz <= center_freq_hz <= self.freq_max_hz):
            err.append(
                f"Merkez frekans {fmt_hz(center_freq_hz)} {self.chip} aralığı dışında "
                f"[{fmt_hz(self.freq_min_hz)} – {fmt_hz(self.freq_max_hz)}]"
            )
        if bandwidth_hz is not None and not (self.bw_min_hz <= bandwidth_hz <= self.bw_max_hz):
            err.append(
                f"Analog kanal BW {fmt_hz(bandwidth_hz)} izinli aralık dışında "
                f"[{fmt_hz(self.bw_min_hz)} – {fmt_hz(self.bw_max_hz)}]"
            )
        if sample_rate_hz is not None:
            if not (self.sample_rate_min_hz <= sample_rate_hz <= self.sample_rate_max_hz):
                err.append(
                    f"Örnekleme hızı {sample_rate_hz / 1e6:.4g} MS/s izinli aralık dışında "
                    f"[{self.sample_rate_min_hz / 1e6:.4g} – {self.sample_rate_max_hz / 1e6:.4g} MS/s]"
                )
            wire = self.wire_rate_bytes_per_s(sample_rate_hz, n_channels)
            if wire > self.interface_max_bytes_per_s:
                warn.append(
                    f"{sample_rate_hz / 1e6:.4g} MS/s × {n_channels} kanal = {wire / 1e6:.1f} MB/s; "
                    f"{self.interface} teorik sınırını ({self.interface_max_bytes_per_s / 1e6:.1f} MB/s) aşıyor "
                    "→ sürekli akışta overflow kesin."
                )
            elif wire > self.interface_practical_bytes_per_s:
                warn.append(
                    f"{sample_rate_hz / 1e6:.4g} MS/s = {wire / 1e6:.1f} MB/s; {self.interface} pratik sınırının "
                    f"(~{self.interface_practical_bytes_per_s / 1e6:.0f} MB/s) üstünde → overflow beklenir. "
                    "`eds measure max_rate` ile doğrulayın."
                )
        if bandwidth_hz is not None and sample_rate_hz is not None and bandwidth_hz > sample_rate_hz:
            warn.append(
                f"Analog BW ({fmt_hz(bandwidth_hz)}) > örnekleme hızı ({fmt_hz(sample_rate_hz)}): "
                "bant kenarında aliasing olabilir."
            )
        if gain_db is not None:
            gr = self.gain_range(center_freq_hz)
            if not (gr.min_db <= gain_db <= gr.max_db):
                err.append(
                    f"Kazanç {gain_db:g} dB, {fmt_hz(center_freq_hz) if center_freq_hz else ''} için "
                    f"[{gr.min_db:g} – {gr.max_db:g} dB] aralığı dışında"
                )
        if n_channels > self.rx_channels:
            err.append(f"{n_channels} RX kanalı istendi; {self.chip} cihaz başına {self.rx_channels} kanal destekler")
        return err, warn

    def validate_rx(self, **kw) -> list[str]:
        """Raise :class:`ProfileViolation` on any error; return warnings."""
        err, warn = self.check_rx(**kw)
        if err:
            raise ProfileViolation(err)
        return warn


# ---------------------------------------------------------------------- loading
_SCALAR_FIELDS = {
    "name", "chip", "description", "status", "backend_hint", "soapy_driver", "rx_channels",
    "tx_channels", "coherent_rx", "adc_bits", "full_scale_counts", "freq_min_hz", "freq_max_hz",
    "bw_min_hz", "bw_max_hz", "sample_rate_min_hz", "sample_rate_max_hz", "ref_clock_hz",
    "ref_clock_ppm", "interface", "interface_max_bytes_per_s", "interface_practical_bytes_per_s",
    "bytes_per_sample", "kernel_buffers", "default_buffer_size",
}


def profile_from_dict(d: dict[str, Any], source: str = "<dict>") -> DeviceProfile:
    d = dict(d)
    known = _SCALAR_FIELDS | {"gain_bands", "candidate_sample_rates_hz", "notes", "extra"}
    unknown = set(d) - known
    if unknown:
        raise ConfigError(f"{source}: bilinmeyen profil anahtar(lar)ı: {sorted(unknown)}")
    if "name" not in d or "chip" not in d:
        raise ConfigError(f"{source}: profil 'name' ve 'chip' içermeli")
    bands = tuple(GainBand(**b) for b in d.pop("gain_bands", []))
    rates = tuple(float(r) for r in d.pop("candidate_sample_rates_hz", []))
    notes = tuple(d.pop("notes", []))
    extra = dict(d.pop("extra", {}))
    float_fields = {k for k in _SCALAR_FIELDS if k.endswith(("_hz", "_s", "_ppm", "_counts"))}
    for k in float_fields & set(d):
        d[k] = float(d[k])
    return DeviceProfile(gain_bands=bands, candidate_sample_rates_hz=rates, notes=notes, extra=extra, **d)


def load_profile(name_or_path: str | Path) -> DeviceProfile:
    """Load by name (``ad9363_pluto``) from ``config/profiles`` or by explicit path."""
    p = Path(name_or_path)
    if p.suffix != ".toml":
        p = profiles_dir() / f"{name_or_path}.toml"
    if not p.is_file():
        avail = sorted(x.stem for x in profiles_dir().glob("*.toml"))
        raise ConfigError(f"Cihaz profili bulunamadı: {name_or_path} (mevcut: {avail})")
    with p.open("rb") as f:
        return profile_from_dict(tomllib.load(f), source=str(p))


def list_profiles() -> list[str]:
    return sorted(x.stem for x in profiles_dir().glob("*.toml"))
