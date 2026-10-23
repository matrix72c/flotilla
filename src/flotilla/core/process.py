"""Start and monitor service processes using actual dependency gates."""

from __future__ import annotations

import asyncio
import logging
import shlex
from collections.abc import Mapping
from dataclasses import dataclass

from flotilla.core.plan import Condition, Healthcheck, TrialPlan, Unit
from flotilla.core.waiting import run_all
from flotilla.platform.base import (
    RUNTIME_MOUNT,
    Clock,
    ErrorCategory,
    ExecResult,
    FlotillaError,
    InstanceHandle,
    Platform,
    ProcessSpec,
)

log = logging.getLogger(__name__)

BUSYBOX = "/.flotilla/bin/busybox"
LOG_DIR = "/run/flotilla"
TAIL_BYTES = 4096
_OWN_TIMEOUT_S = 30.0  # the orchestration core's own execs (create log dir, read log tail)


def log_path(service: str, *, readonly_rootfs: bool = False) -> str:
    return f"{RUNTIME_MOUNT if readonly_rootfs else LOG_DIR}/{service}.log"


def service_spec(unit: Unit, argv: tuple[str, ...], *, timeout_s: float | None) -> ProcessSpec:
    """Build a `ProcessSpec` from the service's exec context (main process and healthchecks use its env, user, cwd)."""
    ctx = unit.context
    return ProcessSpec(argv=argv, uid=ctx.uid, gid=ctx.gid, cwd=ctx.cwd, env=ctx.env, timeout_s=timeout_s)


def own_spec(argv: tuple[str, ...], *, timeout_s: float = _OWN_TIMEOUT_S) -> ProcessSpec:
    """The orchestration core's own operations: empty env, root, `/`."""
    return ProcessSpec(argv=argv, uid=0, gid=0, cwd="/", env={}, timeout_s=timeout_s)


async def launch(platform: Platform, handle: InstanceHandle, service: str, unit: Unit) -> str:
    """Start the main process, appending stdout and stderr to `/run/flotilla/<service>.log`. Return the process ID.

    The log file is first created by root and handed to the service uid / gid; the process only appends as itself.
    """
    assert unit.command is not None
    path = log_path(service, readonly_rootfs=unit.readonly_rootfs)
    ctx = unit.context
    directory = RUNTIME_MOUNT if unit.readonly_rootfs else LOG_DIR
    prep = f"mkdir -p {directory} && : > {shlex.quote(path)} && chown {ctx.uid}:{ctx.gid} {shlex.quote(path)}"
    try:
        result = await platform.exec(handle, own_spec((BUSYBOX, "sh", "-c", prep)))
    except FlotillaError as exc:
        raise _infra(service, exc) from exc
    if result.exit_code != 0 or result.timed_out:
        raise FlotillaError(
            f"{service}: failed to prepare log file: {_stderr(result)}",
            stage="start",
            category=ErrorCategory.TRANSIENT,
            retryable=True,
            service=service,
        )
    # The log path is passed positionally, not via env: the main process env is exactly the context.
    wrapper = (BUSYBOX, "sh", "-c", 'log=$1; shift; exec "$@" >>"$log" 2>&1', "flotilla", path, *unit.command)
    try:
        return await platform.start_process(handle, service_spec(unit, wrapper, timeout_s=None))
    except FlotillaError as exc:
        raise _infra(service, exc) from exc


async def tail(
    platform: Platform, handle: InstanceHandle, service: str, nbytes: int = TAIL_BYTES, *, readonly_rootfs: bool = False
) -> str:
    """Tail of the service log; empty if unreadable (diagnostics only; must not mask the original error)."""
    try:
        result = await platform.exec(
            handle, own_spec((BUSYBOX, "tail", "-c", str(nbytes), log_path(service, readonly_rootfs=readonly_rootfs)))
        )
    except FlotillaError:
        return ""
    return result.stdout.decode(errors="replace") if result.exit_code == 0 else ""


