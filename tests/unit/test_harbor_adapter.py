"""Contract tests against the pinned Harbor API, no real platform credentials."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from harbor.models.task.config import EnvironmentConfig
from harbor.models.trial.paths import TrialPaths

from flotilla import Flotilla
from flotilla.cli import main
from flotilla.compose.task import TaskError
from flotilla.config import FlotillaConfig
from flotilla.core import labels
from flotilla.integrations.harbor import FlotillaEnvironment
from flotilla.integrations.harbor_source import task_environments
from flotilla.models.plan import TaskSource
from flotilla.platform.base import ExecResult, FlotillaError, ProcessSpec
from flotilla.runtime.environment import Environment
from flotilla.runtime.types import OpenOptions
from flotilla.testing.config import config_data, importable
from flotilla.testing.planning import planned_task
from flotilla.testing.platform import FakePlatform, SharedTree
from flotilla.testing.runtime import deployment as runtime_deployment

deployment = runtime_deployment


def test_scan_checks_verifier_context_and_continues_after_invalid_harbor_input(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
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
    output = tmp_path / "scan.jsonl"

    cfg = FlotillaConfig.from_mapping(config_data())
    deployment = importable(cfg)
    monkeypatch.setattr("flotilla.registry.ImageConfigs.image_config", lambda self, pinned: {})
    assert main(["inspect", str(tasks / "valid"), "--deployment", deployment, "--out", str(output)]) == 0
    # `inspect` plans every environment the task declares, so a rejected verifier fails the task.
    from flotilla.inspect import inspect_source
    from flotilla.registry import ImageConfigs

    for name in ("bad-verifier",):
        source = TaskSource(local=tasks / name)
        report = inspect_source(cfg, source, ImageConfigs())
        assert not report.planned and any(reason.startswith("tests: ") for reason in report.rejected)
        assert task_environments(tasks / name)[1].project.rejected


def source(tmp_path: Path, toml_text: str = '[environment]\ndocker_image="r.io/main:1"\ncpus=1\n') -> Path:
    path = tmp_path / "dataset" / "task"
    (path / "environment").mkdir(parents=True, exist_ok=True)
    (path / "tests").mkdir(exist_ok=True)
    (path / "task.toml").write_text(toml_text)
    return path


def test_harbor_resolves_each_role_from_its_own_directory(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """A separate verifier context is a second project, read once, from `tests/`."""
    from flotilla.compose import parse as parse_module

    path = source(tmp_path, '[environment]\ndocker_image="r.io/main:1"\n[verifier]\nenvironment_mode="separate"\n')
    (path / "tests/docker-compose.yaml").write_text("services:\n  main:\n    image: r.io/verifier:1\n")
    read: list[Path] = []
    original = parse_module.load_project

    def record(project_dir: Path, **kwargs: object) -> object:
        read.append(project_dir)
        return original(project_dir, **kwargs)  # type: ignore[arg-type]

    monkeypatch.setattr(parse_module, "load_project", record)
    definitions = task_environments(path)
    assert read == [path / "environment", path / "tests"]
    assert len(definitions) == 2
    assert definitions[1].project.services["main"].image == "r.io/verifier:1"
    assert all(definition.name == path.name for definition in definitions)


async def view(
    tmp_path: Path,
    cfg: FlotillaConfig,
    fleet: Flotilla,
    *,
    environment: str = "environment",
    options: OpenOptions | None = None,
) -> tuple[FlotillaEnvironment, Environment]:
    """Open one role's plan, then borrow it as a Harbor view over the live handle."""
    path = tmp_path / "dataset" / "task"
    if not path.exists():
        source(tmp_path)
    compiled = planned_task(path, cfg, environments=task_environments)
    handle = await fleet.open(compiled["main" if environment == "environment" else "verifier"], options)
    return (
        FlotillaEnvironment.view(
            handle,
            cfg=cfg,
            environment_dir=path / environment,
            environment_name="task",
            session_id="harbor-test",
            trial_paths=TrialPaths(tmp_path / "trial"),
            task_env_config=EnvironmentConfig(docker_image="r.io/main:1"),
        ),
        handle,
    )


