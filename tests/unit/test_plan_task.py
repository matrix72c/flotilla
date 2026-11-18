"""Planning a task directly: what `plan_task` refuses, and what it computes.

The claim that this produces the same trial as the release path is made by
`tests/contract/plan_equivalence.py`. What is stated here is what `plan_task` decides on its own:
which tasks it refuses, and the two things that changed shape when the release went away —
`task_files` pointing at the dataset, and a bind volume copied from where it lies.
"""

from __future__ import annotations

from collections.abc import Mapping
from pathlib import Path
from typing import Any

import pytest

from flotilla.compose.plan_task import NO_OVERLAY, AgentOverlay, PlanError, plan_task
from flotilla.compose.task import load_task
from flotilla.config import ResourceDefaults
from flotilla.core.plan import TaskSource
from flotilla.platform.base import Resources

APP = "r.io/ns/app@sha256:" + "a" * 64
PEER = "r.io/ns/peer@sha256:" + "b" * 64

ROOT_CONFIG: dict[str, Any] = {"User": "0:0", "WorkingDir": "/app", "Env": ["IMAGE_ONLY=from-image"]}


class Configs:
    def __init__(self, **overrides: Mapping[str, Any]) -> None:
        self.configs: dict[str, Mapping[str, Any]] = {APP: ROOT_CONFIG, PEER: ROOT_CONFIG, **overrides}
        self.reads: list[str] = []

    def image_config(self, pinned: str) -> Mapping[str, Any]:
        self.reads.append(pinned)
        return self.configs[pinned]


POLICY = ResourceDefaults.model_validate({"default": {"cpu": "1", "memory": "2Gi"}})


def task_dir(root: Path, *, toml: str = "", compose: str | None = None, files: Mapping[str, str] = {}) -> Path:
    path = root / "task"
    (path / "environment").mkdir(parents=True, exist_ok=True)
    (path / "task.toml").write_text(f'[environment]\ndocker_image = "{APP}"\n' + toml)
    if compose is not None:
        (path / "environment/docker-compose.yaml").write_text(compose)
    for relative, text in files.items():
        target = path / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(text)
    return path


def plan(path: Path, *, configs: Configs | None = None, key: str | None = "tasks/task", **kwargs: Any) -> Any:
    task = load_task(path)
    return plan_task(
        task,
        TaskSource(local=path, key=key),
        configs=configs or Configs(),
        resources=POLICY.for_services(task.name, {n: s.resources for n, s in task.project.services.items()}),
        **kwargs,
    )


def test_task_files_point_at_the_dataset_directory(tmp_path: Path) -> None:
    """Nothing is staged, so the mount source is the task directory the platform already has."""
    result = plan(task_dir(tmp_path), key="datasets/vulhub/task")
    assert result.task_files == "datasets/vulhub/task"
    assert result.task_files_root == "work"


def test_a_read_only_bind_is_mounted_where_it_lies(tmp_path: Path) -> None:
    """Nothing is copied: the mount names the bind source inside the published task directory."""
    path = task_dir(
        tmp_path,
        compose="services:\n  main:\n    volumes:\n      - ./conf:/etc/app:ro\n",
        files={"environment/conf/app.conf": "x\n"},
    )
    result = plan(path, key="datasets/vulhub/task")
    assert result.volumes == ()
    assert [(m.scope, m.key, m.target, m.read_only) for m in result.units["main"].mounts] == [
        ("task", "environment/conf", "/etc/app", True)
    ]


def test_a_writable_bind_becomes_the_trials_own_copy(tmp_path: Path) -> None:
    """A writer makes the group the trial's, so it is copied out of the task directory first."""
    path = task_dir(
        tmp_path,
        compose="services:\n  main:\n    volumes:\n      - ./conf:/etc/app\n",
        files={"environment/conf/app.conf": "x\n"},
    )
    result = plan(path)
    assert [(v.key, v.copy_from) for v in result.volumes] == [("binds/0", "environment/conf")]
    assert [(m.scope, m.key, m.target, m.read_only) for m in result.units["main"].mounts] == [
        ("trial", "binds/0", "/etc/app", False)
    ]


def test_a_named_volume_starts_empty(tmp_path: Path) -> None:
    path = task_dir(
        tmp_path,
        compose='services:\n  main:\n    volumes:\n      - "state:/var/state"\nvolumes:\n  state: {}\n',
    )
    result = plan(path)
    assert [(v.key, v.copy_from) for v in result.volumes] == [("state", None)]


def test_an_image_is_read_once_however_many_services_share_it(tmp_path: Path) -> None:
    path = task_dir(
        tmp_path,
        compose=f'services:\n  main: {{}}\n  peer:\n    image: {APP}\n    command: ["/bin/true"]\n',
    )
    configs = Configs()
    plan(path, configs=configs)
    # Two services, one digest: the caller's cache collapses the second read.
    assert set(configs.reads) == {APP}


def test_a_rejected_task_does_not_become_a_plan(tmp_path: Path) -> None:
    path = task_dir(tmp_path, compose="services:\n  main:\n    pid: host\n")
    with pytest.raises(PlanError, match=r"services\.main\.pid"):
        plan(path)


