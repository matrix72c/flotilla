"""Portable native releases: identity, relocation, bindings and hostile-input rejection."""

from __future__ import annotations

import io
import json
import os
import shutil
import subprocess
import sys
import tarfile
from pathlib import Path

import pytest

from flotilla.config import Storage
from flotilla.manifest import Context, Manifest, Mount, Resources, TrialVolume, Unit
from flotilla.native import (
    Artifact,
    ReleaseBinding,
    ReleaseBindings,
    ReleaseReader,
    ReleaseWriter,
    Task,
    Verification,
    platform_key,
    read_dataset,
)

IMAGE = "example.invalid/runtime@sha256:" + "a" * 64
HOST = Storage(volumes="host", host_path="/srv/shared").settings()
PVC = Storage(volumes="pvc", claim_name="claim", root_subpath="flotilla").settings()


def unit(mounts: tuple[Mount, ...] = ()) -> Unit:
    return Unit(
        image=IMAGE,
        resources=Resources(cpu="1", memory="128Mi"),
        context=Context(env={}, uid=0, gid=0, cwd="/"),
        command=None,
        mounts=list(mounts),
    )


def role(name: str, mounts: tuple[Mount, ...] = ()) -> Manifest:
    return Manifest(task=name, agent_service="main", units={"main": unit(mounts=mounts)})


def build_release(base: Path, *, verifier: bool = True, instruction: str = "Solve the task.\n") -> tuple[Path, Task]:
    """A minimal release: one static input asset, hidden tests and a verifier role."""
    files = base / "input"
    (files / "nested").mkdir(parents=True)
    (files / "data.txt").write_text("static input")
    roles: dict[str, Manifest] = {}
    assets: list[str] = []
    verification = None
    artifacts: tuple[Artifact, ...] = ()
    with ReleaseWriter(base / "release") as writer:
        assets.append(writer.add_asset(files))
        if verifier:
            tests = base / "tests"
            tests.mkdir()
            (tests / "test.sh").write_text("#!/bin/sh\nexit 0\n")
            assets.append(writer.add_asset(tests))
            roles["verifier"] = role("ds/task-a#verifier")
            verification = Verification(
                role="verifier",
                command=("bash", "/tests/test.sh"),
                tests=assets[1],
                output_paths={"reward.txt": "/logs/verifier/reward.txt"},
            )
            artifacts = (Artifact(name="agent", role="main", target="/logs/agent", verifier_target="/handoff/agent"),)
        roles["main"] = role("ds/task-a", (Mount(scope="task", key=assets[0], target="/input", read_only=True),))
        task = Task(key="ds/task-a", roles=roles, assets=tuple(assets), verification=verification, artifacts=artifacts)
        record = writer.add_task(task, instruction)
        writer.publish([{"task_ref": record.task_ref, "task_digest": record.task_digest}])
    return base / "release", task


def row_of(root: Path):
    return read_dataset(root / "dataset.jsonl")[0]


def copy_release(root: Path, name: str, change) -> Path:
    copy = root.parent / name
    shutil.copytree(root, copy, symlinks=True)
    change(copy)
    return copy


def seed(path: Path, members: list[tuple[str, str, int, str | None]]) -> Path:
    with tarfile.open(path, "w") as archive:
        for name, kind, mode, target in members:
            info = tarfile.TarInfo(name)
            info.mode = mode
            info.uid = info.gid = 0
            if kind == "dir":
                archive.addfile(info)
            elif kind == "link":
                info.type = tarfile.SYMTYPE
                info.linkname = target or ""
                archive.addfile(info)
            else:
                payload = b"seed"
                info.size = len(payload)
                archive.addfile(info, io.BytesIO(payload))
    return path


# ───────────────────────── Writer, identity, relocation ─────────────────────────


def test_release_identity_is_content_derived_and_survives_relocation(tmp_path: Path) -> None:
    first, _ = build_release(tmp_path / "one")
    second, _ = build_release(tmp_path / "two", instruction="Other instruction.\n")
    left, right = ReleaseReader(first), ReleaseReader(second)
    assert left.manifest.release_id != right.manifest.release_id  # identity follows content, not location

    moved = tmp_path / "published" / "elsewhere"
    moved.parent.mkdir()
    shutil.move(str(first), str(moved))
    relocated = ReleaseReader(moved)
    assert relocated.manifest.release_id == left.manifest.release_id
    row = row_of(moved)
    assert relocated.read_task(row).key == "ds/task-a"
    assert relocated.instruction(row) == "Solve the task.\n"
    assert [r.annotations for r in relocated.read_dataset()] == [{}]


