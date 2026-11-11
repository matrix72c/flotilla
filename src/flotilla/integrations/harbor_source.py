"""Build-time Harbor environment selection; no training or scoring logic."""

from __future__ import annotations

import tomllib
from collections.abc import Mapping
from pathlib import Path

from flotilla.compose.adapter import ComposeConfigAdapter
from flotilla.compose.paths import confined
from flotilla.compose.task import HarborTask, TaskError, load_environment


def task_environments(
    path: Path,
    *,
    compose_adapter: ComposeConfigAdapter | None = None,
    compose_files: tuple[Path, ...] | None = None,
    compose_environment: Mapping[str, str] | None = None,
) -> list[HarborTask]:
    """Resolve main and separate verifier contexts using Harbor's own inheritance rules."""
    try:
        return _task_environments(
            path,
            compose_adapter=compose_adapter,
            compose_files=compose_files,
            compose_environment=compose_environment,
        )
    except TaskError:
        raise
    except (ValueError, OSError, RuntimeError) as exc:
        raise TaskError(f"Harbor environment definition: {exc}") from exc


def _task_environments(
    path: Path,
    *,
    compose_adapter: ComposeConfigAdapter | None,
    compose_files: tuple[Path, ...] | None,
    compose_environment: Mapping[str, str] | None,
) -> list[HarborTask]:
    raw = tomllib.loads(confined(path, "task.toml").read_text())
    from harbor.models.task.config import TaskConfig
    from harbor.models.task.paths import TaskPaths
    from harbor.models.task.verifier_mode import resolve_verifier_environment_definition

    config = TaskConfig.model_validate(raw)
    definitions = [
        load_environment(
            path,
            TaskPaths(path).environment_dir,
            config.environment.model_dump(mode="json", exclude_unset=True),
            name=path.name,
            compose_adapter=compose_adapter,
            compose_files=compose_files,
            compose_environment=compose_environment,
        )
    ]
    for step in config.steps or [None]:
        definition = resolve_verifier_environment_definition(config, TaskPaths(path), step)
        if definition is None:
            continue
        definitions.append(
            load_environment(
                path,
                definition.directory,
                definition.config.model_dump(mode="json", exclude_unset=True),
                name=path.name,
                compose_adapter=compose_adapter,
                compose_files=compose_files,
                compose_environment=compose_environment,
            )
        )
    return definitions
