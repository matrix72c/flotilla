"""Own instance creation, reconcile uncertainty, and retain capacity until cleanup."""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass, field
from typing import Literal

from flotilla.core import labels
from flotilla.core.anchor import Anchor
from flotilla.core.deletion import Deleter, DeleteScope
from flotilla.core.errors import at_stage
from flotilla.core.volumes import launch_key
from flotilla.core.waiting import Aborted, Backoff, RateLimiter, step_towards, wait_all, within
from flotilla.platform.base import (
    Clock,
    ErrorCategory,
    FlotillaError,
    InstanceHandle,
    InstanceSpec,
    Platform,
    Stage,
)

log = logging.getLogger(__name__)


@dataclass(frozen=True)
class ReaperSettings:
    """The parts of `[limits]` and `[timeouts]` used by the reaper."""

    create_rate: float = 5.0  # per second, process-wide
    create_concurrency: int = 64
    create_request_timeout_s: float = 60.0  # per create request; exceeding it is treated as an unknown outcome
    close_timeout_s: float = 20.0

    def __post_init__(self) -> None:
        if self.create_rate <= 0 or self.create_concurrency < 1:
            raise ValueError("create_rate must be > 0 and create_concurrency must be >= 1")


@dataclass
class _Trial:
    trial_id: str
    cleanup: DeleteScope
    released: asyncio.Event = field(default_factory=asyncio.Event)
    known: set[str] = field(default_factory=set)  # recorded instance IDs (including deleted ones)
    inflight: set[asyncio.Task[InstanceHandle]] = field(default_factory=set)
    # After a create with unknown outcome: the time (monotonic) after which label listing shows no new instances;
    # None means all creates had definite outcomes.
    list_until: float | None = None
    dirs: list[str] = field(default_factory=list)  # volume keys of shared volume directories
    reconciled: bool = False
    done: asyncio.Event = field(default_factory=asyncio.Event)
    error: str | None = None  # last directory-removal or reaping error

    def labels(self, launch_id: str) -> dict[str, str]:
        return {labels.LAUNCH: launch_id, labels.TRIAL: self.trial_id}


