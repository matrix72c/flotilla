"""Docker-free inspection of task environments: scan findings and effective resources."""

from __future__ import annotations

from collections.abc import Callable, Sequence
from dataclasses import dataclass
from pathlib import Path

from flotilla.compose.adapter import ComposeConfigAdapter
from flotilla.compose.task import HarborTask, load_task
from flotilla.config import FlotillaConfig
from flotilla.scan import scan_task


@dataclass(frozen=True)
class EnvironmentInspection:
    task_root: Path
    directory: Path
    name: str
    status: str
    findings: tuple[dict[str, str], ...]
    resources: dict[str, dict[str, str]]


def main_only(path: Path, *, compose_adapter: ComposeConfigAdapter) -> list[HarborTask]:
    return [load_task(path, compose_adapter=compose_adapter)]


def inspect(
    cfg: FlotillaConfig,
    path: str | Path,
    environments: Callable[..., Sequence[HarborTask]] = main_only,
) -> tuple[EnvironmentInspection, ...]:
    """Inspect every environment of the task at `path` against the deployment's capabilities.

    Loading uses the deployment's pinned Compose tool, so findings and resources
    match what preparation would compute for the same task.
    """
    adapter = ComposeConfigAdapter(command=cfg.prepare.compose_command, expected_version=cfg.prepare.compose_version)
    return tuple(inspect_task(cfg, task) for task in environments(Path(path).resolve(), compose_adapter=adapter))


def inspect_task(cfg: FlotillaConfig, task: HarborTask) -> EnvironmentInspection:
    scan = scan_task(task, cfg.platform_capabilities())
    return EnvironmentInspection(
        task.path.resolve(),
        task.env_dir.resolve(),
        task.name,
        scan.status,
        tuple({"path": f.path, "kind": f.kind, "reason": f.reason} for f in scan.findings),
        {
            name: cfg.resources.effective(task.name, name, dict(service.resources))
            for name, service in task.project.services.items()
        },
    )
