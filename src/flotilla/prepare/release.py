"""Host selection and atomic native release preparation; no remote workspace API."""

from __future__ import annotations

import json
import shutil
import threading
import time
import uuid
from collections.abc import Callable, Mapping, Sequence
from concurrent.futures import Future, ThreadPoolExecutor
from dataclasses import dataclass, field, replace
from pathlib import Path
from tempfile import mkdtemp
from typing import Any, TypeVar

from flotilla.build.driver import BuildContext, build_task
from flotilla.build.images import _DIGEST, pin_images, pinned_images, plan_images
from flotilla.build.meta import ImageAccounts
from flotilla.compose.task import TaskError
from flotilla.integrations.native_import import ImportedTask, SelectedTask, UnsupportedTask, discover, import_task
from flotilla.manifest import Manifest
from flotilla.platform.base import Capabilities

_T = TypeVar("_T")


@dataclass(frozen=True)
class SelectionResult:
    rows: tuple[SelectedTask, ...]
    report: tuple[dict[str, Any], ...]


@dataclass(frozen=True)
class PrepareResult:
    output: Path
    name: str
    report: tuple[dict[str, Any], ...]
    images: tuple[str, ...]
    metrics: dict[str, Any] = field(default_factory=dict)
    prepared: int = 0  # selected rows in the release
    failed: int = 0  # selected rows left out; their reasons are in `report`


class _Metrics:
    """Invocation-local observations; nested phase times must not be summed.

    Calls count attempts (including failures), not distinct images or Docker
    containers. A builder call includes build and push: that interface does not
    expose separate timings. Cache hits never count as underlying tool calls.
    Tasks run concurrently, so phase seconds of different tasks overlap.
    """

    def __init__(self) -> None:
        self.started = time.monotonic()
        self._lock = threading.Lock()
        self.phases = {
            name: {"calls": 0, "failed": 0, "seconds": 0.0}
            for name in (
                "import_normalize",
                "registry_digest",
                "image_config",
                "image_accounts",
                "role_prepare",
                "package",
            )
        }
        self.counts = {
            "registry_cache_hits": 0,
            "stored_hits": 0,
        }

    def call(self, phase: str, function: Callable[..., _T], *args: Any, **kwargs: Any) -> _T:
        started, failed = time.monotonic(), False
        try:
            return function(*args, **kwargs)
        except BaseException:
            failed = True
            raise
        finally:
            elapsed = time.monotonic() - started
            with self._lock:
                observation = self.phases[phase]
                observation["calls"] += 1
                observation["failed"] += failed
                observation["seconds"] += elapsed

    def count(self, name: str) -> None:
        with self._lock:
            self.counts[name] += 1

    def snapshot(self) -> dict[str, Any]:
        with self._lock:
            return {
                "elapsed_s": time.monotonic() - self.started,
                "phases": {name: dict(values) for name, values in self.phases.items()},
                "counts": dict(self.counts),
            }


