"""`Reaper` 的单元测试（Architecture §5.5、§14"回收"）。"""

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
from flotilla.platform.fake import FakePlatform, ManualClock, SharedTree

ANCHOR = AnchorSettings(image="anchor@sha256:abc")
SETTINGS = ReaperSettings()
VIS = 30.0  # conftest 能力报告的 list_visibility_s


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
    """让除 `keep`（通常是锚点）以外的实例删除一律失败。"""
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
    """推进虚拟时间直到回收器处理完该 trial。"""

    async def until_done() -> None:
        while reaper.is_tracking(trial):
            await clock.sleep(1.0)

    await clock.run(until_done())


# ───────────────────────────── 创建 ─────────────────────────────


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
    # 平台已接受、响应丢失（transient）：对账在 list_visibility_s 内找到实例，当作创建成功。
    reaper.track("t1")
    platform.inject_create_then_fail(_err(ErrorCategory.TRANSIENT))
    lists = platform.calls["list"]
    handle = await clock.run(reaper.create("t1", "web", _spec("t1", "web")))
    assert _units(platform) == [handle.iid]
    assert platform.calls["list"] > lists  # 是对账找回的，不是别的路径


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
    assert clock.monotonic() - start >= VIS  # 等满了可见时限才下结论


@pytest.mark.asyncio
async def test_reconcile_ignores_known_instances_of_same_unit(
    platform: FakePlatform, clock: ManualClock, reaper: Reaper
) -> None:
    # 同一单元重建：旧实例已记下，结果未知的新创建不能把旧实例当成自己的结果。
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
    # §14：创建请求已被平台接受、响应返回前调用方被取消——实例仍被记下，释放时删除。
    reaper.track("t1")
    platform.set_latency("create", 10.0)
    caller = asyncio.create_task(reaper.create("t1", "web", _spec("t1", "web")))
    await clock.advance(5.0)
    caller.cancel()
    with pytest.raises(asyncio.CancelledError):
        await caller
    await clock.advance(10.0)  # 请求完成
    assert len(_units(platform)) == 1
    lists = platform.calls["list"]
    reaper.release("t1")
    await _drain(clock, reaper, "t1")
    assert _units(platform) == []
    assert platform.calls["list"] == lists  # 结果明确，释放不列出


@pytest.mark.asyncio
async def test_no_create_after_release(platform: FakePlatform, clock: ManualClock, reaper: Reaper) -> None:
    reaper.track("t1")
    reaper.release("t1")
    with pytest.raises(FlotillaError):
        await reaper.create("t1", "web", _spec("t1", "web"))
    await _drain(clock, reaper, "t1")
    assert platform.calls["create"] == 1  # 只有锚点


@pytest.mark.asyncio
async def test_release_drops_queued_creates_immediately(platform: FakePlatform, clock: ManualClock) -> None:
    # 创建名额被别的 trial 占满时，已释放 trial 的排队创建立即放弃，其实例的删除不等全局积压。
    a = Anchor(platform, clock, "L1", ANCHOR)
    await clock.run(a.start())
    reaper = Reaper(platform, clock, a, Deleter(platform, clock), ReaperSettings(create_concurrency=1))
    reaper.track("busy")
    reaper.track("t1")
    first = await clock.run(reaper.create("t1", "a", _spec("t1", "a")))
    platform.set_latency("create", 1000.0)
    hog = asyncio.create_task(reaper.create("busy", "x", _spec("busy", "x")))
    queued = asyncio.create_task(reaper.create("t1", "b", _spec("t1", "b")))
    await clock.advance(1.0)  # hog 占着唯一名额，queued 排队
    reaper.release("t1")
    await clock.advance(1.0)
    assert queued.done()
    with pytest.raises(FlotillaError) as exc:
        await queued
    assert exc.value.retryable
    await clock.advance(30.0)
    assert not reaper.is_tracking("t1")  # 没等 hog
    assert first.iid not in _units(platform)
    creates = platform.calls["create"]
    reaper.release("busy")
    with pytest.raises(FlotillaError):  # hog 自己的请求超时（latency 1000s > 600s），与本测试无关
        await clock.run(hog)
    assert platform.calls["create"] == creates  # queued 从未发出
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
        assert exc.value.category is ErrorCategory.LOST  # 在途的被取消、排队的不发出：都报 lost
    await clock.run(reaper.close())


