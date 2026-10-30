"""Owned resources. All methods run on the client's background event loop."""

from __future__ import annotations

import asyncio
import logging
import math
import time
import uuid
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field, replace
from typing import Any

from flotilla.config import FlotillaConfig
from flotilla.core import labels, network
from flotilla.core.anchor import Anchor
from flotilla.core.deletion import Deleter
from flotilla.core.plan import TrialPlan
from flotilla.core.reaper import CleanupRecord, Reaper
from flotilla.core.runtime import Monitor
from flotilla.core.trial import Ready, Trial
from flotilla.core.waiting import RateLimiter
from flotilla.platform.base import (
    Clock,
    ErrorCategory,
    ExecAttempt,
    ExternalPolicy,
    FlotillaError,
    InstanceHandle,
    Platform,
)
from flotilla.platform.clock import SystemClock
from flotilla.platform.execution import confirm_stopped
from flotilla.runtime.planning import AgentOverlay, to_plan
from flotilla.runtime.types import (
    ENVIRONMENT_RESERVATION_COPIES,
    CleanupError,
    CleanupReport,
    EnvironmentInfo,
    RunRequest,
    RuntimeEvent,
    ServiceInfo,
)
from flotilla.share.release import verify_release


def invalid(message: str) -> FlotillaError:
    return FlotillaError(message, stage="prepare", category=ErrorCategory.INVALID, retryable=False)


@dataclass
class Run:
    request: RunRequest
    plan: TrialPlan
    deadline: float
    reservation: int
    opening: asyncio.Task[EnvironmentInfo] | None = None
    ready: Ready | None = None
    monitor: Monitor | None = None
    released: bool = False
    recovering: int = 0
    timer: asyncio.Task[None] | None = None
    operations: set[asyncio.Task[object]] = field(default_factory=set)
    service_operations: dict[asyncio.Task[object], str] = field(default_factory=dict)
    terminations: dict[str, asyncio.Task[None]] = field(default_factory=dict)


