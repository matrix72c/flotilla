"""Versioned, portable task protocol. No importer or runtime dependencies."""

from __future__ import annotations

import json
import posixpath
import re
from typing import Any, Final, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from flotilla.manifest import SCHEMA_VERSION, Manifest
from flotilla.prepared import PreparedEnvironment

PROTOCOL_VERSION: Final = 2
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
    schema_version: Literal[2] = PROTOCOL_VERSION
    key: str = Field(min_length=1)
    instruction: Literal["instruction.md"] = "instruction.md"
    roles: dict[str, Manifest]
    verification: Verification | None = None
    artifacts: tuple[Artifact, ...] = ()
    provenance: dict[str, Any] = {}
    parameters: dict[str, Any] = {}

    @model_validator(mode="after")
    def consistent(self) -> Task:
        if "main" not in self.roles or set(self.roles) - {"main", "verifier"}:
            raise ValueError("roles require main and optional verifier")
        relative_path(self.key)
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


class DatasetRow(Model):
    """One selected task: `release` names its deployment binding, `task` its directory under `tasks/`."""

    release: str = Field(min_length=1)
    task: str
    annotations: dict[str, Any] = {}
    parameters: dict[str, Any] = {}

    _path = field_validator("task")(relative_path)
