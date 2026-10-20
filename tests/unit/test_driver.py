"""Task file export and the `flotilla build` driver.

Driver tests use a fake registry / builder / image exporter with no network or docker: they verify ordering,
dispositions, skips and error attribution.
"""

from __future__ import annotations

import json
import tarfile
from dataclasses import replace
from pathlib import Path

import pytest

from flotilla.build.builder import BuildRequest
from flotilla.build.driver import BuildOutcome, build_task
from flotilla.build.files import ExportError, export_task_files
from flotilla.build.images import BuildSettings
from flotilla.compose.task import load_task
from flotilla.manifest import Manifest
from flotilla.testing.build import DIGEST, DIGEST2, FakeBuilder, FakeExport, FakeRegistry, _ctx, _task

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


def test_export_skips_binds_written_into_the_image(tmp_path: Path) -> None:
    # Single writer, single reference -> written into the image (mode="image"), not into task files.
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
    assert export_task_files(task.project, task.env_dir, files).binds == {}


def test_export_writes_seed_tar_for_shared_volume(tmp_path: Path) -> None:
    task = load_task(
        _task(
            tmp_path,
            "vol",
            compose=(
                "services:\n"
                "  main:\n    image: r.io/a:1\n"
                "    volumes:\n      - {type: volume, source: data, target: /var/lib/data}\n"
                "  app:\n    image: r.io/b:1\n"
                "    volumes:\n      - {type: volume, source: data, target: /mnt/data}\n"
                "volumes:\n  data: {}\n"
            ),
        )
    )
    files = tmp_path / "files"
    export = FakeExport({"/var/lib/data": {"seed.txt": "from-image"}})
    exported = export_task_files(
        task.project, task.env_dir, files, images={"main": "r.io/a@" + DIGEST}, image_export=export
    )
    key = exported.seeds["data"]
    with tarfile.open(files / key) as tar:
        assert tar.getnames() == ["seed.txt"]
    assert not (files / "_seed-staging").exists()  # staging directory cleaned up


def test_export_no_seed_when_image_path_is_empty(tmp_path: Path) -> None:
    task = load_task(
        _task(
            tmp_path,
            "empty",
            compose=(
                "services:\n"
                "  main:\n    image: r.io/a:1\n"
                "    volumes:\n      - {type: volume, source: data, target: /data}\n"
                "  app:\n    image: r.io/b:1\n"
                "    volumes:\n      - {type: volume, source: data, target: /data2}\n"
                "volumes:\n  data: {}\n"
            ),
        )
    )
    exported = export_task_files(
        task.project, task.env_dir, tmp_path / "files", images={"main": "x", "app": "y"}, image_export=FakeExport({})
    )
    assert exported.seeds == {}  # an empty volume is empty, as in Docker


@pytest.mark.parametrize("read_only", [False, True])
@pytest.mark.parametrize("all_nocopy", [False, True])
def test_volume_seed_ignores_nocopy_references(tmp_path: Path, read_only: bool, all_nocopy: bool) -> None:
    ro = str(read_only).lower()
    nocopy = str(all_nocopy).lower()
    task = load_task(
        _task(
            tmp_path,
            "nocopy",
            compose=(
                "services:\n"
                "  main:\n    image: r.io/a:1\n    volumes:\n"
                f"      - {{type: volume, source: data, target: /skip, read_only: {ro}, volume: {{nocopy: true}}}}\n"
                "  app:\n    image: r.io/b:1\n    volumes:\n"
                f"      - {{type: volume, source: data, target: /seed, read_only: {ro}, "
                f"volume: {{nocopy: {nocopy}}}}}\n"
                "volumes:\n  data: {}\n"
            ),
        )
    )
    files = tmp_path / "files"
    exported = export_task_files(
        task.project,
        task.env_dir,
        files,
        images=None if all_nocopy else {"main": "a", "app": "b"},
        image_export=None if all_nocopy else FakeExport({"/skip": {"wrong": "skip"}, "/seed": {"right": "seed"}}),
    )
    if read_only:
        contents = sorted(p.name for p in (files / exported.readonly_volumes["data"]).iterdir())
    elif all_nocopy:
        assert exported.seeds == {}
        return
    else:
        with tarfile.open(files / exported.seeds["data"]) as tar:
            contents = tar.getnames()
    assert contents == ([] if all_nocopy else ["right"])


def test_writable_root_bind_derives_from_environment_directory(tmp_path: Path) -> None:
    task = load_task(
        _task(
            tmp_path,
            "root-bind",
            compose="services:\n  main:\n    image: r.io/a:1\n    volumes: ['./:/workspace']\n",
        )
    )
    builder = FakeBuilder()
    outcome = build_task(task, tmp_path / "out", _ctx(builder=builder))
    assert outcome.status == "built", outcome.reason
    assert builder.requests[0].context == task.env_dir
    assert 'COPY [".", "/workspace"]' in str(builder.requests[0].dockerfile_text)


