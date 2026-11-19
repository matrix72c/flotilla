"""Fixtures for planning a task directory without a registry: fake image configs and task trees.

`plan_task` reads one thing from outside the task directory -- each image's config blob. These
fakes supply it, so a test can plan a real task tree with no network and no Docker.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from pathlib import Path
from typing import Any

from flotilla.compose.plan_task import plan_task
from flotilla.compose.task import HarborTask
from flotilla.config import FlotillaConfig, ResourceDefaults
from flotilla.core.plan import TaskSource, TrialPlan
from flotilla.integrations.harbor_source import task_environments
from flotilla.platform.base import Capabilities

DIGEST = "sha256:" + "ab" * 32
DIGEST2 = "sha256:" + "cd" * 32
#: Digest-pinned images for fixtures; the dialect refuses a tag.
APP = f"r.io/ns/app@{DIGEST}"
PEER = f"r.io/ns/peer@{DIGEST2}"


class FakeConfigs:
    """Image configs by digest, read-only like the real registry client, counting its reads."""

    def __init__(self, *, user: str = "0:0", overrides: Mapping[str, Mapping[str, Any]] = {}) -> None:
        self.user = user
        self.overrides = dict(overrides)
        self.reads: list[str] = []

    def image_config(self, pinned: str) -> Mapping[str, Any]:
        self.reads.append(pinned)
        if pinned in self.overrides:
            return self.overrides[pinned]
        return {"Cmd": ["/bin/bash"], "WorkingDir": "/app", "User": self.user}


def _caps() -> Capabilities:
    return Capabilities(
        max_label_value_len=63,
        list_visibility_s=30.0,
        max_ttl_seconds=21600,
        link=True,
        link_udp=True,
        link_max_members=10,
        inbound_isolation=True,
        external_forms=frozenset({"none", "allowlist"}),
        implicit_egress=(),
        internal_address="exec",
        shared_volume=True,
        volume_file=True,
        no_auto_mount=True,
        exec_auth=True,
        privileged_runtime=False,
        devices=frozenset(),
    )


def _task(root: Path, name: str, *, toml: str = "", compose: str | None = None, **files: str) -> Path:
    path = root / name
    (path / "environment").mkdir(parents=True)
    (path / "task.toml").write_text(f'[task]\nname = "ds/{name}"\n{toml}')
    if compose is not None:
        (path / "environment" / "docker-compose.yaml").write_text(compose)
    for rel, text in files.items():
        target = path / "environment" / rel.replace("__", "/")
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(text)
    return path


POLICY = ResourceDefaults.model_validate({"default": {"cpu": "1", "memory": "2Gi"}})


#: The read-only input root `flotilla.testing.config` declares for published datasets.
DATASET_ROOT = "datasets"


def plan(
    task: HarborTask,
    *,
    configs: Any = None,
    resources: ResourceDefaults | None = None,
    key: str | None = "tasks/task",
    root: str = DATASET_ROOT,
    **kwargs: Any,
) -> TrialPlan:
    """Plan one already-read environment with the fakes above."""
    policy = resources or POLICY
    declared = {name: svc.resources for name, svc in task.project.services.items()}
    return plan_task(
        task,
        TaskSource(local=task.path, key=key, root=root),
        configs=configs or FakeConfigs(),
        resources=policy.for_services(task.name, declared),
        **kwargs,
    )


def planned_task(
    path: Path,
    cfg: FlotillaConfig | None = None,
    *,
    environments: Callable[..., Sequence[HarborTask]] = task_environments,
    **kwargs: Any,
) -> dict[str, TrialPlan]:
    """Plan a task directory into one plan per role, in `main`, `verifier` order."""
    policy = cfg.resources if cfg is not None else POLICY
    roles = environments(path)
    names = ["main"] if len(roles) == 1 else ["main", "verifier"]
    return {
        role: plan(task, resources=policy, key=f"tasks/{path.name}", **kwargs)
        for role, task in zip(names, roles, strict=True)
    }


def planned_environment(path: Path, cfg: FlotillaConfig | None = None, **kwargs: Any) -> TrialPlan:
    """The main environment of `planned_task(path, cfg)`."""
    return planned_task(path, cfg, **kwargs)["main"]
