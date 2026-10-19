"""Cross-thread cancellation must wait for the worker's resource cleanup."""

import asyncio
import threading
from collections.abc import Iterator

import pytest

from flotilla.runtime.worker import Worker


@pytest.fixture
def worker() -> Iterator[Worker]:
    value = Worker()
    try:
        yield value
    finally:
        value.loop.call_soon_threadsafe(value.loop.stop)
        value.thread.join(timeout=5)
        assert not value.thread.is_alive()


@pytest.mark.asyncio
@pytest.mark.parametrize("cleanup_fails", [False, True])
async def test_cancel_waits_for_cleanup_and_repeated_cancel_does_not_interrupt_it(
    worker: Worker, cleanup_fails: bool
) -> None:
    started, cleaning, finished = threading.Event(), threading.Event(), threading.Event()
    release = asyncio.Event()

    async def operation() -> None:
        started.set()
        try:
            await asyncio.Event().wait()
        finally:
            cleaning.set()
            await release.wait()
            finished.set()
            if cleanup_fails:
                raise RuntimeError("cleanup failed")

    task = asyncio.create_task(worker.call(operation, settle_cancel=True))
    assert await asyncio.to_thread(started.wait, 3)
    task.cancel()
    assert await asyncio.to_thread(cleaning.wait, 3)
    task.cancel()
    await asyncio.sleep(0)
    assert not task.done() and not finished.is_set()
    worker.loop.call_soon_threadsafe(release.set)
    with pytest.raises(asyncio.CancelledError) as caught:
        await asyncio.wait_for(task, 3)
    assert finished.is_set()
    if cleanup_fails:
        assert "cleanup failed" in " ".join(caught.value.__notes__)
    assert await worker.call(lambda: asyncio.sleep(0, result="available")) == "available"


@pytest.mark.asyncio
async def test_cancel_queued_operation_settles_without_leaving_work(worker: Worker) -> None:
    blocked, release, ran = threading.Event(), threading.Event(), threading.Event()

    def block() -> None:
        blocked.set()
        release.wait(3)

    async def operation() -> None:
        ran.set()
        await asyncio.sleep(10)

    worker.loop.call_soon_threadsafe(block)
    assert await asyncio.to_thread(blocked.wait, 3)
    task = asyncio.create_task(worker.call(operation, settle_cancel=True))
    await asyncio.sleep(0)
    task.cancel()
    await asyncio.sleep(0)
    release.set()
    with pytest.raises(asyncio.CancelledError):
        await asyncio.wait_for(task, 3)
    assert not ran.is_set()
