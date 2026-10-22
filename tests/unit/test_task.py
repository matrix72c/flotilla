"""Reading Harbor task directories and `task.toml`-level classification (`flotilla.compose.task`), plus the
`flotilla scan` CLI."""

from __future__ import annotations

import builtins
import json
from pathlib import Path
from typing import Any

import pytest

from flotilla.cli import main
from flotilla.compose.task import TaskError, find_tasks, load_task
from flotilla.platform.base import Capabilities
from flotilla.preparation.source import task_environments
from flotilla.scan import scan_task
from flotilla.testing.reports import report

COMPOSE = """
services:
  main:
    build: .
  db:
    image: postgres:${PG_TAG:-14}
    environment:
      PASSWORD: ${DB_PASSWORD}
"""


def _task(root: Path, name: str, toml: str = "", compose: str | None = None, **files: str) -> Path:
    path = root / name
    (path / "environment").mkdir(parents=True)
    (path / "task.toml").write_text(f'[task]\nname = "ds/{name}"\n{toml}')
    (path / "environment" / "Dockerfile").write_text("FROM scratch\n")
    if compose is not None:
        (path / "environment" / "docker-compose.yaml").write_text(compose)
    for rel, text in files.items():
        (path / "environment" / rel).write_text(text)
    return path


def _rejects(path: Path) -> list[str]:
    return [f.path for f in load_task(path).findings if f.kind == "reject"]


# ───────────────────────────── Reading ─────────────────────────────


def test_single_service_dockerfile(tmp_path: Path) -> None:
    task = load_task(_task(tmp_path, "solo"))
    assert task.name == "ds/solo"
    assert set(task.project.services) == {"main"}
    assert task.project.services["main"].build is not None  # environment/Dockerfile
    assert not task.findings


def test_docker_image_from_task_toml(tmp_path: Path) -> None:
    task = load_task(_task(tmp_path, "img", '[environment]\ndocker_image = "reg/x:1"\n'))
    assert task.project.services["main"].image == "reg/x:1"
    assert task.project.services["main"].build is None


def test_compose_with_dotenv(tmp_path: Path) -> None:
    assert load_task(_task(tmp_path, "plain", compose=COMPOSE)).project.services["db"].image == "postgres:14"
    task = load_task(_task(tmp_path, "multi", compose=COMPOSE, **{".env": "PG_TAG='16'\n# c\nexport X=1\n"}))
    assert set(task.project.services) == {"main", "db"}
    assert task.project.services["db"].image == "postgres:16"
    # Variables the task does not provide become runtime parameters; the build machine environment is not read.
    assert task.project.runtime_params["db"]["PASSWORD"][0] == "${DB_PASSWORD}"


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


@pytest.mark.parametrize("missing", ["harbor", "harbor.models.task.verifier_mode", "missing_dependency"])
def test_harbor_fallback_only_when_optional_package_is_absent(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, missing: str
) -> None:
    path = _task(tmp_path, "optional-harbor")
    original = builtins.__import__

    def load(name: str, *args: Any, **kwargs: Any) -> Any:
        if name.startswith("harbor."):
            raise ModuleNotFoundError(f"missing {missing}", name=missing)
        return original(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", load)
    assert len(task_environments(path)) == 1


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
    assert line["runtime_params"] == ["DB_PASSWORD"]
    assert any(f["path"] == "task.toml:environment.network_mode" for f in line["findings"])


# ───────────────────────────── CLI ─────────────────────────────


def test_scan_cli_writes_one_line_per_task(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    caps = tmp_path / "caps.json"
    caps.write_text(report().dump_json())
    tasks = tmp_path / "tasks"
    _task(tasks, "solo")
    _task(tasks, "multi", compose=COMPOSE)
    _task(tasks, "offline", '[environment]\nnetwork_mode = "no-network"\n')
    _task(tasks, "unknown-network", compose="services:\n  main:\n    networks: [missing]\n")
    (_task(tasks, "broken") / "task.toml").write_text("[task\n")
    out = tmp_path / "scan.jsonl"
    deployment = tmp_path / "deployment.toml"
    deployment.write_text(
        '[opensandbox]\nendpoint="http://os.test"\ncredential_env="FLOTILLA_CRED"\n'
        f"capabilities={json.dumps(str(caps))}\n"
        '[opensandbox.network]\nsandbox_cidrs=["10.0.0.0/16"]\n'
        '[storage]\nhost_path="/mnt/shared"\n[share]\nrelease="test"\n'
        f'release_sha256="{"a" * 64}"\n'
    )
    assert main(["inspect", str(tasks), "--deployment", str(deployment), "--out", str(out)]) == 1
    lines = {row["path"].rsplit("/", 1)[1]: row for row in json.loads(out.read_text())["environments"]}
    assert {k: v["status"] for k, v in lines.items()} == {
        "broken": "rejected",
        "multi": "accepted",
        "offline": "accepted",
        "solo": "accepted",
        "unknown-network": "rejected",
    }
    summary = capsys.readouterr().out
    assert "broken" in summary
    assert "rejected" in summary
