"""Opt-in checks against pinned official Compose (no Docker daemon or workloads).

FLOTILLA_TEST_COMPOSE=/absolute/docker-compose pytest tests/contract/test_compose_official.py
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest
import yaml

from flotilla.compose.adapter import ComposeConfigAdapter
from flotilla.compose.model import Project

BINARY = os.environ.get("FLOTILLA_TEST_COMPOSE")
pytestmark = pytest.mark.skipif(not BINARY, reason="explicit pinned Compose executable required")


def load(root: Path, *texts: str, environment: dict[str, str] | None = None) -> Project:
    assert BINARY is not None
    files = []
    for index, text in enumerate(texts):
        path = root / f"compose-{index}.yaml"
        path.write_text(text)
        files.append(path)
    return ComposeConfigAdapter(command=(BINARY,)).load(
        root,
        files=files,
        environment={"EXPLICIT": "override", **(environment or {})},
        main_image="alpine:3.20",
        main_build=False,
    )


def test_official_interpolation_env_files_and_literals(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("AMBIENT_SECRET", "must-not-leak")
    (tmp_path / ".env").write_text("DOTENV=from-dotenv\nEXPLICIT=dotenv-loses\n")
    (tmp_path / "task.env").write_text('FILE_VALUE=file\nDOUBLE="quoted spaces"\n')
    project = load(
        tmp_path,
        """services:
  main:
    env_file: task.env
    environment:
      DOTENV: ${DOTENV}
      EXPLICIT: ${EXPLICIT}
      FALLBACK: ${UNSET:-fallback}
      LITERAL: $$HOME
      ABSENT: ${AMBIENT_SECRET:-absent}
    command: ["printf", "%s", "$$HOME", "${EXPLICIT}"]
""",
    )
    assert not project.rejected, project.findings
    env = project.services["main"].environment
    assert env == {
        "FILE_VALUE": "file",
        "DOUBLE": "quoted spaces",
        "DOTENV": "from-dotenv",
        "EXPLICIT": "override",
        "FALLBACK": "fallback",
        "LITERAL": "$HOME",
        "ABSENT": "absent",
    }
    assert project.services["main"].command == ("printf", "%s", "$HOME", "override")
    assert not project.runtime_params


@pytest.mark.parametrize("count", range(1, 6))
def test_official_dollar_transport_decoding_preserves_every_source(tmp_path: Path, count: int) -> None:
    literal = "$" * count + "{DO_NOT_EXPAND}"
    escaped = "$" * (2 * count) + "{DO_NOT_EXPAND}"
    key = "KEY" + "$" * count
    (tmp_path / ".env").write_text(f"DOT_LITERAL='{literal}'\n")
    (tmp_path / "normal.env").write_text(f"FROM_ENV_FILE='{literal}'\n")
    (tmp_path / "raw.env").write_text(f"FROM_RAW_ENV_FILE={literal}\n")
    document = {
        "services": {
            "main": {
                "env_file": ["normal.env", {"path": "raw.env", "format": "raw"}],
                "environment": {key: escaped, "FROM_EXPLICIT": "${INJECTED}", "FROM_DOTENV": "${DOT_LITERAL}"},
                "command": ["printf", "%s", escaped],
                "entrypoint": ["echo", escaped],
                "healthcheck": {"test": ["CMD", "echo", escaped]},
                "working_dir": "/tmp/" + escaped,
            }
        }
    }
    project = load(tmp_path, yaml.safe_dump(document), environment={"INJECTED": literal})
    assert not project.rejected, project.findings
    main = project.services["main"]
    assert main.environment == {
        key: literal,
        "FROM_EXPLICIT": literal,
        "FROM_DOTENV": literal,
        "FROM_ENV_FILE": literal,
        "FROM_RAW_ENV_FILE": literal,
    }
    assert main.command == ("printf", "%s", literal)
    assert main.entrypoint == ("echo", literal)
    assert main.healthcheck is not None
    assert main.healthcheck.test == ("echo", literal)
    assert main.working_dir == "/tmp/" + literal
    assert not project.runtime_params


def test_official_multifile_reset_override_and_mount_order(tmp_path: Path) -> None:
    (tmp_path / "data").mkdir()
    project = load(
        tmp_path,
        """services:
  main:
    command: ["old"]
    entrypoint: ["old-entrypoint"]
    environment: {KEEP: "yes", REMOVE: "old"}
    volumes: ["./data:/data:ro"]
""",
        """services:
  main:
    command: !override ["new", "two words"]
    entrypoint: !reset null
    environment:
      REMOVE: !reset null
    volumes: !override ["./data:/changed:ro"]
""",
    )
    assert not project.rejected, project.findings
    main = project.services["main"]
    assert main.command == ("new", "two words")
    assert main.entrypoint is None
    assert main.environment == {"KEEP": "yes"}
    assert [(v.source, v.target, v.read_only) for v in main.volumes] == [("./data", "/changed", True)]


@pytest.mark.parametrize(
    "value,expected",
    [("null", None), ("[]", ()), ('["arg with spaces", "$${LITERAL}"]', ("arg with spaces", "${LITERAL}"))],
)
def test_official_entrypoint_empty_and_null_are_distinct(
    tmp_path: Path, value: str, expected: tuple[str, ...] | None
) -> None:
    project = load(tmp_path, f"services:\n  main:\n    entrypoint: {value}\n    command: []\n")
    assert not project.rejected, project.findings
    assert project.services["main"].entrypoint == expected
    assert project.services["main"].command == ()