class Reaper:
    """Reaper of a launch. `anchor` must be started; the reaper removes it on `close`. `deleter` may be shared (gc)."""

    def __init__(
        self,
        platform: Platform,
        clock: Clock,
        anchor: Anchor,
        deleter: Deleter,
        settings: ReaperSettings,
    ) -> None:
        self._platform = platform
        self._clock = clock
        self._anchor = anchor
        self._deleter = deleter
        self._settings = settings
        self._create_slots = asyncio.Semaphore(settings.create_concurrency)
        self._create_rate = RateLimiter(clock, settings.create_rate)
        self._trials: dict[str, _Trial] = {}
        self._reaping: set[asyncio.Task[None]] = set()
        self._failed: set[str] = set()
        self._closing = False
        self.completed: dict[str, CleanupRecord] = {}

    @property
    def launch_id(self) -> str:
        return self._anchor.launch_id

    def track(self, trial_id: str) -> None:
        """Start tracking `trial_id`. Must be followed by `release`."""
        if self._closing:
            raise RuntimeError("reaper is closed")
        if trial_id in self._trials:
            raise ValueError(f"trial {trial_id} is already tracked")
        self._trials[trial_id] = _Trial(trial_id, self._deleter.scope())

    def add_dir(self, trial_id: str, key: str) -> None:
        """Register a trial shared volume dir. Register **before** `mkdir` so half-created dirs get removed too."""
        self._open(trial_id, "prepare").dirs.append(key)

    async def create(self, trial_id: str, unit: str, spec: InstanceSpec) -> InstanceHandle:
        """Create an instance for the trial's `unit` (see module docs). `spec.labels` needs launch/trial/unit labels."""
        trial = self._open(trial_id, "create")
        expected = {labels.LAUNCH: self.launch_id, labels.TRIAL: trial_id, labels.UNIT: unit}
        if any(spec.labels.get(k) != v for k, v in expected.items()):
            raise ValueError(f"spec.labels must contain {expected}, got {dict(spec.labels)}")
        task = asyncio.create_task(self._create(trial, unit, spec))
        trial.inflight.add(task)
        task.add_done_callback(_retrieve)
        task.add_done_callback(trial.inflight.discard)
        return await asyncio.shield(task)

    def discard(self, trial_id: str, iid: str) -> None:
        """Delete a trial instance in the background now, without waiting for release (old instance on rebuild)."""
        trial = self._trials.get(trial_id)
        if trial is None:
            raise ValueError(f"trial {trial_id} is not tracked")
        trial.known.add(iid)
        trial.cleanup.start(iid)

    def release(self, trial_id: str) -> None:
        """Hand the trial to the reaper and return immediately without awaiting platform calls. Idempotent."""
        trial = self._trials.get(trial_id)
        if trial is None or trial.released.is_set():
            return
        trial.released.set()  # queued creates are abandoned
        task = asyncio.create_task(self._reap(trial))
        self._reaping.add(task)
        task.add_done_callback(self._reaping.discard)

    def is_tracking(self, trial_id: str) -> bool:
        """Whether the trial is still tracked (not released, or released but not fully reaped)."""
        return trial_id in self._trials

    def retry(self, trial_id: str) -> None:
        """Retry a completed cleanup using only this launch's retained ownership record."""
        if self._closing:
            raise RuntimeError("reaper is closed")
        if trial_id in self._trials:
            return
        record = self.completed[trial_id]
        if record.clean:
            return
        trial = _Trial(trial_id, self._deleter.scope())
        trial.known.update(record.instances)
        trial.dirs.extend(record.directories)
        if record.uncertain:
            trial.list_until = self._clock.monotonic() + self._platform.caps.list_visibility_s
        self._trials[trial_id] = trial
        self.completed.pop(trial_id)
        self.release(trial_id)

    def snapshot(self, trial_id: str) -> CleanupRecord:
        if trial_id in self.completed:
            return self.completed[trial_id]
        trial = self._trials[trial_id]
        return self._record(trial, finished=False)

    async def wait(self, trial_id: str) -> CleanupRecord:
        """Wait for this trial's reaping attempt, without polling or cancelling the owned cleanup."""
        trial = self._trials.get(trial_id)
        if trial is not None:
            await trial.done.wait()
        return self.completed[trial_id]

    def _record(self, trial: _Trial, *, finished: bool) -> CleanupRecord:
        pending = (trial.known - trial.cleanup.gone()) | trial.cleanup.pending()
        unknown = trial.list_until is not None and not trial.reconciled
        if not finished:
            reason: CleanupReason | None = "in_progress"
        elif trial.inflight:
            reason = "create_in_flight"
        elif pending:
            reason = "delete_unconfirmed"
        elif unknown:
            reason = "create_outcome_unknown"
        elif trial.dirs:
            reason = "anchor_deactivated" if self._anchor.deactivated.is_set() else "directory_remove_failed"
        else:
            reason = None
        error = trial.cleanup.last_error() if reason == "delete_unconfirmed" else trial.error
        return CleanupRecord(
            trial.trial_id,
            tuple(sorted(pending)),
            tuple(trial.dirs),
            bool(trial.inflight) or unknown,
            finished,
            reason,
            error if reason is not None else None,
        )

    async def close(self) -> bool:
        """Release all trials and wait for the reaping queue to drain (at most `close_timeout_s`).

        If drained and all instances of this launch are confirmed deleted, remove `launches/<launch_id>` via the
        anchor, then the anchor, and return True; otherwise cancel remaining reaping, remove only the anchor, leave
        the rest to gc in other processes, and return False.
        """
        self._closing = True
        for trial_id in list(self._trials):
            self.release(trial_id)
        drained = await wait_all(self._clock, set(self._reaping), self._settings.close_timeout_s)
        clean = drained and not self._failed and not self._anchor.deactivated.is_set()
        if not drained:
            # Cancel in-flight creates too: the platform may have accepted them; leftovers carry launch labels for gc.
            leftover = set(self._reaping) | {t for trial in self._trials.values() for t in trial.inflight}
            for task in leftover:
                task.cancel()
            await asyncio.wait(leftover)
        if clean:
            try:
                await self._anchor.remove(launch_key(self.launch_id), stage="run")
            except FlotillaError as exc:
                log.warning("failed to remove launch directory, leaving it to gc: %s", exc)
                clean = False
        await self._anchor.close()
        return clean

    def _open(self, trial_id: str, stage: Stage) -> _Trial:
        trial = self._trials.get(trial_id)
        if trial is None:
            raise ValueError(f"trial {trial_id} is not tracked")
        if trial.released.is_set():
            raise self._not_sent(trial, stage)
        return trial

    def _not_sent(self, trial: _Trial, stage: Stage) -> FlotillaError:
        """Error for a request abandoned before sending: `lost` if the launch is deactivated; retryable if the trial was
        released (the caller no longer waits for it)."""
        if self._anchor.deactivated.is_set():
            return self._anchor.lost(stage)
        return FlotillaError(
            f"trial {trial.trial_id} was released, request not sent",
            stage=stage,
            category=ErrorCategory.TRANSIENT,
            retryable=True,
        )

    async def _admit(self) -> None:
        """Acquire a create slot and wait for the rate limiter. Returns holding the slot; the caller releases it."""
        await self._create_slots.acquire()
        try:
            await self._create_rate.acquire()
        except BaseException:
            self._create_slots.release()
            raise

    async def _create(self, trial: _Trial, unit: str, spec: InstanceSpec) -> InstanceHandle:
        try:
            await within(self._clock, None, self._admit(), abort=(trial.released, self._anchor.deactivated))
        except Aborted:
            raise self._not_sent(trial, "create") from None
        try:
            if trial.released.is_set():
                raise self._not_sent(trial, "create")
            self._anchor.ensure_alive("create")
            try:
                handle = await within(
                    self._clock,
                    self._settings.create_request_timeout_s,
                    self._platform.create(spec),
                    abort=(self._anchor.deactivated,),
                )
            except Aborted:
                self._uncertain(trial)
                raise self._anchor.lost("create") from None
            except asyncio.CancelledError:
                self._uncertain(trial)
                raise
            except Exception as exc:
                ended = self._uncertain(trial)
                if isinstance(exc, FlotillaError) and exc.category is not ErrorCategory.TRANSIENT:
                    raise at_stage(
                        exc, "create"
                    ) from exc  # the platform reported a definite failure: do not recover it as success
                failure = (
                    at_stage(exc, "create")
                    if isinstance(exc, FlotillaError)
                    else FlotillaError(
                        f"create request outcome unknown: {exc!r}",
                        stage="create",
                        category=ErrorCategory.TRANSIENT,
                        retryable=True,
                    )
                )
            else:
                trial.known.add(handle.iid)
                return handle
        finally:
            self._create_slots.release()
        # Reconciliation does not hold a create slot.
        found = await self._reconcile(trial, unit, deadline=ended)
        if found is None:
            raise failure
        log.info(
            "trial %s unit %s create outcome unknown, reconciliation found instance %s", trial.trial_id, unit, found
        )
        return InstanceHandle(iid=found)

    def _uncertain(self, trial: _Trial) -> float:
        """Record a create with unknown outcome (call just ended). Return when listing can no longer reveal it."""
        until = self._clock.monotonic() + self._platform.caps.list_visibility_s
        trial.list_until = until if trial.list_until is None else max(trial.list_until, until)
        return until

    async def _reconcile(self, trial: _Trial, unit: str, *, deadline: float) -> str | None:
        """List by labels until an unrecorded instance of the unit appears (return its ID) or `deadline` passes (None).

        Also return None if no listing succeeded by the deadline: the caller gets a retryable error, and any instance
        is removed by the sweep on release.
        """
        query = {**trial.labels(self.launch_id), labels.UNIT: unit}
        backoff = Backoff(1.0, cap=8.0)
        while True:
            issued = self._clock.monotonic()
            try:
                listed = await self._platform.list(query)
            except FlotillaError as exc:
                log.warning("trial %s reconciliation list failed: %s", trial.trial_id, exc)
                if not exc.retryable:
                    return None
            else:
                fresh = sorted(st.iid for st in listed if st.iid not in trial.known)
                if fresh:
                    trial.known.update(fresh)  # extras (unexpected) are recorded too and deleted on release
                    return fresh[0]
            if issued >= deadline:
                return None
            await self._clock.sleep(step_towards(self._clock.monotonic(), deadline, backoff.next()))

    async def _reap(self, trial: _Trial) -> None:
        try:
            if trial.inflight:
                await asyncio.wait(set(trial.inflight))
            complete = await trial.cleanup.delete_all(trial.known)
            if complete and trial.list_until is not None:
                complete = await trial.cleanup.sweep(trial.labels(self.launch_id), until=trial.list_until)
                trial.reconciled = complete
            if not complete:
                log.error(
                    "trial %s has instances not confirmed deleted, leaving them to gc and platform TTL", trial.trial_id
                )
            elif trial.dirs:
                await self._remove_dirs(trial)
            if complete:
                self._platform.forget_creates(trial.labels(self.launch_id))
        except asyncio.CancelledError:
            trial.cleanup.cancel()
            raise
        except Exception as exc:
            trial.error = _brief(exc)
            log.exception("error reaping trial %s", trial.trial_id)
        finally:
            record = self._record(trial, finished=True)
            self.completed[trial.trial_id] = record
            if record.clean:
                self._failed.discard(trial.trial_id)
            else:
                self._failed.add(trial.trial_id)
            self._trials.pop(trial.trial_id, None)
            trial.done.set()

    async def _remove_dirs(self, trial: _Trial) -> None:
        """Once instances are gone, remove the trial's volume dirs via the anchor. Refused if deactivated (gc)."""
        for key in tuple(trial.dirs):
            try:
                await self._anchor.remove(key, stage="run")
                trial.dirs.remove(key)
            except FlotillaError as exc:
                trial.error = _brief(exc)
                log.warning("failed to remove trial %s directory %s, leaving it to gc: %s", trial.trial_id, key, exc)
                return


CleanupReason = Literal[
    "in_progress",
    "create_in_flight",
    "create_outcome_unknown",
    "delete_unconfirmed",
    "directory_remove_failed",
    "anchor_deactivated",
]


@dataclass(frozen=True)
class CleanupRecord:
    """A conservative deletion snapshot. Unknown creations and directories count as leftovers.

    `reason` names the first thing still unconfirmed (None when clean); `error` is the last related error, if any.
    """

    environment_id: str
    instances: tuple[str, ...] = ()
    directories: tuple[str, ...] = ()
    uncertain: bool = False
    finished: bool = False
    reason: CleanupReason | None = None
    error: str | None = None

    @property
    def clean(self) -> bool:
        return self.finished and not (self.instances or self.directories or self.uncertain)


def _brief(exc: BaseException, limit: int = 300) -> str:
    text = f"{type(exc).__name__}: {exc}"
    return text if len(text) <= limit else text[: limit - 3] + "..."


def _retrieve(task: asyncio.Task[object]) -> None:
    """The reaper handles or logs background task exceptions; retrieve them so asyncio won't say "never retrieved"."""
    if not task.cancelled():
        task.exception()
