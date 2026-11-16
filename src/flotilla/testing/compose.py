"""Offline double for the Compose adapter boundary.

`OfflineComposeAdapter` emulates the one part of `docker compose config` that
fixtures without interpolation need: the ordered deep merge of the files the
adapter selected, serialized the way the real command serializes it. It runs no
tool, so select/prepare tests work without the pinned Compose binary.

Interpolation, `env_file` and `include`/`extends` are deliberately absent. A
fixture that depends on them has to exercise the official adapter instead; see
`tests/contract/test_compose_official.py`.
"""

from __future__ import annotations

import json
from itertools import pairwise
from pathlib import Path
from typing import Any

import yaml

from flotilla.compose.adapter import ComposeConfigAdapter
from flotilla.compose.task import HarborTask, load_task
from flotilla.integrations.harbor_source import task_environments


def _merge(base: Any, over: Any) -> Any:
    if isinstance(base, dict) and isinstance(over, dict):
        out = dict(base)
        for key, value in over.items():
            out[key] = _merge(base[key], value) if key in base else value
        return out
    return over


class OfflineComposeAdapter(ComposeConfigAdapter):
    """Compose adapter double for select/prepare tests; never executes a tool."""

    def _run(self, args: list[str], cwd: Path, env: dict[str, str]) -> str:  # type: ignore[override]
        if args[-2:] == ["version", "--short"]:
            return self.expected_version
        document: dict[str, Any] = {}
        for flag, value in pairwise(args):
            if flag != "-f":
                continue
            loaded = yaml.safe_load(Path(value).read_text()) or {}
            if not isinstance(loaded, dict):
                raise ValueError(f"offline Compose double: {Path(value).name} is not a mapping")
            document = _merge(document, loaded)
        # The real command escapes every literal dollar so its output can be fed
        # back in as a Compose file; the adapter undoes that exactly once.
        return json.dumps(document).replace("$", "$$")


def offline_task(path: Path, **kwargs: Any) -> HarborTask:
    """`load_task` through the offline double, for fixtures that need no Compose tool."""
    kwargs.setdefault("compose_adapter", OfflineComposeAdapter())
    return load_task(path, **kwargs)


def offline_environments(path: Path, **kwargs: Any) -> list[HarborTask]:
    """`task_environments` through the offline double."""
    kwargs.setdefault("compose_adapter", OfflineComposeAdapter())
    return task_environments(path, **kwargs)
