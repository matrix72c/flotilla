"""Atomic portable releases and launch-local selected-content validation."""

from __future__ import annotations

import hashlib
import os
import shutil
import tempfile
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Any

from pydantic import field_validator

from .files import INSTRUCTION_LIMIT, confined, inventory, read_bytes, validate_inventory, validate_seed
from .models import (
    AssetRecord,
    DatasetRow,
    Model,
    ReleaseManifest,
    Task,
    TaskRecord,
    canonical,
    digest,
    relative_path,
)


class ReleaseWriter:
    """Publish only after every selected task and asset validates; no upload side effects."""

    def __init__(self, output: str | Path) -> None:
        self.output = Path(output).absolute()
        if self.output.exists() or self.output.is_symlink():
            raise FileExistsError(self.output)
        self.output.parent.mkdir(parents=True, exist_ok=True)
        self.staging = Path(tempfile.mkdtemp(prefix=f".{self.output.name}-", dir=self.output.parent))
        self.assets: dict[str, AssetRecord] = {}
        self.tasks: dict[str, TaskRecord] = {}
        self._published = False

    def __enter__(self) -> ReleaseWriter:
        return self

    def __exit__(self, *exc: object) -> None:
        if not self._published:
            shutil.rmtree(self.staging)

    def add_asset(self, source: str | Path, identities: Mapping[str, tuple[int, int, int]] | None = None) -> str:
        source = Path(source)
        entries = inventory(source, identities)
        identity = digest(entries_to_json(entries))
        key = f"assets/{identity}"
        if key in self.assets:
            return key
        target = self.staging / key
        target.parent.mkdir(parents=True, exist_ok=True)
        if source.is_symlink():
            raise ValueError("asset root cannot be a symlink")
        if source.is_dir():
            shutil.copytree(source, target, symlinks=True)
        else:
            shutil.copy2(source, target)
        # Host ownership is not portable. Desired numeric IDs remain in the inventory;
        # publication must preserve/set them, and the platform checks them before use.
        for relative, entry in entries.items():
            path = target if relative == "." else target / relative
            if entry.kind != "symlink":
                path.chmod(entry.mode)
        validate_inventory(target, entries)
        self.assets[key] = AssetRecord(digest=identity, entries=entries)
        return key

    def add_task(self, task: Task, instruction: str) -> TaskRecord:
        task = Task.model_validate_json(task.model_dump_json())
        payload = instruction.encode("utf-8")
        if len(payload) > INSTRUCTION_LIMIT:
            raise ValueError("instruction exceeds byte limit")
        if set(task.assets) - set(self.assets):
            raise ValueError("task refers to assets not added to writer")
        instruction_digest = hashlib.sha256(payload).hexdigest()
        task_digest = digest({"task": task.model_dump(mode="json", by_alias=True), "instruction": instruction_digest})
        ref = f"tasks/{task_digest}/task.json"
        directory = self.staging / ref
        directory.parent.mkdir(parents=True, exist_ok=True)
        directory.write_bytes(canonical(task))
        directory.chmod(0o644)
        (directory.parent / task.instruction).write_bytes(payload)
        (directory.parent / task.instruction).chmod(0o644)
        record = TaskRecord(
            task_ref=ref, task_digest=task_digest, instruction_digest=instruction_digest, assets=task.assets
        )
        self.tasks[ref] = record
        return record

    def publish(self, rows: Sequence[DatasetRow | Mapping[str, Any]]) -> ReleaseManifest:
        if self._published:
            raise RuntimeError("writer already published")
        if not rows:
            raise ValueError("cannot publish an empty selection")
        manifest = ReleaseManifest(release_id="", tasks=self.tasks, assets=self.assets)
        manifest = manifest.model_copy(update={"release_id": manifest.identity()})
        normalized: list[DatasetRow] = []
        for row in rows:
            data = row.model_dump(mode="json") if isinstance(row, DatasetRow) else dict(row)
            data["release_id"] = data.get("release_id") or manifest.release_id
            entry = DatasetRow.model_validate(data)
            if (
                entry.release_id != manifest.release_id
                or entry.task_ref not in self.tasks
                or self.tasks[entry.task_ref].task_digest != entry.task_digest
            ):
                raise ValueError("dataset row does not reference this release")
            normalized.append(entry)
        (self.staging / "manifest.json").write_bytes(canonical(manifest))
        (self.staging / "manifest.json").chmod(0o644)
        dataset = self.staging / "dataset.jsonl"
        dataset.write_text("".join(canonical(row).decode() + "\n" for row in normalized))
        dataset.chmod(0o644)
        reader = ReleaseReader(self.staging)
        for row in normalized:
            reader.read_task(row)
        if self.output.exists() or self.output.is_symlink():
            raise FileExistsError(self.output)
        # rename of a complete staging directory is the sole publication point.
        os.rename(self.staging, self.output)
        self._published = True
        return manifest


def entries_to_json(entries: Mapping[str, Any]) -> dict[str, Any]:
    return {key: value.model_dump(mode="json") for key, value in entries.items()}