class _Once[K, V]:
    """Per-key results computed once even when threads ask concurrently; a failure is not kept."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._keys: dict[K, threading.Lock] = {}
        self._values: dict[K, V] = {}

    def get(self, key: K, compute: Callable[[], V]) -> tuple[V, bool]:
        """Return `(value, hit)`; `hit` is false only for the call that computed the value."""
        with self._lock:
            if key in self._values:
                return self._values[key], True
            lock = self._keys.setdefault(key, threading.Lock())
        with lock:
            with self._lock:
                if key in self._values:
                    return self._values[key], True
            value = compute()
            self.put(key, value)
            return value, False

    def put(self, key: K, value: V) -> None:
        with self._lock:
            self._values[key] = value

    def forget(self, key: K) -> None:
        with self._lock:
            self._values.pop(key, None)


def _digest_of(image: str) -> str | None:
    """The content digest of a digest-pinned reference; tags and unpinned fixtures have none."""
    digest = image.rpartition("@")[2]
    return digest if _DIGEST.fullmatch(digest) else None


def _store_path(store: Path | None, kind: str, image: str) -> Path | None:
    digest = _digest_of(image)
    if store is None or digest is None:
        return None
    return store / kind / f"{digest.replace(':', '-')}.json"


def _store_write(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{uuid.uuid4().hex}")
    temporary.write_text(_json(value) + "\n", encoding="utf-8")
    temporary.replace(path)


class _Accounts:
    """Image account databases by image, kept in the store by digest: they never change for one digest."""

    def __init__(self, read: Callable[[str], ImageAccounts], metrics: _Metrics, store: Path | None) -> None:
        self.read, self.metrics, self.store = read, metrics, store
        self._accounts: _Once[str, ImageAccounts] = _Once()

    def __call__(self, image: str) -> ImageAccounts:
        def load() -> ImageAccounts:
            path = _store_path(self.store, "accounts", image)
            if path is not None and path.is_file():
                self.metrics.count("stored_hits")
                return ImageAccounts(**json.loads(path.read_text(encoding="utf-8")))
            accounts = self.metrics.call("image_accounts", self.read, image)
            if path is not None:
                _store_write(path, {"passwd": accounts.passwd, "groups": accounts.groups})
            return accounts

        return self._accounts.get(image, load)[0]


class PreparationError(ValueError):
    def __init__(self, message: str, report: Sequence[Mapping[str, Any]]) -> None:
        super().__init__(message)
        self.report = tuple(report)


def _json(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False, default=str)


def _write(path: Path | str | None, value: Any) -> None:
    if path is None:
        return
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + ".tmp")
    temporary.write_text(_json(value) + "\n", encoding="utf-8")
    temporary.replace(path)


def select(
    host_root: str | Path,
    *,
    caps: Capabilities,
    out: str | Path | None = None,
    report: str | Path | None = None,
    runtime_params: Mapping[str, str] | None = None,
) -> SelectionResult:
    """Select supported host tasks without Docker image access or platform credentials.

    Parameters are non-secret Compose compile inputs. Unsupported tasks are
    reported and filtered; incomplete traversal, parser/tool failures and an empty
    selection fail the operation rather than publishing a misleading selection.
    """
    root = Path(host_root).expanduser().resolve(strict=True)
    rows, reports = [], []
    paths = discover(root)
    for path in paths:
        key = path.relative_to(root).as_posix()
        row = SelectedTask(
            task_path=path, task_key=key if key != "." else root.name, parameters=dict(runtime_params or {})
        )
        try:
            imported = import_task(row, caps=caps)
            rows.append(row)
            reports.append(
                {
                    "task_key": row.task_key,
                    "status": "selected",
                    "stage": "select",
                    "code": "supported",
                    "roles": list(imported.roles),
                    "warnings": list(imported.warnings),
                    "validation": "metadata/official-compose; images not inspected",
                }
            )
        except UnsupportedTask as exc:
            reports.append(
                {
                    "task_key": row.task_key,
                    "status": "rejected",
                    "stage": "select",
                    "code": "unsupported",
                    "reason": str(exc),
                }
            )
        except TaskError as exc:
            # The task's own files are invalid (for example Compose itself refuses them); tool failures are not.
            reports.append(
                {
                    "task_key": row.task_key,
                    "status": "rejected",
                    "stage": "select",
                    "code": "invalid_task",
                    "reason": str(exc),
                }
            )
        except (OSError, ValueError, RuntimeError) as exc:
            reports.append(
                {
                    "task_key": row.task_key,
                    "status": "failed",
                    "stage": "select",
                    "code": "input_or_tool_error",
                    "reason": str(exc),
                }
            )
    _write(report, reports)
    if not rows or any(row["status"] == "failed" for row in reports):
        raise PreparationError("selection failed or selected zero tasks; inspect the selection report", reports)
    if out is not None:
        destination = Path(out)
        destination.parent.mkdir(parents=True, exist_ok=True)
        with destination.open("x", encoding="utf-8") as stream:
            for row in rows:
                stream.write(row.model_dump_json() + "\n")
    return SelectionResult(tuple(rows), tuple(reports))


def _rows(selected: str | Path | Sequence[SelectedTask | Mapping[str, Any]]) -> list[SelectedTask]:
    if isinstance(selected, (str, Path)):
        source = Path(selected)
        values = [json.loads(line) for line in source.read_text().splitlines() if line.strip()]
        rows = [SelectedTask.model_validate(value) for value in values]
        return [
            row.model_copy(update={"task_path": (source.parent / row.task_path).resolve()})
            if not row.task_path.is_absolute()
            else row
            for row in rows
        ]
    return [SelectedTask.model_validate(row) for row in selected]


class _RegistryCache:
    """Registry reads shared by concurrent tasks; image configs are also kept in the store by digest."""

    def __init__(self, registry: Any, metrics: _Metrics, store: Path | None = None) -> None:
        self.registry, self.metrics, self.store = registry, metrics, store
        self.digests: _Once[tuple[str, str], str] = _Once()
        self.configs: _Once[str, Any] = _Once()

    def digest(self, name: str, reference: str) -> str:
        value, hit = self.digests.get(
            (name, reference), lambda: self.metrics.call("registry_digest", self.registry.digest, name, reference)
        )
        if hit:
            self.metrics.count("registry_cache_hits")
        return value

    def image_config(self, pinned: str) -> Any:
        def load() -> Any:
            path = _store_path(self.store, "configs", pinned)
            if path is not None and path.is_file():
                self.metrics.count("stored_hits")
                return json.loads(path.read_text(encoding="utf-8"))
            config = self.metrics.call("image_config", self.registry.image_config, pinned)
            if path is not None:
                _store_write(path, config)
            return config

        value, hit = self.configs.get(pinned, load)
        if hit:
            self.metrics.count("registry_cache_hits")
        return value


def prepare(
    selected: str | Path | Sequence[SelectedTask | Mapping[str, Any]],
    *,
    out: str | Path,
    name: str,
    deployment: Any = None,
    report: str | Path | None = None,
    context: BuildContext | None = None,
    cache: str | Path | None = None,
    concurrency: int | None = None,
) -> PrepareResult:
    """Import selected rows into one native release named `name`.

    A row that fails is reported and left out; the release holds every row that
    prepared, and nothing is published only when no row did. Preparation reads
    published images; it never builds or pushes one, uploads files, creates
    remote sandboxes, or requires Anchor. Work files live in a sibling directory
    that is removed afterwards.

    `cache` keeps what depends only on an image digest (image configs and account
    databases) across invocations, so a later run does not read an image again.
    `concurrency` tasks are prepared at once (deployment `prepare.concurrency` by
    default, 1 with an explicit `context`); packaging into the release stays in
    row order.

    Phase times are inclusive and may overlap across concurrent tasks. Tool call
    counts exclude cache hits.
    """
    metrics = _Metrics()
    output = Path(out).expanduser().absolute()
    if output.exists():
        raise FileExistsError(f"release output already exists: {output}")
    if report is not None and Path(report).absolute().is_relative_to(output):
        raise ValueError("prepare report must be outside the release")
    store = Path(cache).expanduser().absolute() if cache is not None else None
    if store is not None and store.is_relative_to(output):
        raise ValueError("prepare cache must be outside the release")
    output.parent.mkdir(parents=True, exist_ok=True)
    work = Path(mkdtemp(prefix=f".{output.name}.prepare-", dir=output.parent))
    try:
        return _prepare(
            _rows(selected),
            output=output,
            name=name,
            work=work,
            store=store,
            concurrency=concurrency,
            deployment=deployment,
            report=report,
            context=context,
            metrics=metrics,
        )
    finally:
        shutil.rmtree(work, ignore_errors=True)


class _TaskFailure(Exception):
    def __init__(self, stage: str, code: str, reason: str) -> None:
        super().__init__(reason)
        self.stage, self.code, self.reason = stage, code, reason


@dataclass(frozen=True)
class _Prepared:
    """One task built for every role; manifests still reference files relative to their build directory."""

    imported: ImportedTask
    roles: dict[str, Manifest]
    publishes: dict[str, bool]
    files: dict[str, Path]
    sources: dict[str, dict[str, str]]


def _prepare_task(
    row: SelectedTask,
    *,
    context: BuildContext,
    work: Path,
    published: set[str],
    metrics: _Metrics,
) -> _Prepared:
    settings, registry = context.settings, context.registry
    try:
        imported = metrics.call("import_normalize", import_task, row, caps=context.caps)
    except (OSError, ValueError, RuntimeError) as exc:
        raise _TaskFailure("import", "input_or_image_error", str(exc)) from exc
    try:
        roles: dict[str, Manifest] = {}
        publishes: dict[str, bool] = {}
        files: dict[str, Path] = {}
        sources: dict[str, dict[str, str]] = {}
        for role, task in imported.roles.items():
            sources[role] = pinned_images(pin_images(plan_images(task.project, settings), registry))
            outcome = metrics.call("role_prepare", build_task, task, work / "builds", context)
            if outcome.status != "built" or outcome.manifest_path is None:
                raise ValueError(f"{role}: {outcome.reason}")
            manifest = Manifest.load_json(outcome.manifest_path.read_text())
            published.update(unit.image for unit in manifest.units.values())
            if outcome.needs_publish:
                # Roles export identical task files: the task directory holds one copy.
                files.setdefault("files", outcome.manifest_path.parent / "files")
            roles[role], publishes[role] = manifest, outcome.needs_publish
        if imported.tests is not None:
            if not (imported.tests / "test.sh").is_file():
                raise ValueError("Harbor tests directory has no test.sh")
            files["tests"] = imported.tests
    except (OSError, ValueError, RuntimeError) as exc:
        raise _TaskFailure("prepare", "prepare_error", str(exc)) from exc
    return _Prepared(imported, roles, publishes, files, sources)


def _native_task(key: str, prepared: _Prepared, directory: str) -> Any:
    from flotilla.native import Artifact, Task, Verification

    imported = prepared.imported
    roles = {
        role: _file_paths(manifest, f"{directory}/files") if prepared.publishes[role] else manifest
        for role, manifest in prepared.roles.items()
    }
    verification = None
    artifacts: tuple[Artifact, ...] = ()
    if imported.tests is not None:
        verification = Verification(
            role="verifier" if "verifier" in roles else "main",
            command=("bash", "/tests/test.sh"),
            tests=f"{directory}/tests",
            optional_output_paths={
                "reward.txt": "/logs/verifier/reward.txt",
                "reward.json": "/logs/verifier/reward.json",
            },
            require_output=True,
            env=imported.config.verifier.env,
            timeout_s=imported.config.verifier.timeout_sec,
        )
        artifacts = (Artifact(name="agent", role="main", target="/logs/agent"),)
    # `harbor.config` carries the original task configuration for
    # Harbor presentation; the generic core never reads it back.
    provenance = {
        "importer": "harbor-v1",
        "harbor": {"config": imported.config.model_dump(mode="json")},
        "source_images": prepared.sources,
        "parameters": imported.selection.parameters,
    }
    return Task(key=key, roles=roles, verification=verification, artifacts=artifacts, provenance=provenance)


def _prepare(
    rows: list[SelectedTask],
    *,
    output: Path,
    name: str,
    work: Path,
    store: Path | None,
    concurrency: int | None,
    deployment: Any,
    report: str | Path | None,
    context: BuildContext | None,
    metrics: _Metrics,
) -> PrepareResult:
    from flotilla.native import ReleaseWriter

    if context is None:
        from flotilla.config import FlotillaConfig
        from flotilla.prepare.docker import docker_context

        if deployment is None:
            raise ValueError("prepare requires deployment or an explicit BuildContext")
        cfg = deployment if isinstance(deployment, FlotillaConfig) else FlotillaConfig.load(deployment)
        context = docker_context(cfg, work / "containers")
        concurrency = concurrency or cfg.prepare.concurrency
    workers = concurrency or 1
    if workers < 1:
        raise ValueError("prepare concurrency must be at least 1")
    registry = _RegistryCache(context.registry, metrics, store)
    context = replace(
        context,
        registry=registry,
        image_accounts=_Accounts(context.image_accounts, metrics, store)
        if context.image_accounts is not None
        else None,
    )
    if not rows:
        raise PreparationError("cannot prepare an empty selection", [])
    reports = [
        {"row": i, "task_key": row.task_key, "status": "pending", "stage": "import", "code": "not_started"}
        for i, row in enumerate(rows)
    ]
    # A task selected several times is imported and packaged once; its rows repeat.
    sources: dict[str, Path] = {}
    for i, row in enumerate(rows):
        previous = sources.setdefault(row.task_key, row.task_path)
        if previous != row.task_path:
            reports[i].update(
                status="failed",
                code="input_or_image_error",
                reason=f"task key {row.task_key!r} names two source tasks: {previous}, {row.task_path}",
            )
    published: set[str] = set()
    dataset: list[dict[str, Any]] = []
    try:
        with (
            ReleaseWriter(output, name) as writer,
            ThreadPoolExecutor(max_workers=workers, thread_name_prefix="flotilla-prepare") as pool,
        ):
            futures: dict[str, Future[_Prepared]] = {}
            for i, row in enumerate(rows):
                if reports[i]["status"] == "pending" and row.task_key not in futures:
                    futures[row.task_key] = pool.submit(
                        _prepare_task,
                        row,
                        context=context,
                        work=work,
                        published=published,
                        metrics=metrics,
                    )
            packaged: dict[str, _Prepared] = {}
            failures: dict[str, _TaskFailure] = {}
            try:
                for i, row in enumerate(rows):
                    if reports[i]["status"] != "pending":
                        continue
                    key = row.task_key
                    if key not in packaged and key not in failures:
                        try:
                            prepared = futures[key].result()
                            try:
                                directory = writer.directory(key)
                                metrics.call(
                                    "package",
                                    writer.add_task,
                                    _native_task(key, prepared, directory),
                                    prepared.imported.instruction,
                                    prepared.files,
                                )
                            except (OSError, ValueError, RuntimeError) as exc:
                                shutil.rmtree(writer.staging / writer.directory(key), ignore_errors=True)
                                raise _TaskFailure("package", "prepare_error", str(exc)) from exc
                            packaged[key] = prepared
                        except _TaskFailure as caught:
                            failures[key] = caught
                    if key in failures:
                        failure = failures[key]
                        reports[i].update(
                            status="failed", stage=failure.stage, code=failure.code, reason=failure.reason
                        )
                        continue
                    dataset.append({"task": key, "annotations": row.annotations, "parameters": row.parameters})
                    reports[i].update(status="prepared", stage="package", code="complete", images=packaged[key].sources)
            finally:
                for future in futures.values():
                    future.cancel()
            if not dataset:
                raise PreparationError("no selected row prepared; no release published", reports)
            metrics.call("package", writer.publish, dataset)
    finally:
        measurements = metrics.snapshot()
        images = sorted(published)
        _write(report, {"rows": reports, "images": images, "metrics": measurements})
    return PrepareResult(
        output,
        name,
        tuple(reports),
        tuple(images),
        measurements,
        prepared=len(dataset),
        failed=len(rows) - len(dataset),
    )


def _file_paths(manifest: Manifest, prefix: str) -> Manifest:
    """Rebase the build's `files/`-relative references onto the task directory in the release."""
    units = {
        name: unit.model_copy(
            update={
                "mounts": [
                    mount.model_copy(update={"key": f"{prefix}/{mount.key}"}) if mount.scope == "task" else mount
                    for mount in unit.mounts
                ]
            }
        )
        for name, unit in manifest.units.items()
    }
    volumes = [
        volume.model_copy(update={"copy_from": f"{prefix}/{volume.copy_from}" if volume.copy_from else None})
        for volume in manifest.trial_volumes
    ]
    return manifest.model_copy(update={"units": units, "trial_volumes": volumes, "task_files": None})
