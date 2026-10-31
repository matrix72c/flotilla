"""Public admission reservations and confirmed background process control."""

import asyncio

import pytest

from flotilla import Flotilla
from flotilla.config import FlotillaConfig, Resource
from flotilla.manifest import Resources
from flotilla.platform.base import ExecOutcomeUnknown
from flotilla.runtime.types import Task
from flotilla.testing.environments import FixtureRequest, open_fixture
from flotilla.testing.platform import FakePlatform, SharedTree
from flotilla.testing.runtime import deployment as deployment
from flotilla.testing.runtime import task


def test_reservation_uses_prepared_resources_and_deployment_overrides(
    deployment: tuple[FlotillaConfig, FakePlatform, SharedTree],
) -> None:
    cfg, _, _ = deployment
    main = task()
    manifest = main.manifest()
    unit = manifest.units["main"]
    verifier = Task(
        manifest.model_copy(
            update={"units": {"main": unit.model_copy(update={"resources": Resources(cpu="8", memory="16Gi")})}}
        )
    )
    fleet = Flotilla(deployment=cfg)
    assert fleet.reservation(main).resources["main"]["cpu"] == "1"
    assert fleet.reservation(verifier).resources["main"] == {"cpu": "8", "memory": "16Gi"}
    assert fleet.reservation(verifier).instances == 2  # one active and one reserved generation
    overridden = cfg.model_copy(
        update={"resources": cfg.resources.model_copy(update={"services": {"main": Resource(cpu="3", memory="1Gi")}})}
    )
    assert Flotilla(deployment=overridden).reservation(main).resources["main"]["cpu"] == "3"
    assert fleet.client_reservation().instances == 1


@pytest.mark.asyncio
async def test_wait_timeout_is_not_a_stop_and_stop_confirms_exit(
    deployment: tuple[FlotillaConfig, FakePlatform, SharedTree],
) -> None:
    cfg, platform, _ = deployment
    async with Flotilla(deployment=cfg) as fleet:
        env = await open_fixture(fleet, FixtureRequest(task(), "process"))
        process = await env.agent.start_process(["sleep", "30"])
        with pytest.raises(TimeoutError):
            await process.wait(timeout=0.01, poll_interval=0.001)
        assert (await process.status()).running
        await process.stop()
        status = await process.wait(timeout=1)
        assert status.found and not status.running
        await env.check()
        await env.release()
    assert not await platform.list({})


@pytest.mark.asyncio
async def test_waiter_cancellation_preserves_process_ownership(
    deployment: tuple[FlotillaConfig, FakePlatform, SharedTree],
) -> None:
    cfg, platform, _ = deployment
    async with Flotilla(deployment=cfg) as fleet:
        env = await open_fixture(fleet, FixtureRequest(task(), "cancel-wait"))
        process = await env.agent.start_process(["sleep", "30"])
        waiting = asyncio.create_task(process.wait())
        await asyncio.sleep(0)
        waiting.cancel()
        with pytest.raises(asyncio.CancelledError):
            await waiting
        assert (await process.status()).running
        await env.release()
    assert not await platform.list({})


@pytest.mark.asyncio
async def test_stop_of_exited_process_confirms_without_side_effects(
    deployment: tuple[FlotillaConfig, FakePlatform, SharedTree],
) -> None:
    cfg, platform, _ = deployment
    async with Flotilla(deployment=cfg) as fleet:
        env = await open_fixture(fleet, FixtureRequest(task(), "process"))
        process = await env.agent.start_process(["sleep", "30"])
        iid = next(i for i in platform.iids() if platform.pids(i))
        platform.finish_process(iid, platform.pids(iid)[0], 0)  # The agent exited on its own.
        await process.stop()
        assert (await process.status()).exit_code == 0
        await env.check()
        assert (await env.agent.exec(["true"])).exit_code == 0
        await env.release()
    assert not await platform.list({})


@pytest.mark.asyncio
async def test_stop_after_record_loss_raises_and_releases_the_environment(
    deployment: tuple[FlotillaConfig, FakePlatform, SharedTree],
) -> None:
    cfg, platform, _ = deployment
    async with Flotilla(deployment=cfg) as fleet:
        env = await open_fixture(fleet, FixtureRequest(task(), "process"))
        process = await env.agent.start_process(["sleep", "30"])
        iid = next(i for i in platform.iids() if platform.pids(i))
        platform.lose_processes(iid)  # The exec daemon lost its records.
        with pytest.raises(ExecOutcomeUnknown):
            await process.stop()
    assert not await platform.list({})
