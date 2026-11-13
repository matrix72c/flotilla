"""Public API for preparing and running Compose environments on sandbox platforms."""

from __future__ import annotations

from flotilla._version import __version__
from flotilla.deployment import DatasetPeak, Deployment, RowValidation, RuntimeLayers
from flotilla.platform.base import ErrorCategory, ExecOutcomeUnknown, ExecResult, FlotillaError, ProcessStatus
from flotilla.prepared import PreparedEnvironment, PreparedTask, load_prepared
from flotilla.runtime.client import Flotilla
from flotilla.runtime.environment import Environment, Process, Service
from flotilla.runtime.types import (
    AdmissionTimeout,
    CleanupError,
    CleanupReport,
    EnvironmentInfo,
    OpenOptions,
    ResourceReservation,
    RuntimeEvent,
    ServiceInfo,
)

__all__ = [
    "AdmissionTimeout",
    "CleanupError",
    "CleanupReport",
    "DatasetPeak",
    "Deployment",
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
    "RowValidation",
    "RuntimeEvent",
    "RuntimeLayers",
    "Service",
    "ServiceInfo",
    "__version__",
    "load_prepared",
]