def test_dataset_rows_keep_annotations_and_duplicates_in_order(tmp_path: Path) -> None:
    files = tmp_path / "input"
    files.mkdir()
    (files / "data.txt").write_text("static")
    annotations = {"idx": 3, "tags": ["repeat", "repeat"], "note": "保留原样"}
    with ReleaseWriter(tmp_path / "release") as writer:
        asset = writer.add_asset(files)
        task = Task(
            key="ds/dup",
            roles={"main": role("ds/dup", (Mount(scope="task", key=asset, target="/in", read_only=True),))},
            assets=(asset,),
        )
        record = writer.add_task(task, "instruction")
        # The same selected row twice plus a parameterized variant: order and repetition are the selection.
        rows = [
            {
                "task_ref": record.task_ref,
                "task_digest": record.task_digest,
                "annotations": annotations,
                "parameters": {"seed": "1"},
            },
            {
                "task_ref": record.task_ref,
                "task_digest": record.task_digest,
                "annotations": annotations,
                "parameters": {"seed": "1"},
            },
            {
                "task_ref": record.task_ref,
                "task_digest": record.task_digest,
                "annotations": {},
                "parameters": {"seed": "2"},
            },
        ]
        manifest = writer.publish(rows)
    written = read_dataset(tmp_path / "release" / "dataset.jsonl")
    assert [row.parameters for row in written] == [{"seed": "1"}, {"seed": "1"}, {"seed": "2"}]
    assert written[0].annotations == annotations
    assert all(row.release_id == manifest.release_id for row in written)
    reader = ReleaseReader(tmp_path / "release")
    assert len(reader.manifest.tasks) == 1  # duplicated rows select one stored task
    assert [reader.read_task(row).key for row in written] == ["ds/dup"] * 3
    assert len(reader.read_dataset()) == 3


@pytest.mark.parametrize(
    "rows",
    [
        pytest.param([], id="empty"),
        pytest.param([{"task_ref": "tasks/" + "0" * 64 + "/task.json", "task_digest": "0" * 64}], id="unknown-ref"),
        pytest.param([{"task_ref": "tasks/" + "0" * 64 + "/task.json"}], id="missing-digest"),
    ],
)
def test_publish_rejects_invalid_selections_and_cleans_staging(tmp_path: Path, rows: list[dict]) -> None:
    files = tmp_path / "input"
    files.mkdir()
    (files / "data.txt").write_text("x")
    with pytest.raises(ValueError), ReleaseWriter(tmp_path / "release") as writer:
        asset = writer.add_asset(files)
        record = writer.add_task(
            Task(
                key="ds/a",
                roles={"main": role("ds/a", (Mount(scope="task", key=asset, target="/in", read_only=True),))},
                assets=(asset,),
            ),
            "x",
        )
        if rows and "task_digest" in rows[0]:
            rows[0]["task_digest"] = record.task_digest
        writer.publish(rows)
    assert list(tmp_path.iterdir()) == [files]  # staging removed, nothing half-published


def test_publish_rejects_rows_from_another_release(tmp_path: Path) -> None:
    other, _ = build_release(tmp_path / "other")
    foreign = row_of(other)
    files = tmp_path / "input"
    files.mkdir()
    (files / "data.txt").write_text("x")
    with (
        pytest.raises(ValueError, match="does not reference this release"),
        ReleaseWriter(tmp_path / "release") as writer,
    ):
        writer.add_asset(files)
        writer.publish([{**foreign.model_dump(), "release_id": ""}])
    with (
        pytest.raises(ValueError, match="does not reference this release"),
        ReleaseWriter(tmp_path / "release2") as writer,
    ):
        asset = writer.add_asset(files)
        record = writer.add_task(
            Task(
                key="ds/a",
                roles={"main": role("ds/a", (Mount(scope="task", key=asset, target="/in", read_only=True),))},
                assets=(asset,),
            ),
            "x",
        )
        writer.publish([{"task_ref": record.task_ref, "task_digest": "0" * 64}])
    assert not (tmp_path / "release").exists() and not (tmp_path / "release2").exists()


