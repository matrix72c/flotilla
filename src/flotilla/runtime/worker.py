"""One owned event loop per client, never inherited across processes."""

from __future__ import annotations

import asyncio
import concurrent.futures
import os
import threading
from collections.abc import Callable, Coroutine
from typing import Any


class Worker:
    def __init__(self) -> None:
        self.pid = os.getpid()
        self.loop = asyncio.new_event_loop()
        self._ready = threading.Event()
        self.thread = threading.Thread(target=self._run, name="flotilla", daemon=True)
        self.thread.start()
        self._ready.wait()

    def _run(self) -> None:
        asyncio.set_event_loop(self.loop)
        self._ready.set()
        try:
            self.loop.run_forever()
        finally:
            pending = asyncio.all_tasks(self.loop)
            for task in pending:
                task.cancel()
            if pending:
                self.loop.run_until_complete(asyncio.gather(*pending, return_exceptions=True))
            self.loop.run_until_complete(self.loop.shutdown_asyncgens())
            self.loop.close()

    def submit[T](self, factory: Callable[[], Coroutine[Any, Any, T]]) -> concurrent.futures.Future[T]:
        if os.getpid() != self.pid:
            raise RuntimeError("Flotilla clients cannot cross process boundaries")
        if not self.thread.is_alive() or self.loop.is_closed():
            raise RuntimeError("Flotilla background loop is closed")

        async def invoke() -> T:
            return await factory()

        return asyncio.run_coroutine_threadsafe(invoke(), self.loop)

    async def call[T](
        self,
        factory: Callable[[], Coroutine[Any, Any, T]],
        *,
        settle_cancel: bool = False,
        cancel_result: Callable[[T], Coroutine[Any, Any, None]] | None = None,
    ) -> T:
        if not settle_cancel:
            return await asyncio.wrap_future(self.submit(factory))
        operation: asyncio.Task[Any] | None = None
        cancel_requested = False

        async def invoke() -> T:
            nonlocal operation
            operation = asyncio.current_task()
            if cancel_requested:
                raise asyncio.CancelledError
            return await factory()

        def cancel() -> None:
            nonlocal cancel_requested
            cancel_requested = True
            if operation is not None:
                operation.cancel()

        # Cancelling a concurrent Future marks it done before the worker's
        # coroutine finishes its finally blocks. Keep that future shielded and
        # cancel the actual operation once, then wait for its cleanup to settle.
        result = asyncio.wrap_future(self.submit(invoke))
        try:
            return await asyncio.shield(result)
        except asyncio.CancelledError as primary:
            self.loop.call_soon_threadsafe(cancel)
            while not result.done():
                try:
                    await asyncio.shield(result)
                except asyncio.CancelledError:
                    pass
                except Exception:
                    break
            try:
                value = result.result()
            except asyncio.CancelledError:
                pass
            except Exception as exc:
                primary.add_note(f"Worker operation failed during cancellation: {exc!r}")
            else:
                # A remote start can finish just before cancellation reaches this loop.
                # Its result still needs an owner even though it was never delivered.
                if cancel_result is not None:
                    cleanup = asyncio.wrap_future(self.submit(lambda: cancel_result(value)))
                    while not cleanup.done():
                        try:
                            await asyncio.shield(cleanup)
                        except asyncio.CancelledError:
                            continue
                    cleanup.result()
            raise
