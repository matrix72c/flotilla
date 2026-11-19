"""What a graded attempt needs beyond its Compose project: verification and artifacts.

These used to be a persisted protocol — a `Task` written into a release, read back by a trainer,
with a schema version and migrations. They are computed from the task's own `task.toml` now
(`flotilla.compose.verification`), so what is left is the shape of the two values and the path
rules they must obey.

The path validators stay because the values still name directories a trial mounts. A key that
escapes its root, or an output path that is not canonical, is a task input error — and it has to
be caught before anything is created, not after a sandbox is already running with the wrong mount.
"""

from __future__ import annotations

from collections.abc import Mapping

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from flotilla.models.filesystem import overlaps
from flotilla.models.paths import absolute_path, relative_path
from flotilla.models.plan import TrialPlan


class Model(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class Artifact(Model):
    """A private output directory the agent writes, handed to the verification role afterwards."""

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
    """How an attempt is graded: what runs, where, and which outputs are collected."""

    role: str = "main"
    service: str | None = None
    command: tuple[str, ...] = Field(min_length=1)
    #: Where the verification script lives, relative to the task directory.
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


def check_against_plans(
    verification: Verification | None,
    artifacts: tuple[Artifact, ...],
    *,
    plans: Mapping[str, TrialPlan],
) -> None:
    """Refuse a verification or artifact the task's own plans cannot support.

    `Task` checked this as a model validator, because the whole thing was one persisted document.
    The values are computed separately now, so the cross-check is explicit and runs where both
    halves are in hand -- and it must still run, because one of these rules is what keeps the
    hidden tests hidden.
    """
    names: set[str] = set()
    for artifact in artifacts:
        if artifact.name in names:
            raise ValueError(f"duplicate artifact name: {artifact.name}")
        names.add(artifact.name)
        if artifact.role not in plans:
            raise ValueError(f"artifact {artifact.name} names unknown role {artifact.role!r}")
        if artifact.service and artifact.service not in plans[artifact.role].units:
            raise ValueError(f"artifact {artifact.name} names unknown service {artifact.service!r}")
    if verification is None:
        return
    if verification.role not in plans:
        raise ValueError(f"verification names unknown role {verification.role!r}")
    if verification.service and verification.service not in plans[verification.role].units:
        raise ValueError(f"verification names unknown service {verification.service!r}")
    if verification.tests is None:
        return
    # The tests are staged only after the agent is done. If anything the agent can reach mounts
    # them, or a volume is initialized from them, the answers are inside the task it is solving.
    for plan in plans.values():
        for name, unit in plan.units.items():
            for mount in unit.mounts:
                if mount.scope == "task" and overlaps(mount.key, verification.tests):
                    raise ValueError(f"{name} mounts the hidden tests as a task input: {mount.key}")
        for volume in plan.volumes:
            if volume.copy_from and overlaps(volume.copy_from, verification.tests):
                raise ValueError(f"volume {volume.key} is initialized from the hidden tests")
