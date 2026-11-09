"""Read native Harbor metadata without inspecting environment or verifier payloads.

Only task.toml and instruction.md are opened. This reader has no platform client,
filesystem cache, or persisted identity; callers own discovery and publication.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import multiprocessing
from collections.abc import Callable, Iterable, Mapping
from concurrent.futures import FIRST_COMPLETED, Future, ProcessPoolExecutor, wait
from pathlib import Path
from threading import Event
from typing import Any

from harbor.models.task.config import TaskConfig
from harbor.models.task.task import strip_canary
from pydantic import BaseModel, ConfigDict, field_validator

from flotilla.runtime.workspace import source_task_key

_BATCH_SIZE = 64


class SourceMapping(BaseModel):
    """A local dataset directory and its component-delimited platform key prefix."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    local_root: Path
    platform_prefix: str

    @field_validator("platform_prefix")
    @classmethod
    def _validate_prefix(cls, value: str) -> str:
        return source_task_key(value)


def metadata_identity(description: Mapping[str, Any]) -> str:
    """Hash effective config and visible instruction, independent of task location."""
    canonical = {
        "task_config": TaskConfig.model_validate(description["task_config"]).model_dump(mode="json"),
        "instruction": strip_canary(description["instruction"]),
    }
    return hashlib.sha256(json.dumps(canonical, sort_keys=True, separators=(",", ":")).encode("utf-8")).hexdigest()


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

    def key_for_path(self, path: Path | str) -> str:
        """Return the platform key of an existing directory in exactly one source."""
        resolved = Path(path).expanduser().resolve(strict=True)
        matches = [source for source in self.sources if resolved.is_relative_to(source.local_root)]
        if len(matches) != 1:
            raise ValueError(f"task path must match exactly one source mapping: {path!r}")
        if not resolved.is_dir():
            raise NotADirectoryError(resolved)
        mapping = matches[0]
        relative = resolved.relative_to(mapping.local_root).as_posix()
        key = mapping.platform_prefix if relative == "." else f"{mapping.platform_prefix}/{relative}"
        return source_task_key(key)

    def _input_file(self, key: str, task: Path, name: str) -> Path:
        path = (task / name).resolve(strict=True)
        if not path.is_relative_to(task) or not path.is_relative_to(self._mapping(key).local_root):
            raise ValueError(f"{name} escapes task or source root: {key!r}")
        if not path.is_file():
            raise ValueError(f"{name} is not a regular file: {key!r}")
        return path

    def read_config(self, key: str) -> TaskConfig:
        """Parse native config and defaults without reading instruction.md."""
        task = self.resolve_task(key)
        return TaskConfig.model_validate_toml(self._input_file(key, task, "task.toml").read_text(encoding="utf-8"))

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

    async def read_many(self, keys: Iterable[str], *, workers: int = 8) -> dict[str, dict[str, Any]]:
        """Read unique full keys in first-occurrence order, off the event loop.

        Spawn workers handle batches of 64, with at most twice the worker count
        submitted at once. Cancellation stops submission, cancels queued work,
        and joins running workers before propagating (an active read must finish).
        A single worker uses the direct reader without creating any subprocess.
        """
        return await self._read_many_async(keys, workers, self.read_metadata)

    async def read_configs(self, keys: Iterable[str], *, workers: int = 8) -> dict[str, TaskConfig]:
        """Read config only, with read_many's ordering, concurrency and cleanup rules."""
        return await self._read_many_async(keys, workers, self.read_config)

    async def _read_many_async[T](self, keys: Iterable[str], workers: int, read: Callable[[str], T]) -> dict[str, T]:
        if isinstance(workers, bool) or not isinstance(workers, int) or workers < 1:
            raise ValueError("workers must be a positive integer")
        stop = Event()
        job = asyncio.create_task(asyncio.to_thread(self._read_many, keys, workers, stop, read))
        try:
            return await asyncio.shield(job)
        except asyncio.CancelledError:
            stop.set()
            # Shield cleanup even if the caller cancels again while workers exit.
            while not job.done():
                try:
                    await asyncio.shield(job)
                except asyncio.CancelledError:
                    continue
                except Exception:
                    break
            if not job.cancelled():
                job.exception()
            raise

    def _read_many[T](self, keys: Iterable[str], workers: int, stop: Event, read: Callable[[str], T]) -> dict[str, T]:
        unique: dict[str, None] = {}
        for key in keys:
            if stop.is_set():
                return {}
            self._mapping(key)
            unique[key] = None
        ordered = list(unique)
        if not ordered:
            return {}
        workers = min(workers, (len(ordered) + _BATCH_SIZE - 1) // _BATCH_SIZE)
        if workers == 1:
            result = {}
            for key in ordered:
                if stop.is_set():
                    return {}
                result[key] = read(key)
            return result

        executor = ProcessPoolExecutor(max_workers=workers, mp_context=multiprocessing.get_context("spawn"))
        pending: set[Future[dict[str, T]]] = set()
        result = {}
        offset = 0
        try:
            while offset < len(ordered) or pending:
                if stop.is_set():
                    return {}
                while offset < len(ordered) and len(pending) < 2 * workers and not stop.is_set():
                    batch = ordered[offset : offset + _BATCH_SIZE]
                    pending.add(executor.submit(_read_batch, read, batch))
                    offset += len(batch)
                done, pending = wait(pending, timeout=0.1, return_when=FIRST_COMPLETED)
                for future in done:
                    result.update(future.result())
            return {key: result[key] for key in ordered}
        finally:
            for future in pending:
                future.cancel()
            executor.shutdown(wait=True, cancel_futures=True)


def _under_prefix(key: str, prefix: str) -> bool:
    return key == prefix or key.startswith(prefix + "/")


def _read_batch[T](read: Callable[[str], T], keys: list[str]) -> dict[str, T]:
    return {key: read(key) for key in keys}
