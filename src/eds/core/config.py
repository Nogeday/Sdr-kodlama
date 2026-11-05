"""Application configuration (TOML).

Precedence: dataclass defaults < TOML file < CLI overrides (dotted keys, e.g.
``{"rx.center_freq_hz": 2.4e9}``). Unknown keys are *errors* (typos must not silently
fall back to defaults on a measurement rig).

Layout of a config file (see ``config/*.toml`` for complete examples)::

    [[device]]            # one or more receivers (2× Pluto = two [[device]] tables)
    name = "rx0"
    backend = "soapy"     # "soapy" | "sim"
    profile = "ad9363_pluto"
    args = { driver = "plutosdr", uri = "usb:1.5.5" }
    control_uri = "ip:192.168.2.1"   # libiio thin layer (optional)
    sim = { ppm = 12.0 }             # only for backend = "sim"

    [rx]  [dsp]  [detect]  [agc]  [sweep]  [bus]  [cal]  [measure]  [sim]
"""

from __future__ import annotations

import copy
import dataclasses
import sys
import types
import typing
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .errors import ConfigError

if sys.version_info >= (3, 11):
    import tomllib
else:  # pragma: no cover
    import tomli as tomllib


# --------------------------------------------------------------------------- sections
@dataclass
class DeviceConfig:
    name: str = "rx0"
    backend: str = "sim"                    # "soapy" | "sim"
    profile: str = "ad9363_pluto"
    args: dict[str, str] = field(default_factory=dict)   # Soapy device kwargs
    control_uri: str = ""                   # libiio URI for Pluto thin layer; "" = auto, "none" = off
    channels: list[int] = field(default_factory=lambda: [0])
    sim: dict[str, Any] = field(default_factory=dict)    # per-device simulator params


@dataclass
class RxConfig:
    center_freq_hz: float = 915e6
    sample_rate_hz: float = 4e6
    bandwidth_hz: float = 4e6
    gain_mode: str = "manual"               # manual | soft_agc | hw_slow | hw_fast | hw_hybrid
    gain_db: float = 40.0
    buffer_size: int = 65536                # driver buffer (Soapy stream arg 'bufflen')
    block_size: int = 65536                 # samples per IQBlock delivered to consumers
    settle_buffers: int = -1                # -1 = profile.kernel_buffers + 1
    stream_format: str = "CS16"
    dc_tracking: bool = True                # AD936x BB DC offset tracking
    rf_dc_tracking: bool = True             # AD936x RF DC offset tracking
    quad_tracking: bool = True              # AD936x quadrature (IQ imbalance) tracking
    read_timeout_s: float = 2.0
    queue_blocks: int = 32                  # RX streamer queue depth (host side)


@dataclass
class DspConfig:
    nfft: int = 4096
    window: str = "blackmanharris"          # rect | hann | hamming | blackman | blackmanharris | flattop
    overlap: float = 0.5
    averages: int = 16
    avg_mode: str = "mean"                  # mean | max | min | median
    usable_fraction: float = 0.8            # central fraction of fs treated as flat & alias-free
    dc_notch_bins: int = 3                  # bins on each side of DC treated as invalid (zero-IF)


@dataclass
class DetectConfig:
    method: str = "os_cfar"                 # os_cfar | ca_cfar | fixed
    pfa: float = 1e-6
    guard_bins: int = 4
    train_bins: int = 32
    os_rank: float = 0.75                   # OS-CFAR: rank as fraction of training cells
    min_snr_db: float = 6.0
    merge_gap_bins: int = 2
    min_bw_bins: int = 1
    fixed_threshold: float = -80.0          # for method = fixed (same unit as spectrum)
    exclude_dc: bool = True


@dataclass
class AgcConfig:
    target_peak_dbfs: float = -10.0
    high_peak_dbfs: float = -3.0
    low_peak_dbfs: float = -35.0
    max_clip_fraction: float = 1e-5
    attack_step_db: float = 10.0
    decay_step_db: float = 3.0
    hold_blocks: int = 4


