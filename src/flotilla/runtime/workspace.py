"""Attempt-owned platform storage. No task data is staged on the caller's filesystem."""

from __future__ import annotations

import asyncio
import json
import posixpath
import uuid
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

from flotilla.core.anchor import Anchor

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable, Coroutine

    from flotilla.config import FlotillaConfig
    from flotilla.runtime.client import Flotilla


def relative_key(value: str) -> str:
    """Reject ambiguous paths rather than normalizing a reference to another task."""
    if not isinstance(value, str) or not value or any(p in ("", ".", "..") for p in value.split("/")):
        raise ValueError("storage paths must be nonempty canonical relative paths")
    if "\\" in value or any(ord(c) < 32 for c in value):
        raise ValueError("storage paths cannot contain backslashes or control characters")
    return value


@dataclass(frozen=True)
class WorkspaceInfo:
    id: str
    key: str
    task_key: str
    task_id: str
    source_key: str


@dataclass
class _Owned:
    info: WorkspaceInfo
    lock: asyncio.Lock = field(default_factory=asyncio.Lock)
    closing: bool = False


class WorkspaceStore:
    """Runs on the manager loop; the launch lease owns abandoned directories too."""

    def __init__(self, anchor: Anchor, config: FlotillaConfig) -> None:
        self.anchor, self.config = anchor, config
        self.records: dict[str, _Owned] = {}
        self.jobs: set[asyncio.Task[Any]] = set()
        self.compile_slots = asyncio.Semaphore(config.prepare.concurrency)
        self.closing = False

    async def _settled[T](self, action: Callable[[], Coroutine[Any, Any, T]]) -> T:
        if self.closing:
            raise RuntimeError("workspace store is closing")
        task = asyncio.create_task(action())
        self.jobs.add(task)
        try:
            return await asyncio.shield(task)
        except asyncio.CancelledError as primary:
            # Copy/build may have side effects. Do not delete their directory or
            # allow a caller to retry until the owned operation has terminated.
            while not task.done():
                try:
                    await asyncio.shield(task)
                except asyncio.CancelledError:
                    continue
                except Exception:
                    break
            try:
                task.result()
            except Exception as exc:
                primary.add_note(f"workspace operation failed during cancellation: {exc!r}")
            raise
        finally:
            self.jobs.discard(task)

    def owned(self, wid: str) -> _Owned:
        if wid not in self.records or self.records[wid].closing:
            raise ValueError("workspace is not owned by this client or has been released")
        return self.records[wid]

    async def create(self, task_key: str) -> WorkspaceInfo:
        task_key = relative_key(task_key)
        if task_key.split("/", 1)[0] in {"launches", "share"}:
            raise ValueError("task source must be an original Harbor dataset, not a runtime/publication directory")
        wid = uuid.uuid4().hex
        key = f"launches/{self.anchor.launch_id}/attempts/{wid}"
        name = posixpath.basename(task_key)
        info = WorkspaceInfo(wid, key, task_key, name, f"{key}/source/{name}")
        self.records[wid] = _Owned(info)

        async def copy() -> WorkspaceInfo:
            await self.anchor.copy_source_tree(task_key, info.source_key, stage="prepare")
            return info

        try:
            return await self._settled(copy)
        except BaseException as primary:
            try:
                await self.remove(wid)
            except BaseException as cleanup:
                primary.add_note(f"workspace cleanup also failed: {cleanup!r}")
            raise

    async def operation[T](self, wid: str, action: Callable[[WorkspaceInfo], Awaitable[T]]) -> T:
        record = self.owned(wid)

        async def run() -> T:
            async with record.lock:
                self.owned(wid)
                return await action(record.info)

        return await self._settled(run)

    def path(self, wid: str, relative: str) -> str:
        return f"{self.owned(wid).info.key}/{relative_key(relative)}"

    async def read(self, wid: str, relative: str) -> bytes:
        return await self.operation(wid, lambda _: self.anchor.download(self.path(wid, relative), stage="prepare"))

    async def write(self, wid: str, relative: str, data: bytes) -> None:
        async def run(_: WorkspaceInfo) -> None:
            key = self.path(wid, relative)
            await self.anchor.mkdir(posixpath.dirname(key), stage="prepare")
            await self.anchor.upload(key, data, mode=0o600, uid=0, gid=0, stage="prepare")

        await self.operation(wid, run)

    async def mkdir(self, wid: str, relative: str) -> None:
        await self.operation(wid, lambda _: self.anchor.mkdir(self.path(wid, relative), stage="prepare"))

    async def copy(self, wid: str, source: str, destination: str) -> None:
        await self.operation(
            wid,
            lambda _: self.anchor.copy_tree(self.path(wid, source), self.path(wid, destination), stage="prepare"),
        )

    async def copy_contents(self, wid: str, source: str, destination: str) -> None:
        await self.operation(
            wid,
            lambda _: self.anchor.copy_contents(self.path(wid, source), self.path(wid, destination), stage="prepare"),
        )

    async def prepare(self, wid: str, runtime_params: dict[str, str], *, describe: bool = False) -> dict[str, Any]:
        async def run(info: WorkspaceInfo) -> dict[str, Any]:
            root = self.anchor.path(info.key)
            request = {
                "workspace": root,
                "workspace_key": info.key,
                "task_id": info.task_id,
                "source": self.anchor.path(info.source_key),
                "deployment": self.config.model_dump(mode="json"),
                "capabilities": self.config.load_report().model_dump(mode="json", by_alias=True),
                "runtime_params": runtime_params,
                "describe": describe,
            }
            key = f"{info.key}/request.json"
            await self.anchor.upload(key, json.dumps(request).encode(), mode=0o600, uid=0, gid=0, stage="prepare")
            async with self.compile_slots:
                await self.anchor.execute(
                    (*self.config.prepare.workspace_command, self.anchor.path(key)),
                    timeout_s=self.config.prepare.workspace_timeout,
                    stage="prepare",
                )
            result = json.loads(await self.anchor.download(f"{info.key}/result.json", stage="prepare"))
            if not isinstance(result, dict):
                raise ValueError("workspace compiler returned a non-object result")
            return result

        return await self.operation(wid, run)

    async def remove(self, wid: str) -> None:
        record = self.records.get(wid)
        if record is None:
            return
        async with record.lock:
            record.closing = True
            # Leave ownership registered on failure, so close/GC can retry.
            await self.anchor.remove(record.info.key, stage="run")
            self.records.pop(wid, None)

    async def settle(self) -> None:
        self.closing = True
        if self.jobs:
            await asyncio.gather(*tuple(self.jobs), return_exceptions=True)


