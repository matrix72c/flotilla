"""Build-time Harbor environment selection; no training or scoring logic."""

from __future__ import annotations

import tomllib
from collections.abc import Mapping
from pathlib import Path

from harbor.models.task.config import TaskConfig
from harbor.models.task.paths import TaskPaths
from harbor.models.task.verifier_mode import resolve_verifier_environment_definition

from flotilla.compose.paths import confined
from flotilla.compose.task import HarborTask, TaskError, load_environment


def task_environments(
    path: Path,
    *,
    compose_environment: Mapping[str, str] | None = None,
) -> list[HarborTask]:
    """Resolve main and separate verifier contexts using Harbor's own inheritance rules."""
    try:
        raw = tomllib.loads(confined(path, "task.toml").read_text())
        config = TaskConfig.model_validate(raw)
        definitions = [
            load_environment(
                path,
                TaskPaths(path).environment_dir,
                config.environment.model_dump(mode="json", exclude_unset=True),
                name=path.name,
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
                    compose_environment=compose_environment,
                )
            )
        return definitions
    except TaskError:
        raise
    except (ValueError, OSError, RuntimeError) as exc:
        raise TaskError(f"Harbor environment definition: {exc}") from exc
