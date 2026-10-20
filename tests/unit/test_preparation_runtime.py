"""Current task inputs, per-open plans, and owned preparation cleanup."""

from __future__ import annotations

import asyncio
import contextlib
import json
import os
import signal
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from flotilla import Flotilla, OpenOptions
from flotilla.config import FlotillaConfig
from flotilla.preparation.service import Operation, Preparer
from flotilla.runtime.manager import Manager
from flotilla.testing.platform import FakePlatform, SharedTree
from flotilla.testing.runtime import deployment as deployment

REAL_BUILD_PROCESS = Preparer._build_process


def source(root: Path) -> Path:
    path = root / "task"
    (path / "environment").mkdir(parents=True)
    (path / "task.toml").write_text('[environment]\ndocker_image="r.io/main:1"\ncpus=1\n')
    (path / "instruction.md").write_text("Neutral fixture")
    return path


@pytest.mark.asyncio
async def test_each_open_reads_current_command_resources_and_files(
    tmp_path: Path, deployment: tuple[FlotillaConfig, FakePlatform, SharedTree]
) -> None:
    cfg, platform, _ = deployment
    path = source(tmp_path)
    assets = path / "environment/assets"
    assets.mkdir()
    (assets / "message").write_text("first")
    compose = path / "environment/docker-compose.yaml"
    template = (
        'services:\n  main:\n    image: r.io/main:1\n    command: ["echo", "VALUE"]\n'
        '    environment: {MESSAGE: "VALUE"}\n    volumes: ["./assets:/assets:ro"]\n'
    )
    compose.write_text(template.replace("VALUE", "first"))
    async with Flotilla(deployment=cfg) as fleet:
        first = await fleet.open_task(path)
        (assets / "message").write_text("second")
        compose.write_text(template.replace("VALUE", "second"))
        (path / "task.toml").write_text('[environment]\ndocker_image="r.io/main:1"\ncpus=2\n')
        second = await fleet.open_task(path)

        async def plans(manager: Manager) -> list[Any]:
            return [manager.runs[e.id].plan for e in (first, second)]

        before, after = await fleet._call(plans)
        assert before.units["main"].command == ("echo", "first")
        assert after.units["main"].command == ("echo", "second")
        assert (await first.info()).services[0].cpu == "1"
        assert (await second.info()).services[0].cpu == "2"
        assert json.loads((await first.agent.exec(["true"])).stdout)["env"]["MESSAGE"] == "first"
        assert json.loads((await second.agent.exec(["true"])).stdout)["env"]["MESSAGE"] == "second"
        # Published files from the two opens are distinct and preserve both versions.
        values = list(platform.tree_contents.values())
        assert b"first" in values and b"second" in values
        assert "prepare_images_s" in dict((await second.info()).timings)
    assert not platform.iids()


@pytest.mark.asyncio
async def test_concurrent_opens_prepare_independently_and_preserve_isolation(
    tmp_path: Path, deployment: tuple[FlotillaConfig, FakePlatform, SharedTree], monkeypatch: pytest.MonkeyPatch
) -> None:
    cfg, platform, _ = deployment
    path = source(tmp_path)
    original = Preparer._build_process
    workspaces = []

    async def counted(self: Preparer, request: Path) -> None:
        workspaces.append(json.loads(request.read_text())["work"])
        await asyncio.sleep(0.02)
        await original(self, request)

    monkeypatch.setattr(Preparer, "_build_process", counted)
    async with Flotilla(deployment=cfg) as fleet:
        first, second = await asyncio.gather(fleet.open_task(path), fleet.open_task(path))
        assert len(set(workspaces)) == 2
        await first.agent.write_file("/result", b"first")
        await second.agent.write_file("/result", b"second")
        assert await first.agent.read_file("/result") == b"first"
        assert await second.agent.read_file("/result") == b"second"
    assert all(not Path(work).parent.exists() for work in workspaces)
    assert not platform.iids()


@pytest.mark.asyncio
async def test_source_change_during_preparation_is_accepted_and_next_open_rereads(
    tmp_path: Path, deployment: tuple[FlotillaConfig, FakePlatform, SharedTree], monkeypatch: pytest.MonkeyPatch
) -> None:
    cfg, _, _ = deployment
    path = source(tmp_path)
    compose = path / "environment/docker-compose.yaml"
    original_text = 'services:\n  main:\n    image: r.io/main:1\n    command: ["echo", "FIRST"]\n'
    compose.write_text(original_text)
    original = Preparer._build_process

    async def changed(self: Preparer, request: Path) -> None:
        compose.write_text(original_text.replace("FIRST", "SECOND"))
        await original(self, request)

    monkeypatch.setattr(Preparer, "_build_process", changed)
    async with Flotilla(deployment=cfg) as fleet:
        second = await fleet.open_task(path)
        monkeypatch.setattr(Preparer, "_build_process", original)
        compose.write_text(original_text)
        first = await fleet.open_task(path)

        async def commands(manager: Manager) -> list[Any]:
            return [manager.runs[e.id].plan.units["main"].command for e in (second, first)]

        assert await fleet._call(commands) == [("echo", "SECOND"), ("echo", "FIRST")]


