"""Public client. Construction is local; start owns all remote resources."""

from __future__ import annotations

import asyncio
import concurrent.futures
import copy
import dataclasses
import threading
import time
import uuid
from collections.abc import AsyncIterator, Callable, Coroutine, Mapping
from contextlib import asynccontextmanager, suppress
from types import TracebackType
from typing import TYPE_CHECKING, Any

from flotilla.compose.plan_task import AgentOverlay, ImageConfigSource, plan_task
from flotilla.compose.task import HarborTask, load_task
from flotilla.config import FlotillaConfig
from flotilla.core.plan import TaskSource, TrialPlan
from flotilla.platform.base import Resources
from flotilla.registry import ImageConfigs
from flotilla.resources import cpu, memory
from flotilla.runtime.environment import Environment
from flotilla.runtime.events import EventDispatcher
from flotilla.runtime.manager import Manager
from flotilla.runtime.types import (
    ENVIRONMENT_RESERVATION_COPIES,
    CleanupError,
    CleanupReport,
    OpenOptions,
    ResourceReservation,
    RunRequest,
    RuntimeEvent,
    require_timeout,
)
from flotilla.runtime.worker import Worker

if TYPE_CHECKING:
    pass


def with_options(plan: TrialPlan, options: OpenOptions) -> TrialPlan:
    """Apply what a single run decides to a plan that was resolved without it.

    `plan` resolves a task into a trial; these are the per-run choices `plan` folds in when it
    resolves one itself. A caller handing in an already-resolved plan makes the same choices
    through its `OpenOptions`, so they are applied here rather than silently dropped.
    """
    plan = with_resources(plan, options.resources)
    if options.external_mounts:
        plan = dataclasses.replace(
            plan,
            external_mounts={
                **plan.external_mounts,
                **{name: tuple(mounts) for name, mounts in options.external_mounts.items()},
            },
        )
    if options.workspace_mounts:
        plan = dataclasses.replace(
            plan,
            workspace_root=options.workspace_root or plan.workspace_root,
            workspace_mounts={
                **plan.workspace_mounts,
                **{name: tuple(mounts) for name, mounts in options.workspace_mounts.items()},
            },
        )
    return plan


def plan_for(
    cfg: FlotillaConfig,
    task: HarborTask,
    source: TaskSource,
    configs: ImageConfigSource,
    options: OpenOptions | None = None,
) -> TrialPlan:
    """Resolve `task` into the plan `open` would run, against this deployment's overlay and resources.

    The one place a task directory becomes a trial plan: `Flotilla.plan_environment` and `flotilla
    inspect` both land here, so a task that plans for one plans for the other.
    """
    declared = {name: svc.resources for name, svc in task.project.services.items()}
    overlay = cfg.agent_overlay
    plan = plan_task(
        task,
        source,
        configs=configs,
        resources=cfg.resources.for_services(task.name, declared),
        overlay=AgentOverlay(
            env=overlay.env,
            services=None if overlay.services is None else frozenset(overlay.services),
        ),
    )
    return with_options(plan, options or OpenOptions())


def with_resources(plan: TrialPlan, overrides: Mapping[str, Mapping[str, str]]) -> TrialPlan:
    """One run's CPU / memory override, applied to a plan.

    A run may be given more capacity than the task asks for without becoming a different task,
    so this is the one planned value a caller can still change at `open`.
    """
    if not overrides:
        return plan
    units = dict(plan.units)
    for name, override in overrides.items():
        if name not in units:
            raise ValueError(f"resource override names unknown service {name!r}")
        if set(override) - {"cpu", "memory"}:
            raise ValueError("only CPU and memory resource overrides are supported")
        original = units[name].resources
        units[name] = dataclasses.replace(
            units[name],
            resources=Resources(
                cpu=cpu(override.get("cpu", original.cpu)), memory=memory(override.get("memory", original.memory))
            ),
        )
    return dataclasses.replace(plan, units=units)


