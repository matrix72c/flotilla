"""Core task selection, independent of optional framework installations."""

from pathlib import Path

from flotilla.compose.task import HarborTask, load_task


def task_environments(path: Path) -> list[HarborTask]:
    return [load_task(path)]
