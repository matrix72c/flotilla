"""Native Harbor description and management compiler contracts, no real platform/tools."""

from __future__ import annotations

import errno
import json
import os
import shutil
import stat
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import Mock

import pytest

from flotilla.integrations import workspace as compiler
from flotilla.testing.planning import plan as plan_environment


def task_copy(tmp_path: Path, *, toml: str = "", script: bool = True) -> tuple[Path, Path]:
    workspace = tmp_path / "attempt"
    source = workspace / "source" / "task"
    (source / "environment").mkdir(parents=True)
    (source / "tests").mkdir()
    (source / "task.toml").write_text('[environment]\ndocker_image="r.io/main:1"\n' + toml)
    (source / "instruction.md").write_text("Solve the task.\n")
    if script:
        (source / "tests/test.sh").write_text("#!/bin/sh\nexit 0\n")
        (source / "tests/test.sh").chmod(0o640)
    return workspace, source


@pytest.fixture
def read_clock(monkeypatch: pytest.MonkeyPatch) -> SimpleNamespace:
    clock = SimpleNamespace(now=0.0, delays=[], oversleep=0.0)

    def sleep(delay: float) -> None:
        clock.delays.append(delay)
        clock.now += delay + clock.oversleep

    monkeypatch.setattr(compiler, "time", SimpleNamespace(monotonic=lambda: clock.now, sleep=sleep))
    return clock


@pytest.mark.parametrize("fault", ["root", "ancestor", "config", "instruction"])
@pytest.mark.parametrize("resources", [False, True])
def test_stale_native_reads_recover_without_changing_metadata(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, read_clock: SimpleNamespace, fault: str, resources: bool
) -> None:
    from harbor.models.task.config import TaskConfig

    _, source = task_copy(tmp_path)
    key = source.relative_to(tmp_path).as_posix()
    request = {
        "storage_root": str(tmp_path),
        "task_keys": [key],
    }
    expected = compiler.describe_tasks_request(request, resources=resources)
    parse = Mock(wraps=TaskConfig.model_validate_toml)
    monkeypatch.setattr(TaskConfig, "model_validate_toml", parse)
    target = {
        "root": tmp_path,
        "ancestor": source.parent,
        "config": source / "task.toml",
        "instruction": source / "instruction.md",
    }[fault]
    owner, method = Path, "read_text" if fault in {"config", "instruction"} else "lstat"
    original = getattr(owner, method)
    failed = False

    def read(path: Any, *args: Any, **kwargs: Any) -> Any:
        nonlocal failed
        if Path(path) == target and not failed:
            failed = True
            raise OSError(errno.ESTALE, "injected stale read")
        return original(path, *args, **kwargs)

    monkeypatch.setattr(owner, method, read)
    assert compiler.describe_tasks_request(request, resources=resources) == expected
    assert failed and read_clock.delays == [0.5]
    # Failed instruction hydration restarts parsing; no partially-read metadata is cached.
    assert parse.call_count == (2 if fault == "instruction" else 1)


def test_stale_read_retry_limits_and_original_error(read_clock: SimpleNamespace) -> None:
    error = OSError(errno.ESTALE, "persistent stale read")
    operation = Mock(side_effect=error)
    with pytest.raises(OSError) as caught:
        compiler._ReadBudget().run(operation, stage="fixture")
    assert caught.value is error
    assert operation.call_count == 5
    assert read_clock.delays == [0.5, 1.0, 2.0, 4.0]
    assert "attempt limit" in error.__notes__[-1]


@pytest.mark.parametrize("oversleep", [False, True])
def test_stale_read_deadline_does_not_admit_another_attempt(read_clock: SimpleNamespace, oversleep: bool) -> None:
    error = OSError(errno.ESTALE, "slow stale read")

    def operation() -> None:
        if not oversleep:
            read_clock.now += 30
        raise error

    read_clock.oversleep = 30 if oversleep else 0
    call = Mock(side_effect=operation)
    with pytest.raises(OSError) as caught:
        compiler._ReadBudget().run(call, stage="fixture")
    assert caught.value is error and call.call_count == 1
    assert read_clock.delays == ([0.5] if oversleep else [])
    assert "deadline" in error.__notes__[-1]


