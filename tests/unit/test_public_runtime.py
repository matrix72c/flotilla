"""Real public client + background loop + fake platform, with cancellation and resource ownership."""

from __future__ import annotations

import asyncio
import json
import time
from typing import Any

import pytest

from flotilla import AdmissionTimeout, Flotilla
from flotilla.config import FlotillaConfig
from flotilla.core import labels
from flotilla.platform.base import ErrorCategory, ExecResult, FlotillaError, ProcessSpec
from flotilla.platform.clock import SystemClock
from flotilla.runtime.manager import Manager
from flotilla.testing.environments import FixtureRequest as RunRequest
from flotilla.testing.environments import environment, open_fixture
from flotilla.testing.platform import FakePlatform, SharedTree
from flotilla.testing.runtime import deployment as deployment
from flotilla.testing.runtime import task


@pytest.mark.asyncio
async def test_public_lifecycle_and_exec_environment(
    deployment: tuple[FlotillaConfig, FakePlatform, SharedTree],
) -> None:
    cfg, platform, _ = deployment
    async with Flotilla(deployment=cfg) as fleet:
        async with environment(fleet, RunRequest(task(), "sample", exec_env={"main": {"LITERAL": "$(no)"}})) as env:
            assert (await env.info()).request_id == "sample"
            result = await env.agent.exec(["echo", "hello"], env={"TOKEN": "literal"})
            assert json.loads(result.stdout)["env"]["LITERAL"] == "$(no)"
            with pytest.raises(KeyError):
                env.service("unknown")
            await env.agent.write_file("/tmp/file", b"hello")
            assert await env.agent.read_file("/tmp/file") == b"hello"
            await env.check()
        await env.release()
        with pytest.raises(FlotillaError):
            await env.agent.exec(["true"])
        assert (await fleet.drain(2)).clean
    assert fleet.cleanup_report is not None and fleet.cleanup_report.clean
    assert not await platform.list({labels.LAUNCH: fleet.cleanup_report.launch_id})


@pytest.mark.asyncio
async def test_timeout_runs_while_callers_loop_is_blocked(
    deployment: tuple[FlotillaConfig, FakePlatform, SharedTree],
) -> None:
    cfg, _, _ = deployment
    async with Flotilla(deployment=cfg) as fleet:
        env = await open_fixture(fleet, RunRequest(task(), "short", timeout=0.15))
        time.sleep(0.3)  # deliberately model a colocated training step blocking the caller's loop
        with pytest.raises(FlotillaError):
            await env.check()
        assert (await fleet.drain(2)).clean


@pytest.mark.asyncio
async def test_cancel_open_reclaims_late_creation(deployment: tuple[FlotillaConfig, FakePlatform, SharedTree]) -> None:
    cfg, platform, _ = deployment
    async with Flotilla(deployment=cfg) as fleet:
        platform.set_latency("create", 0.1, phase="after")
        pending = asyncio.create_task(open_fixture(fleet, RunRequest(task(), "cancel")))
        await asyncio.sleep(0.02)
        pending.cancel()
        with pytest.raises(asyncio.CancelledError):
            await pending
        assert (await fleet.drain(2)).clean
    assert fleet.cleanup_report is not None and fleet.cleanup_report.clean


@pytest.mark.asyncio
async def test_lost_create_response_is_adopted_within_the_create_stage(
    deployment: tuple[FlotillaConfig, FakePlatform, SharedTree],
) -> None:
    # The platform creates the sandbox but its response never arrives in time. The request deadline, not the whole
    # create stage, decides when to look the sandbox up by labels and adopt it.
    cfg, platform, _ = deployment
    timeouts = cfg.timeouts.model_copy(update={"create_request": 0.1, "create": 3.0})
    cfg = cfg.model_copy(update={"timeouts": timeouts})
    async with Flotilla(deployment=cfg) as fleet:
        platform.set_latency("create", 30.0, phase="after")
        began = time.monotonic()
        async with environment(fleet, RunRequest(task(), "lost-response", timeout=5)) as env:
            assert time.monotonic() - began < 2
            assert (await env.agent.exec(["true"])).exit_code == 0
        assert (await fleet.drain(2)).clean


@pytest.mark.asyncio
async def test_queue_deadline_and_instance_budget(deployment: tuple[FlotillaConfig, FakePlatform, SharedTree]) -> None:
    cfg, _, _ = deployment
    cfg = cfg.model_copy(update={"limits": cfg.limits.model_copy(update={"instance_budget": 3})})
    async with Flotilla(deployment=cfg) as fleet:
        first = await open_fixture(fleet, RunRequest(task(), "first"))
        with pytest.raises(AdmissionTimeout, match=r"active=1/\d+, reserved=3\+2/3"):
            await open_fixture(fleet, RunRequest(task(), "queued", timeout=0.05))
        await first.release()
        async with environment(fleet, RunRequest(task(), "after-cleanup", timeout=2)):
            pass