@pytest.mark.asyncio
async def test_no_create_request_after_grace(platform: FakePlatform, clock: ManualClock) -> None:
    # §5.6：t0 + G 之后不再有创建请求到达平台。后端在调用内排队 / 退避时，停用时刻取消它。
    for _ in range(1000):  # 续期从不成功：t0 是锚点创建请求的发出时刻 0，停用在 G
        platform.inject_fault("renew", _err(ErrorCategory.TRANSIENT))
    a = Anchor(platform, clock, "L1", ANCHOR)
    await clock.run(a.start())
    reaper = Reaper(platform, clock, a, Deleter(platform, clock), SETTINGS)
    reaper.track("t1")
    await clock.advance(ANCHOR.grace_s - 5.0 - clock.monotonic())  # 还剩 5s 宽限
    platform.set_latency("create", 20.0)  # 请求要 20s 后才到达平台
    with pytest.raises(FlotillaError) as exc:
        await clock.run(reaper.create("t1", "web", _spec("t1", "web")))
    assert exc.value.category is ErrorCategory.LOST
    await clock.advance(60.0)
    assert _units(platform) == []  # 请求没有在 G 之后到达
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
    # 5 个/秒：11 个至少跨 2 秒。
    assert clock.monotonic() - start == pytest.approx(2.0)


# ───────────────────────────── 释放 ─────────────────────────────


@pytest.mark.asyncio
async def test_release_returns_immediately_and_deletes_all(
    platform: FakePlatform, clock: ManualClock, reaper: Reaper
) -> None:
    reaper.track("t1")
    for unit in ("web", "db", "cache"):
        await clock.run(reaper.create("t1", unit, _spec("t1", unit)))
    calls = sum(platform.calls.values())
    reaper.release("t1")
    assert sum(platform.calls.values()) == calls  # release 本身不调用平台
    lists = platform.calls["list"]
    await _drain(clock, reaper, "t1")
    assert _units(platform) == []
    assert platform.calls["list"] == lists  # §14：结果都明确的 trial 释放不调用 list


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
    # 平台已建好实例、响应丢失，但对账期间 list 一直失败：对账到时限放弃、调用方得到可重试错误。
    # 实例仍在——释放时的按标签扫尾必须删掉它（§5.5 删尽的判定）。
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
    # 平台已生效、响应超过请求时限仍未返回：按结果未知对账，找到实例。
    reaper.track("t1")
    platform.set_latency("create", SETTINGS.create_request_timeout_s + 1, phase="after")
    handle = await clock.run(reaper.create("t1", "web", _spec("t1", "web")))
    assert _units(platform) == [handle.iid]


@pytest.mark.asyncio
async def test_late_arriving_create_is_found(platform: FakePlatform, clock: ManualClock, reaper: Reaper) -> None:
    # 后端排队 / 退避 20s 后请求才到达平台，随即 5xx。可见时限从请求到达起算——对账与扫尾都不能从调用开始起算。
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
    # 平台已接受、之后才失败（调度超时、拉取失败）：后端报非 transient，但实例存在。不对账，但释放时要扫尾。
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
    assert tree.events == ["rm launches/L1/t1"]  # 删尽之后才删目录


@pytest.mark.asyncio
async def test_reconcile_and_sweep_scoped_to_launch(platform: FakePlatform, clock: ManualClock, reaper: Reaper) -> None:
    # 另一个 launch 恰好用了同名 trial：本回收器既不把它的实例当成自己的创建结果，也不删除它。
    foreign = await platform.create(_spec("t1", "web", launch="L0"))
    await clock.advance(VIS)
    reaper.track("t1")
    platform.inject_fault("create", _err(ErrorCategory.TRANSIENT))  # 没建成
    with pytest.raises(FlotillaError):
        await clock.run(reaper.create("t1", "web", _spec("t1", "web")))
    reaper.release("t1")
    await _drain(clock, reaper, "t1")
    assert _units(platform) == [foreign.iid]


@pytest.mark.asyncio
async def test_sweep_deletes_unattributed_instances(platform: FakePlatform, clock: ManualClock, reaper: Reaper) -> None:
    # trial 标签下有一个回收器没记下的实例（例如对账放弃了的创建）。它的请求不晚于回收器最后一个创建请求，
    # C1 保证它在 last_sent + list_visibility_s 内出现，释放时的按标签扫尾必须把它删掉。
    reaper.track("t1")
    stray = await platform.create(_spec("t1", "other"))
    platform.inject_create_then_fail(_err(ErrorCategory.TRANSIENT))
    await clock.run(reaper.create("t1", "web", _spec("t1", "web")))  # 结果未知、对账找到
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
    # 平台接受了删除但实例还在（异步终止）：get 仍返回状态就不算删除，稍后再删、再确认。
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
    assert platform.calls["get"] - gets == 2  # 第一次确认看到实例仍在，第二次 not_found


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
    assert tree.events == []  # 实例没删干净，目录不动
    assert not await clock.run(reaper.close())  # 有遗留，不算干净退出


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


# ───────────────────────────── 进程退出 ─────────────────────────────


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
    assert platform.instances() == []  # 实例与锚点都删了


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
    assert tree.events == []  # launch 目录留给 gc
    assert a.iid not in {s.iid for s in platform.instances()}  # 锚点删了
    assert len(_units(platform)) == 1  # 实例留给 gc / TTL


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
