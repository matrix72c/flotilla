"""Replace external Docker operations while retaining the complete preparation chain."""

from __future__ import annotations

import json
from pathlib import Path

from flotilla.build.driver import build_task
from flotilla.build.keys import files_json
from flotilla.compose.task import load_environment
from flotilla.preparation.service import Preparer
from flotilla.testing.build import _ctx


async def fake_build_process(self: Preparer, request: Path) -> None:
    data = json.loads(request.read_text())
    definition = data["definition"]
    config = dict(definition["config"])
    if data["force_build"] and (Path(definition["directory"]) / "Dockerfile").is_file():
        config.pop("docker_image", None)
    task = load_environment(
        Path(definition["task_root"]), Path(definition["directory"]), config, name=definition["name"]
    )
    outcome = build_task(task, Path(data["work"]), _ctx())
    if outcome.manifest_path is None:
        raise ValueError(outcome.reason)
    result = outcome.to_json()
    if outcome.needs_publish:
        result["files_digest"] = files_json(outcome.manifest_path.parent / "files")[1]
    Path(data["result"]).write_text(json.dumps(result))
