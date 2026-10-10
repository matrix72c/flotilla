"""Harbor 任务目录 → `Project`（Architecture §4.1、§4.2 第 1 步）。不调用平台。

一个任务目录有 `task.toml` 与 `environment/`：`environment/docker-compose.yaml`（多服务），或只有
`environment/Dockerfile` / `task.toml` 的 `docker_image`（单服务，§3.5 的保活覆盖即全部内容）。插值只用
`environment/.env`，不读构建机环境（§4.2 第 1 步）。

`task.toml` 中与环境相关、flotilla 首版不承载的设置在这里归类（§13、§6.6、第 11 节）：

- Harbor `network_policy` 不是 `public`（环境基线、agent / verifier 阶段、多步任务的阶段、独立验证环境，含已废弃的
  `allow_internet = false`）：首版不声明 `disable_internet` / `network_allowlist`，拒绝；
- 独立验证模式下要收集 `main` 以外服务的产物、或有收集钩子：需要 `stop_service`，首版不支持，拒绝。
"""

from __future__ import annotations

import os
import tomllib
from collections.abc import Iterable, Iterator, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml

from flotilla.compose.model import MAIN, Finding, Project
from flotilla.compose.normalize import harbor_overlay, normalize

TASK_FILE = "task.toml"
ENV_DIR = "environment"
COMPOSE_FILE = "docker-compose.yaml"
PUBLIC = "public"


class TaskError(ValueError):
    """任务目录读不出来（缺文件、TOML / YAML 语法错误）。"""


@dataclass(frozen=True)
class HarborTask:
    name: str  # `[task].name`，缺省为目录名
    path: Path
    env_dir: Path
    project: Project
    findings: tuple[Finding, ...]  # `task.toml` 层面的归类，与 `project.findings` 并列


def find_tasks(roots: Iterable[Path]) -> Iterator[Path]:
    """`roots` 下的任务目录：含 `task.toml` 的目录，不再向下找；跳过隐藏目录。按路径排序。"""
    for root in roots:
        if (root / TASK_FILE).is_file():
            yield root
            continue
        for current, dirs, files in os.walk(root):
            if TASK_FILE in files:
                dirs.clear()
                yield Path(current)
                continue
            dirs[:] = sorted(d for d in dirs if not d.startswith("."))


def load_task(path: Path) -> HarborTask:
    try:
        config = tomllib.loads((path / TASK_FILE).read_text())
    except (OSError, tomllib.TOMLDecodeError) as exc:
        raise TaskError(f"{TASK_FILE} 读取失败：{exc}") from exc
    env_dir = path / ENV_DIR
    compose: dict[str, Any] = {}
    if (env_dir / COMPOSE_FILE).is_file():
        try:
            loaded = yaml.safe_load((env_dir / COMPOSE_FILE).read_text())
        except (OSError, yaml.YAMLError) as exc:
            raise TaskError(f"{ENV_DIR}/{COMPOSE_FILE} 读取失败：{exc}") from exc
        if not isinstance(loaded, dict):
            raise TaskError(f"{ENV_DIR}/{COMPOSE_FILE} 不是映射")
        compose = loaded
    environment = _table(config, "environment")
    image = environment.get("docker_image")
    main_image = image if isinstance(image, str) and image else None
    overlay = harbor_overlay(
        compose, main_image=main_image, main_build=main_image is None and (env_dir / "Dockerfile").is_file()
    )
    project = normalize(overlay, _dotenv(env_dir / ".env"))
    name = _table(config, "task").get("name")
    return HarborTask(
        name=name if isinstance(name, str) and name else path.name,
        path=path,
        env_dir=env_dir,
        project=project,
        findings=(*_network_policies(config), *_separate_verifier(config)),
    )


def _network_policies(config: Mapping[str, Any]) -> Iterator[Finding]:
    phases: list[tuple[str, Mapping[str, Any], bool]] = [
        ("environment", _table(config, "environment"), True),
        ("agent", _table(config, "agent"), False),
        ("verifier", _table(config, "verifier"), False),
        ("verifier.environment", _table(_table(config, "verifier"), "environment"), True),
    ]
    steps = config.get("steps")
    for i, step in enumerate(steps if isinstance(steps, list) else []):
        if isinstance(step, Mapping):
            phases += [(f"steps[{i}].agent", _table(step, "agent"), False)]
            phases += [(f"steps[{i}].verifier", _table(step, "verifier"), False)]
            phases += [(f"steps[{i}].verifier.environment", _table(_table(step, "verifier"), "environment"), True)]
    for where, table, baseline in phases:
        mode = _network_mode(table, baseline=baseline)
        if mode is not None and mode != PUBLIC:
            yield Finding(
                f"task.toml:{where}.network_mode",
                "reject",
                f"Harbor network_policy = {mode}：首版不声明 disable_internet / network_allowlist（§6.6）",
            )


def _network_mode(table: Mapping[str, Any], *, baseline: bool) -> str | None:
    """该处显式或经 `allow_internet` 得到的 network_mode；没有设置时为 None（基线即 public）。"""
    mode = table.get("network_mode")
    if isinstance(mode, str):
        return mode
    if baseline and "allowed_hosts" not in table and isinstance(table.get("allow_internet"), bool):
        return PUBLIC if table["allow_internet"] else "no-network"
    return None


def _separate_verifier(config: Mapping[str, Any]) -> Iterator[Finding]:
    verifier = _table(config, "verifier")
    mode = verifier.get("environment_mode")
    separate = mode == "separate" or (mode is None and isinstance(verifier.get("environment"), Mapping))
    if not separate:
        return
    artifacts = config.get("artifacts")
    sidecar = [
        a.get("service")
        for a in (artifacts if isinstance(artifacts, list) else [])
        if isinstance(a, Mapping) and a.get("service") not in (None, MAIN)
    ]
    if sidecar:
        yield Finding(
            "task.toml:artifacts",
            "reject",
            f"独立验证模式下收集 {sorted(set(map(str, sidecar)))} 的产物需要 stop_service，首版不支持（第 11 节）",
        )
    collect = verifier.get("collect")
    if isinstance(collect, list) and collect:
        yield Finding(
            "task.toml:verifier.collect", "reject", "独立验证模式下的收集钩子需要 stop_service，首版不支持（第 11 节）"
        )


def _table(config: Mapping[str, Any], key: str) -> Mapping[str, Any]:
    value = config.get(key)
    return value if isinstance(value, Mapping) else {}


def _dotenv(path: Path) -> dict[str, str]:
    """`KEY=VALUE` 行；`#` 开头为注释，值两侧的一对引号去掉。与 Compose 一样不做值内展开。"""
    if not path.is_file():
        return {}
    out: dict[str, str] = {}
    for line in path.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        key = key.removeprefix("export ").strip()
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in "'\"":
            value = value[1:-1]
        out[key] = value
    return out