async def wait_healthy(
    platform: Platform,
    clock: Clock,
    handle: InstanceHandle,
    service: str,
    unit: Unit,
    pid: str,
    *,
    started_at: float,
) -> None:
    """Run healthchecks with Docker semantics until the first healthy result. Raise a `service` error on unhealthy.

    - The first check runs one interval after start; `start_interval` within `start_period`, `interval` after;
    - exit code 0 is healthy; timeout or non-zero is a failure; failures within `start_period` don't count to `retries`;
    - transient exec channel errors are not check results; that round is skipped;
    - after each check with a result, the process is checked: if it exited, fail immediately. In Docker the container
      exits with its main process, so failures don't wait out retries and success doesn't count (checks like
      `test -f /ready` can still pass after the process exits, while the placeholder entrypoint keeps the sandbox up).
    """
    hc = unit.healthcheck
    assert hc is not None
    failures = 0
    while True:
        await clock.sleep(_interval(hc, clock.monotonic() - started_at))
        in_start_period = clock.monotonic() - started_at < hc.start_period_s
        result = await _probe(platform, handle, service, unit, hc)
        if result is None:
            continue
        exit_code = await _exited(platform, handle, service, pid)
        if exit_code is not None:
            raise await _service_error(
                platform,
                handle,
                service,
                f"process exited during healthcheck (exit code {exit_code})",
                readonly_rootfs=unit.readonly_rootfs,
            )
        if result.exit_code == 0 and not result.timed_out:
            return
        if not in_start_period:
            failures += 1
        if failures >= hc.retries:
            why = "timeout" if result.timed_out else f"exit code {result.exit_code}"
            raise await _service_error(
                platform,
                handle,
                service,
                f"healthcheck failed {failures} consecutive times (last: {why})",
                readonly_rootfs=unit.readonly_rootfs,
            )


async def wait_exit(
    platform: Platform,
    clock: Clock,
    handle: InstanceHandle,
    service: str,
    pid: str,
    *,
    poll_interval_s: float,
) -> int:
    """Poll `process_status` every `poll_interval_s` and return the exit code (exit detection during start)."""
    while True:
        exit_code = await _exited(platform, handle, service, pid)
        if exit_code is not None:
            return exit_code
        await clock.sleep(poll_interval_s)


def _interval(hc: Healthcheck, elapsed: float) -> float:
    return hc.start_interval_s if elapsed < hc.start_period_s else hc.interval_s


async def _probe(
    platform: Platform, handle: InstanceHandle, service: str, unit: Unit, hc: Healthcheck
) -> ExecResult | None:
    try:
        return await platform.exec(handle, service_spec(unit, hc.test, timeout_s=hc.timeout_s))
    except FlotillaError as exc:
        if exc.category in (ErrorCategory.TRANSIENT, ErrorCategory.RATE_LIMITED):
            log.warning("%s: healthcheck exec channel error, skipping this round: %s", service, exc)
            return None
        raise _infra(service, exc) from exc


async def _exited(platform: Platform, handle: InstanceHandle, service: str, pid: str) -> int | None:
    """Return the exit code if exited, None if running. A missing process (exec daemon lost it) is an infra failure."""
    try:
        status = await platform.process_status(handle, pid)
    except FlotillaError as exc:
        if exc.category in (ErrorCategory.TRANSIENT, ErrorCategory.RATE_LIMITED):
            return None
        raise _infra(service, exc) from exc
    if not status.found:
        raise FlotillaError(
            f"{service}: main process {pid} not found (exec daemon may have restarted)",
            stage="start",
            category=ErrorCategory.TRANSIENT,
            retryable=True,
            service=service,
        )
    if status.running:
        return None
    return status.exit_code if status.exit_code is not None else -1


def _infra(service: str, exc: FlotillaError) -> FlotillaError:
    """Platform errors during start: a vanished instance is retryable; others keep their category."""
    if exc.category is ErrorCategory.NOT_FOUND:
        return FlotillaError(
            f"{service}: instance vanished during start: {exc}",
            stage="start",
            category=ErrorCategory.TRANSIENT,
            retryable=True,
            service=service,
        )
    return FlotillaError(str(exc), stage="start", category=exc.category, retryable=exc.retryable, service=service)


async def _service_error(
    platform: Platform, handle: InstanceHandle, service: str, why: str, *, readonly_rootfs: bool = False
) -> FlotillaError:
    logs = await tail(platform, handle, service, readonly_rootfs=readonly_rootfs)
    message = f"{service}: {why}" + (f"\n--- log tail ---\n{logs}" if logs else "")
    return FlotillaError(message, stage="start", category=ErrorCategory.SERVICE, retryable=False, service=service)


def _stderr(result: ExecResult) -> str:
    return result.stderr.decode(errors="replace").strip()[-500:]


@dataclass(frozen=True)
class Started:
    """Result of the start stage: process IDs per service (excluding those without one) and exit codes of finished
    one-shot services."""

    pids: Mapping[str, str]
    exit_codes: Mapping[str, int]


async def start_all(
    platform: Platform,
    clock: Clock,
    plan: TrialPlan,
    handles: Mapping[str, InstanceHandle],
    *,
    poll_interval_s: float = 2.0,
) -> Started:
    """Start all services gated by depends_on and wait until ready (see module docs). Any failure cancels the rest."""
    runner = _Runner(platform, clock, plan, handles, poll_interval_s)
    return await runner.run()