class ReleaseReader:
    """Immutable release binding. Validation caches live only for this reader/launch."""

    def __init__(self, root: str | Path, *, ownership: bool = False) -> None:
        self.root = Path(root).absolute()
        if self.root.is_symlink() or not self.root.is_dir():
            raise ValueError("release root must be a real directory")
        self.ownership = ownership
        self.manifest = ReleaseManifest.model_validate_json(read_bytes(self.root, "manifest.json"))
        if self.manifest.identity() != self.manifest.release_id:
            raise ValueError("release identity mismatch")
        for ref, record in self.manifest.tasks.items():
            relative_path(ref)
            if ref != record.task_ref or ref != f"tasks/{record.task_digest}/task.json":
                raise ValueError("invalid task inventory path")
            if set(record.assets) - set(self.manifest.assets):
                raise ValueError("task inventory references missing assets")
        for key, asset in self.manifest.assets.items():
            if key != f"assets/{asset.digest}" or digest(entries_to_json(asset.entries)) != asset.digest:
                raise ValueError("asset inventory identity mismatch")
            for entry in asset.entries:
                if entry != ".":
                    relative_path(entry)
        self._tasks: dict[str, Task] = {}
        self._assets: set[str] = set()

    def read_task(self, row: DatasetRow) -> Task:
        if row.release_id != self.manifest.release_id:
            raise ValueError("dataset release identity does not match binding")
        record = self.manifest.tasks.get(row.task_ref)
        if record is None or record.task_digest != row.task_digest:
            raise ValueError("dataset task identity does not match release")
        if row.task_ref not in self._tasks:
            task = Task.model_validate_json(read_bytes(self.root, row.task_ref))
            instruction = read_bytes(
                self.root, str(PurePosixPath(row.task_ref).parent / task.instruction), INSTRUCTION_LIMIT
            )
            instruction_digest = hashlib.sha256(instruction).hexdigest()
            if (
                instruction_digest != record.instruction_digest
                or digest({"task": task.model_dump(mode="json", by_alias=True), "instruction": instruction_digest})
                != record.task_digest
            ):
                raise ValueError("task content identity mismatch")
            if task.assets != record.assets:
                raise ValueError("task assets differ from manifest")
            for asset in task.assets:
                if asset not in self._assets:
                    validate_inventory(
                        confined(self.root, asset), self.manifest.assets[asset].entries, ownership=self.ownership
                    )
                    self._assets.add(asset)
            for role in task.roles.values():
                for unit in role.units.values():
                    for mount in unit.mounts:
                        if mount.scope == "task":
                            self._reference(mount.key)
                for volume in role.trial_volumes:
                    if volume.seed:
                        validate_seed(self._reference(volume.seed))
                    if volume.copy_from:
                        self._reference(volume.copy_from)
            if task.verification and task.verification.tests:
                self._reference(task.verification.tests)
            self._tasks[row.task_ref] = task
        return self._tasks[row.task_ref].model_copy(deep=True)

    def _reference(self, key: str) -> Path:
        path = confined(self.root, key)
        if not path.exists():
            raise ValueError(f"missing consumed asset: {key}")
        return path

    def instruction(self, row: DatasetRow) -> str:
        task = self.read_task(row)
        return read_bytes(
            self.root, str(PurePosixPath(row.task_ref).parent / task.instruction), INSTRUCTION_LIMIT
        ).decode("utf-8")

    def read_dataset(self, path: str | Path | None = None) -> list[DatasetRow]:
        rows = read_dataset(path or self.root / "dataset.jsonl")
        for row in rows:
            if row.release_id != self.manifest.release_id:
                raise ValueError("dataset contains an unbound release")
        return rows


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

    def instruction(self) -> str:
        return self.reader.instruction(self.row)


class ReleaseBindings:
    def __init__(
        self,
        bindings: Mapping[str, ReleaseBinding | Mapping[str, Any]],
        *,
        storage: Any,
        inputs: Mapping[str, Any] | None = None,
    ) -> None:
        self._readers: dict[str, tuple[ReleaseReader, str, str]] = {}
        local_roots: set[Path] = set()
        platform_keys: set[tuple[str, str]] = set()
        inputs = inputs or {}
        for value in bindings.values():
            binding = value if isinstance(value, ReleaseBinding) else ReleaseBinding.model_validate(value)
            if binding.root != "work" and binding.root not in inputs:
                raise ValueError(f"release bound to unconfigured storage root {binding.root!r}")
            key = platform_key(binding.platform_root, storage if binding.root == "work" else inputs[binding.root])
            reader = ReleaseReader(binding.local_root)
            identity = reader.manifest.release_id
            root = reader.root.resolve()
            if identity in self._readers or root in local_roots or (binding.root, key) in platform_keys:
                raise ValueError("conflicting or duplicate release bindings")
            self._readers[identity] = reader, key, binding.root
            local_roots.add(root)
            platform_keys.add((binding.root, key))

    def read_dataset(self, path: str | Path) -> list[DatasetRow]:
        rows = read_dataset(path)
        if any(row.release_id not in self._readers for row in rows):
            raise ValueError("dataset references a release without an explicit binding")
        return rows

    def resolve(self, row: DatasetRow | Mapping[str, Any]) -> BoundTask:
        row = row if isinstance(row, DatasetRow) else DatasetRow.model_validate(row)
        if row.release_id not in self._readers:
            raise ValueError("release is not explicitly bound")
        reader, key, root = self._readers[row.release_id]
        return BoundTask(row, reader.read_task(row), reader, key, root)


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
