"""Unit tests for `ManualClock`.

All core timing tests build on it, so three things are pinned down here: virtual time only moves on `advance`; woken
coroutines resume at **their own** deadlines; and when `advance` returns, the woken coroutines' effects have landed
(tests need not drain the event loop by hand).
"""

from __future__ import annotations

import asyncio

import pytest

from flotilla.testing.platform import ManualClock


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
    assert woke_at == [60.0]  # done by the time advance returns, no manual asyncio.sleep(0) needed
    await task


@pytest.mark.asyncio
async def test_task_created_before_advance_sleeps_from_current_time(clock: ManualClock) -> None:
    # advance is called right after create_task, before the task reaches sleep: sleep should count from the pre-advance
    # time.
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
    # The reaper / renewal are periodic loops: within one advance(90), a 30s loop must fire 3 times, each at the right
    # virtual time.
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
    # After waking it still yields several times (e.g. awaiting a fake platform call) before taking effect; that must
    # also land before advance returns.
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
    assert ran  # yielded once, so other got to run
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
    clock.jump(50.0)  # past the sleeper's deadline, but without waking it
    await clock.advance(1.0)
    assert woke_at == [50.0]  # on waking it sees the current time, not the long-past 10
    assert clock.monotonic() == 51.0
    await task


@pytest.mark.asyncio
async def test_long_yield_chain_is_not_overtaken(clock: ManualClock) -> None:
    # A coroutine with zero virtual duration that needs many yields to finish: the clock must wait for it before
    # advancing, not skip past it.
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
    assert clock.monotonic() == 5.0  # did not jump to the cancelled 1000s


@pytest.mark.asyncio
async def test_run_reports_deadlock(clock: ManualClock) -> None:
    never = asyncio.Event()
    with pytest.raises(RuntimeError):
        await clock.run(never.wait())
