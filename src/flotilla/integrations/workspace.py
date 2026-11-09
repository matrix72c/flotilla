"""Trusted management-image entrypoint for native Harbor tasks.

Invoke with ``python -I -m flotilla.integrations.workspace /absolute/request.json``;
add ``--describe-tasks`` for canonical-source instruction/config metadata, or
``--describe-task-resources`` for capacity-only inspection without image work.
The package and its tools must be installed in the management image. Nothing is
imported or executed from the dataset by this reader. BuildKit executes Dockerfile
instructions only through the explicitly configured builder.
"""

from __future__ import annotations

import errno
import json
import logging
import os
import shutil
import stat
import subprocess
import sys
import time
import uuid
from collections.abc import Callable, Iterator
from copy import deepcopy
from dataclasses import dataclass, replace
from functools import partial
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import Any

from flotilla.build.driver import build_task
from flotilla.build.keys import entries
from flotilla.compose.adapter import ComposeConfigAdapter
from flotilla.compose.paths import confined
from flotilla.compose.task import COMPOSE_FILE
from flotilla.config import FlotillaConfig
from flotilla.integrations.harbor_source import task_environments
from flotilla.manifest import Manifest, Resources
from flotilla.prepare.docker import docker_context, sweep_journal
from flotilla.runtime.workspace import (
    METADATA_MAX_TASKS,
    METADATA_REQUEST_BYTES,
    METADATA_RESULT_BYTES,
    runtime_parameters,
    source_task_key,
)

_LOG = logging.getLogger(__name__)
_READ_DELAYS = (0.5, 1.0, 2.0, 4.0)
_READ_RETRY_WINDOW = 30.0


@dataclass
class _ReadBudget:
    """Shared added-wait allowance, not a timeout for hard-mounted NFS calls."""

    remaining_sleep: float = 15.0

    def run[T](self, operation: Callable[[], T], *, stage: str) -> T:
        deadline = time.monotonic() + _READ_RETRY_WINDOW
        for attempt in range(1, len(_READ_DELAYS) + 2):
            try:
                result = operation()
            except OSError as exc:
                if exc.errno != errno.ESTALE:
                    raise
                reason = "attempt limit"
                if attempt <= len(_READ_DELAYS):
                    delay = _READ_DELAYS[attempt - 1]
                    if delay > self.remaining_sleep:
                        reason = "request wait budget"
                    elif time.monotonic() + delay >= deadline:
                        reason = "read retry deadline"
                    else:
                        self.remaining_sleep -= delay
                        time.sleep(delay)
                        if time.monotonic() < deadline:
                            continue
                        reason = "read retry deadline"
                exc.add_note(f"{stage}: stale read recovery exhausted after {attempt} attempts ({reason})")
                raise
            if attempt > 1:
                _LOG.warning("%s: stale read recovered after %d attempts", stage, attempt)
            return result
        raise AssertionError("unreachable read retry state")


def _tree_entries(root: Path) -> Iterator[tuple[Path, int]]:
    # os.walk/rglob can suppress enumeration and DirEntry.is_dir errors. Use
    # explicit lstat so a failed directory lookup cannot hide a whole subtree.
    pending = [root]
    while pending:
        directory = pending.pop()
        if not stat.S_ISDIR(directory.lstat().st_mode):
            raise ValueError("task tree directory changed type during inspection")
        with os.scandir(directory) as entries:
            for entry in entries:
                path = Path(entry.path)
                mode = path.lstat().st_mode
                yield path, mode
                if stat.S_ISDIR(mode):
                    pending.append(path)


def validate_tree(root: Path) -> None:
    """Check the whole private copy before any parser follows an input path."""
    if not stat.S_ISDIR(root.lstat().st_mode):
        raise ValueError("task copy must be a real directory")
    root = root.resolve(strict=True)
    for path, mode in _tree_entries(root):
        if stat.S_ISLNK(mode):
            if not path.resolve(strict=True).is_relative_to(root):
                raise ValueError(f"task link escapes its private copy: {path.relative_to(root)}")
        elif not (stat.S_ISREG(mode) or stat.S_ISDIR(mode)):
            raise ValueError(f"unsupported task file type: {path.relative_to(root)}")


def _directory(path: Path) -> Path:
    if path.is_symlink():
        raise ValueError(f"workspace output directory must not be a symlink: {path}")
    path.mkdir(exist_ok=True)
    if not path.is_dir():
        raise ValueError(f"workspace output must be a directory: {path}")
    return path


