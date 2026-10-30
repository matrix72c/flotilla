"""Docker-backed build context for `flotilla prepare` and cleanup of interrupted export containers."""

from __future__ import annotations

import subprocess
from collections.abc import Sequence
from pathlib import Path

from flotilla.build.docker import DockerBuilder, DockerImageExport, DockerRegistry
from flotilla.build.driver import BuildContext
from flotilla.config import FlotillaConfig


def docker_context(cfg: FlotillaConfig, journal: Path) -> BuildContext:
    """Export containers are journaled under `journal` before creation, so a killed run can remove them."""
    command = cfg.prepare.docker_command
    exporter = DockerImageExport(docker_cmd=command, ownership_dir=journal)
    return BuildContext(
        settings=cfg.build.settings(),
        caps=cfg.platform_capabilities(),
        registry=DockerRegistry(docker_cmd=command),
        builder=DockerBuilder(docker_cmd=command),
        image_export=exporter,
        image_accounts=exporter.accounts,
    )


def sweep_journal(docker_command: Sequence[str], journal: Path, *, timeout_s: float = 30) -> list[str]:
    """Remove export containers left by an interrupted run; returns the names that could not be removed."""
    remaining: list[str] = []
    if not journal.is_dir():
        return remaining
    for record in sorted(journal.iterdir()):
        try:
            result = subprocess.run(
                [*docker_command, "rm", "-f", "-v", record.name],
                capture_output=True,
                text=True,
                timeout=timeout_s,
                check=False,
            )
        except (OSError, subprocess.TimeoutExpired):
            remaining.append(record.name)
            continue
        if result.returncode and "No such container" not in result.stderr:
            remaining.append(record.name)
            continue
        record.unlink(missing_ok=True)
    return remaining