@pytest.mark.asyncio
async def test_failed_preparation_can_be_fixed_without_refresh(
    tmp_path: Path, deployment: tuple[FlotillaConfig, FakePlatform, SharedTree]
) -> None:
    cfg, _, _ = deployment
    path = source(tmp_path)
    compose = path / "environment/docker-compose.yaml"
    compose.write_text("services:\n  main:\n    image: r.io/main:1\n    profiles: [unsupported]\n")
    async with Flotilla(deployment=cfg) as fleet:
        with pytest.raises(ValueError):
            await fleet.open_task(path)
        compose.write_text("services:\n  main:\n    image: r.io/main:1\n")
        async with fleet.environment(path) as env:
            await env.check()


@pytest.mark.asyncio
async def test_harbor_cpu_override_does_not_mutate_source_and_borrowed_stop_is_non_owning(
    tmp_path: Path, deployment: tuple[FlotillaConfig, FakePlatform, SharedTree]
) -> None:
    pytest.importorskip("harbor")
    from harbor.models.task.config import EnvironmentConfig
    from harbor.models.trial.paths import TrialPaths

    from flotilla.integrations.harbor import FlotillaEnvironment

    cfg, _, _ = deployment
    path = source(tmp_path)
    original = EnvironmentConfig(docker_image="r.io/main:1", cpus=1)
    async with Flotilla(deployment=cfg) as fleet:
        adapter = FlotillaEnvironment(
            deployment="unused",
            fleet=fleet,
            environment_dir=path / "environment",
            environment_name="task",
            session_id="override",
            trial_paths=TrialPaths(tmp_path / "trial"),
            task_env_config=original,
            override_cpus=2,
        )
        assert original.cpus == 1 and adapter._definition.config["cpus"] == 1
        await adapter.start()
        assert (await adapter.handle.info()).services[0].cpu == "2"
        view = FlotillaEnvironment.view(
            adapter.handle,
            fleet=fleet,
            deployment="unused",
            environment_dir=path / "environment",
            environment_name="task",
            session_id="borrowed",
            trial_paths=TrialPaths(tmp_path / "borrowed"),
            task_env_config=original,
        )
        await view.stop()
        await adapter.handle.check()
        await adapter.stop()


@pytest.mark.asyncio
async def test_force_build_selects_source_and_effective_config_is_preserved(
    tmp_path: Path, deployment: tuple[FlotillaConfig, FakePlatform, SharedTree], monkeypatch: pytest.MonkeyPatch
) -> None:
    cfg, _, _ = deployment
    path = source(tmp_path)
    (path / "environment/Dockerfile").write_text("FROM r.io/base:1\n")
    seen = []
    original = Preparer._build_process

    async def track(self: Preparer, request: Path) -> None:
        seen.append(json.loads(request.read_text())["force_build"])
        await original(self, request)

    monkeypatch.setattr(Preparer, "_build_process", track)
    async with Flotilla(deployment=cfg) as fleet:
        definition = fleet.inspect(path)[0].definition
        definition = replace(definition, config={**definition.config, "workdir": "/custom"})
        async with fleet.environment(definition) as env:
            assert (await env.info()).services[0].cwd == "/custom"
            assert (await env.info()).services[0].image.startswith("r.io/main@")
        async with fleet.environment(path, OpenOptions(force_build=True)) as env:
            assert (await env.info()).services[0].image.startswith("r.io/ns/out@")
        assert definition.config["docker_image"] == "r.io/main:1"
    assert seen == [False, True]