@dataclass
class SweepConfig:
    start_hz: float = 325e6
    stop_hz: float = 3.8e9
    sample_rate_hz: float = 20e6            # bursts: USB limit does not apply between steps
    bandwidth_hz: float = 20e6
    nfft: int = 2048
    rbw_hz: float = 0.0                     # >0 overrides nfft (nfft = next pow2 for this RBW)
    averages: int = 4
    window: str = "blackmanharris"
    gain_mode: str = "fixed"                # fixed | auto (per-step coarse AGC)
    gain_db: float = 50.0
    usable_fraction: float = 0.8
    dc_avoid: bool = True                   # overlap steps so zero-IF DC never lands in result
    settle_buffers: int = -1
    publish_max_points: int = 8192


@dataclass
class BusConfig:
    mode: str = "direct"                    # direct | proxy
    pub_endpoint: str = "tcp://*:5555"      # direct: publisher binds here
    sub_endpoint: str = "tcp://127.0.0.1:5555"
    proxy_frontend: str = "tcp://127.0.0.1:5550"   # proxy: publishers connect (XSUB side)
    proxy_backend: str = "tcp://127.0.0.1:5551"    # proxy: subscribers connect (XPUB side)
    ctl_endpoints: list[str] = field(default_factory=list)  # direct mode: where ctl.* comes from
    validate: bool = False                  # validate outgoing messages against JSON schema
    status_period_s: float = 1.0
    spectrum_rate_hz: float = 10.0
    spectrum_max_bins: int = 4096
    hwm: int = 200


@dataclass
class CalConfig:
    dir: str = ""                           # "" = <repo>/cal
    ppm_mode: str = "auto"                  # auto | hardware | software | off
    ppm_override: float = float("nan")      # finite value forces this ppm (bypasses store)
    use_power_cal: bool = True
    temp_warn_delta_c: float = 5.0          # warn if |T - T_cal| exceeds this


@dataclass
class MeasureConfig:
    out_dir: str = ""                       # "" = <repo>/reports
    targets_file: str = "ktr_targets.toml"  # relative to config dir
    source: str = "sim"                     # sim | pluto_tx | scpi | manual | none
    source_args: dict[str, Any] = field(default_factory=dict)
    params: dict[str, dict[str, Any]] = field(default_factory=dict)   # per spec_id overrides


@dataclass
class SimConfig:
    """Shared simulated RF scene (all sim devices see the same emitters)."""

    emitters: list[dict[str, Any]] = field(default_factory=list)
    temperature_c: float = 35.0
    seed: int = 1234


@dataclass
class AppConfig:
    devices: list[DeviceConfig] = field(default_factory=lambda: [DeviceConfig()])
    rx: RxConfig = field(default_factory=RxConfig)
    dsp: DspConfig = field(default_factory=DspConfig)
    detect: DetectConfig = field(default_factory=DetectConfig)
    agc: AgcConfig = field(default_factory=AgcConfig)
    sweep: SweepConfig = field(default_factory=SweepConfig)
    bus: BusConfig = field(default_factory=BusConfig)
    cal: CalConfig = field(default_factory=CalConfig)
    measure: MeasureConfig = field(default_factory=MeasureConfig)
    sim: SimConfig = field(default_factory=SimConfig)
    source_path: str = ""

    def device(self, name: str | None = None) -> DeviceConfig:
        if name is None:
            return self.devices[0]
        for d in self.devices:
            if d.name == name:
                return d
        raise ConfigError(f"'{name}' adlı cihaz konfigürasyonda yok: {[d.name for d in self.devices]}")

    def to_dict(self) -> dict[str, Any]:
        return dataclasses.asdict(self)


