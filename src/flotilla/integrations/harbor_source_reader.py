"""Read native Harbor metadata without inspecting environment or verifier payloads.

Only task.toml and instruction.md are opened. This reader has no platform client,
filesystem cache, or persisted identity; callers own discovery and publication.
"""

from __future__ import annotations

from collections.abc import Iterable
from pathlib import Path
from typing import Any

from harbor.models.task.config import TaskConfig
from harbor.models.task.task import strip_canary
from pydantic import BaseModel, ConfigDict, field_validator

from flotilla.runtime.workspace import source_task_key


class SourceMapping(BaseModel):
    """A local dataset directory and its component-delimited platform key prefix."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    local_root: Path
    platform_prefix: str

    @field_validator("platform_prefix")
    @classmethod
    def _validate_prefix(cls, value: str) -> str:
        return source_task_key(value)


class NativeTaskSource:
    """Resolve unambiguous native task sources and read their metadata on demand."""

    def __init__(self, sources: Iterable[SourceMapping | dict[str, Any]]) -> None:
        mappings: list[SourceMapping] = []
        for source in sources:
            mapping = SourceMapping.model_validate(source)
            root = mapping.local_root.expanduser().resolve(strict=True)
            if not root.is_dir():
                raise NotADirectoryError(root)
            mapping = mapping.model_copy(update={"local_root": root})
            for previous in mappings:
                if (
                    _under_prefix(mapping.platform_prefix, previous.platform_prefix)
                    or _under_prefix(previous.platform_prefix, mapping.platform_prefix)
                    or root.is_relative_to(previous.local_root)
                    or previous.local_root.is_relative_to(root)
                ):
                    raise ValueError(f"overlapping source mappings: {previous} and {mapping}")
            mappings.append(mapping)
        self.sources = tuple(mappings)

    def _mapping(self, key: str) -> SourceMapping:
        source_task_key(key)
        matches = [source for source in self.sources if _under_prefix(key, source.platform_prefix)]
        if len(matches) != 1:
            raise ValueError(f"task key must match exactly one source mapping: {key!r}")
        return matches[0]

    def resolve_task(self, key: str) -> Path:
        """Resolve an existing task directory, never following a source escape."""
        mapping = self._mapping(key)
        suffix = key[len(mapping.platform_prefix) :].lstrip("/")
        path = (mapping.local_root / suffix).resolve(strict=True)
        if not path.is_relative_to(mapping.local_root):
            raise ValueError(f"task path escapes source root: {key!r}")
        if not path.is_dir():
            raise NotADirectoryError(path)
        return path

    def _input_file(self, key: str, task: Path, name: str) -> Path:
        path = (task / name).resolve(strict=True)
        if not path.is_relative_to(task) or not path.is_relative_to(self._mapping(key).local_root):
            raise ValueError(f"{name} escapes task or source root: {key!r}")
        if not path.is_file():
            raise ValueError(f"{name} is not a regular file: {key!r}")
        return path

    def read_metadata(self, key: str) -> dict[str, Any]:
        """Read only config and canary-stripped instruction for one task."""
        task = self.resolve_task(key)
        config = TaskConfig.model_validate_toml(self._input_file(key, task, "task.toml").read_text(encoding="utf-8"))
        instruction = self._input_file(key, task, "instruction.md").read_text(encoding="utf-8")
        return {
            "task_id": task.name,
            "instruction": strip_canary(instruction),
            "task_config": config.model_dump(mode="json"),
        }


def _under_prefix(key: str, prefix: str) -> bool:
    return key == prefix or key.startswith(prefix + "/")