def test_a_service_running_as_a_named_user_is_refused(tmp_path: Path) -> None:
    """Resolving a name meant creating a container from the image to read its /etc/passwd."""
    path = task_dir(tmp_path, compose='services:\n  main:\n    user: "app"\n')
    with pytest.raises(PlanError, match="numeric uid"):
        plan(path)


def test_a_non_root_image_is_refused(tmp_path: Path) -> None:
    """The exec daemon is the sandbox entrypoint and needs a root image."""
    path = task_dir(tmp_path)
    with pytest.raises(PlanError, match="USER"):
        plan(path, configs=Configs(**{APP: {"User": "1000:1000"}}))


def test_a_numeric_compose_user_runs_as_itself(tmp_path: Path) -> None:
    path = task_dir(tmp_path, compose='services:\n  main:\n    user: "1000:1000"\n')
    result = plan(path)
    assert (result.units["main"].context.uid, result.units["main"].context.gid) == (1000, 1000)


def test_missing_resources_are_refused_rather_than_defaulted(tmp_path: Path) -> None:
    task = load_task(task_dir(tmp_path))
    with pytest.raises(PlanError, match="no resources resolved"):
        plan_task(task, TaskSource(local=tmp_path / "task"), configs=Configs(), resources={})


def test_an_unsized_tmpfs_takes_its_size_from_the_resolved_memory(tmp_path: Path) -> None:
    path = task_dir(tmp_path, compose="services:\n  main:\n    tmpfs:\n      - /scratch\n")
    task = load_task(path)
    result = plan_task(
        task,
        TaskSource(local=path, key="tasks/task"),
        configs=Configs(),
        resources={"main": Resources(cpu="1", memory="4Gi")},
    )
    mount = result.units["main"].tmpfs_mounts[0]
    assert mount.mount_path == "/scratch"
    assert mount.size_bytes is not None and mount.size_bytes > 0


def test_the_agent_overlay_extends_the_services_own_value(tmp_path: Path) -> None:
    path = task_dir(tmp_path, compose="services:\n  main:\n    environment:\n      PATH: /declared\n")
    result = plan(path, overlay=AgentOverlay(env={"PATH": "/overlay:${PATH}"}))
    assert result.units["main"].context.env["PATH"] == "/overlay:/declared"


def test_the_overlay_can_name_other_services(tmp_path: Path) -> None:
    path = task_dir(
        tmp_path,
        compose=f'services:\n  main: {{}}\n  peer:\n    image: {PEER}\n    command: ["/bin/true"]\n',
    )
    result = plan(path, overlay=AgentOverlay(env={"MARKER": "set"}, services=frozenset({"peer"})))
    assert result.units["peer"].context.env["MARKER"] == "set"
    assert "MARKER" not in result.units["main"].context.env


def test_an_overlay_on_an_unknown_service_is_refused(tmp_path: Path) -> None:
    path = task_dir(tmp_path)
    with pytest.raises(PlanError, match="does not define"):
        plan(path, overlay=AgentOverlay(env={"X": "1"}, services=frozenset({"absent"})))


def test_a_task_with_no_files_needs_no_key(tmp_path: Path) -> None:
    """A task whose services mount nothing has nothing to point at."""
    result = plan(task_dir(tmp_path), key=None)
    assert result.task_files is None
    assert result.volumes == ()


@pytest.mark.parametrize("key", ["/absolute", "../escape", "a/../b", ""])
def test_a_task_key_that_is_not_a_canonical_relative_path_is_refused(key: str) -> None:
    with pytest.raises(ValueError, match="canonical relative path"):
        TaskSource(local=Path("/tmp"), key=key)


def test_external_units_are_the_services_on_a_non_internal_network(tmp_path: Path) -> None:
    path = task_dir(
        tmp_path,
        compose=f"""services:
  main:
    networks: [front]
  peer:
    image: {PEER}
    command: ["/bin/true"]
    networks: [back]
networks:
  front: {{}}
  back:
    internal: true
""",
    )
    result = plan(path)
    assert result.external_units == frozenset({"main"})


def test_the_hosts_table_names_services_aliases_and_hostnames(tmp_path: Path) -> None:
    path = task_dir(
        tmp_path,
        compose=f"""services:
  main:
    networks: [back]
  peer:
    image: {PEER}
    command: ["/bin/true"]
    hostname: peer-host
    networks:
      back:
        aliases: [primary]
networks:
  back: {{}}
""",
    )
    result = plan(path)
    assert result.networks["back"].names == {
        "main": frozenset({"main"}),
        "peer": frozenset({"peer"}),
        "peer-host": frozenset({"peer"}),
        "primary": frozenset({"peer"}),
    }


def test_no_overlay_is_the_default(tmp_path: Path) -> None:
    path = task_dir(tmp_path, compose="services:\n  main:\n    environment:\n      DECLARED: value\n")
    assert plan(path, overlay=NO_OVERLAY).units["main"].context.env["DECLARED"] == "value"