def test_export_requires_exporter_for_seeds(tmp_path: Path) -> None:
    task = load_task(
        _task(
            tmp_path,
            "noexp",
            compose=(
                "services:\n"
                "  main:\n    image: r.io/a:1\n"
                "    volumes:\n      - {type: volume, source: data, target: /data}\n"
                "  app:\n    image: r.io/b:1\n"
                "    volumes:\n      - {type: volume, source: data, target: /data2}\n"
                "volumes:\n  data: {}\n"
            ),
        )
    )
    with pytest.raises(ExportError, match="data"):
        export_task_files(task.project, task.env_dir, tmp_path / "files")


# ───────────────────────────── Driver ─────────────────────────────


def test_build_links_pullable_image_without_building(tmp_path: Path) -> None:
    task = load_task(_task(tmp_path, "linked", toml='[environment]\ndocker_image = "r.io/ns/app:1"\n'))
    ctx = _ctx()
    out = build_task(task, tmp_path / "out", ctx)
    assert out.status == "built" and out.dispositions == {"main": "link"} and out.built == ()
    manifest = json.loads(out.manifest_path.read_text())  # type: ignore[union-attr]
    assert manifest["units"]["main"]["image"] == f"r.io/ns/app@{DIGEST}"
    assert manifest["units"]["main"]["context"]["cwd"] == "/app"  # from image metadata
    assert ctx.builder.requests == []  # type: ignore[union-attr]


def test_build_runs_builder_for_build_service(tmp_path: Path) -> None:
    path = _task(tmp_path, "built")
    (path / "environment" / "Dockerfile").write_text("FROM r.io/base:1\nRUN echo hi\n")
    ctx = _ctx()
    out = build_task(load_task(path), tmp_path / "out", ctx)
    assert out.status == "built" and out.dispositions == {"main": "build"} and out.built == ("main",)
    (request,) = ctx.builder.requests  # type: ignore[union-attr]
    assert request.dockerfile == "Dockerfile" and request.service == "main"


def test_build_always_compiles_a_new_plan(tmp_path: Path) -> None:
    task = load_task(_task(tmp_path, "idem", toml='[environment]\ndocker_image = "r.io/ns/app:1"\n'))
    out_dir = tmp_path / "out"
    first = build_task(task, out_dir, _ctx())
    second = build_task(task, out_dir, _ctx())
    assert (first.status, second.status) == ("built", "built")
    assert second.build_key != first.build_key
    assert not list(out_dir.glob("**/flotilla.source.json"))


def test_build_reports_rejected_task_without_building(tmp_path: Path) -> None:
    # no-network: rejected by the scan (the first release does not declare disable_internet), never reaches the build.
    task = load_task(
        _task(tmp_path, "offline", toml='[environment]\ndocker_image = "r.io/a:1"\nnetwork_mode = "no-network"\n')
    )
    ctx = _ctx()
    out = build_task(task, tmp_path / "out", ctx)
    assert out.status == "rejected" and out.manifest_path is None
    assert ctx.builder.requests == []  # type: ignore[union-attr]


def test_build_failure_is_reported_not_raised(tmp_path: Path) -> None:
    path = _task(tmp_path, "nobuilder")
    (path / "environment" / "Dockerfile").write_text("FROM r.io/base:1\n")
    out = build_task(load_task(path), tmp_path / "out", _ctx(builder=None))
    assert out.status == "failed" and "no builder was provided" in out.reason


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


def _shared_volume_task(root: Path, name: str) -> Path:
    return _task(
        root,
        name,
        compose=(
            "services:\n"
            "  main:\n    image: r.io/a:1\n"
            "    volumes:\n      - {type: volume, source: data, target: /data}\n"
            "  app:\n    image: r.io/b:1\n"
            "    volumes:\n      - {type: volume, source: data, target: /data2}\n"
            "volumes:\n  data: {}\n"
        ),
    )


def test_manifest_declares_seed_only_when_exported(tmp_path: Path) -> None:
    """The seeds declared in the manifest must match what was actually exported.

    Hit on a real deployment: the manifest unconditionally declared `seeds/<v>.tar`, but no tar is produced when the
    image has no content under the mount point, so the trial's prepare stage failed unpacking a nonexistent file.
    """
    path = _shared_volume_task(tmp_path, "noseed")
    out = build_task(load_task(path), tmp_path / "out", _ctx(image_export=FakeExport({})))
    assert out.status == "built"
    manifest = json.loads(out.manifest_path.read_text())  # type: ignore[union-attr]
    (volume,) = manifest["trial_volumes"]
    assert volume["key"] == "data" and volume["seed"] is None  # no content -> no seed declared


def test_manifest_declares_seed_when_image_has_content(tmp_path: Path) -> None:
    path = _shared_volume_task(tmp_path, "withseed")
    export = FakeExport({"/data": {"seed.txt": "x"}})
    out = build_task(load_task(path), tmp_path / "out", _ctx(image_export=export))
    manifest = json.loads(out.manifest_path.read_text())  # type: ignore[union-attr]
    (volume,) = manifest["trial_volumes"]
    assert volume["seed"] == "seeds/data.tar"


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
    ctx = _ctx(registry=registry, settings=BuildSettings(target="r.io/out", mirror_images=True))
    first = build_task(task, tmp_path / "out", ctx)
    assert first.status == "built" and len(registry.copied) == 1
    assert build_task(task, tmp_path / "out", ctx).status == "built"
    assert len(registry.copied) == 2
    registry.digests["r.io/app:1"] = DIGEST2
    second = build_task(task, tmp_path / "out", ctx)
    assert second.build_key != first.build_key and second.status == "built"
    assert _manifest(second).units["main"].image == f"r.io/out@{DIGEST2}"


