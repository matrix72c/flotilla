"""Validated runtime plans, dependencies, and mount ownership."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Literal

from flotilla.platform.base import Resources

Condition = Literal["service_started", "service_healthy", "service_completed_successfully"]
RestartPolicy = Literal["no", "always", "unless-stopped", "on-failure"]


@dataclass(frozen=True)
class ExecContext:
    """Exec context of a service: the main process, healthchecks, and client `execute` build `ProcessSpec` from it."""

    env: Mapping[str, str]  # image ENV -> env_file -> environment, with runtime parameters substituted
    uid: int
    gid: int
    cwd: str


@dataclass(frozen=True)
class Healthcheck:
    """Docker-style healthcheck. `test` is normalized to argv: `CMD` as is, `CMD-SHELL` as `["/bin/sh","-c",...]`."""

    test: tuple[str, ...]
    interval_s: float = 30.0
    timeout_s: float = 30.0
    retries: int = 3
    start_period_s: float = 0.0
    start_interval_s: float = 5.0

    def __post_init__(self) -> None:
        if not self.test:
            raise ValueError("healthcheck.test must not be empty (leave healthcheck unset for NONE / disable)")
        if self.interval_s <= 0 or self.timeout_s <= 0 or self.retries < 1 or self.start_interval_s <= 0:
            raise ValueError("healthcheck intervals and timeout must be > 0 and retries must be >= 1")
        if self.start_period_s < 0:
            raise ValueError("start_period_s must not be negative")


@dataclass(frozen=True)
class Mount:
    """A shared-storage mount of a unit.

    `scope="task"`: `key` is relative to the published task files directory (`tasks/<build_key>/...`), shared by all
    trials, read-only.
    `scope="trial"`: `key` is relative to this trial's directory (`launches/<launch>/<trial>/...`), set up by prepare.
    share is mounted into every unit by the orchestration core, not here.
    """

    scope: Literal["task", "trial"]
    key: str
    target: str
    read_only: bool


@dataclass(frozen=True)
class Unit:
    """One sandbox: a Compose service (or one replica)."""

    image: str
    resources: Resources
    context: ExecContext
    command: (
        tuple[str, ...] | None
    )  # main process argv; None means no main process (a standalone sandbox driven only by client exec)
    healthcheck: Healthcheck | None = None
    restart: RestartPolicy = "no"
    mounts: tuple[Mount, ...] = ()
    privileged: bool = False


@dataclass(frozen=True)
class Network:
    """A Compose network. `names`: name -> services, used to generate each unit's hosts."""

    internal: bool
    members: frozenset[str]
    names: Mapping[str, frozenset[str]] = field(default_factory=dict)


@dataclass(frozen=True)
class Dependency:
    condition: Condition
    required: bool = True


@dataclass(frozen=True)
class TrialVolume:
    """A shared volume directory of this trial (a bind source group with multiple references and a writer).

    prepare creates it at `launches/<launch>/<trial>/<key>`: with `seed`, unpacks a tar from the task files into it;
    with `copy_from`, copies from the task files (preserving ownership and mode); otherwise creates an empty
    directory and chowns it to `owner`.
    """

    key: str
    owner: tuple[int, int] | None = None
    seed: str | None = None  # tar relative to the task files directory
    copy_from: str | None = None  # source directory relative to the task files directory

    def __post_init__(self) -> None:
        if self.seed is not None and self.copy_from is not None:
            raise ValueError("seed and copy_from are mutually exclusive")


