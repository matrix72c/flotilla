"""`flotilla.core.waiting` 的单元测试：时限、中止、限速都在虚拟时间上确定性地成立。"""

from __future__ import annotations

import asyncio

import pytest

from flotilla.core.waiting import Aborted, Backoff, Expired, RateLimiter, wait_all, within
from flotilla.platform.fake import ManualClock


@pytest.mark.asyncio
async def test_within_returns_result(clock: ManualClock) -> None:
    async def work() -> int:
        await clock.sleep(5.0)
        return 7

    assert await clock.run(within(clock, 10.0, work())) == 7


@pytest.mark.asyncio
async def test_within_expires_and_cancels(clock: ManualClock) -> None:
    cleaned = False

    async def work() -> None:
        nonlocal cleaned
        try:
            await clock.sleep(100.0)
        finally:
            cleaned = True

    with pytest.raises(Expired):
        await clock.run(within(clock, 10.0, work()))
    assert cleaned  # 返回前已执行完被等待对象的 finally
    assert clock.monotonic() == 10.0


@pytest.mark.asyncio
async def test_within_does_not_mask_inner_timeout_error(clock: ManualClock) -> None:
    async def work() -> None:
        raise TimeoutError("inner")

    with pytest.raises(TimeoutError) as exc:
        await clock.run(within(clock, 10.0, work()))
    assert not isinstance(exc.value, Expired)


@pytest.mark.asyncio
async def test_within_aborts_on_event(clock: ManualClock) -> None:
    stop = asyncio.Event()

    async def work() -> None:
        await clock.sleep(100.0)

    async def trigger() -> None:
        await clock.sleep(3.0)
        stop.set()

    trig = asyncio.create_task(trigger())
    with pytest.raises(Aborted):
        await clock.run(within(clock, None, work(), abort=(stop,)))
    assert clock.monotonic() == 3.0
    await trig


@pytest.mark.asyncio
async def test_within_cancelled_from_outside_cancels_inner(clock: ManualClock) -> None:
    cleaned = False

    async def work() -> None:
        nonlocal cleaned
        try:
            await clock.sleep(100.0)
        finally:
            cleaned = True

    outer = asyncio.create_task(within(clock, 50.0, work()))
    await clock.advance(1.0)
    outer.cancel()
    with pytest.raises(asyncio.CancelledError):
        await outer
    assert cleaned


@pytest.mark.asyncio
async def test_wait_all_times_out_without_cancelling(clock: ManualClock) -> None:
    slow = asyncio.create_task(clock.sleep(100.0))
    assert not await clock.run(wait_all(clock, [slow], 10.0))
    assert not slow.done()
    await clock.advance(100.0)
    assert slow.done()


def test_backoff_caps() -> None:
    b = Backoff(1.0, cap=5.0)
    assert [b.next() for _ in range(5)] == [1.0, 2.0, 4.0, 5.0, 5.0]


@pytest.mark.asyncio
async def test_rate_limiter_spacing_and_burst(clock: ManualClock) -> None:
    limiter = RateLimiter(clock, 2.0, burst=3)
    times: list[float] = []

    async def take() -> None:
        await limiter.acquire()
        times.append(clock.monotonic())

    await clock.run(asyncio.gather(*(take() for _ in range(6))))
    assert times == [0.0, 0.0, 0.0, 0.5, 1.0, 1.5]
