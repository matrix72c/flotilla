"""core 内的错误辅助。"""

from __future__ import annotations

from flotilla.platform.base import FlotillaError, Stage


def at_stage(exc: FlotillaError, stage: Stage) -> FlotillaError:
    """把平台错误换到 trial 的阶段上（后端不知道调用发生在哪个阶段），保留类别、可重试性与服务名。

    用法：`raise at_stage(exc, "create") from exc`。阶段已一致时原样返回。
    """
    if exc.stage == stage:
        return exc
    return FlotillaError(str(exc), stage=stage, category=exc.category, retryable=exc.retryable, service=exc.service)
