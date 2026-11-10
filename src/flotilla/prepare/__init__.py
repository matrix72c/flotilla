"""Public host selection/preparation API and explicitly named legacy warmup."""

from __future__ import annotations

from collections.abc import Sequence
from pathlib import Path
from typing import Any

from flotilla.config import FlotillaConfig
from flotilla.integrations.native_import import SelectedTask
from flotilla.platform.base import FlotillaError
from flotilla.prepare.release import PreparationError, PrepareResult, SelectionResult, prepare, select

__all__ = ["PreparationError", "PrepareResult", "SelectedTask", "SelectionResult", "legacy_warmup", "prepare", "select"]


async def legacy_warmup(tasks: Sequence[str], *, deployment: str | Path | FlotillaConfig) -> list[dict[str, Any]]:
    """Warm original Harbor task keys in independent temporary workspaces.

    Preserve input order and repetitions. Returned records are diagnostics, not
    portable environment manifests. Use HarborAttempt for runtime ownership.
    """
    from flotilla.runtime.client import Flotilla

    results: list[dict[str, Any]] = []
    async with Flotilla(deployment=deployment) as fleet:
        for key in tasks:
            try:
                async with await fleet.workspace(key) as workspace:
                    result = await workspace.prepare()
                    results.append(
                        {
                            "task_key": key,
                            "id": workspace.task_id,
                            "status": "ready",
                            "roles": list(result["environments"]),
                        }
                    )
            except (ValueError, OSError, RuntimeError, FlotillaError) as exc:
                results.append({"task_key": key, "status": "failed", "error": str(exc)})
    return results
