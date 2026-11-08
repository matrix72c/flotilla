"""Native Harbor description and management compiler contracts, no real platform/tools."""

from __future__ import annotations

import json
import os
import stat
import subprocess
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
