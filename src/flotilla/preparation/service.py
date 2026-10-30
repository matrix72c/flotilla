"""Prepare task sources without exposing build artifacts as user inputs."""

from __future__ import annotations

import asyncio
import contextlib
import json
import os
import shutil
import signal
import sys
import tempfile
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

from flotilla._io import owned_io
from flotilla.compose.task import HarborTask, load_environment
from flotilla.config import FlotillaConfig
from flotilla.core.anchor import Anchor
from flotilla.manifest import Manifest
from flotilla.preparation.source import task_environments
from flotilla.runtime.types import (
    EnvironmentDefinition,
    EnvironmentInspection,
    PreparationResidual,
    Task,
)
from flotilla.scan import scan_task
from flotilla.share.publish import mark_published, publish_task_files


def definition_of(task: HarborTask) -> EnvironmentDefinition:
    return EnvironmentDefinition(
        task.path.resolve(), task.env_dir.resolve(), task.name, dict(task.environment_config or {})
    )


def definitions(path: str | Path) -> tuple[EnvironmentDefinition, ...]:
    tasks = task_environments(Path(path).resolve())
    for task in tasks:
        rejected = [f.reason for f in task.findings if f.kind == "reject"]
        if rejected:
            raise ValueError("unsupported task environment: " + "; ".join(rejected))
    return tuple(definition_of(task) for task in tasks)


def inspect(cfg: FlotillaConfig, path: str | Path) -> tuple[EnvironmentInspection, ...]:
    return tuple(_inspect_task(cfg, task) for task in task_environments(Path(path).resolve()))


def inspect_environment(cfg: FlotillaConfig, definition: EnvironmentDefinition) -> EnvironmentInspection:
    """Inspect exactly the effective context supplied to open_environment."""
    task = load_environment(definition.task_root, definition.directory, definition.config, name=definition.name)
    return _inspect_task(cfg, task)


def _inspect_task(cfg: FlotillaConfig, task: HarborTask) -> EnvironmentInspection:
    scan = scan_task(task, cfg.platform_capabilities())
    return EnvironmentInspection(
        definition_of(task),
        scan.status,
        tuple({"path": f.path, "kind": f.kind, "reason": f.reason} for f in scan.findings),
        {
            name: cfg.resources.effective(task.name, name, dict(service.resources))
            for name, service in task.project.services.items()
        },
    )


@dataclass
class Operation:
    directory: Path
    processes: dict[int, asyncio.subprocess.Process] = field(default_factory=dict)
    remote_dirs: set[str] = field(default_factory=set)
    cleaned: bool = False


async def settle(task: asyncio.Task[Any]) -> Any:
    """Wait for owned work even when its waiter is cancelled repeatedly."""
    while not task.done():
        try:
            await asyncio.shield(task)
        except asyncio.CancelledError:
            continue
    return task.result()