class _Gate:
    """One-shot result gate: after `open()` or `fail(exc)`, `wait()` returns or raises that exception."""

    def __init__(self) -> None:
        self._event = asyncio.Event()
        self._error: FlotillaError | None = None

    def open(self) -> None:
        self._event.set()

    def fail(self, exc: FlotillaError) -> None:
        self._error = exc
        self._event.set()

    async def wait(self) -> None:
        await self._event.wait()
        if self._error is not None:
            raise self._error


class _Runner:
    def __init__(
        self,
        platform: Platform,
        clock: Clock,
        plan: TrialPlan,
        handles: Mapping[str, InstanceHandle],
        poll_interval_s: float,
    ) -> None:
        self._platform = platform
        self._clock = clock
        self._plan = plan
        self._handles = handles
        self._poll = poll_interval_s
        # Three gates per service: started, healthy, exited with 0.
        self._gates: dict[tuple[str, Condition], _Gate] = {
            (u, c): _Gate()
            for u in plan.units
            for c in ("service_started", "service_healthy", "service_completed_successfully")
        }
        awaited = {(dep, d.condition) for deps in plan.depends_on.values() for dep, d in deps.items()}
        self._wait_exit = {u for u, c in awaited if c == "service_completed_successfully"}
        self._pids: dict[str, str] = {}
        self._exit_codes: dict[str, int] = {}

    async def run(self) -> Started:
        await run_all(self._service(u) for u in sorted(self._plan.units))
        return Started(pids=dict(self._pids), exit_codes=dict(self._exit_codes))

    async def _service(self, name: str) -> None:
        unit = self._plan.units[name]
        if not await self._wait_dependencies(name):
            return  # a required dep failed: its task reports the original error; dependents neither start nor report
        if unit.command is None:  # standalone sandbox driven only by client exec: no main process
            self._gates[(name, "service_started")].open()
            return
        handle = self._handles[name]
        pid = await launch(self._platform, handle, name, unit)
        started_at = self._clock.monotonic()
        self._pids[name] = pid
        self._gates[(name, "service_started")].open()
        if name in self._wait_exit:
            # For service_completed_successfully deps Compose only waits for exit, not health (so does up --wait).
            await self._completion(name, pid)
        elif unit.healthcheck is not None:
            await self._health(name, unit, pid, started_at)

    async def _wait_dependencies(self, name: str) -> bool:
        """Wait for dependency conditions. False if a required dependency failed (the dependent must not start)."""
        for dep, d in sorted(self._plan.depends_on.get(name, {}).items()):
            if dep not in self._plan.units:
                continue  # validate() ensures required dependencies exist; this can only be an absent optional one
            try:
                await self._gates[(dep, d.condition)].wait()
            except FlotillaError:
                if d.required:
                    return False
                log.info("%s: optional dependency %s did not reach %s, not blocking", name, dep, d.condition)
        return True

    async def _health(self, name: str, unit: Unit, pid: str, started_at: float) -> None:
        gate = self._gates[(name, "service_healthy")]
        try:
            await wait_healthy(self._platform, self._clock, self._handles[name], name, unit, pid, started_at=started_at)
        except FlotillaError as exc:
            gate.fail(exc)
            raise
        gate.open()

    async def _completion(self, name: str, pid: str) -> None:
        gate = self._gates[(name, "service_completed_successfully")]
        handle = self._handles[name]
        try:
            code = await wait_exit(self._platform, self._clock, handle, name, pid, poll_interval_s=self._poll)
            self._exit_codes[name] = code
            if code != 0:
                raise await _service_error(self._platform, handle, name, f"one-shot service exit code {code}")
        except FlotillaError as exc:
            gate.fail(exc)
            raise
        gate.open()


def start_timeout(plan: TrialPlan, *, slack_s: float = 120.0, cap_s: float = 1800.0) -> float:
    """Start stage timeout: the longest sum of healthcheck durations along a dependency chain plus `slack_s`, capped
    at `cap_s`.

    A single healthcheck's maximum duration is estimated as `start_period + (retries + 1) * (interval + timeout)`.
    Readiness requires all healthchecks to pass, so take the longest chain over all services, not just dependencies.
    """
    longest: dict[str, float] = {}

    def chain(name: str) -> float:
        if name not in longest:
            hc = plan.units[name].healthcheck
            own = 0.0 if hc is None else hc.start_period_s + (hc.retries + 1) * (hc.interval_s + hc.timeout_s)
            deps = [d for d in plan.depends_on.get(name, {}) if d in plan.units]
            longest[name] = own + max((chain(d) for d in deps), default=0.0)
        return longest[name]

    return min(cap_s, max(chain(u) for u in plan.units) + slack_s)
