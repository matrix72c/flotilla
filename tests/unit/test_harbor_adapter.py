"""Contract tests against the pinned Harbor API, no real platform credentials."""

from __future__ import annotations

import asyncio
import json
from pathlib import Path

import pytest

pytest.importorskip("harbor")
import toml  # type: ignore[import-untyped]
from harbor.models.task.config import EnvironmentConfig
from harbor.models.trial.paths import TrialPaths

from flotilla import Flotilla
from flotilla.cli import main
from flotilla.compose.task import TaskError
from flotilla.config import FlotillaConfig
from flotilla.core import labels
from flotilla.integrations.harbor import FlotillaEnvironment
from flotilla.integrations.harbor_source import task_environments
from flotilla.platform.base import ExecResult, FlotillaError, ProcessSpec
from flotilla.testing.platform import FakePlatform, SharedTree
from flotilla.testing.reports import report
from flotilla.testing.runtime import deployment as runtime_deployment

deployment = runtime_deployment


def test_scan_checks_verifier_context_and_continues_after_invalid_harbor_input(tmp_path: Path) -> None:
    tasks = tmp_path / "tasks"
    for name in ("valid", "bad-verifier", "bad-config"):
        root = tasks / name
        (root / "environment").mkdir(parents=True)
        (root / "tests").mkdir()
        (root / "task.toml").write_text('[environment]\ndocker_image="r.io/main:1"\n')
    (tasks / "bad-verifier/task.toml").write_text(
        '[environment]\ndocker_image="r.io/main:1"\n[verifier]\nenvironment_mode="separate"\n'
    )
    (tasks / "bad-verifier/tests/docker-compose.yaml").write_text(
        "services:\n  main:\n    image: fixture/verifier:1\n    profiles: [unsupported]\n"
    )
    (tasks / "bad-config/task.toml").write_text(
        '[environment]\ndocker_image="r.io/main:1"\n[verifier]\nenvironment_mode="invalid"\n'
    )
    with pytest.raises(TaskError, match="Harbor environment definition"):
        task_environments(tasks / "bad-config")
    caps = tmp_path / "caps.json"
    caps.write_text(report().dump_json())
    output = tmp_path / "scan.jsonl"
    from flotilla.testing.config import config_data

    config = config_data()
    config["opensandbox"]["capabilities"] = str(caps)
    deployment_path = tmp_path / "inspect.toml"
    deployment_path.write_text(toml.dumps(config))
    assert main(["inspect", str(tasks / "valid"), "--deployment", str(deployment_path), "--out", str(output)]) == 0
    # Invalid sources are rejected independently by the same library resolver.
    from flotilla.preparation.service import inspect

    for name in ("bad-verifier",):
        assert all(entry.status == "accepted" for entry in inspect(FlotillaConfig.load(deployment_path), tasks / name))
        assert task_environments(tasks / name)[1].project.rejected


def adapter(tmp_path: Path, cfg: FlotillaConfig, fleet: Flotilla) -> FlotillaEnvironment:
    config = tmp_path / "deployment.toml"
    config.write_text(toml.dumps(cfg.model_dump(exclude_none=True, exclude={"base_dir"})))
    source = tmp_path / "dataset" / "task"
    (source / "environment").mkdir(parents=True)
    (source / "task.toml").write_text('[environment]\ndocker_image="r.io/main:1"\n')
    return FlotillaEnvironment(
        environment_dir=source / "environment",
        environment_name="task",
        session_id="harbor-test",
        trial_paths=TrialPaths(tmp_path / "trial"),
        task_env_config=EnvironmentConfig(docker_image="r.io/main:1"),
        deployment=str(config),
        fleet=fleet,
    )


@pytest.mark.asyncio
async def test_harbor_environment_contract(
    tmp_path: Path,
    deployment: tuple[FlotillaConfig, FakePlatform, SharedTree],
) -> None:
    cfg, _, _ = deployment
    async with Flotilla(deployment=cfg) as fleet:
        env = adapter(tmp_path, cfg, fleet)
        assert env.capabilities.docker_compose
        await env.start()
        result = await env.exec("echo ok", env={"LITERAL": "$(no)"})
        assert result.return_code == 0
        await env._env().agent.write_file("/tmp/reward", b"0")
        target = tmp_path / "reward"
        await env.download_file("/tmp/reward", target)
        assert target.read_bytes() == b"0"
        await env.stop()