@pytest.mark.parametrize(
    "error",
    [
        OSError(errno.EACCES, "denied"),
        OSError(errno.ENOENT, "missing"),
        OSError(errno.EIO, "io error"),
        ValueError("invalid task"),
        KeyboardInterrupt(),
    ],
)
def test_read_recovery_does_not_swallow_other_errors(read_clock: SimpleNamespace, error: BaseException) -> None:
    call = Mock(side_effect=error)
    with pytest.raises(type(error)) as caught:
        compiler._ReadBudget().run(call, stage="fixture")
    assert caught.value is error and call.call_count == 1
    assert read_clock.delays == []


def test_batch_read_budget_is_shared_and_successful_tasks_are_not_replayed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, read_clock: SimpleNamespace
) -> None:
    sources = [task_copy(tmp_path / str(index))[1] for index in range(3)]
    keys = [source.relative_to(tmp_path).as_posix() for source in sources]
    original = compiler._describe_source
    attempts = dict.fromkeys(sources, 0)

    def read(source: Path) -> dict[str, Any]:
        attempts[source] += 1
        if attempts[source] < 5:
            raise OSError(errno.ESTALE, "injected stale read")
        return original(source)

    monkeypatch.setattr(compiler, "_describe_source", read)
    with pytest.raises(OSError) as error:
        compiler.describe_tasks_request({"storage_root": str(tmp_path), "task_keys": keys})
    assert list(attempts.values()) == [5, 5, 1]
    assert sum(read_clock.delays) == 15
    assert "request wait budget" in error.value.__notes__[-1]


def test_traversal_does_not_follow_a_replaced_directory(tmp_path: Path) -> None:
    root = tmp_path / "root"
    child = root / "child"
    child.mkdir(parents=True)
    outside = tmp_path / "outside"
    outside.mkdir()
    traversal = compiler._tree_entries(root)
    path, mode = next(traversal)
    assert path == child and stat.S_ISDIR(mode)
    child.rmdir()
    child.symlink_to(outside, target_is_directory=True)
    with pytest.raises(ValueError, match="changed type"):
        list(traversal)


@pytest.mark.parametrize("resources", [False, True])
def test_stale_read_exhaustion_publishes_no_batch_result(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, read_clock: SimpleNamespace, resources: bool
) -> None:
    _, workspace, _ = batch_request(tmp_path, monkeypatch, resources)
    parse = Mock(side_effect=OSError(errno.ESTALE, "persistent native read"))
    monkeypatch.setattr(compiler, "_describe_source", parse)
    with pytest.raises(OSError):
        compiler.main()
    assert parse.call_count == 5
    assert not (workspace / "result.json").exists()
    assert not (workspace / "result.tmp").exists()
    assert sum(read_clock.delays) == 7.5


def source_snapshot(source: Path) -> dict[str, Any]:
    return {
        path.relative_to(source).as_posix(): (
            path.lstat().st_mode,
            path.lstat().st_uid,
            path.lstat().st_gid,
            path.lstat().st_mtime_ns,
            path.lstat().st_ctime_ns,
            os.readlink(path) if path.is_symlink() else path.read_bytes() if path.is_file() else None,
        )
        for path in [source, *source.rglob("*")]
    }


@pytest.mark.parametrize("mode", ["shared", "separate", "bundled"])
def test_batch_native_parity_dedup_basename_and_no_source_writes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, mode: str
) -> None:
    toml = '[verifier]\nenvironment_mode="separate"\n' if mode != "shared" else ""
    if mode == "bundled":
        toml += '[verifier.environment]\ndocker_image="r.io/verifier:1"\n'
    source = task_copy(tmp_path / "one", toml=toml, script=mode != "bundled")[1]
    other = task_copy(tmp_path / "two", toml=toml, script=mode != "bundled")[1]
    (other / "instruction.md").write_text("Different same-basename task.\n")
    before = [source_snapshot(path) for path in (source, other)]
    expected = {}
    keys = [path.relative_to(tmp_path).as_posix() for path in (source, other)]
    for key, path in zip(keys, (source, other), strict=True):
        description = compiler._describe_source(path)
        expected[key] = {name: description[name] for name in ("instruction", "task_config")}
    forbidden = Mock(side_effect=AssertionError("batch metadata must not stage, chmod or run tools"))
    monkeypatch.setattr(subprocess, "run", forbidden)
    monkeypatch.setattr(Path, "chmod", forbidden)
    parse = Mock(wraps=compiler._describe_source)
    monkeypatch.setattr(compiler, "_describe_source", parse)
    actual = json.loads(compiler.describe_tasks_request({"storage_root": str(tmp_path), "task_keys": [*keys, keys[0]]}))
    assert actual == expected
    assert parse.call_count == 2
    assert [source_snapshot(path) for path in (source, other)] == before
    forbidden.assert_not_called()