@pytest.mark.asyncio
async def test_cleanup_failure_preserves_budget_and_reports_instances(
    deployment: tuple[FlotillaConfig, FakePlatform, SharedTree],
) -> None:
    from flotilla.runtime.types import CleanupError

    cfg, platform, _ = deployment
    cfg = cfg.model_copy(update={"limits": cfg.limits.model_copy(update={"instance_budget": 3})})
    fleet = Flotilla(deployment=cfg)
    original_delete = platform.delete
    left = ""
    with pytest.raises(CleanupError) as failed:
        async with fleet:
            env = await open_fixture(fleet, RunRequest(task(), "leak"))
            left = (await env.info()).services[0].instance_id

            async def refuse(iid: str) -> None:
                if iid == left:
                    raise FlotillaError("delete refused", stage="run", category=ErrorCategory.TRANSIENT, retryable=True)
                await original_delete(iid)

            platform.delete = refuse  # type: ignore[method-assign]

            async def shorten(manager: Manager) -> None:
                manager.deleter.retry_deadline_s = 0.001

            await fleet._call(shorten)
            await env.release()
            report = await fleet.drain(2)
            assert not report.clean
            assert left in report.remaining[0].instances
            with pytest.raises(TimeoutError):
                await open_fixture(fleet, RunRequest(task(), "must-not-overcommit", timeout=0.05))
    assert left in failed.value.report.remaining[0].instances


@pytest.mark.asyncio
async def test_directory_cleanup_failure_is_not_success(
    deployment: tuple[FlotillaConfig, FakePlatform, SharedTree],
) -> None:
    from flotilla.runtime.types import CleanupError

    cfg, platform, _ = deployment
    original_exec = platform.exec_handler

    def fail_remove(iid: str, proc: ProcessSpec) -> ExecResult:
        if proc.argv[0] == "rm" and "owned-directory" in proc.argv[-1]:
            return ExecResult(1, b"", b"permission denied")
        return original_exec(iid, proc)

    fleet = Flotilla(deployment=cfg)
    with pytest.raises(CleanupError) as failed:
        async with fleet:
            env = await open_fixture(fleet, RunRequest(task(), "dir-failure"))

            async def add_directory(manager: Manager) -> None:
                manager.reaper.add_dir(env.id, f"launches/{manager.launch_id}/owned-directory")

            await fleet._call(add_directory)
            platform.set_exec_handler(fail_remove)
            await env.release()
            assert not (await fleet.drain(2)).clean
    assert failed.value.report.remaining[0].directories


