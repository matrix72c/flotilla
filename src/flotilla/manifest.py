"""Versioned prepared-environment schema: the flotilla-owned `env` payload of a prepared row."""

from __future__ import annotations

from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from flotilla.platform.base import TaskDevice, TmpfsMount

SCHEMA_VERSION = 9

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
    shm_size_bytes: Annotated[int, Field(gt=0)] | None = None
    devices: list[TaskDevice] = []  # sorted, unique; run on the privileged runtime

    @model_validator(mode="after")
    def _sorted_devices(self) -> Unit:
        if self.devices != sorted(set(self.devices)):
            raise ValueError("devices must be sorted and unique")
        return self


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
    copy_from: str | None = None


class Finding(_Model):
    path: str
    kind: Literal["equivalent", "warn", "reject"]
    reason: str


class Manifest(_Model):
    schema_: Annotated[int, Field(alias="schema")] = SCHEMA_VERSION
    task: str
    agent_service: str
    units: dict[str, Unit]
    networks: dict[str, Network] = {}
    depends_on: dict[str, dict[str, Dependency]] = {}
    trial_volumes: list[TrialVolume] = []
    task_files: str | None = None  # relative to the shared root; None without task files
    external_units: list[str] = []
    findings: list[Finding] = []

    model_config = ConfigDict(extra="forbid", frozen=True, populate_by_name=True)

    @model_validator(mode="before")
    @classmethod
    def _upgrade(cls, data: object) -> object:
        """Load an older manifest by dropping what this version no longer writes.

        Each step drops a field when it is empty, so a release prepared against the
        older schema keeps loading, and refuses it when populated, because that
        manifest carries state this version cannot honor; prepare its release again.

        Schema 7 carried two such fields. `runtime_params` held deferred
        expressions; the Compose config adapter now resolves every reference at
        preparation. `task_files_published` recorded the storage root of the removed
        file-publication path, which a native release never used.

        Schema 8 carried `trial_volumes[].seed`: a tar of the initial content Docker
        would have copied out of the image into an empty named volume. Flotilla no
        longer emulates that copy-up, so a trial volume starts empty; `owner` gives
        its root identity.
        """
        if not isinstance(data, dict):
            return data
        version = data.get("schema", data.get("schema_"))
        if version not in (7, 8):  # anything else is current, or too old to upgrade
            return data
        upgraded = dict(data)
        if version == 7:
            if upgraded.pop("runtime_params", None):
                raise ValueError("manifest defers runtime parameters; prepare its release again")
            if upgraded.pop("task_files_published", None):
                raise ValueError("manifest records published task files; prepare its release again")
            version = 8
        if "trial_volumes" in upgraded:
            volumes = [dict(volume) if isinstance(volume, dict) else volume for volume in upgraded["trial_volumes"]]
            if any(isinstance(volume, dict) and volume.get("seed") for volume in volumes):
                raise ValueError("manifest seeds a trial volume from its image; prepare its release again")
            for volume in volumes:
                if isinstance(volume, dict):
                    volume.pop("seed", None)
            upgraded["trial_volumes"] = volumes
        upgraded["schema"] = SCHEMA_VERSION
        upgraded.pop("schema_", None)
        return upgraded

    def dump_json(self) -> str:
        return self.model_dump_json(by_alias=True, indent=2)

    @classmethod
    def load_json(cls, text: str | bytes) -> Manifest:
        return cls._checked(cls.model_validate_json(text))

    @classmethod
    def load(cls, data: object) -> Manifest:
        return cls._checked(cls.model_validate(data))

    @staticmethod
    def _checked(manifest: Manifest) -> Manifest:
        if manifest.schema_ != SCHEMA_VERSION:
            raise ValueError(f"unsupported manifest schema version {manifest.schema_} (expected {SCHEMA_VERSION})")
        return manifest
