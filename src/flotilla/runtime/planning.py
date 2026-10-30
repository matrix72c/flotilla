"""Resolve private prepared manifests into runtime plans and admission requirements."""

from __future__ import annotations

import re
from collections.abc import Mapping
from dataclasses import dataclass, field, replace

from flotilla.compose.interpolate import interpolate
from flotilla.core.plan import (
    Dependency,
    ExecContext,
    Healthcheck,
    Mount,
    Network,
    TrialPlan,
    TrialVolume,
    Unit,
)
from flotilla.manifest import Manifest
from flotilla.manifest import Unit as ManifestUnit
from flotilla.platform.base import Resources, TmpfsMount
from flotilla.platform.filesystem import memory_bytes

_REF = re.compile(r"\$\{([A-Za-z_][A-Za-z0-9_]*)\}")


@dataclass(frozen=True)
class AgentOverlay:
    """Agent overlay: process-level environment variables applied to `services` (default: the manifest agent service).

    `${VAR}` in a value refers to the service's original value (e.g. `PATH=/x:${PATH}`); empty if the service lacks it.
    """

    env: Mapping[str, str] = field(default_factory=dict)
    services: frozenset[str] | None = None


NO_OVERLAY = AgentOverlay()


class PlanError(ValueError):
    """The manifest cannot become a trial: missing runtime parameters, overlay on unknown services, etc."""


def to_plan(
    manifest: Manifest,
    *,
    runtime_params: Mapping[str, str],
    overlay: AgentOverlay = NO_OVERLAY,
    resources: Mapping[str, Resources] | None = None,
) -> TrialPlan:
    """Turn a manifest into core trial input (see module docs). The result is already `validate()`d."""
    env_by_unit = {name: dict(u.context.env) for name, u in manifest.units.items()}
    _apply_runtime_params(manifest, runtime_params, env_by_unit)
    targets = overlay.services if overlay.services is not None else frozenset({manifest.agent_service})
    unknown = targets - set(manifest.units)
    if unknown:
        raise PlanError(f"agent overlay services are not in the task: {sorted(unknown)}")
    for name in targets:
        env_by_unit[name] = _overlay(env_by_unit[name], overlay.env)

    plan = TrialPlan(
        task=manifest.task,
        task_files=manifest.task_files,
        units={
            name: _unit(u, env_by_unit[name], resources.get(name) if resources else None)
            for name, u in manifest.units.items()
        },
        networks={
            name: Network(
                internal=n.internal,
                members=frozenset(n.members),
                names={host: frozenset(svcs) for host, svcs in n.names.items()},
            )
            for name, n in manifest.networks.items()
        },
        depends_on={
            u: {d: Dependency(condition=dep.condition, required=dep.required) for d, dep in deps.items()}
            for u, deps in manifest.depends_on.items()
        },
        volumes=tuple(
            TrialVolume(key=v.key, owner=v.owner, seed=v.seed, copy_from=v.copy_from) for v in manifest.trial_volumes
        ),
        external_units=frozenset(manifest.external_units),
    )
    try:
        plan.validate()
    except ValueError as exc:
        raise PlanError(f"manifest {manifest.task} is inconsistent: {exc}") from exc
    return plan


def _apply_runtime_params(
    manifest: Manifest, values: Mapping[str, str], env_by_unit: dict[str, dict[str, str]]
) -> None:
    """Substitute configured values into the original expressions; fall back to defaults, error if there is none."""
    for p in manifest.runtime_params:
        if p.service not in env_by_unit:
            raise PlanError(f"runtime parameter {p.param} targets unknown service {p.service}")
        result = interpolate(p.expr, values)
        missing = [name for name, default in result.unresolved.items() if default is None]
        if missing:
            raise PlanError(
                f"task {manifest.task} requires runtime parameters {', '.join(missing)} ({p.service}.{p.var})"
            )
        env_by_unit[p.service][p.var] = str(result.value)


_TMPFS_FLOOR_BYTES = 64 * 1024 * 1024


def _resolve_tmpfs(mounts: tuple[TmpfsMount, ...], resources: Resources) -> tuple[TmpfsMount, ...]:
    """Give unsized tmpfs mounts a concrete size: docker bounds them only by the container memory,
    so half the service memory (the Linux tmpfs convention) is the closest fixed approximation,
    floored at the historical 64 MiB so small services keep working."""
    default = max(_TMPFS_FLOOR_BYTES, memory_bytes(resources.memory) // 2)
    return tuple(m if m.size_bytes is not None else replace(m, size_bytes=default) for m in mounts)


def _overlay(env: dict[str, str], overlay: Mapping[str, str]) -> dict[str, str]:
    out = dict(env)
    for key, value in overlay.items():
        out[key] = _REF.sub(lambda m: env.get(m.group(1), ""), value)
    return out


def _unit(u: ManifestUnit, env: dict[str, str], resources: Resources | None) -> Unit:
    hc = u.healthcheck
    if resources is None:
        if u.resources.cpu is None or u.resources.memory is None:
            raise PlanError("resolve deployment resources before planning a task with unspecified resource fields")
        resources = Resources(cpu=u.resources.cpu, memory=u.resources.memory)
    return Unit(
        image=u.image,
        resources=resources,
        context=ExecContext(env=env, uid=u.context.uid, gid=u.context.gid, cwd=u.context.cwd),
        command=tuple(u.command) if u.command is not None else None,
        healthcheck=None
        if hc is None
        else Healthcheck(
            test=tuple(hc.test),
            interval_s=hc.interval_s,
            timeout_s=hc.timeout_s,
            retries=hc.retries,
            start_period_s=hc.start_period_s,
            start_interval_s=hc.start_interval_s,
        ),
        restart=u.restart,
        mounts=tuple(Mount(scope=m.scope, key=m.key, target=m.target, read_only=m.read_only) for m in u.mounts),
        privileged=u.privileged,
        readonly_rootfs=u.readonly_rootfs,
        tmpfs_mounts=_resolve_tmpfs(u.tmpfs_mounts, resources),
        shm_size_bytes=u.shm_size_bytes,
    )
