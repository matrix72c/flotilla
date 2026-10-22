"""Isolated, per-open image preparation. BuildKit owns build-cache reuse."""

from __future__ import annotations

import json
import sys
import tempfile
from pathlib import Path

from flotilla.build.docker import DockerBuilder, DockerImageExport, DockerRegistry
from flotilla.build.driver import BuildContext, build_task
from flotilla.build.keys import files_json
from flotilla.compose.task import load_environment
from flotilla.config import FlotillaConfig


def run(request: Path) -> None:
    data = json.loads(request.read_text())
    cfg = FlotillaConfig.from_mapping(data["config"])
    definition = data["definition"]
    environment = dict(definition["config"])
    if data["force_build"] and (Path(definition["directory"]) / "Dockerfile").is_file():
        environment.pop("docker_image", None)
    task = load_environment(
        Path(definition["task_root"]),
        Path(definition["directory"]),
        environment,
        name=definition["name"],
    )
    work = Path(data["work"])
    work.mkdir(parents=True, exist_ok=True)
    # All builder/export scratch directories belong to this operation, including on SIGKILL.
    scratch = work / "tmp"
    scratch.mkdir()
    tempfile.tempdir = str(scratch)
    command = cfg.preparation.docker_command
    exporter = DockerImageExport(docker_cmd=command, ownership_dir=Path(data["containers"]))
    context = BuildContext(
        settings=cfg.build.settings(),
        caps=cfg.platform_capabilities(),
        registry=DockerRegistry(docker_cmd=command),
        builder=DockerBuilder(docker_cmd=command),
        image_export=exporter,
        image_accounts=exporter.accounts,
    )
    outcome = build_task(task, work, context)
    if outcome.manifest_path is None:
        raise ValueError(f"environment preparation {outcome.status}: {outcome.reason}")
    result = outcome.to_json()
    if outcome.needs_publish:
        result["files_digest"] = files_json(outcome.manifest_path.parent / "files")[1]
    Path(data["result"]).write_text(json.dumps(result))


if __name__ == "__main__":
    run(Path(sys.argv[1]))
