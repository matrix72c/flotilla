"""Native Harbor description and management compiler contracts, no real platform/tools."""

from __future__ import annotations

import errno
import json
import os
import shutil
import stat
import subprocess
import sys
from copy import deepcopy
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import Mock

import pytest

pytest.importorskip("harbor")

from flotilla.build.driver import BuildContext, build_task
from flotilla.compose.adapter import COMPOSE_VERSION
from flotilla.integrations import workspace as compiler
from flotilla.testing.build import FakeBuilder, _ctx


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


def request_for(workspace: Path, source: Path, **overrides: Any) -> dict[str, Any]:
    return {
        "workspace": str(workspace),
        "source": str(source),
        "task_id": source.name,
        "workspace_key": "attempts/unique",
        "capabilities": {"fixture": True},
        "deployment": {"opensandbox": {"capabilities": "/original/evidence.json"}},
        "runtime_params": {"SHARED": "request", "RUNTIME": "value"},
        **overrides,
    }


def setup_compile(
    monkeypatch: pytest.MonkeyPatch, workspace: Path
) -> tuple[SimpleNamespace, Mock, BuildContext, Mock, Mock, list[tuple[list[str], dict[str, Any]]]]:
    cfg = SimpleNamespace(
        prepare=SimpleNamespace(
            compose_command=("compose-tool",), compose_version=COMPOSE_VERSION, docker_command=("docker-tool",)
        ),
        runtime_params={"SHARED": "deployment", "DEFAULT": "value"},
        load_report=Mock(return_value={"fixture": True}),
        check_against=Mock(),
    )
    validate = Mock(return_value=cfg)
    monkeypatch.setattr(compiler, "FlotillaConfig", SimpleNamespace(model_validate=validate))
    context = _ctx()
    docker_context = Mock(return_value=context)
    sweep = Mock(return_value=[])
    monkeypatch.setattr(compiler, "docker_context", docker_context)
    monkeypatch.setattr(compiler, "sweep_journal", sweep)
    calls = []

    def compose_run(args: list[str], **kwargs: Any) -> subprocess.CompletedProcess[str]:
        calls.append((args, kwargs))
        if args[-2:] == ["version", "--short"]:
            return subprocess.CompletedProcess(args, 0, COMPOSE_VERSION, "")
        # Official config mock only: no actual CLI has been installed or exercised.
        assert args[0] == "compose-tool"
        assert args[-4:] == ["config", "--format", "json", "--no-path-resolution"]
        root = kwargs["cwd"]
        mounts = [{"type": "bind", "source": "./data", "target": "/data"}] if (root / "data").exists() else []
        document = {
            "name": "flotilla",
            "services": {
                "main": {
                    "image": "r.io/main:1",
                    "command": ["sh", "-c", "sleep infinity"],
                    "volumes": mounts,
                }
            },
        }
        return subprocess.CompletedProcess(args, 0, json.dumps(document), "")

    # describe once before replacing subprocess.run, so real cp is restricted to
    # a generated local fixture. compile_request reuses and validates that copy.
    compiler.describe(workspace / "source/task", workspace)
    monkeypatch.setattr("flotilla.compose.adapter.subprocess.run", compose_run)
    return cfg, validate, context, docker_context, sweep, calls


@pytest.fixture
def read_clock(monkeypatch: pytest.MonkeyPatch) -> SimpleNamespace:
    clock = SimpleNamespace(now=0.0, delays=[], oversleep=0.0)

    def sleep(delay: float) -> None:
        clock.delays.append(delay)
        clock.now += delay + clock.oversleep

    monkeypatch.setattr(compiler, "time", SimpleNamespace(monotonic=lambda: clock.now, sleep=sleep))
    return clock


