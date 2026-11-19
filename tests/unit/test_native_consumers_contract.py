"""The real-platform fixture must not depend on an image's default command."""

import json

import pytest

# The agent runtime, not the task format: only the consumer contract needs it.
pytest.importorskip("harbor.agents.nop")

from flotilla.compose.model import ImageMeta
from flotilla.compose.normalize import normalize
from flotilla.compose.plan_task import synthesize_command
from tests.contract.native_consumers import fixture_files


def test_compose_fixture_overrides_image_entrypoint() -> None:
    files = fixture_files("compose", "registry.example/neutral@sha256:" + "a" * 64)
    document = json.loads(files["environment/docker-compose.yaml"])
    project = normalize(document)
    command = synthesize_command(
        project.services["db"], ImageMeta(entrypoint=("/.flotilla/bin/busybox", "sleep", "2147483647"))
    )
    assert command is not None
    assert command[:2] == ("/bin/sh", "-c")
    assert "sidecar-owned" in command[2]
