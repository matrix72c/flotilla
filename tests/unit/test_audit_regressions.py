"""End-to-end regressions for the engineering audit, using only isolated local inputs."""

from __future__ import annotations

from dataclasses import replace
from pathlib import Path

import pytest

from flotilla.compose.model import ImageMeta
from flotilla.compose.normalize import normalize
from flotilla.compose.plan_task import PlanError, synthesize_command
from flotilla.compose.task import load_task
from flotilla.config import ResourceDefaults
from flotilla.scan import scan
from flotilla.testing.planning import APP, FakeConfigs, _caps, _task, plan


def test_a_volume_name_that_is_a_path_never_reaches_storage(tmp_path: Path) -> None:
    """A volume key becomes a directory under the trial root, so a path in its name is refused."""
    decoy = tmp_path / "precious"
    decoy.mkdir()
    sentinel = decoy / "sentinel"
    sentinel.write_text("keep")
    for name in (str(decoy), "../precious", "..", "x/../../precious"):
        parsed = normalize({"services": {"main": {"image": APP}}, "volumes": {name: {}}})
        assert parsed.rejected
        assert sentinel.read_text() == "keep"


def test_sibling_prefix_symlink_is_rejected_before_build(tmp_path: Path) -> None:
    env = tmp_path / "environment"
    env.mkdir()
    outside = tmp_path / "environment-neighbor"
    outside.mkdir()
    (env / "escape").symlink_to(outside, target_is_directory=True)
    service: dict[str, object] = {
        "image": APP,
        "volumes": [{"type": "bind", "source": "./escape", "target": "/data"}],
    }
    result = scan("t", normalize({"services": {"main": service}}), _caps(), env_dir=str(env))
    assert result.status == "rejected"
    assert any("escapes" in f.reason for f in result.findings)


def test_string_command_and_explicit_empty_preserve_entrypoint() -> None:
    meta = ImageMeta(entrypoint=("/app/server",), cmd=("--old",))
    for value, expected in [
        (' --listen "hello world"', ("/app/server", "--listen", "hello world")),
        ("", ("/app/server",)),
        (None, ("/app/server", "--old")),
    ]:
        p = normalize({"services": {"main": {"image": APP, "command": value}}})
        assert synthesize_command(p.services["main"], meta) == expected


@pytest.mark.parametrize(
    "field",
    [
        {"privileged": "false"},
        {"healthcheck": {"disable": "false"}},
        {"volumes": [{"type": "volume", "source": "data", "target": "/d", "volume": {"subpath": "ignored"}}]},
        {"volumes": [{"type": "volume", "source": "data", "target": "/d", "read_only": "false"}]},
        {"depends_on": {"db": "healthy"}},
        {"networks": {"default": "bad"}},
        {"build": {"args": 123}},
    ],
)
def test_invalid_nested_fields_are_visible_rejections(field: dict[str, object]) -> None:
    assert normalize({"services": {"main": {"image": APP, **field}}, "volumes": {"data": {}}}).rejected


def test_tmpfs_is_not_derived_without_approximating_container_identity(tmp_path: Path) -> None:
    path = _task(
        tmp_path,
        "tmpfs",
        compose=f"""services:
  main:
    image: {APP}
    tmpfs: /cache
    volumes:
      - {{type: tmpfs, target: /scratch}}
""",
    )
    task = load_task(path)
    assert scan(task.name, task.project, replace(_caps(), native_tmpfs=True)).status == "accepted"
    result = plan(task)
    # Mount-point directories are runtime responsibilities: nothing is derived from the image.
    assert [m.mount_path for m in result.units["main"].tmpfs_mounts] == ["/cache", "/scratch"]
    assert result.networks["default"].names == {"main": frozenset({"main"})}
    compose = task.env_dir / "docker-compose.yaml"
    compose.write_text(compose.read_text().replace(f"image: {APP}", f"image: {APP}\n    container_name: agent-alias"))
    names = plan(load_task(path)).networks["default"].names
    assert names == {"agent-alias": frozenset({"main"}), "main": frozenset({"main"})}


def test_resource_precedence_and_harbor_declaration(tmp_path: Path) -> None:
    path = _task(tmp_path, "resources", compose=f"services:\n  main:\n    image: {APP}\n")
    (path / "task.toml").write_text("[environment]\ncpus=8\nmemory_mb=16384\n")
    task = load_task(path)
    assert task.project.services["main"].resources == {"cpu": "8", "memory": "16384Mi"}
    config = ResourceDefaults.model_validate(
        {
            "default": {"cpu": "1", "memory": "1Gi"},
            "services": {"main": {"cpu": "2", "memory": "2Gi"}},
            "tasks": {"resources": {"services": {"main": {"cpu": "4", "memory": "4Gi"}}}},
        }
    )
    declared = {name: svc.resources for name, svc in task.project.services.items()}
    # The deployment's task-scoped policy wins over what the task declares.
    assert config.for_services(task.name, declared)["main"].cpu == "4"
    assert config.for_services(task.name, declared)["main"].memory == "4Gi"
    assert config.effective("other", "db", {"cpu": "8"}) == {"cpu": "8", "memory": "1Gi"}
    # With no policy for this task, the declaration is what the plan runs with.
    assert plan(task, resources=ResourceDefaults()).units["main"].resources.cpu == "8"


def test_a_named_identity_is_refused_rather_than_looked_up_in_the_image(tmp_path: Path) -> None:
    """Resolving `app:writers` meant creating a container and reading its passwd and group files."""
    path = _task(tmp_path, "named", compose=f'services:\n  main:\n    image: {APP}\n    user: "app:writers"\n')
    with pytest.raises(PlanError, match="numeric uid"):
        plan(load_task(path))
    numeric = _task(tmp_path, "numeric", compose=f'services:\n  main:\n    image: {APP}\n    user: "123:456"\n')
    context = plan(load_task(numeric)).units["main"].context
    assert (context.uid, context.gid) == (123, 456)
    # A non-root image USER is refused too: the exec daemon is the sandbox entrypoint.
    with pytest.raises(PlanError, match="USER"):
        plan(load_task(numeric), configs=FakeConfigs(user="1000:1000"))


def test_resource_policy_is_task_scoped() -> None:
    base = ResourceDefaults()
    other = ResourceDefaults.model_validate(
        {"tasks": {"different": {"services": {"db": {"cpu": "8", "memory": "8Gi"}}}}}
    )
    assert base.effective("mine", "db", {}) == other.effective("mine", "db", {})
    assert base.effective("different", "db", {}) != other.effective("different", "db", {})


def test_client_configuration_snapshot_cannot_be_mutated_via_public_accessor() -> None:
    from flotilla import Flotilla
    from flotilla.testing.config import config

    original = config(runtime_params={"TOKEN": "original"})
    fleet = Flotilla(deployment=original)
    original.runtime_params["TOKEN"] = "changed"
    snapshot = fleet.config
    snapshot.runtime_params["TOKEN"] = "also-changed"
    assert fleet.config.runtime_params["TOKEN"] == "original"


def test_rl_corpus_memory_alias_is_applied_not_silently_defaulted(tmp_path: Path) -> None:
    path = _task(tmp_path, "rl-memory", compose=f"services:\n  main:\n    image: {APP}\n")
    metadata = path / "task.toml"
    metadata.write_text('[environment]\nmemory="12G"\n')
    assert load_task(path).project.services["main"].resources["memory"] == "12Gi"
    metadata.write_text('[environment]\nmemory="12G"\nmemory_mb=2048\n')
    with pytest.raises(ValueError, match="only one"):
        load_task(path)
