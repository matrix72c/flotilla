"""Harbor 任务目录的读取与 `task.toml` 层面的归类（`flotilla.compose.task`），以及 `flotilla scan` 命令行。"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from flotilla.cli import main
from flotilla.compose.task import TaskError, find_tasks, load_task
from flotilla.platform.base import Capabilities
from flotilla.scan import scan_task
from tests.unit.reports import report

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


# ───────────────────────────── 读取 ─────────────────────────────


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
    # 任务没有给出的变量记为运行时参数，不读构建机环境。
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
    _task(a, "inner")  # 任务目录里的东西不再当作任务
    _task(tmp_path / "set" / ".cache", "hidden")
    assert list(find_tasks([tmp_path])) == [a, b]
    assert list(find_tasks([a])) == [a]


# ───────────────────────────── network_policy（§6.6）─────────────────────────────


@pytest.mark.parametrize(
    ("toml", "rejected"),
    [
        ("", []),
        ("[environment]\nallow_internet = true\n", []),
        ("[environment]\nallow_internet = false\n", ["task.toml:environment.network_mode"]),
        ('[environment]\nnetwork_mode = "public"\nallow_internet = false\n', []),  # 显式 network_mode 优先
        ('[agent]\nnetwork_mode = "allowlist"\nallowed_hosts = ["a.com"]\n', ["task.toml:agent.network_mode"]),
        ('[verifier]\nnetwork_mode = "no-network"\n', ["task.toml:verifier.network_mode"]),
        ("[verifier.environment]\nallow_internet = false\n", ["task.toml:verifier.environment.network_mode"]),
        (
            '[[steps]]\nname = "s"\n[steps.agent]\nnetwork_mode = "no-network"\n',
            ["task.toml:steps[0].agent.network_mode"],
        ),
    ],
)
def test_network_policy_other_than_public_rejected(tmp_path: Path, toml: str, rejected: list[str]) -> None:
    assert _rejects(_task(tmp_path, "n", toml)) == rejected


# ───────────────────────────── 独立验证模式（第 11 节）─────────────────────────────


@pytest.mark.parametrize(
    ("toml", "rejected"),
    [
        # 独立验证模式本身支持；只拒绝要在 main 以外的服务上收集产物、或运行收集钩子的任务。
        ('[verifier]\nenvironment_mode = "separate"\n', []),
        ('[verifier]\nenvironment_mode = "separate"\n[[artifacts]]\nsource = "/x"\n', []),
        (
            '[verifier]\nenvironment_mode = "separate"\n[[artifacts]]\nsource = "/x"\nservice = "web"\n',
            ["task.toml:artifacts"],
        ),
        ('[verifier.environment]\ncpus = 1\n[[artifacts]]\nsource = "/x"\nservice = "web"\n', ["task.toml:artifacts"]),
        ('[[artifacts]]\nsource = "/x"\nservice = "web"\n', []),  # 共享模式不需要 stop_service
        (
            '[verifier]\nenvironment_mode = "separate"\n[[verifier.collect]]\ncommand = "dump"\nservice = "db"\n',
            ["task.toml:verifier.collect"],
        ),
    ],
)
def test_separate_verifier_needing_stop_service_rejected(tmp_path: Path, toml: str, rejected: list[str]) -> None:
    assert _rejects(_task(tmp_path, "v", toml)) == rejected


def test_scan_task_merges_task_and_deployment_findings(tmp_path: Path, caps: Capabilities) -> None:
    path = _task(tmp_path, "m", "[environment]\nallow_internet = false\n", compose=COMPOSE)
    result = scan_task(load_task(path), caps)
    assert result.status == "rejected"
    assert result.services == ("db", "main")
    line = result.to_json()
    assert line["runtime_params"] == ["DB_PASSWORD"]
    assert any(f["path"] == "task.toml:environment.network_mode" for f in line["findings"])


# ───────────────────────────── 命令行 ─────────────────────────────


def test_scan_cli_writes_one_line_per_task(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    caps = tmp_path / "caps.json"
    caps.write_text(report().dump_json())
    tasks = tmp_path / "tasks"
    _task(tasks, "solo")
    _task(tasks, "multi", compose=COMPOSE)
    _task(tasks, "offline", "[environment]\nallow_internet = false\n")
    (_task(tasks, "broken") / "task.toml").write_text("[task\n")
    out = tmp_path / "scan.jsonl"
    assert main(["scan", str(tasks), "--capabilities", str(caps), "--out", str(out)]) == 0
    lines = {json.loads(line)["path"].rsplit("/", 1)[1]: json.loads(line) for line in out.read_text().splitlines()}
    assert {k: v["status"] for k, v in lines.items()} == {
        "broken": "rejected",
        "multi": "accepted",
        "offline": "rejected",
        "solo": "accepted",
    }
    summary = capsys.readouterr().out
    assert "任务 4 个：通过 2，拒绝 2" in summary
    assert "无法读取任务目录" in summary
