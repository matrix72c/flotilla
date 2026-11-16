"""Trusted management-image entrypoint for native Harbor task metadata.

Invoke with ``python -I -m flotilla.integrations.workspace --describe-tasks
/absolute/request.json`` for canonical-source instruction/config metadata, or
``--describe-task-resources`` for capacity-only inspection without image work.
The package and its tools must be installed in the management image. Nothing is
imported or executed from the dataset by this reader.
"""

from __future__ import annotations

import errno
import json
import logging
import os
import shutil
import stat
import sys
import time
from collections.abc import Callable, Iterator
from dataclasses import dataclass
from functools import partial
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import Any

from flotilla.compose.adapter import ComposeConfigAdapter
from flotilla.compose.paths import confined
from flotilla.compose.task import COMPOSE_FILE
from flotilla.integrations.harbor_source import task_environments
from flotilla.manifest import Resources
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


def validate_tree(root: Path, *, relocatable: bool = False) -> None:
    """Check the whole private copy before any parser follows an input path."""
    if not stat.S_ISDIR(root.lstat().st_mode):
        raise ValueError("task copy must be a real directory")
    root = root.resolve(strict=True)
    for path, mode in _tree_entries(root):
        if stat.S_ISLNK(mode):
            if relocatable and Path(os.readlink(path)).is_absolute():
                raise ValueError(f"absolute test symlinks cannot be relocated: {path}")
            if not path.resolve(strict=True).is_relative_to(root):
                raise ValueError(f"task link escapes its private copy: {path.relative_to(root)}")
        elif not (stat.S_ISREG(mode) or stat.S_ISDIR(mode)):
            raise ValueError(f"unsupported task file type: {path.relative_to(root)}")


def _describe_source(source: Path) -> dict[str, Any]:
    """Read metadata and role coordinates; payload validation belongs to use sites."""
    from harbor.models.task.config import TaskConfig, TaskOS
    from harbor.models.task.paths import TaskPaths
    from harbor.models.task.verifier_mode import resolve_verifier_environment_definition

    from flotilla.integrations.harbor_source_reader import NativeTaskSource, SourceMapping

    source = source.resolve(strict=True)
    reader = NativeTaskSource([SourceMapping(local_root=source.parent, platform_prefix="source")])
    metadata = reader.read_metadata(f"source/{source.name}")
    config = TaskConfig.model_validate(metadata["task_config"])
    paths = TaskPaths(source)
    if config.steps:
        raise ValueError("training supports single-step Harbor tasks; multi-step tasks are not silently flattened")
    definition = resolve_verifier_environment_definition(config, paths)
    configs = [config.environment] + ([definition.config] if definition is not None else [])
    if any(config.os != TaskOS.LINUX for config in configs):
        raise ValueError("this backend supports Linux Harbor environments only")
    definitions = {
        "main": {
            "directory": paths.environment_dir.relative_to(source).as_posix(),
            "config": config.environment.model_dump(mode="json"),
            "bundled_tests": False,
        }
    }
    if definition is not None:
        definitions["verifier"] = {
            "directory": definition.directory.relative_to(source).as_posix(),
            "config": definition.config.model_dump(mode="json"),
            "bundled_tests": definition.bundled_tests,
        }
    script = paths.discovered_test_path_for(configs[-1].os)
    if not (definition is not None and definition.bundled_tests) and script is None:
        raise ValueError("Harbor task is missing its verifier script")
    return {
        **metadata,
        "definitions": definitions,
        "tests": {
            "script": script.name if script is not None else None,
            "directory": "private-tests",
        },
    }


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
            # Declared resources before any image planning, registry access,
            # build, export or workload creation.
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


def main() -> None:
    if len(sys.argv) != 3 or sys.argv[1] not in {"--describe-tasks", "--describe-task-resources"}:
        raise SystemExit(
            "usage: python -I -m flotilla.integrations.workspace "
            "--describe-tasks|--describe-task-resources /absolute/request.json"
        )
    path = Path(sys.argv[-1])
    if not path.is_absolute():
        raise ValueError("request path must be absolute")
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
    output = Path(request["workspace"]) / "result.json"
    temporary = output.with_suffix(".tmp")
    with temporary.open("wb") as stream:
        stream.write(encoded)
    temporary.chmod(0o600)
    temporary.replace(output)


if __name__ == "__main__":
    main()