@pytest.mark.asyncio
async def test_cancel_during_ready_delivery_does_not_orphan(
    deployment: tuple[FlotillaConfig, FakePlatform, SharedTree],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    cfg, _, _ = deployment
    original = Manager.open
    import threading

    ready = threading.Event()

    async def delayed(self: Manager, *args: Any, **kwargs: Any):  # type: ignore[no-untyped-def]
        info = await original(self, *args, **kwargs)
        ready.set()
        await asyncio.sleep(1)
        return info

    monkeypatch.setattr(Manager, "open", delayed)
    async with Flotilla(deployment=cfg) as fleet:
        pending = asyncio.create_task(open_fixture(fleet, RunRequest(task(), "ready-cancel")))
        assert await asyncio.to_thread(ready.wait, 3)
        pending.cancel()
        with pytest.raises(asyncio.CancelledError):
            await pending
        assert (await fleet.drain(2)).clean


@pytest.mark.asyncio
async def test_close_during_anchor_creation_reconciles_unknown_instance(
    deployment: tuple[FlotillaConfig, FakePlatform, SharedTree],
) -> None:
    cfg, platform, _ = deployment
    platform.set_latency("create", 0.2, phase="after")
    fleet = Flotilla(deployment=cfg)
    opening = asyncio.create_task(fleet.start())
    await asyncio.sleep(0.04)
    report = await fleet.close(2)
    await asyncio.gather(opening, return_exceptions=True)
    assert report.clean
    assert not await platform.list({labels.LAUNCH: report.launch_id})


@pytest.mark.asyncio
async def test_failed_share_verification_reclaims_anchor(
    deployment: tuple[FlotillaConfig, FakePlatform, SharedTree],
) -> None:
    cfg, platform, _ = deployment
    cfg = cfg.model_copy(update={"share": cfg.share.model_copy(update={"release_sha256": "f" * 64})})
    fleet = Flotilla(deployment=cfg)
    with pytest.raises(FlotillaError):
        await fleet.start()
    assert fleet.cleanup_report is not None and fleet.cleanup_report.clean
    assert not await platform.list({labels.LAUNCH: fleet.cleanup_report.launch_id})


@pytest.mark.asyncio
async def test_cancelling_one_start_waiter_does_not_cancel_shared_start(
    deployment: tuple[FlotillaConfig, FakePlatform, SharedTree],
) -> None:
    cfg, platform, _ = deployment
    platform.set_latency("create", 0.1, phase="after")
    fleet = Flotilla(deployment=cfg)
    first = asyncio.create_task(fleet.start())
    second = asyncio.create_task(fleet.start())
    await asyncio.sleep(0.02)
    first.cancel()
    with pytest.raises(asyncio.CancelledError):
        await first
    await second
    assert (await fleet.close(2)).clean


@pytest.mark.asyncio
async def test_explicit_development_acceptance_allows_operations_and_records_gaps(
    deployment: tuple[FlotillaConfig, FakePlatform, SharedTree],
) -> None:
    cfg, platform, _ = deployment
    capability_data = cfg.load_report().model_dump(mode="json")
    gaps = {"inbound_isolation", "exec_auth", "no_auto_mount"}
    for section, name in [("network", "inbound_isolation"), ("runtime", "exec_auth"), ("storage", "no_auto_mount")]:
        capability_data[section][name] = {"ok": False, "source": "declared", "evidence": "known development gap"}
    cfg.capabilities_path().write_text(json.dumps(capability_data))
    platform.caps = cfg.load_report().to_capabilities()
    cfg = cfg.model_copy(update={"security": cfg.security.model_copy(update={"accept_insecure": frozenset(gaps)})})
    from flotilla.manifest import Network
    from flotilla.runtime.types import Task

    # A general development waiver does not promise Compose network_mode: none isolation.
    connected = Task(
        task()
        .manifest()
        .model_copy(
            update={
                "networks": {"default": Network(internal=False, members=["main"], names={})},
                "external_units": ["main"],
            }
        )
    )
    async with Flotilla(deployment=cfg) as fleet, environment(fleet, RunRequest(connected, "development")) as env:
        info = await env.info()
        assert set(info.security_gaps) == gaps
        assert set(info.accepted_security_gaps) == gaps
        assert (await env.agent.exec(["true"])).exit_code == 0
    assert fleet.cleanup_report is not None and fleet.cleanup_report.clean


@pytest.mark.asyncio
@pytest.mark.parametrize("known,confirmed", [(True, True), (False, False), (True, False)])
async def test_cancel_confirms_exact_command_or_releases_environment(
    deployment: tuple[FlotillaConfig, FakePlatform, SharedTree],
    monkeypatch: pytest.MonkeyPatch,
    known: bool,
    confirmed: bool,
) -> None:
    import threading

    from flotilla.platform.base import ExecAttempt, InstanceHandle

    cfg, platform, _ = deployment
    started = threading.Event()
    original = platform.exec

    async def execute(handle: InstanceHandle, proc: ProcessSpec, *, attempt: ExecAttempt | None = None) -> ExecResult:
        if proc.argv != ("hang",):
            return await original(handle, proc, attempt=attempt)
        assert attempt is not None
        attempt.sent = True
        if known:
            attempt.command_id = await platform.start_process(handle, proc)
        started.set()
        await asyncio.Future()
        raise AssertionError("unreachable")

    async def broken_interrupt(handle: InstanceHandle, pid: str) -> None:
        platform.lose_processes(handle.iid)

    monkeypatch.setattr(platform, "exec", execute)
    if not confirmed:
        monkeypatch.setattr(platform, "interrupt", broken_interrupt)
    async with Flotilla(deployment=cfg) as fleet:
        env = await open_fixture(fleet, RunRequest(task(), "cancel"))
        call = asyncio.create_task(env.agent.exec(["hang"]))
        assert await asyncio.to_thread(started.wait, 2)
        call.cancel()
        with pytest.raises(asyncio.CancelledError):
            await call

        # Cancellation has settled before returning to the caller: log/file
        # collection may begin immediately, without polling private state.
        if confirmed:
            await env.check()
            assert (await env.agent.exec(["true"])).exit_code == 0
        else:
            with pytest.raises(FlotillaError):
                await env.agent.exec(["true"])
        await env.release()
        assert (await fleet.drain(2)).clean


@pytest.mark.asyncio
async def test_command_local_deadline_is_confirmed_timeout(
    deployment: tuple[FlotillaConfig, FakePlatform, SharedTree],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from flotilla.platform.base import ExecAttempt, InstanceHandle

    cfg, platform, _ = deployment
    original = platform.exec

    async def execute(handle: InstanceHandle, proc: ProcessSpec, *, attempt: ExecAttempt | None = None) -> ExecResult:
        if proc.argv != ("hang",):
            return await original(handle, proc, attempt=attempt)
        assert attempt
        attempt.sent = True
        attempt.command_id = await platform.start_process(handle, proc)
        await asyncio.Future()
        raise AssertionError("unreachable")

    monkeypatch.setattr(platform, "exec", execute)
    async with Flotilla(deployment=cfg) as fleet, environment(fleet, RunRequest(task(), "deadline")) as env:
        result = await env.agent.exec(["hang"], timeout=0.05)
        assert result.timed_out
        await env.check()


@pytest.mark.asyncio
async def test_events_correlate_queue_execution_and_cleanup_without_command_secrets(
    deployment: tuple[FlotillaConfig, FakePlatform, SharedTree],
) -> None:
    from flotilla import RuntimeEvent

    cfg, _, _ = deployment
    events: list[RuntimeEvent] = []

    def consume(event: RuntimeEvent) -> None:
        events.append(event)
        if event.kind == "operation_finished":
            raise RuntimeError("consumer failure must not break cleanup")

    async with Flotilla(deployment=cfg, on_event=consume) as fleet:
        async with environment(fleet, RunRequest(task(), "event-test")) as env:
            await env.agent.exec(["secret-command"], env={"SECRET": "private-value"})
        assert (await fleet.drain(2)).clean
    kinds = {e.kind for e in events}
    assert {"queued", "admitted", "create", "operation_started", "operation_finished", "cleanup_finished"} <= kinds
    assert all(e.request_id == "event-test" and e.task_digest == events[0].task_digest for e in events)
    assert "secret-command" not in repr(events) and "private-value" not in repr(events)


@pytest.mark.asyncio
async def test_expiry_before_post_is_infrastructure_not_command_timeout(
    deployment: tuple[FlotillaConfig, FakePlatform, SharedTree],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from flotilla.platform.base import ExecAttempt, InstanceHandle

    cfg, platform, _ = deployment
    original = platform.exec

    async def execute(handle: InstanceHandle, proc: ProcessSpec, *, attempt: ExecAttempt | None = None) -> ExecResult:
        if proc.argv == ("queued",):
            # Represents a command waiting at the backend's shared rate gate.
            await asyncio.Future()
        return await original(handle, proc, attempt=attempt)

    monkeypatch.setattr(platform, "exec", execute)
    async with Flotilla(deployment=cfg) as fleet, environment(fleet, RunRequest(task(), "queue-expiry")) as env:
        with pytest.raises(FlotillaError) as error:
            await env.agent.exec(["queued"], timeout=0.01)
        assert error.value.category is ErrorCategory.CAPACITY
        await env.check()


@pytest.mark.asyncio
@pytest.mark.parametrize("known", [False, True])
async def test_unconfirmed_deadline_reports_unknown_outcome_not_caller_cancellation(
    deployment: tuple[FlotillaConfig, FakePlatform, SharedTree],
    monkeypatch: pytest.MonkeyPatch,
    known: bool,
) -> None:
    from flotilla.platform.base import ExecAttempt, ExecOutcomeUnknown, InstanceHandle

    cfg, platform, _ = deployment
    original = platform.exec

    async def execute(handle: InstanceHandle, proc: ProcessSpec, *, attempt: ExecAttempt | None = None) -> ExecResult:
        if proc.argv != ("hang",):
            return await original(handle, proc, attempt=attempt)
        assert attempt is not None
        attempt.sent = True
        if known:
            attempt.command_id = await platform.start_process(handle, proc)
            platform.lose_processes(handle.iid)
        await asyncio.Future()
        raise AssertionError("unreachable")

    monkeypatch.setattr(platform, "exec", execute)
    async with Flotilla(deployment=cfg) as fleet:
        env = await open_fixture(fleet, RunRequest(task(), "unknown-deadline"))
        with pytest.raises(ExecOutcomeUnknown):
            await env.agent.exec(["hang"], timeout=0.02)
        with pytest.raises(FlotillaError):
            await env.check()
        assert (await fleet.drain(2)).clean


@pytest.mark.parametrize("entry", ["config", "runtime"])
@pytest.mark.asyncio
async def test_configured_create_timeout_reaches_http(
    deployment: tuple[FlotillaConfig, FakePlatform, SharedTree], monkeypatch: pytest.MonkeyPatch, entry: str
) -> None:
    import httpx

    from flotilla import config
    from flotilla.platform.base import ExternalPolicy, InstanceSpec
    from flotilla.platform.base import Resources as PlatformResources
    from flotilla.platform.opensandbox import OpenSandboxPlatform, build
    from flotilla.runtime import manager

    cfg, fake, _ = deployment
    cfg = cfg.model_copy(update={"timeouts": cfg.timeouts.model_copy(update={"create_request": 45.0})})
    monkeypatch.setenv("FLOTILLA_CRED", "test-only")
    requests: list[httpx.Request] = []

    def handle(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(200, json={"id": "owned"})

    built: dict[str, Any] = {}

    def factory(**kwargs: Any) -> Any:
        built.update(kwargs)
        return build(**kwargs, transport=httpx.MockTransport(handle))

    monkeypatch.setattr(config, "build_opensandbox", factory)
    # The CLI commands construct through `build_platform`; the runtime must reach the same factory.
    platform = (
        cfg.build_platform(clock=SystemClock(), caps=fake.caps) if entry == "config" else manager.Manager(cfg).platform
    )
    assert isinstance(platform, OpenSandboxPlatform)
    try:
        await platform.create(
            InstanceSpec(
                image="test",
                entrypoint=("sleep", "60"),
                labels={},
                timeout_seconds=60,
                resources=PlatformResources(cpu="1", memory="512Mi"),
                volumes=(),
                external=ExternalPolicy(mode="none"),
            )
        )
        assert requests[0].extensions["timeout"]["read"] == 45.0
        assert built["max_connections"] == 100  # the default pool size
    finally:
        await platform.aclose()


@pytest.mark.asyncio
async def test_queueing_does_not_consume_the_startup_budget(
    deployment: tuple[FlotillaConfig, FakePlatform, SharedTree],
) -> None:
    from flotilla import OpenOptions

    cfg, _, _ = deployment
    cfg = cfg.model_copy(update={"limits": cfg.limits.model_copy(update={"trial_concurrency": 1})})
    async with Flotilla(deployment=cfg) as fleet:
        first = await open_fixture(fleet, RunRequest(task(), "first"))
        # Startup alone fits in 0.3s; the queue wait (0.4s) is longer and must not count against it.
        queued = asyncio.create_task(
            fleet.open(task(), OpenOptions(request_id="queued", admission_timeout=5, startup_timeout=0.3))
        )
        await asyncio.sleep(0.4)
        assert not queued.done()
        await first.release()
        env = await asyncio.wait_for(queued, 5)
        await env.release()
        assert (await fleet.drain(2)).clean


@pytest.mark.asyncio
async def test_client_start_downloads_only_the_release_manifest(
    deployment: tuple[FlotillaConfig, FakePlatform, SharedTree],
) -> None:
    cfg, platform, _ = deployment
    downloads: list[str] = []
    original = platform.read_file

    async def read_file(handle: Any, path: str) -> bytes:
        downloads.append(path)
        return await original(handle, path)

    platform.read_file = read_file  # type: ignore[method-assign]
    async with Flotilla(deployment=cfg) as fleet:
        await fleet.start()
    assert len(downloads) == 1 and downloads[0].endswith("/RELEASE.json")


@pytest.mark.asyncio
async def test_instance_ttl_outlives_the_startup_budget(
    deployment: tuple[FlotillaConfig, FakePlatform, SharedTree],
) -> None:
    """Instances are renewed only once ready, so their creation TTL must cover the whole startup budget."""
    from flotilla import OpenOptions

    cfg, platform, _ = deployment
    renew_before = cfg.reaper.renew_before_s
    async with Flotilla(deployment=cfg) as fleet:
        long = await fleet.open(task(), OpenOptions(request_id="long", startup_timeout=4 * cfg.reaper.trial_ttl))
        short = await fleet.open(task(), OpenOptions(request_id="short", startup_timeout=1))
        for env, expected in (
            (long, 4 * cfg.reaper.trial_ttl + renew_before),
            (short, cfg.reaper.trial_ttl),
        ):
            info = await env.info()
            assert all(platform.spec_of(s.instance_id).timeout_seconds == expected for s in info.services)
            await env.release()
        assert (await fleet.drain(2)).clean