# --------------------------------------------------------------------------- building
_SECTIONS = {
    "rx": RxConfig, "dsp": DspConfig, "detect": DetectConfig, "agc": AgcConfig,
    "sweep": SweepConfig, "bus": BusConfig, "cal": CalConfig, "measure": MeasureConfig,
    "sim": SimConfig,
}


def _coerce(value: Any, tp: Any, where: str) -> Any:
    origin = typing.get_origin(tp)
    if tp is Any:
        return value
    if origin in (typing.Union, types.UnionType):
        last: Exception | None = None
        for arg in typing.get_args(tp):
            if arg is type(None) and value is None:
                return None
            try:
                return _coerce(value, arg, where)
            except ConfigError as e:
                last = e
        raise last or ConfigError(where)
    if tp is float:
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            raise ConfigError(f"{where}: sayı bekleniyordu, {value!r} geldi")
        return float(value)
    if tp is int:
        if isinstance(value, bool) or not isinstance(value, (int, float)) or int(value) != value:
            raise ConfigError(f"{where}: tamsayı bekleniyordu, {value!r} geldi")
        return int(value)
    if tp is bool:
        if not isinstance(value, bool):
            raise ConfigError(f"{where}: true/false bekleniyordu, {value!r} geldi")
        return value
    if tp is str:
        if not isinstance(value, str):
            raise ConfigError(f"{where}: metin bekleniyordu, {value!r} geldi")
        return value
    if origin is list:
        (arg,) = typing.get_args(tp) or (Any,)
        if not isinstance(value, list):
            raise ConfigError(f"{where}: liste bekleniyordu, {value!r} geldi")
        return [_coerce(v, arg, f"{where}[{i}]") for i, v in enumerate(value)]
    if origin is dict:
        args = typing.get_args(tp) or (str, Any)
        if not isinstance(value, dict):
            raise ConfigError(f"{where}: tablo bekleniyordu, {value!r} geldi")
        vt = args[1]
        if vt is str:   # Soapy kwargs: stringify scalars
            return {str(k): (v if isinstance(v, str) else str(v)) for k, v in value.items()}
        return {str(k): _coerce(v, vt, f"{where}.{k}") for k, v in value.items()}
    return value


def _build(cls: type, data: dict[str, Any], where: str):
    if not isinstance(data, dict):
        raise ConfigError(f"{where}: tablo bekleniyordu")
    hints = typing.get_type_hints(cls)
    names = {f.name for f in dataclasses.fields(cls)}
    unknown = set(data) - names
    if unknown:
        raise ConfigError(f"[{where}] bilinmeyen anahtar(lar): {sorted(unknown)} — geçerli: {sorted(names)}")
    kwargs = {k: _coerce(v, hints[k], f"{where}.{k}") for k, v in data.items()}
    return cls(**kwargs)


def config_from_dict(raw: dict[str, Any], source: str = "<dict>") -> AppConfig:
    raw = dict(raw)
    allowed = set(_SECTIONS) | {"device"}
    unknown = set(raw) - allowed
    if unknown:
        raise ConfigError(f"{source}: bilinmeyen bölüm(ler): {sorted(unknown)} — geçerli: {sorted(allowed)}")
    kw: dict[str, Any] = {}
    for sec, cls in _SECTIONS.items():
        kw[sec] = _build(cls, raw.get(sec, {}), sec)
    devs = raw.get("device")
    if devs is None:
        kw["devices"] = [DeviceConfig()]
    else:
        if isinstance(devs, dict):
            devs = [devs]
        kw["devices"] = [_build(DeviceConfig, d, f"device[{i}]") for i, d in enumerate(devs)]
        if not kw["devices"]:
            raise ConfigError(f"{source}: en az bir [[device]] gerekli")
        for d in kw["devices"]:
            if not d.channels:
                raise ConfigError(f"{source}: device '{d.name}': channels boş olamaz")
        names = [d.name for d in kw["devices"]]
        if len(set(names)) != len(names):
            raise ConfigError(f"{source}: cihaz adları benzersiz olmalı: {names}")
    for d in kw["devices"]:
        if d.backend not in ("soapy", "sim"):
            raise ConfigError(f"device '{d.name}': backend 'soapy' ya da 'sim' olmalı (geldi: {d.backend!r})")
    cfg = AppConfig(**kw)
    cfg.source_path = source
    _check_enums(cfg)
    return cfg