@pytest.mark.parametrize("fault", ["root", "ancestor", "lstat", "scandir", "instruction"])
@pytest.mark.parametrize("resources", [False, True])
def test_stale_native_reads_recover_without_changing_metadata(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, read_clock: SimpleNamespace, fault: str, resources: bool
) -> None:
    from harbor.models.task import task as task_module

    _, source = task_copy(tmp_path)
    key = source.relative_to(tmp_path).as_posix()
    request = {
        "storage_root": str(tmp_path),
        "task_keys": [key],
        "compose_command": ["unused"],
        "compose_version": COMPOSE_VERSION,
    }
    expected = compiler.describe_tasks_request(request, resources=resources)
    task = Mock(wraps=task_module.Task)
    monkeypatch.setattr(task_module, "Task", task)
    target = {
        "root": tmp_path,
        "ancestor": source.parent,
        "lstat": source / "tests/test.sh",
        "scandir": source / "tests",
        "instruction": source / "instruction.md",
    }[fault]
    owner, method = (
        (os, "scandir") if fault == "scandir" else (Path, "read_text" if fault == "instruction" else "lstat")
    )
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
    if fault == "instruction":
        assert task.call_count >= 2  # no failed Task/cached_property is reused


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


def test_enumeration_permission_error_cannot_hide_a_subtree(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, read_clock: SimpleNamespace
) -> None:
    _, source = task_copy(tmp_path)
    original = os.scandir

    def scan(path: Any) -> Any:
        if Path(path) == source / "tests":
            raise PermissionError(errno.EACCES, "injected enumeration failure")
        return original(path)

    monkeypatch.setattr(os, "scandir", scan)
    with pytest.raises(PermissionError):
        compiler.describe_tasks_request(
            {"storage_root": str(tmp_path), "task_keys": [source.relative_to(tmp_path).as_posix()]}
        )
    assert not read_clock.delays


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


def test_compilation_recovers_reads_without_replaying_side_effects(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, read_clock: SimpleNamespace
) -> None:
    workspace, source = task_copy(tmp_path)
    _, _, _, docker_context, sweep, calls = setup_compile(monkeypatch, workspace)
    parse = Mock(side_effect=[OSError(errno.ESTALE, "injected"), compiler._describe_source(source)])
    copy = Mock(wraps=compiler._copy_tests)
    build = Mock(wraps=build_task)
    monkeypatch.setattr(compiler, "_describe_source", parse)
    monkeypatch.setattr(compiler, "_copy_tests", copy)
    monkeypatch.setattr(compiler, "build_task", build)
    compiler.compile_request(request_for(workspace, source))
    assert parse.call_count == 2 and read_clock.delays == [0.5]
    copy.assert_called_once()
    build.assert_called_once()
    docker_context.assert_called_once()
    sweep.assert_called_once()
    assert len(calls) == 2  # one Compose version check and one config invocation


def test_test_copy_failure_is_not_replayed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, read_clock: SimpleNamespace
) -> None:
    workspace, source = task_copy(tmp_path)
    copy = Mock(side_effect=OSError(errno.ESTALE, "copy failure, not a safe read"))
    monkeypatch.setattr(compiler, "_copy_tests", copy)
    with pytest.raises(OSError):
        compiler.describe(source, workspace)
    copy.assert_called_once()
    assert not read_clock.delays


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


