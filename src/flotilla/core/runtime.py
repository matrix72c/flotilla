"""Maintain environment health and renewable leases."""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime, timedelta

from flotilla.core import process
from flotilla.core.anchor import Anchor
from flotilla.core.plan import TrialPlan
from flotilla.core.waiting import Backoff, Expired, describe, run_all, within
from flotilla.platform.base import (
    Clock,
    ErrorCategory,
    FlotillaError,
    InstanceHandle,
    InstanceStatus,
    Platform,
)

log = logging.getLogger(__name__)


@dataclass(frozen=True)
class MonitorSettings:
    probe_interval_s: float = 300.0  # one get per unit
    renew_before_s: float = 1800.0  # renew when remaining TTL drops below this (30 minutes)
    ttl_s: int = 6 * 3600  # renewal sets expiry to now + ttl_s (capped at the deployment max_ttl_seconds)
    check_timeout_s: float = 10.0  # check() timeout
    renew_margin_s: float = 120.0  # on renewal failure retry with backoff; lost once remaining TTL drops below this

    def __post_init__(self) -> None:
        if self.probe_interval_s <= 0 or self.check_timeout_s <= 0:
            raise ValueError("probe_interval_s and check_timeout_s must be > 0")
        if not 0 < self.renew_margin_s < self.renew_before_s < self.ttl_s:
            raise ValueError("must satisfy 0 < renew_margin_s < renew_before_s < ttl_s")


