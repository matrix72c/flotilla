"""Turn a task directory into a trial plan, in one pure function.

What used to be three stages — prepare a task into a release, read the release back into a
manifest, resolve the manifest into a plan — is one call. The release existed to hold the result
of a pure function between a host that computed it and a trainer that used it; nothing else
needed it, and the measured cost of just recomputing is 0.97 ms of parse plus one cached
registry read per distinct image.

The only input flotilla cannot compute is the image config (`Entrypoint`, `Cmd`, `WorkingDir`,
`User`), which the platform does not read on its own. That comes from `ImageConfigs`, keyed by
digest and so cacheable forever.

`task_files` points straight at the dataset directory. There is no copy: `scope="task"` mounts
have always been read-only and shared by every trial, so the staging step that used to duplicate
them was moving bytes to produce the same bytes under a different name. Only a named volume with
`copy_from` is materialized per trial, and that is the trial's own work.
"""

from __future__ import annotations

import re
from collections.abc import Mapping
from dataclasses import dataclass, field, replace
from typing import Any, Protocol

from flotilla.compose.image_meta import parse_config
from flotilla.compose.layout import Layout, classify
from flotilla.compose.model import MAIN, ImageMeta, Project, Service
from flotilla.compose.task import HarborTask
from flotilla.core.plan import (
    Dependency,
    ExecContext,
    Healthcheck,
    Mount,
    Network,
    TaskSource,
    TrialPlan,
    TrialVolume,
    Unit,
)
from flotilla.platform.base import Resources
from flotilla.platform.filesystem import unsized_tmpfs_bytes

#: Image `USER` / Compose `user` values that mean root. Docker resolves `USER` by name inside the
#: image, so the string and numeric spellings are the same identity and neither needs a lookup.
ROOT_USERS = frozenset({"", "0", "0:0", "root", "root:root"})

#: `${VAR}` in an agent overlay value, referring to the service's own declared value.
_REFERENCE = re.compile(r"\$\{([A-Za-z_][A-Za-z0-9_]*)\}")


class PlanError(ValueError):
    """The task cannot become a trial plan: a rejected field, an unusable image, a missing parameter."""


class ImageConfigSource(Protocol):
    """Reads the `config` section of an image config, by digest."""

    def image_config(self, pinned: str) -> Mapping[str, Any]: ...


@dataclass(frozen=True)
class AgentOverlay:
    """Process-level environment applied to `services` (default: the agent service).

    `${VAR}` in a value refers to the service's resolved value, so `PATH=/x:${PATH}` extends what
    the service already has, whether the task declared it or the image did.
    """

    env: Mapping[str, str] = field(default_factory=dict)
    services: frozenset[str] | None = None


NO_OVERLAY = AgentOverlay()


def plan_task(
    task: HarborTask,
    source: TaskSource,
    *,
    configs: ImageConfigSource,
    resources: Mapping[str, Resources],
    overlay: AgentOverlay = NO_OVERLAY,
    external_mounts: Mapping[str, tuple[Any, ...]] | None = None,
    workspace_mounts: Mapping[str, tuple[Any, ...]] | None = None,
    workspace_root: str | None = None,
) -> TrialPlan:
    """Resolve `task` into a validated `TrialPlan`. Pure apart from `configs`, which reads a registry."""
    project = task.project
    rejected = [f for f in (*task.findings, *project.findings) if f.kind == "reject"]
    if rejected:
        raise PlanError(f"{task.name} is rejected: " + "; ".join(f"{f.path}: {f.reason}" for f in rejected[:3]))
    missing = sorted(set(project.services) - set(resources))
    if missing:
        raise PlanError(f"{task.name}: no resources resolved for {missing}")

    meta = {name: _meta(name, svc, configs) for name, svc in project.services.items()}
    layout = classify(project.services)
    prefix = _env_prefix(task)
    # `svc.environment` includes `env_file` and overrides the image's own ENV.
    env = {name: {**meta[name].env, **svc.environment} for name, svc in project.services.items()}
    targets = overlay.services if overlay.services is not None else frozenset({MAIN})
    unknown = targets - set(project.services)
    if unknown:
        raise PlanError(f"{task.name}: agent overlay names services the task does not define: {sorted(unknown)}")
    for name in targets:
        env[name] = _overlay(env[name], overlay.env)

    plan = TrialPlan(
        task=task.name,
        task_files=source.key,
        task_files_root=source.root,
        # A Harbor task's agent always works in `main`, which `normalize` guarantees exists.
        agent_service=MAIN,
        units={
            name: _unit(svc, meta[name], env[name], resources[name], layout, prefix)
            for name, svc in project.services.items()
        },
        networks=_networks(project),
        depends_on={
            name: {d: Dependency(condition=dep.condition, required=dep.required) for d, dep in svc.depends_on.items()}
            for name, svc in project.services.items()
            if svc.depends_on
        },
        volumes=_volumes(layout, prefix),
        external_units=frozenset(
            name
            for name, svc in project.services.items()
            if any(not project.networks[net].internal for net in svc.networks)
        ),
        external_mounts={name: tuple(mounts) for name, mounts in (external_mounts or {}).items()},
        workspace_mounts={name: tuple(mounts) for name, mounts in (workspace_mounts or {}).items()},
        workspace_root=workspace_root,
    )
    try:
        plan.validate()
    except ValueError as exc:
        raise PlanError(f"{task.name} does not resolve to a consistent trial: {exc}") from exc
    return plan


