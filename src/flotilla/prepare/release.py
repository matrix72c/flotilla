"""Host selection and atomic native release preparation; no remote workspace API."""

from __future__ import annotations

import hashlib
import json
import shutil
import threading
import time
import uuid
from collections.abc import Callable, Mapping, Sequence
from concurrent.futures import Future, ThreadPoolExecutor
from dataclasses import dataclass, field, replace
from functools import partial
from pathlib import Path
from tempfile import mkdtemp
from typing import Any, TypeVar

from flotilla.build.builder import Builder, BuildRequest
from flotilla.build.dockerfile import config_only
from flotilla.build.driver import BuildContext, build_task, preflight_images
from flotilla.build.images import _DIGEST, ImageRef, pin_images, plan_images
from flotilla.build.meta import ImageAccounts
from flotilla.compose.adapter import ComposeConfigAdapter
from flotilla.compose.task import HarborTask, TaskError
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
                "image_preflight",
                "registry_digest",
                "registry_lookup",
                "image_config",
                "image_accounts",
                "registry_copy",
                "user0_build_push",
                "application_build_push",
                "image_extract",
                "seed_cache_copy",
                "role_prepare",
                "package",
            )
        }
        self.counts = {
            "registry_cache_hits": 0,
            "seed_cache_hits": 0,
            "build_cache_hits": 0,
            "config_only": 0,
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


class _MeasuredBuilder:
    """Times builds and builds each tag once: concurrent tasks may ask for the same adaptation or cache tag."""

    def __init__(self, builder: Builder, metrics: _Metrics) -> None:
        self.builder, self.metrics = builder, metrics
        self._built: _Once[str, str] = _Once()

    def build(self, request: BuildRequest) -> str:
        # The driver supplies inline Dockerfile text only for its USER0 adapter;
        # source application builds refer to their on-disk Dockerfile instead.
        phase = "user0_build_push" if request.dockerfile_text is not None else "application_build_push"
        digest, _ = self._built.get(request.tag, lambda: self.metrics.call(phase, self.builder.build, request))
        return digest


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
    compose_adapter: ComposeConfigAdapter | None = None,
    require_prebuilt_images: bool = False,
    runtime_params: Mapping[str, str] | None = None,
) -> SelectionResult:
    """Select supported host tasks without Docker image access or platform credentials.

    Parameters are non-secret Compose compile inputs. Unsupported tasks are
    reported and filtered; incomplete traversal, parser/tool failures and an empty
    selection fail the operation rather than publishing a misleading selection.
    """
    root = Path(host_root).expanduser().resolve(strict=True)
    adapter = compose_adapter or ComposeConfigAdapter()
    rows, reports = [], []
    paths = discover(root)
    for path in paths:
        key = path.relative_to(root).as_posix()
        row = SelectedTask(
            task_path=path, task_key=key if key != "." else root.name, parameters=dict(runtime_params or {})
        )
        try:
            imported = import_task(
                row,
                caps=caps,
                compose_adapter=adapter,
                require_prebuilt_images=require_prebuilt_images,
            )
            rows.append(row)
            reports.append(
                {
                    "task_key": row.task_key,
                    "status": "selected",
                    "stage": "select",
                    "code": "supported",
                    "roles": list(imported.roles),
                    "images": {role: _image_sources(task) for role, task in imported.roles.items()},
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


def _image_sources(task: HarborTask) -> dict[str, str]:
    """Per service: `image`, `build`, or `config-only` (a build that may reuse its base digest)."""
    out: dict[str, str] = {}
    for name, svc in sorted(task.project.services.items()):
        if svc.build is None:
            out[name] = "image"
            continue
        dockerfile = task.env_dir / svc.build.context / (svc.build.dockerfile or "Dockerfile")
        out[name] = "config-only" if config_only(dockerfile.read_text()) else "build"
    return out


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
        self.copies: _Once[tuple[str, str], None] = _Once()

    def digest(self, name: str, reference: str) -> str:
        value, hit = self.digests.get(
            (name, reference), lambda: self.metrics.call("registry_digest", self.registry.digest, name, reference)
        )
        if hit:
            self.metrics.count("registry_cache_hits")
        return value

    def find(self, name: str, reference: str) -> str | None:
        found: str | None = self.metrics.call("registry_lookup", self.registry.find, name, reference)
        if found is not None:
            self.digests.put((name, reference), found)
        return found

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

    def copy(self, source: str, target: str) -> None:
        def run() -> None:
            self.metrics.call("registry_copy", self.registry.copy, source, target)
            name, _, tag = target.rpartition(":")
            self.digests.forget((name, tag))

        if self.copies.get((source, target), run)[1]:
            self.metrics.count("registry_cache_hits")


class _Exports:
    """Volume seed extraction by (image, path) under `root`.

    Each entry is a content directory plus a `.json` record written after it, so
    an interrupted extraction is never reused. Entries of digest-pinned images
    are reused from `root` across invocations when `root` is the store.
    """

    def __init__(self, exporter: Any, root: Path, metrics: _Metrics) -> None:
        self.exporter, self.root, self.metrics = exporter, root, metrics
        self.cached: _Once[tuple[str, str], tuple[Path, bool, dict[str, Any]]] = _Once()

    def _load(self, image: str, path: str) -> tuple[Path, bool, dict[str, Any]]:
        name = hashlib.sha256(_json((image.rpartition("@")[2], path)).encode()).hexdigest()
        directory, record = self.root / name, self.root / f"{name}.json"
        if _digest_of(image) is not None and record.is_file() and directory.is_dir():
            self.metrics.count("stored_hits")
            value = json.loads(record.read_text(encoding="utf-8"))
            return directory, value["populated"], {k: tuple(v) for k, v in value["owners"].items()}
        self.root.mkdir(parents=True, exist_ok=True)
        # A failed extraction records nothing, so a later task with the same image and
        # path retries it from a clean directory instead of the partial attempt.
        staged = Path(mkdtemp(prefix=f".{name}.", dir=self.root))
        try:
            owners: dict[str, Any] = {}
            populated = self.metrics.call(
                "image_extract", self.exporter.extract, image, path, staged, identities=owners
            )
            record.unlink(missing_ok=True)
            if directory.exists():
                shutil.rmtree(directory)
            staged.replace(directory)
        except BaseException:
            shutil.rmtree(staged, ignore_errors=True)
            raise
        _store_write(record, {"populated": populated, "owners": owners})
        return directory, populated, owners

    def extract(self, image: str, path: str, dest: Path, *, identities: dict[str, Any] | None = None) -> bool:
        (directory, populated, owners), hit = self.cached.get(
            (image.rpartition("@")[2], path), lambda: self._load(image, path)
        )
        if hit:
            self.metrics.count("seed_cache_hits")
        # symlinks=True: a seed's links must stay links, not become copies of their targets.
        self.metrics.call(
            "seed_cache_copy", partial(shutil.copytree, symlinks=True), directory, dest, dirs_exist_ok=True
        )
        if identities is not None:
            identities.update(owners)
        return populated


def _repo(value: str) -> str:
    ref = ImageRef.parse(value)
    if ref.tag is not None or ref.digest is not None or ref.name != value or any(c.isspace() for c in value):
        raise ValueError("image_repo must be a full registry/repository without tag or digest")
    return value


def prepare(
    selected: str | Path | Sequence[SelectedTask | Mapping[str, Any]],
    *,
    out: str | Path,
    name: str,
    deployment: Any = None,
    image_repo: str | None = None,
    report: str | Path | None = None,
    require_prebuilt_images: bool = False,
    context: BuildContext | None = None,
    compose_adapter: ComposeConfigAdapter | None = None,
    cache: str | Path | None = None,
    concurrency: int | None = None,
) -> PrepareResult:
    """Import selected rows into one native release named `name`.

    A row that fails is reported and left out; the release holds every row that
    prepared, and nothing is published only when no row did. Building/pushing
    necessary images is authorized by invoking this operation. It never uploads
    files, creates remote sandboxes, or requires Anchor. Work files live in a
    sibling directory that is removed afterwards.

    `cache` keeps what depends only on an image digest (image configs, account
    databases, volume seeds) across invocations, so a later run does not pull an
    image again. `concurrency` tasks are prepared at once (deployment
    `prepare.concurrency` by default, 1 with an explicit `context`); packaging
    into the release stays in row order.

    Phase times are inclusive and may overlap (role_prepare includes image
    extraction, and concurrent tasks overlap). Tool call counts exclude cache
    hits; build and push share one backend call/timer.
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
            image_repo=image_repo,
            report=report,
            require_prebuilt_images=require_prebuilt_images,
            context=context,
            compose_adapter=compose_adapter,
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
    images: dict[str, dict[str, Any]]


def _prepare_task(
    row: SelectedTask,
    *,
    context: BuildContext,
    adapter: ComposeConfigAdapter,
    require_prebuilt_images: bool,
    work: Path,
    published: set[str],
    metrics: _Metrics,
) -> _Prepared:
    settings, registry = context.settings, context.registry
    try:
        imported = metrics.call(
            "import_normalize",
            import_task,
            row,
            caps=context.caps,
            compose_adapter=adapter,
            require_prebuilt_images=require_prebuilt_images,
        )
        # Preflight every role of the task before its first image publication.
        for task in imported.roles.values():
            metrics.call(
                "image_preflight", preflight_images, pin_images(plan_images(task.project, settings), registry), context
            )
    except (OSError, ValueError, RuntimeError) as exc:
        raise _TaskFailure("import", "input_or_image_error", str(exc)) from exc
    try:
        roles: dict[str, Manifest] = {}
        publishes: dict[str, bool] = {}
        files: dict[str, Path] = {}
        sources: dict[str, dict[str, str]] = {}
        images: dict[str, dict[str, Any]] = {}
        for role, task in imported.roles.items():
            sources[role] = {
                p.service: p.ref.pinned(p.ref.digest or "")
                for p in pin_images(plan_images(task.project, settings), registry)
                if p.ref
            }
            outcome = metrics.call("role_prepare", build_task, task, work / "builds", context)
            if outcome.status != "built" or outcome.manifest_path is None:
                raise ValueError(f"{role}: {outcome.reason}")
            images[role] = {"dispositions": dict(outcome.dispositions), "adapted": list(outcome.adapted)}
            for disposition in outcome.dispositions.values():
                if disposition == "cache-hit":
                    metrics.count("build_cache_hits")
                elif disposition == "config-only":
                    metrics.count("config_only")
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
    return _Prepared(imported, roles, publishes, files, sources, images)


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
        "adaptation": "user0-v1",
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
    image_repo: str | None,
    report: str | Path | None,
    require_prebuilt_images: bool,
    context: BuildContext | None,
    compose_adapter: ComposeConfigAdapter | None,
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
        compose_adapter = compose_adapter or ComposeConfigAdapter(
            command=cfg.prepare.compose_command, expected_version=cfg.prepare.compose_version
        )
        concurrency = concurrency or cfg.prepare.concurrency
    workers = concurrency or 1
    if workers < 1:
        raise ValueError("prepare concurrency must be at least 1")
    settings = replace(context.settings, target=_repo(image_repo)) if image_repo is not None else context.settings
    if settings.target:
        _repo(settings.target)
    registry = _RegistryCache(context.registry, metrics, store)
    context = replace(
        context,
        settings=settings,
        registry=registry,
        adaptations={},
        builder=_MeasuredBuilder(context.builder, metrics) if context.builder is not None else None,
        image_accounts=_Accounts(context.image_accounts, metrics, store)
        if context.image_accounts is not None
        else None,
    )
    if context.image_export is not None:
        seeds = store / "seeds" if store is not None else work / "seeds"
        context = replace(context, image_export=_Exports(context.image_export, seeds, metrics))
    adapter = compose_adapter or ComposeConfigAdapter()
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
                        adapter=adapter,
                        require_prebuilt_images=require_prebuilt_images,
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
                    reports[i].update(status="prepared", stage="package", code="complete", images=packaged[key].images)
            finally:
                for future in futures.values():
                    future.cancel()
            if not dataset:
                raise PreparationError("no selected row prepared; no release published", reports)
            metrics.call("package", writer.publish, dataset)
    finally:
        measurements = metrics.snapshot()
        images = sorted(published | set(context.adaptations.values()))
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
        volume.model_copy(
            update={
                "seed": f"{prefix}/{volume.seed}" if volume.seed else None,
                "copy_from": f"{prefix}/{volume.copy_from}" if volume.copy_from else None,
            }
        )
        for volume in manifest.trial_volumes
    ]
    return manifest.model_copy(update={"units": units, "trial_volumes": volumes, "task_files": None})
