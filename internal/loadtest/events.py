"""Append-only loadtest event log (`loadtest-event-v1`).

One JSON object per line; the single data source for all arms. Flotilla RuntimeEvent
objects pass through verbatim under kind="runtime" (they carry environment_id/attempt_id,
so the report joins them to tasks by attempt, not by task name).
"""

from __future__ import annotations

import json
import os
import time
from dataclasses import asdict, is_dataclass
from pathlib import Path

SCHEMA = "loadtest-event-v1"


class EventLog:
    """Thread-safe enough for one asyncio loop + the observer's own process."""

    def __init__(self, path: str | Path, run: str, arm: str, extra: dict | None = None):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.run = run
        self.arm = arm
        self.base = dict(extra or {})
        self._fd = os.open(self.path, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)

    def emit(self, kind: str, task: str | None = None, **fields) -> None:
        record = {
            "v": SCHEMA,
            "ts": time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime()),
            "run": self.run,
            "arm": self.arm,
            "kind": kind,
        }
        if task is not None:
            record["task"] = task
        record.update(self.base)
        record.update(fields)
        os.write(self._fd, (json.dumps(record, separators=(",", ":"), default=str) + "\n").encode())

    def runtime(self, event) -> None:
        payload = asdict(event) if is_dataclass(event) else dict(event)
        self.emit("runtime", payload.get("kind") and None, event=payload)

    def close(self) -> None:
        os.close(self._fd)


def read_events(path: str | Path) -> list[dict]:
    """Parse an event log (for resume and reporting)."""
    records = []
    with open(path, encoding="utf-8") as stream:
        for line in stream:
            line = line.strip()
            if line:
                records.append(json.loads(line))
    return records