@pytest.mark.parametrize(
    "kind",
    [
        "escape",
        "dangling",
        "fifo",
        "source-link",
        "ancestor-link",
        "copy-sibling",
        "absolute-test-link",
        "multistep",
        "missing-script",
        "windows",
    ],
)
@pytest.mark.parametrize("resources", [False, True])
def test_batch_rejects_invalid_coordinates_but_defers_unused_payload_validation(
    tmp_path: Path, kind: str, resources: bool
) -> None:
    workspace, source = task_copy(tmp_path)
    request = {
        "storage_root": str(tmp_path),
        "task_keys": [source.relative_to(tmp_path).as_posix()],
    }
    expected = compiler.describe_tasks_request(request, resources=resources)
    if kind == "escape":
        (source / "outside").symlink_to(tmp_path)
    elif kind == "dangling":
        (source / "dangling").symlink_to("missing")
    elif kind == "fifo":
        os.mkfifo(source / "fifo")
    elif kind == "source-link":
        alias = workspace / "source/alias"
        alias.symlink_to(source, target_is_directory=True)
        source = alias
    elif kind == "ancestor-link":
        alias = tmp_path / "alias"
        alias.symlink_to(workspace, target_is_directory=True)
        source = alias / "source/task"
    elif kind == "copy-sibling":
        (source / "payload").write_text("outside tests subtree")
        (source / "tests/alias").symlink_to("../payload")
    elif kind == "absolute-test-link":
        (source / "tests/alias").symlink_to(source / "tests/test.sh")
    elif kind == "multistep":
        (source / "task.toml").write_text('[[steps]]\nname="first"\n')
        (source / "steps/first").mkdir(parents=True)
        (source / "steps/first/instruction.md").write_text("Step")
    elif kind == "missing-script":
        (source / "tests/test.sh").unlink()
    else:
        (source / "task.toml").write_text('[environment]\nos="windows"\ndocker_image="r.io/main:1"\n')
        (source / "tests/test.bat").write_text("exit 0")
    request["task_keys"] = [source.relative_to(tmp_path).as_posix()]
    if kind in {"escape", "dangling", "fifo", "copy-sibling", "absolute-test-link"}:
        assert compiler.describe_tasks_request(request, resources=resources) == expected
        assert not (workspace / "public").exists()
        assert not (workspace / "private-tests").exists()
    else:
        with pytest.raises((ValueError, OSError)):
            compiler.describe_tasks_request(request, resources=resources)
        assert not (workspace / "public").exists()
        assert not (workspace / "private-tests").exists()


@pytest.mark.parametrize("key", ["../outside", "/outside", "a//b", "a/./b", "share/t", "launches/t", "a\\b", "a\x7fb"])
def test_batch_reader_revalidates_canonical_source_keys(tmp_path: Path, key: str) -> None:
    with pytest.raises(ValueError):
        compiler.describe_tasks_request({"storage_root": str(tmp_path), "task_keys": [key]})


def batch_request(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, resources: bool = False) -> tuple[Path, Path, str]:
    _, source = task_copy(tmp_path)
    key = source.relative_to(tmp_path).as_posix()
    workspace = tmp_path / "launches/test/metadata/unique"
    workspace.mkdir(parents=True)
    request = workspace / "request.json"
    request.write_text(
        json.dumps(
            {
                "storage_root": str(tmp_path),
                "workspace": str(workspace),
                "task_keys": [key],
            }
        )
    )
    monkeypatch.setattr(
        sys, "argv", ["workspace", "--describe-task-resources" if resources else "--describe-tasks", str(request)]
    )
    return request, workspace, key


@pytest.mark.parametrize("resources", [False, True])
def test_batch_entrypoint_writes_only_bounded_metadata_result(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, resources: bool
) -> None:
    _, workspace, key = batch_request(tmp_path, monkeypatch, resources)
    compiler.main()
    output = workspace / "result.json"
    result = json.loads(output.read_bytes())
    assert set(result) == {key}
    if resources:
        assert result[key] == {"main": {"main": {}}}  # Missing declarations are not invented.
    else:
        assert set(result[key]) == {"instruction", "task_config"}
        assert result[key]["instruction"] == "Solve the task.\n"
    assert stat.S_IMODE(output.stat().st_mode) == 0o600
    assert sorted(path.name for path in workspace.iterdir()) == ["request.json", "result.json"]


