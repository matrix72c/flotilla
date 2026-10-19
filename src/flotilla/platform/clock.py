"""System clock implementation for production backends."""

from __future__ import annotations

import asyncio
import time
from datetime import UTC, datetime


class SystemClock:
    """Satisfies `flotilla.platform.base.Clock`. `now()` is aware UTC (used for expiry times sent to the platform)."""

    def now(self) -> datetime:
        return datetime.now(UTC)

    def monotonic(self) -> float:
        return time.monotonic()

    async def sleep(self, seconds: float) -> None:
        await asyncio.sleep(max(0.0, seconds))
