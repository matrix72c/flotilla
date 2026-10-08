"""规范化后的 Compose 项目与逐字段归类（Architecture §4.2 第 1 步、§4.6）。

`Project` 是 build 与 scan 共用的输入：短语法已展开、插值已完成、Harbor 的保活覆盖已叠加。
`Finding` 是一个字段的归类结果，类别取自 §4.6 与 PRD 6.3 节：

- `implemented`：flotilla 实现了它的语义（不记录，只有非"实现"的字段才进报告，§13）；
- `equivalent`：行为与 Docker 不完全相同，但对任务的可观察结果一致，只记录；
- `warn`：任务不拒绝，但报告提醒（例如自身主机名缺口、评测路径限制）；
- `reject`：首版不支持（含需要平台能力的缺口），任务不能构建。
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Literal

Kind = Literal["equivalent", "warn", "reject"]
Condition = Literal["service_started", "service_healthy", "service_completed_successfully"]
RestartPolicy = Literal["no", "always", "unless-stopped", "on-failure"]

MAIN = "main"  # Harbor 的 agent 服务名（§3.5 保活）


@dataclass(frozen=True)
class Finding:
    """一个非"实现"字段的归类结果。`path` 是字段路径，例如 `services.target.read_only`。"""

    path: str
    kind: Kind
    reason: str


@dataclass(frozen=True)
class Build:
    context: str  # 相对任务 environment 目录
    dockerfile: str | None = None
    args: Mapping[str, str] = field(default_factory=dict)


@dataclass(frozen=True)
class Healthcheck:
    """`test` 已归一为 argv：`CMD` 原样，`CMD-SHELL` 为 `["/bin/sh","-c",…]`。时长单位为秒。"""

    test: tuple[str, ...]
    interval_s: float = 30.0
    timeout_s: float = 30.0
    retries: int = 3
    start_period_s: float = 0.0
    start_interval_s: float = 5.0


@dataclass(frozen=True)
class Dependency:
    condition: Condition = "service_started"
    required: bool = True


@dataclass(frozen=True)
class NetworkAttachment:
    aliases: tuple[str, ...] = ()


@dataclass(frozen=True)
class VolumeMount:
    """一个卷引用（长语法）。`kind="volume"` 时 `source` 是顶层 named volume 名；`kind="bind"` 时是相对
    environment 目录的路径。"""

    kind: Literal["volume", "bind"]
    source: str
    target: str
    read_only: bool = False
    nocopy: bool = False
    create_host_path: bool = True  # bind 源不存在时：Docker 在源处建空目录并挂载（长语法默认 true）


@dataclass(frozen=True)
class Service:
    name: str
    image: str | None = None
    build: Build | None = None
    command: tuple[str, ...] | None = None
    entrypoint: tuple[str, ...] | None = None
    environment: Mapping[str, str] = field(default_factory=dict)
    working_dir: str | None = None
    user: str | None = None
    healthcheck: Healthcheck | None = None
    healthcheck_disabled: bool = False  # `disable: true` 或 `test: [NONE]`：连镜像的 HEALTHCHECK 也不用
    depends_on: Mapping[str, Dependency] = field(default_factory=dict)
    networks: Mapping[str, NetworkAttachment] = field(default_factory=dict)  # 空 = network_mode: none
    volumes: tuple[VolumeMount, ...] = ()
    tmpfs: tuple[str, ...] = ()
    restart: RestartPolicy = "no"
    privileged: bool = False
    hostname: str | None = None
    udp_ports: tuple[int, ...] = ()


@dataclass(frozen=True)
class NetworkDef:
    internal: bool = False


@dataclass(frozen=True)
class Project:
    """一个任务规范化后的 Compose 项目。`runtime_params`：`environment` 中引用了任务未提供变量的位置
    （服务 → 变量名 → 原表达式、默认值），运行时由 provider 配置给值（§4.2）。"""

    services: Mapping[str, Service]
    networks: Mapping[str, NetworkDef]
    volumes: frozenset[str]  # 顶层 named volume
    runtime_params: Mapping[str, Mapping[str, tuple[str, str | None]]] = field(default_factory=dict)
    findings: tuple[Finding, ...] = ()

    @property
    def rejected(self) -> bool:
        return any(f.kind == "reject" for f in self.findings)
