"""Versioned, portable task protocol. No importer or runtime dependencies."""

from __future__ import annotations

import hashlib
import json
import posixpath
import re
from typing import Any, Final, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from flotilla.manifest import SCHEMA_VERSION, Manifest
from flotilla.prepared import PreparedEnvironment

PROTOCOL_VERSION: Final = 1
_DIGEST = re.compile(r"[0-9a-f]{64}")
_IMAGE = re.compile(r"[^\s@]+@sha256:[0-9a-f]{64}")


def relative_path(value: str) -> str:
    if not value or any(p in ("", ".", "..") for p in value.split("/")):
        raise ValueError(f"expected canonical relative path: {value!r}")
    if "\\" in value or any(ord(c) < 32 or ord(c) == 127 for c in value):
        raise ValueError("paths cannot contain backslashes or control characters")
    return value


def absolute_path(value: str) -> str:
    if not value.startswith("/") or value.startswith("//") or posixpath.normpath(value) != value:
        raise ValueError("expected canonical absolute path")
    relative_path(value[1:])
    return value


def canonical(value: Any) -> bytes:
    if isinstance(value, BaseModel):
        value = value.model_dump(mode="json", by_alias=True)
    return json.dumps(value, sort_keys=True, ensure_ascii=False, separators=(",", ":"), allow_nan=False).encode()


def digest(value: Any) -> str:
    return hashlib.sha256(canonical(value)).hexdigest()


