"""Unit tests for `Monitor`: runtime liveness confirmation, renewal, `lost`, `check()`."""

from __future__ import annotations

from collections.abc import AsyncIterator
from dataclasses import replace

import pytest
import pytest_asyncio

from flotilla.core import labels
from flotilla.core.anchor import Anchor, AnchorSettings
from flotilla.core.plan import ExecContext, RestartPolicy, TrialPlan, Unit
from flotilla.core.runtime import Monitor, MonitorSettings
from flotilla.platform.base import (
    ErrorCategory,
    ExecResult,
    ExternalPolicy,
    FlotillaError,
    InstanceHandle,
    InstanceSpec,
    InstanceState,
    InstanceStatus,
    ProcessSpec,
    Resources,
)
from flotilla.testing.platform import FakePlatform, ManualClock

CTX = ExecContext(env={}, uid=0, gid=0, cwd="/")
ANCHOR = AnchorSettings(image="anchor@sha256:abc")
SETTINGS = MonitorSettings(ttl_s=3600, renew_before_s=1800)


def _unit(restart: RestartPolicy = "no") -> Unit:
    return Unit(image="x", resources=Resources(cpu="1", memory="1Gi"), context=CTX, command=("serve",), restart=restart)


def _transient() -> FlotillaError:
    return FlotillaError("503", stage="run", category=ErrorCategory.TRANSIENT, retryable=True)


@pytest_asyncio.fixture
async def anchor(platform: FakePlatform, clock: ManualClock) -> AsyncIterator[Anchor]:
    a = Anchor(platform, clock, "L1", ANCHOR)
    await clock.run(a.start())
    yield a
    await a.close()


async def _ready(
    platform: FakePlatform, clock: ManualClock, anchor: Anchor, plan: TrialPlan, settings: MonitorSettings = SETTINGS
) -> tuple[Monitor, dict[str, InstanceHandle], dict[str, str]]:
    """Create the units, start one main process in each, and return a started Monitor."""
    handles: dict[str, InstanceHandle] = {}
    pids: dict[str, str] = {}
    for name in sorted(plan.units):
        h = await platform.create(
            InstanceSpec(
                image="x",
                entrypoint=("sleep",),
                labels={labels.UNIT: name},
                timeout_seconds=min(settings.ttl_s, platform.caps.max_ttl_seconds or settings.ttl_s),
                resources=Resources(cpu="1", memory="1Gi"),
                volumes=(),
                external=ExternalPolicy(mode="none"),
            )
        )
        handles[name] = h
        pids[name] = await platform.start_process(h, ProcessSpec(("serve",), 0, 0, "/", {}, None))
    monitor = Monitor(platform, clock, anchor, plan, handles, pids, settings)
    monitor.start()
    return monitor, handles, pids


def _plan(**units: Unit) -> TrialPlan:
    return TrialPlan(task="t", task_files=None, units=units or {"main": _unit(), "db": _unit()})


# ───────────────────────────── Liveness confirmation ─────────────────────────────


@pytest.mark.asyncio
async def test_healthy_trial_stays_healthy_and_never_lists(
    platform: FakePlatform, clock: ManualClock, anchor: Anchor
) -> None:
    monitor, _, _ = await _ready(platform, clock, anchor, _plan())
    lists = platform.calls["list"]
    await clock.advance(3 * 3600)
    assert monitor.lost is None
    assert await clock.run(monitor.check()) is None
    assert platform.calls["list"] == lists  # no label listing at runtime
    await monitor.stop()


@pytest.mark.asyncio
async def test_probes_are_staggered_and_periodic(platform: FakePlatform, clock: ManualClock, anchor: Anchor) -> None:
    monitor, _, _ = await _ready(platform, clock, anchor, _plan())
    await clock.advance(0)  # let the renewal loop read initial expiries (one get per unit) before counting
    gets0 = platform.calls["get"]
    await clock.advance(150.0)  # two units: the first checked at 150s, the second at 300s
    assert platform.calls["get"] - gets0 == 1
    await clock.advance(150.0)
    assert platform.calls["get"] - gets0 == 2
    await clock.advance(SETTINGS.probe_interval_s * 4)
    assert platform.calls["get"] - gets0 == 2 + 8  # once per unit every 300s
    await monitor.stop()


