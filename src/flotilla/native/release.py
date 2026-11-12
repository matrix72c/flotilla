"""Native releases: a plain directory tree of tasks plus a dataset.jsonl selection."""

from __future__ import annotations

import os
import shutil
import tempfile
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Any

from pydantic import field_validator

from .files import confined
from .models import DatasetRow, Model, Task, canonical, relative_path

TASKS = "tasks"


class ReleaseWriter:
    """Write `tasks/<key>/` directories into a staging sibling, then rename it into place."""

    def __init__(self, output: str | Path, name: str) -> None:
        self.output = Path(output).absolute()
        if self.output.exists() or self.output.is_symlink():
            raise FileExistsError(self.output)
        if not name or "/" in name:
            raise ValueError("release name must be one nonempty segment")
        self.name = name
        self.output.parent.mkdir(parents=True, exist_ok=True)
        self.staging = Path(tempfile.mkdtemp(prefix=f".{self.output.name}-", dir=self.output.parent))
        self.tasks: set[str] = set()
        self._published = False

    def __enter__(self) -> ReleaseWriter:
        return self

    def __exit__(self, *exc: object) -> None:
        if not self._published:
            shutil.rmtree(self.staging)

    @staticmethod
    def directory(key: str) -> str:
        """Release-relative directory of task `key`; task.json references files below it."""
        return f"{TASKS}/{relative_path(key)}"

    def add_task(self, task: Task, instruction: str, files: Mapping[str, Path] | None = None) -> str:
        """Write task.json, instruction.md and each `files` entry (copied, modes kept) under the task directory."""
        if task.key in self.tasks:
            raise ValueError(f"duplicate task key: {task.key}")
        directory = self.staging / self.directory(task.key)
        directory.mkdir(parents=True)
        for name, source in (files or {}).items():
            target = directory / relative_path(name)
            target.parent.mkdir(parents=True, exist_ok=True)
            if source.is_dir():
                shutil.copytree(source, target, symlinks=True)
            else:
                shutil.copy2(source, target)
        (directory / "task.json").write_bytes(canonical(task))
        (directory / task.instruction).write_text(instruction, encoding="utf-8")
        self.tasks.add(task.key)
        return task.key

    def publish(self, rows: Sequence[DatasetRow | Mapping[str, Any]]) -> list[DatasetRow]:
        if self._published:
            raise RuntimeError("writer already published")
        normalized: list[DatasetRow] = []
        for row in rows:
            data = row.model_dump(mode="json") if isinstance(row, DatasetRow) else dict(row)
            entry = DatasetRow.model_validate({**data, "release": self.name})
            if entry.task not in self.tasks:
                raise ValueError(f"dataset row names a task not in this release: {entry.task}")
            normalized.append(entry)
        (self.staging / "dataset.jsonl").write_text("".join(canonical(row).decode() + "\n" for row in normalized))
        self.staging.chmod(0o755)  # mkdtemp creates 0700
        os.rename(self.staging, self.output)
        self._published = True
        return normalized


class ReleaseReader:
    """Read task.json and instruction.md of one release directory; parsed tasks are cached."""

    def __init__(self, root: str | Path) -> None:
        self.root = Path(root).absolute()
        self._tasks: dict[str, Task] = {}

    def directory(self, task: str) -> Path:
        return confined(self.root, ReleaseWriter.directory(task))

    def read_task(self, task: str) -> Task:
        if task not in self._tasks:
            self._tasks[task] = Task.model_validate_json((self.directory(task) / "task.json").read_bytes())
        return self._tasks[task].model_copy(deep=True)

    def instruction(self, task: str) -> str:
        return (self.directory(task) / self.read_task(task).instruction).read_text(encoding="utf-8")

    def read_dataset(self, path: str | Path | None = None) -> list[DatasetRow]:
        return read_dataset(path or self.root / "dataset.jsonl")


