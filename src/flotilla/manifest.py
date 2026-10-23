"""Versioned subprocess IPC schema; not a public task-input format."""

from __future__ import annotations

from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field

from flotilla.platform.base import TmpfsMount

SCHEMA_VERSION = 5

Condition = Literal["service_started", "service_healthy", "service_completed_successfully"]
RestartPolicy = Literal["no", "always", "unless-stopped", "on-failure"]


class _Model(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class Resources(_Model):
    """Task declarations only; absent fields are resolved by the runtime deployment."""

    cpu: str | None = None
    memory: str | None = None


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
    image: str  # repo@sha256:... (digest-pinned)
    resources: Resources
    context: Context
    command: list[str] | None  # main process argv; None means no main process
    healthcheck: Healthcheck | None = None
    restart: RestartPolicy = "no"
    mounts: list[Mount] = []
    privileged: bool = False
    readonly_rootfs: bool = False
    tmpfs_mounts: list[TmpfsMount] = []


class Network(_Model):
    internal: bool
    members: list[str]
    names: dict[str, list[str]]  # name -> services


class Dependency(_Model):
    condition: Condition
    required: bool = True


class TrialVolume(_Model):
    key: str
    owner: tuple[int, int] | None = None
    seed: str | None = None
    copy_from: str | None = None


class RuntimeParam(_Model):
    """An `environment` entry referencing a variable the task lacks: the provider substitutes `param` into `expr`."""

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
    storage_root: str  # `StorageSettings.root` at publication time
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
    task_files: str | None = None  # relative to the shared root; None without task files
    task_files_published: Published | None = None  # written by file publication
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
            raise ValueError(f"unsupported manifest schema version {manifest.schema_} (expected {SCHEMA_VERSION})")
        return manifest
