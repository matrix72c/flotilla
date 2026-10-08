"""`ManualClock` 的单元测试。

core 的时序测试全建在它上面，所以这里钉死三件事：虚拟时间只在 `advance` 时前进；被唤醒的协程在
**各自的**到期时刻 resume；`advance` 返回时被唤醒协程的效果已经落地（测试不必手动排空事件循环）。
"""

from __future__ import annotations

import asyncio

import pytest

from flotilla.platform.fake import ManualClock


@pytest.mark.asyncio
async def test_advance_moves_monotonic_and_now(clock: ManualClock) -> None:
    start = clock.now()
    assert clock.monotonic() == 0.0
    await clock.advance(90.0)
    assert clock.monotonic() == 90.0
    assert (clock.now() - start).total_seconds() == 90.0


@pytest.mark.asyncio
async def test_advance_rejects_backwards(clock: ManualClock) -> None:
    with pytest.raises(ValueError):
        await clock.advance(-1.0)


@pytest.mark.asyncio
async def test_sleep_wakes_exactly_at_deadline(clock: ManualClock) -> None:
    woke_at: list[float] = []

    async def sleeper() -> None:
        await clock.sleep(60.0)
        woke_at.append(clock.monotonic())

    task = asyncio.create_task(sleeper())
    await clock.advance(59.0)
    assert woke_at == []
    await clock.advance(1.0)
    assert woke_at == [60.0]  # advance 返回时已跑完，无需手动 asyncio.sleep(0)
    await task


@pytest.mark.asyncio
async def test_task_created_before_advance_sleeps_from_current_time(clock: ManualClock) -> None:
    # 任务刚 create_task、尚未运行到 sleep 就调 advance：sleep 应从推进前的时刻起算。
    woke_at: list[float] = []

    async def sleeper() -> None:
        await clock.sleep(10.0)
        woke_at.append(clock.monotonic())

    task = asyncio.create_task(sleeper())
    await clock.advance(10.0)
    assert woke_at == [10.0]
    await task


@pytest.mark.asyncio
async def test_periodic_loop_fires_at_each_tick_within_one_advance(clock: ManualClock) -> None:
    # 回收器 / 续期是周期循环：一次 advance(90) 内每 30s 的循环要触发 3 次，且各在正确的虚拟时刻。
    ticks: list[float] = []

    async def loop() -> None:
        while True:
            await clock.sleep(30.0)
            ticks.append(clock.monotonic())

    task = asyncio.create_task(loop())
    await clock.advance(90.0)
    assert ticks == [30.0, 60.0, 90.0]
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task


@pytest.mark.asyncio
async def test_woken_coroutine_effects_land_before_advance_returns(clock: ManualClock) -> None:
    # 被唤醒后还要再经过若干次让出（例如 await 一次 fake 平台调用）才产生效果，也要在 advance 返回前落地。
    done = asyncio.Event()

    async def worker() -> None:
        await clock.sleep(5.0)
        for _ in range(3):
            await asyncio.sleep(0)
        done.set()

    task = asyncio.create_task(worker())
    await clock.advance(5.0)
    assert done.is_set()
    await task


@pytest.mark.asyncio
async def test_same_deadline_wakes_in_registration_order(clock: ManualClock) -> None:
    order: list[str] = []

    async def sleeper(name: str) -> None:
        await clock.sleep(10.0)
        order.append(name)

    tasks = [asyncio.create_task(sleeper(n)) for n in ("a", "b", "c")]
    await clock.advance(10.0)
    assert order == ["a", "b", "c"]
    await asyncio.gather(*tasks)


@pytest.mark.asyncio
async def test_sleep_zero_yields_without_advancing(clock: ManualClock) -> None:
    ran = False

    async def other() -> None:
        nonlocal ran
        ran = True

    task = asyncio.create_task(other())
    await clock.sleep(0)
    assert ran  # 让出了一次，other 得以运行
    assert clock.monotonic() == 0.0
    await task


@pytest.mark.asyncio
async def test_time_never_goes_backwards_after_jump(clock: ManualClock) -> None:
    woke_at: list[float] = []

    async def sleeper() -> None:
        await clock.sleep(10.0)
        woke_at.append(clock.monotonic())

    task = asyncio.create_task(sleeper())
    await clock.advance(0)
    clock.jump(50.0)  # 越过 sleeper 的到期时刻，但没唤醒它
    await clock.advance(1.0)
    assert woke_at == [50.0]  # 醒来看到的是当前时刻，不是早已过去的 10
    assert clock.monotonic() == 51.0
    await task


@pytest.mark.asyncio
async def test_long_yield_chain_is_not_overtaken(clock: ManualClock) -> None:
    # 虚拟耗时为 0、但要让出很多次才完成的协程：时钟推进前必须等它跑完，不能越过它。
    done_at: list[float] = []

    async def busy() -> None:
        for _ in range(500):
            await asyncio.sleep(0)
        done_at.append(clock.monotonic())

    task = asyncio.create_task(busy())
    await clock.advance(1000.0)
    assert done_at == [0.0]
    await task


@pytest.mark.asyncio
async def test_cancelled_sleep_does_not_stall_run(clock: ManualClock) -> None:
    async def sleeper() -> None:
        await clock.sleep(1000.0)

    stale = asyncio.create_task(sleeper())
    await clock.advance(0)
    stale.cancel()
    with pytest.raises(asyncio.CancelledError):
        await stale

    async def short() -> str:
        await clock.sleep(5.0)
        return "ok"

    assert await clock.run(short()) == "ok"
    assert clock.monotonic() == 5.0  # 没有跳到已取消的 1000s 处


@pytest.mark.asyncio
async def test_run_reports_deadlock(clock: ManualClock) -> None:
    never = asyncio.Event()
    with pytest.raises(RuntimeError):
        await clock.run(never.wait())
