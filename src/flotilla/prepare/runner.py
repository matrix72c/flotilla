"""Prepare dataset directories into a jsonl of prepared rows, one per task.

A task is rebuilt only when its id has no row in the output (or with `force`); every rebuild republishes its task
files, so a row and the files under `tasks/<id>/<role>` always come from the same run. Rows are appended as tasks
finish, so an interrupted run resumes where it stopped; the output is compacted (deduplicated, sorted) at the end.

Each dataset gets its own image repository, `<build.target>/<dataset>`, tagged `<task>--<role>--<service>--<key>`.
"""

from __future__ import annotations

import asyncio
import json
import math
import os
import re
import shutil
import subprocess
import tempfile
from collections.abc import Awaitable, Callable, Sequence
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field, replace
from decimal import Decimal
from pathlib import Path
from typing import Any, TextIO

from flotilla._version import __version__
from flotilla.build.driver import BuildContext, build_task
from flotilla.compose.task import HarborTask, TaskError, find_tasks
from flotilla.config import FlotillaConfig
from flotilla.core.anchor import Anchor
from flotilla.manifest import Manifest
from flotilla.platform.base import FlotillaError
from flotilla.platform.filesystem import memory_bytes
from flotilla.prepare.docker import docker_context, sweep_journal
from flotilla.prepare.inspect import main_only
from flotilla.prepared import ROLES, PreparedEnvironment, PreparedTask, dump_row, load_prepared
from flotilla.share.publish import TASKS, PublishError, mark_published, publish_task_dir

Environments = Callable[[Path], Sequence[HarborTask]]
ContextFactory = Callable[[Path], BuildContext]  # journal directory -> build context
AnchorFactory = Callable[[], Awaitable[Anchor]]  # returns a started anchor; prepare closes it
_UNSAFE = re.compile(r"[^A-Za-z0-9_.-]+")
_REPO_UNSAFE = re.compile(r"[^a-z0-9]+")


@dataclass(frozen=True)
class PrepareReport:
    prepared: tuple[str, ...] = ()
    skipped: tuple[str, ...] = ()
    failed: tuple[str, ...] = ()
    leftover_containers: tuple[str, ...] = field(default=())


@dataclass(frozen=True)
class _Source:
    id: str  # `<dataset>/<task path>`
    task_dir: Path
    dataset_root: Path

    @property
    def repository(self) -> str:
        """Image repository component for the dataset: lowercase, runs of other characters become `-`."""
        return _REPO_UNSAFE.sub("-", self.id.partition("/")[0].lower()).strip("-") or "dataset"


class _Failed(Exception):
    def __init__(self, stage: str, reason: str, environment: str | None = None) -> None:
        super().__init__(reason)
        self.stage, self.reason, self.environment = stage, reason, environment


def failures_path(out: Path) -> Path:
    return out.with_name(out.name.removesuffix(".jsonl") + ".failures.jsonl")


