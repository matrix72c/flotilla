"""Backend protocol, structured outcomes, capabilities, and injected clock contract."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum
from typing import Literal, Protocol, runtime_checkable

from flotilla.platform.filesystem import (
    NO_FILESYSTEM_LIMITS,
)
from flotilla.platform.filesystem import (
    RESERVED_MOUNTS as RESERVED_MOUNTS,
)
from flotilla.platform.filesystem import (
    RUNTIME_MOUNT as RUNTIME_MOUNT,
)
from flotilla.platform.filesystem import (
    FilesystemLimits as FilesystemLimits,
)
from flotilla.platform.filesystem import (
    FilesystemState as FilesystemState,
)
from flotilla.platform.filesystem import (
    TmpfsMount as TmpfsMount,
)
from flotilla.platform.filesystem import (
    overlaps as overlaps,
)
from flotilla.platform.filesystem import (
    validate_mounts as validate_mounts,
)
from flotilla.platform.network import non_dns_egress

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


# Capability declaration


@dataclass(frozen=True)
class Capabilities:
    """Capability report from the deployment (produced by `flotilla probe`), loaded at provider `start`.

    Fields map one-to-one to the capabilities in `Platform_Requirements.md`, not to any platform field.
    Only capabilities the first interface version uses are included; the report may record more for evaluation.
    """

    # Lifecycle
    max_label_value_len: int
    list_visibility_s: float
    max_ttl_seconds: int | None
    # Intra-group linking and inbound isolation
    link: bool
    link_udp: bool
    link_max_members: int
    inbound_isolation: bool  # security
    # External egress
    external_forms: frozenset[str]  # subset of {"none", "any", "allowlist"}
    implicit_egress: tuple[str, ...]
    # Instance address
    internal_address: Literal["api", "exec", "none"]
    # Shared storage
    shared_volume: bool
    volume_file: bool
    no_auto_mount: bool  # security
    # Exec channel authentication
    exec_auth: bool  # security
    # Optional runtimes
    privileged_runtime: bool
    devices: frozenset[str]  # subset of {"fuse", "kvm"}
    cluster_dns: tuple[str, ...] = ()  # explicitly declared resolver IPs; only TCP/UDP 53 may be implicit
    sandbox_ingress: bool = False  # backend configured to enforce ingress using sandbox identity rules
    readonly_rootfs: bool = False
    native_tmpfs: bool = False
    filesystem_limits: FilesystemLimits = NO_FILESYSTEM_LIMITS

    def require_filesystem(
        self,
        readonly_rootfs: bool,
        tmpfs_mounts: tuple[TmpfsMount, ...],
        *,
        privileged: bool = False,
        memory: str | None = None,
        volume_paths: tuple[str, ...] = (),
    ) -> None:
        if readonly_rootfs and not self.readonly_rootfs:
            raise ValueError("deployment does not support readonly_rootfs")
        if tmpfs_mounts and not self.native_tmpfs:
            raise ValueError("deployment does not support native_tmpfs; directory emulation is not supported")
        if readonly_rootfs and privileged:
            raise ValueError("readonly_rootfs cannot be combined with privileged")
        if tmpfs_mounts and privileged:
            raise ValueError("native_tmpfs has not been verified for privileged runtimes")
        validate_mounts(tmpfs_mounts, volume_paths, limits=self.filesystem_limits, memory=memory)

    @property
    def supports_no_network(self) -> bool:
        return "none" in self.external_forms and not non_dns_egress(self.implicit_egress, self.cluster_dns)

    @property
    def supports_network_none(self) -> bool:
        return self.supports_no_network and (self.inbound_isolation or self.sandbox_ingress)


# Creation parameters and value objects


@dataclass(frozen=True)
class Resources:
    """CPU / memory limits, passed to the platform verbatim without conversion."""

    cpu: str  # e.g. "2", "0.5"
    memory: str  # e.g. "4Gi"


@dataclass(frozen=True)
class SharedVolume:
    """A subdirectory mount on shared storage."""

    key: str  # path relative to the shared root, no ".."; the anchor key is "" (the shared root itself)
    mount_path: str
    read_only: bool


@dataclass(frozen=True)
class ExternalPolicy:
    """Egress policy of one instance toward targets outside the group; fixed for the trial."""

    mode: Literal["none", "any", "allowlist"]
    hosts: tuple[str, ...] = ()  # domain names
    cidrs: tuple[str, ...] = ()


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


# Clock injection seam


@runtime_checkable
class Clock(Protocol):
    """Injectable clock. The core reads time and waits only through it, so timing invariants test deterministically.

    The real implementation uses the system clock and `asyncio.sleep`; tests use a manually advanced one.
    """

    def now(self) -> datetime: ...

    def monotonic(self) -> float: ...

    async def sleep(self, seconds: float) -> None: ...


# Platform interface


@runtime_checkable
class Platform(Protocol):
    """All core calls to the platform go through this protocol. See `docs/Architecture.md` for the backend contract."""

    caps: Capabilities

    # Lifecycle
    async def create(self, spec: InstanceSpec) -> InstanceHandle:
        """Create an instance. Not idempotent; the core does not retry but reconciles.

        On error the platform may already have accepted the request. Backends must report "unknown whether the
        platform received it" (5xx, dropped connection, timeout) as `transient` -- only then does the core
        reconcile by labels to recover the instance; other categories are reported as "no usable instance", but
        release still sweeps by labels. The call may be cancelled (the core cancels in-flight creates when a
        launch is stopped).
        """
        ...

    def forget_creates(self, labels: Mapping[str, str]) -> None:
        """Drop local unresolved-create metadata after the owner confirms its scope is empty.

        Memory-only, idempotent, no remote effects. Must not be called during reconciliation.
        """
        ...

    async def get(self, iid: str) -> InstanceState: ...

    async def aclose(self) -> None:
        """Close transport resources owned by this platform."""
        ...

    async def delete(self, iid: str) -> None:
        """Delete; a missing instance counts as success."""
        ...

    async def list(self, labels: Mapping[str, str]) -> list[InstanceState]:
        """Filter by labels, paging internally to fetch all; only for reconciliation, reclaim, and gc."""
        ...

    async def renew(self, iid: str, expires_at: datetime) -> None: ...

    # Execution, processes, files
    async def exec(
        self, handle: InstanceHandle, proc: ProcessSpec, *, attempt: ExecAttempt | None = None
    ) -> ExecResult:
        """Foreground exec: return exit code and output; past `proc.timeout_s` kill it and return `timed_out=True`."""
        ...

    async def start_process(
        self, handle: InstanceHandle, proc: ProcessSpec, *, attempt: ExecAttempt | None = None
    ) -> str:
        """Start a background process and return its process ID."""
        ...

    async def interrupt(self, handle: InstanceHandle, pid: str) -> None:
        """Signal this exact command. Caller must confirm termination using process_status."""
        ...

    async def process_status(self, handle: InstanceHandle, pid: str) -> ProcessStatus:
        """Query background process status; return `found=False` when not found."""
        ...

    async def write_file(
        self,
        handle: InstanceHandle,
        path: str,
        data: bytes,
        *,
        mode: int,
        uid: int,
        gid: int,
    ) -> None: ...

    async def read_file(self, handle: InstanceHandle, path: str) -> bytes: ...

    # Address
    async def internal_address(self, handle: InstanceHandle) -> str: ...

    # Network
    async def link(
        self,
        members: Mapping[str, InstanceHandle],
        topology: Topology,
    ) -> None:
        """Intra-group linking: the topology is in effect when the call returns. Idempotent."""
        ...
