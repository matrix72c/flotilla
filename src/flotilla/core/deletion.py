"""删除实例并确认（Architecture §5.5、§5.6）。回收器与 gc 共用。

`Deleter` 是进程级的：持有删除的并发上限与重试时限。`DeleteScope` 是一组相关实例（一个 trial、
一个残留 launch）的删除：同一实例只删一次；可以等全部删尽；可以按标签反复列出、删除新出现的实例，
直到某个时刻之后列出为空（§5.5"删尽的判定"）。
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Iterable, Mapping

from flotilla.core.waiting import Backoff, step_towards
from flotilla.platform.base import Clock, ErrorCategory, FlotillaError, Platform

log = logging.getLogger(__name__)


class Deleter:
    """删除实例并以 `get` 确认（`not_found`）；失败按指数退避重试，最长 `retry_deadline_s`（§5.5）。"""

    def __init__(
        self,
        platform: Platform,
        clock: Clock,
        *,
        concurrency: int = 16,
        retry_deadline_s: float = 1800.0,
    ) -> None:
        if concurrency < 1 or retry_deadline_s <= 0:
            raise ValueError("concurrency 须 >= 1，retry_deadline_s 须 > 0")
        self.platform = platform
        self.clock = clock
        self.retry_deadline_s = retry_deadline_s
        self._slots = asyncio.Semaphore(concurrency)

    def scope(self) -> DeleteScope:
        return DeleteScope(self)

    async def delete(self, iid: str) -> bool:
        """删除并确认。返回是否已确认删除。"""
        give_up = self.clock.monotonic() + self.retry_deadline_s
        backoff = Backoff(1.0, cap=60.0)
        while True:
            try:
                async with self._slots:
                    await self.platform.delete(iid)
                    await self.platform.get(iid)
            except FlotillaError as exc:
                if exc.category is ErrorCategory.NOT_FOUND:
                    return True
                log.warning("删除实例 %s 失败，重试：%s", iid, exc)
            # get 仍返回了状态：平台尚未删完（例如正在终止），稍后再删一次再确认。
            if self.clock.monotonic() >= give_up:
                log.error("实例 %s 在 %ss 内未能确认删除", iid, self.retry_deadline_s)
                return False
            await self.clock.sleep(backoff.next())


class DeleteScope:
    """一组相关实例的删除。"""

    def __init__(self, deleter: Deleter) -> None:
        self._deleter = deleter
        self._tasks: dict[str, asyncio.Task[bool]] = {}

    def start(self, iid: str) -> asyncio.Task[bool]:
        """在后台删除 `iid`；已在删除的返回同一个任务。"""
        task = self._tasks.get(iid)
        if task is None:
            task = asyncio.create_task(self._deleter.delete(iid))
            task.add_done_callback(_retrieve)
            self._tasks[iid] = task
        return task

    async def delete_all(self, iids: Iterable[str]) -> bool:
        """删除 `iids` 并等它们结束。返回是否全部确认删除。"""
        tasks = [self.start(iid) for iid in iids]
        if not tasks:
            return True
        await asyncio.wait(tasks)
        return all(t.result() for t in tasks)

    def gone(self) -> set[str]:
        """已确认删除的实例。"""
        return {iid for iid, t in self._tasks.items() if t.done() and not t.cancelled() and t.result()}

    async def sweep(self, query: Mapping[str, str], *, until: float) -> bool:
        """按 `query` 列出并删除，直到一次在 `until` 之后发出的列出为空。返回是否删尽。

        已确认删除（`get` 为 `not_found`）的实例可能还滞留在列表里，按不存在处理。列出失败按退避重试，
        与删除共用 `retry_deadline_s`。
        """
        clock = self._deleter.clock
        give_up = clock.monotonic() + self._deleter.retry_deadline_s
        backoff = Backoff(1.0, cap=8.0)
        while True:
            issued = clock.monotonic()
            try:
                listed = await self._deleter.platform.list(query)
            except FlotillaError as exc:
                log.warning("列出 %s 的实例失败，重试：%s", dict(query), exc)
            else:
                remaining = {st.iid for st in listed} - self.gone()
                if not remaining and issued >= until:
                    return True
                if not await self.delete_all(remaining):
                    return False
            if clock.monotonic() >= give_up:
                return False
            await clock.sleep(step_towards(clock.monotonic(), until, backoff.next()))

    def cancel(self) -> None:
        for task in self._tasks.values():
            task.cancel()


def _retrieve(task: asyncio.Task[bool]) -> None:
    """删除任务的结果由等待者取用；无人等待时（例如被丢弃的旧实例）取走异常，免得 asyncio 报 "never retrieved"。"""
    if not task.cancelled():
        task.exception()
