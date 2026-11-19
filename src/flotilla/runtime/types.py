"""Immutable values crossing the caller/background-loop boundary."""

from __future__ import annotations

import math
from dataclasses import dataclass, field

from flotilla.core.reaper import CleanupRecord
from flotilla.models.plan import TrialPlan
from flotilla.models.platform import SharedVolume


@dataclass(frozen=True)
class RunRequest:
    """One environment to open: the trial to create, plus what is still the caller's to choose.

    The plan already carries resources and mounts, because resolving those is part of planning
    the task. What stays here is what a single run decides: how to be identified, what the agent
    process sees, whether it may reach the network, and how long it may live.
    """

    plan: TrialPlan
    request_id: str
    exec_env: dict[str, dict[str, str]] = field(default_factory=dict, repr=False)
    disable_internet: bool = False
    lifetime: float | None = None
    workspace_id: str | None = None

    def __post_init__(self) -> None:
        if not self.request_id:
            raise ValueError("request_id must be nonempty")


@dataclass(frozen=True)
class RuntimeEvent:
    """Small diagnostic record: no argv, output, environment values or credentials."""

    kind: str
    environment_id: str
    request_id: str
    task: str  # the task slug, for correlating records of one environment
    elapsed_s: float = 0
    service: str | None = None
    category: str | None = None
    attempt_id: str | None = None
    command_id: str | None = None


@dataclass(frozen=True)
class ServiceInfo:
    name: str
    instance_id: str
    image: str
    cwd: str
    cpu: str
    memory: str


@dataclass(frozen=True)
class EnvironmentInfo:
    environment_id: str
    request_id: str
    task: str
    agent_service: str
    services: tuple[ServiceInfo, ...]
    timings: tuple[tuple[str, float], ...]
    state: str
    #: Isolation properties this deployment is configured to run without
    #: (`security.accept_insecure`). What a deployment actually lacks is measured by
    #: `flotilla probe`, in another process, so a running environment can only report what was
    #: accepted -- never claim the rest were verified here.
    accepted_security_gaps: tuple[str, ...] = ()


@dataclass(frozen=True)
class CleanupReport:
    launch_id: str
    cleaned: int
    remaining: tuple[CleanupRecord, ...] = ()
    launch_directories: tuple[str, ...] = ()
    anchor_instances: tuple[str, ...] = ()
    uncertain: bool = False

    @property
    def clean(self) -> bool:
        return not (self.remaining or self.launch_directories or self.anchor_instances or self.uncertain)


class CleanupError(RuntimeError):
    def __init__(self, report: CleanupReport) -> None:
        self.report = report
        super().__init__(f"launch {report.launch_id}: cleanup incomplete; inspect .report")


@dataclass(frozen=True)
class OpenOptions:
    request_id: str | None = None
    admission_timeout: float | None = None
    startup_timeout: float | None = None
    lifetime: float | None = None
    exec_env: dict[str, dict[str, str]] = field(default_factory=dict, repr=False)
    resources: dict[str, dict[str, str]] = field(default_factory=dict)
    #: Compose interpolation values for this one task, over the deployment's own table.
    parameters: dict[str, str] = field(default_factory=dict)
    disable_internet: bool = False
    # Caller-owned, read-only directories relative to the deployment's shared root, by service.
    external_mounts: dict[str, tuple[SharedVolume, ...]] = field(default_factory=dict)
    # Attempt-owned mounts may be writable, but must remain under this client's workspace.
    workspace_id: str | None = None
    #: Storage key of that workspace, which every `workspace_mounts` key must be inside.
    workspace_root: str | None = None
    workspace_mounts: dict[str, tuple[SharedVolume, ...]] = field(default_factory=dict)

    def __post_init__(self) -> None:
        for value in (self.admission_timeout, self.startup_timeout, self.lifetime):
            if value is not None:
                require_timeout(value)
        if self.request_id == "":
            raise ValueError("request_id must be nonempty")


def require_timeout(value: float, name: str = "timeout") -> None:
    """The one check every caller-supplied duration passes through."""
    if not math.isfinite(value) or value <= 0:
        raise ValueError(f"{name} must be finite and positive")


class AdmissionTimeout(TimeoutError):
    """No trial_concurrency/instance_budget slot freed up within the admission timeout; nothing was created."""


# One active generation plus one reserved generation for retries/deferred deletion.
ENVIRONMENT_RESERVATION_COPIES = 2


@dataclass(frozen=True)
class ResourceReservation:
    """Infrastructure admission budget; resources are per service before copies."""

    resources: dict[str, dict[str, str]]
    copies: int = 1

    @property
    def instances(self) -> int:
        return len(self.resources) * self.copies