def read_dataset(path: str | Path) -> list[DatasetRow]:
    rows: list[DatasetRow] = []
    with Path(path).open(encoding="utf-8") as handle:
        for number, line in enumerate(handle, 1):
            if not line.strip():
                continue
            try:
                rows.append(DatasetRow.model_validate_json(line))
            except ValueError as exc:
                raise ValueError(f"{path}:{number}: invalid native row: {exc}") from exc
    return rows


class ReleaseBinding(Model):
    local_root: Path
    platform_root: str
    root: str = "work"  # storage root holding the platform copy: "work" or a named input root

    @field_validator("platform_root")
    @classmethod
    def nonempty(cls, value: str) -> str:
        if not value:
            raise ValueError("platform_root is required")
        return value


@dataclass(frozen=True)
class BoundTask:
    row: DatasetRow
    task: Task
    reader: ReleaseReader
    platform_key: str
    root: str = "work"

    @property
    def directory(self) -> Path:
        """Local task directory (task.json, instruction.md)."""
        return self.reader.directory(self.row.task)

    def instruction(self) -> str:
        return self.reader.instruction(self.row.task)


class ReleaseBindings:
    """Deployment `[task_releases.<name>]` bindings; a row's `release` selects one by name."""

    def __init__(
        self,
        bindings: Mapping[str, ReleaseBinding | Mapping[str, Any]],
        *,
        storage: Any,
        inputs: Mapping[str, Any] | None = None,
    ) -> None:
        self._readers: dict[str, tuple[ReleaseReader, str, str]] = {}
        inputs = inputs or {}
        for name, value in bindings.items():
            binding = value if isinstance(value, ReleaseBinding) else ReleaseBinding.model_validate(value)
            if binding.root != "work" and binding.root not in inputs:
                raise ValueError(f"release bound to unconfigured storage root {binding.root!r}")
            key = platform_key(binding.platform_root, storage if binding.root == "work" else inputs[binding.root])
            self._readers[name] = ReleaseReader(binding.local_root), key, binding.root

    def read_dataset(self, path: str | Path) -> list[DatasetRow]:
        rows = read_dataset(path)
        unbound = sorted({row.release for row in rows} - set(self._readers))
        if unbound:
            raise ValueError(f"dataset references releases without a [task_releases] binding: {unbound}")
        return rows

    def resolve(self, row: DatasetRow | Mapping[str, Any]) -> BoundTask:
        row = row if isinstance(row, DatasetRow) else DatasetRow.model_validate(row)
        if row.release not in self._readers:
            raise ValueError(f"release {row.release!r} has no [task_releases] binding")
        reader, key, root = self._readers[row.release]
        return BoundTask(row, reader.read_task(row.task), reader, key, root)


def platform_key(value: str, storage: Any) -> str:
    """Map only an explicit host root or PVC identity; never guess a mount prefix."""
    if storage.volumes == "host":
        if value.startswith("pvc:"):
            raise ValueError("host storage requires an explicit host path, not a PVC identity")
        if not value.startswith("/"):
            key = value
        elif not storage.host_path.startswith("/"):
            raise ValueError(
                "storage host path is a platform identifier, not a filesystem path; use a relative platform root"
            )
        else:
            root, path = PurePosixPath(storage.host_path, storage.root_subpath), PurePosixPath(value)
            if str(path) != value or ".." in path.parts:
                raise ValueError("noncanonical platform root")
            try:
                key = str(path.relative_to(root))
            except ValueError as exc:
                raise ValueError("platform root is outside configured host storage") from exc
    elif value.startswith("pvc:"):
        prefix = f"pvc:{storage.claim_name}/" + (storage.root_subpath + "/" if storage.root_subpath else "")
        if not value.startswith(prefix):
            raise ValueError("platform root is outside configured PVC storage")
        key = value[len(prefix) :]
    else:
        raise ValueError("PVC storage requires an explicit pvc:<claim>/<subpath>/... platform root")
    key = relative_path(key)
    if key.split("/")[0] in {"launches", "share"}:
        raise ValueError("release cannot overlap runtime or shared-tools storage")
    return key
