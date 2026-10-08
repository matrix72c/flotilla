"""任务清单 → `flotilla.core.plan.TrialPlan`：运行时参数代入、agent 叠加（Architecture §10、§4.2、§3.5）。

位于 core 之外（core 只依赖 `platform.base`，§16.2），xtuner 与 Harbor 适配共用。`request.env_vars` 不在这里并入：
它只作用于使用方的 `execute`，不进入业务进程与健康检查（§3.5），由客户端在每次执行时合并。
"""

from __future__ import annotations

import re
from collections.abc import Mapping
from dataclasses import dataclass, field

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
from flotilla.platform.base import Resources

_REF = re.compile(r"\$\{([A-Za-z_][A-Za-z0-9_]*)\}")


@dataclass(frozen=True)
class AgentOverlay:
    """§10 的 agent 叠加：进程级环境变量，叠加到 `services`（默认只有清单的 agent 服务）。

    值中的 `${VAR}` 引用服务原有的值（例如 `PATH=/x:${PATH}`）；服务没有该变量时替换为空串。
    """

    env: Mapping[str, str] = field(default_factory=dict)
    services: frozenset[str] | None = None


NO_OVERLAY = AgentOverlay()


class PlanError(ValueError):
    """清单不能变成 trial：缺运行时参数、叠加到不存在的服务等。由 provider 以 `invalid` 报告。"""


def to_plan(
    manifest: Manifest,
    *,
    runtime_params: Mapping[str, str],
    overlay: AgentOverlay = NO_OVERLAY,
) -> TrialPlan:
    """把清单变成 core 的 trial 输入（见模块说明）。结果已 `validate()`。"""
    env_by_unit = {name: dict(u.context.env) for name, u in manifest.units.items()}
    _apply_runtime_params(manifest, runtime_params, env_by_unit)
    targets = overlay.services if overlay.services is not None else frozenset({manifest.agent_service})
    unknown = targets - set(manifest.units)
    if unknown:
        raise PlanError(f"agent 叠加的服务不在任务中：{sorted(unknown)}")
    for name in targets:
        env_by_unit[name] = _overlay(env_by_unit[name], overlay.env)

    plan = TrialPlan(
        task=manifest.task,
        task_files=manifest.task_files,
        units={name: _unit(u, env_by_unit[name]) for name, u in manifest.units.items()},
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
        raise PlanError(f"清单 {manifest.task} 不一致：{exc}") from exc
    return plan


def _apply_runtime_params(
    manifest: Manifest, values: Mapping[str, str], env_by_unit: dict[str, dict[str, str]]
) -> None:
    """按配置值代入原表达式（§4.2、§10）；没配置时用默认值，没有默认值时报错。"""
    for p in manifest.runtime_params:
        if p.service not in env_by_unit:
            raise PlanError(f"运行时参数 {p.param} 指向不存在的服务 {p.service}")
        if p.param in values:
            value = values[p.param]
        elif p.default is not None:
            value = p.default
        else:
            raise PlanError(f"任务 {manifest.task} 需要运行时参数 {p.param}（{p.service}.{p.var}），配置中没有给值")
        env_by_unit[p.service][p.var] = _substitute(p.expr, p.param, value)


def _substitute(expr: str, param: str, value: str) -> str:
    """把表达式中对 `param` 的引用（`${P}`、`${P:-d}`、`${P-d}`）替换为 `value`。"""
    return re.sub(r"\$\{" + re.escape(param) + r"(?:(?::-|-)[^}]*)?\}", lambda _: value, expr)


def _overlay(env: dict[str, str], overlay: Mapping[str, str]) -> dict[str, str]:
    out = dict(env)
    for key, value in overlay.items():
        out[key] = _REF.sub(lambda m: env.get(m.group(1), ""), value)
    return out


def _unit(u: ManifestUnit, env: dict[str, str]) -> Unit:
    hc = u.healthcheck
    return Unit(
        image=u.image,
        resources=Resources(cpu=u.resources.cpu, memory=u.resources.memory),
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
    )
