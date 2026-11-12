"""Official config process boundary and fail-closed translation (no Docker required)."""

from __future__ import annotations

import json
import subprocess
from pathlib import Path
from typing import Any
from unittest.mock import patch

import pytest
import yaml

from flotilla.compose.adapter import COMPOSE_VERSION, ComposeConfigAdapter
from flotilla.compose.manifest_gen import synthesize_command
from flotilla.compose.model import ImageMeta
from flotilla.compose.normalize import normalize
from flotilla.compose.task import TaskError, load_task


def test_adapter_delegates_ordered_merge_and_interpolation(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("SECRET_FROM_HOST", "must-not-leak")
    first = tmp_path / "compose.yaml"
    second = tmp_path / "override.yaml"
    first.write_text("services: {}")
    second.write_text("services: {}")
    (tmp_path / ".env").write_text("FROM_DOTENV=explicit\n")
    calls = []

    def run(args: list[str], **kwargs: Any) -> subprocess.CompletedProcess[str]:
        calls.append((args, kwargs))
        if args[-2:] == ["version", "--short"]:
            return subprocess.CompletedProcess(args, 0, f"v{COMPOSE_VERSION}\n", "")
        assert kwargs["cwd"] == tmp_path
        assert "SECRET_FROM_HOST" not in kwargs["env"]
        assert kwargs["env"]["CONTEXT_DIR"] == str(tmp_path)
        assert kwargs["env"]["EXPLICIT"] == "yes"
        paths = [args[i + 1] for i, arg in enumerate(args) if arg == "-f"]
        assert paths[1:] == [str(first), str(second)]
        defaults = yaml.safe_load(Path(paths[0]).read_text())
        assert defaults["services"]["main"]["command"] == ["sh", "-c", "sleep infinity"]
        assert args[args.index("--env-file") + 1] == str(tmp_path / ".env")
        # Stand-in for official config: literals must not pass through our interpolator.
        output = {
            "name": "flotilla",
            "services": {
                "main": {
                    "image": "task-image",
                    "build": {"context": "."},
                    "command": [],
                    "entrypoint": [],
                    "environment": {"LITERAL": "${UNCHANGED}", "DOLLARS": "$HOME", "KEY$$": "one$two$$three$$$"},
                    "volumes": [{"type": "bind", "source": str(tmp_path / "data"), "target": "/data"}],
                }
            },
            "networks": {"default": {"name": "flotilla_default"}},
        }
        return subprocess.CompletedProcess(args, 0, json.dumps(output).replace("$", "$$"), "")

    with patch("flotilla.compose.adapter.subprocess.run", side_effect=run):
        project = ComposeConfigAdapter().load(
            tmp_path,
            files=(first, second),
            environment={"EXPLICIT": "yes"},
            main_image="harbor-image",
            main_build=False,
        )
    assert not project.rejected, project.findings
    service = project.services["main"]
    assert service.image == "harbor-image" and service.build is None
    assert service.command == service.entrypoint == ()
    assert service.environment == {"LITERAL": "${UNCHANGED}", "DOLLARS": "$HOME", "KEY$$": "one$two$$three$$$"}
    assert service.volumes[0].source == "./data"
    assert project.runtime_params == {}
    assert len(calls) == 2
    assert calls[-1][0][-4:] == ["config", "--format", "json", "--no-path-resolution"]


def test_adapter_checks_the_compose_version_once_per_process(tmp_path: Path) -> None:
    calls: list[list[str]] = []

    def run(args: list[str], **kwargs: Any) -> subprocess.CompletedProcess[str]:
        calls.append(args)
        if args[-2:] == ["version", "--short"]:
            return subprocess.CompletedProcess(args, 0, COMPOSE_VERSION, "")
        return subprocess.CompletedProcess(args, 0, '{"name": "flotilla", "services": {"main": {"image": "i"}}}', "")

    (tmp_path / "compose.yaml").write_text("services: {}")
    with patch("flotilla.compose.adapter.subprocess.run", side_effect=run):
        for _ in range(3):
            ComposeConfigAdapter().load(
                tmp_path, files=(tmp_path / "compose.yaml",), environment={}, main_image="i", main_build=False
            )
    assert [args[-2:] for args in calls].count(["version", "--short"]) == 1
    assert len(calls) == 4


@pytest.mark.parametrize("failure", ["missing", "wrong-version", "config-error"])
def test_adapter_never_falls_back(tmp_path: Path, failure: str) -> None:
    def run(args: list[str], **kwargs: Any) -> subprocess.CompletedProcess[str]:
        if failure == "missing":
            raise FileNotFoundError("docker")
        if args[-2:] == ["version", "--short"]:
            return subprocess.CompletedProcess(args, 0, "1.0.0" if failure == "wrong-version" else COMPOSE_VERSION, "")
        return subprocess.CompletedProcess(args, 1, "", "invalid compose input")

    with patch("flotilla.compose.adapter.subprocess.run", side_effect=run), pytest.raises(ValueError):
        ComposeConfigAdapter().load(tmp_path, files=(), environment={}, main_image="image", main_build=False)


@pytest.mark.parametrize(
    "text",
    [
        "include: /etc/secret.yaml\nservices: {}\n",
        "services:\n  main:\n    extends: {file: /etc/secret.yaml, service: main}\n",
        "services:\n  main:\n    env_file: /etc/secret.env\n",
        "services:\n  main:\n    env_file: ../secret.env\n",
        "services:\n  main:\n    env_file: ${ENV_FILE}\n",
        "services:\n  main:\n    env_file: [{path: '${CONTEXT_DIR}/secret.env', required: false}]\n",
        "services:\n  main:\n    label_file: /etc/secret.env\n",
        "configs:\n  secret: {file: /etc/secret}\nservices: {}\n",
        "secrets:\n  secret: {file: /etc/secret}\nservices: {}\n",
        "services:\n  main:\n    secrets: [secret]\n",
        "x-base: &base\n  env_file: /etc/secret.env\nservices:\n  main:\n    <<: *base\n",
        "x-base: &base\n  include: /etc/secret.yaml\n<<: *base\nservices: {}\n",
    ],
)
def test_unsafe_file_features_are_rejected_before_any_compose_call(tmp_path: Path, text: str) -> None:
    compose = tmp_path / "compose.yaml"
    compose.write_text(text)
    with patch("flotilla.compose.adapter.subprocess.run") as run, pytest.raises(ValueError):
        ComposeConfigAdapter().load(tmp_path, files=(compose,), environment={}, main_image="image", main_build=False)
    run.assert_not_called()


@pytest.mark.parametrize("name", [".env", "task.env", "compose.yaml"])
def test_config_input_symlinks_cannot_escape_project(tmp_path: Path, name: str) -> None:
    root = tmp_path / "project"
    root.mkdir()
    outside = tmp_path / "secret"
    outside.write_text("PRIVATE=value\n")
    compose = root / "compose.yaml"
    if name != "compose.yaml":
        compose.write_text("services:\n  main:\n    env_file: task.env\n")
    (root / name).symlink_to(outside)
    with patch("flotilla.compose.adapter.subprocess.run") as run, pytest.raises(ValueError):
        ComposeConfigAdapter().load(root, files=(compose,), environment={}, main_image="image", main_build=False)
    run.assert_not_called()


def test_path_gate_preserves_compose_tags_and_confined_env_file_aliases(tmp_path: Path) -> None:
    compose = tmp_path / "compose.yaml"
    compose.write_text(
        "x-env: &env\n  env_file: [{path: local.env, required: true}]\n"
        "services:\n  main:\n    <<: *env\n    command: !override []\n    entrypoint: !reset null\n"
    )
    (tmp_path / "local.env").write_text("VALUE=private\n")
    output = {"services": {"main": {"image": "image", "command": [], "environment": {"VALUE": "private"}}}}
    with patch(
        "flotilla.compose.adapter.subprocess.run",
        side_effect=[
            subprocess.CompletedProcess([], 0, COMPOSE_VERSION, ""),
            subprocess.CompletedProcess([], 0, json.dumps(output), ""),
        ],
    ) as run:
        project = ComposeConfigAdapter().load(
            tmp_path,
            files=(compose,),
            environment={},
            main_image="image",
            main_build=False,
        )
    assert not project.rejected and project.services["main"].command == ()
    assert project.services["main"].environment == {"VALUE": "private"}
    assert run.call_count == 2
    assert "!override" in compose.read_text()  # gate never rewrites the task's YAML


def test_loader_uses_official_adapter_without_parsing_yaml_itself(tmp_path: Path) -> None:
    (tmp_path / "task.toml").write_text('[environment]\ndocker_image="image"\n')
    env = tmp_path / "environment"
    env.mkdir()
    (env / "docker-compose.yaml").write_text("!override official-compose-tags")
    document = {"services": {"main": {"image": "image", "command": ["sh"]}}}
    with patch(
        "flotilla.compose.adapter.subprocess.run",
        side_effect=[
            subprocess.CompletedProcess([], 0, COMPOSE_VERSION, ""),
            subprocess.CompletedProcess([], 0, json.dumps(document), ""),
        ],
    ):
        task = load_task(tmp_path, compose_adapter=ComposeConfigAdapter())
    assert task.project.services["main"].command == ("sh",)
    with (
        patch("flotilla.compose.adapter.subprocess.run", side_effect=FileNotFoundError("docker")),
        pytest.raises(TaskError, match="official Compose"),
    ):
        load_task(tmp_path, compose_adapter=ComposeConfigAdapter())


@pytest.mark.parametrize("entrypoint", [None, [], "", ["override"]])
@pytest.mark.parametrize("command", [None, [], "", ["argument"]])
def test_null_and_empty_command_entrypoint(entrypoint: str | list[str] | None, command: str | list[str] | None) -> None:
    project = normalize({"services": {"main": {"image": "image", "entrypoint": entrypoint, "command": command}}}, {})
    meta = ImageMeta(entrypoint=("image-entry",), cmd=("image-cmd",))
    expected_entry = ("image-entry",) if entrypoint is None else tuple(entrypoint)
    expected_cmd = (("image-cmd",) if entrypoint is None else ()) if command is None else tuple(command)
    assert synthesize_command(project.services["main"], meta) == ((*expected_entry, *expected_cmd) or None)


@pytest.mark.parametrize(
    "field,value",
    [
        ("ports", ["8080:80"]),
        ("ports", [80]),
        ("hostname", "own-host"),
        ("container_name", "own-container"),
        ("restart", "always"),
        ("restart", "unless-stopped"),
        ("restart", "on-failure:3"),
        ("init", True),
        ("init", False),
        ("stop_signal", "SIGQUIT"),
        ("stop_grace_period", "30s"),
        ("privileged", True),
        ("logging", {"driver": "json-file"}),
        ("pull_policy", "always"),
    ],
)
def test_unimplemented_semantics_rejected(field: str, value: object, tmp_path: Path) -> None:
    from flotilla.build.driver import build_task
    from flotilla.compose.task import HarborTask
    from flotilla.testing.build import FakeBuilder, FakeRegistry, _ctx

    project = normalize({"services": {"main": {"image": "image", field: value}}}, {})
    assert any(f.kind == "reject" and f.path == f"services.main.{field}" for f in project.findings)
    builder, registry = FakeBuilder(), FakeRegistry()
    task = HarborTask("rejected", tmp_path, tmp_path, project, ())
    outcome = build_task(task, tmp_path / "out", _ctx(builder=builder, registry=registry))
    assert outcome.status == "rejected"
    assert not builder.requests and not registry.resolved and not registry.copied
    assert registry.configs == 0 and not (tmp_path / "out").exists()