def _validate_tests(source: Path) -> None:
    # Relative symlinks confined to the tests subtree survive relocation. Links
    # into sibling task directories or absolute paths do not; reject, don't
    # silently dereference them or publish a dangling/escaping private copy.
    source = source.resolve(strict=True)
    validate_tree(source)
    for path, mode in _tree_entries(source):
        if stat.S_ISLNK(mode) and Path(os.readlink(path)).is_absolute():
            raise ValueError(f"absolute test symlinks cannot be relocated: {path}")


def _copy_tests(source: Path, target: Path) -> None:
    source = source.resolve(strict=True)
    _validate_tests(source)
    if target.is_symlink():
        raise ValueError("private tests directory must not be a symlink")
    if not target.exists():
        subprocess.run(["cp", "-a", "--", str(source), str(target)], check=True)
    validate_tree(target)


def _describe_source(source: Path) -> dict[str, Any]:
    """Native parsing and supported-tree validation, with no filesystem writes."""
    from harbor.models.task.config import TaskOS
    from harbor.models.task.task import Task
    from harbor.models.task.verifier_mode import resolve_verifier_environment_definition

    validate_tree(source)
    task = Task(source)
    if task.has_steps:
        raise ValueError("training supports single-step Harbor tasks; multi-step tasks are not silently flattened")
    definition = resolve_verifier_environment_definition(task.config, task.paths)
    configs = [task.config.environment] + ([definition.config] if definition is not None else [])
    if any(config.os != TaskOS.LINUX for config in configs):
        raise ValueError("this backend supports Linux Harbor environments only")
    definitions = {
        "main": {
            "directory": task.paths.environment_dir.relative_to(source).as_posix(),
            "config": task.config.environment.model_dump(mode="json"),
            "bundled_tests": False,
        }
    }
    if definition is not None:
        definitions["verifier"] = {
            "directory": definition.directory.relative_to(source).as_posix(),
            "config": definition.config.model_dump(mode="json"),
            "bundled_tests": definition.bundled_tests,
        }
    script = task.paths.discovered_test_path_for(configs[-1].os)
    if not (definition is not None and definition.bundled_tests):
        if script is None:
            raise ValueError("Harbor task is missing its verifier script")
        _validate_tests(task.paths.tests_dir)
    return {
        "task_id": source.name,
        "task_config": task.config.model_dump(mode="json"),
        "instruction": task.instruction,
        "definitions": definitions,
        "tests": {
            "script": script.name if script is not None else None,
            "directory": "private-tests",
        },
    }


def describe(source: Path, workspace: Path) -> dict[str, Any]:
    from harbor.models.task.paths import TaskPaths

    result = _ReadBudget().run(lambda: _describe_source(source), stage="task description")
    public = _directory(workspace / "public")
    instruction = public / "instruction.md"
    if instruction.is_symlink():
        raise ValueError("public instruction must not be a symlink")
    instruction.write_text(result["instruction"])
    instruction.chmod(0o644)
    if not result["definitions"].get("verifier", {}).get("bundled_tests", False):
        private_tests = workspace / "private-tests"
        _copy_tests(TaskPaths(source).tests_dir, private_tests)
        executable = private_tests / result["tests"]["script"]
        # Native Harbor makes the test script executable before invocation. Keep
        # its owner and read/write permissions; do not recursively chmod the tree.
        executable.chmod(stat.S_IMODE(executable.stat().st_mode) | 0o111)
    return result