@pytest.mark.asyncio
async def test_instance_gone_marks_lost(platform: FakePlatform, clock: ManualClock, anchor: Anchor) -> None:
    monitor, handles, _ = await _ready(platform, clock, anchor, _plan())
    await platform.delete(handles["db"].iid)
    await clock.advance(SETTINGS.probe_interval_s)
    err = monitor.lost
    assert err is not None
    assert (err.stage, err.category, err.retryable) == ("run", ErrorCategory.LOST, True)
    assert "db" in str(err)
    assert await monitor.check() is err  # returns directly once marked
    await monitor.stop()


@pytest.mark.asyncio
@pytest.mark.parametrize("status", [InstanceStatus.TERMINAL, InstanceStatus.UNKNOWN, InstanceStatus.PENDING])
async def test_non_running_status_marks_lost(
    platform: FakePlatform, clock: ManualClock, anchor: Anchor, status: InstanceStatus
) -> None:
    monitor, handles, _ = await _ready(platform, clock, anchor, _plan())
    platform.set_status(handles["main"].iid, status, reason="Paused")
    await clock.advance(SETTINGS.probe_interval_s)
    assert monitor.lost is not None and "Paused" in str(monitor.lost)
    await monitor.stop()


@pytest.mark.asyncio
async def test_transient_probe_error_is_not_lost(platform: FakePlatform, clock: ManualClock, anchor: Anchor) -> None:
    monitor, _, _ = await _ready(platform, clock, anchor, _plan())
    for _ in range(3):
        platform.inject_fault("get", _transient())
    await clock.advance(SETTINGS.probe_interval_s * 2)
    assert monitor.lost is None
    await monitor.stop()


@pytest.mark.asyncio
async def test_confirm_on_not_found_marks_lost_immediately(
    platform: FakePlatform, clock: ManualClock, anchor: Anchor
) -> None:
    monitor, handles, _ = await _ready(platform, clock, anchor, _plan())
    await platform.delete(handles["db"].iid)
    err = await monitor.confirm("db")  # the client's exec call got not_found
    assert err is not None and err.category is ErrorCategory.LOST
    assert await monitor.confirm("main") is err  # already marked: no more queries
    await monitor.stop()


@pytest.mark.asyncio
async def test_confirm_on_healthy_unit_is_none(platform: FakePlatform, clock: ManualClock, anchor: Anchor) -> None:
    monitor, _, _ = await _ready(platform, clock, anchor, _plan())
    assert await monitor.confirm("db") is None
    await monitor.stop()


@pytest.mark.asyncio
async def test_launch_deactivation_marks_lost(platform: FakePlatform, clock: ManualClock, anchor: Anchor) -> None:
    monitor, _, _ = await _ready(platform, clock, anchor, _plan())
    for _ in range(1000):
        platform.inject_fault("renew", _transient())
    await clock.advance(ANCHOR.grace_s)
    assert monitor.lost is not None and "was deactivated" in str(monitor.lost)
    await monitor.stop()


@pytest.mark.asyncio
async def test_first_lost_reason_is_kept(platform: FakePlatform, clock: ManualClock, anchor: Anchor) -> None:
    monitor, handles, _ = await _ready(platform, clock, anchor, _plan())
    await platform.delete(handles["db"].iid)
    first = await monitor.confirm("db")
    await platform.delete(handles["main"].iid)
    await clock.advance(SETTINGS.probe_interval_s)
    assert monitor.lost is first and "db" in str(first)
    await monitor.stop()


# ───────────────────────────── Renewal ─────────────────────────────


@pytest.mark.asyncio
async def test_renews_when_ttl_runs_low(platform: FakePlatform, clock: ManualClock, anchor: Anchor) -> None:
    monitor, handles, _ = await _ready(platform, clock, anchor, _plan())
    renews0 = platform.calls["renew"]
    await clock.advance(SETTINGS.ttl_s - SETTINGS.renew_before_s - 1)
    unit_renews = platform.calls["renew"] - renews0
    await clock.advance(2)
    # Each of the two units renews once (the anchor renews separately, so compare only the extra 2 in the delta).
    assert platform.calls["renew"] - renews0 - unit_renews >= 2
    for h in handles.values():
        state = await platform.get(h.iid)
        assert state.expires_at is not None
        assert (state.expires_at - clock.now()).total_seconds() > SETTINGS.ttl_s - 10
    # Long trial: renewal keeps going and instances never expire.
    await clock.advance(5 * SETTINGS.ttl_s)
    assert monitor.lost is None
    for h in handles.values():
        state = await platform.get(h.iid)
        assert state.expires_at is not None and state.expires_at > clock.now()
    await monitor.stop()


