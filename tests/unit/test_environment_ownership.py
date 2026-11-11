"""Resource outcomes at the public API boundary, using actual fake-platform deletion."""

from __future__ import annotations

import asyncio

import pytest

from flotilla import CleanupError, Flotilla
from flotilla.config import FlotillaConfig, Resource, ResourceDefaults
from flotilla.core import labels
from flotilla.platform.base import ErrorCategory, ExecResult, FlotillaError, ProcessSpec
from flotilla.runtime.manager import Manager
from flotilla.runtime.types import Task
from flotilla.testing.environments import FixtureRequest as RunRequest
from flotilla.testing.environments import open_fixture
from flotilla.testing.platform import FakePlatform, SharedTree
from flotilla.testing.runtime import deployment as deployment
from flotilla.testing.runtime import task


@pytest.mark.asyncio
async def test_release_confirms_deletion_without_waiting_for_other_environments(
    deployment: tuple[FlotillaConfig, FakePlatform, SharedTree],
) -> None:
    cfg, platform, _ = deployment
    async with Flotilla(deployment=cfg) as fleet:
        first = await open_fixture(fleet, RunRequest(task(), "first"))
        second = await open_fixture(fleet, RunRequest(task(), "second"))
        first_iid = (await first.info()).services[0].instance_id
        second_iid = (await second.info()).services[0].instance_id
        platform.set_latency("delete", 0.03)
        report = await first.release()
        assert report.clean
        assert first_iid not in platform.iids()
        assert second_iid in platform.iids()
        await second.check()
        assert await first.release() is report


@pytest.mark.asyncio
async def test_release_timeout_retains_cleanup_and_repeated_wait_succeeds(
    deployment: tuple[FlotillaConfig, FakePlatform, SharedTree],
) -> None:
    cfg, platform, _ = deployment
    async with Flotilla(deployment=cfg) as fleet:
        env = await open_fixture(fleet, RunRequest(task(), "slow-delete"))
        platform.set_latency("delete", 0.1)
        with pytest.raises(CleanupError) as error:
            await env.release(0.01)
        assert error.value.report.remaining[0].environment_id == env.id
        assert not error.value.report.remaining[0].finished
        assert (await env.release(2)).clean
        assert not await platform.list({labels.TRIAL: env.id})


