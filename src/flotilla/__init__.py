"""Public API for preparing and running Compose environments on sandbox platforms."""

from __future__ import annotations

from flotilla._version import __version__
from flotilla.platform.base import ErrorCategory, ExecOutcomeUnknown, ExecResult, FlotillaError, ProcessStatus
from flotilla.prepared import PreparedEnvironment, PreparedTask, load_prepared
from flotilla.runtime.client import Flotilla
from flotilla.runtime.environment import Environment, Process, Service
from flotilla.runtime.types import (
    CleanupError,
    CleanupReport,
    EnvironmentInfo,
    OpenOptions,
    ResourceReservation,
    RuntimeEvent,
    ServiceInfo,
)

__all__ = [
    "CleanupError",
    "CleanupReport",
    "Environment",
    "EnvironmentInfo",
    "ErrorCategory",
    "ExecOutcomeUnknown",
    "ExecResult",
    "Flotilla",
    "FlotillaError",
    "OpenOptions",
    "PreparedEnvironment",
    "PreparedTask",
    "Process",
    "ProcessStatus",
    "ResourceReservation",
    "RuntimeEvent",
    "Service",
    "ServiceInfo",
    "__version__",
    "load_prepared",
]
