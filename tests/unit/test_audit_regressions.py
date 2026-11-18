"""End-to-end regressions for the engineering audit, using only isolated local inputs."""

from __future__ import annotations

from dataclasses import replace
from pathlib import Path

import pytest

from flotilla.build.driver import build_task
from flotilla.build.files import ExportError, export_task_files
from flotilla.build.meta import ImageAccounts, parse_user
from flotilla.compose.manifest_gen import synthesize_command
from flotilla.compose.model import ImageMeta, Project, Service, VolumeMount
from flotilla.compose.normalize import normalize
from flotilla.config import ResourceDefaults
from flotilla.manifest import Manifest, Resources
from flotilla.scan import scan
from flotilla.testing.build import _caps, _ctx, _task
from flotilla.testing.compose import offline_task as load_task


def test_volume_name_never_controls_recursive_deletion(tmp_path: Path) -> None:
    decoy = tmp_path / "precious"
    decoy.mkdir()
    sentinel = decoy / "sentinel"
    sentinel.write_text("keep")
    for name in (str(decoy), "../precious", "..", "x/../../precious"):
        services = {
            s: Service(s, image="r.io/a", volumes=(VolumeMount("volume", name, "/data"),)) for s in ("main", "db")
        }
        project = Project(services, {}, frozenset({name}))
        with pytest.raises(ExportError, match="unsafe volume"):
            export_task_files(project, tmp_path, tmp_path / "files")
        parsed = normalize({"services": {"main": {"image": "r.io/a"}}, "volumes": {name: {}}})
        assert parsed.rejected
        assert sentinel.read_text() == "keep"


def test_sibling_prefix_symlink_is_rejected_before_build(tmp_path: Path) -> None:
    env = tmp_path / "environment"
    env.mkdir()
    outside = tmp_path / "environment-neighbor"
    outside.mkdir()
    (env / "escape").symlink_to(outside, target_is_directory=True)
    service: dict[str, object] = {
        "image": "r.io/a",
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
        p = normalize({"services": {"main": {"image": "r.io/a", "command": value}}})
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
    assert normalize({"services": {"main": {"image": "r.io/a", **field}}, "volumes": {"data": {}}}).rejected


def test_tmpfs_is_not_derived_without_approximating_container_identity(tmp_path: Path) -> None:
    task = load_task(
        _task(
            tmp_path,
            "tmpfs",
            compose="""services:
  main:
    image: r.io/a
    tmpfs: /cache
    volumes:
      - {type: tmpfs, target: /scratch}
""",
        )
    )
    ctx = _ctx()
    outcome = build_task(task, tmp_path / "out", replace(ctx, caps=replace(ctx.caps, native_tmpfs=True)))
    assert outcome.status == "built", outcome.reason
    # Mount-point directories are runtime responsibilities: no derivation build.
    assert outcome.manifest_path
    manifest = Manifest.load_json(outcome.manifest_path.read_bytes())
    assert [m.mount_path for m in manifest.units["main"].tmpfs_mounts] == ["/cache", "/scratch"]
    assert manifest.networks["default"].names == {"main": ["main"]}
    compose = task.env_dir / "docker-compose.yaml"
    compose.write_text(compose.read_text().replace("image: r.io/a", "image: r.io/a\n    container_name: agent-alias"))
    aliased = build_task(
        load_task(task.path), tmp_path / "aliased", replace(ctx, caps=replace(ctx.caps, native_tmpfs=True))
    )
    assert aliased.status == "built", aliased.reason
    assert aliased.manifest_path
    names = Manifest.load_json(aliased.manifest_path.read_bytes()).networks["default"].names
    assert names == {"agent-alias": ["main"], "main": ["main"]}


def test_resource_precedence_and_harbor_declaration(tmp_path: Path) -> None:
    path = _task(tmp_path, "resources", compose="services:\n  main:\n    image: r.io/a\n")
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
    outcome = build_task(task, tmp_path / "out", _ctx())
    assert outcome.manifest_path, outcome.reason
    manifest = Manifest.load_json(outcome.manifest_path.read_bytes())
    assert manifest.units["main"].resources == Resources(cpu="8", memory="16384Mi")
    assert config.for_manifest(manifest)["main"].cpu == "4"
    assert config.for_manifest(manifest)["main"].memory == "4Gi"
    assert config.effective("other", "db", {"cpu": "8"}) == {"cpu": "8", "memory": "1Gi"}


def test_image_user_and_group_have_distinct_namespaces() -> None:
    accounts = ImageAccounts("app:x:123:456::/:/bin/sh\n", "app:x:789:\nwriters:x:42:\n")
    assert parse_user("123", lookup=accounts.user) == (123, 456)
    assert parse_user("999", lookup=accounts.user) == (999, 0)
    assert parse_user("app:writers", lookup=accounts.user, group_lookup=accounts.group) == (123, 42)


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
    path = _task(tmp_path, "rl-memory", compose="services:\n  main:\n    image: r.io/a\n")
    metadata = path / "task.toml"
    metadata.write_text('[environment]\nmemory="12G"\n')
    assert load_task(path).project.services["main"].resources["memory"] == "12Gi"
    metadata.write_text('[environment]\nmemory="12G"\nmemory_mb=2048\n')
    with pytest.raises(ValueError, match="only one"):
        load_task(path)
