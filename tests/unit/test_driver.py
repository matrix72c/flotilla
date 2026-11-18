"""Task file export and the offline preparation driver used by `flotilla prepare`.

Driver tests use a fake registry and image exporter with no network or docker: they verify ordering, skips
and error attribution.
"""

from __future__ import annotations

import json
import os
from dataclasses import replace
from pathlib import Path

import pytest

from flotilla.build.driver import BuildOutcome, build_task
from flotilla.build.files import ExportError, export_task_files
from flotilla.manifest import Manifest
from flotilla.testing.build import DIGEST, DIGEST2, FakeRegistry, _ctx, _task
from flotilla.testing.compose import offline_task as load_task

# ───────────────────────────── Exporting task files ─────────────────────────────


def test_export_copies_platform_mounted_binds(tmp_path: Path) -> None:
    task = load_task(
        _task(
            tmp_path,
            "binds",
            compose=(
                "services:\n"
                "  main:\n    image: r.io/a:1\n"
                "  app:\n    image: r.io/b:1\n"
                "    volumes:\n"
                "      - {type: bind, source: ./shared, target: /data, read_only: true}\n"
                "  web:\n    image: r.io/c:1\n"
                "    volumes:\n"
                "      - {type: bind, source: ./shared, target: /mirror, read_only: true}\n"
            ),
            **{"shared__f.txt": "payload"},
        )
    )
    files = tmp_path / "files"
    exported = export_task_files(task.project, task.env_dir, files)
    (key,) = exported.binds.values()
    assert (files / key / "f.txt").read_text() == "payload"


def test_export_stages_single_writable_bind(tmp_path: Path) -> None:
    # Even one writable reference needs a real mount, not an image COPY.
    task = load_task(
        _task(
            tmp_path,
            "img",
            compose=(
                "services:\n  main:\n    image: r.io/a:1\n"
                "    volumes:\n      - {type: bind, source: ./seed, target: /seed}\n"
            ),
            **{"seed__f.txt": "x"},
        )
    )
    files = tmp_path / "files"
    assert export_task_files(task.project, task.env_dir, files).binds == {0: "binds/0"}
    assert (files / "binds/0/f.txt").read_text() == "x"


def test_bind_export_preserves_same_tree_hardlinks(tmp_path: Path) -> None:
    task = load_task(
        _task(
            tmp_path,
            "hardlinks",
            compose="services:\n  main:\n    image: r.io/a:1\n    volumes: ['./data:/data']\n",
            data__first="payload",
        )
    )
    os.link(task.env_dir / "data/first", task.env_dir / "data/second")
    files = tmp_path / "files"
    export_task_files(task.project, task.env_dir, files)
    assert (files / "binds/0/first").stat().st_ino == (files / "binds/0/second").stat().st_ino
    (files / "binds/0/first").write_text("changed")
    assert (files / "binds/0/second").read_text() == "changed"
    assert (task.env_dir / "data/first").read_text() == "payload"


def test_bind_export_rejects_cross_root_hardlinks(tmp_path: Path) -> None:
    task = load_task(
        _task(
            tmp_path,
            "hardlinks",
            compose="services:\n  main:\n    image: r.io/a:1\n    volumes: ['./first:/a', './second:/b']\n",
            first="payload",
        )
    )
    os.link(task.env_dir / "first", task.env_dir / "second")
    with pytest.raises(ExportError, match="independent bind"):
        export_task_files(task.project, task.env_dir, tmp_path / "files")


def test_writable_root_bind_derives_from_environment_directory(tmp_path: Path) -> None:
    task = load_task(
        _task(
            tmp_path,
            "root-bind",
            compose="services:\n  main:\n    image: r.io/a:1\n    volumes: ['./:/workspace']\n",
        )
    )
    outcome = build_task(task, tmp_path / "out", _ctx())
    assert outcome.status == "built", outcome.reason
    manifest = _manifest(outcome)
    assert manifest.units["main"].mounts[0].target == "/workspace"
    assert manifest.trial_volumes[0].copy_from == "binds/0"


# ───────────────────────────── Driver ─────────────────────────────


def test_published_image_is_pinned_without_building(tmp_path: Path) -> None:
    task = load_task(_task(tmp_path, "linked", toml='[environment]\ndocker_image = "r.io/ns/app:1"\n'))
    out = build_task(task, tmp_path / "out", _ctx())
    assert out.status == "built"
    manifest = json.loads(out.manifest_path.read_text())  # type: ignore[union-attr]
    assert manifest["units"]["main"]["image"] == f"r.io/ns/app@{DIGEST}"
    assert manifest["units"]["main"]["context"]["cwd"] == "/app"  # from image metadata