def test_writer_refuses_existing_output_and_unadded_assets(tmp_path: Path) -> None:
    (tmp_path / "release").mkdir()
    with pytest.raises(FileExistsError):
        ReleaseWriter(tmp_path / "release")
    (tmp_path / "file-output").write_text("occupied")
    with pytest.raises(FileExistsError):
        ReleaseWriter(tmp_path / "file-output")
    link = tmp_path / "linked"
    link.symlink_to(tmp_path / "release")
    with pytest.raises(FileExistsError):
        ReleaseWriter(link)

    files = tmp_path / "input"
    files.mkdir()
    (files / "data.txt").write_text("x")
    phantom = "assets/" + "b" * 64
    missing = Task(
        key="ds/a",
        roles={"main": role("ds/a", (Mount(scope="task", key=phantom, target="/in", read_only=True),))},
        assets=(phantom,),
    )
    with pytest.raises(ValueError, match="not added to writer"), ReleaseWriter(tmp_path / "fresh") as writer:
        writer.add_asset(files)
        writer.add_task(missing, "x")


# ───────────────────────── Reader integrity: mismatches and drift ─────────────────────────


def test_reader_rejects_manifest_and_task_tampering(tmp_path: Path) -> None:
    root, _ = build_release(tmp_path / "base")
    row = row_of(root)

    def flip_manifest(copy: Path) -> None:
        data = json.loads((copy / "manifest.json").read_text())
        data["tasks"][row.task_ref]["instruction_digest"] = "0" * 64
        (copy / "manifest.json").write_text(json.dumps(data))

    def flip_task(copy: Path) -> None:
        path = copy / row.task_ref
        data = json.loads(path.read_bytes())
        data["key"] = "ds/other"
        path.write_text(json.dumps(data))

    def flip_instruction(copy: Path) -> None:
        (copy / row.task_ref).parent.joinpath("instruction.md").write_text("drift")

    with pytest.raises(ValueError, match="release identity mismatch"):
        ReleaseReader(copy_release(root, "manifest", flip_manifest))
    for name, change in (("task", flip_task), ("instruction", flip_instruction)):
        with pytest.raises(ValueError, match="task content identity mismatch"):
            ReleaseReader(copy_release(root, name, change)).read_task(row)
    with pytest.raises(ValueError, match="identity does not match binding"):
        ReleaseReader(root).read_task(row.model_copy(update={"release_id": "1" * 64}))
    with pytest.raises(ValueError, match="identity does not match release"):
        ReleaseReader(root).read_task(row.model_copy(update={"task_digest": "1" * 64}))


def test_reader_detects_asset_drift(tmp_path: Path) -> None:
    root, task = build_release(tmp_path / "base")
    row = row_of(root)
    asset = task.assets[0]

    def mutated(name: str, change) -> Path:
        return copy_release(root, name, lambda copy: change(copy / asset))

    for name, change in [
        ("content", lambda a: (a / "data.txt").write_text("drift")),
        ("missing", lambda a: (a / "data.txt").unlink()),
        ("mode", lambda a: (a / "data.txt").chmod(0o600)),
        ("extra", lambda a: (a / "extra").write_text("unlisted")),
        ("swap", lambda a: ((a / "data.txt").unlink(), (a / "data.txt").symlink_to("nested"))),
    ]:
        with pytest.raises(ValueError):
            ReleaseReader(mutated(name, change)).read_task(row)

    if os.geteuid() == 0:
        ownership = mutated("owner", lambda a: os.chown(a / "data.txt", 1, 1))
        with pytest.raises(ValueError):
            ReleaseReader(ownership, ownership=True).read_task(row)
        ReleaseReader(ownership).read_task(row)  # uid/gid drift alone does not change content identity


