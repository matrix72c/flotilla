"""Unit tests for gc."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator

import pytest
import pytest_asyncio

from flotilla.core import labels
from flotilla.core.anchor import Anchor, AnchorSettings
from flotilla.core.deletion import Deleter
from flotilla.core.gc import Gc
from flotilla.platform.base import ErrorCategory, ExternalPolicy, FlotillaError, InstanceSpec, Resources
from flotilla.testing.platform import FakePlatform, ManualClock, SharedTree

ANCHOR = AnchorSettings(image="anchor@sha256:abc")
W = 180.0
VIS = 30.0


def _unit(launch: str, trial: str = "t1") -> InstanceSpec:
    return InstanceSpec(
        image="svc@sha256:abc",
        entrypoint=("sleep",),
        labels={labels.LAUNCH: launch, labels.TRIAL: trial, labels.UNIT: "web", labels.ROLE: labels.ROLE_UNIT},
        timeout_seconds=3600,
        resources=Resources(cpu="1", memory="2Gi"),
        volumes=(),
        external=ExternalPolicy(mode="none"),
    )


def _err() -> FlotillaError:
    return FlotillaError("503", stage="run", category=ErrorCategory.TRANSIENT, retryable=True)


@pytest_asyncio.fixture
async def anchor(platform: FakePlatform, clock: ManualClock, tree: SharedTree) -> AsyncIterator[Anchor]:
    """Anchor of this process (launch L1), with launches/L1 already created."""
    a = Anchor(platform, clock, "L1", ANCHOR)
    await clock.run(a.start())
    await a.mkdir("launches/L1", stage="prepare")
    yield a
    await a.close()


@pytest.fixture
def gc(platform: FakePlatform, clock: ManualClock, anchor: Anchor) -> Gc:
    return Gc(platform, clock, anchor, Deleter(platform, clock), window_s=W)


async def _orphan(platform: FakePlatform, tree: SharedTree, launch: str, n: int = 2) -> list[str]:
    """A launch whose process is dead: directory and instances exist, no anchor."""
    tree.dirs.update({"launches", f"launches/{launch}", f"launches/{launch}/t1"})
    return [(await platform.create(_unit(launch))).iid for _ in range(n)]


def _of(platform: FakePlatform, launch: str) -> list[str]:
    """Workload instances of that launch (excluding the anchor)."""
    return [
        s.iid
        for s in platform.instances()
        if s.labels.get(labels.LAUNCH) == launch and s.labels.get(labels.ROLE) == labels.ROLE_UNIT
    ]


@pytest.mark.asyncio
async def test_orphan_deleted_only_after_two_observations(
    platform: FakePlatform, clock: ManualClock, tree: SharedTree, gc: Gc
) -> None:
    await _orphan(platform, tree, "L0")
    await clock.advance(VIS)
    first = await clock.run(gc.run_once())
    assert first.candidates == {"L0"} and not first.removed  # first pass only records it
    await clock.advance(W - 1)
    second = await clock.run(gc.run_once())
    assert not second.removed and _of(platform, "L0")  # W not yet elapsed
    await clock.advance(1)
    third = await clock.run(gc.run_once())
    assert third.removed == {"L0"}
    assert _of(platform, "L0") == []
    assert "launches/L0" not in tree.dirs


@pytest.mark.asyncio
async def test_directory_removed_after_instances(
    platform: FakePlatform, clock: ManualClock, tree: SharedTree, gc: Gc
) -> None:
    await _orphan(platform, tree, "L0")
    await clock.run(gc.run_once())
    await clock.advance(W)
    deletes_before = platform.calls["delete"]
    tree.events.clear()
    await clock.run(gc.run_once())
    assert platform.calls["delete"] - deletes_before == 2
    assert tree.events == ["rm launches/L0"]  # directory removed only after all instances are gone (and only once)


@pytest.mark.asyncio
async def test_alive_launch_never_collected(
    platform: FakePlatform, clock: ManualClock, tree: SharedTree, gc: Gc
) -> None:
    # Another live process: anchor being renewed, directory and instances present. Repeated gc passes
    # never touch it (renewed instances are not collected).
    other = Anchor(platform, clock, "L2", ANCHOR)
    await clock.run(other.start())
    await other.mkdir("launches/L2", stage="prepare")
    mine = (await platform.create(_unit("L2"))).iid
    for _ in range(5):
        report = await clock.run(gc.run_once())
        assert "L2" in report.alive and "L2" not in report.candidates
        await clock.advance(W)
    assert mine in _of(platform, "L2")
    assert "launches/L2" in tree.dirs
    await other.close()


@pytest.mark.asyncio
async def test_own_launch_never_candidate(platform: FakePlatform, clock: ManualClock, tree: SharedTree, gc: Gc) -> None:
    # Even if this process's anchor is temporarily unlisted (list lag), its own launch is not a candidate.
    for _ in range(3):
        report = await clock.run(gc.run_once())
        assert "L1" not in report.candidates and "L1" not in report.removed
        await clock.advance(W)
    assert "launches/L1" in tree.dirs


@pytest.mark.asyncio
async def test_candidate_that_revives_is_forgotten(
    platform: FakePlatform, clock: ManualClock, tree: SharedTree, gc: Gc
) -> None:
    # Anchor temporarily missing from the list on the first observation, back on the second: nothing is
    # deleted and the candidate is dropped.
    await _orphan(platform, tree, "L0")
    await clock.run(gc.run_once())
    late = Anchor(platform, clock, "L0", ANCHOR)
    await clock.run(late.start())
    await clock.advance(W)
    report = await clock.run(gc.run_once())
    assert "L0" in report.alive and not report.removed
    assert len(_of(platform, "L0")) == 2
    await late.close()
    # Then the anchor really disappears: two fresh observations are required again.
    await clock.advance(VIS)
    assert (await clock.run(gc.run_once())).candidates == {"L0"}
    assert not (await clock.run(gc.run_once())).removed


@pytest.mark.asyncio
async def test_temporary_anchor_is_not_candidate(
    platform: FakePlatform, clock: ManualClock, tree: SharedTree, gc: Gc
) -> None:
    # Ephemeral anchors (publish / CLI gc) create no launch directory: never candidates, dead or alive.
    temp = Anchor(platform, clock, "T9", ANCHOR)
    await clock.run(temp.start())
    await temp.close()
    for _ in range(2):
        report = await clock.run(gc.run_once())
        assert "T9" not in report.candidates and "T9" not in report.removed
        await clock.advance(W)


@pytest.mark.asyncio
async def test_late_instance_of_dead_launch_is_caught(
    platform: FakePlatform, clock: ManualClock, tree: SharedTree, gc: Gc
) -> None:
    # The last create request before the process died reaches the platform late and only shows up in the
    # list within the visibility delay. W > G + list_visibility_s guarantees it is visible by the second
    # observation and deleted before the directory.
    tree.dirs.update({"launches", "launches/L0"})
    platform.set_latency("create", 5.0)
    late = asyncio.create_task(platform.create(_unit("L0")))
    await clock.run(gc.run_once())  # first observation: instance has not reached the platform yet
    assert _of(platform, "L0") == []
    await clock.advance(W)
    iid = (await late).iid
    report = await clock.run(gc.run_once())
    assert report.removed == {"L0"}
    assert iid not in _of(platform, "L0")


@pytest.mark.asyncio
async def test_undeletable_instance_keeps_directory(
    platform: FakePlatform, clock: ManualClock, tree: SharedTree, anchor: Anchor, monkeypatch: pytest.MonkeyPatch
) -> None:
    gc = Gc(platform, clock, anchor, Deleter(platform, clock, retry_deadline_s=30.0), window_s=W)
    [stuck, _] = await _orphan(platform, tree, "L0")
    original = platform.delete

    async def delete(iid: str) -> None:
        if iid == stuck:
            raise _err()
        await original(iid)

    monkeypatch.setattr(platform, "delete", delete)
    await clock.run(gc.run_once())
    await clock.advance(W)
    report = await clock.run(gc.run_once())
    assert report.pending == {"L0"} and not report.removed
    assert "launches/L0" in tree.dirs  # the directory is gc's only way to find it; keep it until all instances are gone
    monkeypatch.setattr(platform, "delete", original)
    report = await clock.run(gc.run_once())  # next pass finishes without waiting for W again
    assert report.removed == {"L0"}
    assert "launches/L0" not in tree.dirs


@pytest.mark.asyncio
async def test_listing_anchors_failure_aborts_round(
    platform: FakePlatform, clock: ManualClock, tree: SharedTree, gc: Gc
) -> None:
    await _orphan(platform, tree, "L0")
    await clock.run(gc.run_once())
    await clock.advance(W)
    platform.inject_fault("list", _err())
    with pytest.raises(FlotillaError):
        await clock.run(gc.run_once())
    assert len(_of(platform, "L0")) == 2  # failing to list the anchor != no anchor
    assert "launches/L0" in tree.dirs


@pytest.mark.asyncio
async def test_gc_refuses_when_own_launch_deactivated(
    platform: FakePlatform, clock: ManualClock, tree: SharedTree, gc: Gc
) -> None:
    await _orphan(platform, tree, "L0")
    for _ in range(1000):
        platform.inject_fault("renew", _err())
    await clock.advance(ANCHOR.grace_s)
    platform.clear_faults()
    with pytest.raises(FlotillaError) as exc:
        await clock.run(gc.run_once())
    assert exc.value.category is ErrorCategory.LOST
    assert len(_of(platform, "L0")) == 2


# ───────────────────────────── gc --launch ─────────────────────────────


@pytest.mark.asyncio
async def test_collect_launch_skips_waiting(
    platform: FakePlatform, clock: ManualClock, tree: SharedTree, gc: Gc
) -> None:
    await _orphan(platform, tree, "L0")
    assert await clock.run(gc.collect("L0"))
    assert _of(platform, "L0") == []
    assert "launches/L0" not in tree.dirs


@pytest.mark.asyncio
async def test_collect_launch_refuses_while_anchor_exists(
    platform: FakePlatform, clock: ManualClock, tree: SharedTree, gc: Gc
) -> None:
    await _orphan(platform, tree, "L0")
    alive = Anchor(platform, clock, "L0", ANCHOR)
    await clock.run(alive.start())
    with pytest.raises(FlotillaError) as exc:
        await clock.run(gc.collect("L0"))
    assert exc.value.category is ErrorCategory.INVALID
    assert len(_of(platform, "L0")) == 2
    await alive.close()


@pytest.mark.asyncio
async def test_collect_refuses_own_launch(clock: ManualClock, gc: Gc) -> None:
    with pytest.raises(ValueError):
        await clock.run(gc.collect("L1"))


@pytest.mark.asyncio
async def test_collect_launch_catches_instance_appearing_within_visibility(
    platform: FakePlatform, clock: ManualClock, tree: SharedTree, gc: Gc
) -> None:
    # Process just exited and its last create request just reached the platform, not yet listed:
    # --launch has no second observation, so it must wait out the visibility delay before concluding.
    tree.dirs.update({"launches", "launches/L0"})
    straggler = await platform.create(_unit("L0"))
    start = clock.monotonic()
    assert await clock.run(gc.collect("L0"))
    assert straggler.iid not in _of(platform, "L0")
    assert clock.monotonic() - start >= VIS
    assert tree.events[-1] == "rm launches/L0"


@pytest.mark.asyncio
async def test_launch_without_directory_is_found_by_labels(
    platform: FakePlatform, clock: ManualClock, tree: SharedTree, gc: Gc
) -> None:
    # The process died after creating sandboxes but before `launches/<id>` existed.
    tree.dirs.add("launches")
    await platform.create(_unit("L0"))
    await clock.advance(VIS)
    assert (await clock.run(gc.run_once())).candidates == {"L0"}
    await clock.advance(W)
    assert (await clock.run(gc.run_once())).removed == {"L0"}
    assert _of(platform, "L0") == []


@pytest.mark.asyncio
async def test_platform_only_mode_deletes_sandboxes_and_reports_directories(
    platform: FakePlatform, clock: ManualClock, tree: SharedTree
) -> None:
    await _orphan(platform, tree, "L0")
    gc = Gc(platform, clock, None, Deleter(platform, clock), window_s=W)
    await clock.advance(VIS)
    first = await clock.run(gc.run_once())
    assert first.mode == "platform" and first.candidates == {"L0"}
    await clock.advance(W)
    second = await clock.run(gc.run_once())
    assert second.directories_skipped == {"L0"} and not second.removed
    assert _of(platform, "L0") == []
    assert "launches/L0" in tree.dirs  # nothing on shared storage is touched without an anchor


@pytest.mark.asyncio
async def test_staging_collection_removes_only_old_timestamped_entries(
    clock: ManualClock, tree: SharedTree, gc: Gc
) -> None:
    now = int(clock.now().timestamp())
    old, fresh = f"a.{now - 90_000}.0123456789ab", f"b.{now - 60}.0123456789ab"
    tree.dirs.update(
        {"tasks", "tasks/.staging", f"tasks/.staging/{old}", f"tasks/.staging/{fresh}", "tasks/.staging/legacy"}
    )
    report = await clock.run(gc.collect_staging(24 * 3600))
    assert report.removed == (f"tasks/.staging/{old}",)
    assert report.unrecognized == ("tasks/.staging/legacy",)
    assert f"tasks/.staging/{fresh}" in tree.dirs and "tasks/.staging/legacy" in tree.dirs
