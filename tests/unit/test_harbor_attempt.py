"""Platform workspace Harbor contract, without a local task tree or cloud calls."""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, Mock

import pytest
from harbor.models.task.config import TaskConfig
from harbor.models.trial.paths import TrialPaths

from flotilla.integrations.harbor_attempt import HarborAttempt, WorkspaceVerifier, task_key
from flotilla.integrations.harbor_source_reader import metadata_identity
from flotilla.platform.base import SharedVolume


def metadata(
    *, separate: bool = False, bundled: bool = False, mount: dict[str, object] | None = None
) -> dict[str, Any]:
    unit = {
        "image": "r.io/task@sha256:" + "a" * 64,
        "resources": {},
        "context": {"env": {}, "uid": 0, "gid": 0, "cwd": "/"},
        "command": None,
        "mounts": [] if mount is None else [mount],
    }
    manifest = {"task": "task", "agent_service": "main", "units": {"main": unit}}
    result: dict[str, Any] = {
        "task_config": TaskConfig().model_dump(mode="json"),
        "instruction": "real instruction",
        "tests": {"script": "test.sh", "directory": "private-tests"},
        "definitions": {"main": {"directory": "environment", "config": {}, "bundled_tests": False}},
        "environments": {"main": manifest},
    }
    if separate:
        result["definitions"]["verifier"] = {"directory": "tests", "config": {}, "bundled_tests": bundled}
        result["environments"]["verifier"] = manifest
    return result


def fleet_for(data: dict[str, Any]) -> tuple[Mock, SimpleNamespace]:
    workspace = SimpleNamespace(
        id="random-workspace",
        key="launches/random/workspaces/random-workspace",
        task_id="same-name",
        task_key="dataset-a/same-name",
        source_key="launches/random/workspaces/random-workspace/source/same-name",
        prepare=AsyncMock(return_value=data),
        mkdir=AsyncMock(),
        copy=AsyncMock(),
        copy_contents=AsyncMock(),
        close=AsyncMock(),
        write=AsyncMock(),
    )
    fleet = Mock(workspace=AsyncMock(return_value=workspace), open=AsyncMock())
    return fleet, workspace


@pytest.mark.parametrize("key", ["", "/absolute", "../escape", "a/../b", "a//b", "a/./b", "a\\b"])
def test_task_keys_are_confined(key: str) -> None:
    with pytest.raises(ValueError):
        task_key(key)


@pytest.mark.asyncio
async def test_hidden_tests_are_empty_until_grading_and_workspace_outlives_main() -> None:
    fleet, workspace = fleet_for(metadata(separate=True))
    main = SimpleNamespace(release=AsyncMock())
    verifier = SimpleNamespace(release=AsyncMock())
    fleet.open.side_effect = [main, verifier]
    attempt = await HarborAttempt.create(fleet, "dataset-a/same-name")
    assert attempt.task.name == "same-name"
    workspace.copy.assert_not_awaited()  # no GPU instruction transfer or private tree copy/mount
    await attempt.open()
    options = fleet.open.call_args.args[1]
    mounts = options.workspace_mounts["main"]
    assert all("source" not in m.key and "private-tests" not in m.key for m in mounts)
    tests = next(m for m in mounts if m.mount_path == "/tests")
    assert tests.read_only and tests.key == workspace.key + "/tests"
    workspace.copy_contents.assert_not_awaited()
    await attempt.release("main")
    workspace.close.assert_not_awaited()
    await attempt.stage_tests()
    workspace.copy_contents.assert_awaited_once_with("private-tests", "tests")
    await attempt.open("verifier")
    mounts = fleet.open.call_args.args[1].workspace_mounts["main"]
    assert SharedVolume(workspace.key + "/handoff", "/harbor-handoff", True) in mounts
    await attempt.close()
    await attempt.close()
    main.release.assert_awaited_once()
    verifier.release.assert_awaited_once()
    workspace.close.assert_awaited_once()


@pytest.mark.asyncio
async def test_prepare_and_release_failure_always_attempt_workspace_cleanup() -> None:
    fleet, workspace = fleet_for(metadata())
    workspace.prepare.side_effect = ValueError("compile failed")
    with pytest.raises(ValueError, match="compile failed"):
        await HarborAttempt.create(fleet, "dataset/task")
    workspace.close.assert_awaited_once()
    workspace.prepare.side_effect = None
    workspace.close.reset_mock()
    attempt = await HarborAttempt.create(fleet, "dataset/task")
    fleet.open.return_value = SimpleNamespace(release=AsyncMock(side_effect=RuntimeError("release failed")))
    await attempt.open()
    with pytest.raises(RuntimeError, match="release failed"):
        await attempt.close()
    workspace.close.assert_awaited_once()