def test_reader_rejects_non_directories(tmp_path: Path) -> None:
    root, _ = build_release(tmp_path / "base")
    (tmp_path / "file").write_text("not a release")
    with pytest.raises(ValueError, match="real directory"):
        ReleaseReader(tmp_path / "file")
    with pytest.raises(ValueError, match="real directory"):
        ReleaseReader(tmp_path / "missing")
    linked = tmp_path / "linked"
    linked.symlink_to(root, target_is_directory=True)
    with pytest.raises(ValueError, match="real directory"):
        ReleaseReader(linked)


# ───────────────────────── Unsafe assets and seeds ─────────────────────────


@pytest.mark.parametrize(
    "plant",
    [
        pytest.param(lambda d: (d / "escape").symlink_to("../../outside"), id="escaping-symlink"),
        pytest.param(lambda d: (d / "absolute").symlink_to("/etc/hosts"), id="absolute-symlink"),
        pytest.param(lambda d: (d / "dangling").symlink_to("nowhere"), id="dangling-symlink"),
        pytest.param(lambda d: os.mkfifo(d / "fifo"), id="fifo"),
        pytest.param(lambda d: ((d / "setuid").write_text("x"), (d / "setuid").chmod(0o4755)), id="setuid"),
    ],
)
def test_writer_rejects_unsafe_asset_contents(tmp_path: Path, plant) -> None:
    source = tmp_path / "input"
    source.mkdir()
    plant(source)
    with pytest.raises(ValueError), ReleaseWriter(tmp_path / "release") as writer:
        writer.add_asset(source)
    assert not (tmp_path / "release").exists()


def test_writer_accepts_internal_symlinks(tmp_path: Path) -> None:
    source = tmp_path / "input"
    (source / "nested").mkdir(parents=True)
    (source / "data.txt").write_text("x")
    (source / "link.txt").symlink_to("data.txt")
    (source / "nested" / "up").symlink_to("../data.txt")
    with ReleaseWriter(tmp_path / "release") as writer:
        asset = writer.add_asset(source)
        record = writer.add_task(
            Task(
                key="ds/links",
                roles={"main": role("ds/links", (Mount(scope="task", key=asset, target="/in", read_only=True),))},
                assets=(asset,),
            ),
            "x",
        )
        writer.publish([{"task_ref": record.task_ref, "task_digest": record.task_digest}])
    ReleaseReader(tmp_path / "release").read_task(row_of(tmp_path / "release"))


def test_writer_rejects_symlinked_asset_root_and_stale_identities(tmp_path: Path) -> None:
    real = tmp_path / "real"
    real.mkdir()
    (real / "data").write_text("x")
    link = tmp_path / "link"
    link.symlink_to(real)
    with pytest.raises(ValueError), ReleaseWriter(tmp_path / "release") as writer:
        writer.add_asset(link)

    plain = tmp_path / "plain"
    plain.mkdir()
    (plain / "data").write_text("x")
    with pytest.raises(ValueError, match="missing asset entries"), ReleaseWriter(tmp_path / "release2") as writer:
        writer.add_asset(plain, identities={"ghost": (1, 1, 0o644)})


def publish_with_seed(tmp_path: Path, members: list[tuple[str, str, int, str | None]]) -> None:
    files = tmp_path / "input"
    files.mkdir()
    (files / "data.txt").write_text("x")
    tar = seed(tmp_path / "seed.tar", members)
    with ReleaseWriter(tmp_path / "release") as writer:
        asset = writer.add_asset(files)
        tar_asset = writer.add_asset(tar)
        task = Task(
            key="ds/seeded",
            roles={
                "main": Manifest(
                    task="ds/seeded",
                    agent_service="main",
                    units={"main": unit()},
                    trial_volumes=[TrialVolume(key="work", seed=tar_asset)],
                )
            },
            assets=(asset, tar_asset),
        )
        record = writer.add_task(task, "instruction")
        writer.publish([{"task_ref": record.task_ref, "task_digest": record.task_digest}])


def test_valid_seed_passes_through_public_api(tmp_path: Path) -> None:
    publish_with_seed(tmp_path, [("sub", "dir", 0o755, None), ("sub/seed.txt", "file", 0o644, None)])
    ReleaseReader(tmp_path / "release").read_task(row_of(tmp_path / "release"))


