"""Check a dataset without creating anything: plan every task and report what fails.

Planning is the same code `open` runs, so a task that plans here is one that will open there. It
reads the task directory and each image's config from the registry, and touches no sandbox. That
makes it the cheap gate: a dataset of 60k tasks with 1% malformed is 600 runs that fail partway
through, and finding them costs minutes here instead of GPU hours there.

It also warms the image-config cache, because planning is what fills it.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from flotilla.compose.plan_task import ImageConfigSource, PlanError, plan_task
from flotilla.compose.task import TaskError, find_tasks, load_task
from flotilla.config import FlotillaConfig
from flotilla.core.plan import TaskSource
from flotilla.native.dataset import Dataset, DatasetError
from flotilla.registry import RegistryError
from flotilla.scan import scan_task


@dataclass(frozen=True)
class TaskReport:
    """One task's outcome. `planned` is the whole claim: it would open as described."""

    task: str
    planned: bool
    services: tuple[str, ...] = ()
    #: Every field the dialect refused, as `path: reason`, and every warning it kept.
    rejected: tuple[str, ...] = ()
    warnings: tuple[str, ...] = ()
    #: Why planning failed, when it did. A rejected field is reported above instead.
    error: str = ""

    def to_json(self) -> dict[str, object]:
        data: dict[str, object] = {"task": self.task, "planned": self.planned}
        if self.services:
            data["services"] = list(self.services)
        for name in ("rejected", "warnings"):
            if value := getattr(self, name):
                data[name] = list(value)
        if self.error:
            data["error"] = self.error
        return data


def inspect_source(cfg: FlotillaConfig, source: TaskSource, configs: ImageConfigSource) -> TaskReport:
    """Plan one task against this deployment. Never raises: a bad task is a report, not an exception."""
    key = source.key or str(source.local)
    try:
        task = load_task(source.local, compose_environment=cfg.runtime_params)
    except (TaskError, ValueError, OSError) as exc:
        return TaskReport(task=key, planned=False, error=f"{type(exc).__name__}: {exc}")

    scan = scan_task(task, cfg.platform_capabilities())
    rejected = tuple(f"{f.path}: {f.reason}" for f in scan.findings if f.kind == "reject")
    warnings = tuple(f"{f.path}: {f.reason}" for f in scan.findings if f.kind != "reject")
    if rejected:
        return TaskReport(task=key, planned=False, services=scan.services, rejected=rejected, warnings=warnings)

    # Only now does anything leave the machine: one registry read per image not already cached.
    try:
        plan = plan_task(
            task,
            source,
            configs=configs,
            resources=cfg.resources.for_services(task.name, {n: s.resources for n, s in task.project.services.items()}),
        )
    except (PlanError, RegistryError, ValueError, OSError) as exc:
        return TaskReport(
            task=key, planned=False, services=scan.services, warnings=warnings, error=f"{type(exc).__name__}: {exc}"
        )
    return TaskReport(task=key, planned=True, services=tuple(sorted(plan.units)), warnings=warnings)


def sources_of(cfg: FlotillaConfig, targets: list[str]) -> list[TaskSource]:
    """What to inspect: a declared dataset by name, or task directories under local paths.

    A dataset name is the form that matters, because that is what a run is given and it carries the
    platform key a plan has to be checked against. A path is for a dataset not declared yet; those
    tasks are planned with no platform key, so a plan's storage rules are not exercised.
    """
    if not targets:
        declared = cfg.resolve_datasets()
        if not declared:
            raise DatasetError("the deployment declares no datasets; name a task directory instead")
        return [source for dataset in declared.values() for source in _dataset_sources(dataset)]
    sources: list[TaskSource] = []
    for target in targets:
        if dataset := cfg.datasets.get(target):
            resolved = Dataset.from_platform_root(
                dataset.local_root,
                dataset.platform_root,
                storage=cfg.storage_settings_for(dataset.input or "work"),
                root=dataset.input or "work",
            )
            sources.extend(_dataset_sources(resolved))
            continue
        path = Path(target).expanduser().absolute()
        if not path.is_dir():
            raise DatasetError(f"{target} is neither a declared dataset nor a directory")
        found = sorted(find_tasks([path]))
        if not found:
            raise DatasetError(f"no task directory below {path}")
        # No platform key: a dataset not declared yet has none, so a plan's storage rules go unchecked.
        sources.extend(TaskSource(local=p) for p in found)
    return sources


def _dataset_sources(dataset: Dataset) -> list[TaskSource]:
    return [dataset.source(key) for key in dataset.tasks()]


__all__ = ["TaskReport", "inspect_source", "sources_of"]