@pytest.mark.asyncio
async def test_harbor_views_each_role_from_its_own_plan(
    tmp_path: Path, deployment: tuple[FlotillaConfig, FakePlatform, SharedTree]
) -> None:
    cfg, _, _ = deployment
    source(tmp_path, '[environment]\ndocker_image="r.io/main:1"\n[verifier]\nenvironment_mode="separate"\n')
    (tmp_path / "dataset/task/tests/docker-compose.yaml").write_text("services:\n  main:\n    image: r.io/verifier:1\n")
    async with Flotilla(deployment=cfg) as fleet:
        # The image reference reaches the sandbox as the task wrote it; nothing resolves it here.
        for directory, image in (("environment", "r.io/main:1"), ("tests", "r.io/verifier:1")):
            env, handle = await view(tmp_path, cfg, fleet, environment=directory)
            assert (await handle.info()).services[0].image == image
            await env.stop()
            await handle.release()


@pytest.mark.asyncio
async def test_harbor_requires_a_view_and_rejects_owning_misuse(
    tmp_path: Path, deployment: tuple[FlotillaConfig, FakePlatform, SharedTree]
) -> None:
    cfg, platform, _ = deployment
    path = source(tmp_path)
    async with Flotilla(deployment=cfg) as fleet:
        env, _ = await view(tmp_path, cfg, fleet)
        with pytest.raises(ValueError, match="force_build is unsupported"):
            await env.start(force_build=True)
        await env.start()  # the handle was open all along; start only prepares Harbor's log directories
        with pytest.raises(ValueError, match="resource overrides"):
            FlotillaEnvironment(
                environment_dir=path / "environment",
                environment_name="task",
                session_id="unopened",
                trial_paths=TrialPaths(tmp_path / "trial"),
                task_env_config=EnvironmentConfig(docker_image="r.io/main:1"),
                override_cpus=2,
                cfg=cfg,
            )
        with pytest.raises(RuntimeError, match="environment is not started"):
            await FlotillaEnvironment(
                environment_dir=path / "environment",
                environment_name="task",
                session_id="unopened",
                trial_paths=TrialPaths(tmp_path / "trial"),
                task_env_config=EnvironmentConfig(docker_image="r.io/main:1"),
                cfg=cfg,
            ).start()
    assert not await platform.list({labels.ROLE: labels.ROLE_UNIT})


@pytest.mark.asyncio
async def test_harbor_borrowed_stop_leaves_the_owner_running(
    tmp_path: Path, deployment: tuple[FlotillaConfig, FakePlatform, SharedTree]
) -> None:
    cfg, _, _ = deployment
    async with Flotilla(deployment=cfg) as fleet:
        env, handle = await view(tmp_path, cfg, fleet)
        await env.stop()
        await handle.check()  # stopping a borrowed view leaves the owner's environment running
        await handle.release()


@pytest.mark.parametrize("extra", [False, True])
def test_harbor_advertises_no_network_only_for_declared_dns(
    tmp_path: Path,
    deployment: tuple[FlotillaConfig, FakePlatform, SharedTree],
    extra: bool,
) -> None:
    cfg, _, _ = deployment
    implicit = ("10.96.0.10:53/tcp,udp",) + (("10.0.0.1:443/tcp",) if extra else ())
    cfg = cfg.model_copy(
        update={
            "platform": cfg.platform.model_copy(update={"cluster_dns": ("10.96.0.10",), "implicit_egress": implicit})
        }
    )
    env = FlotillaEnvironment(
        environment_dir=source(tmp_path) / "environment",
        environment_name="task",
        session_id="harbor-test",
        trial_paths=TrialPaths(tmp_path / "trial"),
        task_env_config=EnvironmentConfig(docker_image="r.io/main:1"),
        cfg=cfg,
    )
    assert env.capabilities.disable_internet is not extra


