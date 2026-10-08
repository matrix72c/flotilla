"""`SystemClock`：满足 `Clock` 协议、`now()` 带时区、`sleep` 接受非正数。"""

from __future__ import annotations

from datetime import timedelta

import pytest

from flotilla.platform.base import Clock
from flotilla.platform.clock import SystemClock


def test_satisfies_clock_protocol() -> None:
    assert isinstance(SystemClock(), Clock)


def test_now_is_timezone_aware_utc() -> None:
    now = SystemClock().now()
    assert now.utcoffset() == timedelta(0)  # naive datetime 的 utcoffset() 为 None


@pytest.mark.asyncio
async def test_sleep_advances_monotonic_and_tolerates_non_positive() -> None:
    clock = SystemClock()
    before = clock.monotonic()
    await clock.sleep(-1.0)  # 与 ManualClock 一致：非正数只让出一次
    await clock.sleep(0.01)
    assert clock.monotonic() - before >= 0.01