@pytest.mark.parametrize("change", ["target", "replacement"])
def test_effective_build_configuration_applies_to_next_open(tmp_path: Path, change: str) -> None:
    task = load_task(_task(tmp_path, "config", Dockerfile="FROM r.io/base:1\n"))
    ctx = _ctx()
    first = build_task(task, tmp_path / "out", ctx)
    if change == "target":
        other = replace(ctx, settings=replace(ctx.settings, target="r.io/other"))
    else:
        other = replace(ctx, settings=replace(ctx.settings, image_replacements={"r.io/base:1": "r.io/base:2"}))
    second = build_task(task, tmp_path / "out", other)
    assert first.status == second.status == "built", second.reason
    assert first.build_key != second.build_key


@pytest.mark.parametrize("source", ["build", "image", "prebuilt"])
@pytest.mark.parametrize("mount", ["bind", "nocopy"])
def test_all_image_sources_apply_compose_derivation(tmp_path: Path, source: str, mount: str) -> None:
    origin = "image: r.io/base:1" if source == "image" else "build: {context: .}"
    volume = (
        "{type: bind, source: ./payload, target: /data}"
        if mount == "bind"
        else "{type: volume, source: cache, target: /data, volume: {nocopy: true}}"
    )
    path = _task(
        tmp_path,
        "derive",
        compose=f"services:\n  main:\n    {origin}\n    volumes:\n      - {volume}\nvolumes:\n  cache: {{}}\n",
        Dockerfile="FROM r.io/base:1\n",
        payload__value="payload",
    )
    if source == "image":
        # Harbor's main_build override replaces main's image when a Dockerfile exists.
        (path / "environment/Dockerfile").unlink()
    builder = FakeBuilder(digests=[DIGEST, DIGEST2] if source == "build" else [DIGEST2])
    ctx = _ctx(builder=builder, registry=FakeRegistry(user="1000:1001"))
    if source == "prebuilt":
        (path / "task.toml").write_text('[environment]\ndocker_image="r.io/base:1"\n')
    outcome = build_task(load_task(path), tmp_path / "out", ctx)
    manifest = _manifest(outcome)
    assert len(builder.requests) == (2 if source == "build" else 1)
    last = builder.requests[-1]
    text = last.dockerfile_text
    assert text is not None and f"@{DIGEST}" in text.splitlines()[0]
    assert text.splitlines()[1] == "USER 0:0" and text.splitlines()[-1] == "USER 1000:1001"
    if mount == "bind":
        assert 'COPY ["payload", "/data"]' in text
    else:
        assert "rm -rf /data/*" in text
    assert manifest.units["main"].image == f"r.io/ns/out@{DIGEST2}"
    assert manifest.units["main"].mounts == []
    assert not outcome.needs_publish
    assert manifest.units["main"].context.uid == 1000
    assert manifest.units["main"].command == ["sh", "-c", "sleep infinity"]  # keep Harbor's agent command override
    assert "CMD " not in text and "ENTRYPOINT " not in text


def test_concurrent_builds_keep_their_metadata_digest_when_output_tag_is_overwritten(tmp_path: Path) -> None:
    from concurrent.futures import ThreadPoolExecutor
    from threading import Event

    path = _task(tmp_path, "racing", Dockerfile="FROM r.io/base:1\nCOPY marker /marker\n", marker="first")
    first_built, second_built = Event(), Event()
    requests = []

    class RacingBuilder:
        def __init__(self, first: bool) -> None:
            self.first = first

        def build(self, request: BuildRequest) -> str:
            requests.append(request)
            if self.first:
                assert (request.context / "marker").read_text() == "first"
                first_built.set()
                assert second_built.wait(5)
                return DIGEST
            assert (request.context / "marker").read_text() == "second"
            second_built.set()
            return DIGEST2

    with ThreadPoolExecutor(max_workers=2) as executor:
        first = executor.submit(build_task, load_task(path), tmp_path / "out", _ctx(builder=RacingBuilder(True)))
        assert first_built.wait(5)
        (path / "environment/marker").write_text("second")
        second = executor.submit(build_task, load_task(path), tmp_path / "out", _ctx(builder=RacingBuilder(False)))
        outcomes = [first.result(), second.result()]
    assert requests[0].tag == requests[1].tag  # Stable publish name; not a cache or version key.
    assert outcomes[0].manifest_path != outcomes[1].manifest_path
    for outcome, digest in zip(outcomes, [DIGEST, DIGEST2], strict=True):
        assert outcome.status == "built"
        assert outcome.manifest_path is not None
        manifest = json.loads(outcome.manifest_path.read_text())
        assert manifest["units"]["main"]["image"] == f"r.io/ns/out@{digest}"
