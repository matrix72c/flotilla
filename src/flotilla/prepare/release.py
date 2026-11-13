"""Host selection and atomic native release preparation; no remote workspace API."""

from __future__ import annotations

import hashlib
import json
import shutil
import time
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field, replace
from functools import partial
from pathlib import Path
from tempfile import mkdtemp
from typing import Any, TypeVar

from flotilla.build.builder import Builder, BuildRequest
from flotilla.build.dockerfile import config_only
from flotilla.build.driver import BuildContext, build_task, preflight_images
from flotilla.build.images import ImageRef, pin_images, plan_images
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


class _Metrics:
    """Invocation-local observations; nested phase times must not be summed.

    Calls count attempts (including failures), not distinct images or Docker
    containers. A builder call includes build and push: that interface does not
    expose separate timings. Cache hits never count as underlying tool calls.
    """

    def __init__(self) -> None:
        self.started = time.monotonic()
        self.phases = {
            name: {"calls": 0, "failed": 0, "seconds": 0.0}
            for name in (
                "import_normalize",
                "image_preflight",
                "registry_digest",
                "registry_lookup",
                "image_config",
                "image_accounts",
                "mount_path_inspection",
                "registry_copy",
                "user0_build_push",
                "application_build_push",
                "image_extract",
                "seed_cache_copy",
                "role_prepare",
                "package",
            )
        }
        self.counts = {"registry_cache_hits": 0, "seed_cache_hits": 0, "build_cache_hits": 0, "config_only": 0}

    def call(self, phase: str, function: Callable[..., _T], *args: Any, **kwargs: Any) -> _T:
        observation = self.phases[phase]
        observation["calls"] += 1
        started = time.monotonic()
        try:
            return function(*args, **kwargs)
        except BaseException:
            observation["failed"] += 1
            raise
        finally:
            observation["seconds"] += time.monotonic() - started

    def snapshot(self) -> dict[str, Any]:
        return {
            "elapsed_s": time.monotonic() - self.started,
            "phases": {name: dict(values) for name, values in self.phases.items()},
            "counts": dict(self.counts),
        }


class _MeasuredBuilder:
    def __init__(self, builder: Builder, metrics: _Metrics) -> None:
        self.builder, self.metrics = builder, metrics

    def build(self, request: BuildRequest) -> str:
        # The driver supplies inline Dockerfile text only for its USER0 adapter;
        # source application builds refer to their on-disk Dockerfile instead.
        phase = "user0_build_push" if request.dockerfile_text is not None else "application_build_push"
        return self.metrics.call(phase, self.builder.build, request)


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
    def __init__(self, registry: Any, metrics: _Metrics) -> None:
        self.registry, self.metrics = registry, metrics
        self.digests: dict[tuple[str, str], str] = {}
        self.configs: dict[str, Any] = {}
        self.copies: set[tuple[str, str]] = set()

    def digest(self, name: str, reference: str) -> str:
        key = (name, reference)
        if key not in self.digests:
            self.digests[key] = self.metrics.call("registry_digest", self.registry.digest, name, reference)
        else:
            self.metrics.counts["registry_cache_hits"] += 1
        return self.digests[key]

    def find(self, name: str, reference: str) -> str | None:
        found: str | None = self.metrics.call("registry_lookup", self.registry.find, name, reference)
        if found is not None:
            self.digests[(name, reference)] = found
        return found

    def image_config(self, pinned: str) -> Any:
        if pinned not in self.configs:
            self.configs[pinned] = self.metrics.call("image_config", self.registry.image_config, pinned)
        else:
            self.metrics.counts["registry_cache_hits"] += 1
        return self.configs[pinned]

    def copy(self, source: str, target: str) -> None:
        if (source, target) not in self.copies:
            self.metrics.call("registry_copy", self.registry.copy, source, target)
            self.copies.add((source, target))
            name, _, tag = target.rpartition(":")
            self.digests.pop((name, tag), None)
        else:
            self.metrics.counts["registry_cache_hits"] += 1


