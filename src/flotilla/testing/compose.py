"""Offline double for the Compose adapter boundary.

`OfflineComposeAdapter` emulates the one part of `docker compose config` that
fixtures without interpolation need: the ordered deep merge of the files the
adapter selected, serialized the way the real command serializes it. It runs no
tool, so select/prepare tests work without the pinned Compose binary.

Interpolation, `env_file` and `include`/`extends` are deliberately absent, and a
file that uses one is refused rather than resolved differently from the real
command. A fixture that depends on them has to exercise the official adapter
instead; see `tests/contract/test_compose_official.py`.
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
            text = Path(value).read_text()
            # Refuse rather than emulate: a silently unsubstituted "${VAR}" would
            # reach normalize() as a literal and the fixture would pass on a value
            # the real command never produces.
            if "$" in text:
                raise ValueError(f"offline Compose double cannot interpolate {Path(value).name}")
            loaded = yaml.safe_load(text) or {}
            if not isinstance(loaded, dict):
                raise ValueError(f"offline Compose double: {Path(value).name} is not a mapping")
            services = loaded.get("services") or {}
            if isinstance(services, dict) and any(
                isinstance(service, dict) and "env_file" in service for service in services.values()
            ):
                raise ValueError(f"offline Compose double cannot read env_file in {Path(value).name}")
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