@pytest.mark.parametrize(
    "members",
    [
        pytest.param([("../escape", "file", 0o644, None)], id="parent-escape"),
        pytest.param([("/abs", "file", 0o644, None)], id="absolute-member"),
        pytest.param([("dup", "file", 0o644, None), ("dup", "file", 0o644, None)], id="duplicate"),
        pytest.param([("dev", "file", 0o4755, None)], id="special-bits"),
        pytest.param([("bad", "link", 0o777, "/absolute/target")], id="absolute-link"),
        pytest.param([("out", "link", 0o777, "../outside")], id="escaping-link"),
        pytest.param([("nowhere", "link", 0o777, "missing")], id="dangling-link"),
        pytest.param(
            [("a", "link", 0o777, "b"), ("b", "link", 0o777, "target"), ("target", "file", 0o644, None)],
            id="chained-links",
        ),
        pytest.param(
            [("sub", "dir", 0o755, None), ("alias", "link", 0o777, "sub"), ("alias/x", "file", 0o644, None)],
            id="link-through",
        ),
    ],
)
def test_unsafe_seed_archives_rejected_at_publish(tmp_path: Path, members) -> None:
    with pytest.raises(ValueError):
        publish_with_seed(tmp_path, members)
    assert not (tmp_path / "release").exists()


# ───────────────────────── Task schema guards ─────────────────────────


def test_task_schema_guards() -> None:
    good = "assets/" + "a" * 64
    hidden = "assets/" + "b" * 64
    mount = Mount(scope="task", key=good, target="/in", read_only=True)

    def manifest(
        image: str = IMAGE, mounts: tuple[Mount, ...] = (mount,), volumes: tuple[TrialVolume, ...] = (), **extra
    ) -> Manifest:
        return Manifest(
            task="ds/a",
            agent_service="main",
            **extra,
            units={
                "main": Unit(
                    image=image,
                    resources=Resources(cpu="1", memory="128Mi"),
                    context=Context(env={}, uid=0, gid=0, cwd="/"),
                    command=None,
                    mounts=list(mounts),
                )
            },
            trial_volumes=list(volumes),
        )

    def task(**kwargs) -> Task:
        base: dict = {"key": "ds/a", "roles": {"main": manifest()}, "assets": (good,)}
        base.update(kwargs)
        return Task(**base)

    with pytest.raises(ValueError, match="digest pinned"):
        task(roles={"main": manifest(image="image:latest")})
    with pytest.raises(ValueError, match="read-only"):
        task(roles={"main": manifest(mounts=(Mount(scope="task", key=good, target="/in", read_only=False),))})
    with pytest.raises(ValueError, match="hidden tests cannot be mounted"):
        task(
            roles={"main": manifest(mounts=(mount, Mount(scope="task", key=hidden, target="/tests", read_only=True)))},
            verification=Verification(command=("true",), tests=hidden),
            assets=(good, hidden),
        )
    with pytest.raises(ValueError, match="hidden tests cannot initialize"):
        task(
            roles={"main": manifest(volumes=(TrialVolume(key="v", seed=hidden),))},
            verification=Verification(command=("true",), tests=hidden),
            assets=(good, hidden),
        )
    with pytest.raises(ValueError, match="undeclared asset reference"):
        task(roles={"main": manifest(mounts=(mount, Mount(scope="task", key=hidden, target="/extra", read_only=True)))})
    with pytest.raises(ValueError, match="content addressed"):
        task(assets=("datasets/not-a-digest",))
    with pytest.raises(ValueError, match="publication paths"):
        task(roles={"main": manifest(task_files="datasets/a")})
    with pytest.raises(ValueError, match="duplicate artifact name"):
        artifact = Artifact(name="agent", role="main", target="/logs/agent")
        task(artifacts=(artifact, artifact))
    with pytest.raises(ValueError, match="unknown verification role"):
        task(verification=Verification(role="ghost", command=("true",)))
    with pytest.raises(ValueError, match="roles require main"):
        task(roles={"side": role("ds/a")})
    with pytest.raises(ValueError, match="duplicate assets"):
        task(assets=(good, good))


# ───────────────────────── Platform roots and bindings ─────────────────────────


