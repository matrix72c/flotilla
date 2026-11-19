"""Backend protocol, capabilities, and injected clock contract.

The value objects and error categories the protocols use live in `flotilla.models.platform` and
`flotilla.models.filesystem`; they are re-exported here so a backend imports everything it needs
from this one module.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime
from typing import Protocol, runtime_checkable

from flotilla.models.filesystem import (
    NO_FILESYSTEM_LIMITS as NO_FILESYSTEM_LIMITS,
)
from flotilla.models.filesystem import (
    RESERVED_MOUNTS as RESERVED_MOUNTS,
)
from flotilla.models.filesystem import (
    RUNTIME_MOUNT as RUNTIME_MOUNT,
)
from flotilla.models.filesystem import (
    FilesystemLimits as FilesystemLimits,
)
from flotilla.models.filesystem import (
    FilesystemState as FilesystemState,
)
from flotilla.models.filesystem import (
    TmpfsMount as TmpfsMount,
)
from flotilla.models.filesystem import (
    overlaps as overlaps,
)
from flotilla.models.filesystem import (
    validate_mounts as validate_mounts,
)
from flotilla.models.platform import (
    DEVICES as DEVICES,
)
from flotilla.models.platform import (
    TASK_DEVICE_PATHS as TASK_DEVICE_PATHS,
)
from flotilla.models.platform import (
    WORK_ROOT as WORK_ROOT,
)
from flotilla.models.platform import (
    CommandBusy as CommandBusy,
)
from flotilla.models.platform import (
    Device as Device,
)
from flotilla.models.platform import (
    ErrorCategory as ErrorCategory,
)
from flotilla.models.platform import (
    ExecAttempt as ExecAttempt,
)
from flotilla.models.platform import (
    ExecOutcomeUnknown as ExecOutcomeUnknown,
)
from flotilla.models.platform import (
    ExecResult as ExecResult,
)
from flotilla.models.platform import (
    ExternalPolicy as ExternalPolicy,
)
from flotilla.models.platform import (
    FlotillaError as FlotillaError,
)
from flotilla.models.platform import (
    InstanceHandle as InstanceHandle,
)
from flotilla.models.platform import (
    InstanceProfile as InstanceProfile,
)
from flotilla.models.platform import (
    InstanceSpec as InstanceSpec,
)
from flotilla.models.platform import (
    InstanceState as InstanceState,
)
from flotilla.models.platform import (
    InstanceStatus as InstanceStatus,
)
from flotilla.models.platform import (
    ProcessSpec as ProcessSpec,
)
from flotilla.models.platform import (
    ProcessStatus as ProcessStatus,
)
from flotilla.models.platform import (
    Resources as Resources,
)
from flotilla.models.platform import (
    SharedVolume as SharedVolume,
)
from flotilla.models.platform import (
    Stage as Stage,
)
from flotilla.models.platform import (
    StorageRoot as StorageRoot,
)
from flotilla.models.platform import (
    TaskDevice as TaskDevice,
)
from flotilla.models.platform import (
    Topology as Topology,
)
from flotilla.models.platform import (
    check_root_name as check_root_name,
)
from flotilla.platform.network import non_dns_egress

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
    # Shared storage
    shared_volume: bool
    volume_file: bool
    # Optional runtimes
    privileged_runtime: bool
    devices: frozenset[str]  # subset of `Device`
    cluster_dns: tuple[str, ...] = ()  # explicitly declared resolver IPs; only TCP/UDP 53 may be implicit
    sandbox_ingress: bool = False  # backend configured to enforce ingress using sandbox identity rules
    readonly_rootfs: bool = False
    native_tmpfs: bool = False
    shm_size: bool = False  # explicit `/dev/shm` size per instance
    filesystem_limits: FilesystemLimits = NO_FILESYSTEM_LIMITS

    def require_filesystem(
        self,
        readonly_rootfs: bool,
        tmpfs_mounts: tuple[TmpfsMount, ...],
        *,
        privileged: bool = False,
        memory: str | None = None,
        volume_paths: tuple[str, ...] = (),
        shm_size_bytes: int | None = None,
    ) -> None:
        if readonly_rootfs and not self.readonly_rootfs:
            raise ValueError("deployment does not support readonly_rootfs")
        if tmpfs_mounts and not self.native_tmpfs:
            raise ValueError("deployment does not support native_tmpfs; directory emulation is not supported")
        if shm_size_bytes is not None and not self.shm_size:
            raise ValueError("deployment does not support shm_size")
        if readonly_rootfs and privileged:
            raise ValueError("readonly_rootfs cannot be combined with privileged")
        if tmpfs_mounts and privileged:
            raise ValueError("native_tmpfs has not been verified for privileged runtimes")
        if shm_size_bytes is not None and privileged:
            raise ValueError("shm_size has not been verified for privileged runtimes")
        validate_mounts(
            tmpfs_mounts,
            volume_paths,
            limits=self.filesystem_limits,
            memory=memory,
            shm_size_bytes=shm_size_bytes,
        )

    @property
    def supports_no_network(self) -> bool:
        return "none" in self.external_forms and not non_dns_egress(self.implicit_egress, self.cluster_dns)

    @property
    def supports_network_none(self) -> bool:
        return self.supports_no_network and (self.inbound_isolation or self.sandbox_ingress)


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
    """All core calls to the platform go through this protocol. See `docs/Writing_a_Backend.md` for the contract."""

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
