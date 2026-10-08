"""基于注入 `Clock` 的等待原语：超时、退避、等一组任务、等实例可执行命令（Architecture §2.3"时钟"）。

core 不用 `asyncio.timeout` / `asyncio.wait_for`——它们挂在事件循环的挂钟上（`pyproject.toml` 的 banned-api）；
超时一律经 `within`，退避经 `Backoff`，限速经 `RateLimiter`，从而能在 `ManualClock` 上确定性地测试。
"""

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
    """指数退避的间隔序列：`initial`、`initial × factor`、…，封顶 `cap`。"""

    def __init__(self, initial: float = 1.0, *, factor: float = 2.0, cap: float = 30.0) -> None:
        self._next = initial
        self._factor = factor
        self._cap = cap

    def next(self) -> float:
        delay = min(self._next, self._cap)
        self._next = delay * self._factor
        return delay


def step_towards(now: float, deadline: float, delay: float) -> float:
    """时限之前不越过时限（保证时限一到就再查一次），之后按 `delay`。"""
    return min(delay, deadline - now) if now < deadline else delay


class Expired(TimeoutError):
    """`within` 的时限到了。与被等待对象自己抛出的 `TimeoutError` 区分开：调用方只捕获它。"""


class Aborted(Exception):
    """`within` 的某个 `abort` 事件先于完成置位。"""


async def within[T](
    clock: Clock,
    timeout: float | None,
    aw: Awaitable[T],
    *,
    abort: Iterable[asyncio.Event] = (),
) -> T:
    """在 `clock` 上的 `timeout` 秒内（`None` 为不限时）完成 `aw`，超时取消它并抛 `Expired`；
    `abort` 中任一事件先置位则取消它并抛 `Aborted`。

    无论超时、中止还是外层被取消，都等 `aw` 处理完取消（执行完它的 finally）再返回。`aw` 与它们同时结束时以 `aw` 为准。
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
        raise Expired(f"超过 {timeout}s 未完成")
    raise Aborted()


async def settle(task: asyncio.Future[Any]) -> None:
    """等 `task` 结束而不抛出它的结果；它的异常已无人关心，取走以免 asyncio 报 "never retrieved"。"""
    await asyncio.wait({task})
    if not task.cancelled():
        task.exception()


async def wait_all(clock: Clock, tasks: Collection[asyncio.Future[Any]], timeout: float) -> bool:
    """等 `tasks` 全部结束，最多 `timeout` 秒；不取消它们。返回是否全部结束。"""
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
    """按退避轮询 `get`，直到实例 `RUNNING`（§5.1）。

    实例终止或消失即失败；查询出错与 `PENDING` / `UNKNOWN` 都按未就绪继续等（§2.3"状态归一"）。
    本函数不设时限，调用方用 `within` 套上本阶段的超时。
    """
    backoff = Backoff(0.5, cap=5.0)
    while True:
        try:
            state = await platform.get(iid)
        except FlotillaError as exc:
            if exc.category is ErrorCategory.NOT_FOUND:
                raise FlotillaError(
                    f"实例 {iid} 在就绪前消失",
                    stage=stage,
                    category=ErrorCategory.TRANSIENT,
                    retryable=True,
                ) from exc
        else:
            if state.status is InstanceStatus.RUNNING:
                return state
            if state.status is InstanceStatus.TERMINAL:
                # core 不解释平台的原生原因（§2.3"状态归一"）：按可重试报告，原因写进消息。
                raise FlotillaError(
                    f"实例 {iid} 在就绪前终止：{describe(state)}",
                    stage=stage,
                    category=ErrorCategory.TRANSIENT,
                    retryable=True,
                )
        await clock.sleep(backoff.next())


def describe(state: InstanceState) -> str:
    """实例状态的一行诊断：归一状态加平台原生原因与消息（C12）。"""
    parts = [state.status.value, state.reason, state.message]
    return " / ".join(p for p in parts if p)


class RateLimiter:
    """按 `rate` 个/秒放行、允许 `burst` 个突发的限速器，先到先得（§16.3 `create_rate`）。

    每次 `acquire` 预约一个时间槽再等到该时刻；被取消的预约不归还（只会让后来者略晚，不会超速）。
    """

    def __init__(self, clock: Clock, rate: float, *, burst: int = 1) -> None:
        if rate <= 0 or burst < 1:
            raise ValueError("rate 须 > 0，burst 须 >= 1")
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
    """并发执行，全部成功时按输入顺序返回结果；任一失败即取消其余、等它们结束，再原样抛出**最先发生**的失败。

    同一批完成的多个失败按输入顺序取第一个（确定性）。不包成 ExceptionGroup：trial 的错误按
    stage / category / retryable 向使用方报告（§5.4）。外层被取消时同样取消全部并等它们结束。
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
                t.exception()  # 其余失败已无人关心；取走以免 "never retrieved"