@pytest.mark.asyncio
async def test_renew_retries_then_lost_when_margin_exhausted(
    platform: FakePlatform, clock: ManualClock, anchor: Anchor, monkeypatch: pytest.MonkeyPatch
) -> None:
    monitor, handles, _ = await _ready(platform, clock, anchor, _plan())
    original = platform.renew
    unit_iids = {h.iid for h in handles.values()}

    async def failing(iid: str, expires_at: object) -> None:
        if iid in unit_iids:
            raise _transient()
        await original(iid, expires_at)  # type: ignore[arg-type]

    monkeypatch.setattr(platform, "renew", failing)
    await clock.advance(SETTINGS.ttl_s - SETTINGS.renew_before_s + 60)
    assert monitor.lost is None  # margin left: retrying
    await clock.advance(SETTINGS.renew_before_s - SETTINGS.renew_margin_s)
    assert monitor.lost is not None and "renewal failed" in str(monitor.lost)
    # When marked lost the instance has not expired yet (margin kept), so the platform has not reclaimed it.
    state = await platform.get(handles["main"].iid)
    assert state.expires_at is not None and state.expires_at > clock.now()
    await monitor.stop()


@pytest.mark.asyncio
async def test_transient_renew_failure_recovers(
    platform: FakePlatform, clock: ManualClock, anchor: Anchor, monkeypatch: pytest.MonkeyPatch
) -> None:
    monitor, handles, _ = await _ready(platform, clock, anchor, _plan())
    original = platform.renew
    unit_iids = {h.iid for h in handles.values()}
    failures = [3]

    async def flaky(iid: str, expires_at: object) -> None:
        if iid in unit_iids and failures[0]:
            failures[0] -= 1
            raise _transient()
        await original(iid, expires_at)  # type: ignore[arg-type]

    monkeypatch.setattr(platform, "renew", flaky)
    await clock.advance(2 * SETTINGS.ttl_s)
    assert monitor.lost is None
    await monitor.stop()


@pytest.mark.asyncio
async def test_short_platform_ttl_does_not_spin(
    platform: FakePlatform, clock: ManualClock, anchor: Anchor, monkeypatch: pytest.MonkeyPatch
) -> None:
    # The deployment TTL cap (600s) is below the default renewal lead (1800s): the lead shrinks to half the TTL (300s),
    # so each unit renews about every 300s instead of being "due" again right after renewing and renewing repeatedly.
    platform.caps = replace(platform.caps, max_ttl_seconds=600)
    monitor, handles, _ = await _ready(platform, clock, anchor, _plan(), MonitorSettings())
    unit_iids = {h.iid for h in handles.values()}
    renew_times: list[float] = []
    original = platform.renew

    async def recording(iid: str, expires_at: object) -> None:
        if iid in unit_iids:
            renew_times.append(clock.monotonic())
        await original(iid, expires_at)  # type: ignore[arg-type]

    monkeypatch.setattr(platform, "renew", recording)
    await clock.advance(3600)
    assert monitor.lost is None
    per_unit = len(renew_times) / len(unit_iids)
    assert 3600 / 300 - 2 <= per_unit <= 3600 / 300 + 2
    for h in handles.values():  # instances never expired
        state = await platform.get(h.iid)
        assert state.expires_at is not None and state.expires_at > clock.now()
    await monitor.stop()


# ───────────────────────────── check() ─────────────────────────────


@pytest.mark.asyncio
async def test_check_detects_lost_process_record(platform: FakePlatform, clock: ManualClock, anchor: Anchor) -> None:
    monitor, handles, _ = await _ready(platform, clock, anchor, _plan())
    platform.lose_processes(handles["db"].iid)
    err = await clock.run(monitor.check())
    assert isinstance(err, FlotillaError) and err.category is ErrorCategory.LOST
    await monitor.stop()


@pytest.mark.asyncio
async def test_check_detects_dead_exec_channel(platform: FakePlatform, clock: ManualClock, anchor: Anchor) -> None:
    monitor, handles, _ = await _ready(platform, clock, anchor, _plan())
    platform.set_exec_handler(
        lambda iid, proc: ExecResult(exit_code=0 if iid != handles["db"].iid else 1, stdout=b"", stderr=b"")
    )
    err = await clock.run(monitor.check())
    assert isinstance(err, FlotillaError) and err.category is ErrorCategory.LOST and "db" in str(err)
    await monitor.stop()


