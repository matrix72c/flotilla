"""Offline inspection findings about a task's own files.

Capability admission lives in `flotilla.core.admission` and runs on the trial plan, so inspect
and `open` cannot disagree; what is left here are the checks that need the task directory
itself: bind sources that exist and stay confined, and mounts that keep out of flotilla's own
paths.
"""

from __future__ import annotations

import os
from collections.abc import Iterable
from pathlib import Path

from flotilla.compose.layout import classify
from flotilla.compose.model import Finding, Project
from flotilla.compose.paths import confined
from flotilla.platform.base import RESERVED_MOUNTS, Capabilities, overlaps


def scan(
    project: Project,
    caps: Capabilities,
    *,
    env_dir: str | None = None,
    extra: Iterable[Finding] = (),
) -> tuple[Finding, ...]:
    """Findings about the task's files. With `env_dir`, check bind sources exist and are not files."""
    findings = [*extra, *project.findings]
    if env_dir is not None:
        findings.extend(_input_paths(project, Path(env_dir)))
        findings.extend(_bind_sources(project, env_dir, caps))
    findings.extend(_share_conflicts(project))
    return tuple(findings)


def _bind_sources(project: Project, env_dir: str, caps: Capabilities) -> list[Finding]:
    layout = classify(project.services)
    out: list[Finding] = []
    for g in layout.binds:
        for unit, source, target, _ in g.references:
            path = f"services.{unit}.volumes[{target}]"
            full = os.path.join(env_dir, source)
            if not os.path.exists(full):
                # Docker would create an empty directory, but the platform mounts it over the image path the same
                # way: a task whose files were never committed starts with empty tools (or fails outright when the
                # image has a file there) and grades as unsolvable. Refuse so the dataset is fixed instead.
                out.append(
                    Finding(
                        path,
                        "reject",
                        f"bind source {source} does not exist: it would mount as an empty directory",
                    )
                )
            elif os.path.isfile(full) and not caps.volume_file:
                out.append(
                    Finding(path, "reject", f"bind source {source} is a single file; this deployment lacks file mounts")
                )
    return out


def _share_conflicts(project: Project) -> list[Finding]:
    """Keep task mounts and working directories out of framework-owned paths."""
    out: list[Finding] = []
    for name, svc in project.services.items():
        paths = [m.target for m in svc.volumes] + [m.mount_path for m in svc.tmpfs]
        for p in paths:
            for reserved in RESERVED_MOUNTS:
                if overlaps(p, reserved):
                    out.append(
                        Finding(f"services.{name}", "reject", f"{p} conflicts with the reserved mount point {reserved}")
                    )
        if any(m.target == "/tmp" and m.read_only for m in svc.volumes):
            # Framework file transfers and the platform exec daemon's command logs are written under /tmp.
            out.append(
                Finding(f"services.{name}.volumes[/tmp]", "reject", "/tmp must stay writable; mount it read-write")
            )
        if svc.working_dir and any(
            svc.working_dir == p or svc.working_dir.startswith(p + "/") for p in RESERVED_MOUNTS
        ):
            out.append(
                Finding(f"services.{name}.working_dir", "reject", "working directory conflicts with a reserved mount")
            )
    return out


def _input_paths(project: Project, root: Path) -> list[Finding]:
    out: list[Finding] = []
    for name, service in project.services.items():
        paths = [(f"volumes[{m.target}].source", m.source) for m in service.volumes if m.kind == "bind"]
        for field, relative in paths:
            try:
                confined(root, relative)
            except (ValueError, OSError, RuntimeError) as exc:
                out.append(Finding(f"services.{name}.{field}", "reject", str(exc)))
    return out