@pytest.mark.asyncio
@pytest.mark.parametrize("shutdown", ["cancel", "timeout"])
async def test_real_preparation_cancel_then_close_reaps_slow_terminating_child(
    tmp_path: Path,
    deployment: tuple[FlotillaConfig, FakePlatform, SharedTree],
    monkeypatch: pytest.MonkeyPatch,
    shutdown: str,
) -> None:
    from flotilla.preparation import service

    cfg, platform, _ = deployment
    marker = tmp_path / "pid"
    executable = tmp_path / "worker"
    executable.write_text(
        "#!/usr/bin/env python3\nimport os,signal,time\n"
        "signal.signal(signal.SIGTERM, signal.SIG_IGN)\n"
        f'open({str(marker)!r},"w").write(str(os.getpid()))\ntime.sleep(100)\n'
    )
    executable.chmod(0o755)
    monkeypatch.setattr(service, "sys", SimpleNamespace(executable=str(executable)))
    monkeypatch.setattr(Preparer, "_build_process", REAL_BUILD_PROCESS)
    fleet = Flotilla(deployment=cfg)
    pending = asyncio.create_task(
        fleet.open_task(source(tmp_path), OpenOptions(startup_timeout=0.4 if shutdown == "timeout" else 10))
    )
    pid = None
    try:
        async with asyncio.timeout(5):
            while not marker.exists() or not marker.read_text():
                await asyncio.sleep(0.01)
        pid = int(marker.read_text())
        if shutdown == "cancel":
            pending.cancel()
        with pytest.raises(asyncio.CancelledError if shutdown == "cancel" else TimeoutError):
            await pending
        assert (await fleet.close(timeout=3)).clean
        with pytest.raises(ProcessLookupError):
            os.kill(pid, 0)
        assert not platform.iids()
    finally:
        if pid is not None:
            with contextlib.suppress(ProcessLookupError):
                os.killpg(pid, signal.SIGKILL)
        await fleet.close(timeout=3)


@pytest.mark.asyncio
async def test_docker_cleanup_failure_is_reported_and_journal_retained(
    tmp_path: Path, deployment: tuple[FlotillaConfig, FakePlatform, SharedTree], monkeypatch: pytest.MonkeyPatch
) -> None:
    cfg, _, _ = deployment
    original = Preparer._build_process
    workspaces = []

    async def leftovers(self: Preparer, request: Path) -> None:
        journal = request.parent / "containers"
        journal.mkdir()
        (journal / "flotilla-export-fixture").touch()
        workspaces.append(request.parent)
        await original(self, request)

    monkeypatch.setattr(Preparer, "_build_process", leftovers)
    cfg = cfg.model_copy(
        update={"preparation": cfg.preparation.model_copy(update={"docker_command": ("missing-fixture-docker",)})}
    )
    fleet = Flotilla(deployment=cfg)
    try:
        env = await fleet.open_task(source(tmp_path))
        await env.release()
        assert not (await fleet.drain()).clean
        report = await fleet.close()
        assert not report.clean
        assert {r.kind for r in report.preparation_residuals} == {"docker_container", "directory"}
        assert (workspaces[0] / "containers/flotilla-export-fixture").is_file()
    finally:
        await fleet.close()
        import shutil

        for work in workspaces:
            shutil.rmtree(work)


def test_removed_workflows_and_config_have_clear_migration_errors() -> None:
    import flotilla
    from flotilla.cli import _build_parser
    from flotilla.config import Build, Preparation

    for field in ("policy", "cache_dir", "snapshot"):
        with pytest.raises(ValueError, match="Extra inputs"):
            Preparation.model_validate({field: "legacy"})
    with pytest.raises(ValueError, match="Extra inputs"):
        Build.model_validate({"prebuilt": {"reference": "r.io/{task}"}})
    for name in ("Task", "TaskIndex", "RunRequest", "run_many", "PreparationReport", "PreparedEnvironment"):
        assert not hasattr(flotilla, name)
    assert not hasattr(Flotilla, "prepare")
    with pytest.raises(SystemExit):
        _build_parser().parse_args(["prepare"])


@pytest.mark.asyncio
async def test_close_deadline_reports_pending_cleanup_and_leaves_its_owner_alive(
    tmp_path: Path, deployment: tuple[FlotillaConfig, FakePlatform, SharedTree], monkeypatch: pytest.MonkeyPatch
) -> None:
    import threading
    import time

    cfg, _, _ = deployment
    started, finished = threading.Event(), threading.Event()
    original = Preparer._cleanup
    directories = []

    async def slow_cleanup(self: Preparer, operation: Operation) -> None:
        directories.append(operation.directory)
        started.set()
        await asyncio.sleep(0.4)
        await original(self, operation)
        finished.set()

    monkeypatch.setattr(Preparer, "_cleanup", slow_cleanup)
    fleet = Flotilla(deployment=cfg)
    pending = asyncio.create_task(fleet.open_task(source(tmp_path)))
    try:
        async with asyncio.timeout(5):
            while not started.is_set():
                await asyncio.sleep(0.01)
        pending.cancel()
        with pytest.raises(asyncio.CancelledError):
            await pending
        before = time.monotonic()
        report = await fleet.close(timeout=0.05)
        assert time.monotonic() - before < 0.3
        assert not report.clean and any(r.kind == "operation" for r in report.preparation_residuals)
        async with asyncio.timeout(3):
            while fleet._worker is not None and fleet._worker.thread.is_alive():
                await asyncio.sleep(0.01)
        assert finished.is_set()
        assert all(not directory.exists() for directory in directories)
    finally:
        await fleet.close()
