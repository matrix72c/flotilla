"""Shared Harbor grading lifecycle and platform-only artifact handoff."""

from __future__ import annotations

import asyncio
import contextlib
import json
import shlex
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, Mock

import pytest
from harbor.models.task.config import TaskConfig, TaskOS
from harbor.models.trial.config import ArtifactConfig
from harbor.models.trial.paths import TrialPaths

from flotilla.integrations import harbor_verification as shared
from flotilla.integrations.harbor_attempt import HarborAttempt
from flotilla.integrations.harbor_verification import HarborVerification, WorkspaceArtifacts


def artifact_owner() -> Any:
    return SimpleNamespace(
        workspace=SimpleNamespace(write=AsyncMock()), scratch_relative=HarborAttempt.scratch_relative
    )


def artifact_source(*, directory: bool = True) -> Any:
    # Deliberately no upload/download methods: transport must stay platform-side.
    return SimpleNamespace(
        handle=SimpleNamespace(agent=SimpleNamespace(name="agent")),
        service_is_dir=AsyncMock(return_value=directory),
        service_exec=AsyncMock(return_value=SimpleNamespace(return_code=0)),
    )


def artifact_target() -> Any:
    return SimpleNamespace(
        empty_dirs=AsyncMock(), ensure_dirs=AsyncMock(), exec=AsyncMock(return_value=SimpleNamespace(return_code=0))
    )


@pytest.mark.asyncio
async def test_artifact_handoff_preserves_exclusions_services_and_original_destination() -> None:
    owner = artifact_owner()
    handler = WorkspaceArtifacts(
        owner,
        [ArtifactConfig(source="/evidence/data", destination="renamed", exclude=["*.secret"], service="db")],
        "/logs/artifacts",
    )
    source = artifact_source()
    assert handler.sidecar_services() == {"db"}
    await handler.collect(source, {"main"})
    # Harbor always includes the conventional main-service artifact directory.
    assert source.service_exec.await_args.kwargs["service"] is None
    assert "/logs/artifacts" in shlex.split(source.service_exec.await_args.args[0])
    manifest = await handler.collect(source, {"db"})
    command = shlex.split(source.service_exec.await_args.args[0])
    assert "--exclude=*.secret" in command
    assert "/harbor-handoff/1.tar.gz" in command
    assert command[-3:] == ["-C", "/evidence/data", "."]
    assert source.service_exec.await_args.kwargs == {"service": "db", "user": "root", "timeout_sec": 120}
    entry = next(entry for entry in manifest.entries if entry.source == "/evidence/data")
    assert entry.status == "ok" and entry.destination == "artifacts/renamed" and entry.service == "db"
    target = artifact_target()
    await handler.restore(target, "/logs/artifacts")
    target.empty_dirs.assert_any_await(["/evidence/data"], chmod=True)
    command = shlex.split(target.exec.await_args.args[0])
    assert "/harbor-handoff/db/1.tar.gz" in command
    assert "renamed" not in command
    owner.workspace.write.assert_awaited()
    manifest_path, payload = owner.workspace.write.await_args.args
    assert manifest_path == "artifact-manifest.json"
    assert json.loads(payload)["entries"][-1]["status"] == "ok"


@pytest.mark.asyncio
async def test_file_artifact_uses_agent_scratch_and_ensures_parent() -> None:
    handler = WorkspaceArtifacts(
        artifact_owner(), [ArtifactConfig(source="/work/result.txt", destination="published.txt")], "/logs/artifacts"
    )
    source = artifact_source(directory=False)
    await handler.collect(source, {"main"})
    command = shlex.split(source.service_exec.await_args.args[0])
    assert command[-3:] == ["-C", "/work", "result.txt"]
    target = artifact_target()
    await handler.restore(target, "/logs/artifacts")
    target.ensure_dirs.assert_any_await(["/work"], chmod=True)
    target.empty_dirs.assert_not_awaited()
    assert "/harbor-handoff/agent/1.tar.gz" in shlex.split(target.exec.await_args.args[0])


@pytest.mark.asyncio
async def test_required_artifact_failure_is_not_a_reward() -> None:
    handler = WorkspaceArtifacts(artifact_owner(), ["/evidence/required"], "/logs/artifacts")
    source = artifact_source()
    source.service_exec.return_value.return_code = 1
    with pytest.raises(RuntimeError, match="required artifact"):
        await handler.collect(source, {"main"})
    assert handler.entries[0].status == "failed"


@pytest.mark.asyncio
async def test_destination_collision_rejects_required_later_entry() -> None:
    handler = WorkspaceArtifacts(
        artifact_owner(),
        [
            ArtifactConfig(source="/first", destination="shared"),
            ArtifactConfig(source="/second", destination="shared/nested"),
        ],
        "/logs/artifacts",
    )
    with pytest.raises(RuntimeError, match=r"required artifact.*second"):
        await handler.collect(artifact_source(), {"main"})
    assert handler.entries[-1].status == "skipped"


