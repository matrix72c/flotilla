"""Immutable values crossing the caller/background-loop boundary."""

from __future__ import annotations

import hashlib
import math
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from flotilla.core.reaper import CleanupRecord
from flotilla.manifest import Manifest


@dataclass(frozen=True)
class Task:
    """Private execution plan for one open operation."""

    _manifest: Manifest = field(repr=False)
    digest: str = field(init=False)

    def __post_init__(self) -> None:
        manifest = self._manifest.model_copy(deep=True)
        object.__setattr__(self, "_manifest", manifest)
        object.__setattr__(self, "digest", hashlib.sha256(manifest.dump_json().encode()).hexdigest())
        if manifest.agent_service not in manifest.units:
            raise ValueError("manifest agent_service does not exist")
        if any(f.kind == "reject" for f in manifest.findings):
            raise ValueError("manifest contains rejected findings")

    def manifest(self) -> Manifest:
        return self._manifest.model_copy(deep=True)


@dataclass(frozen=True)
class RunRequest:
    task: Task
    request_id: str
    runtime_params: dict[str, str] = field(default_factory=dict)
    exec_env: dict[str, dict[str, str]] = field(default_factory=dict, repr=False)
    disable_internet: bool = False
    resources: dict[str, dict[str, str]] = field(default_factory=dict)
    lifetime: float | None = None

    def __post_init__(self) -> None:
        if not self.request_id:
            raise ValueError("request_id must be nonempty")


@dataclass(frozen=True)
class RuntimeEvent:
    """Small diagnostic record: no argv, output, environment values or credentials."""

    kind: str
    environment_id: str
    request_id: str
    task_digest: str
    elapsed_s: float = 0
    service: str | None = None
    category: str | None = None
    attempt_id: str | None = None
    command_id: str | None = None


@dataclass(frozen=True)
class ServiceInfo:
    name: str
    instance_id: str
    image: str
    cwd: str
    cpu: str
    memory: str


@dataclass(frozen=True)
class EnvironmentInfo:
    environment_id: str
    request_id: str
    task_digest: str
    agent_service: str
    services: tuple[ServiceInfo, ...]
    timings: tuple[tuple[str, float], ...]
    state: str
    security_gaps: tuple[str, ...] = ()
    accepted_security_gaps: tuple[str, ...] = ()


@dataclass(frozen=True)
class PreparationResidual:
    kind: str
    identifier: str
    reason: str


@dataclass(frozen=True)
class CleanupReport:
    launch_id: str
    cleaned: int
    remaining: tuple[CleanupRecord, ...] = ()
    launch_directories: tuple[str, ...] = ()
    anchor_instances: tuple[str, ...] = ()
    uncertain: bool = False
    preparation_residuals: tuple[PreparationResidual, ...] = ()

    @property
    def clean(self) -> bool:
        return not (
            self.remaining
            or self.launch_directories
            or self.anchor_instances
            or self.uncertain
            or self.preparation_residuals
        )


class CleanupError(RuntimeError):
    def __init__(self, report: CleanupReport) -> None:
        self.report = report
        super().__init__(f"launch {report.launch_id}: cleanup incomplete; inspect .report")


@dataclass(frozen=True)
class EnvironmentDefinition:
    """An effective Harbor environment, selected by its host framework."""

    task_root: Path
    directory: Path
    name: str
    config: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class OpenOptions:
    request_id: str | None = None
    startup_timeout: float | None = None
    lifetime: float | None = None
    runtime_params: dict[str, str] = field(default_factory=dict)
    exec_env: dict[str, dict[str, str]] = field(default_factory=dict, repr=False)
    resources: dict[str, dict[str, str]] = field(default_factory=dict)
    disable_internet: bool = False
    force_build: bool = False

    def __post_init__(self) -> None:
        for value in (self.startup_timeout, self.lifetime):
            if value is not None and (not math.isfinite(value) or value <= 0):
                raise ValueError("timeouts must be finite and positive")
        if self.request_id == "":
            raise ValueError("request_id must be nonempty")


@dataclass(frozen=True)
class EnvironmentInspection:
    definition: EnvironmentDefinition
    status: str
    findings: tuple[dict[str, str], ...]
    resources: dict[str, dict[str, str]]
