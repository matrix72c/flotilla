"""OpenSandbox implementation of the internal Platform protocol."""

from __future__ import annotations

import secrets
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass, replace
from datetime import datetime
from typing import Any
from urllib.parse import quote

import httpx

from flotilla.platform.base import (
    Capabilities,
    Clock,
    ErrorCategory,
    ExecAttempt,
    ExecResult,
    ExternalPolicy,
    FilesystemState,
    FlotillaError,
    InstanceHandle,
    InstanceSpec,
    InstanceState,
    InstanceStatus,
    ProcessSpec,
    ProcessStatus,
    Stage,
    Topology,
)
from flotilla.platform.opensandbox import address
from flotilla.platform.opensandbox.execd import Execd, ExecdSettings
from flotilla.platform.opensandbox.filesystem import SHM_SIZE_EXTENSION, UPSTREAM_FILESYSTEM, FilesystemSettings
from flotilla.platform.opensandbox.http import DEFAULT_RETRY, Http, RetryPolicy
from flotilla.platform.opensandbox.lifecycle import Lifecycle
from flotilla.platform.opensandbox.network import NetworkSettings, compile_policy
from flotilla.platform.opensandbox.storage import StorageSettings

CREDENTIAL_HEADER = "OPEN-SANDBOX-API-KEY"  # credentials only go in request headers
EXECD_PORT = 44772  # exec channel port
EXECD_TOKEN_ENV = "EXECD_ACCESS_TOKEN"  # env var execd reads its access token from
EXECD_TOKEN_HEADER = "X-EXECD-ACCESS-TOKEN"
API_VERSION = "v1"
MIN_TTL_SECONDS = 60  # minimum timeout
#: Default per-request timeout (command streams have a total deadline, see `http.stream_post`). The 30s read
#: timeout is well above execd's ping interval (3s).
DEFAULT_TIMEOUT = httpx.Timeout(30.0, connect=10.0)
DEFAULT_MAX_CONNECTIONS = 100  # httpx default; each in-flight foreground exec holds one connection
#: Create request fields managed by flotilla, forbidden in `create_fields`. Includes `env`: it holds only the execd
#: access token; workload env is passed per execution.
MANAGED_CREATE_FIELDS = frozenset(
    {
        "image",
        "entrypoint",
        "metadata",
        "timeout",
        "resourceLimits",
        "resourceRequests",
        "volumes",
        "networkPolicy",
        "extensions",
        "env",
        "readOnlyRootFilesystem",
        "tmpfsMounts",
    }
)


def _label_key(labels: Mapping[str, str]) -> frozenset[tuple[str, str]]:
    return frozenset(labels.items())


def _filesystem_matches(effective: FilesystemState | None, requested: FilesystemState) -> bool:
    """An explicit shm size must be echoed exactly; without one, any platform default is acceptable."""
    if effective is None:
        return False
    if requested.shm_size_bytes is None:
        effective = replace(effective, shm_size_bytes=None)
    return effective == requested


@dataclass(frozen=True)
class _Unit:
    """Backend record of an instance created by this process."""

    external: ExternalPolicy  # external policy at create (for recompiling on link)
    token: str  # execd access token
    filesystem: FilesystemState