def test_dockerfile_service_is_rejected_as_out_of_scope(tmp_path: Path) -> None:
    """Producing images is a different job: a task that needs one is rejected, not built."""
    path = _task(tmp_path, "needs-build")
    (path / "environment" / "Dockerfile").write_text("FROM r.io/base:1\nRUN echo hi\n")
    out = build_task(load_task(path), tmp_path / "out", _ctx())
    assert out.status == "rejected" and "already published" in out.reason


def test_build_always_compiles_a_new_plan(tmp_path: Path) -> None:
    task = load_task(_task(tmp_path, "idem", toml='[environment]\ndocker_image = "r.io/ns/app:1"\n'))
    out_dir = tmp_path / "out"
    first = build_task(task, out_dir, _ctx())
    second = build_task(task, out_dir, _ctx())
    assert (first.status, second.status) == ("built", "built")
    assert second.build_key != first.build_key
    assert not list(out_dir.glob("**/flotilla.source.json"))


def test_build_reports_rejected_task_without_building(tmp_path: Path) -> None:
    # Unknown network semantics must still be rejected before preparing images.
    task = load_task(
        _task(tmp_path, "offline", toml='[environment]\ndocker_image = "r.io/a:1"\nnetwork_mode = "unknown"\n')
    )
    out = build_task(task, tmp_path / "out", _ctx())
    assert out.status == "rejected" and out.manifest_path is None


@pytest.mark.parametrize("policy", ['network_mode = "no-network"', "allow_internet = false"])
def test_build_preserves_no_network_and_peer_topology(tmp_path: Path, policy: str) -> None:
    task = load_task(
        _task(
            tmp_path,
            "offline",
            toml=f'[environment]\ndocker_image = "r.io/a:1"\n{policy}\n',
            compose=(
                "services:\n  main: {}\n  db:\n    image: r.io/b:1\n"
                "  init:\n    image: r.io/c:1\n    network_mode: none\n"
            ),
        )
    )
    result = build_task(task, tmp_path / "out", _ctx())
    assert result.status == "built" and result.manifest_path is not None
    manifest = Manifest.load_json(result.manifest_path.read_text())
    assert manifest.external_units == []
    assert manifest.networks["default"].internal
    assert set(manifest.networks["default"].members) == {"main", "db"}


def test_unpullable_image_is_reported_not_raised(tmp_path: Path) -> None:
    task = load_task(_task(tmp_path, "unpullable", toml='[environment]\ndocker_image = "docker.io/library/redis:7"\n'))
    out = build_task(task, tmp_path / "out", _ctx())
    assert out.status == "failed" and "not pullable" in out.reason


def test_needs_publish_set_when_task_files_exported(tmp_path: Path) -> None:
    # Exported a bind (platform mount) -> must publish before running (publish writes the manifest's task_files).
    path = _task(
        tmp_path,
        "withfiles",
        compose=(
            "services:\n"
            "  main:\n    image: r.io/a:1\n"
            "  app:\n    image: r.io/b:1\n"
            "    volumes:\n      - {type: bind, source: ./shared, target: /data, read_only: true}\n"
            "  web:\n    image: r.io/c:1\n"
            "    volumes:\n      - {type: bind, source: ./shared, target: /mirror, read_only: true}\n"
        ),
        **{"shared__f.txt": "x"},
    )
    out_dir = tmp_path / "out"
    built = build_task(load_task(path), out_dir, _ctx())
    assert built.status == "built" and built.needs_publish is True
    # Skips must be reported faithfully too, otherwise a rerun would miss "not yet published".
    assert build_task(load_task(path), out_dir, _ctx()).needs_publish is True


def test_needs_publish_false_without_task_files(tmp_path: Path) -> None:
    task = load_task(_task(tmp_path, "nofiles", toml='[environment]\ndocker_image = "r.io/ns/app:1"\n'))
    assert build_task(task, tmp_path / "out", _ctx()).needs_publish is False