def _describe_resources(
    source: Path, metadata: dict[str, Any], request: dict[str, Any], budget: _ReadBudget
) -> dict[str, Any]:
    adapter = ComposeConfigAdapter(
        command=tuple(request["compose_command"]),
        expected_version=request["compose_version"],
        resources_only=True,
    )

    def inspect(path: Path) -> dict[str, Any]:
        environments = task_environments(path, compose_adapter=adapter, compose_environment=request["runtime_params"])
        result = {}
        for role, environment in zip(metadata["definitions"], environments, strict=True):
            rejected = [
                finding.reason
                for finding in (*environment.findings, *environment.project.findings)
                if finding.kind == "reject"
            ]
            if rejected:
                raise ValueError(f"{source.name}/{role}: {'; '.join(rejected[:3])}")
            # Same declared resources as build_task, before any image planning,
            # registry access, build, export or workload creation.
            result[role] = {
                name: Resources.model_validate(service.resources).model_dump(exclude_none=True)
                for name, service in environment.project.services.items()
            }
        return result

    def has_compose() -> bool:
        return any(
            confined(source / definition["directory"], COMPOSE_FILE).is_file()
            for definition in metadata["definitions"].values()
        )

    if not budget.run(has_compose, stage="resource context discovery"):
        return inspect(source)
    # Compose reads are confined to each environment by the existing adapter
    # gate (include/extends remain unsupported). Keep complete role contexts,
    # including .env/env_file and build/bind paths; omit unrelated hidden tests
    # and payloads only when there are no links whose targets we might remove.
    contexts = {Path(definition["directory"]).parts[0] for definition in metadata["definitions"].values()}
    keep = contexts | {"task.toml"}
    linked = budget.run(
        lambda: any(stat.S_ISLNK(mode) for _, mode in _tree_entries(source)), stage="resource link discovery"
    )

    def ignore(directory: str, names: list[str]) -> set[str]:
        return set(names) - keep if not linked and Path(directory) == source else set()

    # Compose and its overlays must never run in the immutable native tree.
    with TemporaryDirectory(prefix="flotilla-resources-") as temporary:
        private = Path(temporary) / source.name
        shutil.copytree(source, private, symlinks=True, ignore=ignore)
        validate_tree(private)
        return inspect(private)


def _metadata_root(root: Path) -> Path:
    if not root.is_absolute() or not stat.S_ISDIR(root.lstat().st_mode):
        raise ValueError("metadata storage root must be a real absolute directory")
    return root.resolve(strict=True)


def _read_metadata(root: Path, key: str) -> tuple[Path, dict[str, Any]]:
    source = _metadata_root(root)
    for component in key.split("/"):
        source = source / component
        if not stat.S_ISDIR(source.lstat().st_mode):
            raise ValueError("task source and its ancestors must be real directories, not symlinks")
    return source, _describe_source(source)


def describe_tasks_request(request: dict[str, Any], *, resources: bool = False) -> bytes:
    """Read immutable canonical sources; publish nothing until the whole batch succeeds."""
    if resources:
        request = {**request, "runtime_params": runtime_parameters(request.get("runtime_params"))}
    keys = request["task_keys"]
    if not isinstance(keys, list) or not 1 <= len(keys) <= METADATA_MAX_TASKS:
        raise ValueError("metadata request must contain 1 to 128 task keys")
    keys = list(dict.fromkeys(source_task_key(key) for key in keys))
    root = Path(request["storage_root"])
    budget = _ReadBudget()
    budget.run(lambda: _metadata_root(root), stage="metadata storage root")
    parts: list[bytes] = []
    size = 2  # outer JSON object braces
    for key in keys:
        source, metadata = budget.run(partial(_read_metadata, root, key), stage="task metadata")
        value = (
            _describe_resources(source, metadata, request, budget)
            if resources
            else {"instruction": metadata["instruction"], "task_config": metadata["task_config"]}
        )
        entry = json.dumps(key).encode() + b":" + json.dumps(value).encode()
        size += len(entry) + (1 if parts else 0)
        if size > METADATA_RESULT_BYTES:
            raise ValueError("metadata result exceeds output byte limit")
        parts.append(entry)
    return b"{" + b",".join(parts) + b"}"


def restore_identities(root: Path) -> None:
    """The ephemeral exporter may use staging ownership; restore its numeric metadata."""
    if root.is_symlink() or not root.is_dir():
        raise ValueError("exported files must be a real directory")
    for entry in entries(root):
        path = root / str(entry["path"])
        os.chown(path, int(entry["uid"]), int(entry["gid"]), follow_symlinks=False)
        if entry["type"] != "link":
            path.chmod(int(str(entry["mode"]), 8))