def synthesize_command(svc: Service, meta: ImageMeta) -> tuple[str, ...] | None:
    """The unit's main process argv, by Docker's rules.

    Compose `entrypoint` replaces the image ENTRYPOINT and also drops the image CMD; `command`
    replaces CMD alone. When both end up empty the unit has no main process, and the sandbox is
    driven by client `execute` only.
    """
    if svc.entrypoint is not None:
        entrypoint, cmd = svc.entrypoint, svc.command or ()
    else:
        entrypoint = meta.entrypoint or ()
        cmd = svc.command if svc.command is not None else (meta.cmd or ())
    return (*entrypoint, *cmd) or None


def _meta(service: str, svc: Service, configs: ImageConfigSource) -> ImageMeta:
    """The image's own `Entrypoint` / `Cmd` / `WorkingDir`, plus the identity to run as.

    An explicit Compose `user` makes the image `USER` irrelevant. A named identity is refused
    rather than resolved: resolving it meant creating a container from the image and reading its
    `/etc/passwd`, which made planning a task require a Docker daemon and a full image pull for
    two numbers. A task that needs a specific identity writes it numerically.
    """
    if svc.image is None:
        raise PlanError(f"service {service} declares no image")
    config = configs.image_config(svc.image)
    identity = svc.user if svc.user is not None else str(config.get("User") or "0:0")
    if identity in ROOT_USERS:
        identity = "0:0"
    user, _, group = identity.partition(":")
    if not user.isdigit() or (group and not group.isdigit()):
        raise PlanError(
            f"service {service} runs as {identity!r}: a named user or group would have to be looked up inside "
            "the image; declare a numeric uid[:gid] with Compose user"
        )
    image_user = str(config.get("User") or "")
    if image_user not in ROOT_USERS:
        raise PlanError(
            f"service {service} has image USER {image_user!r}: the exec daemon is the sandbox entrypoint and "
            "needs a root image; publish it with USER 0:0 and declare the application identity with Compose user"
        )
    return parse_config({**config, "User": identity})


def _overlay(env: Mapping[str, str], overlay: Mapping[str, str]) -> dict[str, str]:
    out = dict(env)
    for key, value in overlay.items():
        out[key] = _REFERENCE.sub(lambda m: env.get(m.group(1), ""), value)
    return out


def _unit(
    svc: Service,
    meta: ImageMeta,
    env: Mapping[str, str],
    resources: Resources,
    layout: Layout,
    env_prefix: str,
) -> Unit:
    assert svc.image is not None  # `_meta` refused a service without one
    check = None if svc.healthcheck_disabled else (svc.healthcheck or meta.healthcheck)
    tmpfs = tuple(
        m if m.size_bytes is not None else replace(m, size_bytes=unsized_tmpfs_bytes(resources.memory))
        for m in svc.tmpfs
    )
    return Unit(
        image=svc.image,
        resources=resources,
        context=ExecContext(env=dict(env), uid=meta.uid, gid=meta.gid, cwd=svc.working_dir or meta.workdir or "/"),
        command=synthesize_command(svc, meta),
        healthcheck=None
        if check is None
        else Healthcheck(
            test=tuple(check.test),
            interval_s=check.interval_s,
            timeout_s=check.timeout_s,
            retries=check.retries,
            start_period_s=check.start_period_s,
            start_interval_s=check.start_interval_s,
        ),
        restart=svc.restart,
        mounts=tuple(
            Mount(
                scope=m.scope,
                # A task mount names a path inside the published task directory, and a bind source
                # is relative to the environment directory, so the two have to be joined.
                key=f"{env_prefix}{m.key}" if m.scope == "task" else m.key,
                target=m.target,
                read_only=m.read_only,
            )
            for m in layout.mounts.get(svc.name, ())
        ),
        privileged=svc.privileged,
        readonly_rootfs=svc.readonly_rootfs,
        tmpfs_mounts=tmpfs,
        shm_size_bytes=svc.shm_size_bytes,
        devices=frozenset(svc.devices),
        udp_ports=svc.udp_ports,
        extra_hosts=dict(svc.extra_hosts),
    )


def _networks(project: Project) -> dict[str, Network]:
    """Members and the name table per network: service names, aliases, `hostname` and `container_name`."""
    out: dict[str, Network] = {}
    for net, definition in sorted(project.networks.items()):
        members = sorted(n for n, s in project.services.items() if net in s.networks)
        names: dict[str, set[str]] = {}
        for name in members:
            svc = project.services[name]
            for host in (name, *svc.networks[net].aliases, *filter(None, (svc.hostname, svc.container_name))):
                names.setdefault(host, set()).add(name)
        out[net] = Network(
            internal=definition.internal,
            members=frozenset(members),
            names={host: frozenset(targets) for host, targets in sorted(names.items())},
        )
    return out


def _env_prefix(task: HarborTask) -> str:
    """The environment directory as a path under the task directory, which is what `source.key` names.

    A bind source is relative to the environment directory, while the trial copies from the task
    files root, so the two have to be joined. Preparation used to sidestep this by staging the
    bind data into a flat `files/binds/<n>`; nothing is staged now, so the real path is used.
    """
    try:
        relative = task.env_dir.resolve().relative_to(task.path.resolve())
    except ValueError as exc:
        raise PlanError(f"{task.name}: the environment directory is outside the task directory") from exc
    return "" if str(relative) == "." else f"{relative.as_posix()}/"


def _volumes(layout: Layout, env_prefix: str) -> tuple[TrialVolume, ...]:
    """Directories this trial owns: a named volume starts empty, a bind group is copied from the task files."""
    named = tuple(TrialVolume(key=v.name) for v in layout.volumes)
    binds = tuple(
        TrialVolume(key=f"binds/{g.index}", copy_from=f"{env_prefix}{g.root.removeprefix('./')}")
        for g in layout.binds
        if g.mode == "trial"
    )
    return (*named, *binds)
