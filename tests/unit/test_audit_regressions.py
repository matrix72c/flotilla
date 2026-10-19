"""End-to-end regressions for the engineering audit, using only isolated local inputs."""

from __future__ import annotations

import tarfile
from dataclasses import replace
from pathlib import Path

import pytest

from flotilla.build.driver import build_task
from flotilla.build.files import ExportError, export_task_files
from flotilla.build.meta import ImageAccounts, parse_user
from flotilla.compose.manifest_gen import build_manifest, synthesize_command
from flotilla.compose.model import ImageMeta, Project, Service, VolumeMount
from flotilla.compose.normalize import normalize
from flotilla.compose.task import load_task
from flotilla.config import ResourceDefaults
from flotilla.manifest import Manifest, Resources
from flotilla.runtime.planning import to_plan
from flotilla.scan import scan
from tests.support.build import FakeBuilder, FakeExport, _caps, _ctx, _task


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
        parsed = normalize({"services": {"main": {"image": "r.io/a"}}, "volumes": {name: {}}}, {})
        assert parsed.rejected
        assert sentinel.read_text() == "keep"


def test_seed_output_symlink_cannot_escape(tmp_path: Path) -> None:
    outside = tmp_path / "outside"
    outside.mkdir()
    sentinel = outside / "keep"
    sentinel.write_text("safe")
    files = tmp_path / "files"
    files.mkdir()
    (files / "seeds").symlink_to(outside, target_is_directory=True)
    services = {s: Service(s, volumes=(VolumeMount("volume", "data", "/data"),)) for s in ("main", "db")}
    with pytest.raises(ValueError, match="escapes"):
        export_task_files(
            Project(services, {}, frozenset({"data"})),
            tmp_path,
            files,
            images={"main": "a", "db": "b"},
            image_export=FakeExport({"/data": {"x": "y"}}),
        )
    assert list(outside.iterdir()) == [sentinel]


@pytest.mark.parametrize("kind", ["bind", "build"])
def test_sibling_prefix_symlink_is_rejected_before_build(tmp_path: Path, kind: str) -> None:
    env = tmp_path / "environment"
    env.mkdir()
    outside = tmp_path / "environment-neighbor"
    outside.mkdir()
    (outside / "Dockerfile").write_text("FROM r.io/a")
    (env / "escape").symlink_to(outside, target_is_directory=True)
    service = (
        {"image": "r.io/a", "build": {"context": "escape"}}
        if kind == "build"
        else {"image": "r.io/a", "volumes": [{"type": "bind", "source": "./escape", "target": "/data"}]}
    )
    result = scan("t", normalize({"services": {"main": service}}, {}), _caps(), env_dir=str(env))
    assert result.status == "rejected"
    assert any("escapes" in f.reason for f in result.findings)


def test_string_command_and_explicit_empty_preserve_entrypoint() -> None:
    meta = ImageMeta(entrypoint=("/app/server",), cmd=("--old",))
    for value, expected in [
        (' --listen "hello world"', ("/app/server", "--listen", "hello world")),
        ("", ("/app/server",)),
        (None, ("/app/server", "--old")),
    ]:
        p = normalize({"services": {"main": {"image": "r.io/a", "command": value}}}, {})
        assert synthesize_command(p.services["main"], meta) == expected


@pytest.mark.parametrize(
    "environment",
    [
        {"ENDPOINT": "${KNOWN}/$${literal}/${HOST}:${PORT:-80}"},
        ["ENDPOINT=${KNOWN}/$${literal}/${HOST}:${PORT:-80}"],
    ],
)
def test_runtime_interpolation_preserves_all_references_and_literal_dollars(environment: object) -> None:
    p = normalize({"services": {"main": {"image": "r.io/a", "environment": environment}}}, {"KNOWN": "build$value"})
    m = build_manifest(
        task="t",
        build_key="a" * 64,
        project=p,
        images={"main": "r.io/a"},
        meta={"main": ImageMeta()},
        resources={"main": Resources(cpu="1", memory="1Gi")},
    )
    assert {r.param for r in m.runtime_params} == {"HOST", "PORT"}
    plan = to_plan(m, runtime_params={"HOST": "db", "PORT": ""})
    assert plan.units["main"].context.env["ENDPOINT"] == "build$value/${literal}/db:80"


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
    assert normalize({"services": {"main": {"image": "r.io/a", **field}}, "volumes": {"data": {}}}, {}).rejected


def test_tmpfs_is_derived_and_container_alias_is_written(tmp_path: Path) -> None:
    task = load_task(
        _task(
            tmp_path,
            "tmpfs",
            compose="""services:
  main:
    image: r.io/a
    container_name: agent-alias
    tmpfs: /cache
    volumes:
      - {type: tmpfs, target: /scratch}
""",
        )
    )
    builder = FakeBuilder()
    outcome = build_task(task, tmp_path / "out", replace(_ctx(), builder=builder))
    assert outcome.status == "built", outcome.reason
    assert builder.requests and "/cache" in str(builder.requests[-1].dockerfile_text)
    assert "/scratch" in str(builder.requests[-1].dockerfile_text)
    assert outcome.manifest_path
    assert Manifest.load_json(outcome.manifest_path.read_bytes()).networks["default"].names["agent-alias"] == ["main"]


def test_seed_uses_first_populated_reference(tmp_path: Path) -> None:
    services = {s: Service(s, volumes=(VolumeMount("volume", "data", f"/{s}"),)) for s in ("main", "db")}
    result = export_task_files(
        Project(services, {}, frozenset({"data"})),
        tmp_path,
        tmp_path / "files",
        images={"main": "a", "db": "b"},
        image_export=FakeExport({"/db": {"seed": "populated"}}),
    )
    with tarfile.open(tmp_path / "files" / result.seeds["data"]) as archive:
        stream = archive.extractfile("seed")
        assert stream and stream.read() == b"populated"


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


def test_env_file_order_explicit_environment_and_literal_dollar(tmp_path: Path) -> None:
    path = _task(
        tmp_path,
        "env-files",
        compose="""services:
  main:
    image: r.io/a
    env_file: [first.env, second.env]
    environment: [WINNER=explicit]
""",
    )
    (path / "environment/first.env").write_text("WINNER=first\nLITERAL='${literal}'\n")
    (path / "environment/second.env").write_text("WINNER=second\nDEFERRED=${TOKEN}\n")
    project = load_task(path).project
    assert project.services["main"].environment["WINNER"] == "explicit"
    assert project.services["main"].environment["LITERAL"] == "${literal}"
    assert set(project.runtime_params["main"]) == {"DEFERRED"}


def test_resource_policy_is_task_scoped() -> None:
    base = ResourceDefaults()
    other = ResourceDefaults.model_validate(
        {"tasks": {"different": {"services": {"db": {"cpu": "8", "memory": "8Gi"}}}}}
    )
    assert base.effective("mine", "db", {}) == other.effective("mine", "db", {})
    assert base.effective("different", "db", {}) != other.effective("different", "db", {})


def test_client_configuration_snapshot_cannot_be_mutated_via_public_accessor() -> None:
    from flotilla import Flotilla
    from tests.support.config import config

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
