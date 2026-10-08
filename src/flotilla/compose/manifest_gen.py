"""规范化的 Compose 项目 → 任务清单（Architecture §4.2 第 5、8 步、§4.5、§3.5）。纯函数，不调用平台、不碰镜像仓库。

镜像相关的输入（digest、镜像元数据）由 `flotilla.build` 在构建机上取得后传入：

- `ImageMeta`：镜像的 `ENTRYPOINT`、`CMD`、`USER`（已按镜像内的 /etc/passwd、/etc/group 解析为数字）、`WORKDIR`、
  `ENV`、`HEALTHCHECK`（§4.2 第 5 步）；
- `images`：服务 → `repo@sha256:…`（§4.2 第 8 步）。

本模块按 Docker 规则合成业务命令与执行上下文（§3.5），按 §7.1、§4.4 分类卷与 bind，写出清单。
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field

from flotilla.compose.layout import Layout, classify
from flotilla.compose.model import MAIN, Healthcheck, Project, Service
from flotilla.manifest import (
    Context,
    Dependency,
    Finding,
    Manifest,
    Mount,
    Network,
    Resources,
    RuntimeParam,
    TrialVolume,
    Unit,
)
from flotilla.manifest import Healthcheck as ManifestHealthcheck


@dataclass(frozen=True)
class ImageMeta:
    """镜像元数据（`docker image inspect` 的 Config 部分，USER 已解析为数字）。"""

    entrypoint: tuple[str, ...] | None = None
    cmd: tuple[str, ...] | None = None
    uid: int = 0
    gid: int = 0
    workdir: str = "/"
    env: Mapping[str, str] = field(default_factory=dict)
    healthcheck: Healthcheck | None = None


def build_manifest(
    *,
    task: str,
    build_key: str,
    project: Project,
    images: Mapping[str, str],
    meta: Mapping[str, ImageMeta],
    resources: Mapping[str, Resources],
    users: Mapping[str, tuple[int, int]] | None = None,
) -> Manifest:
    """写出任务清单。`users`：Compose `user` 字段解析出的数字身份（服务 → (uid, gid)），由构建器按镜像解析。

    项目有拒绝项时不应调用（scan 先拒绝）。
    """
    if project.rejected:
        raise ValueError("项目有拒绝项，不能写清单")
    layout = classify(project.services)
    units = {
        name: _unit(svc, images[name], meta[name], resources[name], (users or {}).get(name), layout)
        for name, svc in project.services.items()
    }
    return Manifest(
        task=task,
        build_key=build_key,
        agent_service=MAIN,
        units=units,
        networks=_networks(project),
        depends_on={
            n: {d: Dependency(condition=dep.condition, required=dep.required) for d, dep in s.depends_on.items()}
            for n, s in project.services.items()
            if s.depends_on
        },
        trial_volumes=_trial_volumes(layout),
        task_files=None,  # 由 publish 写入
        runtime_params=[
            RuntimeParam(service=svc, var=var, expr=expr, param=_param_name(expr), default=default)
            for svc, params in sorted(project.runtime_params.items())
            for var, (expr, default) in sorted(params.items())
        ],
        external_units=sorted(
            n for n, s in project.services.items() if any(not project.networks[net].internal for net in s.networks)
        ),
        findings=[Finding(path=f.path, kind=f.kind, reason=f.reason) for f in project.findings],
    )


def synthesize_command(svc: Service, meta: ImageMeta) -> tuple[str, ...] | None:
    """Docker 规则（§3.5）：Compose 的 `entrypoint` 覆盖镜像 ENTRYPOINT，且设置了 `entrypoint` 时镜像 CMD 不再生效；
    `command` 覆盖 CMD。两者都为空时没有业务进程（返回 None）。"""
    if svc.entrypoint is not None:
        entrypoint = svc.entrypoint
        cmd = svc.command or ()
    else:
        entrypoint = meta.entrypoint or ()
        cmd = svc.command if svc.command is not None else (meta.cmd or ())
    argv = (*entrypoint, *cmd)
    return argv or None


def _unit(
    svc: Service,
    image: str,
    meta: ImageMeta,
    resources: Resources,
    user: tuple[int, int] | None,
    layout: Layout,
) -> Unit:
    uid, gid = user if user is not None else (meta.uid, meta.gid)
    env = {**meta.env, **svc.environment}  # 镜像 ENV → environment，后者覆盖（env_file 首版未实现）
    hc = None if svc.healthcheck_disabled else (svc.healthcheck or meta.healthcheck)
    return Unit(
        image=image,
        resources=resources,
        context=Context(env=env, uid=uid, gid=gid, cwd=svc.working_dir or meta.workdir or "/"),
        command=list(c) if (c := synthesize_command(svc, meta)) is not None else None,
        healthcheck=None
        if hc is None
        else ManifestHealthcheck(
            test=list(hc.test),
            interval_s=hc.interval_s,
            timeout_s=hc.timeout_s,
            retries=hc.retries,
            start_period_s=hc.start_period_s,
            start_interval_s=hc.start_interval_s,
        ),
        restart=svc.restart,
        mounts=[
            Mount(scope=m.scope, key=m.key, target=m.target, read_only=m.read_only)
            for m in layout.mounts.get(svc.name, ())
        ],
        privileged=svc.privileged,
    )


def _networks(project: Project) -> dict[str, Network]:
    """每个网络的成员与名字表（§3.7）：服务名、alias、`hostname` 都是名字。"""
    out: dict[str, Network] = {}
    for net, definition in sorted(project.networks.items()):
        members = sorted(n for n, s in project.services.items() if net in s.networks)
        names: dict[str, set[str]] = {}
        for n in members:
            svc = project.services[n]
            for name in (n, *svc.networks[net].aliases, *((svc.hostname,) if svc.hostname else ())):
                names.setdefault(name, set()).add(n)
        out[net] = Network(
            internal=definition.internal,
            members=members,
            names={k: sorted(v) for k, v in sorted(names.items())},
        )
    return out


def _trial_volumes(layout: Layout) -> list[TrialVolume]:
    out: list[TrialVolume] = []
    for v in layout.volumes:
        if v.mode == "trial":
            out.append(TrialVolume(key=v.name, seed=None if v.nocopy else f"seeds/{v.name}.tar"))
    for g in layout.binds:
        if g.mode == "trial":
            out.append(TrialVolume(key=f"binds/{g.index}", copy_from=f"binds/{g.index}"))
    return out


def _param_name(expr: str) -> str:
    start = expr.index("${") + 2
    end = start
    while end < len(expr) and (expr[end].isalnum() or expr[end] == "_"):
        end += 1
    return expr[start:end]