class OpenSandboxPlatform:
    """OpenSandbox backend. Satisfies the `flotilla.platform.base.Platform` protocol."""

    def __init__(
        self,
        lifecycle: Lifecycle,
        execd_http: Callable[[str], Http],
        *,
        settings: NetworkSettings,
        storage: StorageSettings,
        caps: Capabilities,
        filesystem: FilesystemSettings = UPSTREAM_FILESYSTEM,
        execd: ExecdSettings | None = None,
        workload_http: Callable[[str], Http] | None = None,
        command_gate: Callable[[], Awaitable[None]] | None = None,
        extensions: Mapping[str, Any] | None = None,
        privileged_extensions: Mapping[str, Any] | None = None,
        create_fields: Mapping[str, Any] | None = None,
        group_extension: str | None = None,
        create_timeout_s: float | None = None,
        close: Callable[[], Awaitable[None]] | None = None,
    ) -> None:
        clash = sorted(MANAGED_CREATE_FIELDS & set(create_fields or {}))
        if clash:
            raise ValueError(f"create_fields cannot override create fields managed by flotilla: {clash}")
        if group_extension is not None and (
            group_extension in (extensions or {}) or group_extension in (privileged_extensions or {})
        ):
            raise ValueError(
                f"extensions cannot set the placement group key {group_extension!r}: flotilla writes it per trial"
            )
        if SHM_SIZE_EXTENSION in (extensions or {}) or SHM_SIZE_EXTENSION in (privileged_extensions or {}):
            raise ValueError(
                f"extensions cannot set {SHM_SIZE_EXTENSION!r}: flotilla writes it per instance from Compose shm_size"
            )
        self.caps = filesystem.capabilities(caps)
        self._filesystem = filesystem
        self._lifecycle = lifecycle
        self._execd_http = execd_http
        self._workload_http = workload_http or execd_http
        self._command_gate = command_gate
        self._settings = settings
        self._storage = storage
        self._execd = execd or ExecdSettings()
        self._create_fields = dict(create_fields or {})
        self._extensions = dict(extensions or {})
        self._privileged_extensions = dict(privileged_extensions or {})
        self._group_extension = group_extension
        self._create_timeout_s = create_timeout_s
        self._close = close
        self._units: dict[str, _Unit] = {}  # iid -> instance created by this process
        self._pending: dict[frozenset[tuple[str, str]], _Unit] = {}  # labels -> sent creates (for reconciliation)
        self._addresses: dict[str, str] = {}  # iid -> internal address (fixed while alive)

    async def create(self, spec: InstanceSpec) -> InstanceHandle:
        try:
            self.caps.require_filesystem(
                spec.readonly_rootfs,
                spec.tmpfs_mounts,
                privileged=spec.privileged,
                memory=spec.resources.memory,
                volume_paths=tuple(v.mount_path for v in spec.volumes),
                shm_size_bytes=spec.shm_size_bytes,
            )
        except ValueError as exc:
            raise FlotillaError(str(exc), stage="create", category=ErrorCategory.INVALID, retryable=False) from exc
        unit = _Unit(
            external=spec.external,
            token=secrets.token_urlsafe(32),
            filesystem=FilesystemState(
                spec.readonly_rootfs,
                tuple(sorted(spec.tmpfs_mounts, key=lambda m: m.mount_path)),
                spec.shm_size_bytes,
            ),
        )
        body = self._create_body(spec, unit.token)
        body["metadata"] = {**spec.labels, "flotilla/create-id": secrets.token_hex(8)}
        key = _label_key(body["metadata"])
        self._pending[key] = unit  # record first: recover by labels if the response is lost
        try:
            iid = await self._lifecycle.create(body, timeout=self._create_timeout_s)
        except FlotillaError as exc:
            if exc.category is not ErrorCategory.TRANSIENT:
                self._pending.pop(key, None)  # platform explicitly rejected: no instance, nothing to reconcile
            raise
        self._units[iid] = self._pending.pop(key, unit)
        return InstanceHandle(iid=iid)

    def forget_creates(self, labels: Mapping[str, str]) -> None:
        query = _label_key(labels)
        for key in tuple(self._pending):
            if query <= key:
                del self._pending[key]

    async def get(self, iid: str) -> InstanceState:
        state = await self._lifecycle.get(iid)
        self._remember(state)
        # The caller has adopted the ID before readiness checks. A mismatch cannot
        # turn a successful POST into an untracked, leaked instance.
        unit = self._units.get(iid)
        if (
            self._filesystem.protocol == "sandbox-system-v1"
            and unit
            and state.status is InstanceStatus.RUNNING
            and not _filesystem_matches(state.filesystem, unit.filesystem)
        ):
            raise FlotillaError(
                "effective filesystem configuration is missing or differs from the requested configuration",
                stage="create",
                category=ErrorCategory.INVALID,
                retryable=False,
            )
        return state

    async def delete(self, iid: str) -> None:
        await self._lifecycle.delete(iid)
        self._forget(iid)

    async def list(self, labels: Mapping[str, str]) -> list[InstanceState]:
        states = await self._lifecycle.list(labels)
        for state in states:
            self._remember(state)
        return states

    async def renew(self, iid: str, expires_at: datetime) -> None:
        await self._lifecycle.renew(iid, expires_at)

    async def exec(
        self, handle: InstanceHandle, proc: ProcessSpec, *, attempt: ExecAttempt | None = None
    ) -> ExecResult:
        execd = await self._execd_of(handle.iid, stage="run", workload=attempt is not None)
        return await execd.exec(proc, attempt=attempt)

    async def interrupt(self, handle: InstanceHandle, pid: str) -> None:
        await (await self._execd_of(handle.iid, stage="run")).interrupt(pid)

    async def start_process(
        self, handle: InstanceHandle, proc: ProcessSpec, *, attempt: ExecAttempt | None = None
    ) -> str:
        return await (await self._execd_of(handle.iid, stage="start")).start_process(proc, attempt=attempt)

    async def process_status(self, handle: InstanceHandle, pid: str) -> ProcessStatus:
        return await (await self._execd_of(handle.iid, stage="run")).process_status(pid)

    async def write_file(
        self,
        handle: InstanceHandle,
        path: str,
        data: bytes,
        *,
        mode: int,
        uid: int,
        gid: int,
    ) -> None:
        execd = await self._execd_of(handle.iid, stage="run")
        await execd.write_file(path, data, mode=mode, uid=uid, gid=gid)

    async def read_file(self, handle: InstanceHandle, path: str) -> bytes:
        return await (await self._execd_of(handle.iid, stage="run")).read_file(path)

    async def internal_address(self, handle: InstanceHandle) -> str:
        cached = self._addresses.get(handle.iid)
        if cached is None:
            execd = await self._execd_of(handle.iid, stage="address")
            await self._await_execd(execd)
            cached = await address.read_internal_address(execd, handle.iid)
            self._addresses[handle.iid] = cached
        return cached

    async def _await_execd(self, execd: Execd, *, timeout_s: float = 30.0, interval_s: float = 1.0) -> None:
        """Gate the first command on an instance behind an execd /ping readiness poll.

        kata instances read Running before the platform injects execd; the address discovery
        command would then hit a 503 from the proxy, and commands are never retried (not
        idempotent), failing the environment at the address stage. Bounded here; the trial's
        stage timeout still bounds the overall wait. Timeouts degrade to proceeding: the
        original command error is more informative than a readiness timeout.
        """
        clock = execd._http.clock  # package-internal; the injectable clock is virtual in tests
        deadline = clock.monotonic() + timeout_s
        while True:
            try:
                await execd.ping()
                return
            except FlotillaError:
                if clock.monotonic() >= deadline:
                    return
                await clock.sleep(interval_s)

    async def link(self, members: Mapping[str, InstanceHandle], topology: Topology) -> None:
        """Replace the whole policy with "external policy + peer targets"; dataplane convergence is up to deployment."""
        if self._settings.link == "sandbox":
            targets = {name: handle.iid for name, handle in members.items()}
        else:
            # Already read and cached in the address stage; usually no request here.
            targets = {name: await self.internal_address(handle) for name, handle in members.items()}
        for name, handle in members.items():
            peers = _peers_of(name, topology, targets)
            unit = await self._unit_of(handle.iid, stage="wire")
            policy = compile_policy(unit.external, peers, self._settings)
            # Upstream's Docker runtime serves this route through execd's proxy, which requires the execd token.
            await self._lifecycle.put_policy(handle.iid, policy, stage="wire", headers={EXECD_TOKEN_HEADER: unit.token})

    async def aclose(self) -> None:
        """Close the shared HTTP clients (called by whoever built the platform when done)."""
        try:
            if self._close is not None:
                await self._close()
        finally:
            self._pending.clear()
            self._units.clear()
            self._addresses.clear()

    def _create_body(self, spec: InstanceSpec, token: str) -> dict[str, Any]:
        """Create request: `env` holds only the execd token (workload env per exec); always has `networkPolicy`."""
        extensions = dict(self._extensions)
        if spec.privileged:
            extensions.update(self._privileged_extensions)
        if spec.group is not None and self._group_extension is not None:
            extensions[self._group_extension] = spec.group
        extensions.update(self._filesystem.extensions(spec))
        resources = {"cpu": spec.resources.cpu, "memory": spec.resources.memory}
        body: dict[str, Any] = {
            **self._create_fields,
            "image": {"uri": spec.image},
            "entrypoint": list(spec.entrypoint),
            "metadata": dict(spec.labels),
            "timeout": max(int(spec.timeout_seconds), MIN_TTL_SECONDS),
            "resourceLimits": resources,
            "resourceRequests": dict(resources),
            "volumes": self._storage.compile(spec.volumes),
            "networkPolicy": compile_policy(spec.external, [], self._settings),
            "env": {EXECD_TOKEN_ENV: token},
            **self._filesystem.compile(spec),
        }
        if extensions:
            body["extensions"] = extensions
        return body

    async def _execd_of(self, iid: str, *, stage: Stage, workload: bool = False) -> Execd:
        token = (await self._unit_of(iid, stage=stage)).token
        factory = self._workload_http if workload else self._execd_http
        return Execd(
            factory(iid).with_headers({EXECD_TOKEN_HEADER: token}), self._execd, command_gate=self._command_gate
        )

    async def _unit_of(self, iid: str, *, stage: Stage) -> _Unit:
        """External policy and token recorded before create. Without an iid record (reconciled), match `get` labels."""
        unit = self._units.get(iid)
        if unit is None:
            state = await self._lifecycle.get(iid)
            unit = self._pending.pop(_label_key(state.labels), None)
            if unit is None:
                raise FlotillaError(
                    f"instance {iid} was not created by this process; its external policy and execd token are unknown",
                    stage=stage,
                    category=ErrorCategory.INVALID,
                    retryable=False,
                )
            self._units[iid] = unit
        return unit

    def _remember(self, state: InstanceState) -> None:
        unit = self._pending.pop(_label_key(state.labels), None)
        if unit is not None:
            self._units[state.iid] = unit

    def _forget(self, iid: str) -> None:
        self._units.pop(iid, None)
        self._addresses.pop(iid, None)