@pytest.mark.parametrize(
    "failure", ["count", "request-bytes", "output-bytes", "later-task", "output-path", "output-symlink"]
)
@pytest.mark.parametrize("resources", [False, True])
def test_batch_entrypoint_limits_and_failures_publish_no_partial_result(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, failure: str, resources: bool
) -> None:
    path, workspace, key = batch_request(tmp_path, monkeypatch, resources)
    request = json.loads(path.read_bytes())
    if failure == "count":
        request["task_keys"] = [key] * 129
    elif failure == "request-bytes":
        monkeypatch.setattr(compiler, "METADATA_REQUEST_BYTES", path.stat().st_size - 1)
    elif failure == "output-bytes":
        monkeypatch.setattr(compiler, "METADATA_RESULT_BYTES", 10)
    elif failure == "later-task":
        request["task_keys"].append("missing/task")
    elif failure == "output-path":
        request["workspace"] = str(tmp_path / key)
    else:
        alias = tmp_path / "launches/test/metadata/alias"
        alias.symlink_to(workspace, target_is_directory=True)
        request["workspace"] = str(alias)
        path = alias / "request.json"
        monkeypatch.setattr(
            sys, "argv", ["workspace", "--describe-task-resources" if resources else "--describe-tasks", str(path)]
        )
    path.write_text(json.dumps(request))
    with pytest.raises((ValueError, OSError)):
        compiler.main()
    assert not (workspace / "result.json").exists()
    assert not (workspace / "result.tmp").exists()
    assert not (tmp_path / key / "result.json").exists()


@pytest.mark.asyncio
@pytest.mark.parametrize("compose", [False, True])
@pytest.mark.parametrize("mode", ["shared", "separate", "bundled"])
async def test_resource_reader_matches_compiled_reservations(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, compose: bool, mode: str
) -> None:
    from unittest.mock import AsyncMock

    from flotilla import Flotilla
    from flotilla.compose import parse as parse_module
    from flotilla.config import ResourceDefaults
    from flotilla.integrations.harbor_source import task_environments
    from flotilla.testing.config import config

    toml = "cpus=3\nmemory_mb=768\n"
    if mode != "shared":
        toml += '[verifier]\nenvironment_mode="separate"\n'
    if mode == "bundled":
        toml += '[verifier.environment]\ndocker_image="r.io/verifier:1"\ncpus=4\n'
    _, source = task_copy(tmp_path, toml=toml)
    if compose:
        (source / "environment/docker-compose.yaml").write_text(
            'services:\n  main:\n    cpus: "${CPU}"\n    mem_limit: 1Gi\n'
            '  db:\n    image: r.io/db:1\n    cpus: "${CPU}"\n    mem_limit: 5Gi\n'
            "    env_file: settings.env\n  other:\n    image: r.io/other:1\n"
        )
        (source / "environment/settings.env").write_text("X=env-file\n")
        if mode == "separate":
            (source / "tests/docker-compose.yaml").write_text("services:\n  main:\n    image: r.io/verifier:1\n")
    calls: list[Path] = []
    original_parse = parse_module.load_project

    def record(project_dir: Path, **kwargs: Any) -> Any:
        """Observe which directory each project is read from; the parse itself is the real one."""
        calls.append(project_dir)
        if not project_dir.is_relative_to(source):
            # The private copy holds the environment and nothing the agent authored.
            assert not (project_dir.parent / "instruction.md").exists()
            if mode == "shared":
                assert not (project_dir.parent / "tests").exists()
            if project_dir.name == "environment":
                assert (project_dir / "settings.env").read_text() == "X=env-file\n"
        assert kwargs["environment"]["CPU"] == "2"
        return original_parse(project_dir, **kwargs)

    monkeypatch.setattr(parse_module, "load_project", record)
    policy = ResourceDefaults.model_validate(
        {
            "default": {"cpu": "1", "memory": "2Gi"},
            "services": {"db": {"cpu": "7", "memory": "8Gi"}},
            "tasks": {"task": {"services": {"db": {"cpu": "9", "memory": "10Gi"}}}},
        }
    )
    fleet = Flotilla(deployment=config().model_copy(update={"resources": policy}))
    environments = task_environments(source, compose_environment={"CPU": "2"})
    roles = ["main"] if mode == "shared" else ["main", "verifier"]
    expected = {}
    for role, environment in zip(roles, environments, strict=True):
        expected[role] = fleet.reservation(plan_environment(environment, resources=policy))
    before = source_snapshot(source)
    calls.clear()
    forbidden = Mock(side_effect=AssertionError("resource inspection must not build, export or run a tool"))
    monkeypatch.setattr(subprocess, "run", forbidden)
    if not compose:
        monkeypatch.setattr(shutil, "copytree", forbidden)
    key = source.relative_to(tmp_path).as_posix()
    request = {
        "storage_root": str(tmp_path),
        "task_keys": [key, key],
        "runtime_params": {"CPU": "2"},
    }
    raw = json.loads(compiler.describe_tasks_request(request, resources=True))
    assert set(raw) == {key}
    assert raw[key]["main"]["main"] == {"cpu": "3", "memory": "768Mi"}
    if compose:
        assert raw[key]["main"]["db"] == {"cpu": "2", "memory": "5Gi"}
        # Read from a private copy that is gone afterwards, never from the canonical source.
        assert calls and all(not path.is_relative_to(source) and not path.exists() for path in calls)
    else:
        # Nothing to copy without a Compose file, so the task is read where it lies.
        assert calls and all(path.is_relative_to(source) for path in calls)
    assert source_snapshot(source) == before
    forbidden.assert_not_called()
    manager = SimpleNamespace(workspaces=SimpleNamespace(describe_task_resources=AsyncMock(return_value=raw)))
    monkeypatch.setattr(fleet, "start", AsyncMock())

    async def call(action: Any, **kwargs: Any) -> Any:
        return await action(manager)

    monkeypatch.setattr(fleet, "_call", call)
    assert await fleet.describe_task_resources([key], runtime_params={"CPU": "2"}) == {key: expected}