@pytest.mark.asyncio
@pytest.mark.parametrize("target", ["/", "/tests", "/tests/data", "/harbor-instruction", "/harbor-handoff"])
async def test_owned_mounts_never_shadow_task_mounts(target: str) -> None:
    data = metadata(mount={"scope": "trial", "key": "content", "target": target, "read_only": False})
    fleet, _ = fleet_for(data)
    attempt = await HarborAttempt.create(fleet, "dataset/task")
    with pytest.raises(ValueError, match="overlap"):
        attempt.options("main")
    await attempt.close()


@pytest.mark.asyncio
async def test_bundled_verifier_owns_its_tests_and_rejects_mount_mismatch() -> None:
    fleet, workspace = fleet_for(metadata(separate=True, bundled=True))
    attempt = await HarborAttempt.create(fleet, "dataset/task")
    await attempt.stage_tests()
    workspace.copy_contents.assert_not_awaited()
    assert all(m.mount_path != "/tests" for m in attempt.options("verifier").workspace_mounts["main"])
    await attempt.close()
    data = metadata(
        separate=True, bundled=True, mount={"scope": "trial", "key": "tests", "target": "/tests", "read_only": True}
    )
    fleet, _ = fleet_for(data)
    attempt = await HarborAttempt.create(fleet, "dataset/task")
    with pytest.raises(ValueError, match="bundled"):
        attempt.options("verifier")
    await attempt.close()


@pytest.mark.asyncio
async def test_native_verifier_uses_remote_discovery_and_reward_parser(tmp_path: Path) -> None:
    from harbor.environments.base import ExecResult
    from harbor.models.task.config import TaskOS

    fleet, _ = fleet_for(metadata())
    attempt = await HarborAttempt.create(fleet, "dataset/task")
    paths = TrialPaths(tmp_path / "trial")
    paths.mkdir()
    environment = SimpleNamespace(
        os=TaskOS.LINUX,
        capabilities=SimpleNamespace(mounted=False),
        exec=AsyncMock(return_value=ExecResult(stdout="", stderr="", return_code=0)),
    )

    async def download(source_dir: str, target_dir: Path) -> None:
        Path(target_dir, "reward.txt").write_text("0.75")

    environment.download_dir = download
    verifier = WorkspaceVerifier(attempt=attempt, task=attempt.task, trial_paths=paths, environment=environment)
    with pytest.raises(RuntimeError, match="staged"):
        await verifier.verify()
    await attempt.stage_tests()
    result = await verifier.verify()
    assert result.rewards == {"reward": 0.75}
    assert len(environment.exec.await_args_list) == 1  # chmod was applied platform-side before RO staging
    assert "/tests/test.sh" in environment.exec.call_args.args[0]
    assert not hasattr(attempt.task, "paths")
    await attempt.close()


@pytest.mark.asyncio
async def test_expected_metadata_identity_accepts_canonical_match_before_open() -> None:
    data = metadata()
    expected = metadata_identity({"task_config": {}, "instruction": "# Canary\n\nreal instruction"})
    fleet, workspace = fleet_for(data)
    attempt = await HarborAttempt.create(fleet, "dataset/task", expected_metadata_identity=expected)
    assert attempt.task.instruction == "real instruction"
    workspace.close.assert_not_awaited()
    workspace.mkdir.assert_awaited()
    fleet.open.assert_not_awaited()
    fleet.open.return_value = SimpleNamespace(release=AsyncMock())
    await attempt.open()
    fleet.open.assert_awaited_once()
    await attempt.close()
    workspace.close.assert_awaited_once()


@pytest.mark.asyncio
@pytest.mark.parametrize("changed", ["instruction", "task_config"])
async def test_metadata_mismatch_closes_workspace_before_open(changed: str) -> None:
    data = metadata()
    expected = metadata_identity(data)
    if changed == "instruction":
        data["instruction"] = "different instructions"
    else:
        data["task_config"]["environment"]["cpus"] = 7
    fleet, workspace = fleet_for(data)
    with pytest.raises(ValueError, match="host/platform instruction or task config differs"):
        await HarborAttempt.create(fleet, "dataset/task", expected_metadata_identity=expected)
    workspace.prepare.assert_awaited_once()
    workspace.close.assert_awaited_once()
    workspace.mkdir.assert_not_awaited()
    fleet.open.assert_not_awaited()


@pytest.mark.asyncio
async def test_metadata_mismatch_remains_primary_when_workspace_close_fails() -> None:
    fleet, workspace = fleet_for(metadata())
    workspace.close.side_effect = RuntimeError("close failed")
    with pytest.raises(ValueError, match="host/platform") as caught:
        await HarborAttempt.create(fleet, "dataset/task", expected_metadata_identity="not-the-current-identity")
    assert any("workspace cleanup also failed" in note and "close failed" in note for note in caught.value.__notes__)
    workspace.close.assert_awaited_once()
    fleet.open.assert_not_awaited()
