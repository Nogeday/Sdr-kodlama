"""Exception hierarchy. Every module raises subclasses of :class:`EDSError` so callers
(CLI, RX node, measurement runner) can catch one base type and report cleanly."""

from __future__ import annotations


class EDSError(Exception):
    """Base class for all EDS errors."""


class ConfigError(EDSError):
    """Invalid / unknown configuration key or value."""


class ProfileViolation(ConfigError):
    """A requested setting violates the physical limits of the device profile.

    ``violations`` holds one human-readable (Turkish) line per violated limit so the
    operator sees *all* problems at once, not just the first.
    """

    def __init__(self, violations: list[str]):
        self.violations = list(violations)
        super().__init__("Cihaz profili ihlali:\n  - " + "\n  - ".join(self.violations))


class DeviceError(EDSError):
    """Hardware / driver level failure (open, configure, stream setup)."""


class DeviceNotFound(DeviceError):
    """No device matched the given selection arguments."""


class BackendUnavailable(DeviceError):
    """A required optional backend module (SoapySDR, iio, ...) is not importable."""


class StreamError(DeviceError):
    """Unrecoverable stream error (device unplugged, stream corrupted)."""


class StreamTimeout(StreamError):
    """No samples arrived within the read timeout."""


class NotSupported(EDSError):
    """The device/backend cannot perform the requested operation."""


class BusError(EDSError):
    """Message bus transport / contract error (ZMQ endpoint, encode/decode, schema)."""


class CalibrationError(EDSError):
    """Calibration could not be computed (e.g. reference tone not found / SNR too low)."""


class MeasurementError(EDSError):
    """A measurement procedure failed or its preconditions are not met."""
