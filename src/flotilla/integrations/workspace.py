"""Trusted management-image entrypoint for an attempt-local Harbor task.

Invoke with ``python -I -m flotilla.integrations.workspace /absolute/request.json``.
The package and its tools must be installed in the management image. Nothing is
imported or executed from the dataset by this reader. BuildKit executes Dockerfile
instructions only through the explicitly configured builder.
"""

from __future__ import annotations

import json
import os
import stat
import subprocess
import sys
import uuid
from copy import deepcopy
from dataclasses import replace
from pathlib import Path
from typing import Any

from flotilla.build.driver import build_task
from flotilla.build.keys import entries
from flotilla.compose.adapter import ComposeConfigAdapter
from flotilla.config import FlotillaConfig
from flotilla.integrations.harbor_source import task_environments
from flotilla.manifest import Manifest
from flotilla.prepare.docker import docker_context, sweep_journal


def validate_tree(root: Path) -> None:
    """Check the whole private copy before any parser follows an input path."""
    if root.is_symlink() or not root.is_dir():
        raise ValueError("task copy must be a real directory")
    root = root.resolve(strict=True)
    for directory, dirs, files in os.walk(root, followlinks=False):
        for name in [*dirs, *files]:
            path = Path(directory) / name
            mode = path.lstat().st_mode
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


def _copy_tests(source: Path, target: Path) -> None:
    # Relative symlinks confined to the tests subtree survive relocation. Links
    # into sibling task directories or absolute paths do not; reject, don't
    # silently dereference them or publish a dangling/escaping private copy.
    source = source.resolve(strict=True)
    validate_tree(source)
    for path in source.rglob("*"):
        if path.is_symlink() and Path(os.readlink(path)).is_absolute():
            raise ValueError(f"absolute test symlinks cannot be relocated: {path}")
    if target.is_symlink():
        raise ValueError("private tests directory must not be a symlink")
    if not target.exists():
        subprocess.run(["cp", "-a", "--", str(source), str(target)], check=True)
    validate_tree(target)


def describe(source: Path, workspace: Path) -> dict[str, Any]:
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
    public = _directory(workspace / "public")
    instruction = public / "instruction.md"
    if instruction.is_symlink():
        raise ValueError("public instruction must not be a symlink")
    instruction.write_text(task.instruction)
    instruction.chmod(0o644)
    private_tests = workspace / "private-tests"
    if not (definition is not None and definition.bundled_tests):
        if script is None:
            raise ValueError("Harbor task is missing its verifier script")
        _copy_tests(task.paths.tests_dir, private_tests)
        executable = private_tests / script.name
        # Native Harbor makes the test script executable before invocation. Keep
        # its owner and read/write permissions; do not recursively chmod the tree.
        executable.chmod(stat.S_IMODE(executable.stat().st_mode) | 0o111)
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
    if len(sys.argv) != 2:
        raise SystemExit("usage: python -I -m flotilla.integrations.workspace /absolute/request.json")
    path = Path(sys.argv[1])
    if not path.is_absolute():
        raise ValueError("request path must be absolute")
    request = json.loads(path.read_bytes())
    result = compile_request(request)
    output = Path(request["workspace"]) / "result.json"
    temporary = output.with_suffix(".tmp")
    with temporary.open("w") as stream:
        json.dump(result, stream)
    temporary.chmod(0o600)
    temporary.replace(output)


if __name__ == "__main__":
    main()
