"""Check a dataset without creating anything: plan every task and report what fails.

Planning is the same code `open` runs -- every environment definition the task declares, the
deployment's agent overlay, and the same admission list the manager enforces -- so a task that
plans here is one that will open there. It reads the task directory and each image's config
from the registry, and touches no sandbox. That makes it the cheap gate: a dataset of 60k tasks
with 1% malformed is 600 runs that fail partway through, and finding them costs minutes here
instead of GPU hours.

It also warms the image-config cache, because planning is what fills it.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from flotilla.compose.harbor_source import task_environments
from flotilla.compose.plan_task import ImageConfigSource, PlanError
from flotilla.compose.task import TaskError, find_tasks
from flotilla.config import FlotillaConfig
from flotilla.core.admission import admission_errors
from flotilla.models.plan import TaskSource
from flotilla.native.dataset import Dataset, DatasetError
from flotilla.registry import RegistryError
from flotilla.runtime.client import plan_for
from flotilla.scan import scan


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
    caps = cfg.platform_capabilities()
    try:
        definitions = task_environments(source.local, compose_environment=cfg.runtime_params)
    except (TaskError, ValueError, OSError) as exc:
        return TaskReport(task=key, planned=False, error=f"{type(exc).__name__}: {exc}")

    rejected: list[str] = []
    warnings: list[str] = []
    services: tuple[str, ...] = ()
    for definition in definitions:
        env_name = Path(definition.env_dir).name
        # Findings and errors name their role when the task has more than the main environment.
        tag = "" if env_name == "environment" else f"{env_name}: "
        findings = scan(definition.project, caps, env_dir=str(definition.env_dir), extra=definition.findings)
        rejected.extend(f"{tag}{f.path}: {f.reason}" for f in findings if f.kind == "reject")
        warnings.extend(f"{tag}{f.path}: {f.reason}" for f in findings if f.kind != "reject")
        if env_name == "environment":
            services = tuple(sorted(definition.project.services))
        if rejected:
            continue  # a task already known not to open costs no registry read
        # Only now does anything leave the machine: one registry read per image not already cached.
        try:
            plan = plan_for(cfg, definition, source, configs)
        except (PlanError, RegistryError, ValueError, OSError) as exc:
            return TaskReport(
                task=key,
                planned=False,
                services=services,
                rejected=tuple(rejected),
                warnings=tuple(warnings),
                error=f"{tag}{type(exc).__name__}: {exc}",
            )
        rejected.extend(f"{tag}{error}" for error in admission_errors(plan, caps))
    report = TaskReport(task=key, planned=not rejected, services=services, rejected=tuple(rejected))
    return report if rejected else TaskReport(task=key, planned=True, services=services, warnings=tuple(warnings))


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