async def prepare(
    cfg: FlotillaConfig,
    roots: Sequence[Path],
    out: Path,
    *,
    anchor: AnchorFactory,
    environments: Environments = main_only,
    context: ContextFactory | None = None,
    failures: Path | None = None,
    force: bool = False,
    concurrency: int | None = None,
    work: Path | None = None,
) -> PrepareReport:
    """Prepare every task under `roots` (dataset directories, or single task directories) into `out`.

    `anchor` starts the shared-storage anchor; it is called only once a task has files to publish.
    """
    sources = _discover(roots)
    existing = {row.id for row in load_prepared(out)} if out.exists() else set()
    todo = [s for s in sources if force or s.id not in existing]
    skipped = tuple(s.id for s in sources if s not in todo)
    work = (work or out.with_name(out.name + ".work")).resolve()
    journal = work / "containers"
    scratch = work / "tmp"
    scratch.mkdir(parents=True, exist_ok=True)
    command = cfg.prepare.docker_command
    leftover = await asyncio.to_thread(sweep_journal, command, journal)
    if leftover:
        raise RuntimeError(f"export containers from an earlier run could not be removed: {leftover}")
    ctx = (context or (lambda j: docker_context(cfg, j)))(journal)
    revs = {root: _dataset_rev(root) for root in {s.dataset_root for s in todo}}
    failures = failures or failures_path(out)
    out.parent.mkdir(parents=True, exist_ok=True)
    failures.parent.mkdir(parents=True, exist_ok=True)
    workers = concurrency or cfg.prepare.concurrency
    session = _Session(cfg, ctx, anchor, environments, work, revs)
    previous_tempdir = tempfile.tempdir
    # Docker exports stage archives under the default temp dir; keep them on the work volume, not the root disk.
    tempfile.tempdir = str(scratch)
    prepared: list[str] = []
    failed: list[str] = []
    try:
        with (
            out.open("a", encoding="utf-8") as rows,
            failures.open("w", encoding="utf-8") as errors,
            ThreadPoolExecutor(max_workers=workers, thread_name_prefix="flotilla-prepare") as pool,
        ):
            session.pool = pool
            gate = asyncio.Semaphore(workers)

            async def one(source: _Source) -> None:
                async with gate:
                    try:
                        row = await session.prepare(source)
                    except _Failed as exc:
                        failed.append(source.id)
                        failure = {
                            "id": source.id,
                            "task_dir": str(source.task_dir),
                            "environment": exc.environment,
                            "stage": exc.stage,
                            "reason": exc.reason,
                        }
                        _append(errors, json.dumps(failure, ensure_ascii=False))
                        return
                    _append(rows, dump_row(row))
                    prepared.append(source.id)

            # Anything other than a per-task failure (e.g. the anchor cannot start) cancels the remaining tasks.
            async with asyncio.TaskGroup() as group:
                for source in todo:
                    group.create_task(one(source))
    finally:
        tempfile.tempdir = previous_tempdir
        await session.close()
        leftover = await asyncio.to_thread(sweep_journal, command, journal)
    if out.exists():
        _compact(out, drop=set(failed))
    return PrepareReport(tuple(sorted(prepared)), skipped, tuple(sorted(failed)), tuple(leftover))


class _Session:
    def __init__(
        self,
        cfg: FlotillaConfig,
        ctx: BuildContext,
        anchor: AnchorFactory,
        environments: Environments,
        work: Path,
        revs: dict[Path, str | None],
    ) -> None:
        self.cfg, self.ctx, self.environments, self.work, self.revs = cfg, ctx, environments, work, revs
        self._make_anchor = anchor
        self._anchor: Anchor | None = None
        self._lock = asyncio.Lock()
        self.pool: ThreadPoolExecutor | None = None

    async def _thread(self, fn: Callable[..., Any], *args: Any, **kwargs: Any) -> Any:
        return await asyncio.get_running_loop().run_in_executor(self.pool, lambda: fn(*args, **kwargs))

    async def anchor(self) -> Anchor:
        async with self._lock:
            if self._anchor is None:
                self._anchor = await self._make_anchor()
            return self._anchor

    async def close(self) -> None:
        if self._anchor is not None:
            await self._anchor.close()

    async def prepare(self, source: _Source) -> PreparedTask:
        try:
            envs = list(await self._thread(self.environments, source.task_dir))
        except (TaskError, ValueError, OSError) as exc:
            raise _Failed("resolve", f"{type(exc).__name__}: {exc}") from exc
        if len(envs) > len(ROLES):
            raise _Failed("resolve", "multiple verifier environments are not supported")
        scratch = self.work / _UNSAFE.sub("-", source.id)
        try:
            environments = {
                role: await self._environment(source, role, env, scratch / role)
                for role, env in zip(ROLES, envs, strict=False)
            }
        finally:
            await self._thread(shutil.rmtree, scratch, ignore_errors=True)
        return PreparedTask(
            id=source.id,
            task_dir=str(source.task_dir),
            environments=environments,
            dataset_rev=self.revs.get(source.dataset_root),
            flotilla_version=__version__,
            attrs=_attrs(self.cfg, environments),
        )

    async def _environment(self, source: _Source, role: str, env: HarborTask, scratch: Path) -> PreparedEnvironment:
        ctx = self.ctx
        if ctx.settings.target:
            ctx = replace(ctx, settings=replace(ctx.settings, target=f"{ctx.settings.target}/{source.repository}"))
        identity = f"{source.id.partition('/')[2].replace('/', '--')}--{role}"
        outcome = await self._thread(build_task, env, scratch, ctx, identity=identity)
        if outcome.manifest_path is None:
            stage = "scan" if outcome.status == "rejected" else "build"
            raise _Failed(stage, outcome.reason, role)
        manifest = Manifest.load_json(outcome.manifest_path.read_bytes())
        if outcome.needs_publish:
            anchor = await self.anchor()  # an anchor that cannot start ends the run
            try:
                published = await publish_task_dir(
                    anchor, f"{TASKS}/{source.id}/{role}", outcome.manifest_path.parent / "files"
                )
            except (PublishError, FlotillaError, OSError) as exc:
                raise _Failed("publish", f"{type(exc).__name__}: {exc}", role) from exc
            manifest = mark_published(manifest, published.task_files, self.cfg.storage_settings().root)
        return PreparedEnvironment(manifest)