@pytest.mark.asyncio
async def test_harbor_prepares_role_logs_and_records_cleanup(
    tmp_path: Path, deployment: tuple[FlotillaConfig, FakePlatform, SharedTree]
) -> None:
    cfg, platform, _ = deployment
    original = platform.exec_handler
    commands: list[ProcessSpec] = []

    def execute(iid: str, proc: ProcessSpec) -> ExecResult:
        if proc.argv[:2] in (("/.flotilla/bin/busybox", "mkdir"), ("/.flotilla/bin/busybox", "chmod")):
            commands.append(proc)
        return original(iid, proc)

    platform.set_exec_handler(execute)
    fleet = Flotilla(deployment=cfg)
    env = adapter(tmp_path, cfg, fleet)
    env._owns_fleet = True
    await env.start()
    await env.start()  # idempotent start must not reset agent outputs
    assert len(commands) == 2
    for proc in commands:
        assert proc.uid == proc.gid == 0 and proc.cwd == "/"
        assert set(proc.argv[3:]) == {"/logs/agent", "/logs/user-agent", "/logs/verifier", "/logs/artifacts"}
    info = json.loads((tmp_path / "trial/flotilla/harbor-test/environment.json").read_text())
    assert info["services"][0]["instance_id"]
    assert "accepted_security_gaps" in info
    await env.stop()
    cleanup = json.loads((tmp_path / "trial/flotilla/harbor-test/cleanup.json").read_text())
    assert cleanup["clean"] is True and cleanup["remaining"] == []


@pytest.mark.asyncio
@pytest.mark.parametrize("owned", [False, True])
async def test_harbor_log_initialization_failure_releases_environment(
    tmp_path: Path, deployment: tuple[FlotillaConfig, FakePlatform, SharedTree], owned: bool
) -> None:
    cfg, platform, _ = deployment
    original = platform.exec_handler

    def execute(iid: str, proc: ProcessSpec) -> ExecResult:
        if proc.argv[:2] == ("/.flotilla/bin/busybox", "chmod"):
            return ExecResult(1, b"", b"permission denied")
        return original(iid, proc)

    platform.set_exec_handler(execute)
    fleet = Flotilla(deployment=cfg)
    env = adapter(tmp_path, cfg, fleet)
    env._owns_fleet = owned
    try:
        with pytest.raises(RuntimeError, match="initialize Harbor log"):
            await env.start()
        assert env._environment is None
        report = await fleet.drain(2)
        assert report.clean
        assert not await platform.list({labels.LAUNCH: report.launch_id, labels.ROLE: labels.ROLE_UNIT})
        if owned:
            assert fleet.cleanup_report is not None and fleet.cleanup_report.clean
        else:
            assert fleet.cleanup_report is None
    finally:
        await fleet.close(2)


@pytest.mark.asyncio
async def test_harbor_reward_read_does_not_hide_lost_environment(
    tmp_path: Path,
    deployment: tuple[FlotillaConfig, FakePlatform, SharedTree],
) -> None:
    cfg, platform, _ = deployment
    async with Flotilla(deployment=cfg) as fleet:
        env = adapter(tmp_path, cfg, fleet)
        await env.start()
        await env._env().agent.write_file("/tmp/reward", b"0")
        info = await env._env().info()
        await fleet._call(lambda manager: manager.platform.delete(info.services[0].instance_id))
        with pytest.raises(FlotillaError):
            await env.download_file("/tmp/reward", tmp_path / "reward")
        with pytest.raises(FlotillaError):
            await env.stop()
    assert fleet.cleanup_report is not None
    assert not await platform.list({labels.LAUNCH: fleet.cleanup_report.launch_id})


@pytest.mark.asyncio
async def test_cancelled_stop_still_closes_owned_fleet(
    tmp_path: Path,
    deployment: tuple[FlotillaConfig, FakePlatform, SharedTree],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    cfg, _, _ = deployment
    fleet = Flotilla(deployment=cfg)
    env = adapter(tmp_path, cfg, fleet)
    env._owns_fleet = True
    await env.start()
    release = env._env().release

    async def interrupted() -> None:
        await release()
        raise asyncio.CancelledError

    monkeypatch.setattr(env._env(), "release", interrupted)
    with pytest.raises(asyncio.CancelledError):
        await env.stop()
    assert fleet.cleanup_report is not None and fleet.cleanup_report.clean
