"""Offline inspection findings for task semantics and declared deployment capabilities."""

from __future__ import annotations

import os
from collections.abc import Iterable
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal

from flotilla.compose.interpolate import interpolate
from flotilla.compose.layout import classify
from flotilla.compose.model import Finding, Project
from flotilla.compose.paths import confined
from flotilla.compose.task import HarborTask
from flotilla.platform.base import Capabilities

SHARE_MOUNT = "/.flotilla"

Status = Literal["accepted", "rejected"]


@dataclass(frozen=True)
class ScanResult:
    task: str
    status: Status
    findings: tuple[Finding, ...]
    restart_services: tuple[str, ...]  # services with a restart policy (for statistics)
    runtime_params: tuple[str, ...]  # runtime parameter names requiring configuration
    external_units: tuple[str, ...]  # services receiving the caller's egress policy
    services: tuple[str, ...] = ()

    def to_json(self) -> dict[str, Any]:
        """One-line report: status plus the kind and reason of every non-implemented field."""
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
    """One Harbor task directory: `task.toml`-level findings plus `scan` (including bind source checks)."""
    return scan(task.name, task.project, caps, env_dir=str(task.env_dir), extra=task.findings)


def scan(
    task: str,
    project: Project,
    caps: Capabilities,
    *,
    env_dir: str | None = None,
    extra: Iterable[Finding] = (),
) -> ScanResult:
    """Classify a normalized task against capabilities. With `env_dir`, check bind sources exist and are not files."""
    findings = [*extra, *project.findings]
    findings.extend(_deployment(project, caps))
    if env_dir is not None:
        findings.extend(_input_paths(project, Path(env_dir)))
        findings.extend(_bind_sources(project, env_dir, caps))
    findings.extend(_share_conflicts(project))
    rejected = any(f.kind == "reject" for f in findings)
    external_networks = {name for name, net in project.networks.items() if not net.internal}
    return ScanResult(
        task=task,
        status="rejected" if rejected else "accepted",
        findings=tuple(findings),
        restart_services=tuple(sorted(n for n, s in project.services.items() if s.restart != "no")),
        runtime_params=tuple(
            sorted(
                {
                    name
                    for params in project.runtime_params.values()
                    for expr, _ in params.values()
                    for name in interpolate(expr, {}).unresolved
                }
            )
        ),
        external_units=tuple(
            sorted(n for n, s in project.services.items() if external_networks.intersection(s.networks))
        ),
        services=tuple(sorted(project.services)),
    )


def _deployment(project: Project, caps: Capabilities) -> Iterable[Finding]:
    members = [n for n, s in project.services.items() if s.networks]
    external_networks = {name for name, net in project.networks.items() if not net.internal}
    if len(members) >= 2:
        if not caps.link:
            yield Finding(
                "services", "reject", "multi-service networking requires link, unavailable in this deployment"
            )
        elif len(members) > caps.link_max_members:
            yield Finding(
                "services",
                "reject",
                f"{len(members)} linked members exceed the deployment limit {caps.link_max_members}",
            )
    for name, svc in project.services.items():
        if not external_networks.intersection(svc.networks) and not caps.supports_no_network:
            yield Finding(
                f"services.{name}.networks",
                "reject",
                "no-network requires deny-all egress with only declared cluster DNS exceptions",
            )
        if not svc.networks and not (caps.inbound_isolation or caps.sandbox_ingress):
            yield Finding(
                f"services.{name}.network_mode",
                "reject",
                "network_mode none requires inbound isolation or sandbox ingress enforcement",
            )
        if svc.udp_ports and not caps.link_udp:
            yield Finding(f"services.{name}.ports", "reject", "UDP ports declared, but this deployment lacks link_udp")
        if svc.privileged and not caps.privileged_runtime:
            yield Finding(
                f"services.{name}.privileged", "reject", "requires a privileged runtime, unavailable in this deployment"
            )
        elif svc.privileged:
            yield Finding(f"services.{name}.privileged", "warn", "placed in a privileged runtime")


def _bind_sources(project: Project, env_dir: str, caps: Capabilities) -> Iterable[Finding]:
    layout = classify(project.services)
    for g in layout.binds:
        for unit, source, target, _ in g.references:
            path = f"services.{unit}.volumes[{target}]"
            full = os.path.join(env_dir, source)
            if not os.path.exists(full):
                mount = next(m for m in project.services[unit].volumes if m.target == target)
                if mount.create_host_path:
                    yield Finding(
                        path, "warn", f"bind source {source} does not exist: mounted as an empty dir, as in Docker"
                    )
                else:
                    yield Finding(path, "reject", f"bind source {source} does not exist and create_host_path is false")
            elif os.path.isfile(full) and g.mode != "image" and not caps.volume_file:
                yield Finding(
                    path, "reject", f"bind source {source} is a single file; this deployment lacks file mounts"
                )


def _share_conflicts(project: Project) -> Iterable[Finding]:
    """Reject service volumes, binds, tmpfs, or `working_dir` under `/.flotilla`: the share is mounted there."""
    for name, svc in project.services.items():
        paths = [m.target for m in svc.volumes] + list(svc.tmpfs) + ([svc.working_dir] if svc.working_dir else [])
        for p in paths:
            if p == SHARE_MOUNT or p.startswith(SHARE_MOUNT + "/"):
                yield Finding(f"services.{name}", "reject", f"{p} conflicts with the share mount point {SHARE_MOUNT}")


def _input_paths(project: Project, root: Path) -> Iterable[Finding]:
    for name, service in project.services.items():
        paths = [(f"volumes[{m.target}].source", m.source) for m in service.volumes if m.kind == "bind"]
        if service.build:
            paths.extend(
                [
                    ("build.context", service.build.context),
                    ("build.dockerfile", str(Path(service.build.context) / (service.build.dockerfile or "Dockerfile"))),
                ]
            )
        for field, relative in paths:
            try:
                confined(root, relative)
            except (ValueError, OSError, RuntimeError) as exc:
                yield Finding(f"services.{name}.{field}", "reject", str(exc))
