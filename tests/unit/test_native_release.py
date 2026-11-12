"""Native releases: a plain task directory tree, name-routed bindings and the installed helper."""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path
from typing import Any

import pytest

from flotilla.config import Storage
from flotilla.manifest import Context, Manifest, Mount, Resources, TrialVolume, Unit
from flotilla.native import (
    Artifact,
    DatasetRow,
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


def build_release(
    base: Path, *, name: str = "rel", key: str = "ds/task-a", instruction: str = "Solve the task.\n"
) -> tuple[Path, Task]:
    """A minimal release: one static input directory, hidden tests and a verifier role."""
    files = base / "input"
    (files / "nested").mkdir(parents=True)
    (files / "data.txt").write_text("static input")
    (files / "run.sh").write_text("#!/bin/sh\n")
    (files / "run.sh").chmod(0o755)
    tests = base / "tests"
    tests.mkdir()
    (tests / "test.sh").write_text("#!/bin/sh\nexit 0\n")
    directory = ReleaseWriter.directory(key)
    task = Task(
        key=key,
        roles={
            "main": role(key, (Mount(scope="task", key=f"{directory}/files", target="/input", read_only=True),)),
            "verifier": role(f"{key}#verifier"),
        },
        verification=Verification(
            role="verifier",
            command=("bash", "/tests/test.sh"),
            tests=f"{directory}/tests",
            output_paths={"reward.txt": "/logs/verifier/reward.txt"},
        ),
        artifacts=(Artifact(name="agent", role="main", target="/logs/agent", verifier_target="/handoff/agent"),),
    )
    with ReleaseWriter(base / "release", name) as writer:
        writer.add_task(task, instruction, {"files": files, "tests": tests})
        writer.publish([{"task": key}])
    return base / "release", task


def row_of(root: Path) -> DatasetRow:
    return read_dataset(root / "dataset.jsonl")[0]


# ───────────────────────── Writer and reader ─────────────────────────


def test_release_is_a_plain_task_tree_that_survives_relocation(tmp_path: Path) -> None:
    root, task = build_release(tmp_path / "one")
    assert sorted(p.relative_to(root).as_posix() for p in root.rglob("*") if p.is_file()) == [
        "dataset.jsonl",
        "tasks/ds/task-a/files/data.txt",
        "tasks/ds/task-a/files/run.sh",
        "tasks/ds/task-a/instruction.md",
        "tasks/ds/task-a/task.json",
        "tasks/ds/task-a/tests/test.sh",
    ]
    assert (root / "tasks/ds/task-a/files/run.sh").stat().st_mode & 0o777 == 0o755  # modes are kept
    assert root.stat().st_mode & 0o777 == 0o755
    assert row_of(root) == DatasetRow(release="rel", task="ds/task-a")

    moved = tmp_path / "published" / "elsewhere"
    moved.parent.mkdir()
    root.rename(moved)
    reader = ReleaseReader(moved)
    assert reader.read_task("ds/task-a") == task
    assert reader.instruction("ds/task-a") == "Solve the task.\n"
    assert [r.task for r in reader.read_dataset()] == ["ds/task-a"]


def test_dataset_rows_keep_annotations_and_duplicates_in_order(tmp_path: Path) -> None:
    annotations = {"idx": 3, "tags": ["repeat", "repeat"], "note": "保留原样"}
    with ReleaseWriter(tmp_path / "release", "rel") as writer:
        writer.add_task(Task(key="ds/dup", roles={"main": role("ds/dup")}), "instruction")
        rows = [
            {"task": "ds/dup", "annotations": annotations, "parameters": {"seed": "1"}},
            {"task": "ds/dup", "annotations": annotations, "parameters": {"seed": "1"}},
            {"task": "ds/dup", "parameters": {"seed": "2"}},
        ]
        written = writer.publish(rows)
    assert read_dataset(tmp_path / "release" / "dataset.jsonl") == written
    assert [row.parameters for row in written] == [{"seed": "1"}, {"seed": "1"}, {"seed": "2"}]
    assert written[0].annotations == annotations
    assert {row.release for row in written} == {"rel"}


def test_publish_rejects_unknown_tasks_and_cleans_staging(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="not in this release"), ReleaseWriter(tmp_path / "release", "rel") as writer:
        writer.add_task(Task(key="ds/a", roles={"main": role("ds/a")}), "x")
        writer.publish([{"task": "ds/other"}])
    assert list(tmp_path.iterdir()) == []  # staging removed, nothing half-published


def test_writer_refuses_existing_output_duplicate_keys_and_bad_names(tmp_path: Path) -> None:
    (tmp_path / "release").mkdir()
    with pytest.raises(FileExistsError):
        ReleaseWriter(tmp_path / "release", "rel")
    link = tmp_path / "linked"
    link.symlink_to(tmp_path / "missing")
    with pytest.raises(FileExistsError):
        ReleaseWriter(link, "rel")
    with pytest.raises(ValueError, match="one nonempty segment"):
        ReleaseWriter(tmp_path / "fresh", "a/b")
    with pytest.raises(ValueError, match="duplicate task key"), ReleaseWriter(tmp_path / "fresh", "rel") as writer:
        writer.add_task(Task(key="ds/a", roles={"main": role("ds/a")}), "x")
        writer.add_task(Task(key="ds/a", roles={"main": role("ds/a")}), "y")


def test_reader_does_not_follow_symlinks_into_task_paths(tmp_path: Path) -> None:
    root, _ = build_release(tmp_path / "base")
    (root / "tasks" / "linked").symlink_to(root / "tasks" / "ds")
    with pytest.raises(ValueError, match="symlink in release path"):
        ReleaseReader(root).read_task("linked/task-a")


def test_task_schema_guards() -> None:
    good = "tasks/ds/a/files"
    hidden = "tasks/ds/a/tests"
    mount = Mount(scope="task", key=good, target="/in", read_only=True)

    def manifest(
        image: str = IMAGE, mounts: tuple[Mount, ...] = (mount,), volumes: tuple[TrialVolume, ...] = (), **extra: Any
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

    def task(**kwargs: Any) -> Task:
        base: dict[str, Any] = {"key": "ds/a", "roles": {"main": manifest()}}
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
        )
    with pytest.raises(ValueError, match="hidden tests cannot initialize"):
        task(
            roles={"main": manifest(volumes=(TrialVolume(key="v", seed=hidden),))},
            verification=Verification(command=("true",), tests=hidden),
        )
    with pytest.raises(ValueError, match="canonical relative path"):
        task(roles={"main": manifest(mounts=(Mount(scope="task", key="../x", target="/in", read_only=True),))})
    with pytest.raises(ValueError, match="canonical relative path"):
        task(key="/abs")
    with pytest.raises(ValueError, match="publication paths"):
        task(roles={"main": manifest(task_files="datasets/a")})
    with pytest.raises(ValueError, match="duplicate artifact name"):
        artifact = Artifact(name="agent", role="main", target="/logs/agent")
        task(artifacts=(artifact, artifact))
    with pytest.raises(ValueError, match="unknown verification role"):
        task(verification=Verification(role="ghost", command=("true",)))
    with pytest.raises(ValueError, match="roles require main"):
        task(roles={"side": role("ds/a")})
    with pytest.raises(ValueError):
        task(schema_version=1)


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


def test_bindings_route_rows_by_release_name(tmp_path: Path) -> None:
    release_a, task_a = build_release(tmp_path / "a", name="a")
    release_b, _ = build_release(tmp_path / "b", name="b", instruction="Different instruction.\n")
    unbound, _ = build_release(tmp_path / "unbound", name="c")

    bindings = ReleaseBindings(
        {
            "a": ReleaseBinding(local_root=release_a, platform_root="/srv/shared/releases/a"),
            "b": ReleaseBinding(local_root=release_b, platform_root="/srv/shared/releases/b"),
        },
        storage=HOST,
    )
    resolved = bindings.resolve(row_of(release_a))
    assert resolved.platform_key == "releases/a"
    assert resolved.task == task_a
    assert resolved.instruction() == "Solve the task.\n"
    assert resolved.directory == release_a / "tasks" / "ds" / "task-a"
    assert bindings.resolve(row_of(release_b)).instruction() == "Different instruction.\n"
    assert len(bindings.read_dataset(release_a / "dataset.jsonl")) == 1

    with pytest.raises(ValueError, match="no \\[task_releases\\] binding"):
        bindings.resolve(row_of(unbound))
    with pytest.raises(ValueError, match="without a \\[task_releases\\] binding"):
        bindings.read_dataset(unbound / "dataset.jsonl")
    with pytest.raises(ValueError, match="platform_root is required"):
        ReleaseBindings({"a": ReleaseBinding(local_root=release_a, platform_root="")}, storage=HOST)
    with pytest.raises(ValueError, match="unconfigured storage root"):
        ReleaseBindings({"a": ReleaseBinding(local_root=release_a, platform_root="x", root="datasets")}, storage=HOST)


def test_read_dataset_reports_reject_line_numbers(tmp_path: Path) -> None:
    path = tmp_path / "dataset.jsonl"
    valid = '{"release": "rel", "task": "ds/x"}'
    path.write_text(valid + "\nnot json\n")
    with pytest.raises(ValueError, match=r"dataset\.jsonl:2: invalid native row"):
        read_dataset(path)
    path.write_text('{"release_id": "' + "0" * 64 + '", "task_ref": "tasks/x/task.json"}\n')
    with pytest.raises(ValueError, match=r"dataset\.jsonl:1: invalid native row"):  # v1 rows are not read
        read_dataset(path)
    path.write_text("\n" + valid + "\n\n")
    assert len(read_dataset(path)) == 1  # blank lines are skipped, not fatal


# ───────────────────────── Installed helper and import boundaries ─────────────────────────


def helper_process(tmp_path: Path, *args: str) -> subprocess.CompletedProcess[str]:
    module = __import__("flotilla")
    assert module.__file__ is not None
    source = Path(module.__file__).parents[1]
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


def test_installed_helper_clears_output_directory_in_place(tmp_path: Path) -> None:
    output = tmp_path / "output"
    (output / "nested").mkdir(parents=True)
    (output / "reward.txt").write_text("1")
    (output / "link").symlink_to(tmp_path)
    inode = output.stat().st_ino
    result = helper_process(tmp_path, "--clear-directory", str(output))
    assert result.returncode == 0, result.stderr
    assert list(output.iterdir()) == [] and output.stat().st_ino == inode
    assert tmp_path.exists()
    assert helper_process(tmp_path, "--clear-directory", str(tmp_path / "missing")).returncode != 0


def test_native_modules_never_import_compose_or_builder(tmp_path: Path) -> None:
    module = __import__("flotilla")
    assert module.__file__ is not None
    source = Path(module.__file__).parents[1]
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
