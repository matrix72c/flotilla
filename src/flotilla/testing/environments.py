"""Open prepared fixture environments for public lifecycle contracts."""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass, field

from flotilla import Environment, Flotilla, OpenOptions
from flotilla.runtime.types import Task


@dataclass
class FixtureRequest:
    task: Task
    request_id: str
    timeout: float | None = None
    runtime_params: dict[str, str] = field(default_factory=dict)
    exec_env: dict[str, dict[str, str]] = field(default_factory=dict)
    disable_internet: bool = False


def options(request: FixtureRequest) -> OpenOptions:
    return OpenOptions(
        request_id=request.request_id,
        admission_timeout=request.timeout,
        startup_timeout=request.timeout,
        lifetime=request.timeout,
        runtime_params=request.runtime_params,
        exec_env=request.exec_env,
        disable_internet=request.disable_internet,
    )


async def open_fixture(fleet: Flotilla, request: FixtureRequest) -> Environment:
    return await fleet.open(request.task, options(request))


@asynccontextmanager
async def environment(fleet: Flotilla, request: FixtureRequest) -> AsyncIterator[Environment]:
    async with fleet.environment(request.task, options(request)) as env:
        yield env
