"""gc 的单元测试（Architecture §5.6、§14"gc"）。"""

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
from flotilla.platform.fake import FakePlatform, ManualClock, SharedTree

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
    """本进程（launch L1）的锚点，已建好 launches/L1。"""
    a = Anchor(platform, clock, "L1", ANCHOR)
    await clock.run(a.start())
    await a.mkdir("launches/L1", stage="prepare")
    yield a
    await a.close()


@pytest.fixture
def gc(platform: FakePlatform, clock: ManualClock, anchor: Anchor) -> Gc:
    return Gc(platform, clock, anchor, Deleter(platform, clock), window_s=W)


async def _orphan(platform: FakePlatform, tree: SharedTree, launch: str, n: int = 2) -> list[str]:
    """一个进程已死的 launch：有目录与实例，没有锚点。"""
    tree.dirs.update({"launches", f"launches/{launch}", f"launches/{launch}/t1"})
    return [(await platform.create(_unit(launch))).iid for _ in range(n)]


def _of(platform: FakePlatform, launch: str) -> list[str]:
    """该 launch 的业务实例（不含锚点）。"""
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
    assert first.candidates == {"L0"} and not first.removed  # 第一次只登记
    await clock.advance(W - 1)
    second = await clock.run(gc.run_once())
    assert not second.removed and _of(platform, "L0")  # 不满 W
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
    assert tree.events == ["rm launches/L0"]  # 实例删尽之后才删目录（且只删一次）


@pytest.mark.asyncio
async def test_alive_launch_never_collected(
    platform: FakePlatform, clock: ManualClock, tree: SharedTree, gc: Gc
) -> None:
    # 另一个活着的进程：锚点在续期、目录与实例都在。多轮 gc 都不碰它（续期后的实例不被 gc）。
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
    # 即使本进程锚点暂时没被列出（列表滞后），自己的 launch 也不是候选。
    for _ in range(3):
        report = await clock.run(gc.run_once())
        assert "L1" not in report.candidates and "L1" not in report.removed
        await clock.advance(W)
    assert "launches/L1" in tree.dirs


@pytest.mark.asyncio
async def test_candidate_that_revives_is_forgotten(
    platform: FakePlatform, clock: ManualClock, tree: SharedTree, gc: Gc
) -> None:
    # 第一次观察时锚点暂时不在列表中，第二次又出现：不删除，候选作废。
    await _orphan(platform, tree, "L0")
    await clock.run(gc.run_once())
    late = Anchor(platform, clock, "L0", ANCHOR)
    await clock.run(late.start())
    await clock.advance(W)
    report = await clock.run(gc.run_once())
    assert "L0" in report.alive and not report.removed
    assert len(_of(platform, "L0")) == 2
    await late.close()
    # 之后锚点真的消失：须重新经过两次观察。
    await clock.advance(VIS)
    assert (await clock.run(gc.run_once())).candidates == {"L0"}
    assert not (await clock.run(gc.run_once())).removed


@pytest.mark.asyncio
async def test_temporary_anchor_is_not_candidate(
    platform: FakePlatform, clock: ManualClock, tree: SharedTree, gc: Gc
) -> None:
    # 临时锚点（publish / 命令行 gc）不建 launch 目录：无论死活都不是候选。
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
    # 进程死前最后一个创建请求很晚才到达平台、可见时限内才出现在列表中。W > G + list_visibility_s
    # 保证第二次观察时它已可见，删目录之前被删掉。
    tree.dirs.update({"launches", "launches/L0"})
    platform.set_latency("create", 5.0)
    late = asyncio.create_task(platform.create(_unit("L0")))
    await clock.run(gc.run_once())  # 第一次观察：实例还没到平台
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
    assert "launches/L0" in tree.dirs  # 目录是 gc 找到它的唯一入口，实例没删尽就不能删
    monkeypatch.setattr(platform, "delete", original)
    report = await clock.run(gc.run_once())  # 下一轮完成，不需要再等 W
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
    assert len(_of(platform, "L0")) == 2  # 列不出锚点 ≠ 没有锚点
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
    # 进程刚退出，最后一个创建请求刚到平台、还不在列表里：--launch 没有两次观察，须等满可见时限再下结论。
    tree.dirs.update({"launches", "launches/L0"})
    straggler = await platform.create(_unit("L0"))
    start = clock.monotonic()
    assert await clock.run(gc.collect("L0"))
    assert straggler.iid not in _of(platform, "L0")
    assert clock.monotonic() - start >= VIS
    assert tree.events[-1] == "rm launches/L0"