def test_describe_uses_native_harbor_without_compose_or_build(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    workspace, source = task_copy(tmp_path)
    forbidden = Mock(side_effect=AssertionError("description must not compile"))
    monkeypatch.setattr(compiler, "ComposeConfigAdapter", forbidden)
    monkeypatch.setattr(compiler, "docker_context", forbidden)
    monkeypatch.setattr(compiler, "build_task", forbidden)
    result = compiler.compile_request(request_for(workspace, source, describe=True))
    assert result["task_id"] == "task"
    assert result["instruction"] == "Solve the task.\n"
    assert result["definitions"]["main"]["directory"] == "environment"
    assert result["tests"]["script"] == "test.sh"
    assert "environments" not in result
    assert stat.S_IMODE((source / "tests/test.sh").stat().st_mode) == 0o640
    assert stat.S_IMODE((workspace / "private-tests/test.sh").stat().st_mode) == 0o751
    assert stat.S_IMODE((workspace / "public/instruction.md").stat().st_mode) == 0o644
    assert not (workspace / "generated").exists()
    forbidden.assert_not_called()


def test_describe_native_bundled_verifier_needs_no_host_script(tmp_path: Path) -> None:
    workspace, source = task_copy(
        tmp_path,
        script=False,
        toml='[verifier]\nenvironment_mode="separate"\n[verifier.environment]\ndocker_image="r.io/verifier:1"\n',
    )
    result = compiler.describe(source, workspace)
    assert list(result["definitions"]) == ["main", "verifier"]
    assert result["definitions"]["verifier"]["bundled_tests"] is True
    assert result["definitions"]["verifier"]["config"]["docker_image"] == "r.io/verifier:1"
    assert result["tests"]["script"] is None
    assert not (workspace / "private-tests").exists()


def test_describe_rejects_multistep(tmp_path: Path) -> None:
    workspace, source = task_copy(tmp_path, toml='[[steps]]\nname="first"\n')
    (source / "steps/first").mkdir(parents=True)
    (source / "steps/first/instruction.md").write_text("Step")
    with pytest.raises(ValueError, match="multi-step"):
        compiler.describe(source, workspace)


def test_native_task_validation_is_not_bypassed(tmp_path: Path) -> None:
    workspace, source = task_copy(tmp_path, script=False)
    with pytest.raises(FileNotFoundError, match=r"test\.sh"):
        compiler.describe(source, workspace)


def test_private_test_copy_preserves_links_modes_and_source(tmp_path: Path) -> None:
    workspace, source = task_copy(tmp_path)
    data = source / "tests/data"
    data.write_text("private")
    data.chmod(0o600)
    (source / "tests/alias").symlink_to("data")
    os.link(data, source / "tests/hardlink")
    compiler.describe(source, workspace)
    copied = workspace / "private-tests"
    assert (copied / "alias").is_symlink() and os.readlink(copied / "alias") == "data"
    assert (copied / "data").stat().st_ino == (copied / "hardlink").stat().st_ino
    assert (copied / "data").stat().st_ino != data.stat().st_ino
    assert stat.S_IMODE((copied / "data").stat().st_mode) == 0o600


@pytest.mark.parametrize("kind", ["escape", "dangling", "fifo", "source-link", "copy-sibling", "absolute-test-link"])
def test_confined_source_and_relocatable_test_copy(tmp_path: Path, kind: str) -> None:
    workspace, source = task_copy(tmp_path)
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
    elif kind == "copy-sibling":
        (source / "payload").write_text("outside tests subtree")
        (source / "tests/alias").symlink_to("../payload")
    else:
        (source / "tests/alias").symlink_to(source / "tests/test.sh")
    with pytest.raises((ValueError, OSError)):
        compiler.compile_request(request_for(workspace, source, describe=True))


@pytest.mark.parametrize("target", ["public", "private-tests"])
def test_description_rejects_output_directory_symlinks(tmp_path: Path, target: str) -> None:
    workspace, source = task_copy(tmp_path)
    outside = tmp_path / "outside"
    outside.mkdir()
    (workspace / target).symlink_to(outside, target_is_directory=True)
    with pytest.raises(ValueError, match="symlink"):
        compiler.describe(source, workspace)
    assert list(outside.iterdir()) == []


def test_compile_uses_official_adapter_and_attempt_file_identity(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    workspace, source = task_copy(tmp_path)
    (source / "environment/data").mkdir()
    (source / "environment/data/value").write_text("seed")
    (source / "environment/data/value").chmod(0o640)
    cfg, validate, context, docker_context, sweep, calls = setup_compile(monkeypatch, workspace)
    restore = Mock(wraps=compiler.restore_identities)
    monkeypatch.setattr(compiler, "restore_identities", restore)
    request = request_for(workspace, source)
    original = deepcopy(request)
    result = compiler.compile_request(request)
    assert request == original  # compiler must not rewrite caller deployment config
    cfg.check_against.assert_called_once_with({"fixture": True})
    assert validate.call_args.args[0]["opensandbox"]["capabilities"] == str(workspace / "capabilities.json")
    assert stat.S_IMODE((workspace / "capabilities.json").stat().st_mode) == 0o600
    assert len(calls) == 2  # native main environment normalized exactly once
    assert calls[-1][1]["env"]["SHARED"] == "request"
    assert calls[-1][1]["env"]["DEFAULT"] == "value"
    assert calls[-1][1]["env"]["RUNTIME"] == "value"
    manifest = result["environments"]["main"]
    assert manifest["task"] == "task"
    assert manifest["task_files"].startswith("attempts/unique/generated/main/")
    assert manifest["units"]["main"]["mounts"][0]["scope"] == "trial"
    restore.assert_called_once()
    exported = restore.call_args.args[0]
    assert stat.S_IMODE((exported / "binds/0/value").stat().st_mode) == 0o640
    assert isinstance(context.builder, FakeBuilder)
    assert context.builder.requests == []  # no mounted-data image baking
    docker_context.assert_called_once()
    sweep.assert_called_once_with(("docker-tool",), workspace / "generated/containers")


def test_compile_identity_is_unique_even_for_repeated_workspace(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    workspace, source = task_copy(tmp_path)
    setup_compile(monkeypatch, workspace)
    build = Mock(wraps=build_task)
    monkeypatch.setattr(compiler, "build_task", build)
    request = request_for(workspace, source)
    compiler.compile_request(request)
    compiler.compile_request(request)
    identities = [call.kwargs["identity"] for call in build.call_args_list]
    assert len(set(identities)) == 2
    assert all(identity.endswith("--main") for identity in identities)
    assert all(call.args[0].name == "task" for call in build.call_args_list)


@pytest.mark.parametrize(
    "failure", ["build", "context", "restore", "cleanup", "build-and-cleanup", "cleanup-exception"]
)
def test_compiler_cleanup_preserves_primary_failure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, failure: str
) -> None:
    workspace, source = task_copy(tmp_path)
    if failure == "restore":
        (source / "environment/data").mkdir()
    _, _, _, docker_context, sweep, _ = setup_compile(monkeypatch, workspace)
    if failure in ("build", "build-and-cleanup"):
        monkeypatch.setattr(compiler, "build_task", Mock(side_effect=ValueError("primary build error")))
    if failure == "context":
        docker_context.side_effect = ValueError("primary context error")
    if failure == "restore":
        monkeypatch.setattr(compiler, "restore_identities", Mock(side_effect=ValueError("primary restore error")))
    if failure in ("cleanup", "build-and-cleanup"):
        sweep.return_value = ["leftover-container"]
    if failure == "cleanup-exception":
        monkeypatch.setattr(compiler, "build_task", Mock(side_effect=ValueError("primary build error")))
        sweep.side_effect = OSError("cleanup storage error")
    expected = RuntimeError if failure == "cleanup" else ValueError
    with pytest.raises(expected) as error:
        compiler.compile_request(request_for(workspace, source))
    sweep.assert_called_once()
    if failure in ("build-and-cleanup", "cleanup-exception"):
        assert str(error.value) == "primary build error"
        assert any("cleanup" in note for note in error.value.__notes__)


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
    one, source = task_copy(tmp_path / "one", toml=toml, script=mode != "bundled")
    two, other = task_copy(tmp_path / "two", toml=toml, script=mode != "bundled")
    (other / "instruction.md").write_text("Different same-basename task.\n")
    before = [source_snapshot(path) for path in (source, other)]
    expected = {}
    keys = [path.relative_to(tmp_path).as_posix() for path in (source, other)]
    for key, path, workspace in zip(keys, (source, other), (one, two), strict=True):
        description = compiler.describe(path, workspace)
        expected[key] = {name: description[name] for name in ("instruction", "task_config")}
    forbidden = Mock(side_effect=AssertionError("batch metadata must not stage, chmod or compile"))
    for name in ("_copy_tests", "_directory", "compile_request", "build_task"):
        monkeypatch.setattr(compiler, name, forbidden)
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
def test_batch_rejects_unsupported_native_trees_and_symlink_ancestors(
    tmp_path: Path, kind: str, resources: bool
) -> None:
    workspace, source = task_copy(tmp_path)
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
    with pytest.raises((ValueError, OSError)):
        compiler.describe_tasks_request(
            {"storage_root": str(tmp_path), "task_keys": [source.relative_to(tmp_path).as_posix()]}, resources=resources
        )
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
                "compose_command": ["docker", "compose"],
                "compose_version": COMPOSE_VERSION,
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
@pytest.mark.parametrize("image", [False, True])
async def test_resource_reader_matches_compiled_reservations(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, compose: bool, mode: str, image: bool
) -> None:
    from unittest.mock import AsyncMock

    from flotilla import Flotilla
    from flotilla.compose.adapter import ComposeConfigAdapter
    from flotilla.config import ResourceDefaults
    from flotilla.integrations.harbor_source import task_environments
    from flotilla.manifest import Manifest
    from flotilla.prepared import PreparedEnvironment
    from flotilla.testing.config import config

    toml = "cpus=3\nmemory_mb=768\n"
    if mode != "shared":
        toml += '[verifier]\nenvironment_mode="separate"\n'
    if mode == "bundled":
        toml += '[verifier.environment]\ndocker_image="r.io/verifier:1"\ncpus=4\n'
    _, source = task_copy(tmp_path, toml=toml)
    if not image:
        text = (source / "task.toml").read_text().replace('docker_image="r.io/main:1"\n', "")
        (source / "task.toml").write_text(text)
        (source / "environment/Dockerfile").write_text("FROM r.io/main:1\n")
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

    def normalize(args: list[str], cwd: Path, env: dict[str, str]) -> str:
        # Mock only the official CLI output, not the task/native resource rules.
        calls.append(cwd)
        if not cwd.is_relative_to(source):
            assert not (cwd.parent / "instruction.md").exists()
            if mode == "shared":
                assert not (cwd.parent / "tests").exists()
            if cwd.name == "environment":
                assert (cwd / "settings.env").read_text() == "X=env-file\n"
        if args[-2:] == ["version", "--short"]:
            return COMPOSE_VERSION
        assert args[-4:] == ["config", "--format", "json", "--no-path-resolution"]
        assert env["CPU"] == "2"
        services: dict[str, Any] = {"main": {"image": "r.io/main:1"}}
        if (cwd / "settings.env").exists():
            services["main"].update(cpus="2", mem_limit="1Gi")
            services["db"] = {"image": "r.io/db:1", "cpus": "2", "mem_limit": "5Gi"}
            services["other"] = {"image": "r.io/other:1"}
        return json.dumps({"services": services})

    monkeypatch.setattr(ComposeConfigAdapter, "_run", staticmethod(normalize))
    policy = ResourceDefaults.model_validate(
        {
            "default": {"cpu": "1", "memory": "2Gi"},
            "services": {"db": {"cpu": "7", "memory": "8Gi"}},
            "tasks": {"task": {"services": {"db": {"cpu": "9", "memory": "10Gi"}}}},
        }
    )
    fleet = Flotilla(deployment=config().model_copy(update={"resources": policy}))
    environments = task_environments(source, compose_adapter=ComposeConfigAdapter(), compose_environment={"CPU": "2"})
    roles = ["main"] if mode == "shared" else ["main", "verifier"]
    expected = {}
    for role, environment in zip(roles, environments, strict=True):
        outcome = build_task(environment, tmp_path / "built" / role, _ctx())
        assert outcome.manifest_path is not None, outcome.reason
        expected[role] = fleet.reservation(PreparedEnvironment(Manifest.load_json(outcome.manifest_path.read_bytes())))
    before = source_snapshot(source)
    calls.clear()
    forbidden = Mock(side_effect=AssertionError("resource inspection must not build/export/copy tests"))
    for name in (
        "build_task",
        "docker_context",
        "_copy_tests",
        "compile_request",
        "restore_identities",
        "sweep_journal",
    ):
        monkeypatch.setattr(compiler, name, forbidden)
    monkeypatch.setattr(subprocess, "run", forbidden)
    if not compose:
        monkeypatch.setattr(shutil, "copytree", forbidden)
    key = source.relative_to(tmp_path).as_posix()
    request = {
        "storage_root": str(tmp_path),
        "task_keys": [key, key],
        "runtime_params": {"CPU": "2"},
        "compose_command": ["docker", "compose"],
        "compose_version": COMPOSE_VERSION,
    }
    raw = json.loads(compiler.describe_tasks_request(request, resources=True))
    assert set(raw) == {key}
    assert raw[key]["main"]["main"] == {"cpu": "3", "memory": "768Mi"}
    if compose:
        assert raw[key]["main"]["db"] == {"cpu": "2", "memory": "5Gi"}
        assert calls and all(not path.is_relative_to(source) and not path.exists() for path in calls)
    else:
        assert calls == []  # No Compose subprocess, even for Dockerfile/separate verifier.
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
    from flotilla.compose.adapter import ComposeConfigAdapter

    _, source = task_copy(tmp_path)
    (source / "payload").write_text("linked payload")
    (source / "environment/alias").symlink_to("../payload")
    (source / "environment/docker-compose.yaml").write_text("services:\n  main:\n    image: r.io/main:1\n")
    before = source_snapshot(source)
    roots = []

    def run(args: list[str], cwd: Path, env: dict[str, str]) -> str:
        roots.append(cwd)
        assert (cwd / "alias").is_symlink()
        assert os.readlink(cwd / "alias") == "../payload"
        assert (cwd / "alias").read_text() == "linked payload"
        if args[-2:] == ["version", "--short"]:
            return COMPOSE_VERSION
        return json.dumps({"services": {"main": {"image": "r.io/main:1"}}})

    monkeypatch.setattr(ComposeConfigAdapter, "_run", staticmethod(run))
    key = source.relative_to(tmp_path).as_posix()
    assert json.loads(
        compiler.describe_tasks_request(
            {
                "storage_root": str(tmp_path),
                "task_keys": [key],
                "compose_command": ["docker", "compose"],
                "compose_version": COMPOSE_VERSION,
            },
            resources=True,
        )
    ) == {key: {"main": {"main": {}}}}
    assert roots and all(not path.exists() for path in roots)
    assert source_snapshot(source) == before


@pytest.mark.parametrize(
    "failure", ["cli", "invalid-resource", "include", "extends", "env-escape", "copy", "copy-stale"]
)
def test_resource_compose_failure_cleans_private_tree_and_preserves_source(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, failure: str
) -> None:
    from flotilla.compose.adapter import ComposeConfigAdapter

    _, source = task_copy(tmp_path)
    text = "services:\n  main:\n    image: r.io/main:1\n"
    if failure == "include":
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

    def run(args: list[str], cwd: Path, env: dict[str, str]) -> str:
        if failure == "cli":
            raise ValueError("CLI failure")
        if args[-2:] == ["version", "--short"]:
            return COMPOSE_VERSION
        return json.dumps({"services": {"main": {"image": "r.io/main:1", "cpus": "bad"}}})

    monkeypatch.setattr(shutil, "copytree", copy)
    monkeypatch.setattr(ComposeConfigAdapter, "_run", staticmethod(run))
    with pytest.raises((ValueError, OSError)):
        compiler.describe_tasks_request(
            {
                "storage_root": str(tmp_path),
                "task_keys": [source.relative_to(tmp_path).as_posix()],
                "compose_command": ["docker", "compose"],
                "compose_version": COMPOSE_VERSION,
            },
            resources=True,
        )
    assert private and all(not path.exists() for path in private)
    assert source_snapshot(source) == before


def test_restore_identities_uses_numeric_metadata_and_does_not_chmod_links(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    files = tmp_path / "files"
    files.mkdir()
    (files / "data").write_text("payload")
    (files / "link").symlink_to("data")
    (tmp_path / "file-identities.json").write_text(json.dumps({"data": [123, 456, 0o640], "link": [12, 34, 0o777]}))
    chown = Mock()
    monkeypatch.setattr(os, "chown", chown)
    compiler.restore_identities(files)
    assert stat.S_IMODE((files / "data").stat().st_mode) == 0o640
    chown.assert_any_call(files / "data", 123, 456, follow_symlinks=False)
    chown.assert_any_call(files / "link", 12, 34, follow_symlinks=False)
    assert (files / "link").is_symlink()
    alias = tmp_path / "alias"
    alias.symlink_to(files, target_is_directory=True)
    with pytest.raises(ValueError, match="real directory"):
        compiler.restore_identities(alias)
