"""Bounded confirmation that an identified remote command has stopped."""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from contextlib import suppress

from flotilla.platform.base import Clock, FlotillaError, ProcessStatus


async def confirm_stopped(
    clock: Clock,
    command_id: str | None,
    interrupt: Callable[[str], Awaitable[None]],
    status: Callable[[str], Awaitable[ProcessStatus]],
    timeout: float,
) -> bool:
    if command_id is None:
        return False

    async def confirm() -> bool:
        # Interrupt can race with normal exit; the status record is authoritative.
        with suppress(FlotillaError):
            await interrupt(command_id)
        while True:
            result = await status(command_id)
            if not result.found:
                return False
            if not result.running:
                return True
            await clock.sleep(0.1)

    work = asyncio.create_task(confirm())
    timer = asyncio.create_task(clock.sleep(timeout))
    try:
        await asyncio.wait({work, timer}, return_when=asyncio.FIRST_COMPLETED)
        return work.done() and work.result()
    except FlotillaError:
        return False
    finally:
        work.cancel()
        timer.cancel()
        await asyncio.gather(work, timer, return_exceptions=True)
