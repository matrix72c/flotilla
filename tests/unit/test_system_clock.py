"""`SystemClock`: satisfies the `Clock` protocol, `now()` is timezone-aware, `sleep` accepts non-positive values."""

from __future__ import annotations

from datetime import timedelta

import pytest

from flotilla.platform.base import Clock
from flotilla.platform.clock import SystemClock


def test_satisfies_clock_protocol() -> None:
    assert isinstance(SystemClock(), Clock)


def test_now_is_timezone_aware_utc() -> None:
    now = SystemClock().now()
    assert now.utcoffset() == timedelta(0)  # a naive datetime's utcoffset() is None


@pytest.mark.asyncio
async def test_sleep_advances_monotonic_and_tolerates_non_positive() -> None:
    clock = SystemClock()
    before = clock.monotonic()
    await clock.sleep(-1.0)  # same as ManualClock: non-positive just yields once
    await clock.sleep(0.01)
    assert clock.monotonic() - before >= 0.01