def test_platform_key_maps_only_declared_storage_roots() -> None:
    assert platform_key("/srv/shared/releases/tb1", HOST) == "releases/tb1"
    assert platform_key("releases/tb1", HOST) == "releases/tb1"
    assert platform_key("pvc:claim/flotilla/releases/tb1", PVC) == "releases/tb1"
    assert (
        platform_key("pvc:claim/releases/tb1", Storage(volumes="pvc", claim_name="claim").settings()) == "releases/tb1"
    )

    with pytest.raises(ValueError, match="outside configured host storage"):
        platform_key("/elsewhere/releases/tb1", HOST)
    with pytest.raises(ValueError, match="noncanonical"):
        platform_key("/srv/shared/../shared/x", HOST)
    with pytest.raises(ValueError, match="noncanonical"):
        platform_key("/srv/shared/x/", HOST)
    with pytest.raises(ValueError):
        platform_key("/srv/shared", HOST)
    with pytest.raises(ValueError, match="outside configured PVC"):
        platform_key("pvc:other/flotilla/releases/tb1", PVC)
    with pytest.raises(ValueError, match="outside configured PVC"):
        platform_key("pvc:claim/other/releases/tb1", PVC)
    for reserved in ("launches/x", "share/x", "/srv/shared/launches/x"):
        with pytest.raises(ValueError, match="overlap"):
            platform_key(reserved, HOST)


def test_platform_key_rejects_cross_storage_forms() -> None:
    # A PVC identity must never resolve under host storage: its claim prefix would
    # silently become a directory name inside the host root.
    with pytest.raises(ValueError, match="host storage"):
        platform_key("pvc:claim/flotilla/releases/tb1", HOST)
    # A bare relative root must never resolve under PVC storage: the claim name and
    # shared subpath would be ignored and the key would point outside the fleet root.
    with pytest.raises(ValueError, match="explicit pvc"):
        platform_key("releases/tb1", PVC)


def test_bindings_resolve_bound_tasks_and_reject_conflicts(tmp_path: Path) -> None:
    release_a, task_a = build_release(tmp_path / "a")
    release_b, _ = build_release(tmp_path / "b", instruction="Different instruction.\n")
    unbound, _ = build_release(tmp_path / "unbound", instruction="Third task.\n")
    row_a, row_unbound = row_of(release_a), row_of(unbound)

    bindings = ReleaseBindings(
        {
            "a": ReleaseBinding(local_root=release_a, platform_root="/srv/shared/releases/a"),
            "b": ReleaseBinding(local_root=release_b, platform_root="/srv/shared/releases/b"),
        },
        storage=HOST,
    )
    resolved = bindings.resolve(row_a)
    assert resolved.platform_key == "releases/a"
    assert resolved.task.key == task_a.key
    assert resolved.instruction() == "Solve the task.\n"
    assert len(bindings.read_dataset(release_a / "dataset.jsonl")) == 1

    with pytest.raises(ValueError, match="not explicitly bound"):
        bindings.resolve(row_unbound)
    with pytest.raises(ValueError, match="without an explicit binding"):
        bindings.read_dataset(unbound / "dataset.jsonl")

    moved = tmp_path / "moved-a"
    shutil.copytree(release_a, moved, symlinks=True)
    relocated = ReleaseBindings(
        {"a": ReleaseBinding(local_root=moved, platform_root="/srv/shared/releases/a2")}, storage=HOST
    )
    again = relocated.resolve(row_a)
    assert again.reader.manifest.release_id == resolved.reader.manifest.release_id
    assert again.platform_key == "releases/a2"

    with pytest.raises(ValueError, match="duplicate release bindings"):  # same release identity at two roots
        ReleaseBindings(
            {
                "a1": ReleaseBinding(local_root=release_a, platform_root="/srv/shared/x1"),
                "a2": ReleaseBinding(local_root=moved, platform_root="/srv/shared/x2"),
            },
            storage=HOST,
        )
    with pytest.raises(ValueError, match="duplicate release bindings"):  # two releases claiming one platform root
        ReleaseBindings(
            {
                "a1": ReleaseBinding(local_root=release_a, platform_root="/srv/shared/same"),
                "b1": ReleaseBinding(local_root=release_b, platform_root="/srv/shared/same"),
            },
            storage=HOST,
        )
    with pytest.raises(ValueError, match="duplicate release bindings"):  # same local root twice
        ReleaseBindings(
            {
                "a1": ReleaseBinding(local_root=release_a, platform_root="/srv/shared/p1"),
                "a2": ReleaseBinding(local_root=release_a, platform_root="/srv/shared/p2"),
            },
            storage=HOST,
        )
    with pytest.raises(ValueError, match="platform_root is required"):
        ReleaseBindings({"a": ReleaseBinding(local_root=release_a, platform_root="")}, storage=HOST)