class Flotilla:
    def __init__(
        self, *, deployment: str | FlotillaConfig, on_event: Callable[[RuntimeEvent], None] | None = None
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
        #: Image configs, by digest, for as long as this client lives. A digest names one config
        #: forever, so the only cost of a second task naming the same image is a dict lookup.
        self.image_configs = ImageConfigs(
            credentials=self._deployment.registry.credentials(),
            store=self._deployment.registry.config_cache,
        )

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
        async with asyncio.timeout(cfg.timeouts.client_start):
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

    def reservation(self, plan: TrialPlan) -> ResourceReservation:
        """Admission budget of one environment, at the resources its plan resolved to."""
        resources = {
            name: {"cpu": unit.resources.cpu, "memory": unit.resources.memory} for name, unit in plan.units.items()
        }
        return ResourceReservation(resources, copies=ENVIRONMENT_RESERVATION_COPIES)

    def client_reservation(self) -> ResourceReservation:
        """Per-client anchor overhead, in addition to environment reservations."""
        resource = self._deployment.anchor_settings(workspace=True).resources
        return ResourceReservation({"anchor": {"cpu": resource.cpu, "memory": resource.memory}})

    def plan(self, source: TaskSource, options: OpenOptions | None = None) -> TrialPlan:
        """Resolve a task directory into the trial `open` would create, without creating it.

        Separate from `open` so a caller can check a whole dataset before spending GPU time on
        it, and so the cost of planning is visible rather than hidden inside an await.
        """
        options = options or OpenOptions()
        parameters = {**self._deployment.runtime_params, **options.parameters}
        return self.plan_environment(load_task(source.local, compose_environment=parameters), source, options)

    def plan_environment(self, task: HarborTask, source: TaskSource, options: OpenOptions | None = None) -> TrialPlan:
        """The same, for a task directory already read: a graded task reads it once per role."""
        return plan_for(self._deployment, task, source, self.image_configs, options)

    async def open(self, source: TaskSource | TrialPlan, options: OpenOptions | None = None) -> Environment:
        """Open an environment for a task directory, or for a plan already resolved from one."""
        options = copy.deepcopy(options or OpenOptions())
        # A plan the caller already resolved still needs this run's own choices folded in;
        # resolving a task source here does that as part of planning it.
        plan = with_options(source, options) if isinstance(source, TrialPlan) else self.plan(source, options)
        admission = options.admission_timeout or self._deployment.limits.admission_timeout
        startup = options.startup_timeout or self._deployment.runtime.startup_timeout_s
        lifetime = options.lifetime or self._deployment.runtime.run_timeout_s
        if lifetime > self._deployment.runtime.run_timeout_s:
            raise ValueError("lifetime exceeds runtime.run_timeout_s")
        eid = uuid.uuid4().hex
        request = RunRequest(
            plan,
            options.request_id or eid,
            exec_env=options.exec_env,
            disable_internet=options.disable_internet,
            workspace_id=options.workspace_id,
            lifetime=lifetime,
        )
        try:
            # Each budget bounds its own phase: client start (timeouts.client_start, inside start()), the admission
            # queue, then startup from admission to ready.
            await self.start()
            info = await self._call(
                lambda manager: manager.open(eid, request, admission_s=admission, startup_s=startup)
            )
            return Environment(self, info)
        except BaseException:
            self._release_background(eid)
            raise

    @asynccontextmanager
    async def environment(
        self, source: TaskSource | TrialPlan, options: OpenOptions | None = None
    ) -> AsyncIterator[Environment]:
        env = await self.open(source, options)
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
        require_timeout(timeout)
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

        return await self._call(lambda manager: manager.drain(timeout))

    async def close(self, timeout: float | None = None) -> CleanupReport:
        timeout = self._deployment.timeouts.close if timeout is None else timeout
        require_timeout(timeout)
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
                    return await manager.close(max(0.001, deadline - time.monotonic()))

                self._close_future = worker.submit(finish)
            future = self._close_future

        # Shield the owned shutdown. On cancellation it still runs in the worker and stops that loop itself.
        def stop_after_owned_cleanup(_: concurrent.futures.Future[CleanupReport]) -> None:
            if not worker.thread.is_alive() or worker.loop.is_closed():
                return

            async def stop_loop() -> None:
                if self._events is not None:
                    self._events.close()
                worker.loop.stop()

            worker.submit(stop_loop)

        with self._lock:
            if not self._stop_callback_registered:
                self._stop_callback_registered = True
                future.add_done_callback(stop_after_owned_cleanup)
        report = await asyncio.shield(asyncio.wrap_future(future))
        self.cleanup_report = report
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
