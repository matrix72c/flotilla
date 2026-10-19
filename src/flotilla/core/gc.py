"""Collect abandoned launches using bounded, independent observations."""

from __future__ import annotations

import logging
from dataclasses import dataclass

from flotilla.core import labels
from flotilla.core.anchor import Anchor
from flotilla.core.deletion import Deleter
from flotilla.core.volumes import LAUNCHES, launch_key
from flotilla.platform.base import Clock, ErrorCategory, FlotillaError, Platform

log = logging.getLogger(__name__)


@dataclass(frozen=True)
class GcReport:
    """Result of one gc round, for logs and CLI output."""

    alive: frozenset[str]  # launches with an anchor
    candidates: frozenset[str]  # candidates not yet past W
    removed: frozenset[str]  # instances gone, directory removed
    pending: frozenset[str]  # past W but not fully deleted; retried next round


class Gc:
    """Per-process gc state: candidates and when each was first observed. The operator explicitly calls `run_once`.

    `anchor` is this process's anchor: it performs directory operations, and its launch is never a candidate.
    `window_s` is `W`.
    """

    def __init__(self, platform: Platform, clock: Clock, anchor: Anchor, deleter: Deleter, *, window_s: float) -> None:
        if window_s <= 0:
            raise ValueError("window_s must be > 0")
        self._platform = platform
        self._clock = clock
        self._anchor = anchor
        self._deleter = deleter
        self._window_s = window_s
        self._first_seen: dict[str, float] = {}

    async def run_once(self) -> GcReport:
        """Run one gc round. Raises and abandons the round if listing anchors or directories fails: "cannot list"
        must never be taken as "no anchor"."""
        observed = self._clock.monotonic()
        alive = await self._alive_launches()
        dirs = set(await self._anchor.listdir(LAUNCHES, stage="run"))
        orphans = dirs - alive - {self._anchor.launch_id}

        # Stop tracking candidates that revived (anchor reappeared) or whose directory vanished; record new ones.
        for launch in list(self._first_seen):
            if launch not in orphans:
                del self._first_seen[launch]
        for launch in orphans:
            self._first_seen.setdefault(launch, observed)

        due = {launch for launch in orphans if observed - self._first_seen[launch] >= self._window_s}
        removed: set[str] = set()
        for launch in sorted(due):
            if await self._remove_launch(launch):
                removed.add(launch)
                del self._first_seen[launch]
        report = GcReport(
            alive=frozenset(alive),
            candidates=frozenset(orphans - due),
            removed=frozenset(removed),
            pending=frozenset(due - removed),
        )
        if report.removed or report.pending:
            log.info("gc: removed %s, pending %s", sorted(report.removed), sorted(report.pending))
        return report

    async def collect(self, launch: str) -> bool:
        """CLI `flotilla gc --launch <id>`: handle only the given launch, skipping the two-observation wait.

        The operator confirms the launch's process has exited; refused while its anchor is still listed (an anchor
        expires within one anchor TTL after its process exits). Without two observations, creates sent before the
        process exited may not be listed yet: repeatedly list by label and delete until a listing after
        `list_visibility_s` is empty. Returns whether all are gone.
        """
        if launch == self._anchor.launch_id:
            raise ValueError("cannot collect this process's own launch")
        if launch in await self._alive_launches():
            raise FlotillaError(
                f"launch {launch} still has an anchor; refusing to collect (process may be alive; retry later)",
                stage="run",
                category=ErrorCategory.INVALID,
                retryable=False,
            )
        return await self._remove_launch(launch, until=self._clock.monotonic() + self._platform.caps.list_visibility_s)

    async def _alive_launches(self) -> set[str]:
        anchors = await self._platform.list({labels.ROLE: labels.ROLE_ANCHOR})
        return {st.labels[labels.LAUNCH] for st in anchors if labels.LAUNCH in st.labels}

    async def _remove_launch(self, launch: str, *, until: float | None = None) -> bool:
        """Delete all of the launch's instances (empty listing after `until`), then its directory.
        Returns whether everything completed.

        `until` defaults to now: the second observation is past t0 + W, so the launch's creates are either listed
        or never will be, and one empty listing means all are gone.
        """
        try:
            scope = self._deleter.scope()
            deadline = self._clock.monotonic() if until is None else until
            if not await scope.sweep({labels.LAUNCH: launch}, until=deadline):
                log.warning("gc: instances of launch %s not fully deleted; keeping directory", launch)
                return False
            await self._anchor.remove(launch_key(launch), stage="run")
        except FlotillaError as exc:
            log.warning("gc: collecting launch %s failed, retrying next round: %s", launch, exc)
            return False
        return True
