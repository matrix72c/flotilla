"""End-to-end regressions for the engineering audit, using only isolated local inputs."""

from __future__ import annotations

import io
import os
import tarfile
from dataclasses import replace
from pathlib import Path

import pytest

from flotilla.build.docker import extract_archive
from flotilla.build.driver import build_task
from flotilla.build.files import ExportError, export_task_files
from flotilla.build.images import ImageError
from flotilla.build.meta import ImageAccounts, parse_user
from flotilla.compose.manifest_gen import build_manifest, synthesize_command
from flotilla.compose.model import ImageMeta, Project, Service, VolumeMount
from flotilla.compose.normalize import normalize
from flotilla.compose.task import load_task
from flotilla.config import ResourceDefaults
from flotilla.manifest import Manifest, Resources
from flotilla.runtime.planning import to_plan
from flotilla.scan import scan
from flotilla.testing.build import DIGEST, FakeBuilder, FakeExport, _caps, _ctx, _task


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
    service: dict[str, object] = (
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
        project=p,
        images={"main": "r.io/a"},
        meta={"main": ImageMeta()},
        resources={"main": Resources(cpu="1", memory="1Gi")},
    )
    assert len(m.runtime_params) == 1
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


def test_tmpfs_is_inspected_not_derived_without_approximating_container_identity(tmp_path: Path) -> None:
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
    builder = FakeBuilder()
    export = FakeExport()
    ctx = _ctx(builder=builder, image_export=export)
    outcome = build_task(
        task, tmp_path / "out", replace(ctx, builder=builder, caps=replace(ctx.caps, native_tmpfs=True))
    )
    assert outcome.status == "built", outcome.reason
    # Mount-point directories are runtime responsibilities: no derivation build,
    # but every tmpfs target path is inspected in the source image for links.
    assert builder.requests == []
    assert export.inspected == [(f"r.io/a@{DIGEST}", ("/cache", "/scratch"))]
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


def test_mount_path_inspection_rejects_image_symlink(tmp_path: Path) -> None:
    task = load_task(
        _task(
            tmp_path,
            "linked",
            compose="services:\n  main:\n    image: r.io/a\n    tmpfs: /cache/scratch\n",
        )
    )
    export = FakeExport(links=("/cache",))
    ctx = _ctx(image_export=export)
    outcome = build_task(task, tmp_path / "out", replace(ctx, caps=replace(ctx.caps, native_tmpfs=True)))
    assert outcome.status == "failed" and "symlink" in outcome.reason
    assert export.inspected  # the rejection came from real path inspection, not a silent skip


class _ImageContents:
    """Exporter double keyed by (image, path); `None` content is an existing empty directory."""

    def __init__(self, contents: dict[tuple[str, str], dict[str, str] | None]) -> None:
        self.contents = contents
        self.calls: list[tuple[str, str]] = []

    def extract(
        self, image: str, path: str, dest: Path, *, identities: dict[str, tuple[int, int, int]] | None = None
    ) -> bool:
        self.calls.append((image, path))
        if (image, path) not in self.contents:
            return False
        files = self.contents[(image, path)]
        if identities is not None:
            identities["."] = (0, 0, 0o755)
        for name, text in (files or {}).items():
            (dest / name).write_text(text)
            if identities is not None:
                identities[name] = (1000, 1000, 0o644)
        return bool(files)


A, B = "r.io/a@sha256:" + "a" * 64, "r.io/b@sha256:" + "b" * 64


def _seed(tmp_path: Path, exporter: _ImageContents, other: str = "/data") -> dict[str, object]:
    services = {
        "main": Service("main", volumes=(VolumeMount("volume", "data", "/data"),)),
        "db": Service("db", volumes=(VolumeMount("volume", "data", other),)),
    }
    files = tmp_path / "files"
    export_task_files(
        Project(services, {}, frozenset({"data"})), tmp_path, files, images={"main": A, "db": B}, image_export=exporter
    )
    seeds = sorted(p.name for p in (files / "seeds").glob("*")) if (files / "seeds").exists() else []
    leftovers = [p.name for p in files.iterdir() if p.name.startswith(".")] if files.exists() else []
    return {"seeds": seeds, "leftovers": leftovers}


def test_seed_with_one_populated_source_uses_it(tmp_path: Path) -> None:
    exporter = _ImageContents({(A, "/data"): {"secret": "s"}})
    assert _seed(tmp_path, exporter, "/run/secrets") == {"seeds": ["data.tar"], "leftovers": []}
    assert sorted(exporter.calls) == [(A, "/data"), (B, "/run/secrets")]


def test_seed_with_all_sources_absent_is_empty(tmp_path: Path) -> None:
    assert _seed(tmp_path, _ImageContents({}), "/run/secrets")["seeds"] == []


def test_seed_with_identical_contents_is_accepted(tmp_path: Path) -> None:
    exporter = _ImageContents({(A, "/data"): {"f": "same"}, (B, "/other"): {"f": "same"}})
    assert _seed(tmp_path, exporter, "/other")["seeds"] == ["data.tar"]