class Manager:
    def __init__(self, cfg: FlotillaConfig, *, platform: Platform | None = None, clock: Clock | None = None) -> None:
        self.cfg = cfg
        self.on_event: Callable[[RuntimeEvent], None] | None = None
        self.clock = clock or SystemClock()
        report = cfg.load_report()
        cfg.check_against(report)
        self.capability_report = report
        command_rate = RateLimiter(self.clock, cfg.limits.exec_rate)
        self.platform = platform or cfg.build_platform(
            clock=self.clock, caps=cfg.platform_capabilities(report), command_gate=command_rate.acquire
        )
        self.launch_id = f"run-{uuid.uuid4().hex[:16]}"
        self.anchor = Anchor(self.platform, self.clock, self.launch_id, cfg.anchor_settings())
        self.deleter = Deleter(self.platform, self.clock, concurrency=cfg.limits.reap_concurrency)
        self.reaper = Reaper(self.platform, self.clock, self.anchor, self.deleter, cfg.reaper_settings())
        self.runs: dict[str, Run] = {}
        self.reaping: dict[str, asyncio.Task[None]] = {}
        self.failures: dict[str, CleanupRecord] = {}
        self.failed_runs: dict[str, Run] = {}
        self.cleaned = 0
        self.reserved = 1  # anchor; each task reserves two instances per service for the startup retry
        self.changed = asyncio.Condition()
        self.exec_slots = asyncio.Semaphore(cfg.limits.exec_concurrency)
        self.cancellations: set[asyncio.Task[None]] = set()
        self.closing = False
        self.started = False
        self.launch_directory_attempted = False
        self.close_report: CleanupReport | None = None
        self.closing_task: asyncio.Task[CleanupReport] | None = None

    def emit(
        self,
        kind: str,
        eid: str,
        *,
        request: RunRequest | None = None,
        elapsed_s: float = 0,
        service: str | None = None,
        category: str | None = None,
        attempt_id: str | None = None,
        command_id: str | None = None,
    ) -> None:
        run = self.runs.get(eid)
        req = request or (run.request if run else None)
        if self.on_event is None or req is None:
            return
        try:
            self.on_event(
                RuntimeEvent(
                    kind, eid, req.request_id, req.task.digest, elapsed_s, service, category, attempt_id, command_id
                )
            )
        except Exception:
            logging.getLogger(__name__).warning("runtime event callback failed")

    async def start(self) -> None:
        # The client owns shutdown even when preparation fails or is cancelled.
        await self.anchor.start()
        if not await self.anchor.exists("", stage="prepare"):
            raise invalid("shared storage root does not exist")
        report = await verify_release(
            self.anchor, self.cfg.share.release, expected_sha256=self.cfg.share.release_sha256
        )
        if not report.ok:
            raise invalid("pinned share release failed verification")
        if not await self.anchor.exists(f"share/releases/{self.cfg.share.release}/bin/busybox", stage="prepare"):
            raise invalid("share release has no bin/busybox")
        self.launch_directory_attempted = True
        await self.anchor.mkdir(f"launches/{self.launch_id}", stage="prepare")
        self.started = True

    def plan(self, request: RunRequest) -> TrialPlan:
        manifest = request.task.manifest()
        published = manifest.task_files_published
        if manifest.task_files and (published is None or published.storage_root != self.cfg.storage_settings().root):
            raise invalid("task files must be published to this deployment's storage root")
        if set(request.exec_env) - set(manifest.units):
            raise invalid("exec_env contains unknown services")
        cfg = self.cfg.model_copy(update={"runtime_params": {**self.cfg.runtime_params, **request.runtime_params}})
        cfg.check_against(self.capability_report)
        resources = cfg.resources.for_manifest(manifest)
        if set(request.resources) - set(manifest.units):
            raise invalid("resource override names unknown service")
        from flotilla.compose.resources import cpu, memory
        from flotilla.platform.base import Resources

        for name, override in request.resources.items():
            if set(override) - {"cpu", "memory"}:
                raise invalid("only CPU and memory resource overrides are supported")
            original = resources[name]
            resources[name] = Resources(
                cpu=cpu(override.get("cpu", original.cpu)),
                memory=memory(override.get("memory", original.memory)),
            )
        overlay = cfg.agent_overlay
        plan = to_plan(
            manifest,
            runtime_params=cfg.runtime_params,
            resources=resources,
            overlay=AgentOverlay(
                env=overlay.env,
                services=None if overlay.services is None else frozenset(overlay.services),
            ),
        )
        members = network.link_members(plan)
        caps = self.platform.caps
        for name, unit in plan.units.items():
            try:
                caps.require_filesystem(
                    unit.readonly_rootfs,
                    unit.tmpfs_mounts,
                    privileged=unit.privileged,
                    memory=unit.resources.memory,
                    volume_paths=tuple(m.target for m in unit.mounts),
                    shm_size_bytes=unit.shm_size_bytes,
                )
                if unit.readonly_rootfs and not caps.shared_volume:
                    raise ValueError("readonly_rootfs requires shared storage for framework scratch space")
            except ValueError as exc:
                raise invalid(f"{name}: {exc}") from exc
        if network.topology(plan) is not None and (not caps.link or len(members) > caps.link_max_members):
            raise invalid("deployment cannot connect this task's services")
        if plan.volumes and not caps.shared_volume:
            raise invalid("deployment does not support shared trial volumes")
        if any(unit.privileged for unit in plan.units.values()) and not caps.privileged_runtime:
            raise invalid("deployment does not support privileged services")
        if (request.disable_internet or set(plan.units) - plan.external_units) and not caps.supports_no_network:
            raise invalid("no-network requires deny-all egress with only declared cluster DNS exceptions")
        if set(plan.units) - members and not caps.supports_network_none:
            raise invalid("network_mode none requires inbound isolation or sandbox ingress enforcement")
        return plan

    async def open(self, eid: str, request: RunRequest, deadline: float) -> EnvironmentInfo:
        if self.closing or not self.started:
            raise RuntimeError("Flotilla is not accepting environments")
        queued = time.monotonic()
        self.emit("queued", eid, request=request)
        plan = self.plan(request)
        reservation = len(plan.units) * ENVIRONMENT_RESERVATION_COPIES
        if reservation + 1 > self.cfg.limits.instance_budget:
            raise invalid("task exceeds instance_budget (includes startup retry reservations and anchor)")
        try:
            async with asyncio.timeout(max(0, deadline - time.monotonic())):
                async with self.changed:
                    await self.changed.wait_for(
                        lambda: (
                            self.closing
                            or (
                                sum(not run.released for run in self.runs.values()) < self.cfg.limits.trial_concurrency
                                and self.reserved + reservation <= self.cfg.limits.instance_budget
                            )
                        )
                    )
                    if self.closing:
                        raise RuntimeError("Flotilla is closing")
                    self.anchor.ensure_alive("create")
                    run = Run(request, plan, deadline, reservation)
                    run.opening = asyncio.current_task()
                    self.runs[eid] = run
                    self.reserved += reservation
                    self.reaper.track(eid)
                self.emit("admitted", eid, elapsed_s=time.monotonic() - queued)
                settings = self.cfg.trial_settings()
                if request.disable_internet:
                    settings = replace(settings, external=ExternalPolicy(mode="none"))
                trial = Trial(self.platform, self.clock, self.anchor, self.reaper, plan, eid, settings)
                try:
                    run.ready = await trial.start()
                finally:
                    for stage, elapsed in trial.timings.items():
                        self.emit(stage, eid, elapsed_s=elapsed)
                if request.lifetime is not None:
                    run.deadline = time.monotonic() + request.lifetime
                run.monitor = Monitor(
                    self.platform,
                    self.clock,
                    self.anchor,
                    plan,
                    run.ready.handles,
                    run.ready.pids,
                    self.cfg.monitor_settings(),
                )
                run.monitor.start()
                run.timer = asyncio.create_task(self._expire(eid, run.deadline))
                return self.info(eid)
        except BaseException as exc:
            self.emit(
                "open_failed",
                eid,
                request=request,
                elapsed_s=time.monotonic() - queued,
                category=exc.category.value if isinstance(exc, FlotillaError) else type(exc).__name__,
            )
            await self.release(eid)
            raise
        finally:
            if eid in self.runs:
                self.runs[eid].opening = None

    async def _expire(self, eid: str, deadline: float) -> None:
        await asyncio.sleep(max(0, deadline - time.monotonic()))
        await self.release(eid)

    def get(self, eid: str) -> Run:
        run = self.runs.get(eid)
        if run is None or run.released:
            raise FlotillaError(
                "environment released or expired", stage="run", category=ErrorCategory.LOST, retryable=True
            )
        if run.recovering:
            raise FlotillaError(
                "command cancellation is being confirmed",
                stage="run",
                category=ErrorCategory.TRANSIENT,
                retryable=False,
            )
        if time.monotonic() >= run.deadline:
            raise TimeoutError("environment request deadline exceeded")
        self.anchor.ensure_alive("run")
        if run.monitor and run.monitor.lost:
            raise run.monitor.lost
        return run

    def info(self, eid: str) -> EnvironmentInfo:
        run = self.runs[eid]
        assert run.ready is not None
        manifest = run.request.task.manifest()
        return EnvironmentInfo(
            eid,
            run.request.request_id,
            run.request.task.digest,
            manifest.agent_service,
            tuple(
                ServiceInfo(
                    name,
                    handle.iid,
                    run.plan.units[name].image,
                    run.plan.units[name].context.cwd,
                    run.plan.units[name].resources.cpu,
                    run.plan.units[name].resources.memory,
                )
                for name, handle in sorted(run.ready.handles.items())
            ),
            tuple(run.ready.timings.items()),
            "released" if run.released else "ready",
            tuple(self.capability_report.security_gaps()),
            tuple(sorted(self.cfg.security.accept_insecure & set(self.capability_report.security_gaps()))),
        )

    async def operation[T](self, eid: str, service: str, action: Callable[[Run], Awaitable[T]]) -> T:
        run = self.get(eid)
        if service not in run.plan.units:
            raise KeyError(service)
        if service in run.terminations:
            raise FlotillaError(
                "service has been terminated",
                stage="run",
                category=ErrorCategory.INVALID,
                retryable=False,
                service=service,
            )

        async def execute() -> T:
            began = time.monotonic()
            operation_id = uuid.uuid4().hex
            self.emit("operation_started", eid, service=service, attempt_id=operation_id)
            try:
                async with asyncio.timeout(max(0, run.deadline - time.monotonic())):
                    self.get(eid)
                    return await action(run)
            except FlotillaError as exc:
                if exc.category is ErrorCategory.NOT_FOUND and run.monitor:
                    lost = await run.monitor.confirm(service)
                    if lost:
                        raise lost from exc
                raise
            finally:
                self.emit(
                    "operation_finished",
                    eid,
                    service=service,
                    elapsed_s=time.monotonic() - began,
                    attempt_id=operation_id,
                )

        task = asyncio.create_task(execute())
        run.operations.add(task)
        run.service_operations[task] = service
        try:
            return await task
        finally:
            run.operations.discard(task)
            run.service_operations.pop(task, None)

    async def terminate(self, eid: str, service: str, timeout: float) -> None:
        """Permanently terminate a single owned sandbox, confirming deletion."""
        _timeout(timeout)
        run = self.get(eid)
        if service not in run.plan.units:
            raise KeyError(service)
        if service not in run.terminations:
            assert run.monitor is not None and run.ready is not None
            run.monitor.retire(service)

            async def finish() -> None:
                assert run.ready is not None and run.monitor is not None
                handle = run.ready.handles[service]
                operations = [task for task, name in run.service_operations.items() if name == service]
                try:
                    async with asyncio.timeout(timeout):
                        for task in operations:
                            task.cancel()
                        if operations:
                            await asyncio.gather(*operations, return_exceptions=True)
                        if not await self.deleter.delete(handle.iid):
                            raise TimeoutError("service deletion could not be confirmed")
                    self.emit("service_terminated", eid, service=service)
                except BaseException as exc:
                    run.monitor.mark_lost(f"termination of service {service} was not confirmed", cause=exc)
                    if isinstance(exc, TimeoutError):
                        raise TimeoutError(
                            f"termination not confirmed: environment={eid}, service={service}, "
                            f"instance={handle.iid}, timeout={timeout}s; release the environment to retry cleanup"
                        ) from exc
                    raise

            task = asyncio.create_task(finish())
            task.add_done_callback(_retrieve_termination)
            run.terminations[service] = task
        await asyncio.shield(run.terminations[service])

    async def cancel_execution(self, eid: str, handle: InstanceHandle, attempt: ExecAttempt) -> None:
        run = self.runs[eid]
        if not attempt.sent or run.released:
            return
        if run.ready and any(run.ready.handles[name].iid == handle.iid for name in run.terminations):
            # The owned termination task confirms deletion of the whole sandbox.
            return
        operation = asyncio.current_task()
        run.recovering += 1
        self.emit("cancel_requested", eid, command_id=attempt.command_id)

        async def confirm() -> None:
            try:
                async with asyncio.timeout(self.cfg.runtime.cancel_confirm_timeout_s):
                    if attempt.command_id is None:
                        await self.release(eid, preserve_operation=operation)
                        return
                    stopped = await confirm_stopped(
                        self.clock,
                        attempt.command_id,
                        lambda pid: self.platform.interrupt(handle, pid),
                        lambda pid: self.platform.process_status(handle, pid),
                        self.cfg.runtime.cancel_confirm_timeout_s,
                    )
                    if not stopped:
                        await self.release(eid, preserve_operation=operation)
                    elif run.monitor and (error := await run.monitor.check()):
                        raise error
            except Exception:
                await self.release(eid, preserve_operation=operation)
            finally:
                run.recovering -= 1
                self.emit(
                    "cancel_finished",
                    eid,
                    category="released" if run.released else "confirmed",
                    command_id=attempt.command_id,
                )

        task = asyncio.create_task(confirm())
        self.cancellations.add(task)
        task.add_done_callback(self.cancellations.discard)
        await asyncio.shield(task)

    async def check(self, eid: str) -> None:
        run = self.get(eid)
        assert run.monitor is not None
        if (error := await run.monitor.check()) is not None:
            raise error
        self.get(eid)

    async def release(self, eid: str, *, preserve_operation: asyncio.Task[Any] | None = None) -> None:
        run = self.runs.get(eid)
        if run is None or run.released:
            return
        # No suspension before ownership transfer: cancellation cannot interrupt this transition.
        run.released = True
        current = asyncio.current_task()
        # Confirmation runs in its own task. Let its initiating operation report
        # the unknown outcome instead of cancelling it as collateral cleanup.
        for task in (run.opening, run.timer, *run.operations):
            if task is not None and task is not current and task is not preserve_operation:
                task.cancel()
        self.reaper.release(eid)
        self.reaping[eid] = asyncio.create_task(self._finish_release(eid, run))
        async with self.changed:
            self.changed.notify_all()

    async def _finish_release(self, eid: str, run: Run) -> None:
        began = time.monotonic()
        if run.monitor:
            await run.monitor.stop()
        if run.operations:
            await asyncio.gather(*tuple(run.operations), return_exceptions=True)
        if run.terminations:
            for task in run.terminations.values():
                task.cancel()
            await asyncio.gather(*run.terminations.values(), return_exceptions=True)
        record = await self.reaper.wait(eid)
        if record.clean:
            self.reaper.completed.pop(eid)
            self.failures.pop(eid, None)
            self.failed_runs.pop(eid, None)
            self.cleaned += 1
            self.reserved -= run.reservation
        else:
            self.failures[eid] = record
            self.failed_runs[eid] = run
        self.emit(
            "cleanup_finished",
            eid,
            elapsed_s=time.monotonic() - began,
            category="clean" if record.clean else "incomplete",
        )
        self.runs.pop(eid, None)
        self.reaping.pop(eid, None)
        async with self.changed:
            self.changed.notify_all()

    async def wait_release(self, eid: str, timeout: float) -> CleanupReport:
        """Wait only for this environment; cancellation never cancels its reaper."""
        _timeout(timeout)
        if eid in self.failures and eid not in self.reaping and not self.closing:
            self.reaper.retry(eid)
            self.reaping[eid] = asyncio.create_task(self._finish_release(eid, self.failed_runs[eid]))
        await self.release(eid)
        pending = self.reaping.get(eid)
        if pending is not None:
            done, _ = await asyncio.wait({pending}, timeout=timeout)
            if done:
                pending.result()
        report = self.report({eid})
        if not report.clean:
            raise CleanupError(report)
        return report

    def report(self, ids: set[str] | None = None) -> CleanupReport:
        records = dict(self.failures)
        for eid in self.reaping:
            if self.reaper.is_tracking(eid) or eid in self.reaper.completed:
                records[eid] = self.reaper.snapshot(eid)
        return CleanupReport(
            self.launch_id,
            self.cleaned,
            tuple(record for eid, record in sorted(records.items()) if ids is None or eid in ids),
        )

    async def drain(self, timeout: float) -> CleanupReport:
        _timeout(timeout)
        ids = set(self.reaping) | set(self.failures)
        tasks = tuple(self.reaping.values())
        if tasks:
            await asyncio.wait(tasks, timeout=timeout)
        return self.report(ids)

    async def close(self, timeout: float) -> CleanupReport:
        _timeout(timeout)
        if self.closing_task is None:
            self.closing_task = asyncio.create_task(self._close(timeout))
        return await asyncio.shield(self.closing_task)

    async def _close(self, timeout: float) -> CleanupReport:
        self.closing = True
        async with self.changed:
            self.changed.notify_all()
        for eid in tuple(self.runs):
            await self.release(eid)
        anchor_id = self.anchor.iid
        launch_dirs: tuple[str, ...] = ()
        anchor_left: tuple[str, ...] = ()
        uncertain = False
        anchor_cleanup = self.deleter.scope()
        try:
            async with asyncio.timeout(timeout):
                await self.drain(timeout)
                if self.launch_directory_attempted:
                    clean = await self.reaper.close()
                else:
                    await self.anchor.close()
                    clean = True
                # Reaper's close deletes the anchor; confirm independently before claiming clean.
                if anchor_id and not await self.deleter.delete(anchor_id):
                    anchor_left = (anchor_id,)
                if self.anchor.creation_uncertain:
                    # Cancelled create may have taken effect without returning an ID.
                    uncertain = not await anchor_cleanup.sweep(
                        labels.anchor_labels(self.launch_id),
                        until=self.clock.monotonic() + self.platform.caps.list_visibility_s,
                    )
                    anchor_left = tuple(sorted(set(anchor_left) | anchor_cleanup.pending()))
                if not clean:
                    launch_dirs = (f"launches/{self.launch_id}",)
        except TimeoutError:
            launch_dirs = (f"launches/{self.launch_id}",) if self.launch_directory_attempted else ()
            anchor_left = tuple(sorted(({anchor_id} if anchor_id else set()) | anchor_cleanup.pending()))
            uncertain = True
        finally:
            anchor_cleanup.cancel()
            for task in tuple(self.reaping.values()):
                task.cancel()
            if self.reaping:
                await asyncio.gather(*tuple(self.reaping.values()), return_exceptions=True)
            for task in tuple(self.cancellations):
                task.cancel()
            if self.cancellations:
                await asyncio.gather(*tuple(self.cancellations), return_exceptions=True)
            await self._close_platform()
        self.close_report = replace(
            self.report(), launch_directories=launch_dirs, anchor_instances=anchor_left, uncertain=uncertain
        )
        return self.close_report

    async def _close_platform(self) -> None:
        try:
            await asyncio.wait_for(self.platform.aclose(), timeout=5)
        except Exception as exc:
            # Connection closure must not replace the resource cleanup report.
            logging.getLogger(__name__).warning("platform connection close failed (%s)", type(exc).__name__)


def _timeout(value: float) -> None:
    if not math.isfinite(value) or value <= 0:
        raise ValueError("timeout must be finite and positive")


def _retrieve_termination(task: asyncio.Task[None]) -> None:
    if not task.cancelled():
        task.exception()
