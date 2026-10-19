"""Preserve structured failure categories across lifecycle stages."""

from __future__ import annotations

from flotilla.platform.base import FlotillaError, Stage


def at_stage(exc: FlotillaError, stage: Stage) -> FlotillaError:
    """Move a platform error onto the trial's stage (backends do not know which stage a call belongs to),
    keeping category, retryability, and service name.

    Usage: `raise at_stage(exc, "create") from exc`. Returns the error unchanged when the stage already matches.
    """
    if exc.stage == stage:
        return exc
    return FlotillaError(str(exc), stage=stage, category=exc.category, retryable=exc.retryable, service=exc.service)
