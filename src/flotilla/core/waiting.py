"""Clock-driven deadlines, cancellation, retries, and admission primitives."""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Collection, Iterable
from typing import Any

from flotilla.platform.base import (
    Clock,
    ErrorCategory,
    FlotillaError,
    InstanceState,
    InstanceStatus,
    Platform,
    Stage,
)


class Backoff:
    """Exponential backoff delays: `initial`, `initial * factor`, ..., capped at `cap`."""

    def __init__(self, initial: float = 1.0, *, factor: float = 2.0, cap: float = 30.0) -> None:
        self._next = initial
        self._factor = factor
        self._cap = cap

    def next(self) -> float:
        delay = min(self._next, self._cap)
        self._next = delay * self._factor
        return delay


def step_towards(now: float, deadline: float, delay: float) -> float:
    """Before the deadline, never step past it (so we check again right at the deadline); after it, use `delay`."""
    return min(delay, deadline - now) if now < deadline else delay


class Expired(TimeoutError):
    """The `within` deadline passed. Distinct from a `TimeoutError` raised by the awaited object: callers catch only
    this."""


class Aborted(Exception):
    """One of the `within` `abort` events was set before completion."""


async def within[T](
    clock: Clock,
    timeout: float | None,
    aw: Awaitable[T],
    *,
    abort: Iterable[asyncio.Event] = (),
) -> T:
    """Complete `aw` within `timeout` seconds on `clock` (`None` means no limit); on timeout cancel it and raise
    `Expired`; if any event in `abort` is set first, cancel it and raise `Aborted`.

    On timeout, abort, or outer cancellation, wait for `aw` to finish handling cancellation (run its finally)
    before returning. If `aw` finishes at the same time, its outcome wins.
    """
    task = asyncio.ensure_future(aw)
    timer = asyncio.ensure_future(clock.sleep(timeout)) if timeout is not None else None
    stoppers = [asyncio.ensure_future(event.wait()) for event in abort]
    guards: set[asyncio.Future[Any]] = {*stoppers} | ({timer} if timer is not None else set())
    try:
        await asyncio.wait({task, *guards}, return_when=asyncio.FIRST_COMPLETED)
    except BaseException:
        task.cancel()
        await settle(task)
        raise
    finally:
        for guard in guards:
            guard.cancel()
    if task.done():
        return task.result()
    task.cancel()
    await settle(task)
    if timer is not None and timer.done() and not timer.cancelled():
        raise Expired(f"not completed within {timeout}s")
    raise Aborted()


async def settle(task: asyncio.Future[Any]) -> None:
    """Wait for `task` to finish without raising its result; nobody cares about its exception, so retrieve it to
    avoid asyncio reporting "never retrieved"."""
    await asyncio.wait({task})
    if not task.cancelled():
        task.exception()


async def wait_all(clock: Clock, tasks: Collection[asyncio.Future[Any]], timeout: float) -> bool:
    """Wait up to `timeout` seconds for all `tasks` without cancelling them. Returns whether all finished."""
    pending = {t for t in tasks if not t.done()}
    if not pending:
        return True
    timer = asyncio.ensure_future(clock.sleep(timeout))
    try:
        while pending and not timer.done():
            done, _ = await asyncio.wait(pending | {timer}, return_when=asyncio.FIRST_COMPLETED)
            pending -= done
    finally:
        timer.cancel()
    return not pending


async def wait_running(platform: Platform, clock: Clock, iid: str, *, stage: Stage) -> InstanceState:
    """Poll `get` with backoff until the instance is `RUNNING`.

    Fails if the instance terminates, disappears, or the query is definitively rejected; keeps waiting on
    retryable query errors and `PENDING` / `UNKNOWN`. No deadline here; callers wrap it in `within` with the
    stage timeout.
    """
    backoff = Backoff(0.5, cap=5.0)
    while True:
        try:
            state = await platform.get(iid)
        except FlotillaError as exc:
            if exc.category is ErrorCategory.NOT_FOUND:
                raise FlotillaError(
                    f"instance {iid} disappeared before becoming ready",
                    stage=stage,
                    category=ErrorCategory.TRANSIENT,
                    retryable=True,
                ) from exc
            if not exc.retryable:
                raise FlotillaError(str(exc), stage=stage, category=exc.category, retryable=False) from exc
        else:
            if state.status is InstanceStatus.RUNNING:
                return state
            if state.status is InstanceStatus.TERMINAL:
                # core does not interpret native reasons (normalized status): report retryable, reason in message.
                raise FlotillaError(
                    f"instance {iid} terminated before becoming ready: {describe(state)}",
                    stage=stage,
                    category=ErrorCategory.TRANSIENT,
                    retryable=True,
                )
        await clock.sleep(backoff.next())


def describe(state: InstanceState) -> str:
    """One-line diagnostic of an instance state: normalized status plus native platform reason and message."""
    parts = [state.status.value, state.reason, state.message]
    return " / ".join(p for p in parts if p)


class RateLimiter:
    """First-come-first-served rate limiter admitting `rate` per second with bursts of `burst` (`create_rate`).

    Each `acquire` reserves a time slot and waits until it; cancelled reservations are not returned (later
    callers are only slightly delayed, the rate is never exceeded).
    """

    def __init__(self, clock: Clock, rate: float, *, burst: int = 1) -> None:
        if rate <= 0 or burst < 1:
            raise ValueError("rate must be > 0 and burst must be >= 1")
        self._clock = clock
        self._interval = 1.0 / rate
        self._slack = (burst - 1) * self._interval
        self._next = float("-inf")

    async def acquire(self) -> None:
        now = self._clock.monotonic()
        slot = max(self._next, now - self._slack)
        self._next = slot + self._interval
        if slot > now:
            await self._clock.sleep(slot - now)


async def run_all[T](aws: Iterable[Awaitable[T]]) -> list[T]:
    """Run concurrently; on success return results in input order. On any failure cancel the rest, wait for them,
    then re-raise the **earliest** failure as-is.

    Among failures completing in the same batch, the first in input order wins (deterministic). Not wrapped in an
    ExceptionGroup: trial errors are reported to users by stage / category / retryable. Outer cancellation also
    cancels all and waits for them.
    """
    tasks = [asyncio.ensure_future(aw) for aw in aws]
    pending = set(tasks)
    try:
        while pending:
            done, pending = await asyncio.wait(pending, return_when=asyncio.FIRST_COMPLETED)
            for t in tasks:
                if t in done and not t.cancelled() and (exc := t.exception()) is not None:
                    raise exc
        return [t.result() for t in tasks]
    finally:
        for t in tasks:
            t.cancel()
        if tasks:
            await asyncio.wait(tasks)
        for t in tasks:
            if not t.cancelled():
                t.exception()  # nobody cares about other failures; retrieve to avoid "never retrieved"
