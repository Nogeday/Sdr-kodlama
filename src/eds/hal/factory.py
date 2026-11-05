"""Open devices from configuration. Backends are imported lazily so that a machine
without SoapySDR (CI, laptops) can still run everything on the simulator.

Process isolation (``DeviceConfig.isolation``)
----------------------------------------------
SoapyPlutoSDR keeps ONE libiio context per process (see :mod:`eds.hal.proc`), so a process
must never hold more than one Pluto. :func:`isolation_plan` decides where each device runs:

* ``"inproc"`` / ``"subprocess"`` force the choice.
* ``"auto"``: a soapy device whose driver is ``plutosdr`` (``args.driver``, else the
  profile's ``soapy_driver``) runs in a worker process (:class:`~eds.hal.proc.ProcessRxDevice`)
  when the process would otherwise hold more than one Pluto: the Pluto devices opened
  together PLUS a ``pluto_tx`` measurement source that runs in-process. Everything else
  (sim devices, non-Pluto radios, a single Pluto) stays in-process.

Decision table for ``"auto"`` (what the process would hold → placement):

====================================================  ========================================
2 Pluto [[device]] (2x Pluto DF group)                both devices in their own worker process
1 Pluto + ``measure.source = "pluto_tx"`` (default)   DUT in-process; the TX Pluto runs in a
                                                      worker (``open_source`` isolates it)
1 Pluto + ``pluto_tx`` with ``source_args.isolate     DUT in a worker process (the source
= false``                                             stays in-process)
1 Pluto alone / sim devices / non-Pluto soapy         in-process
====================================================  ========================================

:func:`open_device` honours ``dev.isolation`` alone (``"auto"`` = in-process: one device
cannot conflict with itself); :func:`open_devices` / :func:`open_group` apply the plan.
"""

from __future__ import annotations

import logging
from typing import Any

from ..core.config import AppConfig, DeviceConfig
from ..core.errors import ConfigError, EDSError
from ..core.profile import load_profile
from .base import RxDevice

log = logging.getLogger(__name__)

_WORLD_ATTR = "_eds_sim_world"
ISOLATION_MODES = ("auto", "inproc", "subprocess")


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


# ---------------------------------------------------------------------------- isolation
def is_pluto_config(dev: DeviceConfig) -> bool:
    """True for a soapy device that SoapyPlutoSDR will open (``driver`` arg or profile)."""
    if dev.backend != "soapy":
        return False
    driver = str(dev.args.get("driver", "")).strip().lower()
    if not driver:
        try:
            driver = str(load_profile(dev.profile).soapy_driver).strip().lower()
        except EDSError:
            driver = ""
    return driver == "plutosdr"


def pluto_tx_in_process(app: AppConfig) -> bool:
    """True if ``measure.source = "pluto_tx"`` would open its Pluto in THIS process."""
    from .proc import pluto_tx_isolated

    return app.measure.source == "pluto_tx" and not pluto_tx_isolated(app.measure.source_args)


def _check_mode(dev: DeviceConfig) -> str:
    mode = str(dev.isolation)
    if mode not in ISOLATION_MODES:
        raise ConfigError(f"device '{dev.name}': isolation 'auto' | 'inproc' | 'subprocess' olmalı (geldi: {mode!r})")
    return mode


def isolation_plan(app: AppConfig, devices: list[DeviceConfig] | None = None) -> dict[str, str]:
    """``{device name: "inproc" | "subprocess"}`` for ``devices`` (default: all configured)
    opened together in one process (see module docstring)."""
    devs = list(app.devices if devices is None else devices)
    plutos = {d.name for d in devs if is_pluto_config(d)}
    src_inproc = pluto_tx_in_process(app)
    crowded = len(plutos) + int(src_inproc) > 1
    plan: dict[str, str] = {}
    for d in devs:
        mode = _check_mode(d)
        if mode == "auto":
            mode = "subprocess" if crowded and d.name in plutos else "inproc"
        plan[d.name] = mode
    left = sorted(n for n in plutos if plan[n] == "inproc") + (["measure.source=pluto_tx"] if src_inproc else [])
    if len(left) > 1:
        log.warning("Bu süreçte %d Pluto aynı anda açılacak (%s): SoapyPlutoSDR süreç başına TEK libiio bağlamı "
                    "tutar, ikinci Pluto sessizce birinciye bağlanır ve birini kapatmak diğerinin bağlamını yok "
                    "eder. isolation = \"auto\" ya da \"subprocess\" kullanın (pluto_tx için source_args.isolate "
                    "= true).", len(left), ", ".join(left))
    return plan


# ---------------------------------------------------------------------------- open
def _open(dev: DeviceConfig, app: AppConfig | None, mode: str) -> RxDevice:
    if mode == "subprocess":
        from .proc import ProcessRxDevice

        return ProcessRxDevice(dev, app)
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


def open_device(dev: DeviceConfig, app: AppConfig | None = None) -> RxDevice:
    """Open one device; ``dev.isolation = "subprocess"`` runs it in a worker process
    (:class:`~eds.hal.proc.ProcessRxDevice`), ``"auto"`` / ``"inproc"`` in this process."""
    mode = _check_mode(dev)
    return _open(dev, app, "subprocess" if mode == "subprocess" else "inproc")


def open_devices(app: AppConfig, names: list[str] | None = None) -> list[RxDevice]:
    """Open the configured devices (all, or ``names``) placed by :func:`isolation_plan`."""
    devs = [d for d in app.devices if names is None or d.name in names]
    plan = isolation_plan(app, devs)
    opened: list[RxDevice] = []
    try:
        for d in devs:
            opened.append(_open(d, app, plan[d.name]))
    except Exception:
        for o in opened:
            try:
                o.close()
            except EDSError as e:
                log.warning("%s: açılış hatası sonrası kapatma başarısız: %s", o.name, e)
        raise
    return opened


def open_group(app: AppConfig, names: list[str] | None = None) -> RxDevice:
    """Open several single-channel devices as one multi-channel (non-coherent) device.

    Used for 2× Pluto DF in phase 0 (each Pluto in its own worker process, see
    :func:`isolation_plan`). With one device configured it returns that device.
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