def test_named_volume_is_declared_empty_and_nocopy_is_already_the_behavior(tmp_path: Path) -> None:
    """flotilla does not copy image content into a named volume, so `nocopy` only restates what happens."""
    path = _task(
        tmp_path,
        "shared",
        compose=(
            "services:\n"
            "  main:\n    image: r.io/a:1\n"
            "    volumes:\n      - {type: volume, source: data, target: /data}\n"
            "  app:\n    image: r.io/b:1\n"
            "    volumes:\n      - {type: volume, source: data, target: /data2, volume: {nocopy: true}}\n"
            "volumes:\n  data: {}\n"
        ),
    )
    out = build_task(load_task(path), tmp_path / "out", _ctx())
    assert out.status == "built", out.reason
    manifest = _manifest(out)
    assert [v.key for v in manifest.trial_volumes] == ["data"]
    assert manifest.trial_volumes[0].copy_from is None
    assert not out.needs_publish  # an empty volume needs nothing from the task files
    nocopy = [f for f in manifest.findings if f.path.endswith(".volume.nocopy")]
    assert nocopy and all(f.kind == "equivalent" for f in nocopy)


def _manifest(outcome: BuildOutcome) -> Manifest:
    assert outcome.status in ("built", "built"), outcome.reason
    assert outcome.manifest_path is not None
    return Manifest.load_json(outcome.manifest_path.read_bytes())


def test_same_environment_does_not_reuse_another_task_identity(tmp_path: Path) -> None:
    ctx = _ctx()
    outcomes = [
        build_task(
            load_task(_task(tmp_path, name, toml='[environment]\ndocker_image="r.io/app:1"')),
            tmp_path / "out",
            ctx,
        )
        for name in ("alpha", "beta")
    ]
    assert outcomes[0].build_key != outcomes[1].build_key
    assert [_manifest(o).task for o in outcomes] == ["ds/alpha", "ds/beta"]


def test_readonly_bind_change_is_read_on_next_build(tmp_path: Path) -> None:
    path = _task(
        tmp_path,
        "bind",
        compose="services:\n  main:\n    image: r.io/app:1\n    volumes:\n"
        "      - {type: bind, source: ./shared, target: /data, read_only: true}\n",
        shared__value="before",
    )
    ctx = _ctx()
    first = build_task(load_task(path), tmp_path / "out", ctx)
    (path / "environment/shared/value").write_text("after")
    second = build_task(load_task(path), tmp_path / "out", ctx)
    assert first.build_key != second.build_key and second.status == "built"
    assert second.manifest_path is not None
    assert (second.manifest_path.parent / "files/binds/0/value").read_text() == "after"


def test_tag_movement_is_resolved_on_each_open(tmp_path: Path) -> None:
    task = load_task(_task(tmp_path, "tags", toml='[environment]\ndocker_image="r.io/app:1"'))
    registry = FakeRegistry(digests={"r.io/app:1": DIGEST})
    ctx = _ctx(registry=registry)
    first = build_task(task, tmp_path / "out", ctx)
    assert _manifest(first).units["main"].image == f"r.io/app@{DIGEST}"
    registry.digests["r.io/app:1"] = DIGEST2
    second = build_task(task, tmp_path / "out", ctx)
    assert second.build_key != first.build_key and second.status == "built"
    assert _manifest(second).units["main"].image == f"r.io/app@{DIGEST2}"


def test_effective_image_replacement_applies_to_next_open(tmp_path: Path) -> None:
    task = load_task(_task(tmp_path, "config", toml='[environment]\ndocker_image="r.io/base:1"'))
    ctx = _ctx(registry=FakeRegistry(digests={"r.io/base:1": DIGEST, "r.io/base:2": DIGEST2}))
    first = build_task(task, tmp_path / "out", ctx)
    other = replace(ctx, settings=replace(ctx.settings, image_replacements={"r.io/base:1": "r.io/base:2"}))
    second = build_task(task, tmp_path / "out", other)
    assert first.status == second.status == "built", second.reason
    assert _manifest(first).units["main"].image != _manifest(second).units["main"].image


def test_nonroot_image_is_rejected_instead_of_adapted(tmp_path: Path) -> None:
    """Flotilla no longer publishes a USER0 derivation, so a non-root image fails preparation."""
    task = load_task(_task(tmp_path, "nonroot", toml='[environment]\ndocker_image="r.io/app:1"'))
    outcome = build_task(task, tmp_path / "out", _ctx(registry=FakeRegistry(user="appuser")))
    assert outcome.status == "failed" and "root image" in outcome.reason


@pytest.mark.parametrize("user", ["root", "root:root", "0", "0:0", ""])
def test_root_image_spellings_are_accepted(tmp_path: Path, user: str) -> None:
    task = load_task(_task(tmp_path, "root", toml='[environment]\ndocker_image="r.io/app:1"'))
    outcome = build_task(task, tmp_path / "out", _ctx(registry=FakeRegistry(user=user)))
    assert outcome.status == "built", outcome.reason
