"""Host selection and atomic native release preparation; no remote workspace API."""

from __future__ import annotations

import hashlib
import json
import shutil
import stat
import time
from collections.abc import Callable, Mapping, Sequence
from dataclasses import asdict, dataclass, field, replace
from functools import partial
from pathlib import Path
from typing import Any, TypeVar

from flotilla.build.builder import Builder, BuildRequest
from flotilla.build.driver import BuildContext, build_task, preflight_images
from flotilla.build.images import ImageRef, pin_images, plan_images
from flotilla.compose.adapter import ComposeConfigAdapter
from flotilla.compose.paths import confined
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
    release_id: str
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
                "fingerprint",
                "image_preflight",
                "registry_digest",
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
        self.counts = {"registry_cache_hits": 0, "seed_cache_hits": 0, "adaptations_resumed": 0}

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
    single_step_only: bool = False,
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
                single_step_only=single_step_only,
            )
            rows.append(row)
            reports.append(
                {
                    "task_key": row.task_key,
                    "status": "selected",
                    "stage": "select",
                    "code": "supported",
                    "roles": list(imported.roles),
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


def _inventory(root: Path) -> list[dict[str, Any]]:
    """Content and numeric metadata; reject links/special files rather than following them."""
    result = []
    for entry in [root, *sorted(root.rglob("*"))] if root.is_dir() else [root]:
        info = entry.lstat()
        if not (stat.S_ISREG(info.st_mode) or stat.S_ISDIR(info.st_mode)):
            raise ValueError(f"input must contain only regular files and directories: {entry}")
        item = {
            "path": entry.relative_to(root).as_posix(),
            "mode": stat.S_IMODE(info.st_mode),
            "uid": info.st_uid,
            "gid": info.st_gid,
        }
        if stat.S_ISREG(info.st_mode):
            digest = hashlib.sha256()
            with entry.open("rb") as stream:
                for chunk in iter(lambda: stream.read(1024 * 1024), b""):
                    digest.update(chunk)
            item["digest"] = digest.hexdigest()
        result.append(item)
    return result


def _fingerprint(imported: ImportedTask, context: BuildContext) -> str:
    inputs = []
    for role, task in imported.roles.items():
        plans = pin_images(plan_images(task.project, context.settings), context.registry)
        dependencies = {}
        for service in task.project.services.values():
            for mount in service.volumes:
                if mount.kind == "bind":
                    dependencies[mount.source] = _inventory(confined(task.env_dir, mount.source))
            if service.build is not None:
                dependencies[service.build.context] = _inventory(confined(task.env_dir, service.build.context))
        inputs.append(
            {
                "role": role,
                "project": asdict(task.project),
                "images": [asdict(plan) for plan in plans],
                "files": dependencies,
            }
        )
    value = {
        "key": imported.selection.task_key,
        "parameters": imported.selection.parameters,
        "config": imported.config.model_dump(mode="json"),
        "instruction": imported.instruction,
        "roles": inputs,
        "tests": _inventory(imported.tests) if imported.tests else None,
    }
    return hashlib.sha256(_json(value).encode()).hexdigest()


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
    deployment: Any = None,
    image_repo: str | None = None,
    report: str | Path | None = None,
    resume: bool = False,
    work_dir: str | Path | None = None,
    require_prebuilt_images: bool = False,
    single_step_only: bool = False,
    context: BuildContext | None = None,
    compose_adapter: ComposeConfigAdapter | None = None,
) -> PrepareResult:
    """Import selected rows into one complete native release, or publish nothing.

    Building/pushing necessary images is authorized by invoking this operation.
    It never uploads files, creates remote sandboxes, or requires Anchor. Resume
    is explicit and checks source content/pinned inputs plus every saved remote
    adaptation. Reports/work evidence stay outside the executable release.

    Report/result metrics describe this invocation only, including failed calls;
    resume does not carry forward old durations. Phase times are inclusive and
    may overlap (for example role_prepare includes image extraction). Tool call
    counts exclude cache hits; build and push share one backend call/timer.
    """
    from flotilla.native import Artifact, ReleaseWriter, Task, Verification

    metrics = _Metrics()
    output = Path(out).expanduser().absolute()
    if output.exists():
        raise FileExistsError(f"release output already exists: {output}")
    work = Path(work_dir).absolute() if work_dir else output.with_name(f".{output.name}.prepare")
    if work.is_relative_to(output) or (report is not None and Path(report).absolute().is_relative_to(output)):
        raise ValueError("prepare work directory and report must be outside the release")
    if work.exists() != resume:
        raise ValueError("existing work evidence requires explicit resume; resume requires existing evidence")
    work.mkdir(parents=True, exist_ok=True)
    state_path = work / "state.json"
    if resume:
        if not state_path.is_file():
            raise PreparationError("resume requires work evidence from an interrupted prepare of the same inputs", [])
        state = json.loads(state_path.read_text())
    else:
        state = {}
    if context is None:
        from flotilla.config import FlotillaConfig
        from flotilla.prepare.docker import docker_context, sweep_journal

        if deployment is None:
            raise ValueError("prepare requires deployment or an explicit BuildContext")
        cfg = deployment if isinstance(deployment, FlotillaConfig) else FlotillaConfig.load(deployment)
        if resume and sweep_journal(cfg.prepare.docker_command, work / "containers"):
            raise ValueError("interrupted image export containers could not be cleaned up")
        context = docker_context(cfg, work / "containers")
        compose_adapter = compose_adapter or ComposeConfigAdapter(
            command=cfg.prepare.compose_command, expected_version=cfg.prepare.compose_version
        )
    settings = replace(context.settings, target=_repo(image_repo)) if image_repo is not None else context.settings
    if settings.target:
        _repo(settings.target)
    # Each invocation owns fresh memoization; caller BuildContexts do not become global caches.
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
        # Seed extraction deduplicates within this invocation; resume re-extracts
        # rather than trusting unverified partial local seed caches.
        from tempfile import mkdtemp

        context = replace(
            context, image_export=_Exports(context.image_export, Path(mkdtemp(prefix="seeds-", dir=work)), metrics)
        )
    adapter = compose_adapter or ComposeConfigAdapter()
    rows = _rows(selected)
    if not rows:
        raise PreparationError("cannot prepare an empty selection", [])
    reports = [
        {"row": i, "task_key": row.task_key, "status": "pending", "stage": "import", "code": "not_started"}
        for i, row in enumerate(rows)
    ]
    imported_rows: list[ImportedTask | None] = []
    fingerprints: list[str | None] = []
    for i, row in enumerate(rows):
        try:
            imported = metrics.call(
                "import_normalize",
                import_task,
                row,
                caps=context.caps,
                compose_adapter=adapter,
                require_prebuilt_images=require_prebuilt_images,
                single_step_only=single_step_only,
            )
            fingerprint = metrics.call("fingerprint", _fingerprint, imported, context)
            imported_rows.append(imported)
            fingerprints.append(fingerprint)
            # Preflight every selected role before the first image publication.
            for task in imported.roles.values():
                metrics.call(
                    "image_preflight",
                    preflight_images,
                    pin_images(plan_images(task.project, settings), registry),
                    context,
                )
            reports[i].update(stage="prepare", code="validated")
        except (OSError, ValueError, RuntimeError) as exc:
            if len(imported_rows) <= i:
                imported_rows.append(None)
                fingerprints.append(None)
            reports[i].update(status="failed", code="input_or_image_error", reason=str(exc))
    operation = hashlib.sha256(
        _json(
            {
                "inputs": fingerprints,
                "rows": [r.model_dump(mode="json", exclude={"task_path"}) for r in rows],
                "settings": asdict(settings),
                "policy": require_prebuilt_images,
                "version": 1,
            }
        ).encode()
    ).hexdigest()
    if resume and state.get("input_id") != operation:
        raise PreparationError("resume inputs changed; start a new prepare work directory", reports)
    if any(row["status"] == "failed" for row in reports):
        for report_row in reports:
            if report_row["status"] == "pending":
                report_row.update(status="not_run", code="preflight_failed")
        _write(report, {"rows": reports, "images": [], "metrics": metrics.snapshot()})
        raise PreparationError("prepare preflight failed; no release published", reports)
    for key, pinned in state.get("adaptations", {}).items():
        ref = ImageRef.parse(pinned)
        if ref.digest is None or registry.digest(ref.name, ref.digest) != ref.digest:
            raise PreparationError("resume adaptation is not available at its recorded remote digest", reports)
        context.adaptations[key] = pinned
        metrics.counts["adaptations_resumed"] += 1
    state = {"input_id": operation, "adaptations": context.adaptations}
    _write(state_path, state)
    records: dict[str, Any] = {}
    dataset = []
    published_images: set[str] = set(context.adaptations.values())
    try:
        with ReleaseWriter(output) as writer:
            for i, imported_row in enumerate(imported_rows):
                assert imported_row is not None
                imported = imported_row
                try:
                    stored = fingerprints[i]
                    assert stored is not None
                    fingerprint = stored
                    if fingerprint not in records:
                        roles = {}
                        assets = []
                        sources = {}
                        for role, task in imported.roles.items():
                            sources[role] = {
                                p.service: p.ref.pinned(p.ref.digest or "")
                                for p in pin_images(plan_images(task.project, settings), registry)
                                if p.ref
                            }
                            outcome = metrics.call(
                                "role_prepare", build_task, task, work / "builds", context, identity=fingerprint
                            )
                            _write(state_path, state)
                            published_images.update(context.adaptations.values())
                            if outcome.status != "built" or outcome.manifest_path is None:
                                raise ValueError(f"{role}: {outcome.reason}")
                            manifest = Manifest.load_json(outcome.manifest_path.read_text())
                            published_images.update(unit.image for unit in manifest.units.values())
                            if outcome.needs_publish:
                                directory = outcome.manifest_path.parent
                                identities = json.loads((directory / "file-identities.json").read_text())
                                asset = metrics.call(
                                    "package", writer.add_asset, directory / "files", identities=identities
                                )
                                if asset not in assets:  # roles publish identical files: one asset entry
                                    assets.append(asset)
                                manifest = _asset_paths(manifest, asset)
                            roles[role] = manifest
                        verification = None
                        artifacts: tuple[Artifact, ...] = ()
                        if imported.tests is not None:
                            if not (imported.tests / "test.sh").is_file():
                                raise ValueError("Harbor tests directory has no test.sh")
                            tests = metrics.call("package", writer.add_asset, imported.tests)
                            assets.append(tests)
                            verification = Verification(
                                role="verifier" if "verifier" in roles else "main",
                                command=("bash", "/tests/test.sh"),
                                tests=tests,
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
                            "source_images": sources,
                            "adaptation": "user0-v1",
                            "parameters": imported.selection.parameters,
                        }
                        native = Task(
                            key=imported.selection.task_key,
                            roles=roles,
                            assets=tuple(assets),
                            verification=verification,
                            artifacts=artifacts,
                            provenance=provenance,
                        )
                        records[fingerprint] = metrics.call("package", writer.add_task, native, imported.instruction)
                    record = records[fingerprint]
                    dataset.append(
                        {
                            "release_id": "",
                            "task_ref": record.task_ref,
                            "task_digest": record.task_digest,
                            "annotations": imported.selection.annotations,
                            "parameters": imported.selection.parameters,
                        }
                    )
                    reports[i].update(
                        status="prepared",
                        stage="package",
                        code="complete",
                        validation="source digests/static assets/native schema; platform execution not tested",
                    )
                except (OSError, ValueError, RuntimeError) as exc:
                    reports[i].update(status="failed", stage="prepare", code="prepare_error", reason=str(exc))
                    # Keep processing so each selected row receives its own diagnosis.
                finally:
                    _write(state_path, state)
            if any(row["status"] == "failed" for row in reports):
                raise PreparationError("one or more selected rows failed; no release published", reports)
            release = metrics.call("package", writer.publish, dataset)
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
    return PrepareResult(output, release.release_id, tuple(reports), tuple(sorted(published_images)), measurements)


def _asset_paths(manifest: Manifest, asset: str) -> Manifest:
    units = {
        name: unit.model_copy(
            update={
                "mounts": [
                    mount.model_copy(update={"key": f"{asset}/{mount.key}"}) if mount.scope == "task" else mount
                    for mount in unit.mounts
                ]
            }
        )
        for name, unit in manifest.units.items()
    }
    volumes = [
        volume.model_copy(
            update={
                "seed": f"{asset}/{volume.seed}" if volume.seed else None,
                "copy_from": f"{asset}/{volume.copy_from}" if volume.copy_from else None,
            }
        )
        for volume in manifest.trial_volumes
    ]
    return manifest.model_copy(
        update={"units": units, "trial_volumes": volumes, "task_files": None, "task_files_published": None}
    )