def test_seed_rejects_different_contents(tmp_path: Path) -> None:
    exporter = _ImageContents({(A, "/data"): {"f": "one"}, (B, "/other"): {"f": "two"}})
    with pytest.raises(ExportError, match="different initial content"):
        _seed(tmp_path, exporter, "/other")


def test_seed_rejects_different_empty_root_identities(tmp_path: Path) -> None:
    class Roots(_ImageContents):
        def extract(
            self, image: str, path: str, dest: Path, *, identities: dict[str, tuple[int, int, int]] | None = None
        ) -> bool:
            assert identities is not None
            identities["."] = (0, 0, 0o755) if image == A else (999, 999, 0o700)
            return False

    with pytest.raises(ExportError, match="different empty root identities"):
        _seed(tmp_path, Roots({}), "/other")


def test_seed_allows_same_digest_and_path_across_registry_aliases(tmp_path: Path) -> None:
    from unittest.mock import Mock

    services = {s: Service(s, volumes=(VolumeMount("volume", "data", "/data"),)) for s in ("main", "db")}
    digest = "sha256:" + "a" * 64
    exporter = Mock(wraps=FakeExport({"/data": {"seed": "identical"}}))
    result = export_task_files(
        Project(services, {}, frozenset({"data"})),
        tmp_path,
        tmp_path / "files",
        images={"main": f"r.io/a@{digest}", "db": f"mirror.io/b@{digest}"},
        image_export=exporter,
    )
    exporter.extract.assert_called_once()
    with tarfile.open(tmp_path / "files" / result.seeds["data"]) as archive:
        stream = archive.extractfile("seed")
        assert stream and stream.read() == b"identical"


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


def _archive(*members: tuple[tarfile.TarInfo, bytes | None]) -> io.BytesIO:
    buffer = io.BytesIO()
    with tarfile.open(fileobj=buffer, mode="w") as tar:
        for info, payload in members:
            tar.addfile(info, io.BytesIO(payload) if payload is not None else None)
    buffer.seek(0)
    return buffer


def _symlink(name: str, linkname: str) -> tuple[tarfile.TarInfo, None]:
    info = tarfile.TarInfo(name)
    info.type, info.linkname, info.mode = tarfile.SYMTYPE, linkname, 0o777
    return info, None


def _file(name: str, payload: bytes = b"x") -> tuple[tarfile.TarInfo, bytes]:
    info = tarfile.TarInfo(name)
    info.size, info.mode = len(payload), 0o644
    return info, payload


def _directory(name: str) -> tuple[tarfile.TarInfo, None]:
    info = tarfile.TarInfo(name)
    info.type, info.mode = tarfile.DIRTYPE, 0o755
    return info, None


def test_volume_seed_keeps_symlinks_that_stay_inside(tmp_path: Path) -> None:
    """Shared-library aliases like libclntsh.so -> libclntsh.so.19.1 must survive export."""
    identities: dict[str, tuple[int, int, int]] = {}
    archive = _archive(
        _directory("./lib"),
        _file("./lib/libclntsh.so.19.1", b"elf"),
        _symlink("./lib/libclntsh.so", "libclntsh.so.19.1"),
        _symlink("./lib/nested", "../lib"),
    )
    assert extract_archive(archive, tmp_path, identities)
    link = tmp_path / "lib" / "libclntsh.so"
    assert link.is_symlink() and os.readlink(link) == "libclntsh.so.19.1"
    assert link.read_bytes() == b"elf"
    assert (tmp_path / "lib" / "nested").is_symlink()
    assert identities["lib/libclntsh.so"][2] == 0o777


@pytest.mark.parametrize(
    ("name", "linkname"),
    [
        ("./lib/escape", "/etc"),
        ("./lib/escape", "/etc/passwd"),
        ("./escape", "../outside"),
        ("./lib/escape", "../../outside"),
        ("./lib/escape", "lib/../../../outside"),
        ("./lib/escape", ""),
    ],
)
def test_volume_seed_rejects_symlinks_leaving_the_volume(tmp_path: Path, name: str, linkname: str) -> None:
    archive = _archive(_directory("./lib"), _symlink(name, linkname))
    with pytest.raises(ImageError, match=r"points outside the volume|unsafe to extract"):
        extract_archive(archive, tmp_path, {})
    assert not (tmp_path / name.removeprefix("./")).exists()


def test_volume_seed_keeps_symlinks_resolving_within_the_volume(tmp_path: Path) -> None:
    """A target reached through `..` is still inside: only leaving the root is refused."""
    archive = _archive(_directory("./lib"), _file("./shared", b"data"), _symlink("./lib/up", "../shared"))
    assert extract_archive(archive, tmp_path, {})
    assert (tmp_path / "lib" / "up").read_bytes() == b"data"


def test_volume_seed_rejects_writing_through_a_symlinked_directory(tmp_path: Path) -> None:
    """`tar` unpacking the seed would follow the link, writing outside the volume directory."""
    archive = _archive(_symlink("./here", "."), _file("./here/passwd", b"root:x:0:0"))
    with pytest.raises(ImageError, match="reached through a symlink"):
        extract_archive(archive, tmp_path, {})