@pytest.mark.asyncio
async def test_restore_failure_propagates() -> None:
    handler = WorkspaceArtifacts(artifact_owner(), ["/evidence/required"], "/logs/artifacts")
    await handler.collect(artifact_source(), {"main"})
    target = artifact_target()
    target.exec.return_value.return_code = 1
    with pytest.raises(RuntimeError, match="failed to restore artifact"):
        await handler.restore(target, "/logs/artifacts")


def lifecycle(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, *, separate: bool = True, bundled: bool = False
) -> tuple[HarborVerification, Any, Any, Any, list[str]]:
    events: list[str] = []

    async def release(role: str) -> None:
        events.append("release-" + role)

    async def stage_tests() -> None:
        events.append("stage-tests")

    async def open_role(role: str, options: Any) -> Any:
        events.append("open-" + role)
        return SimpleNamespace()

    async def initialize() -> None:
        events.append("initialize-verifier")

    definition = SimpleNamespace(directory="tests", config={}, bundled_tests=bundled)
    owner: Any = SimpleNamespace(
        roles={"verifier": definition} if separate else {},
        stage_tests=AsyncMock(side_effect=stage_tests),
        release=AsyncMock(side_effect=release),
        close=AsyncMock(),
        open=AsyncMock(side_effect=open_role),
        task=SimpleNamespace(short_name="task", config=TaskConfig()),
        fleet=object(),
        runtime_params={"param": "value"},
    )
    main: Any = SimpleNamespace(
        handle=SimpleNamespace(check=AsyncMock()),
        session_id="attempt",
        os=TaskOS.LINUX,
        empty_dirs=AsyncMock(),
        with_default_user=Mock(return_value=contextlib.nullcontext()),
    )
    verifier = SimpleNamespace(
        handle=SimpleNamespace(check=AsyncMock()),
        initialize=AsyncMock(side_effect=initialize),
        os=TaskOS.LINUX,
        empty_dirs=AsyncMock(),
        with_default_user=Mock(return_value=contextlib.nullcontext()),
    )
    monkeypatch.setattr(
        "flotilla.integrations.harbor_verification.FlotillaEnvironment.view", Mock(return_value=verifier)
    )
    verification = HarborVerification(
        attempt=owner, main=main, paths=TrialPaths(tmp_path / "trial"), deployment="deployment", run_timeout=30
    )
    return verification, owner, main, verifier, events


@pytest.mark.asyncio
@pytest.mark.parametrize("bundled", [False, True])
async def test_separate_verifier_releases_main_before_staging_and_keeps_workspace(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, bundled: bool
) -> None:
    verification, owner, main, verifier, events = lifecycle(tmp_path, monkeypatch, bundled=bundled)
    await verification.check()
    main.handle.check.assert_awaited_once()
    deadline = asyncio.get_running_loop().time() + 10
    async with verification.verifier_environment(deadline=deadline) as (target, is_bundled):
        assert target is verifier and is_bundled is bundled
        assert verification.main_closed
        assert verification.active_verifier is verifier
        assert events == ["release-main", "stage-tests", "open-verifier", "initialize-verifier"]
        await verification.check()
        main.handle.check.assert_awaited_once()
        verifier.handle.check.assert_awaited_once()
        owner.close.assert_not_awaited()
        options = owner.open.await_args.args[1]
        assert options.request_id == "attempt-verifier"
        assert options.runtime_params == {"param": "value"}
        assert options.lifetime == 30
        assert 0 < options.admission_timeout <= 10
    assert events[-1] == "release-verifier"
    assert verification.active_verifier is None
    await verification.release_main()
    assert events.count("release-main") == 1
    owner.close.assert_not_awaited()


