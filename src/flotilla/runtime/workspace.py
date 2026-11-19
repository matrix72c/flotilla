"""Attempt-owned platform storage. No task data is staged on the caller's filesystem."""

from __future__ import annotations

import asyncio
import uuid
from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

from flotilla.core.anchor import Anchor
from flotilla.core.waiting import finish_past_cancel
from flotilla.native.dataset import RESERVED
from flotilla.native.models import relative_path

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable, Coroutine

    from flotilla.config import FlotillaConfig
    from flotilla.runtime.client import Flotilla


def source_task_key(value: str) -> str:
    if relative_path(value).split("/", 1)[0] in RESERVED:
        raise ValueError("task source must be an original Harbor dataset, not a runtime/publication directory")
    return value


def runtime_parameters(value: Mapping[str, str] | None) -> dict[str, str]:
    if value is None:
        return {}
    if not isinstance(value, Mapping) or any(
        not isinstance(k, str) or not isinstance(v, str) for k, v in value.items()
    ):
        raise TypeError("runtime_params must be a mapping of strings to strings")
    return dict(value)


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
        self.closing = False

    async def create_empty(self, task_key: str) -> WorkspaceInfo:
        """Create private state without copying or compiling a source task."""
        wid = uuid.uuid4().hex
        key = f"launches/{self.anchor.launch_id}/attempts/{wid}"
        info = WorkspaceInfo(wid, key, task_key, task_key.rsplit("/", 1)[-1], key)
        self.records[wid] = _Owned(info)
        try:
            await self._settled(lambda: self.anchor.mkdir(key, stage="prepare"))
            return info
        except BaseException as primary:
            try:
                await self.remove(wid)
            except BaseException as cleanup:
                primary.add_note(f"workspace cleanup also failed: {cleanup!r}")
            raise

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
            try:
                await finish_past_cancel(task)
            except Exception as exc:
                primary.add_note(f"workspace operation failed during cancellation: {exc!r}")
            raise
        finally:
            self.jobs.discard(task)

    def owned(self, wid: str) -> _Owned:
        if wid not in self.records or self.records[wid].closing:
            raise ValueError("workspace is not owned by this client or has been released")
        return self.records[wid]

    async def operation[T](self, wid: str, action: Callable[[WorkspaceInfo], Awaitable[T]]) -> T:
        record = self.owned(wid)

        async def run() -> T:
            async with record.lock:
                self.owned(wid)
                return await action(record.info)

        return await self._settled(run)

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
    """A caller-side handle to one attempt's platform-side directory."""

    def __init__(self, fleet: Flotilla, info: WorkspaceInfo) -> None:
        self._fleet = fleet
        self.id, self.key = info.id, info.key
        self._closed = False

    async def close(self) -> None:
        if not self._closed:
            await self._fleet._call(lambda m: m.release_workspace(self.id), settle_cancel=True)
            self._closed = True
