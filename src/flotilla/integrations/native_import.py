"""Host-only Harbor importer. Neither discovery nor compilation opens a sandbox."""

from __future__ import annotations

import os
import tomllib
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from pydantic import BaseModel, ConfigDict, Field

from flotilla.compose.adapter import ComposeConfigAdapter
from flotilla.compose.paths import confined
from flotilla.compose.task import HarborTask
from flotilla.integrations.harbor_source import task_environments
from flotilla.platform.base import Capabilities
from flotilla.scan import scan_task


class SelectedTask(BaseModel):
    """Importer input, deliberately distinct from an executable native dataset row."""

    model_config = ConfigDict(extra="forbid", frozen=True)
    source: str = "harbor"
    task_path: Path
    task_key: str
    annotations: dict[str, Any] = Field(default_factory=dict)
    parameters: dict[str, str] = Field(default_factory=dict)


@dataclass(frozen=True)
class ImportedTask:
    selection: SelectedTask
    roles: Mapping[str, HarborTask]
    instruction: str
    config: Any
    tests: Path | None
    warnings: tuple[str, ...] = ()


class UnsupportedTask(ValueError):
    """A task has semantics the current native importer cannot faithfully encode."""


def discover(root: Path) -> list[Path]:
    """Deterministic discovery with fatal traversal errors, not os.walk's silent omissions."""
    root = root.expanduser().resolve(strict=True)
    if not root.is_dir():
        raise NotADirectoryError(root)
    result = []

    def failed(error: OSError) -> None:
        raise error

    for current, dirs, files in os.walk(root, onerror=failed, followlinks=False):
        directory = Path(current)
        if "task.toml" in files:
            result.append(directory)
            dirs.clear()
        else:
            for name in dirs:
                if not name.startswith(".") and (directory / name).is_symlink():
                    raise ValueError(f"discovery cannot silently skip symlink directory: {directory / name}")
            dirs[:] = sorted(name for name in dirs if not name.startswith("."))
    return result


def import_task(
    row: SelectedTask,
    *,
    caps: Capabilities,
    compose_adapter: ComposeConfigAdapter,
    require_prebuilt_images: bool = False,
) -> ImportedTask:
    """Resolve every role through Harbor's inheritance and the official Compose adapter."""
    from harbor.models.task.config import TaskConfig
    from harbor.models.task.task import strip_canary

    if row.source != "harbor":
        raise ValueError(f"unknown selected importer: {row.source!r}")
    path = row.task_path.expanduser().resolve(strict=True)
    config = TaskConfig.model_validate(tomllib.loads(confined(path, "task.toml").read_text()))
    if config.steps:
        raise UnsupportedTask(
            "multi-step Harbor tasks are outside the consume-only native protocol: one agent run, one verification"
        )
    if config.artifacts:
        raise UnsupportedTask("native importer does not yet encode arbitrary Harbor artifact collection")
    if config.verifier.collect:
        raise UnsupportedTask("native importer does not yet encode verifier.collect")
    if config.verifier.user or config.agent.user:
        raise UnsupportedTask("native importer does not yet encode agent/verifier identity overrides")
    if (
        config.agent.network_mode
        or config.verifier.network_mode
        or config.agent.allowed_hosts
        or config.verifier.allowed_hosts
    ):
        raise UnsupportedTask("native importer does not yet encode per-phase network policy")
    environments = task_environments(path, compose_adapter=compose_adapter, compose_environment=row.parameters)
    roles = {}
    warnings: list[str] = []
    for index, environment in enumerate(environments):
        role = "main" if index == 0 else "verifier"
        if role in roles:
            raise UnsupportedTask("multiple verifier environments require a multi-step protocol")
        declared = environment.environment_config or {}
        for key in ("gpus", "gpu_types", "tpu", "mcp_servers", "skills_dir", "healthcheck", "env"):
            if declared.get(key):
                raise UnsupportedTask(f"{role}: environment.{key} is not represented by the native importer")
        if declared.get("storage_mb"):
            warnings.append(f"{role}: environment.storage_mb ignored; the platform has no per-sandbox disk quota")
        if declared.get("os", "linux") != "linux":
            raise UnsupportedTask(f"{role}: only linux images are supported")
        if require_prebuilt_images:
            pending = [name for name, svc in environment.project.services.items() if svc.build is not None]
            if pending:
                raise UnsupportedTask(f"{role}: application images must be published first: {', '.join(pending)}")
        scan = scan_task(environment, caps)
        if scan.status == "rejected":
            raise UnsupportedTask(f"{role}: " + "; ".join(f.reason for f in scan.findings if f.kind == "reject"))
        warnings.extend(f"{role}: {f.path}: {f.reason}" for f in scan.findings if f.kind == "warn")
        if scan.runtime_params:
            raise UnsupportedTask(f"{role}: unresolved prepare parameters: {', '.join(scan.runtime_params)}")
        roles[role] = environment
    instruction = strip_canary(confined(path, "instruction.md").read_text(encoding="utf-8"))
    tests = confined(path, "tests")
    if tests.exists() and not tests.is_dir():
        raise ValueError("tests must be a directory")
    return ImportedTask(row, roles, instruction, config, tests if tests.exists() else None, tuple(warnings))
