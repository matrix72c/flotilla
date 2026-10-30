"""Arm A: task environments driven through flotilla (one sandbox per compose service)."""

from __future__ import annotations

import time
from contextlib import asynccontextmanager
from pathlib import Path
from typing import AsyncIterator

from flotilla import Environment, Flotilla, OpenOptions
from flotilla.config import FlotillaConfig

from loadtest.creds import bind_credential

from loadtest.events import EventLog


class FlotillaArm:
    """One Flotilla client per driver process; one environment per episode."""

    name = "flotilla"

    def __init__(self, deployment: Path, secret: Path, events: EventLog, *, startup_timeout: float = 900):
        self.config = FlotillaConfig.load(deployment)
        bind_credential(self.config, secret)
        self.events = events
        self.startup_timeout = startup_timeout
        self.fleet = Flotilla(deployment=self.config, on_event=self._on_event)

    def _on_event(self, event) -> None:
        self.events.runtime(event)

    @asynccontextmanager
    async def open(self, task: Path, request_id: str) -> AsyncIterator[Environment]:
        began = time.monotonic()
        async with self.fleet.environment(
            task, OpenOptions(request_id=request_id, startup_timeout=self.startup_timeout)
        ) as env:
            info = await env.info()
            self.events.emit(
                "ready",
                str(task),
                ready_s=round(time.monotonic() - began, 3),
                timings=dict(info.timings),
                services={s.name: s.instance_id for s in info.services},
                request_id=request_id,
            )
            yield env

    async def close(self) -> None:
        report = await self.fleet.close()
        self.events.emit(
            "fleet_close",
            clean=report.clean,
            remaining=[{"instances": list(r.instances), "reason": str(r.reason)} for r in report.remaining],
            anchor_instances=list(report.anchor_instances),
        )
