"""Open devices from configuration. Backends are imported lazily so that a machine
without SoapySDR (CI, laptops) can still run everything on the simulator."""

from __future__ import annotations

from typing import Any

from ..core.config import AppConfig, DeviceConfig
from ..core.errors import ConfigError
from ..core.profile import load_profile
from .base import RxDevice

_WORLD_ATTR = "_eds_sim_world"


def get_sim_world(app: AppConfig) -> Any:
    """One shared simulated RF world per AppConfig (all sim devices see the same scene).

    The world is stored *on* the AppConfig instance (not in an id()-keyed cache, whose
    keys CPython reuses after garbage collection), so its lifetime equals the config's.
    """
    from .sim.world import SimWorld

    world = app.__dict__.get(_WORLD_ATTR)
    if world is None:
        world = SimWorld.from_config(app.sim)
        app.__dict__[_WORLD_ATTR] = world
    return world


def open_device(dev: DeviceConfig, app: AppConfig | None = None) -> RxDevice:
    profile = load_profile(dev.profile)
    if dev.backend == "sim":
        from .sim.device import SimRxDevice

        world = get_sim_world(app if app is not None else AppConfig(devices=[dev]))
        return SimRxDevice(dev, profile, world)
    if dev.backend == "soapy":
        from .soapy_backend import SoapyRxDevice

        fmt = app.rx.stream_format if app is not None else "CS16"
        return SoapyRxDevice(dev, profile, stream_format=fmt)
    raise ConfigError(f"Bilinmeyen backend: {dev.backend}")


def open_devices(app: AppConfig, names: list[str] | None = None) -> list[RxDevice]:
    devs = [d for d in app.devices if names is None or d.name in names]
    opened: list[RxDevice] = []
    try:
        for d in devs:
            opened.append(open_device(d, app))
    except Exception:
        for o in opened:
            o.close()
        raise
    return opened


def open_group(app: AppConfig, names: list[str] | None = None) -> RxDevice:
    """Open several single-channel devices as one multi-channel (non-coherent) device.

    Used for 2× Pluto DF in phase 0. With one device configured it returns that device.
    """
    devs = open_devices(app, names)
    if len(devs) == 1:
        return devs[0]
    from .group import DeviceGroup

    return DeviceGroup(devs)


def enumerate_devices(backend: str = "soapy", args: dict[str, str] | None = None) -> list[dict[str, str]]:
    """List attached devices (Soapy) or the configured simulator."""
    if backend == "soapy":
        from .soapy_backend import enumerate_soapy

        return enumerate_soapy(args or {})
    if backend == "sim":
        return [{"driver": "sim", "label": "EDS simulator", "serial": "SIM0"}]
    raise ConfigError(f"Bilinmeyen backend: {backend}")