@pytest.mark.asyncio
async def test_failed_deletion_is_reported_and_retried(
    deployment: tuple[FlotillaConfig, FakePlatform, SharedTree],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    cfg, platform, _ = deployment
    original = platform.delete
    fail = True
    async with Flotilla(deployment=cfg) as fleet:
        env = await open_fixture(fleet, RunRequest(task(), "retry-delete"))
        iid = (await env.info()).services[0].instance_id

        async def delete(target: str) -> None:
            if target == iid and fail:
                raise FlotillaError("denied", stage="run", category=ErrorCategory.INVALID, retryable=False)
            await original(target)

        monkeypatch.setattr(platform, "delete", delete)

        async def short_retry(manager: Manager) -> None:
            manager.deleter.retry_deadline_s = 0.01

        await fleet._call(short_retry)
        with pytest.raises(CleanupError) as error:
            await env.release(2)
        assert error.value.report.remaining[0].finished
        assert iid in error.value.report.remaining[0].instances
        fail = False
        assert (await env.release(2)).clean
    assert fleet.cleanup_report is not None and fleet.cleanup_report.clean


@pytest.mark.asyncio
@pytest.mark.parametrize("recovered", [False, True])
async def test_close_retries_owned_directory_cleanup_without_hiding_persistent_failure(
    deployment: tuple[FlotillaConfig, FakePlatform, SharedTree], recovered: bool
) -> None:
    cfg, platform, tree = deployment
    execute = platform.exec_handler
    busy = True
    removals = 0

    def remove(iid: str, proc: ProcessSpec) -> ExecResult:
        nonlocal removals
        if proc.argv[0] == "rm" and proc.argv[-1].endswith("owned-directory"):
            removals += 1
            if busy:
                return ExecResult(1, b"", b".nfs: Device or resource busy")
        return execute(iid, proc)

    fleet = Flotilla(deployment=cfg)
    try:
        await fleet.start()
        env = await open_fixture(fleet, RunRequest(task(), "busy-directory"))

        async def add_directory(manager: Manager) -> str:
            key = f"launches/{manager.launch_id}/{env.id}/owned-directory"
            manager.reaper.add_dir(env.id, key)
            await manager.anchor.mkdir(key, stage="prepare")
            return key

        key = await fleet._call(add_directory)
        platform.set_exec_handler(remove)
        with pytest.raises(CleanupError) as failed:
            await env.release()
        assert failed.value.report.remaining[0].directories == (key,)
        assert key in tree.dirs
        assert removals == 1
        busy = not recovered
    finally:
        report = await fleet.close()
    assert removals == 2
    assert report.clean is recovered
    assert (key in tree.dirs) is (not recovered)
    if recovered:
        assert report.cleaned == 1
        assert not report.remaining and not report.launch_directories
    else:
        assert report.remaining[0].directories == (key,)
        assert report.remaining[0].reason == "directory_remove_failed"
        assert report.launch_directories
    assert not platform.instances()


@pytest.mark.asyncio
async def test_cancelled_release_waiter_does_not_cancel_deletion(
    deployment: tuple[FlotillaConfig, FakePlatform, SharedTree],
) -> None:
    cfg, platform, _ = deployment
    async with Flotilla(deployment=cfg) as fleet:
        env = await open_fixture(fleet, RunRequest(task(), "cancel-delete"))
        platform.set_latency("delete", 0.05)
        waiter = asyncio.create_task(env.release())
        await asyncio.sleep(0.01)
        waiter.cancel()
        with pytest.raises(asyncio.CancelledError):
            await waiter
        assert (await env.release()).clean
        assert not await platform.list({labels.TRIAL: env.id})


@pytest.mark.asyncio
async def test_terminate_one_service_leaves_sidecar_available(
    deployment: tuple[FlotillaConfig, FakePlatform, SharedTree],
) -> None:
    cfg, platform, _ = deployment
    manifest = task().manifest()
    manifest.units["sidecar"] = manifest.units["main"].model_copy(deep=True)
    async with Flotilla(deployment=cfg) as fleet:
        env = await open_fixture(fleet, RunRequest(Task(manifest), "terminate"))
        main_iid = (await env.info()).services[0].instance_id
        await asyncio.gather(env.agent.terminate(), env.agent.terminate())
        assert main_iid not in platform.iids()
        await env.check()
        assert (await env.service("sidecar").exec(["true"])).exit_code == 0
        with pytest.raises(FlotillaError, match="terminated"):
            await env.agent.exec(["true"])
        assert (await env.release()).clean


@pytest.mark.asyncio
async def test_runtime_resource_override_reuses_the_same_manifest(
    deployment: tuple[FlotillaConfig, FakePlatform, SharedTree],
) -> None:
    cfg, platform, _ = deployment
    artifact = task()
    original_digest = artifact.digest
    for cpu, memory in (("2", "4Gi"), ("4", "8Gi")):
        configured = cfg.model_copy(
            update={"resources": ResourceDefaults(services={"main": Resource(cpu=cpu, memory=memory)})}
        )
        async with Flotilla(deployment=configured) as fleet:
            env = await open_fixture(fleet, RunRequest(artifact, f"resources-{cpu}"))
            service = (await env.info()).services[0]
            allocated = platform.spec_of(service.instance_id).resources
            assert (allocated.cpu, allocated.memory) == (cpu, memory)
            assert (service.cpu, service.memory) == (cpu, memory)
            assert artifact.digest == original_digest
            assert artifact.manifest().units["main"].resources.cpu == "1"
