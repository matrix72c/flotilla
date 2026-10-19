"""User handles; no platform connection or asynchronous primitive crosses event loops."""

from __future__ import annotations

import asyncio
import io
import math
import os
import stat
import tarfile
import time
import uuid
from collections.abc import Mapping, Sequence
from contextlib import suppress
from pathlib import Path
from typing import TYPE_CHECKING

from flotilla._io import owned_io
from flotilla.platform.base import (
    ErrorCategory,
    ExecAttempt,
    ExecOutcomeUnknown,
    ExecResult,
    FlotillaError,
    ProcessSpec,
    ProcessStatus,
)
from flotilla.runtime.manager import Manager, Run
from flotilla.runtime.types import CleanupReport, EnvironmentInfo

if TYPE_CHECKING:
    from flotilla.runtime.client import Flotilla

MAX_TRANSFER_BYTES = 512 * 1024 * 1024


class Environment:
    def __init__(self, client: Flotilla, info: EnvironmentInfo) -> None:
        self._client = client
        self._info = info
        self.id = info.environment_id
        self._cleanup_report: CleanupReport | None = None

    @property
    def agent(self) -> Service:
        return self.service(self._info.agent_service)

    def service(self, name: str) -> Service:
        if name not in {s.name for s in self._info.services}:
            raise KeyError(name)
        return Service(self, name)

    async def info(self) -> EnvironmentInfo:
        async def get(manager: Manager) -> EnvironmentInfo:
            return manager.info(self.id)

        return await self._client._call(get)

    async def check(self) -> None:
        await self._client._call(lambda manager: manager.check(self.id))

    async def release(self, timeout: float | None = None) -> CleanupReport:
        """Release and confirm this environment, retaining cleanup ownership on cancellation."""
        from flotilla.runtime.manager import _timeout

        budget = self._client.config.timeouts.close if timeout is None else timeout
        _timeout(budget)
        if self._cleanup_report is None:
            self._cleanup_report = await self._client._release_environment(self.id, budget)
        return self._cleanup_report


