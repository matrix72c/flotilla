"""Public client. Construction is local; start owns all remote resources."""

from __future__ import annotations

import asyncio
import concurrent.futures
import copy
import threading
import time
import uuid
from collections.abc import AsyncIterator, Callable, Coroutine
from contextlib import asynccontextmanager, suppress
from dataclasses import replace
from pathlib import Path
from types import TracebackType
from typing import Any

from flotilla.config import FlotillaConfig
from flotilla.preparation.service import Preparer, inspect, inspect_environment
from flotilla.runtime.environment import Environment
from flotilla.runtime.events import EventDispatcher
from flotilla.runtime.manager import Manager
from flotilla.runtime.types import (
    CleanupError,
    CleanupReport,
    EnvironmentDefinition,
    EnvironmentInfo,
    EnvironmentInspection,
    OpenOptions,
    PreparationResidual,
    ResourceReservation,
    RunRequest,
    RuntimeEvent,
)
from flotilla.runtime.worker import Worker


class Flotilla:
    def __init__(
        self, *, deployment: str | Path | FlotillaConfig, on_event: Callable[[RuntimeEvent], None] | None = None
    ) -> None:
        self._deployment = (
            deployment.model_copy(deep=True)
            if isinstance(deployment, FlotillaConfig)
            else FlotillaConfig.load(deployment)
        )
        self._on_event = on_event
        self._events: EventDispatcher | None = None
        self._worker: Worker | None = None
        self._manager: Manager | None = None
        self._start_future: concurrent.futures.Future[None] | None = None
        self._initializing_task: asyncio.Task[Any] | None = None
        self._close_future: concurrent.futures.Future[CleanupReport] | None = None
        self._lock = threading.Lock()
        self._closed = False
        self._stop_callback_registered = False
        self.cleanup_report: CleanupReport | None = None
        self._preparer: Preparer | None = None

    @property
    def config(self) -> FlotillaConfig:
        """An independent configuration snapshot; callers cannot mutate the active policy."""
        return self._deployment.model_copy(deep=True)

    async def _initialize(self) -> None:
        self._initializing_task = asyncio.current_task()
        cfg = self._deployment
        self._manager = Manager(cfg)
        if self._on_event is not None:
            self._events = EventDispatcher(self._on_event)
            self._manager.on_event = self._events.emit
        self._preparer = Preparer(cfg, self._manager.anchor)
        async with asyncio.timeout(cfg.timeouts.prepare):
            await self._manager.start()

    async def start(self) -> None:
        with self._lock:
            if self._closed:
                raise RuntimeError("Flotilla is closed")
            if self._worker is None:
                self._worker = Worker()
                self._start_future = self._worker.submit(self._initialize)
            future = self._start_future
        assert future is not None
        try:
            await asyncio.shield(asyncio.wrap_future(future))
        except asyncio.CancelledError:
            # One cancelled waiter must not cancel initialization shared by other opens.
            raise
        except BaseException:
            # Initialization belongs to this client even when the awaiting caller disappears.
            future.cancel()
            with suppress(BaseException):
                await self.close()
            raise

    async def _call[T](
        self,
        action: Callable[[Manager], Coroutine[Any, Any, T]],
        *,
        settle_cancel: bool = False,
        cancel_result: Callable[[Manager, T], Coroutine[Any, Any, None]] | None = None,
    ) -> T:
        if self._closed or self._worker is None or self._manager is None:
            raise RuntimeError("Flotilla must be started and not closed")
        manager = self._manager
        return await self._worker.call(
            lambda: action(manager),
            settle_cancel=settle_cancel,
            cancel_result=(lambda value: cancel_result(manager, value)) if cancel_result is not None else None,
        )

    def inspect(self, task_path: str | Path) -> tuple[EnvironmentInspection, ...]:
        return inspect(self._deployment, task_path)

    def inspect_environment(self, definition: EnvironmentDefinition) -> EnvironmentInspection:
        return inspect_environment(self._deployment, definition)

    def client_reservation(self) -> ResourceReservation:
        """Per-client anchor overhead, in addition to environment reservations."""
        resource = self._deployment.anchor_settings().resources
        return ResourceReservation({"anchor": {"cpu": resource.cpu, "memory": resource.memory}})

    async def open_task(self, task_path: str | Path, options: OpenOptions | None = None) -> Environment:
        return await self._open_source(Path(task_path), options)

    async def open_environment(
        self,
        definition: EnvironmentDefinition,
        options: OpenOptions | None = None,
    ) -> Environment:
        return await self._open_source(definition, options)

    async def _open_source(self, definition: EnvironmentDefinition | Path, options: OpenOptions | None) -> Environment:
        options = copy.deepcopy(options or OpenOptions())
        definition = copy.deepcopy(definition)
        startup = options.startup_timeout or self._deployment.preparation.timeout_s
        lifetime = options.lifetime or self._deployment.runtime.run_timeout_s
        if lifetime > self._deployment.runtime.run_timeout_s:
            raise ValueError("lifetime exceeds runtime.run_timeout_s")
        deadline = time.monotonic() + startup
        eid = uuid.uuid4().hex
        try:
            async with asyncio.timeout(startup):
                await self.start()

                async def open_prepared(manager: Manager) -> EnvironmentInfo:
                    assert self._preparer is not None
                    task, timings = await self._preparer.resolve(definition, force_build=options.force_build)
                    request = RunRequest(
                        task,
                        options.request_id or eid,
                        runtime_params=options.runtime_params,
                        exec_env=options.exec_env,
                        disable_internet=options.disable_internet,
                        resources=options.resources,
                        lifetime=lifetime,
                    )
                    info = await manager.open(eid, request, deadline)
                    ready = manager.runs[eid].ready
                    assert ready is not None
                    manager.runs[eid].ready = replace(ready, timings={**ready.timings, **dict(timings)})
                    return replace(info, timings=(*timings, *info.timings))

                info = await self._call(open_prepared)
                return Environment(self, info)
        except BaseException:
            self._release_background(eid)
            raise

    @asynccontextmanager
    async def environment(
        self,
        source: str | Path | EnvironmentDefinition,
        options: OpenOptions | None = None,
    ) -> AsyncIterator[Environment]:
        env = (
            await self.open_environment(source, options)
            if isinstance(source, EnvironmentDefinition)
            else await self.open_task(source, options)
        )
        try:
            yield env
        except BaseException as primary:
            try:
                await env.release()
            except BaseException as cleanup:
                primary.add_note(f"environment cleanup also failed: {cleanup!r}")
            raise
        else:
            await env.release()

    def _release_background(self, eid: str) -> concurrent.futures.Future[None] | None:
        with self._lock:
            if self._closed or self._worker is None or self._manager is None:
                return None
            manager = self._manager
            return self._worker.submit(lambda: manager.release(eid))

    async def _release_environment(self, eid: str, timeout: float) -> CleanupReport:
        from flotilla.runtime.manager import _timeout

        _timeout(timeout)
        with self._lock:
            closing = self._closed
            if not closing:
                if self._worker is None or self._manager is None:
                    raise RuntimeError("Flotilla must be started")
                manager = self._manager
                future = self._worker.submit(lambda: manager.wait_release(eid, timeout))
        if closing:
            report = await self.close(timeout)
            scoped = CleanupReport(
                report.launch_id,
                report.cleaned,
                tuple(record for record in report.remaining if record.environment_id == eid),
                uncertain=report.uncertain,
            )
            if not scoped.clean:
                raise CleanupError(scoped)
            return scoped
        return await asyncio.shield(asyncio.wrap_future(future))

    async def drain(self, timeout: float | None = None) -> CleanupReport:
        timeout = self._deployment.timeouts.close if timeout is None else timeout
        if self.cleanup_report is not None:
            return self.cleanup_report
        if self._worker is None:
            return CleanupReport("", 0)

        async def drain_owned(manager: Manager) -> CleanupReport:
            report = await manager.drain(timeout)
            residuals = tuple(self._preparer.residuals) if self._preparer is not None else ()
            return replace(report, preparation_residuals=residuals)

        return await self._call(drain_owned)

    async def close(self, timeout: float | None = None) -> CleanupReport:
        from flotilla.runtime.manager import _timeout

        timeout = self._deployment.timeouts.close if timeout is None else timeout
        _timeout(timeout)
        with self._lock:
            if self.cleanup_report is not None:
                return self.cleanup_report
            self._closed = True
            worker = self._worker
            if worker is None:
                self.cleanup_report = CleanupReport("", 0)
                return self.cleanup_report
            if self._close_future is None:

                async def finish() -> CleanupReport:
                    deadline = time.monotonic() + timeout
                    task = self._initializing_task
                    if task is not None and not task.done():
                        task.cancel()
                        await asyncio.wait({task}, timeout=timeout)
                    # Read after initialization settles, on its own loop: a caller-thread snapshot
                    # could miss the manager created while close was being submitted.
                    manager = self._manager
                    if manager is None:
                        return CleanupReport("", 0)
                    residuals: tuple[PreparationResidual, ...] = ()
                    if self._preparer is not None:
                        residuals = await self._preparer.close(max(0.001, deadline - time.monotonic()))
                    report = await manager.close(max(0.001, deadline - time.monotonic()))
                    return replace(report, preparation_residuals=residuals)

                self._close_future = worker.submit(finish)
            future = self._close_future

        # Shield the owned shutdown. On cancellation it still runs in the worker and stops that loop itself.
        def stop_after_owned_cleanup(_: concurrent.futures.Future[CleanupReport]) -> None:
            if not worker.thread.is_alive() or worker.loop.is_closed():
                return

            async def finish_preparation() -> None:
                # close may exhaust its budget before an owned cleanup finishes.
                # Keep its loop alive; Worker finalization must not cancel that cleanup.
                if self._preparer is not None:
                    pending = list(self._preparer.pending)
                    if pending:
                        await asyncio.gather(*pending, return_exceptions=True)
                if self._events is not None:
                    self._events.close()
                worker.loop.stop()

            worker.submit(finish_preparation)

        with self._lock:
            if not self._stop_callback_registered:
                self._stop_callback_registered = True
                future.add_done_callback(stop_after_owned_cleanup)
        report = await asyncio.shield(asyncio.wrap_future(future))
        self.cleanup_report = report
        if any(record.kind == "operation" for record in report.preparation_residuals):
            # The bounded close result reports unconfirmed cleanup; its independent
            # task retains ownership and stops the loop when it has finished.
            return report
        await asyncio.to_thread(worker.thread.join, 5)
        if worker.thread.is_alive():
            raise RuntimeError("Flotilla background thread failed to stop")
        return report

    async def __aenter__(self) -> Flotilla:
        await self.start()
        return self

    async def __aexit__(
        self, typ: type[BaseException] | None, exc: BaseException | None, tb: TracebackType | None
    ) -> None:
        try:
            report = await self.close()
        except BaseException as cleanup:
            if exc is None:
                raise
            exc.add_note(f"Flotilla shutdown failed: {cleanup!r}")
            return
        if not report.clean:
            if exc is None:
                raise CleanupError(report)
            exc.add_note(f"Flotilla cleanup incomplete: {report!r}")
