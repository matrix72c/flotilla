"""Value objects and error categories of the platform contract, without the behavior.

Everything here is pure data plus construction checks: creation parameters, instance
handles and states, execution results, and the error classification callers use to
decide retries. `flotilla.platform.base` keeps the Platform and Clock protocols and the
capability report, and re-exports these names, so backends may keep importing from there.
"""

from __future__ import annotations

import re
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum
from typing import Literal

from flotilla.models.filesystem import FilesystemState, TmpfsMount

# Error classification


class ErrorCategory(StrEnum):
    """Error category; the core uses it to decide retries and the `retryable` reported to callers.

    The first 6 are categories backends map platform errors to; `LOST` and `SERVICE` are decided by the core at
    runtime: `LOST` is an infrastructure failure after readiness (retryable), `SERVICE` is the task's own fault --
    service start failure, health check failure, a one-shot service exiting non-zero, or a service with a restart
    policy exiting (not retryable, sample discarded). `SERVICE` (category) and `FlotillaError.service` (service
    name) are different things.
    """

    RATE_LIMITED = "rate_limited"
    CAPACITY = "capacity"
    TRANSIENT = "transient"
    IMAGE = "image"
    INVALID = "invalid"
    NOT_FOUND = "not_found"
    LOST = "lost"
    SERVICE = "service"


# Trial stage labels.
Stage = Literal["prepare", "create", "address", "wire", "hosts", "start", "run"]


class InstanceStatus(StrEnum):
    """Normalized instance status. Backends map native platform states here; the native string goes in `reason`.

    The core only understands these semantic states and never relies on platform spellings (waiting for
    `RUNNING`, judging `lost`): `PENDING` cannot run commands yet; `RUNNING` can run commands; `TERMINAL` has
    ended (failed, exited, or deleting) and never returns to `RUNNING`; `UNKNOWN` the backend cannot normalize
    (including states the platform adds later), which the core treats as unhealthy.
    """

    PENDING = "pending"
    RUNNING = "running"
    TERMINAL = "terminal"
    UNKNOWN = "unknown"


class FlotillaError(Exception):
    """Base flotilla error, carrying `stage` / `category` / `retryable`.

    Callers use these attributes to record the failure stage and category; `retryable` does not mean commands
    with side effects may be replayed.
    """

    def __init__(
        self,
        message: str,
        *,
        stage: Stage,
        category: ErrorCategory,
        retryable: bool,
        service: str | None = None,
    ) -> None:
        super().__init__(message)
        self.stage = stage
        self.category = category
        self.retryable = retryable
        self.service = service


class CommandBusy(FlotillaError):
    """The exec channel explicitly rejected a command before accepting it. No automatic replay."""

    def __init__(self, message: str, *, stage: Stage):
        super().__init__(message, stage=stage, category=ErrorCategory.TRANSIENT, retryable=True)


class ExecOutcomeUnknown(FlotillaError):
    """Execution may have taken effect. Never turn this into reward or replay it."""

    def __init__(self, message: str, *, stdout: bytes = b"", stderr: bytes = b"", command_id: str | None = None):
        super().__init__(message, stage="run", category=ErrorCategory.TRANSIENT, retryable=False)
        self.stdout = stdout
        self.stderr = stderr
        self.command_id = command_id


# Creation parameters and value objects


@dataclass(frozen=True)
class Resources:
    """CPU / memory limits, passed to the platform verbatim without conversion."""

    cpu: str  # e.g. "2", "0.5"
    memory: str  # e.g. "4Gi"


#: Which configured storage root a volume key is relative to: `"work"` (the writable `[storage]` root: launches,
#: share, anchor state) or the name of a read-only `[inputs.<name>]` root (published task releases, external
#: read-only payloads).
StorageRoot = str
WORK_ROOT = "work"
_ROOT_NAME = re.compile(r"[a-z][a-z0-9_-]{0,62}")


def check_root_name(name: str) -> None:
    if not isinstance(name, str) or not _ROOT_NAME.fullmatch(name):
        raise ValueError(f"storage root names are lowercase identifiers: {name!r}")


@dataclass(frozen=True)
class SharedVolume:
    """A subdirectory mount on shared storage."""

    key: str  # path relative to the shared root, no ".."; the anchor key is "" (the shared root itself)
    mount_path: str
    read_only: bool
    root: StorageRoot = WORK_ROOT

    def __post_init__(self) -> None:
        check_root_name(self.root)
        if self.root != WORK_ROOT and not self.read_only:
            raise ValueError("input storage volumes must be read-only")


@dataclass(frozen=True)
class ExternalPolicy:
    """Egress policy of one instance toward targets outside the group; fixed for the trial."""

    mode: Literal["none", "any", "allowlist"]
    hosts: tuple[str, ...] = ()  # domain names
    cidrs: tuple[str, ...] = ()


