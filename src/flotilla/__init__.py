"""Public API for running Compose task directories on sandbox platforms."""

from __future__ import annotations

from flotilla._version import __version__
from flotilla.deployment import DatasetPeak, Deployment, RuntimeLayers, TaskValidation
from flotilla.models.plan import TaskSource, TrialPlan
from flotilla.models.platform import (
    CommandBusy,
    ErrorCategory,
    ExecOutcomeUnknown,
    ExecResult,
    FlotillaError,
    ProcessStatus,
)
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
    "CommandBusy",
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
    "Process",
    "ProcessStatus",
    "ResourceReservation",
    "RuntimeEvent",
    "RuntimeLayers",
    "Service",
    "ServiceInfo",
    "TaskSource",
    "TaskValidation",
    "TrialPlan",
    "__version__",
]
