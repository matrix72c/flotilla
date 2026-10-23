"""Immutable value types for parsed Compose projects and image metadata."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Literal

from flotilla.platform.base import TmpfsMount

Kind = Literal["equivalent", "warn", "reject"]
Condition = Literal["service_started", "service_healthy", "service_completed_successfully"]
RestartPolicy = Literal["no", "always", "unless-stopped", "on-failure"]

MAIN = "main"  # Harbor's agent service name (kept alive)


@dataclass(frozen=True)
class Finding:
    """Classification of a non-implemented field. `path` is the field path, e.g. `services.target.read_only`."""

    path: str
    kind: Kind
    reason: str


@dataclass(frozen=True)
class Build:
    context: str  # relative to the task environment directory
    dockerfile: str | None = None
    args: Mapping[str, str] = field(default_factory=dict)


@dataclass(frozen=True)
class Healthcheck:
    """`test` normalized to argv: `CMD` as-is, `CMD-SHELL` as `["/bin/sh","-c",...]`. Durations in seconds."""

    test: tuple[str, ...]
    interval_s: float = 30.0
    timeout_s: float = 30.0
    retries: int = 3
    start_period_s: float = 0.0
    start_interval_s: float = 5.0


@dataclass(frozen=True)
class Dependency:
    condition: Condition = "service_started"
    required: bool = True


@dataclass(frozen=True)
class NetworkAttachment:
    aliases: tuple[str, ...] = ()


@dataclass(frozen=True)
class VolumeMount:
    """A volume reference (long syntax). For `kind="volume"`, `source` is a top-level named volume; for
    `kind="bind"`, a path relative to the environment directory."""

    kind: Literal["volume", "bind"]
    source: str
    target: str
    read_only: bool = False
    nocopy: bool = False
    create_host_path: bool = True  # missing bind source: Docker creates an empty dir (long syntax default true)


@dataclass(frozen=True)
class Service:
    name: str
    image: str | None = None
    build: Build | None = None
    command: tuple[str, ...] | None = None
    entrypoint: tuple[str, ...] | None = None
    environment: Mapping[str, str] = field(default_factory=dict)
    working_dir: str | None = None
    user: str | None = None
    healthcheck: Healthcheck | None = None
    healthcheck_disabled: bool = False  # `disable: true` or `test: [NONE]`: image HEALTHCHECK is also ignored
    depends_on: Mapping[str, Dependency] = field(default_factory=dict)
    networks: Mapping[str, NetworkAttachment] = field(default_factory=dict)  # empty = network_mode: none
    volumes: tuple[VolumeMount, ...] = ()
    tmpfs: tuple[TmpfsMount, ...] = ()
    readonly_rootfs: bool = False
    restart: RestartPolicy = "no"
    privileged: bool = False
    container_name: str | None = None
    hostname: str | None = None
    udp_ports: tuple[int, ...] = ()
    resources: Mapping[str, str] = field(default_factory=dict)


@dataclass(frozen=True)
class NetworkDef:
    internal: bool = False


@dataclass(frozen=True)
class Project:
    """A task's normalized Compose project. `runtime_params`: `environment` entries referencing variables the task
    lacks (service -> variable -> original expression, default), valued by the provider at runtime."""

    services: Mapping[str, Service]
    networks: Mapping[str, NetworkDef]
    volumes: frozenset[str]  # top-level named volumes
    runtime_params: Mapping[str, Mapping[str, tuple[str, str | None]]] = field(default_factory=dict)
    findings: tuple[Finding, ...] = ()

    @property
    def rejected(self) -> bool:
        return any(f.kind == "reject" for f in self.findings)


@dataclass(frozen=True)
class ImageMeta:
    """Image metadata (the Config part of `docker image inspect`, with USER resolved to numbers)."""

    entrypoint: tuple[str, ...] | None = None
    cmd: tuple[str, ...] | None = None
    uid: int = 0
    gid: int = 0
    workdir: str = "/"
    env: Mapping[str, str] = field(default_factory=dict)
    healthcheck: Healthcheck | None = None
