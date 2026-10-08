"""真实时钟 `SystemClock`：`platform.base.Clock` 挂在系统时钟与 `asyncio.sleep` 上（Architecture §2.3"时钟"）。

编排核心不直接用挂钟（ruff banned-api 只约束 `flotilla.core`），由使用方组装时注入本实现；测试注入
`flotilla.platform.fake.ManualClock`。
"""

from __future__ import annotations

import asyncio
import time
from datetime import UTC, datetime


class SystemClock:
    """满足 `flotilla.platform.base.Clock`。`now()` 为带时区的 UTC 时间（写给平台的过期时间用它）。"""

    def now(self) -> datetime:
        return datetime.now(UTC)

    def monotonic(self) -> float:
        return time.monotonic()

    async def sleep(self, seconds: float) -> None:
        await asyncio.sleep(max(0.0, seconds))
