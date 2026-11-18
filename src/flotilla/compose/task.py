"""Read a task directory: its `task.toml` and the Compose project under `environment/`."""

from __future__ import annotations

import os
import tomllib
from collections.abc import Iterable, Iterator, Mapping
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any

from flotilla.compose import parse
from flotilla.compose.model import MAIN, Finding, Project
from flotilla.compose.paths import confined
from flotilla.resources import cpu, memory

TASK_FILE = "task.toml"
ENV_DIR = "environment"
COMPOSE_FILE = "docker-compose.yaml"
PUBLIC = "public"


class TaskError(ValueError):
    """Unreadable task directory (missing files, TOML / YAML syntax errors)."""


@dataclass(frozen=True)
class HarborTask:
    name: str  # `[task].name`, defaults to the directory name
    path: Path
    env_dir: Path
    project: Project
    findings: tuple[Finding, ...]  # `task.toml`-level findings, alongside `project.findings`
    environment_config: Mapping[str, Any] | None = None


def find_tasks(roots: Iterable[Path]) -> Iterator[Path]:
    """Task directories under `roots`: dirs containing `task.toml`, not descended further; hidden dirs skipped."""
    for root in roots:
        if (root / TASK_FILE).is_file():
            yield root
            continue
        for current, dirs, files in os.walk(root):
            if TASK_FILE in files:
                dirs.clear()
                yield Path(current)
                continue
            dirs[:] = sorted(d for d in dirs if not d.startswith("."))


def load_task(
    path: Path,
    *,
    compose_files: tuple[Path, ...] | None = None,
    compose_environment: Mapping[str, str] | None = None,
) -> HarborTask:
    try:
        return _load_task(path, compose_files, compose_environment)
    except TaskError:
        raise
    except (ValueError, OSError, RuntimeError) as exc:
        raise TaskError(f"task input: {exc}") from exc


def _load_task(
    path: Path,
    compose_files: tuple[Path, ...] | None,
    compose_environment: Mapping[str, str] | None,
) -> HarborTask:
    try:
        config = tomllib.loads(confined(path, TASK_FILE).read_text())
    except (OSError, tomllib.TOMLDecodeError) as exc:
        raise TaskError(f"failed to read {TASK_FILE}: {exc}") from exc
    env_dir = confined(path, ENV_DIR)
    return load_environment(
        path,
        env_dir,
        _table(config, "environment"),
        name=_table(config, "task").get("name") or path.name,
        compose_files=compose_files,
        compose_environment=compose_environment,
    )


def load_environment(
    task_dir: Path,
    env_dir: Path,
    environment: Mapping[str, Any],
    *,
    name: str,
    findings: tuple[Finding, ...] = (),
    compose_files: tuple[Path, ...] | None = None,
    compose_environment: Mapping[str, str] | None = None,
) -> HarborTask:
    """Parse a concrete environment context without depending on Harbor's runtime."""
    if not env_dir.resolve().is_relative_to(task_dir.resolve()):
        raise TaskError("environment context escapes task directory")
    if not isinstance(name, str) or not name:
        raise TaskError("task.name must be a nonempty string")
    findings = (*findings, *_network_policies({"environment": environment}))
    compose_path = confined(env_dir, COMPOSE_FILE)
    image = environment.get("docker_image")
    main_image = image if isinstance(image, str) and image else None
    main_build = main_image is None and (env_dir / "Dockerfile").is_file()
    project = parse.load_project(
        env_dir,
        files=compose_files if compose_files is not None else ((compose_path,) if compose_path.is_file() else ()),
        environment=compose_environment or {},
        main_image=main_image,
        main_build=main_build,
        workdir=environment.get("workdir"),
    )
    if _network_mode(environment) == "no-network":
        # Preserve peer topology while removing external access from every service. This survives
        # compilation/publication and cannot be undone by a caller's deployment egress policy.
        project = replace(
            project, networks={name: replace(net, internal=True) for name, net in project.networks.items()}
        )
    if MAIN in project.services:
        declared = dict(project.services[MAIN].resources)
        try:
            if "cpus" in environment:
                declared["cpu"] = cpu(environment["cpus"])
            if "memory" in environment and "memory_mb" in environment:
                raise ValueError("use only one of memory or memory_mb")
            if "memory" in environment:
                declared["memory"] = memory(environment["memory"])
            if "memory_mb" in environment:
                declared["memory"] = memory(str(environment["memory_mb"]) + "Mi")
        except ValueError as exc:
            raise TaskError(f"task.toml environment resources: {exc}") from exc
        services = dict(project.services)
        services[MAIN] = replace(services[MAIN], resources=declared)
        project = replace(project, services=services)
    return HarborTask(
        name=name,
        path=task_dir,
        env_dir=env_dir,
        project=project,
        findings=findings,
        environment_config=environment,
    )


def _network_policies(config: Mapping[str, Any]) -> Iterator[Finding]:
    mode = _network_mode(_table(config, "environment"))
    if mode is not None and mode not in (PUBLIC, "no-network"):
        yield Finding(
            "task.toml:environment.network_mode",
            "reject",
            f"environment network mode {mode!r} is unsupported by core task loading",
        )


def _network_mode(table: Mapping[str, Any]) -> str | None:
    """network_mode set explicitly or via `allow_internet`; None when unset (the baseline is public)."""
    mode = table.get("network_mode")
    if isinstance(mode, str):
        return mode
    if "allowed_hosts" not in table and isinstance(table.get("allow_internet"), bool):
        return PUBLIC if table["allow_internet"] else "no-network"
    return None


def _table(config: Mapping[str, Any], key: str) -> Mapping[str, Any]:
    value = config.get(key)
    if value is None:
        return {}
    if not isinstance(value, Mapping):
        raise TaskError(f"{key}: expected a table")
    return value