class Workspace:
    """A caller-side handle to one platform-side attempt directory."""

    def __init__(self, fleet: Flotilla, info: WorkspaceInfo) -> None:
        self._fleet = fleet
        self.id, self.key = info.id, info.key
        self.task_key, self.task_id, self.source_key = info.task_key, info.task_id, info.source_key
        self._closed = False

    async def read(self, relative: str) -> bytes:
        return await self._fleet._call(lambda m: m.workspaces.read(self.id, relative), settle_cancel=True)

    async def write(self, relative: str, data: bytes) -> None:
        await self._fleet._call(lambda m: m.workspaces.write(self.id, relative, data), settle_cancel=True)

    async def mkdir(self, relative: str) -> None:
        await self._fleet._call(lambda m: m.workspaces.mkdir(self.id, relative), settle_cancel=True)

    async def copy(self, source: str, destination: str) -> None:
        """Copy an entry to an absent destination, preserving metadata and links."""
        await self._fleet._call(lambda m: m.workspaces.copy(self.id, source, destination), settle_cancel=True)

    async def copy_contents(self, source: str, destination: str) -> None:
        await self._fleet._call(lambda m: m.workspaces.copy_contents(self.id, source, destination), settle_cancel=True)

    async def describe(self) -> dict[str, Any]:
        """Read native Harbor metadata without building images or creating workloads."""
        return await self._fleet._call(lambda m: m.workspaces.prepare(self.id, {}, describe=True), settle_cancel=True)

    async def prepare(self, runtime_params: dict[str, str] | None = None) -> dict[str, Any]:
        return await self._fleet._call(
            lambda m: m.workspaces.prepare(self.id, dict(runtime_params or {})), settle_cancel=True
        )

    async def close(self) -> None:
        if not self._closed:
            await self._fleet._call(lambda m: m.release_workspace(self.id), settle_cancel=True)
            self._closed = True

    async def __aenter__(self) -> Workspace:
        return self

    async def __aexit__(self, typ: object, exc: BaseException | None, tb: object) -> None:
        try:
            await self.close()
        except BaseException as cleanup:
            if exc is None:
                raise
            exc.add_note(f"workspace cleanup also failed: {cleanup!r}")