class Service:
    def __init__(self, env: Environment, name: str) -> None:
        self._env = env
        self.name = name

    async def terminate(self, timeout: float | None = None) -> None:
        """Delete this service's sandbox; collect required files before calling."""
        budget = self._env._client.config.timeouts.close if timeout is None else timeout
        await self._env._client._call(lambda manager: manager.terminate(self._env.id, self.name, budget))

    async def exec(
        self,
        argv: Sequence[str],
        *,
        timeout: float | None = None,
        cwd: str | None = None,
        env: Mapping[str, str] | None = None,
        uid: int | None = None,
        gid: int | None = None,
    ) -> ExecResult:
        proc_args = _args(argv, timeout if timeout is not None else 1, env)

        async def call(manager: Manager) -> ExecResult:
            async def execute(run: Run) -> ExecResult:
                assert run.ready is not None
                budget = manager.cfg.runtime.exec_default_timeout_s if timeout is None else timeout
                if budget > manager.cfg.limits.exec_timeout:
                    raise ValueError("command timeout exceeds limits.exec_timeout")
                attempt = ExecAttempt()
                handle = run.ready.handles[self.name]
                queued = time.monotonic()
                try:
                    async with manager.exec_slots:
                        manager.emit(
                            "execution_admitted", self._env.id, service=self.name, elapsed_s=time.monotonic() - queued
                        )
                        manager.get(self._env.id)
                        remaining = min(budget, max(0.001, run.deadline - time.monotonic()))
                        proc = self._spec(run, proc_args, remaining, cwd, env, uid, gid)
                        async with asyncio.timeout(remaining):
                            return await manager.platform.exec(handle, proc, attempt=attempt)
                except asyncio.CancelledError:
                    await manager.cancel_execution(self._env.id, handle, attempt)
                    raise
                except TimeoutError:
                    if not attempt.sent:
                        raise FlotillaError(
                            "command expired waiting for admission",
                            stage="run",
                            category=ErrorCategory.CAPACITY,
                            retryable=True,
                        ) from None
                    await manager.cancel_execution(self._env.id, handle, attempt)
                    if run.released:
                        raise ExecOutcomeUnknown(
                            "command deadline exceeded; termination could not be confirmed",
                            command_id=attempt.command_id,
                        ) from None
                    return ExecResult(-1, b"", b"", timed_out=True)
                except FlotillaError as exc:
                    if attempt.sent or isinstance(exc, ExecOutcomeUnknown):
                        await manager.release(self._env.id)
                    raise

            return await manager.operation(self._env.id, self.name, execute)

        # A cancelled command must finish termination confirmation before the
        # caller can collect files or issue another command in this environment.
        return await self._env._client._call(call, settle_cancel=True)

    async def shell(
        self,
        command: str,
        *,
        shell: str = "/bin/sh",
        timeout: float | None = None,
        cwd: str | None = None,
        env: Mapping[str, str] | None = None,
        uid: int | None = None,
        gid: int | None = None,
    ) -> ExecResult:
        return await self.exec([shell, "-c", command], timeout=timeout, cwd=cwd, env=env, uid=uid, gid=gid)

    def _spec(
        self,
        run: Run,
        argv: tuple[str, ...],
        timeout: float | None,
        cwd: str | None,
        env: Mapping[str, str] | None,
        uid: int | None,
        gid: int | None,
    ) -> ProcessSpec:
        ctx = run.plan.units[self.name].context
        return ProcessSpec(
            argv,
            ctx.uid if uid is None else uid,
            ctx.gid if gid is None else gid,
            ctx.cwd if cwd is None else cwd,
            {**ctx.env, **run.request.exec_env.get(self.name, {}), **dict(env or {})},
            timeout,
        )

    async def start_process(
        self,
        argv: Sequence[str],
        *,
        cwd: str | None = None,
        env: Mapping[str, str] | None = None,
        uid: int | None = None,
        gid: int | None = None,
    ) -> Process:
        args = _args(argv, 1, env)

        async def call(manager: Manager) -> str:
            async def execute(run: Run) -> str:
                assert run.ready is not None
                handle = run.ready.handles[self.name]
                attempt = ExecAttempt()
                try:
                    return await manager.platform.start_process(
                        handle, self._spec(run, args, None, cwd, env, uid, gid), attempt=attempt
                    )
                except asyncio.CancelledError:
                    await manager.cancel_execution(self._env.id, handle, attempt)
                    raise
                except Exception:
                    if attempt.sent:
                        await manager.cancel_execution(self._env.id, handle, attempt)
                    raise

            return await manager.operation(self._env.id, self.name, execute)

        async def undelivered(manager: Manager, pid: str) -> None:
            run = manager.runs[self._env.id]
            if run.ready is not None:
                await manager.cancel_execution(
                    self._env.id, run.ready.handles[self.name], ExecAttempt(sent=True, command_id=pid)
                )

        pid = await self._env._client._call(call, settle_cancel=True, cancel_result=undelivered)
        return Process(self, pid)

    async def read_file(self, path: str) -> bytes:
        async def call(manager: Manager) -> bytes:
            async def read(run: Run) -> bytes:
                assert run.ready is not None
                data = await manager.platform.read_file(run.ready.handles[self.name], path)
                if len(data) > MAX_TRANSFER_BYTES:
                    raise ValueError("file exceeds transfer limit")
                return data

            return await manager.operation(self._env.id, self.name, read)

        return await self._env._client._call(call)

    async def write_file(self, path: str, data: bytes, *, mode: int = 0o644) -> None:
        if len(data) > MAX_TRANSFER_BYTES or not 0 <= mode <= 0o777:
            raise ValueError("invalid file size or mode")

        async def call(manager: Manager) -> None:
            async def write(run: Run) -> None:
                assert run.ready is not None
                ctx = run.plan.units[self.name].context
                await manager.platform.write_file(
                    run.ready.handles[self.name], path, data, mode=mode, uid=ctx.uid, gid=ctx.gid
                )

            await manager.operation(self._env.id, self.name, write)

        await self._env._client._call(call)

    async def upload_file(self, source: str | Path, target: str) -> None:
        path = Path(source)
        info = path.lstat()
        if not stat.S_ISREG(info.st_mode) or info.st_size > MAX_TRANSFER_BYTES:
            raise ValueError("upload requires a regular file within the transfer limit")
        data = await owned_io(path.read_bytes)
        await self.write_file(target, data, mode=stat.S_IMODE(info.st_mode) & 0o777)

    async def download_file(self, source: str, target: str | Path) -> None:
        data = await self.read_file(source)
        path = Path(target)
        if path.is_symlink():
            raise ValueError("download destination is a symlink")
        await owned_io(path.write_bytes, data)

    async def upload_dir(self, source: str | Path, target: str) -> None:
        data = await owned_io(_pack, Path(source))
        temporary = f"/tmp/flotilla-{uuid.uuid4().hex}.tar"
        try:
            await self.write_file(temporary, data, mode=0o600)
            _success(await self.exec(["/.flotilla/bin/busybox", "mkdir", "-p", target]))
            _success(await self.exec(["/.flotilla/bin/busybox", "tar", "-xf", temporary, "-C", target]))
        finally:
            await self._remove_temporary(temporary)

    async def download_dir(self, source: str, target: str | Path) -> None:
        temporary = f"/tmp/flotilla-{uuid.uuid4().hex}.tar"
        try:
            _success(await self.exec(["/.flotilla/bin/busybox", "tar", "-cf", temporary, "-C", source, "."]))
            data = await self.read_file(temporary)
            await owned_io(_unpack, data, Path(target))
        finally:
            await self._remove_temporary(temporary)

    async def _remove_temporary(self, path: str) -> None:
        # Environment release also removes its private /tmp; cleanup cannot mask the transfer error.
        with suppress(Exception):
            await self.exec(["/.flotilla/bin/busybox", "rm", "-f", path])


