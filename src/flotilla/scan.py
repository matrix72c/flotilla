"""扫描器 `flotilla scan`：逐字段归类、按能力报告判断（Architecture 第 13 节）。不调用平台。

与构建共用 `flotilla.compose` 的解析与归类代码，保证"能构建 ⇔ 扫描通过"。本模块在 Compose 本身的归类
（`Project.findings`）之上补充与部署相关的判断：多服务互联（`link`、`link_max_members`）、UDP（`link_udp`）、
特权运行时、挂载点与 `/.flotilla` 冲突、文件 bind（`volume_file`）、缺失的 bind 源。`scan_task` 再并入 Harbor
任务目录在 `task.toml` 层面的归类（`flotilla.compose.task`）。
"""

from __future__ import annotations

import os
from collections.abc import Iterable
from dataclasses import dataclass
from typing import Any, Literal

from flotilla.compose.layout import classify
from flotilla.compose.manifest_gen import param_name
from flotilla.compose.model import Finding, Project
from flotilla.compose.task import HarborTask
from flotilla.platform.base import Capabilities

SHARE_MOUNT = "/.flotilla"

Status = Literal["accepted", "rejected"]


@dataclass(frozen=True)
class ScanResult:
    task: str
    status: Status
    findings: tuple[Finding, ...]
    restart_services: tuple[str, ...]  # 带 restart 策略的服务（§3.6 统计）
    runtime_params: tuple[str, ...]  # 需要配置的运行时参数名
    external_units: tuple[str, ...]  # 获得调用方出站策略的服务（§6.4）
    services: tuple[str, ...] = ()

    def to_json(self) -> dict[str, Any]:
        """一行报告（§13）：状态与每个非"实现"字段的类别、原因。"""
        return {
            "task": self.task,
            "status": self.status,
            "services": list(self.services),
            "findings": [{"path": f.path, "kind": f.kind, "reason": f.reason} for f in self.findings],
            "restart_services": list(self.restart_services),
            "runtime_params": list(self.runtime_params),
            "external_units": list(self.external_units),
        }


def scan_task(task: HarborTask, caps: Capabilities) -> ScanResult:
    """一个 Harbor 任务目录：`task.toml` 层面的归类加上 `scan`（含 bind 源检查）。"""
    return scan(task.name, task.project, caps, env_dir=str(task.env_dir), extra=task.findings)


def scan(
    task: str,
    project: Project,
    caps: Capabilities,
    *,
    env_dir: str | None = None,
    extra: Iterable[Finding] = (),
) -> ScanResult:
    """对一个规范化后的任务按能力报告归类。`env_dir` 给出时检查 bind 源是否存在、是否为文件。"""
    findings = [*extra, *project.findings]
    findings.extend(_deployment(project, caps))
    if env_dir is not None:
        findings.extend(_bind_sources(project, env_dir, caps))
    findings.extend(_share_conflicts(project))
    rejected = any(f.kind == "reject" for f in findings)
    return ScanResult(
        task=task,
        status="rejected" if rejected else "accepted",
        findings=tuple(findings),
        restart_services=tuple(sorted(n for n, s in project.services.items() if s.restart != "no")),
        runtime_params=tuple(
            sorted({param_name(expr) for params in project.runtime_params.values() for expr, _ in params.values()})
        ),
        external_units=tuple(
            sorted(n for n, s in project.services.items() if any(not project.networks[x].internal for x in s.networks))
        ),
        services=tuple(sorted(project.services)),
    )


def _deployment(project: Project, caps: Capabilities) -> Iterable[Finding]:
    members = [n for n, s in project.services.items() if s.networks]
    if len(members) >= 2:
        if not caps.link:
            yield Finding("services", "reject", "多服务互联需要 link（C4），本部署不具备")
        elif len(members) > caps.link_max_members:
            yield Finding("services", "reject", f"互联成员 {len(members)} 个，超过部署上限 {caps.link_max_members}")
    for name, svc in project.services.items():
        if svc.udp_ports and not caps.link_udp:
            yield Finding(f"services.{name}.ports", "reject", "声明了 UDP 端口，本部署不具备 link_udp（C4 第 2 条）")
        if svc.privileged and not caps.privileged_runtime:
            yield Finding(f"services.{name}.privileged", "reject", "需要特权运行时（C14），本部署不具备")
        elif svc.privileged:
            yield Finding(f"services.{name}.privileged", "warn", "放入特权运行时（C14）")


def _bind_sources(project: Project, env_dir: str, caps: Capabilities) -> Iterable[Finding]:
    layout = classify(project.services)
    for g in layout.binds:
        for unit, source, target, _ in g.references:
            path = f"services.{unit}.volumes[{target}]"
            full = os.path.join(env_dir, source)
            if not os.path.exists(full):
                mount = next(m for m in project.services[unit].volumes if m.target == target)
                if mount.create_host_path:
                    yield Finding(path, "warn", f"bind 源 {source} 不存在：与 Docker 一致，挂载为空目录")
                else:
                    yield Finding(path, "reject", f"bind 源 {source} 不存在，且 create_host_path 为 false")
            elif os.path.isfile(full) and g.mode != "image" and not caps.volume_file:
                yield Finding(path, "reject", f"bind 源 {source} 是单个文件，本部署不支持文件挂载（C8 第 7 条）")


def _share_conflicts(project: Project) -> Iterable[Finding]:
    """服务的卷、bind、tmpfs、`working_dir` 落在 `/.flotilla` 之下时拒绝（§13）：share 固定挂在那里（§3.3）。"""
    for name, svc in project.services.items():
        paths = [m.target for m in svc.volumes] + list(svc.tmpfs) + ([svc.working_dir] if svc.working_dir else [])
        for p in paths:
            if p == SHARE_MOUNT or p.startswith(SHARE_MOUNT + "/"):
                yield Finding(f"services.{name}", "reject", f"{p} 与 share 的挂载点 {SHARE_MOUNT} 冲突")