@dataclass(frozen=True)
class TrialPlan:
    """All inputs of a trial. Unit names are Compose service names (replicas get their own) and `link` member names."""

    task: str  # task slug, written to the `flotilla/task` label (truncated)
    task_files: (
        str | None
    )  # published task files directory (relative to the shared root); None if there are no task files
    units: Mapping[str, Unit]
    networks: Mapping[str, Network] = field(default_factory=dict)
    depends_on: Mapping[str, Mapping[str, Dependency]] = field(default_factory=dict)
    volumes: tuple[TrialVolume, ...] = ()
    external_units: frozenset[str] = frozenset()  # units granted external access by caller policy

    def validate(self) -> None:
        """Check cross-field consistency; raise `ValueError` if not (a manifest or translation bug, not platform)."""
        units = set(self.units)
        if not units:
            raise ValueError("a trial needs at least one unit")
        for name, net in self.networks.items():
            _require_subset(net.members, units, f"members of network {name}")
            for host, targets in net.names.items():
                _require_subset(targets, net.members, f"services named {host} in network {name}")
        _require_subset(self.external_units, units, "external_units")
        _require_subset(set(self.depends_on), units, "depends_on units")
        for dependent, deps in self.depends_on.items():
            # required: false deps may be absent (e.g. profile not selected) and do not block; required ones must exist.
            _require_subset({d for d, dep in deps.items() if dep.required}, units, f"dependencies of {dependent}")
            if dependent in deps:
                raise ValueError(f"{dependent} depends on itself")
            for dep, d in deps.items():
                if dep not in self.units:
                    continue
                # Compose has preconditions on dependencies for these conditions; `docker compose up` fails otherwise.
                if d.condition == "service_healthy" and self.units[dep].healthcheck is None:
                    raise ValueError(f"{dependent} depends on {dep} with service_healthy, but {dep} has no healthcheck")
                if d.condition == "service_completed_successfully" and self.units[dep].command is None:
                    raise ValueError(f"{dependent} waits for {dep} to complete, but {dep} has no main process")
        _require_acyclic(self.depends_on)
        if self.task_files is not None:
            _require_key(self.task_files, "task_files")
        keys = [v.key for v in self.volumes]
        for key in keys:
            _require_key(key, "trial volume key")
        if len(keys) != len(set(keys)):
            raise ValueError("duplicate trial volume key")
        for name, unit in self.units.items():
            for m in unit.mounts:
                _require_key(m.key, f"mount {m.target} of {name}")
                if m.scope == "trial" and not any(m.key == k or m.key.startswith(k + "/") for k in keys):
                    raise ValueError(f"{name} mounts undeclared trial volume {m.key}")
                if m.scope == "task":
                    if self.task_files is None:
                        raise ValueError(f"{name} mounts task files, but the task has no published task files")
                    if not m.read_only:
                        raise ValueError(
                            f"{name} mounts task files {m.key} read-write: task files are read-only and shared"
                        )
            if unit.restart != "no" and unit.command is None:
                raise ValueError(f"{name} has no main process but declares a restart policy")
            if unit.healthcheck is not None and unit.command is None:
                raise ValueError(f"{name} has no main process but declares a healthcheck")
        if any(v.seed is not None or v.copy_from is not None for v in self.volumes) and self.task_files is None:
            raise ValueError(
                "trial volumes need a seed or source from the task files, but the task has no published task files"
            )


def _require_key(key: str, what: str) -> None:
    """Path relative to the shared root: no empty, `.`, or `..` segments, no leading `/`. The empty string (the root
    itself) is always rejected."""
    if not key:
        raise ValueError(f"{what} is empty: cannot mount or operate on the shared root itself")
    if any(p in ("", ".", "..") for p in key.split("/")):
        raise ValueError(f"{what} is not a valid relative path: {key!r}")


def _require_acyclic(depends_on: Mapping[str, Mapping[str, Dependency]]) -> None:
    state: dict[str, int] = {}  # 1 = visiting, 2 = done

    def visit(node: str, path: list[str]) -> None:
        if state.get(node) == 2:
            return
        if state.get(node) == 1:
            cycle = [*path[path.index(node) :], node]
            raise ValueError(f"depends_on has a dependency cycle: {' -> '.join(cycle)}")
        state[node] = 1
        for dep in sorted(depends_on.get(node, {})):
            visit(dep, [*path, node])
        state[node] = 2

    for node in sorted(depends_on):
        visit(node, [])


def _require_subset(items: frozenset[str] | set[str], universe: set[str] | frozenset[str], what: str) -> None:
    unknown = set(items) - set(universe)
    if unknown:
        raise ValueError(f"{what} reference unknown units or services: {sorted(unknown)}")
