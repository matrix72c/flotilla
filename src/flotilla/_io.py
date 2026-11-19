"""File work must settle before its owner removes the working directory."""

from __future__ import annotations

import asyncio
import contextlib
from collections.abc import Callable
from typing import Any

from flotilla.core.waiting import finish_past_cancel


async def owned_io[T](function: Callable[..., T], *args: Any, **kwargs: Any) -> T:
    work = asyncio.create_task(asyncio.to_thread(function, *args, **kwargs))
    try:
        return await asyncio.shield(work)
    except asyncio.CancelledError:
        # The work's own outcome is retrieved, not reported; the caller sees only the cancellation.
        with contextlib.suppress(BaseException):
            await finish_past_cancel(work)
        raise
