"""File work must settle before its owner removes the working directory."""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from concurrent.futures import Executor
from functools import partial
from typing import Any


async def owned_io[T](function: Callable[..., T], *args: Any, executor: Executor | None = None, **kwargs: Any) -> T:
    work = (
        asyncio.create_task(asyncio.to_thread(function, *args, **kwargs))
        if executor is None
        else asyncio.get_running_loop().run_in_executor(executor, partial(function, *args, **kwargs))
    )
    try:
        return await asyncio.shield(work)
    except asyncio.CancelledError:
        while not work.done():
            try:
                await asyncio.shield(work)
            except asyncio.CancelledError:
                continue
            except Exception:
                break
        if not work.cancelled():
            work.exception()
        raise