class Model(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class Artifact(Model):
    """A private output directory, explicitly handed to the verification role."""

    name: str
    role: str = "main"
    service: str | None = None
    target: str
    verifier_target: str | None = None

    _target = field_validator("target")(absolute_path)

    @field_validator("name")
    @classmethod
    def name_valid(cls, value: str) -> str:
        relative_path(value)
        if "/" in value:
            raise ValueError("artifact name must be one segment")
        return value

    @field_validator("verifier_target")
    @classmethod
    def verifier_path(cls, value: str | None) -> str | None:
        return absolute_path(value) if value else value


class Verification(Model):
    role: str = "main"
    service: str | None = None
    command: tuple[str, ...] = Field(min_length=1)
    tests: str | None = None
    tests_target: str = "/tests"
    output_paths: dict[str, str] = {}
    optional_output_paths: dict[str, str] = {}
    require_output: bool = False
    output_dir: str = "/logs/verifier"
    env: dict[str, str] = {}
    uid: int | None = Field(default=None, ge=0)
    gid: int | None = Field(default=None, ge=0)
    timeout_s: float = Field(default=600, gt=0, allow_inf_nan=False)

    _target = field_validator("tests_target", "output_dir")(absolute_path)

    @model_validator(mode="after")
    def identity(self) -> Verification:
        if (self.uid is None) != (self.gid is None):
            raise ValueError("verification uid and gid must be specified together")
        if set(self.output_paths) & set(self.optional_output_paths):
            raise ValueError("duplicate verification output names")
        return self

    @field_validator("tests")
    @classmethod
    def tests_path(cls, value: str | None) -> str | None:
        return relative_path(value) if value else value

    @field_validator("output_paths", "optional_output_paths")
    @classmethod
    def outputs(cls, value: dict[str, str]) -> dict[str, str]:
        for path in value.values():
            absolute_path(path)
        return value


class Task(Model):
    schema_version: Literal[1] = PROTOCOL_VERSION
    key: str = Field(min_length=1)
    instruction: Literal["instruction.md"] = "instruction.md"
    roles: dict[str, Manifest]
    assets: tuple[str, ...] = ()
    verification: Verification | None = None
    artifacts: tuple[Artifact, ...] = ()
    provenance: dict[str, Any] = {}
    parameters: dict[str, Any] = {}

    @model_validator(mode="after")
    def consistent(self) -> Task:
        if "main" not in self.roles or set(self.roles) - {"main", "verifier"}:
            raise ValueError("roles require main and optional verifier")
        if len(self.assets) != len(set(self.assets)):
            raise ValueError("duplicate assets")
        for asset in self.assets:
            relative_path(asset)
            if not re.fullmatch(r"assets/[0-9a-f]{64}", asset):
                raise ValueError("asset roots must be content addressed")
        refs: list[str] = []
        for manifest in self.roles.values():
            if manifest.schema_ != SCHEMA_VERSION:
                raise ValueError("unsupported nested manifest version")
            PreparedEnvironment(manifest)
            if manifest.task_files is not None or manifest.task_files_published is not None:
                raise ValueError("native manifests cannot contain publication paths")
            for unit in manifest.units.values():
                if not _IMAGE.fullmatch(unit.image):
                    raise ValueError("native images must be digest pinned")
                if unit.context.uid < 0 or unit.context.gid < 0:
                    raise ValueError("application uid/gid must be nonnegative")
                for mount in unit.mounts:
                    absolute_path(mount.target)
                    relative_path(mount.key)
                    if mount.scope == "task":
                        if not mount.read_only:
                            raise ValueError("static mounts must be read-only")
                        refs.append(mount.key)
            for volume in manifest.trial_volumes:
                refs.extend(p for p in (volume.seed, volume.copy_from) if p is not None)
        if self.verification:
            v = self.verification
            if v.role not in self.roles or (v.service and v.service not in self.roles[v.role].units):
                raise ValueError("unknown verification role/service")
            if v.tests:
                refs.append(v.tests)
                # Tests are staged only after agent execution, never mounted as ordinary input.
                for manifest in self.roles.values():
                    for unit in manifest.units.values():
                        for mount in unit.mounts:
                            if mount.scope == "task" and overlaps(mount.key, v.tests):
                                raise ValueError("hidden tests cannot be mounted in task inputs")
                    for volume in manifest.trial_volumes:
                        if any(overlaps(p, v.tests) for p in (volume.seed, volume.copy_from) if p):
                            raise ValueError("hidden tests cannot initialize task inputs")
        for ref in refs:
            relative_path(ref)
            if not any(ref == root or ref.startswith(root + "/") for root in self.assets):
                raise ValueError(f"undeclared asset reference: {ref}")
        names: set[str] = set()
        for artifact in self.artifacts:
            if artifact.name in names:
                raise ValueError("duplicate artifact name")
            names.add(artifact.name)
            if artifact.role not in self.roles or (
                artifact.service and artifact.service not in self.roles[artifact.role].units
            ):
                raise ValueError("unknown artifact role/service")
        return self


def overlaps(a: str, b: str) -> bool:
    return a == b or a.startswith(b + "/") or b.startswith(a + "/")


class FileEntry(Model):
    kind: Literal["file", "directory", "symlink"]
    mode: int = Field(ge=0, le=0o7777)
    uid: int = Field(default=0, ge=0)
    gid: int = Field(default=0, ge=0)
    size: int = Field(default=0, ge=0)
    sha256: str | None = None
    target: str | None = None


class AssetRecord(Model):
    digest: str
    entries: dict[str, FileEntry]


class TaskRecord(Model):
    task_ref: str
    task_digest: str
    instruction_digest: str
    assets: tuple[str, ...]


class ReleaseManifest(Model):
    schema_version: Literal[1] = PROTOCOL_VERSION
    release_id: str
    tasks: dict[str, TaskRecord]
    assets: dict[str, AssetRecord]

    def identity(self) -> str:
        return digest(self.model_dump(mode="json", exclude={"release_id"}))


class DatasetRow(Model):
    release_id: str
    task_ref: str
    task_digest: str
    annotations: dict[str, Any] = {}
    parameters: dict[str, Any] = {}

    _path = field_validator("task_ref")(relative_path)

    @field_validator("release_id", "task_digest")
    @classmethod
    def valid_digest(cls, value: str) -> str:
        if not _DIGEST.fullmatch(value):
            raise ValueError("expected sha256 hex identity")
        return value