@pytest.mark.asyncio
async def test_same_environment_stages_without_releasing_main(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    verification, owner, main, _, events = lifecycle(tmp_path, monkeypatch, separate=False)
    async with verification.verifier_environment(deadline=asyncio.get_running_loop().time() + 10) as value:
        assert value == (main, False)
        assert not verification.main_closed
    assert events == ["stage-tests"]
    owner.open.assert_not_awaited()
    owner.release.assert_not_awaited()
    owner.close.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize("phase", ["open", "initialize", "body"])
@pytest.mark.parametrize("cancel", [False, True])
async def test_verifier_failure_or_cancellation_releases_role(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, phase: str, cancel: bool
) -> None:
    verification, owner, _, verifier, _ = lifecycle(tmp_path, monkeypatch)
    primary = asyncio.CancelledError("cancelled") if cancel else RuntimeError("verification failed")
    if phase == "open":
        owner.open.side_effect = primary
    elif phase == "initialize":
        verifier.initialize.side_effect = primary
    with pytest.raises(type(primary)) as caught:
        async with verification.verifier_environment(deadline=asyncio.get_running_loop().time() + 10):
            if phase == "body":
                raise primary
            pytest.fail("failed setup must not yield a verifier")
    assert caught.value is primary
    assert [call.args[0] for call in owner.release.await_args_list] == ["main", "verifier"]
    assert verification.active_verifier is None
    owner.close.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize("cancel", [False, True])
async def test_release_failure_is_noted_on_primary_failure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, cancel: bool
) -> None:
    verification, owner, _, _, _ = lifecycle(tmp_path, monkeypatch)
    primary = asyncio.CancelledError("cancelled") if cancel else ValueError("primary verification failure")
    owner.release.side_effect = [None, RuntimeError("release failed")]
    with pytest.raises(type(primary)) as caught:
        async with verification.verifier_environment(deadline=asyncio.get_running_loop().time() + 10):
            raise primary
    assert caught.value is primary
    assert any("verifier cleanup also failed" in note and "release failed" in note for note in primary.__notes__)
    assert verification.active_verifier is None


@pytest.mark.asyncio
async def test_release_failure_without_primary_propagates(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    verification, owner, _, _, _ = lifecycle(tmp_path, monkeypatch)
    owner.release.side_effect = [None, RuntimeError("release failed")]
    with pytest.raises(RuntimeError, match="release failed"):
        async with verification.verifier_environment(deadline=asyncio.get_running_loop().time() + 10):
            pass
    assert verification.active_verifier is None


@pytest.mark.asyncio
async def test_expired_deadline_never_opens_verifier(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    verification, owner, _, _, events = lifecycle(tmp_path, monkeypatch)
    with pytest.raises(TimeoutError, match="deadline expired"):
        async with verification.verifier_environment(deadline=asyncio.get_running_loop().time() - 1):
            pytest.fail("expired verifier must not be yielded")
    assert events == ["release-main", "stage-tests"]
    owner.open.assert_not_awaited()


@pytest.mark.asyncio
async def test_verify_shares_artifact_handoff_and_verifier_lifecycle(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    verification, owner, main, verifier, events = lifecycle(tmp_path, monkeypatch)
    owner.task.config = TaskConfig.model_validate(
        {"verifier": {"collect": [{"service": "db", "command": "flush", "timeout_sec": 12, "user": "collector"}]}}
    )

    async def stop(service: str) -> None:
        events.append("stop-" + service)

    async def run_hook(command: str, **kwargs: Any) -> Any:
        events.append("hook-" + kwargs["service"])
        return SimpleNamespace(return_code=0)

    async def ready(target: Any) -> None:
        assert target is verifier
        events.append("ready")

    class Artifacts:
        def __init__(self, *args: Any) -> None:
            pass

        def sidecar_services(self) -> set[str]:
            return {"db"}

        async def collect(self, target: Any, services: set[str]) -> None:
            assert target is main
            events.append("collect-" + ",".join(sorted(services)))

        async def restore(self, target: Any, convention: Any) -> None:
            assert target is verifier
            events.append("restore")

    async def verify() -> Any:
        events.append("verify")
        return SimpleNamespace(rewards={"reward": 1.0})

    main.stop_service = AsyncMock(side_effect=stop)
    main.service_exec = AsyncMock(side_effect=run_hook)
    native_verifier = Mock(return_value=SimpleNamespace(verify=AsyncMock(side_effect=verify)))
    monkeypatch.setattr(shared, "WorkspaceArtifacts", Artifacts)
    monkeypatch.setattr(shared, "WorkspaceVerifier", native_verifier)
    result = await verification.verify(timeout=10, on_ready=ready, verifier_kwargs={"custom": "argument"})
    assert result.rewards == {"reward": 1.0}
    assert events == [
        "collect-main",
        "stop-main",
        "hook-db",
        "collect-db",
        "release-main",
        "stage-tests",
        "open-verifier",
        "initialize-verifier",
        "ready",
        "restore",
        "verify",
        "release-verifier",
    ]
    main.service_exec.assert_awaited_once_with("flush", service="db", timeout_sec=12, user="collector")
    assert native_verifier.call_args.kwargs["environment"] is verifier
    assert native_verifier.call_args.kwargs["attempt"] is owner
    assert native_verifier.call_args.kwargs["custom"] == "argument"
    verifier.empty_dirs.assert_awaited_once()
    verifier.handle.check.assert_awaited_once()
    owner.close.assert_not_awaited()