# Internal creation profile selected by trusted deployment wiring, never task data.
InstanceProfile = Literal["default", "management"]
#: A host device a deployment may expose to a sandbox.
Device = Literal["fuse", "kvm", "tun"]
#: Every one of them. A runtime that cannot host a device refuses the create request carrying its
#: extension, so a deployment is assumed to offer all three rather than measured for them.
DEVICES: frozenset[Device] = frozenset({"fuse", "kvm", "tun"})
#: Devices a task may request, by the device node it declares. They need the privileged runtime --
#: including FUSE, for which the platform asks no special runtime, because flotilla writes the
#: device extension per instance and only the privileged create path carries it.
TaskDevice = Literal["fuse", "kvm", "tun"]
TASK_DEVICE_PATHS: Mapping[str, TaskDevice] = {
    "/dev/fuse": "fuse",
    "/dev/kvm": "kvm",
    "/dev/net/tun": "tun",
}


@dataclass(frozen=True)
class InstanceSpec:
    """Parameters for creating one instance. Contains only flotilla concepts.

    There is no env field: workload env vars may contain secrets and platform query APIs may echo creation
    parameters verbatim, so env is only passed per execution.
    """

    image: str  # repo@sha256:...
    entrypoint: tuple[str, ...]  # placeholder entrypoint, not the workload command
    labels: Mapping[str, str]
    timeout_seconds: int  # must be finite
    resources: Resources
    volumes: tuple[SharedVolume, ...]
    external: ExternalPolicy
    privileged: bool = False
    #: Placement group: the deployment places instances with equal values where they can link to each other.
    #: flotilla only expresses "same group", not where; deployments that don't need it ignore it. A trial gives
    #: units that must link the same value, one per trial.
    group: str | None = None
    readonly_rootfs: bool = False
    tmpfs_mounts: tuple[TmpfsMount, ...] = ()
    shm_size_bytes: int | None = None  # None keeps the platform's default `/dev/shm`
    devices: frozenset[TaskDevice] = frozenset()  # requires `privileged`
    # Independent of task privileged permission; only workspace anchors select management.
    profile: InstanceProfile = "default"

    def __post_init__(self) -> None:
        if self.profile not in ("default", "management"):
            raise ValueError("instance profile must be default or management")
        if self.profile == "management" and self.privileged:
            raise ValueError("management profile cannot be combined with task privileged permission")
        if not self.devices <= set(TASK_DEVICE_PATHS.values()):
            raise ValueError(f"unknown devices: {sorted(self.devices - set(TASK_DEVICE_PATHS.values()))}")
        if self.devices and not self.privileged:
            raise ValueError("devices require the privileged runtime")


@dataclass(frozen=True)
class ProcessSpec:
    """Process spec for one execution (foreground or background).

    The process environment is **exactly** `env`, without platform component env mixed in (the backend fills
    in what is needed; see the backend docs).
    """

    argv: tuple[str, ...]
    uid: int
    gid: int
    cwd: str
    env: Mapping[str, str]  # complete process environment
    timeout_s: float | None  # required for exec; None for background processes


@dataclass(frozen=True)
class Topology:
    """Intra-group link topology: network name -> member names (keys of members)."""

    networks: Mapping[str, frozenset[str]]


# Instance handles and state


@dataclass(frozen=True)
class InstanceHandle:
    """Instance handle returned by create.

    Contract: a handle can always be rebuilt from `iid` alone -- it carries nothing that cannot be derived from
    `iid`. On the create reconciliation path the core may only get `iid` from `list(labels)` (response lost,
    instance created), and must then build a handle from `iid` for `internal_address` / `link` / `exec`. Backends
    needing details such as the execd address keep an internal `iid -> details` map (OpenSandbox resolves the
    execd address via iid anyway) rather than stuffing them into the handle.
    """

    iid: str


@dataclass(frozen=True)
class InstanceState:
    """Instance state returned by `get` / `list`. `reason` / `message` are for diagnostics and error messages only."""

    iid: str
    status: InstanceStatus  # normalized semantic status; native platform state goes in reason
    expires_at: datetime | None
    labels: Mapping[str, str]
    reason: str | None = None
    message: str | None = None
    filesystem: FilesystemState | None = None


@dataclass
class ExecAttempt:
    """Owned by one foreground call; never shared across retries or environments."""

    sent: bool = False
    command_id: str | None = None


@dataclass(frozen=True)
class ExecResult:
    """Result of a foreground execution.

    Past `ProcessSpec.timeout_s` the backend kills the process and returns `timed_out=True` (`exit_code` is
    whatever the backend got and must not be relied on). A timeout is a result of the command, not an exec
    channel failure: health checks count it as one failure, not as an infrastructure error.
    """

    exit_code: int
    stdout: bytes
    stderr: bytes
    timed_out: bool = False


@dataclass(frozen=True)
class ProcessStatus:
    """Status query result for a background process.

    `found=False` means it could not be found (e.g. the exec daemon lost its records after a restart); the core
    treats it as `lost`.
    """

    found: bool
    running: bool
    exit_code: int | None