def _check_enums(cfg: AppConfig) -> None:
    checks = [
        ("rx.gain_mode", cfg.rx.gain_mode, {"manual", "soft_agc", "hw_slow", "hw_fast", "hw_hybrid"}),
        ("dsp.window", cfg.dsp.window, {"rect", "hann", "hamming", "blackman", "blackmanharris", "flattop"}),
        ("sweep.window", cfg.sweep.window, {"rect", "hann", "hamming", "blackman", "blackmanharris", "flattop"}),
        ("dsp.avg_mode", cfg.dsp.avg_mode, {"mean", "max", "min", "median"}),
        ("detect.method", cfg.detect.method, {"os_cfar", "ca_cfar", "fixed"}),
        ("sweep.gain_mode", cfg.sweep.gain_mode, {"fixed", "auto"}),
        ("bus.mode", cfg.bus.mode, {"direct", "proxy"}),
        ("cal.ppm_mode", cfg.cal.ppm_mode, {"auto", "hardware", "software", "off"}),
        ("measure.source", cfg.measure.source, {"sim", "pluto_tx", "scpi", "manual", "none"}),
        ("rx.stream_format", cfg.rx.stream_format, {"CS16", "CF32"}),
    ]
    for key, val, allowed in checks:
        if val not in allowed:
            raise ConfigError(f"{key} = {val!r} geçersiz; seçenekler: {sorted(allowed)}")
    if not 0.0 <= cfg.dsp.overlap < 1.0:
        raise ConfigError("dsp.overlap [0, 1) aralığında olmalı")
    if not 0.1 <= cfg.dsp.usable_fraction <= 1.0 or not 0.1 <= cfg.sweep.usable_fraction <= 1.0:
        raise ConfigError("usable_fraction (0.1, 1] aralığında olmalı")


def apply_overrides(raw: dict[str, Any], overrides: dict[str, Any] | None) -> dict[str, Any]:
    """Apply dotted-key overrides to a raw TOML dict. ``device.X`` targets device[0]."""
    if not overrides:
        return raw
    raw = copy.deepcopy(raw)
    for key, value in overrides.items():
        parts = key.split(".")
        if parts[0] == "device":
            devs = raw.get("device") or [{}]
            if isinstance(devs, dict):
                devs = [devs]
            devs = [dict(d) for d in devs]
            node = devs[0]
            for p in parts[1:-1]:
                node = node.setdefault(p, {})
                if not isinstance(node, dict):
                    raise ConfigError(f"override {key}: '{p}' bir tablo değil")
            node[parts[-1]] = value
            raw["device"] = devs
            continue
        node = raw
        for p in parts[:-1]:
            node = node.setdefault(p, {})
            if not isinstance(node, dict):
                raise ConfigError(f"override {key}: '{p}' bir tablo değil")
        node[parts[-1]] = value
    return raw


def load_config(path: str | Path | None = None, overrides: dict[str, Any] | None = None) -> AppConfig:
    raw: dict[str, Any] = {}
    source = "<defaults>"
    if path is not None:
        p = Path(path)
        if not p.is_file():
            raise ConfigError(f"Konfigürasyon dosyası yok: {p}")
        try:
            with p.open("rb") as f:
                raw = tomllib.load(f)
        except tomllib.TOMLDecodeError as e:
            raise ConfigError(f"{p}: TOML sözdizimi hatası: {e}") from e
        source = str(p)
    return config_from_dict(apply_overrides(raw, overrides), source=source)