def test_read_dataset_reports_reject_line_numbers(tmp_path: Path) -> None:
    path = tmp_path / "dataset.jsonl"
    valid = '{"release_id": "' + "0" * 64 + '", "task_ref": "tasks/x/task.json", "task_digest": "' + "0" * 64 + '"}'
    path.write_text(valid + "\nnot json\n")
    with pytest.raises(ValueError, match=r"dataset\.jsonl:2: invalid native row"):
        read_dataset(path)
    path.write_text("\n" + valid + "\n\n")
    assert len(read_dataset(path)) == 1  # blank lines are skipped, not fatal


# ───────────────────────── Installed helper and import boundaries ─────────────────────────


def helper_process(tmp_path: Path, *args: str) -> subprocess.CompletedProcess[str]:
    source = Path(__import__("flotilla").__file__).parents[1]
    hostile = tmp_path / "cwd"
    (hostile / "flotilla" / "native").mkdir(parents=True, exist_ok=True)
    (hostile / "flotilla" / "__init__.py").write_text("raise SystemExit(9)\n")
    (hostile / "flotilla" / "native" / "__init__.py").write_text("")
    (hostile / "flotilla" / "native" / "platform_verify.py").write_text("raise SystemExit(8)\n")
    return subprocess.run(
        [sys.executable, "-P", "-m", "flotilla.native.platform_verify", *args],
        cwd=hostile,
        env={**os.environ, "PYTHONPATH": str(source)},
        capture_output=True,
        text=True,
        timeout=180,
    )


def test_installed_helper_self_test_ignores_cwd(tmp_path: Path) -> None:
    result = helper_process(tmp_path, "--self-test")
    assert result.returncode == 0, result.stderr


def test_installed_helper_verifies_request_and_writes_result(tmp_path: Path) -> None:
    root, _ = build_release(tmp_path / "base")
    row = row_of(root)
    result_path = tmp_path / "result.json"
    request = tmp_path / "request.json"
    request.write_text(json.dumps({"root": str(root), "row": row.model_dump(mode="json"), "result": str(result_path)}))
    result = helper_process(tmp_path, str(request))
    assert result.returncode == 0, result.stderr
    payload = json.loads(result_path.read_text())
    assert payload["release_id"] == row.release_id
    assert payload["task_digest"] == row.task_digest
    assert payload["assets"]

    drifted = copy_release(root, "drifted", lambda copy: next(iter(copy.glob("assets/*"))).chmod(0o600))
    bad_result = tmp_path / "bad-result.json"
    bad = tmp_path / "bad.json"
    bad.write_text(json.dumps({"root": str(drifted), "row": row.model_dump(mode="json"), "result": str(bad_result)}))
    assert helper_process(tmp_path, str(bad)).returncode != 0
    assert not bad_result.exists()


def test_native_modules_never_import_compose_or_builder(tmp_path: Path) -> None:
    source = Path(__import__("flotilla").__file__).parents[1]
    code = (
        "import sys, flotilla, flotilla.native, flotilla.native.runtime as r, flotilla.native.platform_verify,"
        "flotilla.runtime.manager, flotilla.runtime.planning;"
        "r.NativeAttempt; r.VerificationResult;"
        "bad = sorted(m for m in sys.modules if m.startswith("
        "('flotilla.compose.', 'flotilla.build.', 'flotilla.integrations', "
        "'flotilla.prepare.', 'flotilla.anchor_build')));"
        "print(bad); sys.exit(1 if bad else 0)"
    )
    result = subprocess.run(
        [sys.executable, "-P", "-c", code],
        env={**os.environ, "PYTHONPATH": str(source)},
        capture_output=True,
        text=True,
        timeout=60,
        cwd=tmp_path,
    )
    assert result.returncode == 0, result.stdout + result.stderr