class Process:
    def __init__(self, service: Service, pid: str) -> None:
        self.service = service
        self.id = pid

    async def status(self) -> ProcessStatus:
        service = self.service

        async def call(manager: Manager) -> ProcessStatus:
            async def get(run: Run) -> ProcessStatus:
                assert run.ready is not None
                result = await manager.platform.process_status(run.ready.handles[service.name], self.id)
                if not result.found and run.monitor:
                    raise run.monitor.mark_lost("background process record disappeared")
                return result

            return await manager.operation(service._env.id, service.name, get)

        return await service._env._client._call(call)


def _args(argv: Sequence[str], timeout: float, env: Mapping[str, str] | None) -> tuple[str, ...]:
    if isinstance(argv, str) or not argv or any(not isinstance(a, str) or "\0" in a for a in argv):
        raise ValueError("argv must be a nonempty sequence of strings without NUL")
    if not math.isfinite(timeout) or timeout <= 0:
        raise ValueError("timeout must be finite and positive")
    if env and any(not k or "=" in k or "\0" in k or "\0" in v for k, v in env.items()):
        raise ValueError("invalid execution environment")
    return tuple(argv)


def _success(result: ExecResult) -> None:
    if result.exit_code != 0 or result.timed_out:
        raise RuntimeError(f"file transfer command failed: {result.stderr[:1024]!r}")


def _pack(root: Path) -> bytes:
    if root.is_symlink() or not root.is_dir():
        raise ValueError("directory transfer requires a real directory")
    buf = io.BytesIO()
    size = 0
    with tarfile.open(fileobj=buf, mode="w") as archive:
        for current, dirs, files in os.walk(root, followlinks=False):
            for name in sorted([*dirs, *files]):
                path = Path(current) / name
                info = path.lstat()
                if not (stat.S_ISDIR(info.st_mode) or stat.S_ISREG(info.st_mode)):
                    raise ValueError("directory transfers reject links and special files")
                size += info.st_size if stat.S_ISREG(info.st_mode) else 0
                if size > MAX_TRANSFER_BYTES:
                    raise ValueError("directory exceeds transfer limit")
                archive.add(path, arcname=str(path.relative_to(root)), recursive=False)
    data = buf.getvalue()
    if len(data) > MAX_TRANSFER_BYTES:
        raise ValueError("archive exceeds transfer limit")
    return data


def _unpack(data: bytes, root: Path) -> None:
    if root.is_symlink():
        raise ValueError("directory destination is a symlink")
    root.mkdir(parents=True, exist_ok=True)
    root = root.resolve()
    total = 0
    with tarfile.open(fileobj=io.BytesIO(data), mode="r:") as archive:
        for member in archive:
            total += member.size
            target = root / member.name
            if total > MAX_TRANSFER_BYTES or not target.resolve().is_relative_to(root):
                raise ValueError("unsafe archive path or oversized directory")
            if member.isdir():
                target.mkdir(parents=True, exist_ok=True)
            elif member.isfile():
                target.parent.mkdir(parents=True, exist_ok=True)
                if target.is_symlink():
                    raise ValueError("archive destination is a symlink")
                stream = archive.extractfile(member)
                assert stream is not None
                target.write_bytes(stream.read())
                target.chmod(member.mode & 0o777)
            else:
                raise ValueError("directory transfers reject links and special files")