def compile_request(request: dict[str, Any]) -> dict[str, Any]:
    root = Path(request["workspace"])
    if not root.is_absolute() or root.is_symlink():
        raise ValueError("workspace must be a real absolute directory")
    root = root.resolve(strict=True)
    source = Path(request["source"])
    if source.is_symlink() or not source.resolve(strict=True).is_relative_to(root / "source"):
        raise ValueError("compiler source must be the attempt's private task copy")
    source = source.resolve(strict=True)
    if source.name != request["task_id"]:
        raise ValueError("task ID must be the original task directory basename")
    result = describe(source, root)
    if request.get("describe", False):
        return result

    report_path = root / "capabilities.json"
    report_path.write_text(json.dumps(request["capabilities"]))
    report_path.chmod(0o600)
    deployment = deepcopy(request["deployment"])
    backend = deployment.get("backend", "opensandbox")
    if backend == "opensandbox":
        deployment["opensandbox"]["capabilities"] = str(report_path)
    else:
        deployment["backends"][backend]["capabilities"] = str(report_path)
    cfg = FlotillaConfig.model_validate(deployment)
    cfg.check_against(cfg.load_report())
    adapter = ComposeConfigAdapter(command=cfg.prepare.compose_command, expected_version=cfg.prepare.compose_version)
    environments = task_environments(
        source,
        compose_adapter=adapter,
        compose_environment={**cfg.runtime_params, **request.get("runtime_params", {})},
    )
    if len(environments) != len(result["definitions"]):
        raise ValueError("Harbor role discovery disagrees with compiled environments")
    generated = _directory(root / "generated")
    journal = generated / "containers"
    if journal.is_symlink():
        raise ValueError("container journal must not be a symlink")
    compiled = {}
    identity_prefix = f"{root.name}--{uuid.uuid4().hex}"
    primary: BaseException | None = None
    try:
        ctx = docker_context(cfg, journal)
        for role, environment in zip(result["definitions"], environments, strict=True):
            output = _directory(generated / role)
            # Build identities isolate simultaneous same-basename tasks, without
            # changing their task ID or treating it as a cache key.
            identity = f"{identity_prefix}--{role}"
            outcome = build_task(replace(environment, name=source.name), output, ctx, identity=identity)
            if outcome.manifest_path is None:
                raise ValueError(f"{source.name}/{role}: {outcome.status}: {outcome.reason}")
            manifest = Manifest.load_json(outcome.manifest_path.read_bytes())
            files = outcome.manifest_path.parent / "files"
            if outcome.needs_publish:
                restore_identities(files)
                files_key = request["workspace_key"] + "/" + files.relative_to(root).as_posix()
                manifest = manifest.model_copy(update={"task_files": files_key, "task_files_published": None})
            compiled[role] = manifest.model_dump(mode="json", by_alias=True)
    except BaseException as exc:
        primary = exc
        raise
    finally:
        try:
            leftover = sweep_journal(cfg.prepare.docker_command, journal)
            if leftover:
                raise RuntimeError(f"workspace export containers not removed: {leftover}")
        except BaseException as cleanup_error:
            if primary is None:
                raise
            primary.add_note(f"workspace compiler cleanup failed: {cleanup_error}")
    result["environments"] = compiled
    return result


def main() -> None:
    batch = len(sys.argv) == 3 and sys.argv[1] in {"--describe-tasks", "--describe-task-resources"}
    if not batch and len(sys.argv) != 2:
        raise SystemExit(
            "usage: python -I -m flotilla.integrations.workspace "
            "[--describe-tasks|--describe-task-resources] /absolute/request.json"
        )
    path = Path(sys.argv[-1])
    if not path.is_absolute():
        raise ValueError("request path must be absolute")
    if batch:
        with path.open("rb") as stream:
            data = stream.read(METADATA_REQUEST_BYTES + 1)
        if len(data) > METADATA_REQUEST_BYTES:
            raise ValueError("metadata request exceeds request byte limit")
        request = json.loads(data)
        root = Path(request["storage_root"])
        workspace = Path(request["workspace"])
        relative = workspace.relative_to(root)
        if (
            len(relative.parts) != 4
            or relative.parts[0] != "launches"
            or relative.parts[2] != "metadata"
            or any(part in {".", ".."} for part in relative.parts)
            or path != workspace / "request.json"
        ):
            raise ValueError("metadata output must belong to a launch metadata directory")
        directory = root
        for part in relative.parts:
            directory /= part
            if directory.is_symlink() or not directory.is_dir():
                raise ValueError("metadata output ancestors must be real directories")
        encoded = describe_tasks_request(request, resources=sys.argv[1] == "--describe-task-resources")
    else:
        request = json.loads(path.read_bytes())
        encoded = json.dumps(compile_request(request)).encode()
    output = Path(request["workspace"]) / "result.json"
    temporary = output.with_suffix(".tmp")
    with temporary.open("wb") as stream:
        stream.write(encoded)
    temporary.chmod(0o600)
    temporary.replace(output)


if __name__ == "__main__":
    main()