def _discover(roots: Sequence[Path]) -> list[_Source]:
    sources: dict[str, _Source] = {}
    for given in roots:
        root = given.resolve()
        base = root.parent if (root / "task.toml").is_file() else root
        for task_dir in find_tasks([root]):
            ident = f"{base.name}/{task_dir.relative_to(base).as_posix()}"
            if ident in sources and sources[ident].task_dir != task_dir:
                raise ValueError(f"two task directories map to id {ident}: {sources[ident].task_dir}, {task_dir}")
            sources[ident] = _Source(ident, task_dir, base)
    repositories: dict[str, str] = {}
    for source in sources.values():
        dataset = source.id.partition("/")[0]
        if repositories.setdefault(source.repository, dataset) != dataset:
            raise ValueError(
                f"datasets {repositories[source.repository]} and {dataset} map to the same image repository"
            )
    return sorted(sources.values(), key=lambda s: s.id)


def _dataset_rev(root: Path) -> str | None:
    """HEAD of the dataset's own git repository (`-dirty` with local changes); None if it is not a repo root."""

    def git(*args: str) -> str | None:
        try:
            result = subprocess.run(["git", "-C", str(root), *args], capture_output=True, text=True, timeout=60)
        except (OSError, subprocess.TimeoutExpired):
            return None
        return result.stdout.strip() if result.returncode == 0 else None

    top = git("rev-parse", "--show-toplevel")
    if top is None or Path(top).resolve() != root:
        return None
    head = git("rev-parse", "HEAD")
    if head is None:
        return None
    return head + ("-dirty" if git("status", "--porcelain") else "")


def _attrs(cfg: FlotillaConfig, environments: dict[str, PreparedEnvironment]) -> dict[str, Any]:
    """Filter attributes of the main environment, at the deployment's effective resources."""
    manifest = environments["main"].manifest()
    resources = cfg.resources.for_manifest(manifest).values()
    return {
        "services": len(manifest.units),
        "cpu": float(sum((Decimal(r.cpu) for r in resources), Decimal())),
        "memory_mib": math.ceil(sum(memory_bytes(r.memory) for r in resources) / (1 << 20)),
        "privileged": any(unit.privileged for unit in manifest.units.values()),
        "has_verifier": "verifier" in environments,
    }


def _append(handle: TextIO, line: str) -> None:
    handle.write(line + "\n")
    handle.flush()
    os.fsync(handle.fileno())


def _compact(out: Path, *, drop: set[str]) -> None:
    rows = sorted((row for row in load_prepared(out) if row.id not in drop), key=lambda row: row.id)
    temporary = out.with_name(f".{out.name}.tmp")
    with temporary.open("w", encoding="utf-8") as handle:
        for row in rows:
            handle.write(dump_row(row) + "\n")
        handle.flush()
        os.fsync(handle.fileno())
    os.replace(temporary, out)