def _peers_of(name: str, topology: Topology, targets: Mapping[str, str]) -> list[str]:
    """Targets of other members sharing at least one network with `name` (IPs or IDs, deduped, sorted)."""
    peers: set[str] = set()
    for net_members in topology.networks.values():
        if name in net_members:
            peers.update(targets[other] for other in net_members if other != name and other in targets)
    return sorted(peers)


def build(
    *,
    endpoint: str,
    credential: str,
    clock: Clock,
    settings: NetworkSettings,
    storage: StorageSettings,
    caps: Capabilities,
    filesystem: FilesystemSettings = UPSTREAM_FILESYSTEM,
    execd: ExecdSettings | None = None,
    extensions: Mapping[str, Any] | None = None,
    privileged_extensions: Mapping[str, Any] | None = None,
    create_fields: Mapping[str, Any] | None = None,
    group_extension: str | None = None,
    transport: httpx.AsyncBaseTransport | None = None,
    retry: RetryPolicy = DEFAULT_RETRY,
    execd_port: int = EXECD_PORT,
    create_timeout_s: float | None = None,
    timeout: httpx.Timeout = DEFAULT_TIMEOUT,
    max_connections: int = DEFAULT_MAX_CONNECTIONS,
    command_gate: Callable[[], Awaitable[None]] | None = None,
) -> OpenSandboxPlatform:
    """Assemble the default server proxy mode: control plane, maintenance commands, and long user commands each get
    their own pool; execd goes via the `/sandboxes/{id}/proxy/{port}` prefix. Call `aclose()` when done.

    `transport=None` uses httpx's default transport (real deployments); tests inject `httpx.MockTransport`.
    `max_connections` is the per-pool limit: long user commands cannot take control or maintenance connections.
    """

    def client() -> httpx.AsyncClient:
        return httpx.AsyncClient(
            base_url=f"{endpoint.rstrip('/')}/{API_VERSION}",
            transport=transport,
            headers={CREDENTIAL_HEADER: credential},
            timeout=timeout,
            limits=httpx.Limits(max_connections=max_connections),
            trust_env=False,
        )

    control_client, maintenance_client, workload_client = client(), client(), client()
    control = Http(control_client, clock, retry)
    maintenance = Http(maintenance_client, clock, retry)
    workload = Http(workload_client, clock, retry)

    def execd_http(iid: str) -> Http:
        return maintenance.with_prefix(f"/sandboxes/{quote(iid, safe='')}/proxy/{execd_port}")

    def workload_http(iid: str) -> Http:
        return workload.with_prefix(f"/sandboxes/{quote(iid, safe='')}/proxy/{execd_port}")

    async def close() -> None:
        # Close all pools even if one transport close fails.
        import asyncio

        await asyncio.gather(control_client.aclose(), maintenance_client.aclose(), workload_client.aclose())

    return OpenSandboxPlatform(
        Lifecycle(control),
        execd_http,
        settings=settings,
        storage=storage,
        caps=caps,
        filesystem=filesystem,
        execd=execd,
        extensions=extensions,
        privileged_extensions=privileged_extensions,
        create_fields=create_fields,
        group_extension=group_extension,
        create_timeout_s=create_timeout_s,
        workload_http=workload_http,
        command_gate=command_gate,
        close=close,
    )
