"""Confirm remote deletion and reconcile uncertain create outcomes."""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Iterable, Mapping

from flotilla.core.waiting import Backoff, step_towards
from flotilla.platform.base import Clock, ErrorCategory, FlotillaError, Platform

log = logging.getLogger(__name__)


class Deleter:
    """Confirm deletion with get; retry transient failures within a bounded deadline."""

    def __init__(
        self,
        platform: Platform,
        clock: Clock,
        *,
        concurrency: int = 16,
        retry_deadline_s: float = 1800.0,
    ) -> None:
        if concurrency < 1 or retry_deadline_s <= 0:
            raise ValueError("concurrency must be >= 1 and retry_deadline_s must be > 0")
        self.platform = platform
        self.clock = clock
        self.retry_deadline_s = retry_deadline_s
        self._slots = asyncio.Semaphore(concurrency)
        self.errors: dict[str, str] = {}  # iid -> last deletion error, for cleanup diagnostics

    def scope(self) -> DeleteScope:
        return DeleteScope(self)

    async def delete(self, iid: str) -> bool:
        """Delete and confirm. Returns whether deletion was confirmed."""
        give_up = self.clock.monotonic() + self.retry_deadline_s
        backoff = Backoff(1.0, cap=60.0)
        while True:
            try:
                async with self._slots:
                    await self.platform.delete(iid)
                    await self.platform.get(iid)
            except FlotillaError as exc:
                if exc.category is ErrorCategory.NOT_FOUND:
                    self.errors.pop(iid, None)
                    return True
                self.errors[iid] = _brief(exc)
                if not exc.retryable:
                    log.error("Deletion of %s was rejected; cleanup remains unconfirmed: %s", iid, exc)
                    return False
                log.warning("Deleting instance %s failed, retrying: %s", iid, exc)
            else:
                self.errors[iid] = "instance still listed after delete"
            # get still returned a state: platform not done yet (e.g. terminating); delete and confirm again later.
            if self.clock.monotonic() >= give_up:
                log.error("Could not confirm deletion of instance %s within %ss", iid, self.retry_deadline_s)
                return False
            await self.clock.sleep(backoff.next())


class DeleteScope:
    """Deletion of a group of related instances."""

    def __init__(self, deleter: Deleter) -> None:
        self._deleter = deleter
        self._tasks: dict[str, asyncio.Task[bool]] = {}

    def start(self, iid: str) -> asyncio.Task[bool]:
        """Delete `iid` in the background; returns the existing task if already deleting."""
        task = self._tasks.get(iid)
        if task is None:
            task = asyncio.create_task(self._deleter.delete(iid))
            task.add_done_callback(_retrieve)
            self._tasks[iid] = task
        return task

    async def delete_all(self, iids: Iterable[str]) -> bool:
        """Delete `iids` and wait for them. Returns whether all deletions were confirmed."""
        tasks = [self.start(iid) for iid in iids]
        if not tasks:
            return True
        await asyncio.wait(tasks)
        return all(t.result() for t in tasks)

    def gone(self) -> set[str]:
        """Instances whose deletion is confirmed."""
        return {
            iid
            for iid, t in self._tasks.items()
            if t.done() and not t.cancelled() and t.exception() is None and t.result()
        }

    def last_error(self) -> str | None:
        """Most recent deletion error among this scope's unconfirmed instances."""
        for iid in sorted(self.pending()):
            if iid in self._deleter.errors:
                return self._deleter.errors[iid]
        return None

    def pending(self) -> set[str]:
        """Known instances whose deletion has not been confirmed, including sweep discoveries."""
        return set(self._tasks) - self.gone()

    async def sweep(self, query: Mapping[str, str], *, until: float) -> bool:
        """List by `query` and delete until a listing issued after `until` is empty. Returns whether all are gone.

        Instances confirmed deleted (`get` is `not_found`) may linger in listings and are treated as absent.
        Listing failures are retried with backoff, sharing `retry_deadline_s` with deletion.
        """
        clock = self._deleter.clock
        give_up = clock.monotonic() + self._deleter.retry_deadline_s
        backoff = Backoff(1.0, cap=8.0)
        while True:
            issued = clock.monotonic()
            try:
                listed = await self._deleter.platform.list(query)
            except FlotillaError as exc:
                if not exc.retryable:
                    log.error("Cleanup listing was rejected; cleanup remains unconfirmed: %s", exc)
                    return False
                log.warning("Listing instances for %s failed, retrying: %s", dict(query), exc)
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


def _brief(exc: BaseException, limit: int = 300) -> str:
    text = f"{type(exc).__name__}: {exc}"
    return text if len(text) <= limit else text[: limit - 3] + "..."


def _retrieve(task: asyncio.Task[bool]) -> None:
    """Waiters consume deletion results; when nobody waits (e.g. discarded old instances), retrieve the exception
    so asyncio does not report "never retrieved"."""
    if not task.cancelled():
        task.exception()
