"""Unit tests for `Reaper`."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator

import pytest
import pytest_asyncio

from flotilla.core import labels
from flotilla.core.anchor import Anchor, AnchorSettings
from flotilla.core.deletion import Deleter
from flotilla.core.reaper import Reaper, ReaperSettings
from flotilla.platform.base import (
    ErrorCategory,
    ExternalPolicy,
    FlotillaError,
    InstanceSpec,
    InstanceStatus,
    Resources,
)
from tests.support.platform import FakePlatform, ManualClock, SharedTree

ANCHOR = AnchorSettings(image="anchor@sha256:abc")
SETTINGS = ReaperSettings()
VIS = 30.0  # list_visibility_s from the conftest capability report


def _spec(trial: str, unit: str, launch: str = "L1") -> InstanceSpec:
    return InstanceSpec(
        image="svc@sha256:abc",
        entrypoint=("/.flotilla/bin/busybox", "sleep", "2147483647"),
        labels={labels.LAUNCH: launch, labels.TRIAL: trial, labels.UNIT: unit, labels.ROLE: labels.ROLE_UNIT},
        timeout_seconds=3600,
        resources=Resources(cpu="1", memory="2Gi"),
        volumes=(),
        external=ExternalPolicy(mode="none"),
    )


def _err(category: ErrorCategory) -> FlotillaError:
    return FlotillaError(category.value, stage="run", category=category, retryable=True)


def _fail_deletes_except(monkeypatch: pytest.MonkeyPatch, platform: FakePlatform, keep: str | None) -> None:
    """Make every delete fail except for `keep` (usually the anchor)."""
    original = platform.delete

    async def delete(iid: str) -> None:
        if iid != keep:
            raise _err(ErrorCategory.TRANSIENT)
        await original(iid)

    monkeypatch.setattr(platform, "delete", delete)


@pytest_asyncio.fixture
async def anchor(platform: FakePlatform, clock: ManualClock) -> AsyncIterator[Anchor]:
    a = Anchor(platform, clock, "L1", ANCHOR)
    await clock.run(a.start())
    yield a
    await a.close()


@pytest.fixture
def reaper(platform: FakePlatform, clock: ManualClock, anchor: Anchor) -> Reaper:
    return Reaper(platform, clock, anchor, Deleter(platform, clock), SETTINGS)


def _units(platform: FakePlatform) -> list[str]:
    return sorted(s.iid for s in platform.instances() if s.labels.get(labels.ROLE) == labels.ROLE_UNIT)


async def _drain(clock: ManualClock, reaper: Reaper, trial: str) -> None:
    """Advance virtual time until the reaper has finished with the trial."""

    async def until_done() -> None:
        while reaper.is_tracking(trial):
            await clock.sleep(1.0)

    await clock.run(until_done())


# ───────────────────────────── Create ─────────────────────────────


@pytest.mark.asyncio
async def test_create_rejects_spec_without_trial_labels(reaper: Reaper) -> None:
    reaper.track("t1")
    with pytest.raises(ValueError):
        await reaper.create("t1", "web", _spec("t2", "web"))


@pytest.mark.asyncio
async def test_create_untracked_trial_raises(reaper: Reaper) -> None:
    with pytest.raises(ValueError):
        await reaper.create("t1", "web", _spec("t1", "web"))


@pytest.mark.asyncio
async def test_definite_rejection_is_not_reconciled(platform: FakePlatform, clock: ManualClock, reaper: Reaper) -> None:
    reaper.track("t1")
    platform.inject_fault("create", _err(ErrorCategory.IMAGE))
    lists = platform.calls["list"]
    with pytest.raises(FlotillaError) as exc:
        await clock.run(reaper.create("t1", "web", _spec("t1", "web")))
    assert exc.value.category is ErrorCategory.IMAGE and exc.value.stage == "create"
    assert platform.calls["list"] == lists


@pytest.mark.asyncio
async def test_lost_response_is_reconciled_to_instance(
    platform: FakePlatform, clock: ManualClock, reaper: Reaper
) -> None:
    # Accepted by the platform, response lost (transient): reconciliation finds the instance within list_visibility_s
    # and treats the create as successful.
    reaper.track("t1")
    platform.inject_create_then_fail(_err(ErrorCategory.TRANSIENT))
    lists = platform.calls["list"]
    handle = await clock.run(reaper.create("t1", "web", _spec("t1", "web")))
    assert _units(platform) == [handle.iid]
    assert platform.calls["list"] > lists  # recovered by reconciliation, not some other path


@pytest.mark.asyncio
async def test_unaccepted_transient_create_fails_after_visibility(
    platform: FakePlatform, clock: ManualClock, reaper: Reaper
) -> None:
    reaper.track("t1")
    platform.inject_fault("create", _err(ErrorCategory.TRANSIENT))
    start = clock.monotonic()
    with pytest.raises(FlotillaError) as exc:
        await clock.run(reaper.create("t1", "web", _spec("t1", "web")))
    assert exc.value.retryable and exc.value.stage == "create"
    assert clock.monotonic() - start >= VIS  # waited the full visibility limit before concluding


@pytest.mark.asyncio
async def test_reconcile_ignores_known_instances_of_same_unit(
    platform: FakePlatform, clock: ManualClock, reaper: Reaper
) -> None:
    # Recreating the same unit: the old instance is recorded; a new create with unknown outcome must not claim the old
    # instance as its result.
    reaper.track("t1")
    old = await clock.run(reaper.create("t1", "web", _spec("t1", "web")))
    await clock.advance(VIS)
    platform.inject_create_then_fail(_err(ErrorCategory.TRANSIENT))
    new = await clock.run(reaper.create("t1", "web", _spec("t1", "web")))
    assert new.iid != old.iid


@pytest.mark.asyncio
async def test_request_timeout_is_uncertain(platform: FakePlatform, clock: ManualClock, reaper: Reaper) -> None:
    reaper.track("t1")
    platform.set_latency("create", SETTINGS.create_request_timeout_s + 1)
    with pytest.raises(FlotillaError) as exc:
        await clock.run(reaper.create("t1", "web", _spec("t1", "web")))
    assert exc.value.category is ErrorCategory.TRANSIENT


@pytest.mark.asyncio
async def test_caller_cancel_does_not_cancel_create(platform: FakePlatform, clock: ManualClock, reaper: Reaper) -> None:
    # the create was accepted by the platform but the caller was cancelled before the response; the instance is
    # still recorded and deleted on release.
    reaper.track("t1")
    platform.set_latency("create", 10.0)
    caller = asyncio.create_task(reaper.create("t1", "web", _spec("t1", "web")))
    await clock.advance(5.0)
    caller.cancel()
    with pytest.raises(asyncio.CancelledError):
        await caller
    await clock.advance(10.0)  # request completes
    assert len(_units(platform)) == 1
    lists = platform.calls["list"]
    reaper.release("t1")
    await _drain(clock, reaper, "t1")
    assert _units(platform) == []
    assert platform.calls["list"] == lists  # outcome known, release does not list


@pytest.mark.asyncio
async def test_no_create_after_release(platform: FakePlatform, clock: ManualClock, reaper: Reaper) -> None:
    reaper.track("t1")
    reaper.release("t1")
    with pytest.raises(FlotillaError):
        await reaper.create("t1", "web", _spec("t1", "web"))
    await _drain(clock, reaper, "t1")
    assert platform.calls["create"] == 1  # only the anchor


@pytest.mark.asyncio
async def test_release_drops_queued_creates_immediately(platform: FakePlatform, clock: ManualClock) -> None:
    SharedTree(platform)
    # When create slots are taken by another trial, queued creates of a released trial give up immediately and its
    # deletes do not wait for the global backlog.
    a = Anchor(platform, clock, "L1", ANCHOR)
    await clock.run(a.start())
    reaper = Reaper(platform, clock, a, Deleter(platform, clock), ReaperSettings(create_concurrency=1))
    reaper.track("busy")
    reaper.track("t1")
    first = await clock.run(reaper.create("t1", "a", _spec("t1", "a")))
    platform.set_latency("create", 1000.0)
    hog = asyncio.create_task(reaper.create("busy", "x", _spec("busy", "x")))
    queued = asyncio.create_task(reaper.create("t1", "b", _spec("t1", "b")))
    await clock.advance(1.0)  # hog holds the only slot, queued waits
    reaper.release("t1")
    await clock.advance(1.0)
    assert queued.done()
    with pytest.raises(FlotillaError) as exc:
        await queued
    assert exc.value.retryable
    await clock.advance(30.0)
    assert not reaper.is_tracking("t1")  # did not wait for hog
    assert first.iid not in _units(platform)
    creates = platform.calls["create"]
    reaper.release("busy")
    with pytest.raises(FlotillaError):  # hog's own request times out (latency 1000s > 600s), unrelated to this test
        await clock.run(hog)
    assert platform.calls["create"] == creates  # queued was never sent
    await clock.run(reaper.close())


@pytest.mark.asyncio
async def test_queued_create_after_deactivation_reports_lost(platform: FakePlatform, clock: ManualClock) -> None:
    a = Anchor(platform, clock, "L1", ANCHOR)
    await clock.run(a.start())
    reaper = Reaper(platform, clock, a, Deleter(platform, clock), ReaperSettings(create_concurrency=1))
    reaper.track("t1")
    platform.set_latency("create", 10_000.0)
    hog = asyncio.create_task(reaper.create("t1", "x", _spec("t1", "x")))
    queued = asyncio.create_task(reaper.create("t1", "y", _spec("t1", "y")))
    for _ in range(1000):
        platform.inject_fault("renew", _err(ErrorCategory.TRANSIENT))
    await clock.advance(ANCHOR.grace_s)
    assert a.deactivated.is_set()
    for task in (hog, queued):
        with pytest.raises(FlotillaError) as exc:
            await task
        assert exc.value.category is ErrorCategory.LOST  # in-flight cancelled, queued never sent: both report lost
    await clock.run(reaper.close())


@pytest.mark.asyncio
async def test_no_create_request_after_grace(platform: FakePlatform, clock: ManualClock) -> None:
    # no create request reaches the platform after t0 + G. If the backend queues / backs off inside the call,
    # deactivation cancels it.
    for _ in range(1000):  # renewal never succeeds: t0 is when the anchor create was sent (0), deactivation at G
        platform.inject_fault("renew", _err(ErrorCategory.TRANSIENT))
    a = Anchor(platform, clock, "L1", ANCHOR)
    await clock.run(a.start())
    reaper = Reaper(platform, clock, a, Deleter(platform, clock), SETTINGS)
    reaper.track("t1")
    await clock.advance(ANCHOR.grace_s - 5.0 - clock.monotonic())  # 5s of grace left
    platform.set_latency("create", 20.0)  # the request reaches the platform only after 20s
    with pytest.raises(FlotillaError) as exc:
        await clock.run(reaper.create("t1", "web", _spec("t1", "web")))
    assert exc.value.category is ErrorCategory.LOST
    await clock.advance(60.0)
    assert _units(platform) == []  # the request did not arrive after G
    await clock.run(reaper.close())


@pytest.mark.asyncio
async def test_create_refused_after_launch_deactivated(
    platform: FakePlatform, clock: ManualClock, anchor: Anchor, reaper: Reaper
) -> None:
    reaper.track("t1")
    for _ in range(1000):
        platform.inject_fault("renew", _err(ErrorCategory.TRANSIENT))
    await clock.advance(ANCHOR.grace_s)
    creates = platform.calls["create"]
    with pytest.raises(FlotillaError) as exc:
        await reaper.create("t1", "web", _spec("t1", "web"))
    assert exc.value.category is ErrorCategory.LOST
    assert platform.calls["create"] == creates


@pytest.mark.asyncio
async def test_create_rate_is_limited(platform: FakePlatform, clock: ManualClock, reaper: Reaper) -> None:
    reaper.track("t1")
    start = clock.monotonic()
    await clock.run(asyncio.gather(*(reaper.create("t1", f"u{i}", _spec("t1", f"u{i}")) for i in range(11))))
    # 5 per second: 11 span at least 2 seconds.
    assert clock.monotonic() - start == pytest.approx(2.0)


# ───────────────────────────── Release ─────────────────────────────


@pytest.mark.asyncio
async def test_release_returns_immediately_and_deletes_all(
    platform: FakePlatform, clock: ManualClock, reaper: Reaper
) -> None:
    reaper.track("t1")
    for unit in ("web", "db", "cache"):
        await clock.run(reaper.create("t1", unit, _spec("t1", unit)))
    calls = sum(platform.calls.values())
    reaper.release("t1")
    assert sum(platform.calls.values()) == calls  # release itself does not call the platform
    lists = platform.calls["list"]
    await _drain(clock, reaper, "t1")
    assert _units(platform) == []
    assert platform.calls["list"] == lists  # releasing a trial whose outcomes are all known does not call list


@pytest.mark.asyncio
async def test_release_waits_for_inflight_create(platform: FakePlatform, clock: ManualClock, reaper: Reaper) -> None:
    reaper.track("t1")
    platform.set_latency("create", 10.0)
    caller = asyncio.create_task(reaper.create("t1", "web", _spec("t1", "web")))
    await clock.advance(1.0)
    reaper.release("t1")
    await _drain(clock, reaper, "t1")
    await caller
    assert _units(platform) == []


@pytest.mark.asyncio
async def test_sweep_deletes_instance_reconcile_gave_up_on(
    platform: FakePlatform, clock: ManualClock, reaper: Reaper
) -> None:
    # The platform created the instance and the response was lost, but list keeps failing during reconciliation: it
    # gives up at the deadline and the caller gets a retryable error. The instance still exists; the label sweep on
    # release must delete it (fully-deleted check).
    reaper.track("t1")
    platform.inject_create_then_fail(_err(ErrorCategory.TRANSIENT))
    for _ in range(1000):
        platform.inject_fault("list", _err(ErrorCategory.TRANSIENT))
    with pytest.raises(FlotillaError) as exc:
        await clock.run(reaper.create("t1", "web", _spec("t1", "web")))
    assert exc.value.retryable
    [orphan] = _units(platform)
    platform.clear_faults()
    reaper.release("t1")
    await _drain(clock, reaper, "t1")
    assert orphan not in _units(platform)


@pytest.mark.asyncio
async def test_create_effect_then_timeout_is_reconciled(
    platform: FakePlatform, clock: ManualClock, reaper: Reaper
) -> None:
    # Took effect on the platform, response not back within the request deadline: reconcile as unknown outcome and find
    # the instance.
    reaper.track("t1")
    platform.set_latency("create", SETTINGS.create_request_timeout_s + 1, phase="after")
    handle = await clock.run(reaper.create("t1", "web", _spec("t1", "web")))
    assert _units(platform) == [handle.iid]


@pytest.mark.asyncio
async def test_late_arriving_create_is_found(platform: FakePlatform, clock: ManualClock, reaper: Reaper) -> None:
    # The backend queues / backs off 20s before the request reaches the platform, then 5xx. The visibility limit counts
    # from request arrival; neither reconciliation nor the sweep may count from the start of the call.
    reaper.track("t1")
    platform.set_latency("create", 20.0)
    platform.inject_create_then_fail(_err(ErrorCategory.TRANSIENT))
    handle = await clock.run(reaper.create("t1", "web", _spec("t1", "web")))
    assert _units(platform) == [handle.iid]


@pytest.mark.asyncio
@pytest.mark.parametrize("category", [ErrorCategory.CAPACITY, ErrorCategory.IMAGE, ErrorCategory.INVALID])
async def test_failed_create_with_instance_is_swept(
    platform: FakePlatform, clock: ManualClock, reaper: Reaper, category: ErrorCategory
) -> None:
    # Accepted by the platform, failed later (scheduling timeout, pull failure): the backend reports non-transient, but
    # the instance exists. No reconciliation, but release must sweep.
    tree = SharedTree(platform)
    reaper.track("t1")
    reaper.add_dir("t1", "launches/L1/t1")
    platform.inject_create_then_fail(_err(category))
    with pytest.raises(FlotillaError) as exc:
        await clock.run(reaper.create("t1", "web", _spec("t1", "web")))
    assert exc.value.category is category
    [orphan] = _units(platform)
    reaper.release("t1")
    await _drain(clock, reaper, "t1")
    assert orphan not in _units(platform)
    assert tree.events == ["rm launches/L1/t1"]  # directory removed only after everything is deleted


@pytest.mark.asyncio
async def test_reconcile_and_sweep_scoped_to_launch(platform: FakePlatform, clock: ManualClock, reaper: Reaper) -> None:
    # Another launch happens to use the same trial name: this reaper neither claims its instance as a create result nor
    # deletes it.
    foreign = await platform.create(_spec("t1", "web", launch="L0"))
    await clock.advance(VIS)
    reaper.track("t1")
    platform.inject_fault("create", _err(ErrorCategory.TRANSIENT))  # not created
    with pytest.raises(FlotillaError):
        await clock.run(reaper.create("t1", "web", _spec("t1", "web")))
    reaper.release("t1")
    await _drain(clock, reaper, "t1")
    assert _units(platform) == [foreign.iid]


@pytest.mark.asyncio
async def test_sweep_deletes_unattributed_instances(platform: FakePlatform, clock: ManualClock, reaper: Reaper) -> None:
    # An instance under the trial label that the reaper never recorded (e.g. a create whose reconciliation gave up). Its
    # request was no later than the reaper's last create request, so list visibility guarantees it appears within
    # last_sent + list_visibility_s, and the label sweep on release must delete it.
    reaper.track("t1")
    stray = await platform.create(_spec("t1", "other"))
    platform.inject_create_then_fail(_err(ErrorCategory.TRANSIENT))
    await clock.run(reaper.create("t1", "web", _spec("t1", "web")))  # unknown outcome, found by reconciliation
    reaper.release("t1")
    await _drain(clock, reaper, "t1")
    assert stray.iid not in _units(platform)
    assert _units(platform) == []


@pytest.mark.asyncio
async def test_delete_retries_until_confirmed(platform: FakePlatform, clock: ManualClock, reaper: Reaper) -> None:
    reaper.track("t1")
    handle = await clock.run(reaper.create("t1", "web", _spec("t1", "web")))
    for _ in range(3):
        platform.inject_fault("delete", _err(ErrorCategory.TRANSIENT))
    reaper.release("t1")
    await _drain(clock, reaper, "t1")
    assert handle.iid not in _units(platform)
    assert platform.calls["delete"] >= 4


@pytest.mark.asyncio
async def test_delete_not_confirmed_while_still_visible(
    platform: FakePlatform, clock: ManualClock, reaper: Reaper, monkeypatch: pytest.MonkeyPatch
) -> None:
    # The platform accepted the delete but the instance remains (async termination): while get still returns a status it
    # is not deleted; delete and confirm again later.
    reaper.track("t1")
    handle = await clock.run(reaper.create("t1", "web", _spec("t1", "web")))
    original = platform.delete
    accepted_only = [True]

    async def lazy_delete(iid: str) -> None:
        if accepted_only[0]:
            accepted_only[0] = False
            return
        await original(iid)

    monkeypatch.setattr(platform, "delete", lazy_delete)
    gets = platform.calls["get"]
    reaper.release("t1")
    await _drain(clock, reaper, "t1")
    assert handle.iid not in _units(platform)
    assert platform.calls["get"] - gets == 2  # first confirmation sees it still there, second gets not_found


@pytest.mark.asyncio
async def test_dirs_removed_only_after_instances_gone(
    platform: FakePlatform, clock: ManualClock, reaper: Reaper, monkeypatch: pytest.MonkeyPatch
) -> None:
    tree = SharedTree(platform)
    events = tree.events
    reaper.track("t1")
    reaper.add_dir("t1", "launches/L1/t1")
    a = await clock.run(reaper.create("t1", "web", _spec("t1", "web")))
    b = await clock.run(reaper.create("t1", "db", _spec("t1", "db")))
    original = platform.delete

    async def logged_delete(iid: str) -> None:
        await original(iid)
        events.append(f"delete {iid}")

    monkeypatch.setattr(platform, "delete", logged_delete)
    reaper.release("t1")
    await _drain(clock, reaper, "t1")
    assert sorted(events[:2]) == sorted([f"delete {a.iid}", f"delete {b.iid}"])
    assert events[2:] == ["rm launches/L1/t1"]


@pytest.mark.asyncio
async def test_dirs_kept_when_instances_not_confirmed(
    platform: FakePlatform, clock: ManualClock, monkeypatch: pytest.MonkeyPatch
) -> None:
    a = Anchor(platform, clock, "L1", ANCHOR)
    await clock.run(a.start())
    reaper = Reaper(platform, clock, a, Deleter(platform, clock, retry_deadline_s=60.0), SETTINGS)
    tree = SharedTree(platform)
    reaper.track("t1")
    reaper.add_dir("t1", "launches/L1/t1")
    await clock.run(reaper.create("t1", "web", _spec("t1", "web")))
    _fail_deletes_except(monkeypatch, platform, keep=a.iid)
    reaper.release("t1")
    await _drain(clock, reaper, "t1")
    assert tree.events == []  # instances not fully deleted, directory untouched
    assert not await clock.run(reaper.close())  # leftovers, not a clean exit


@pytest.mark.asyncio
async def test_discard_deletes_before_release(platform: FakePlatform, clock: ManualClock, reaper: Reaper) -> None:
    reaper.track("t1")
    old = await clock.run(reaper.create("t1", "web", _spec("t1", "web")))
    reaper.discard("t1", old.iid)
    await clock.advance(5.0)
    assert old.iid not in _units(platform)
    new = await clock.run(reaper.create("t1", "web", _spec("t1", "web")))
    reaper.release("t1")
    await _drain(clock, reaper, "t1")
    assert new.iid not in _units(platform)


# ───────────────────────────── Process exit ─────────────────────────────


@pytest.mark.asyncio
async def test_close_clean_removes_launch_dir_then_anchor(
    platform: FakePlatform, clock: ManualClock, anchor: Anchor, reaper: Reaper
) -> None:
    tree = SharedTree(platform)
    reaper.track("t1")
    await clock.run(reaper.create("t1", "web", _spec("t1", "web")))
    clean = await clock.run(reaper.close())
    assert clean
    assert tree.events == ["rm launches/L1"]
    assert platform.instances() == []  # instances and anchor both deleted


@pytest.mark.asyncio
async def test_close_timeout_leaves_launch_dir_for_gc(
    platform: FakePlatform, clock: ManualClock, monkeypatch: pytest.MonkeyPatch
) -> None:
    a = Anchor(platform, clock, "L1", ANCHOR)
    await clock.run(a.start())
    reaper = Reaper(platform, clock, a, Deleter(platform, clock), ReaperSettings(close_timeout_s=20.0))
    tree = SharedTree(platform)
    reaper.track("t1")
    await clock.run(reaper.create("t1", "web", _spec("t1", "web")))
    _fail_deletes_except(monkeypatch, platform, keep=a.iid)
    start = clock.monotonic()
    clean = await clock.run(reaper.close())
    assert not clean
    assert clock.monotonic() - start <= 20.0 + 1e-9
    assert tree.events == []  # launch directory left for gc
    assert a.iid not in {s.iid for s in platform.instances()}  # anchor deleted
    assert len(_units(platform)) == 1  # instance left for gc / TTL


@pytest.mark.asyncio
async def test_close_after_deactivation_is_not_clean(
    platform: FakePlatform, clock: ManualClock, anchor: Anchor, reaper: Reaper
) -> None:
    tree = SharedTree(platform)
    for _ in range(1000):
        platform.inject_fault("renew", _err(ErrorCategory.TRANSIENT))
    await clock.advance(ANCHOR.grace_s)
    assert not await clock.run(reaper.close())
    assert tree.events == []


@pytest.mark.asyncio
async def test_status_terminal_instance_still_deleted(
    platform: FakePlatform, clock: ManualClock, reaper: Reaper
) -> None:
    reaper.track("t1")
    handle = await clock.run(reaper.create("t1", "web", _spec("t1", "web")))
    platform.set_status(handle.iid, InstanceStatus.TERMINAL, reason="Failed")
    reaper.release("t1")
    await _drain(clock, reaper, "t1")
    assert _units(platform) == []