@pytest.mark.asyncio
async def test_harbor_disable_internet_is_fixed_at_open_with_dns_exception(
    tmp_path: Path,
    deployment: tuple[FlotillaConfig, FakePlatform, SharedTree],
) -> None:
    cfg, platform, _ = deployment
    cfg = cfg.model_copy(
        update={
            "network": cfg.network.model_copy(update={"external": "any"}),
            "platform": cfg.platform.model_copy(
                update={"cluster_dns": ("10.96.0.10",), "implicit_egress": ("10.96.0.10:53/udp,tcp",)}
            ),
        }
    )
    platform.caps = cfg.platform_capabilities()
    async with Flotilla(deployment=cfg) as fleet:
        env, handle = await view(tmp_path, cfg, fleet, options=OpenOptions(disable_internet=True))
        info = await handle.info()
        assert platform.spec_of(info.services[0].instance_id).external.mode == "none"
        await env.stop()
        await handle.release()


@pytest.mark.asyncio
async def test_harbor_environment_contract(
    tmp_path: Path,
    deployment: tuple[FlotillaConfig, FakePlatform, SharedTree],
) -> None:
    cfg, _, _ = deployment
    async with Flotilla(deployment=cfg) as fleet:
        env, handle = await view(tmp_path, cfg, fleet)
        assert env.capabilities.docker_compose
        await env.start()
        result = await env.exec("echo ok", env={"LITERAL": "$(no)"})
        assert result.return_code == 0
        await handle.agent.write_file("/tmp/reward", b"0")
        target = tmp_path / "reward"
        await env.download_file("/tmp/reward", target)
        assert target.read_bytes() == b"0"
        await env.stop()
        await handle.release()


@pytest.mark.asyncio
async def test_harbor_prepares_role_logs_once_and_records_the_environment(
    tmp_path: Path,
    deployment: tuple[FlotillaConfig, FakePlatform, SharedTree],
) -> None:
    cfg, platform, _ = deployment
    original = platform.exec_handler
    commands: list[ProcessSpec] = []

    def execute(iid: str, proc: ProcessSpec) -> ExecResult:
        if proc.argv[:2] in (("/.flotilla/bin/busybox", "mkdir"), ("/.flotilla/bin/busybox", "chmod")):
            commands.append(proc)
        return original(iid, proc)

    platform.set_exec_handler(execute)
    async with Flotilla(deployment=cfg) as fleet:
        env, handle = await view(tmp_path, cfg, fleet)
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
        await handle.release()
    assert fleet.cleanup_report is not None and fleet.cleanup_report.clean


@pytest.mark.asyncio
async def test_harbor_log_initialization_failure_leaves_the_handle_to_its_owner(
    tmp_path: Path,
    deployment: tuple[FlotillaConfig, FakePlatform, SharedTree],
) -> None:
    cfg, platform, _ = deployment
    original = platform.exec_handler

    def execute(iid: str, proc: ProcessSpec) -> ExecResult:
        if proc.argv[:2] == ("/.flotilla/bin/busybox", "chmod"):
            return ExecResult(1, b"", b"permission denied")
        return original(iid, proc)

    platform.set_exec_handler(execute)
    fleet = Flotilla(deployment=cfg)
    try:
        env, handle = await view(tmp_path, cfg, fleet)
        with pytest.raises(RuntimeError, match="initialize Harbor log"):
            await env.start()
        await handle.check()  # a failed view start does not tear down the owner's environment
        report = await fleet.drain(2)
        assert report.clean
        assert not await platform.list({labels.LAUNCH: report.launch_id, labels.ROLE: labels.ROLE_UNIT})
    finally:
        await fleet.close(2)


@pytest.mark.asyncio
async def test_harbor_reward_read_does_not_hide_lost_environment(
    tmp_path: Path,
    deployment: tuple[FlotillaConfig, FakePlatform, SharedTree],
) -> None:
    cfg, platform, _ = deployment
    async with Flotilla(deployment=cfg) as fleet:
        env, handle = await view(tmp_path, cfg, fleet)
        await env.start()
        await handle.agent.write_file("/tmp/reward", b"0")
        info = await handle.info()
        await fleet._call(lambda manager: manager.platform.delete(info.services[0].instance_id))
        with pytest.raises(FlotillaError):
            await env.download_file("/tmp/reward", tmp_path / "reward")
        await env.stop()  # detaching never fails; the owner's close reports the loss
    assert fleet.cleanup_report is not None
    assert not await platform.list({labels.LAUNCH: fleet.cleanup_report.launch_id})
