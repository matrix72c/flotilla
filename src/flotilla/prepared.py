"""Prepared task rows: the jsonl that `flotilla prepare` writes and training/evaluation consume.

One line per task. Top-level keys other than `id`, `task_dir`, `dataset_rev`, `flotilla_version` and `env` are
filter attributes (written by prepare) or consumer fields; they are kept as-is. `env` is flotilla-owned: one
prepared environment per role, regenerated whenever the task or flotilla's preparation rules change.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from flotilla.manifest import Manifest

ROLES = ("main", "verifier")
_CORE = ("id", "task_dir", "dataset_rev", "flotilla_version", "env")


@dataclass(frozen=True)
class PreparedEnvironment:
    """One prepared environment: digest-pinned images and published task files, ready to open."""

    _manifest: Manifest = field(repr=False)
    digest: str = field(init=False)

    def __post_init__(self) -> None:
        manifest = self._manifest.model_copy(deep=True)
        object.__setattr__(self, "_manifest", manifest)
        object.__setattr__(self, "digest", hashlib.sha256(manifest.dump_json().encode()).hexdigest())
        if manifest.agent_service not in manifest.units:
            raise ValueError("manifest agent_service does not exist")
        if any(f.kind == "reject" for f in manifest.findings):
            raise ValueError("manifest contains rejected findings")

    def manifest(self) -> Manifest:
        return self._manifest.model_copy(deep=True)


@dataclass(frozen=True)
class PreparedTask:
    id: str
    task_dir: str
    environments: Mapping[str, PreparedEnvironment]
    dataset_rev: str | None = None
    flotilla_version: str | None = None
    attrs: Mapping[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if not self.id:
            raise ValueError("prepared row id must be nonempty")
        if "main" not in self.environments or set(self.environments) - set(ROLES):
            raise ValueError(f"prepared row {self.id}: env must have 'main' and at most 'verifier'")

    def environment(self, role: str = "main") -> PreparedEnvironment:
        return self.environments[role]

    @property
    def verifier(self) -> PreparedEnvironment | None:
        return self.environments.get("verifier")

    @classmethod
    def from_row(cls, row: Mapping[str, Any]) -> PreparedTask:
        env = row.get("env")
        if not isinstance(env, Mapping):
            raise ValueError("prepared row lacks an env object; run flotilla prepare")
        return cls(
            id=str(row.get("id") or ""),
            task_dir=str(row["task_dir"]),
            environments={role: PreparedEnvironment(Manifest.load(data)) for role, data in env.items()},
            dataset_rev=row.get("dataset_rev"),
            flotilla_version=row.get("flotilla_version"),
            attrs={key: value for key, value in row.items() if key not in _CORE},
        )

    def to_row(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "task_dir": self.task_dir,
            **self.attrs,
            "dataset_rev": self.dataset_rev,
            "flotilla_version": self.flotilla_version,
            "env": {
                role: self.environments[role].manifest().model_dump(mode="json", by_alias=True)
                for role in ROLES
                if role in self.environments
            },
        }


def dump_row(task: PreparedTask) -> str:
    return json.dumps(task.to_row(), ensure_ascii=False, separators=(",", ":"))


def load_prepared(path: str | Path) -> list[PreparedTask]:
    """Rows of a prepared jsonl in file order; when an id repeats, its last row wins."""
    rows: dict[str, PreparedTask] = {}
    with Path(path).open(encoding="utf-8") as handle:
        for number, line in enumerate(handle, 1):
            if not line.strip():
                continue
            try:
                task = PreparedTask.from_row(json.loads(line))
            except (ValueError, KeyError, TypeError) as exc:
                raise ValueError(f"{path}:{number}: invalid prepared row: {exc}") from exc
            rows[task.id] = task
    return list(rows.values())
