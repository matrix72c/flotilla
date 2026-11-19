"""A published dataset: finding its tasks and mapping it onto platform storage.

What used to be tested here was a release flotilla wrote and read back: a staging writer, a reader,
a `dataset.jsonl`, and a persisted `Task` with a schema version. None of that exists. The dataset is
an input someone else publishes, so what is left to check is the part neither side can infer -- how
a client path maps to the key a sandbox mounts -- plus the cross-checks that used to live in the
persisted task's validators.
"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import pytest

from flotilla.config import Storage
from flotilla.models.plan import ExecContext, Mount, TrialPlan, TrialVolume, Unit
from flotilla.models.verification import check_against_plans
from flotilla.native import Artifact, Dataset, DatasetError, Verification, datasets, platform_key
from flotilla.platform.base import Resources

IMAGE = "example.invalid/runtime@sha256:" + "a" * 64
HOST = Storage(volumes="host", host_path="/srv/shared").settings()
PVC = Storage(volumes="pvc", claim_name="claim", root_subpath="flotilla").settings()


def task_tree(root: Path, key: str, *, tests: bool = True) -> Path:
    """A task directory shaped the way a published dataset holds one."""
    path = root / key
    (path / "environment").mkdir(parents=True)
    (path / "task.toml").write_text(f'[environment]\ndocker_image = "{IMAGE}"\n')
    (path / "instruction.md").write_text("Solve the task.\n")
    if tests:
        (path / "tests").mkdir()
        (path / "tests/test.sh").write_text("#!/bin/sh\nexit 0\n")
    return path


def plan(*, mounts: tuple[Mount, ...] = (), volumes: tuple[TrialVolume, ...] = ()) -> TrialPlan:
    return TrialPlan(
        task="ds/a",
        task_files="datasets/ds/a",
        task_files_root="datasets",
        units={
            "main": Unit(
                image=IMAGE,
                resources=Resources(cpu="1", memory="128Mi"),
                context=ExecContext(env={}, uid=0, gid=0, cwd="/"),
                command=None,
                mounts=mounts,
            )
        },
        volumes=volumes,
    )


# ───────────────────────── Finding tasks ─────────────────────────


def test_a_dataset_lists_its_tasks_in_a_stable_order(tmp_path: Path) -> None:
    for key in ("ds/b", "ds/a", "other/c"):
        task_tree(tmp_path / "published", key)
    dataset = Dataset(tmp_path / "published", "datasets/tb")
    assert dataset.tasks() == ["ds/a", "ds/b", "other/c"]
    source = dataset.source("ds/a")
    assert source.key == "datasets/tb/ds/a"
    assert source.local == (tmp_path / "published" / "ds" / "a")


def test_a_directory_with_no_tasks_is_an_error_rather_than_an_empty_run(tmp_path: Path) -> None:
    (tmp_path / "empty").mkdir()
    with pytest.raises(DatasetError, match=r"no task\.toml"):
        Dataset(tmp_path / "empty", "datasets/tb").tasks()
    with pytest.raises(DatasetError, match="not a directory"):
        Dataset(tmp_path / "absent", "datasets/tb")


def test_a_task_key_cannot_escape_the_dataset(tmp_path: Path) -> None:
    task_tree(tmp_path / "published", "ds/a")
    outside = tmp_path / "outside"
    task_tree(outside, "ds/a")
    dataset = Dataset(tmp_path / "published", "datasets/tb")
    (tmp_path / "published" / "link").symlink_to(outside)
    with pytest.raises(DatasetError, match="escapes the dataset"):
        dataset.source("link/ds/a")
    for key in ("../outside/ds/a", "/absolute", "ds/../../outside"):
        with pytest.raises(ValueError):
            dataset.source(key)
    with pytest.raises(DatasetError, match="not a task directory"):
        dataset.source("ds")


def test_a_dataset_cannot_claim_storage_flotilla_owns(tmp_path: Path) -> None:
    task_tree(tmp_path / "published", "ds/a")
    for reserved in ("launches/x", "share/x"):
        with pytest.raises(DatasetError, match="flotilla owns"):
            Dataset(tmp_path / "published", reserved)


# ───────────────────────── Platform roots and bindings ─────────────────────────


def test_platform_key_maps_only_declared_storage_roots() -> None:
    assert platform_key("/srv/shared/datasets/tb1", HOST) == "datasets/tb1"
    assert platform_key("datasets/tb1", HOST) == "datasets/tb1"
    assert platform_key("pvc:claim/flotilla/datasets/tb1", PVC) == "datasets/tb1"
    bare = Storage(volumes="pvc", claim_name="claim").settings()
    assert platform_key("pvc:claim/datasets/tb1", bare) == "datasets/tb1"

    with pytest.raises(ValueError, match="outside configured host storage"):
        platform_key("/elsewhere/datasets/tb1", HOST)
    with pytest.raises(ValueError, match="noncanonical"):
        platform_key("/srv/shared/../shared/x", HOST)
    with pytest.raises(ValueError, match="noncanonical"):
        platform_key("/srv/shared/x/", HOST)
    with pytest.raises(ValueError):
        platform_key("/srv/shared", HOST)
    with pytest.raises(ValueError, match="outside configured PVC"):
        platform_key("pvc:other/flotilla/datasets/tb1", PVC)
    with pytest.raises(ValueError, match="outside configured PVC"):
        platform_key("pvc:claim/other/datasets/tb1", PVC)
    for reserved in ("launches/x", "share/x", "/srv/shared/launches/x"):
        with pytest.raises(ValueError, match="overlap"):
            platform_key(reserved, HOST)


def test_platform_key_rejects_cross_storage_forms() -> None:
    # A PVC identity must never resolve under host storage: its claim prefix would
    # silently become a directory name inside the host root.
    with pytest.raises(ValueError, match="host storage"):
        platform_key("pvc:claim/flotilla/datasets/tb1", HOST)
    # A bare relative root must never resolve under PVC storage: the claim name and
    # shared subpath would be ignored and the key would point outside the fleet root.
    with pytest.raises(ValueError, match="explicit pvc"):
        platform_key("datasets/tb1", PVC)


def test_declared_datasets_resolve_against_the_root_they_live_on(tmp_path: Path) -> None:
    task_tree(tmp_path / "work-side", "ds/a")
    task_tree(tmp_path / "input-side", "ds/b")
    inputs = {"datasets": Storage(volumes="host", host_path="/srv/inputs").settings()}
    built = datasets(
        {
            "onwork": {"local_root": tmp_path / "work-side", "platform_root": "/srv/shared/corpora/one"},
            "oninput": {
                "local_root": tmp_path / "input-side",
                "platform_root": "/srv/inputs/corpora/two",
                "root": "datasets",
            },
        },
        storage=HOST,
        inputs=inputs,
    )
    assert built["onwork"].source("ds/a").key == "corpora/one/ds/a"
    assert built["onwork"].source("ds/a").root == "work"
    assert built["oninput"].source("ds/b").key == "corpora/two/ds/b"
    assert built["oninput"].source("ds/b").root == "datasets"
    with pytest.raises(DatasetError, match="unconfigured storage root"):
        datasets(
            {"bad": {"local_root": tmp_path / "work-side", "platform_root": "x", "root": "absent"}},
            storage=HOST,
            inputs=inputs,
        )


# ───────────────────────── Verification against the plans ─────────────────────────


def test_the_hidden_tests_cannot_be_reached_from_any_role() -> None:
    """The tests are staged after the agent is done, so nothing it can reach may name them."""
    hidden = "tests"
    verification = Verification(command=("true",), tests=hidden)
    mounted = plan(mounts=(Mount(scope="task", key=hidden, target="/tests", read_only=True),))
    with pytest.raises(ValueError, match="mounts the hidden tests"):
        check_against_plans(verification, (), plans={"main": mounted})
    seeded = plan(volumes=(TrialVolume(key="v", copy_from=hidden),))
    with pytest.raises(ValueError, match="initialized from the hidden tests"):
        check_against_plans(verification, (), plans={"main": seeded})
    # A sibling directory whose name merely starts the same way is not the tests.
    sibling = plan(mounts=(Mount(scope="task", key="tests-data", target="/data", read_only=True),))
    check_against_plans(verification, (), plans={"main": sibling})


def test_verification_and_artifacts_must_name_roles_and_services_that_exist() -> None:
    plans = {"main": plan()}
    with pytest.raises(ValueError, match="unknown role"):
        check_against_plans(None, (Artifact(name="a", role="ghost", target="/logs/a"),), plans=plans)
    with pytest.raises(ValueError, match="unknown service"):
        check_against_plans(None, (Artifact(name="a", service="ghost", target="/logs/a"),), plans=plans)
    with pytest.raises(ValueError, match="duplicate artifact name"):
        artifact = Artifact(name="agent", target="/logs/agent")
        check_against_plans(None, (artifact, artifact), plans=plans)
    with pytest.raises(ValueError, match="verification names unknown role"):
        check_against_plans(Verification(role="ghost", command=("true",)), (), plans=plans)
    with pytest.raises(ValueError, match="unknown service"):
        check_against_plans(Verification(service="ghost", command=("true",)), (), plans=plans)
    check_against_plans(Verification(command=("true",)), (Artifact(name="agent", target="/logs/agent"),), plans=plans)


def test_paths_in_verification_and_artifacts_must_be_canonical() -> None:
    with pytest.raises(ValueError, match="canonical absolute path"):
        Artifact(name="a", target="logs/a")
    with pytest.raises(ValueError, match="one segment"):
        Artifact(name="a/b", target="/logs/a")
    with pytest.raises(ValueError, match="canonical relative path"):
        Verification(command=("true",), tests="../tests")
    with pytest.raises(ValueError, match="canonical absolute path"):
        Verification(command=("true",), output_paths={"reward.txt": "logs/reward.txt"})
    with pytest.raises(ValueError, match="uid and gid"):
        Verification(command=("true",), uid=1000)
    with pytest.raises(ValueError, match="duplicate verification output names"):
        Verification(
            command=("true",),
            output_paths={"reward.txt": "/logs/reward.txt"},
            optional_output_paths={"reward.txt": "/logs/reward.txt"},
        )


# ───────────────────────── Import boundaries ─────────────────────────


def test_the_installed_package_needs_neither_harbor_nor_a_deployment(tmp_path: Path) -> None:
    """Importing the dataset API pulls in nothing that needs a deployment or the Harbor extra.

    Compose is not on this list: the client resolves a task in-process now, so importing
    `flotilla` imports the parser. What still has to stay out is everything that would make the
    installed package need a Harbor install, an integration, or a deployment file to start.
    """
    module = __import__("flotilla")
    assert module.__file__ is not None
    source = Path(module.__file__).parents[1]
    code = (
        "import sys, flotilla.native.dataset;"
        "bad = sorted(m for m in sys.modules if m.startswith("
        "('flotilla.integrations', 'flotilla.inspect', 'flotilla.anchor_build', 'harbor')));"
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
