"""Shared foundation: types, errors, config, device profiles, units, time."""

from .config import AppConfig, DeviceConfig, load_config
from .errors import (
    BackendUnavailable,
    CalibrationError,
    ConfigError,
    DeviceError,
    DeviceNotFound,
    EDSError,
    MeasurementError,
    NotSupported,
    ProfileViolation,
    StreamError,
    StreamTimeout,
)
from .profile import DeviceProfile, load_profile
from .types import DeviceInfo, GainRange, IQBlock, StreamStats

__all__ = [
    "AppConfig", "DeviceConfig", "load_config", "DeviceProfile", "load_profile",
    "DeviceInfo", "GainRange", "IQBlock", "StreamStats",
    "EDSError", "ConfigError", "ProfileViolation", "DeviceError", "DeviceNotFound",
    "BackendUnavailable", "StreamError", "StreamTimeout", "NotSupported",
    "CalibrationError", "MeasurementError",
]