class Preparer:
    def __init__(self, cfg: FlotillaConfig, anchor: Anchor) -> None:
        self.cfg = cfg
        self.anchor = anchor
        self.closed = False
        self.slots = asyncio.Semaphore(cfg.preparation.concurrency)
        self.pending: set[asyncio.Task[Any]] = set()
        self.operations: dict[asyncio.Task[Any], Operation] = {}
        self.residuals: list[PreparationResidual] = []

    async def close(self, timeout: float) -> tuple[PreparationResidual, ...]:
        self.closed = True
        active = list(self.pending)
        for task in active:
            task.cancel()
        if active:
            _, pending = await asyncio.wait(active, timeout=timeout)
            for task in pending:
                operation = self.operations.get(task)
                if operation is not None:
                    self.residuals.append(
                        PreparationResidual("operation", str(operation.directory), "cleanup deadline exceeded")
                    )
        return tuple(self.residuals)

    async def resolve(
        self, definition: EnvironmentDefinition | Path, *, force_build: bool = False
    ) -> tuple[Task, tuple[tuple[str, float], ...]]:
        if self.closed:
            raise RuntimeError("preparation is closed")
        current = asyncio.current_task()
        assert current is not None
        self.pending.add(current)
        try:
            async with asyncio.timeout(self.cfg.preparation.timeout_s):
                await self.slots.acquire()
                if self.closed:
                    self.slots.release()
                    raise RuntimeError("preparation is closed")
                try:
                    operation = Operation(Path(tempfile.mkdtemp(prefix="flotilla-prepare-")))
                except BaseException:
                    self.slots.release()
                    raise
                self.operations[current] = operation
                try:
                    selected = (
                        (await owned_io(definitions, definition))[0] if isinstance(definition, Path) else definition
                    )
                    return await self._resolve(selected, operation, force_build=force_build)
                finally:
                    cleanup = asyncio.create_task(self._cleanup(operation))
                    try:
                        await asyncio.shield(cleanup)
                    except asyncio.CancelledError:
                        await settle(cleanup)
                        raise
                    finally:
                        self.operations.pop(current, None)
                        if operation.cleaned:
                            self.slots.release()
        finally:
            self.pending.discard(current)

    async def _resolve(
        self, definition: EnvironmentDefinition, operation: Operation, *, force_build: bool
    ) -> tuple[Task, tuple[tuple[str, float], ...]]:
        started = time.monotonic()
        # Definition validation and Docker work run in the isolated child, so
        # filesystem reads cannot block the runtime's renewal/cleanup loop.
        stage = operation.directory
        request, result = stage / "request.json", stage / "result.json"
        config = self.cfg.to_mapping()
        request.write_text(
            json.dumps(
                {
                    "definition": asdict(definition),
                    "config": config,
                    "force_build": force_build,
                    "work": str(stage / "work"),
                    "result": str(result),
                    "containers": str(stage / "containers"),
                },
                default=str,
            )
        )
        await self._build_process(request)
        outcome = json.loads(result.read_text())
        source = Path(outcome["manifest"])
        manifest = Manifest.load_json(source.read_bytes())
        built = time.monotonic()
        if outcome["needs_publish"]:
            files = source.parent / "files"
            identity = outcome["files_digest"]
            published = await publish_task_files(
                self.anchor,
                identity,
                files,
                owned_staging=operation.remote_dirs,
            )
            manifest = mark_published(manifest, published.task_files, self.cfg.storage_settings().root)
        return Task(manifest), (
            ("prepare_images_s", built - started),
            ("publish_files_s", time.monotonic() - built),
        )

    async def _terminate(self, process: asyncio.subprocess.Process, operation: Operation) -> None:
        try:
            with contextlib.suppress(ProcessLookupError):
                os.killpg(process.pid, signal.SIGTERM)
            with contextlib.suppress(TimeoutError):
                await asyncio.wait_for(process.wait(), 0.25)
            # A terminated group leader does not prove its Docker children exited.
            with contextlib.suppress(ProcessLookupError):
                os.killpg(process.pid, signal.SIGKILL)
            await asyncio.wait_for(process.wait(), 1)
            operation.processes.pop(process.pid, None)
        except (OSError, TimeoutError) as exc:
            self.residuals.append(PreparationResidual("process", str(process.pid), type(exc).__name__))

    async def _run(self, argv: list[str], operation: Operation, *, timeout: float | None = None) -> tuple[int, str]:
        with tempfile.TemporaryFile() as output:
            spawn = asyncio.create_task(
                asyncio.create_subprocess_exec(
                    *argv,
                    stdout=output,
                    stderr=output,
                    start_new_session=True,
                    env={
                        key: value
                        for key, value in os.environ.items()
                        if key != self.cfg.backend_settings.credential_env
                    },
                )
            )
            try:
                process = await asyncio.shield(spawn)
            except BaseException:
                process = await settle(spawn)
                operation.processes[process.pid] = process
                await settle(asyncio.create_task(self._terminate(process, operation)))
                raise
            operation.processes[process.pid] = process
            try:
                async with asyncio.timeout(timeout):
                    code = await process.wait()
            except BaseException:
                await settle(asyncio.create_task(self._terminate(process, operation)))
                raise
            operation.processes.pop(process.pid, None)
            output.seek(0, os.SEEK_END)
            output.seek(max(0, output.tell() - 4096))
            return code, output.read().decode(errors="replace")

    async def _build_process(self, request: Path) -> None:
        current = asyncio.current_task()
        assert current is not None
        operation = self.operations[current]
        code, diagnostic = await self._run(
            [sys.executable, "-m", "flotilla.preparation.build_worker", str(request)],
            operation,
        )
        if code:
            raise ValueError(f"environment preparation failed: {diagnostic}")

    async def _cleanup(self, operation: Operation) -> None:
        for process in list(operation.processes.values()):
            await self._terminate(process, operation)
        if operation.processes:
            self.residuals.append(
                PreparationResidual("directory", str(operation.directory), "process exit unconfirmed")
            )
            return
        journal = operation.directory / "containers"
        failed = False
        if journal.is_dir():
            for record in journal.iterdir():
                name = record.name
                try:
                    code, output = await self._run(
                        [*self.cfg.preparation.docker_command, "rm", "-f", "-v", name],
                        operation,
                        timeout=min(5, self.cfg.timeouts.close),
                    )
                    if code and "No such container" not in output:
                        raise RuntimeError("Docker removal was not confirmed")
                    record.unlink()
                except (OSError, RuntimeError, TimeoutError) as exc:
                    self.residuals.append(PreparationResidual("docker_container", name, type(exc).__name__))
                    failed = True
        for directory in list(operation.remote_dirs):
            try:
                async with asyncio.timeout(min(5, self.cfg.timeouts.close)):
                    await self.anchor.remove(directory, stage="prepare")
                operation.remote_dirs.discard(directory)
            except Exception as exc:
                self.residuals.append(PreparationResidual("published_staging", directory, type(exc).__name__))
                failed = True
        if failed or operation.processes:
            self.residuals.append(
                PreparationResidual("directory", str(operation.directory), "retained cleanup journal")
            )
            return
        try:
            await owned_io(shutil.rmtree, operation.directory)
            operation.cleaned = True
        except OSError as exc:
            self.residuals.append(PreparationResidual("directory", str(operation.directory), type(exc).__name__))