class _Exports:
    def __init__(self, exporter: Any, root: Path, metrics: _Metrics) -> None:
        self.exporter, self.root, self.metrics = exporter, root, metrics
        self.cached: dict[tuple[str, str], tuple[Path, bool, dict[str, Any]]] = {}

    def extract(self, image: str, path: str, dest: Path, *, identities: dict[str, Any] | None = None) -> bool:
        key = (image.rpartition("@")[2], path)
        if key not in self.cached:
            directory = self.root / hashlib.sha256(_json(key).encode()).hexdigest()
            directory.mkdir(parents=True)
            owners: dict[str, Any] = {}
            populated = self.metrics.call(
                "image_extract", self.exporter.extract, image, path, directory, identities=owners
            )
            self.cached[key] = directory, populated, owners
        else:
            self.metrics.counts["seed_cache_hits"] += 1
        directory, populated, owners = self.cached[key]
        self.metrics.call("seed_cache_copy", shutil.copytree, directory, dest, dirs_exist_ok=True)
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
) -> PrepareResult:
    """Import selected rows into one native release named `name`, or publish nothing.

    Building/pushing necessary images is authorized by invoking this operation.
    It never uploads files, creates remote sandboxes, or requires Anchor. Work
    files live in a sibling directory that is removed afterwards.

    Phase times are inclusive and may overlap (role_prepare includes image
    extraction). Tool call counts exclude cache hits; build and push share one
    backend call/timer.
    """
    metrics = _Metrics()
    output = Path(out).expanduser().absolute()
    if output.exists():
        raise FileExistsError(f"release output already exists: {output}")
    if report is not None and Path(report).absolute().is_relative_to(output):
        raise ValueError("prepare report must be outside the release")
    output.parent.mkdir(parents=True, exist_ok=True)
    work = Path(mkdtemp(prefix=f".{output.name}.prepare-", dir=output.parent))
    try:
        return _prepare(
            _rows(selected),
            output=output,
            name=name,
            work=work,
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


def _prepare(
    rows: list[SelectedTask],
    *,
    output: Path,
    name: str,
    work: Path,
    deployment: Any,
    image_repo: str | None,
    report: str | Path | None,
    require_prebuilt_images: bool,
    context: BuildContext | None,
    compose_adapter: ComposeConfigAdapter | None,
    metrics: _Metrics,
) -> PrepareResult:
    from flotilla.native import Artifact, ReleaseWriter, Task, Verification

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
    settings = replace(context.settings, target=_repo(image_repo)) if image_repo is not None else context.settings
    if settings.target:
        _repo(settings.target)
    registry = _RegistryCache(context.registry, metrics)
    context = replace(
        context,
        settings=settings,
        registry=registry,
        adaptations={},
        builder=_MeasuredBuilder(context.builder, metrics) if context.builder is not None else None,
        image_accounts=partial(metrics.call, "image_accounts", context.image_accounts)
        if context.image_accounts is not None
        else None,
        validate_mount_paths=partial(metrics.call, "mount_path_inspection", context.validate_mount_paths)
        if context.validate_mount_paths is not None
        else None,
    )
    if context.image_export is not None:
        context = replace(context, image_export=_Exports(context.image_export, work / "seeds", metrics))
    adapter = compose_adapter or ComposeConfigAdapter()
    if not rows:
        raise PreparationError("cannot prepare an empty selection", [])
    reports = [
        {"row": i, "task_key": row.task_key, "status": "pending", "stage": "import", "code": "not_started"}
        for i, row in enumerate(rows)
    ]
    # A task selected several times is imported and packaged once; its rows repeat.
    sources: dict[str, Path] = {}
    imported_tasks: dict[str, ImportedTask] = {}
    for i, row in enumerate(rows):
        try:
            previous = sources.setdefault(row.task_key, row.task_path)
            if previous != row.task_path:
                raise ValueError(f"task key {row.task_key!r} names two source tasks: {previous}, {row.task_path}")
            if row.task_key not in imported_tasks:
                imported = metrics.call(
                    "import_normalize",
                    import_task,
                    row,
                    caps=context.caps,
                    compose_adapter=adapter,
                    require_prebuilt_images=require_prebuilt_images,
                )
                # Preflight every selected role before the first image publication.
                for task in imported.roles.values():
                    metrics.call(
                        "image_preflight",
                        preflight_images,
                        pin_images(plan_images(task.project, settings), registry),
                        context,
                    )
                imported_tasks[row.task_key] = imported
            reports[i].update(stage="prepare", code="validated")
        except (OSError, ValueError, RuntimeError) as exc:
            reports[i].update(status="failed", code="input_or_image_error", reason=str(exc))
    if any(row["status"] == "failed" for row in reports):
        for report_row in reports:
            if report_row["status"] == "pending":
                report_row.update(status="not_run", code="preflight_failed")
        _write(report, {"rows": reports, "images": [], "metrics": metrics.snapshot()})
        raise PreparationError("prepare preflight failed; no release published", reports)
    dataset = []
    published_images: set[str] = set()
    packaged: set[str] = set()
    prepared_images: dict[str, dict[str, Any]] = {}
    try:
        with ReleaseWriter(output, name) as writer:
            for i, row in enumerate(rows):
                imported = imported_tasks[row.task_key]
                try:
                    if row.task_key not in packaged:
                        directory = writer.directory(row.task_key)
                        roles = {}
                        files: dict[str, Path] = {}
                        sources_by_role = {}
                        images_by_role = {}
                        for role, task in imported.roles.items():
                            sources_by_role[role] = {
                                p.service: p.ref.pinned(p.ref.digest or "")
                                for p in pin_images(plan_images(task.project, settings), registry)
                                if p.ref
                            }
                            outcome = metrics.call("role_prepare", build_task, task, work / "builds", context)
                            published_images.update(context.adaptations.values())
                            if outcome.status != "built" or outcome.manifest_path is None:
                                raise ValueError(f"{role}: {outcome.reason}")
                            images_by_role[role] = {
                                "dispositions": dict(outcome.dispositions),
                                "adapted": list(outcome.adapted),
                            }
                            for disposition in outcome.dispositions.values():
                                if disposition == "cache-hit":
                                    metrics.counts["build_cache_hits"] += 1
                                elif disposition == "config-only":
                                    metrics.counts["config_only"] += 1
                            manifest = Manifest.load_json(outcome.manifest_path.read_text())
                            published_images.update(unit.image for unit in manifest.units.values())
                            if outcome.needs_publish:
                                # Roles export identical task files: the task directory holds one copy.
                                files.setdefault("files", outcome.manifest_path.parent / "files")
                                manifest = _file_paths(manifest, f"{directory}/files")
                            roles[role] = manifest
                        verification = None
                        artifacts: tuple[Artifact, ...] = ()
                        if imported.tests is not None:
                            if not (imported.tests / "test.sh").is_file():
                                raise ValueError("Harbor tests directory has no test.sh")
                            files["tests"] = imported.tests
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
                            "source_images": sources_by_role,
                            "adaptation": "user0-v1",
                            "parameters": imported.selection.parameters,
                        }
                        native = Task(
                            key=row.task_key,
                            roles=roles,
                            verification=verification,
                            artifacts=artifacts,
                            provenance=provenance,
                        )
                        metrics.call("package", writer.add_task, native, imported.instruction, files)
                        packaged.add(row.task_key)
                        prepared_images[row.task_key] = images_by_role
                    dataset.append({"task": row.task_key, "annotations": row.annotations, "parameters": row.parameters})
                    reports[i].update(status="prepared", stage="package", code="complete")
                    if row.task_key in prepared_images:
                        reports[i]["images"] = prepared_images[row.task_key]
                except (OSError, ValueError, RuntimeError) as exc:
                    reports[i].update(status="failed", stage="prepare", code="prepare_error", reason=str(exc))
                    # Keep processing so each selected row receives its own diagnosis.
            if any(row["status"] == "failed" for row in reports):
                raise PreparationError("one or more selected rows failed; no release published", reports)
            metrics.call("package", writer.publish, dataset)
    finally:
        measurements = metrics.snapshot()
        _write(
            report,
            {
                "rows": reports,
                "images": sorted(published_images | set(context.adaptations.values())),
                "metrics": measurements,
            },
        )
    return PrepareResult(output, name, tuple(reports), tuple(sorted(published_images)), measurements)


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
    return manifest.model_copy(
        update={"units": units, "trial_volumes": volumes, "task_files": None, "task_files_published": None}
    )
