"""Reading Harbor task directories and `task.toml`-level classification (`flotilla.compose.task`), plus the
`flotilla scan` CLI."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from flotilla.cli import main
from flotilla.compose.task import TaskError, find_tasks, load_task
from flotilla.config import FlotillaConfig
from flotilla.platform.base import Capabilities
from flotilla.scan import scan_task
from flotilla.testing.config import importable

COMPOSE = """
services:
  main:
    image: r.io/ns/app:1
  db:
    image: postgres:14
    environment:
      PASSWORD: secret
"""


def _task(
    root: Path,
    name: str,
    toml: str = "",
    compose: str | None = None,
    *,
    image: str | None = "r.io/ns/app:1",
    **files: str,
) -> Path:
    path = root / name
    (path / "environment").mkdir(parents=True)
    if image is not None:  # every task names a published image; building one is out of scope
        declaration = f'docker_image = "{image}"\n'
        toml = (
            toml.replace("[environment]\n", f"[environment]\n{declaration}", 1)
            if "[environment]\n" in toml
            else f"[environment]\n{declaration}{toml}"
        )
    (path / "task.toml").write_text(f'[task]\nname = "ds/{name}"\n{toml}')
    if compose is not None:
        (path / "environment" / "docker-compose.yaml").write_text(compose)
    for rel, text in files.items():
        (path / "environment" / rel).write_text(text)
    return path


def _rejects(path: Path) -> list[str]:
    return [f.path for f in load_task(path).findings if f.kind == "reject"]


# ───────────────────────────── Reading ─────────────────────────────


def test_single_service_from_the_task_toml_image(tmp_path: Path) -> None:
    task = load_task(_task(tmp_path, "solo"))
    assert task.name == "ds/solo"
    assert set(task.project.services) == {"main"}
    assert task.project.services["main"].image == "r.io/ns/app:1"
    assert not task.findings


def test_dockerfile_without_a_declared_image_is_rejected(tmp_path: Path) -> None:
    """Producing the image is a separate job: the task must name one that is already published."""
    path = _task(tmp_path, "src", image=None)
    (path / "environment" / "Dockerfile").write_text("FROM scratch\n")
    rejected = [f.path for f in load_task(path).project.findings if f.kind == "reject"]
    assert "services.main.build" in rejected


def test_unreadable_task(tmp_path: Path) -> None:
    path = _task(tmp_path, "bad")
    (path / "task.toml").write_text("[task\n")
    with pytest.raises(TaskError):
        load_task(path)
    (path / "task.toml").write_text("")
    (path / "environment" / "docker-compose.yaml").write_text("- a list\n")
    with pytest.raises(TaskError):
        load_task(path)


def test_find_tasks_stops_at_task_dirs_and_skips_hidden(tmp_path: Path) -> None:
    a = _task(tmp_path / "set", "a")
    b = _task(tmp_path / "set" / "nested", "b")
    _task(a, "inner")  # things inside a task directory are not treated as tasks
    _task(tmp_path / "set" / ".cache", "hidden")
    assert list(find_tasks([tmp_path])) == [a, b]
    assert list(find_tasks([a])) == [a]


# ───────────────────────────── network_policy ─────────────────────────────


@pytest.mark.parametrize(
    ("toml", "rejected"),
    [
        ("", []),
        ("[environment]\nallow_internet = true\n", []),
        ("[environment]\nallow_internet = false\n", []),
        ('[environment]\nnetwork_mode = "no-network"\n', []),
        ('[environment]\nnetwork_mode = "allowlist"\n', ["task.toml:environment.network_mode"]),
        ('[environment]\nnetwork_mode = "public"\nallow_internet = false\n', []),  # explicit network_mode wins
        ('[agent]\nnetwork_mode = "allowlist"\nallowed_hosts = ["a.com"]\n', []),
        ('[verifier]\nnetwork_mode = "no-network"\n', []),
        ("[verifier.environment]\nallow_internet = false\n", []),
        (
            '[[steps]]\nname = "s"\n[steps.agent]\nnetwork_mode = "no-network"\n',
            [],
        ),
    ],
)
def test_network_policy_supported_modes(tmp_path: Path, toml: str, rejected: list[str]) -> None:
    assert _rejects(_task(tmp_path, "n", toml)) == rejected


# ───────────────────────────── Separate verification mode (section 11) ─────────────────────────────


@pytest.mark.parametrize(
    ("toml", "rejected"),
    [
        # Separate verification and sidecar artifacts are handled by the Harbor adapter.
        ('[verifier]\nenvironment_mode = "separate"\n', []),
        ('[verifier]\nenvironment_mode = "separate"\n[[artifacts]]\nsource = "/x"\n', []),
        (
            '[verifier]\nenvironment_mode = "separate"\n[[artifacts]]\nsource = "/x"\nservice = "web"\n',
            [],
        ),
        ('[verifier.environment]\ncpus = 1\n[[artifacts]]\nsource = "/x"\nservice = "web"\n', []),
        ('[[artifacts]]\nsource = "/x"\nservice = "web"\n', []),  # shared mode needs no stop_service
        (
            '[verifier]\nenvironment_mode = "separate"\n[[verifier.collect]]\ncommand = "dump"\nservice = "db"\n',
            [],
        ),
    ],
)
def test_separate_verifier_artifacts_are_supported(tmp_path: Path, toml: str, rejected: list[str]) -> None:
    assert _rejects(_task(tmp_path, "v", toml)) == rejected


def test_scan_task_merges_task_and_deployment_findings(tmp_path: Path, caps: Capabilities) -> None:
    path = _task(tmp_path, "m", '[environment]\nnetwork_mode = "allowlist"\n', compose=COMPOSE)
    result = scan_task(load_task(path), caps)
    assert result.status == "rejected"
    assert result.services == ("db", "main")
    line = result.to_json()
    assert any(f["path"] == "task.toml:environment.network_mode" for f in line["findings"])


# ───────────────────────────── CLI ─────────────────────────────


def test_inspect_reports_every_task_and_fails_on_any_that_would_not_open(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    """One malformed task does not hide the rest, and the exit status says the dataset is not clean."""
    tasks = tmp_path / "tasks"
    _task(tasks, "solo")
    _task(tasks, "multi", compose=COMPOSE)
    _task(tasks, "offline", '[environment]\nnetwork_mode = "no-network"\n')
    _task(tasks, "unknown-network", compose="services:\n  main:\n    networks: [missing]\n")
    (_task(tasks, "broken") / "task.toml").write_text("[task\n")
    out = tmp_path / "report.json"
    deployment = importable(
        FlotillaConfig.from_mapping(
            {
                "opensandbox": {
                    "endpoint": "http://os.test",
                    "credential_env": "FLOTILLA_CRED",
                    "network": {"sandbox_cidrs": ["10.0.0.0/16"]},
                },
                "storage": {"host_path": "/mnt/shared"},
                "share": {"release": "test", "release_sha256": "a" * 64},
            }
        )
    )
    # Planning reads each image's config; this test is about the report, not the registry.
    monkeypatch.setattr("flotilla.registry.ImageConfigs.image_config", lambda self, pinned: {})
    assert main(["inspect", str(tasks), "--deployment", deployment, "--out", str(out)]) == 1
    document = json.loads(out.read_text())
    outcomes = {row["task"].rsplit("/", 1)[1]: row["planned"] for row in document["tasks"]}
    assert outcomes == {
        "broken": False,
        "multi": True,
        "offline": True,
        "solo": True,
        "unknown-network": False,
    }
    assert (document["planned"], document["failed"]) == (3, 2)
    summary = capsys.readouterr().out
    # Only the failures are named, each with why; the rest is one count.
    assert "broken" in summary and "unknown-network" in summary and "solo" not in summary
    assert "3 of 5 tasks would open" in summary
