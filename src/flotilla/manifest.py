"""任务清单 schema（带 schema 版本，Architecture §4.5）。训练侧唯一的任务输入，不含平台凭证。

数据边界用 Pydantic v2（`extra="forbid"`）：清单由 `flotilla build` 写出、跨机器传递、被 provider 读入，
任何未知字段或类型不符都在读入时报错，而不是在 trial 中途。

要点（§4.5）：
- 每个单元分 `command`（业务进程 argv）与 `context`（执行上下文：env、uid、gid、cwd，§3.5）；
- 挂载以 `scope`（`task` / `trial`）+ `key` 表达，与 `flotilla.core.plan.Mount` 一致；
- 本 trial 要准备的目录（共享卷与有写者的 bind 源组）都在 `trial_volumes`。
"""

from __future__ import annotations

from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field

SCHEMA_VERSION = 2

Condition = Literal["service_started", "service_healthy", "service_completed_successfully"]
RestartPolicy = Literal["no", "always", "unless-stopped", "on-failure"]


class _Model(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class Resources(_Model):
    cpu: str
    memory: str


class Context(_Model):
    env: dict[str, str]
    uid: int
    gid: int
    cwd: str


class Healthcheck(_Model):
    test: list[str] = Field(min_length=1)
    interval_s: float = Field(gt=0)
    timeout_s: float = Field(gt=0)
    retries: int = Field(ge=1)
    start_period_s: float = Field(ge=0)
    start_interval_s: float = Field(gt=0)


class Mount(_Model):
    scope: Literal["task", "trial"]
    key: str
    target: str
    read_only: bool


class Unit(_Model):
    image: str  # repo@sha256:…（C15）
    resources: Resources
    context: Context
    command: list[str] | None  # 业务进程 argv；None 表示没有业务进程
    healthcheck: Healthcheck | None = None
    restart: RestartPolicy = "no"
    mounts: list[Mount] = []
    privileged: bool = False


class Network(_Model):
    internal: bool
    members: list[str]
    names: dict[str, list[str]]  # 名字 → 服务（§3.7）


class Dependency(_Model):
    condition: Condition
    required: bool = True


class TrialVolume(_Model):
    key: str
    owner: tuple[int, int] | None = None
    seed: str | None = None
    copy_from: str | None = None


class RuntimeParam(_Model):
    """`environment` 中引用了任务未提供变量的位置（§4.2）：provider 按 `param` 的配置值代入 `expr`。"""

    service: str
    var: str
    expr: str
    param: str
    default: str | None


class Finding(_Model):
    path: str
    kind: Literal["equivalent", "warn", "reject"]
    reason: str


class Published(_Model):
    host_path: str
    files_sha256: str


class Manifest(_Model):
    schema_: Annotated[int, Field(alias="schema")] = SCHEMA_VERSION
    task: str
    build_key: str
    agent_service: str
    units: dict[str, Unit]
    networks: dict[str, Network] = {}
    depends_on: dict[str, dict[str, Dependency]] = {}
    trial_volumes: list[TrialVolume] = []
    task_files: str | None = None  # 相对共享根目录；没有任务文件时为 None
    task_files_published: Published | None = None  # `flotilla publish` 之后写入（§4.7）
    runtime_params: list[RuntimeParam] = []
    external_units: list[str] = []
    findings: list[Finding] = []

    model_config = ConfigDict(extra="forbid", frozen=True, populate_by_name=True)

    def dump_json(self) -> str:
        return self.model_dump_json(by_alias=True, indent=2)

    @classmethod
    def load_json(cls, text: str | bytes) -> Manifest:
        manifest = cls.model_validate_json(text)
        if manifest.schema_ != SCHEMA_VERSION:
            raise ValueError(f"清单 schema 版本 {manifest.schema_} 不受支持（需要 {SCHEMA_VERSION}）")
        return manifest