@pytest.mark.asyncio
async def test_check_timeout_is_lost(platform: FakePlatform, clock: ManualClock, anchor: Anchor) -> None:
    # Unknown is not healthy: a confirmation timeout counts as lost.
    monitor, _, _ = await _ready(platform, clock, anchor, _plan())
    platform.set_latency("exec", 60.0)
    err = await clock.run(monitor.check())
    assert isinstance(err, FlotillaError) and err.category is ErrorCategory.LOST
    await monitor.stop()


@pytest.mark.asyncio
async def test_check_transient_query_recovers_within_budget(
    platform: FakePlatform, clock: ManualClock, anchor: Anchor
) -> None:
    monitor, _, _ = await _ready(platform, clock, anchor, _plan())
    await clock.advance(0)  # finish background startup reads so the fault below lands on check()'s get
    platform.inject_fault("get", _transient())
    gets = platform.calls["get"]
    err = await clock.run(monitor.check())  # does not raise
    assert platform.calls["get"] > gets
    assert err is None and monitor.lost is None
    await monitor.stop()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("policy", "exit_code", "voided"),
    [
        ("always", 0, True),
        ("unless-stopped", 0, True),
        ("on-failure", 1, True),
        ("on-failure", 0, False),
        ("no", 1, False),
    ],
)
async def test_check_restart_policy_exit(
    platform: FakePlatform,
    clock: ManualClock,
    anchor: Anchor,
    policy: RestartPolicy,
    exit_code: int,
    voided: bool,
) -> None:
    # A service exits and Docker would restart it: the sample is void (service, not retryable), not lost.
    monitor, handles, pids = await _ready(platform, clock, anchor, _plan(web=_unit(policy), main=_unit()))
    platform.finish_process(handles["web"].iid, pids["web"], exit_code)
    err = await clock.run(monitor.check())
    if voided:
        assert isinstance(err, FlotillaError)
        assert (err.category, err.retryable, err.service) == (ErrorCategory.SERVICE, False, "web")
        assert monitor.lost is None  # not an infrastructure failure
    else:
        assert err is None
    await monitor.stop()


@pytest.mark.asyncio
async def test_business_process_exit_without_restart_is_not_lost(
    platform: FakePlatform, clock: ManualClock, anchor: Anchor
) -> None:
    # A main process exit is in-task behavior (possibly caused by the agent), not an infrastructure failure.
    monitor, handles, pids = await _ready(platform, clock, anchor, _plan())
    platform.finish_process(handles["db"].iid, pids["db"], 139)
    await clock.advance(SETTINGS.probe_interval_s * 3)
    assert monitor.lost is None
    assert await clock.run(monitor.check()) is None
    await monitor.stop()


@pytest.mark.asyncio
async def test_stop_ends_background_calls(platform: FakePlatform, clock: ManualClock, anchor: Anchor) -> None:
    monitor, _, _ = await _ready(platform, clock, anchor, _plan())
    await monitor.stop()
    gets = platform.calls["get"]
    await clock.advance(10 * 3600)
    assert platform.calls["get"] == gets


def test_settings_validation() -> None:
    with pytest.raises(ValueError):
        MonitorSettings(renew_before_s=7200, ttl_s=3600)
    with pytest.raises(ValueError):
        MonitorSettings(renew_margin_s=1800, renew_before_s=1800)


@pytest.mark.asyncio
async def test_check_persistent_transport_failure_remains_bounded_and_lost(
    platform: FakePlatform, clock: ManualClock, anchor: Anchor, monkeypatch: pytest.MonkeyPatch
) -> None:
    monitor, handles, _ = await _ready(platform, clock, anchor, _plan())
    await clock.advance(0)
    original = platform.get
    iids = {h.iid for h in handles.values()}
    calls = 0

    async def fail(iid: str) -> InstanceState:
        nonlocal calls
        if iid in iids:
            calls += 1
            raise _transient()
        return await original(iid)

    monkeypatch.setattr(platform, "get", fail)
    began = clock.monotonic()
    err = await clock.run(monitor.check())
    assert isinstance(err, FlotillaError) and err.category is ErrorCategory.LOST
    assert calls > 1
    assert clock.monotonic() - began <= SETTINGS.check_timeout_s + 1
    await monitor.stop()