def test_resource_private_copy_preserves_internal_relative_links(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from flotilla.compose import parse as parse_module

    _, source = task_copy(tmp_path)
    (source / "payload").write_text("linked payload")
    (source / "environment/alias").symlink_to("../payload")
    (source / "environment/docker-compose.yaml").write_text("services:\n  main:\n    image: r.io/main:1\n")
    before = source_snapshot(source)
    roots = []
    original_parse = parse_module.load_project

    def record(project_dir: Path, **kwargs: Any) -> Any:
        roots.append(project_dir)
        assert (project_dir / "alias").is_symlink()
        assert os.readlink(project_dir / "alias") == "../payload"
        assert (project_dir / "alias").read_text() == "linked payload"
        return original_parse(project_dir, **kwargs)

    monkeypatch.setattr(parse_module, "load_project", record)
    key = source.relative_to(tmp_path).as_posix()
    assert json.loads(
        compiler.describe_tasks_request(
            {
                "storage_root": str(tmp_path),
                "task_keys": [key],
            },
            resources=True,
        )
    ) == {key: {"main": {"main": {}}}}
    assert roots and all(not path.exists() for path in roots)
    assert source_snapshot(source) == before


@pytest.mark.parametrize(
    "failure", ["unreadable", "invalid-resource", "include", "extends", "env-escape", "copy", "copy-stale"]
)
def test_resource_compose_failure_cleans_private_tree_and_preserves_source(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, failure: str
) -> None:
    _, source = task_copy(tmp_path)
    text = "services:\n  main:\n    image: r.io/main:1\n"
    if failure == "unreadable":
        text = "services: [unclosed\n"
    elif failure == "invalid-resource":
        text += "    cpus: bad\n"
    elif failure == "include":
        text += "include: [extra.yaml]\n"
    elif failure == "extends":
        text += "    extends: {file: extra.yaml, service: main}\n"
    elif failure == "env-escape":
        text += "    env_file: ../../outside\n"
    (source / "environment/docker-compose.yaml").write_text(text)
    before = source_snapshot(source)
    private: list[Path] = []
    original_copy = shutil.copytree

    def copy(src: Path, dst: Path, *args: Any, **kwargs: Any) -> Path:
        private.append(Path(dst))
        if failure in ("copy", "copy-stale"):
            assert len(private) == 1  # never replay a partially written destination
            Path(dst).mkdir()
            raise OSError(errno.ESTALE if failure == "copy-stale" else errno.EIO, "copy failure")
        return Path(original_copy(src, dst, *args, **kwargs))

    monkeypatch.setattr(shutil, "copytree", copy)
    with pytest.raises((ValueError, OSError)):
        compiler.describe_tasks_request(
            {
                "storage_root": str(tmp_path),
                "task_keys": [source.relative_to(tmp_path).as_posix()],
            },
            resources=True,
        )
    assert private and all(not path.exists() for path in private)
    assert source_snapshot(source) == before