class Monitor:
    """Runtime monitoring of a ready trial (see module docs)."""

    def __init__(
        self,
        platform: Platform,
        clock: Clock,
        anchor: Anchor,
        plan: TrialPlan,
        handles: Mapping[str, InstanceHandle],
        pids: Mapping[str, str],
        settings: MonitorSettings,
    ) -> None:
        self._platform = platform
        self._clock = clock
        self._anchor = anchor
        self._plan = plan
        self._handles = dict(handles)
        self._pids = dict(pids)
        self._settings = settings
        max_ttl = platform.caps.max_ttl_seconds
        self._ttl = min(settings.ttl_s, max_ttl) if max_ttl is not None else settings.ttl_s
        # The deployment TTL cap may be below renew_before_s: renew at most half a TTL early, or renewals would spin.
        self._renew_before = min(settings.renew_before_s, self._ttl / 2)
        self._renew_margin = min(settings.renew_margin_s, self._renew_before / 2)
        self._lost: FlotillaError | None = None
        self._tasks: list[asyncio.Task[None]] = []
        self._retired: set[str] = set()

    def retire(self, unit: str) -> None:
        """Stop monitoring a service deliberately terminated by its resource owner."""
        if unit not in self._handles:
            raise KeyError(unit)
        self._retired.add(unit)

    @property
    def lost(self) -> FlotillaError | None:
        """The error if the trial is marked `lost`; otherwise None."""
        return self._lost

    def start(self) -> None:
        if self._tasks:
            raise RuntimeError("monitor can only be started once")
        units = sorted(self._handles.keys() - self._retired)
        step = self._settings.probe_interval_s / max(1, len(units))
        for i, unit in enumerate(units):
            # Stagger: unit i first probes after (i + 1) * step so units in a trial don't probe at once.
            self._tasks.append(asyncio.create_task(self._probe_loop(unit, offset=(i + 1) * step)))
        self._tasks.append(asyncio.create_task(self._renew_loop()))
        self._tasks.append(asyncio.create_task(self._watch_launch()))

    async def stop(self) -> None:
        for task in self._tasks:
            task.cancel()
        if self._tasks:
            await asyncio.wait(self._tasks)

    def mark_lost(self, why: str, *, cause: BaseException | None = None) -> FlotillaError:
        """Mark `lost` (only the first time counts) and return the `lost` error."""
        if self._lost is None:
            log.error("trial marked lost: %s", why)
            err = FlotillaError(why, stage="run", category=ErrorCategory.LOST, retryable=True)
            if cause is not None:
                err.__cause__ = cause
            self._lost = err
        return self._lost

    async def confirm(self, unit: str) -> FlotillaError | None:
        """Confirm a unit right after exec returns `not_found`: mark `lost` if the instance is gone or terminated.
        Return the `lost` error or None."""
        if self._lost is not None:
            return self._lost
        await self._probe(unit)
        return self._lost

    async def check(self) -> BaseException | None:
        """Time-bounded health check; never raises. None if healthy, else an error with stage/category/retryable."""
        if self._lost is not None:
            return self._lost
        try:
            result = await within(self._clock, self._settings.check_timeout_s, self._check_with_retry())
        except Expired:
            return self.mark_lost(f"check() could not confirm health within {self._settings.check_timeout_s}s")
        except (
            FlotillaError
        ) as exc:  # the query itself failed: health unconfirmed, treat as lost (_check returns service errors)
            return self.mark_lost(f"check() failed: {exc}", cause=exc)
        return result

    async def _check_with_retry(self) -> FlotillaError | None:
        # These operations only read state or execute `true`; repeating them
        # cannot repeat a user's tool action. The enclosing deadline bounds
        # retries and still fails closed when health cannot be established.
        backoff = Backoff(1.0, cap=4.0)
        while True:
            try:
                return await self._check()
            except FlotillaError as exc:
                if exc.category not in (ErrorCategory.TRANSIENT, ErrorCategory.RATE_LIMITED):
                    raise
                log.warning("transient health probe failure, retrying within check budget: %s", exc)
                await self._clock.sleep(backoff.next())

    async def _check(self) -> FlotillaError | None:
        if self._anchor.deactivated.is_set():
            return self.mark_lost(f"launch {self._anchor.launch_id} was deactivated")
        units = sorted(self._handles)
        await run_all(self._check_unit(u) for u in units)
        if self._lost is not None:
            return self._lost
        for unit in units:
            if (err := await self._check_restart(unit)) is not None:
                return err
        return None

    async def _check_unit(self, unit: str) -> None:
        if unit in self._retired:
            return
        try:
            await self._check_active_unit(unit)
        except FlotillaError:
            if unit not in self._retired:
                raise

    async def _check_active_unit(self, unit: str) -> None:
        handle = self._handles[unit]
        state = await self._platform.get(handle.iid)
        if unit in self._retired:
            return
        if state.status is not InstanceStatus.RUNNING:
            self.mark_lost(f"unit {unit} is not running: {describe(state)}")
            return
        result = await self._platform.exec(handle, process.own_spec((process.BUSYBOX, "true"), timeout_s=5.0))
        if unit in self._retired:
            return
        if result.exit_code != 0 or result.timed_out:
            self.mark_lost(
                f"unit {unit} exec channel unavailable (no-op {'timed out' if result.timed_out else result.exit_code})"
            )
            return
        pid = self._pids.get(unit)
        if pid is not None:
            status = await self._platform.process_status(handle, pid)
            if not status.found and unit not in self._retired:
                self.mark_lost(f"unit {unit} main process {pid} not found (exec daemon may have restarted)")

    async def _check_restart(self, unit: str) -> FlotillaError | None:
        """If a service with a non-`no` restart policy exited and Docker would restart it, the sample is void."""
        policy = self._plan.units[unit].restart
        pid = self._pids.get(unit)
        if policy == "no" or pid is None or unit in self._retired:
            return None
        try:
            status = await self._platform.process_status(self._handles[unit], pid)
        except FlotillaError:
            if unit in self._retired:
                return None
            raise
        if status.running or unit in self._retired:
            return None
        code = status.exit_code
        if policy in ("always", "unless-stopped") or (policy == "on-failure" and code != 0):
            return FlotillaError(
                f"{unit}: service with restart={policy} exited (exit code {code}) and Docker would restart it; "
                "flotilla does not restart during a run, so the sample is void",
                stage="run",
                category=ErrorCategory.SERVICE,
                retryable=False,
                service=unit,
            )
        return None

    async def _probe_loop(self, unit: str, *, offset: float) -> None:
        await self._clock.sleep(offset)
        while self._lost is None and unit not in self._retired:
            await self._probe(unit)
            await self._clock.sleep(self._settings.probe_interval_s)

    async def _probe(self, unit: str) -> None:
        """One `get`: `lost` if the instance is gone or not `RUNNING` (`UNKNOWN` counts as terminated at runtime);
        transient query errors don't count."""
        if unit in self._retired:
            return
        try:
            state = await self._platform.get(self._handles[unit].iid)
        except FlotillaError as exc:
            if unit in self._retired:
                return
            if exc.category is ErrorCategory.NOT_FOUND:
                self.mark_lost(f"unit {unit} instance no longer exists", cause=exc)
            else:
                log.warning("failed to query unit %s, will retry next round: %s", unit, exc)
            return
        if unit not in self._retired and state.status is not InstanceStatus.RUNNING:
            self.mark_lost(f"unit {unit} status is {describe(state)}")

    async def _renew_loop(self) -> None:
        """Renew units with less than `renew_before_s` TTL left to now + ttl. Retry failures with backoff; still failing
        with less than `renew_margin_s` left means `lost`: the platform is about to reclaim the instance."""
        expires = await self._read_expiry()
        while self._lost is None:
            expires = {unit: expiry for unit, expiry in expires.items() if unit not in self._retired}
            if not expires:
                return
            soonest = min(expires.values())
            await self._clock.sleep(max(0.0, _left(soonest, self._clock.now()) - self._renew_before))
            for unit in sorted(u for u, at in expires.items() if _left(at, self._clock.now()) <= self._renew_before):
                renewed = await self._renew(unit, expires[unit])
                if renewed is None:
                    return
                expires[unit] = renewed

    async def _read_expiry(self) -> dict[str, datetime]:
        """Current expiry per unit. If unreadable (or not provided), use "now": renew at once; early is harmless, late
        loses instances."""
        out: dict[str, datetime] = {}
        for unit in sorted(self._handles):
            try:
                state = await self._platform.get(self._handles[unit].iid)
            except FlotillaError as exc:
                log.warning("failed to read expiry of unit %s, renewing now: %s", unit, exc)
                state = None
            out[unit] = state.expires_at if state is not None and state.expires_at is not None else self._clock.now()
        return out

    async def _renew(self, unit: str, current: datetime) -> datetime | None:
        backoff = Backoff(5.0, cap=60.0)
        while True:
            if unit in self._retired:
                return current
            new = self._clock.now() + timedelta(seconds=self._ttl)
            try:
                await self._platform.renew(self._handles[unit].iid, new)
                return new
            except FlotillaError as exc:
                if unit in self._retired:
                    return current
                left = _left(current, self._clock.now())
                if exc.category is ErrorCategory.NOT_FOUND or left <= self._renew_margin:
                    self.mark_lost(f"unit {unit} renewal failed ({left:.0f}s left)", cause=exc)
                    return None
                log.warning("unit %s renewal failed, retrying: %s", unit, exc)
            await self._clock.sleep(min(backoff.next(), max(0.0, left - self._renew_margin)))

    async def _watch_launch(self) -> None:
        await self._anchor.deactivated.wait()
        self.mark_lost(f"launch {self._anchor.launch_id} was deactivated")


def _left(expires_at: datetime, now: datetime) -> float:
    return (expires_at - now).total_seconds()
