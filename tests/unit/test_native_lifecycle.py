"""Consume-only NativeAttempt lifecycle on the fake platform: handoff, isolation, cleanup."""

from __future__ import annotations

import shutil
from dataclasses import replace
from pathlib import Path
from typing import Any

import pytest

from flotilla import Flotilla
from flotilla.config import FlotillaConfig, Storage
from flotilla.core import labels
from flotilla.models.plan import TaskSource, TrialPlan
from flotilla.native import Dataset, NativeAttempt
from flotilla.platform.base import FlotillaError, InstanceSpec, SharedVolume
from flotilla.runtime.manager import Manager
from flotilla.runtime.types import OpenOptions, RunRequest
from flotilla.testing.attempt import AttemptHarness, prepared_source, write_task
from flotilla.testing.planning import DATASET_ROOT, FakeConfigs
from flotilla.testing.platform import FakePlatform, SharedTree
from flotilla.testing.runtime import deployment as deployment

IMAGE = "example.invalid/unit@sha256:" + "c" * 64
#: What `verification_of` computes for a task that ships a `tests/` directory.
VERIFY_COMMAND = ("bash", "/tests/test.sh")
HOST = Storage(volumes="host", host_path="/srv/shared").settings()
Deployment = tuple[FlotillaConfig, FakePlatform, SharedTree]

#: The task input, as the trial mounts it: a read-only bind is read where it is published.
INPUT_KEY = "environment/input"


def volume_of(spec: InstanceSpec, mount_path: str) -> SharedVolume | None:
    matches = [v for v in spec.volumes if v.mount_path == mount_path]
    return matches[0] if matches else None


def prepare(
    tmp_path: Path,
    deployment: Deployment,
    monkeypatch: pytest.MonkeyPatch,
    name: str = "tb1",
    *,
    verifier: bool = True,
    shared_verifier: bool = False,
) -> tuple[FlotillaConfig, FakePlatform, SharedTree, AttemptHarness, TaskSource]:
    """Write one task, publish it on the fake shared tree, and install the attempt harness."""
    cfg, platform, tree = deployment
    harness = AttemptHarness(platform, tree, monkeypatch)
    source = prepared_source(
        tmp_path,
        platform,
        tree,
        name=name,
        image=IMAGE,
        verifier=verifier,
        shared_verifier=shared_verifier,
    )
    return cfg, platform, tree, harness, source


@pytest.fixture(autouse=True)
def offline_registry(monkeypatch: pytest.MonkeyPatch) -> None:
    """Resolving a task reads each image's config; these tests are about the lifecycle."""
    original = Flotilla.__init__

    def patched(self: Flotilla, *args: Any, **kwargs: Any) -> None:
        original(self, *args, **kwargs)
        self.image_configs = FakeConfigs()  # type: ignore[assignment]

    monkeypatch.setattr(Flotilla, "__init__", patched)


@pytest.mark.asyncio
@pytest.mark.parametrize("with_optional", [False, True])
async def test_consume_only_lifecycle_handoff_and_cleanup(
    tmp_path: Path, deployment: Deployment, monkeypatch: pytest.MonkeyPatch, with_optional: bool
) -> None:
    cfg, platform, tree, harness, source = prepare(tmp_path, deployment, monkeypatch)
    harness.with_optional = with_optional

    async with Flotilla(deployment=cfg) as fleet:
        attempt = await NativeAttempt.create(fleet, source)
        harness.workspace_key = attempt.workspace.key

        await attempt.open("main")
        units = [i for i in platform.iids() if platform.spec_of(i).labels.get(labels.ROLE) == labels.ROLE_UNIT]
        assert len(units) == 1
        main_spec = platform.spec_of(units[0])
        mounted = volume_of(main_spec, "/input")
        assert mounted is not None and mounted.read_only
        # A read-only bind is mounted where it is published; nothing is copied for the trial.
        assert mounted.key == f"tb1/ds/task-a/{INPUT_KEY}" and mounted.root == DATASET_ROOT
        artifacts = volume_of(main_spec, "/logs/agent")
        assert artifacts is not None and not artifacts.read_only
        assert artifacts.key == f"{attempt.workspace.key}/artifacts/agent"

        # The agent produces its artifact through the writable private mount.
        answer = f"{attempt.workspace.key}/artifacts/agent/answer.txt"
        tree.files.add(answer)
        platform.tree_contents[answer] = b"agent-owned-answer"

        result = await attempt.verify()
        assert result.exit_code == 0
        assert result.outputs["reward.txt"] == b"reward:agent-owned-answer"  # bytes flowed through the handoff
        assert ("reward.json" in result.outputs) is with_optional
        (spec,) = harness.verification_specs
        assert spec.argv == VERIFY_COMMAND
        assert spec.env.get("TASK_TOKEN") == "native"  # [verifier].env reaches the grading command

        units = [i for i in platform.iids() if platform.spec_of(i).labels.get(labels.ROLE) == labels.ROLE_UNIT]
        assert len(units) == 1  # main was released before the verifier started
        verifier_spec = platform.spec_of(units[0])
        tests_volume = volume_of(verifier_spec, "/tests")
        assert tests_volume is not None and tests_volume.read_only
        assert tests_volume.key == f"{attempt.workspace.key}/tests"
        handoff = volume_of(verifier_spec, "/handoff/agent")
        assert handoff is not None and handoff.read_only
        assert handoff.key == f"{attempt.workspace.key}/handoff/agent"
        output = volume_of(verifier_spec, "/logs/verifier")
        assert output is not None and not output.read_only
        assert output.key == f"{attempt.workspace.key}/verification"
        assert volume_of(verifier_spec, "/input") is None  # task input never reaches the verifier

        # Hidden tests were staged only now, from the dataset on shared storage.
        assert platform.tree_contents.get(f"{attempt.workspace.key}/tests/test.sh") is not None

        downloaded = tmp_path / "downloaded"
        await attempt.download_artifact("agent", downloaded)
        assert (downloaded / "answer.txt").read_bytes() == b"agent-owned-answer"

        await attempt.close()
        assert not any(key.startswith(attempt.workspace.key) for key in tree.dirs | tree.files)
        assert not [i for i in platform.iids() if platform.spec_of(i).labels.get(labels.ROLE) == labels.ROLE_UNIT]

        second = await NativeAttempt.create(fleet, source)
        await second.close()
    assert platform.instances() == []  # fleet teardown deleted anchor and units


@pytest.mark.asyncio
async def test_attempt_isolation_guards(
    tmp_path: Path, deployment: Deployment, monkeypatch: pytest.MonkeyPatch
) -> None:
    cfg, _platform, tree, harness, source = prepare(tmp_path, deployment, monkeypatch)
    plain = prepared_source(tmp_path, harness.platform, tree, name="plain", verifier=False)

    async with Flotilla(deployment=cfg) as fleet:
        attempt = await NativeAttempt.create(fleet, source)
        harness.workspace_key = attempt.workspace.key
        with pytest.raises(ValueError, match=r"through verify\(\)"):
            await attempt.open("verifier")
        with pytest.raises(ValueError, match="owns workspace mounts"):
            await attempt.open("main", OpenOptions(workspace_id="foreign"))
        with pytest.raises(ValueError, match="owns workspace mounts"):
            await attempt.open(
                "main",
                OpenOptions(workspace_mounts={"main": (SharedVolume(key="x", mount_path="/x", read_only=True),)}),
            )
        with pytest.raises(ValueError, match="unknown artifact"):
            await attempt.download_artifact("ghost", tmp_path / "ghost")
        with pytest.raises(ValueError, match="unknown native role"):
            await attempt.open("side")
        await attempt.close()
        with pytest.raises(RuntimeError, match="closed"):
            await attempt.open("main")
        with pytest.raises(RuntimeError):
            await attempt.verify()

        plain_attempt = await NativeAttempt.create(fleet, plain)
        with pytest.raises(ValueError, match="no verification interface"):
            await plain_attempt.verify()
        await plain_attempt.close()


@pytest.mark.asyncio
async def test_verification_runs_once_and_main_stays_closed_after_handoff(
    tmp_path: Path, deployment: Deployment, monkeypatch: pytest.MonkeyPatch
) -> None:
    cfg, platform, tree, harness, source = prepare(tmp_path, deployment, monkeypatch)

    async with Flotilla(deployment=cfg) as fleet:
        attempt = await NativeAttempt.create(fleet, source)
        harness.workspace_key = attempt.workspace.key
        await attempt.open("main")
        answer = f"{attempt.workspace.key}/artifacts/agent/answer.txt"
        tree.files.add(answer)
        platform.tree_contents[answer] = b"answer"
        assert (await attempt.verify()).exit_code == 0

        with pytest.raises(RuntimeError, match="only once"):
            await attempt.verify()
        with pytest.raises(RuntimeError, match="agent phase has ended"):
            await attempt.open("main")
        # The verifier itself stays usable until the attempt closes.
        assert (await attempt.open_verification_role()) is attempt.environments["verifier"]
        await attempt.release("verifier")
        await attempt.close()


@pytest.mark.asyncio
async def test_verification_steps_run_in_order_with_gates(
    tmp_path: Path, deployment: Deployment, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The three verification primitives are `verify` taken apart, and each refuses to run early."""
    cfg, platform, _tree, harness, source = prepare(tmp_path, deployment, monkeypatch)

    async with Flotilla(deployment=cfg) as fleet:
        attempt = await NativeAttempt.create(fleet, source)
        harness.workspace_key = attempt.workspace.key
        await attempt.open("main")

        with pytest.raises(RuntimeError, match="end the agent phase"):
            await attempt.stage_verification()
        with pytest.raises(RuntimeError, match="stage the verification"):
            await attempt.open_verification_role()

        # Ending the agent phase releases main when grading runs in a separate verifier,
        # before any staging or verifier open -- which is the budget claim verify() relied on.
        await attempt.end_agent_phase()
        assert "main" not in attempt.environments
        assert not [i for i in platform.iids() if platform.spec_of(i).labels.get(labels.ROLE) == labels.ROLE_UNIT]
        with pytest.raises(RuntimeError, match="agent phase has ended"):
            await attempt.open("main")
        await attempt.end_agent_phase()  # ending twice is a no-op, not an error

        await attempt.stage_verification()
        with pytest.raises(RuntimeError, match="only once"):
            await attempt.stage_verification()
        assert (await attempt.open_verification_role()) is attempt.environments["verifier"]

        # verify() is the same composition, so it is subject to the same gates.
        with pytest.raises(RuntimeError, match="only once"):
            await attempt.verify()

        await attempt.release("verifier")
        await attempt.close()
        with pytest.raises(RuntimeError, match="closed"):
            await attempt.end_agent_phase()
        with pytest.raises(RuntimeError, match="closed"):
            await attempt.stage_verification()
        with pytest.raises(RuntimeError, match="closed"):
            await attempt.open_verification_role()


@pytest.mark.asyncio
async def test_shared_verifier_grades_in_main_through_the_primitives(
    tmp_path: Path, deployment: Deployment, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A task without a separate verifier environment is graded in main, and main is kept."""
    cfg, platform, tree, harness, source = prepare(
        tmp_path, deployment, monkeypatch, "shared", verifier=False, shared_verifier=True
    )

    async with Flotilla(deployment=cfg) as fleet:
        attempt = await NativeAttempt.create(fleet, source)
        harness.workspace_key = attempt.workspace.key
        main = await attempt.open("main")
        answer = f"{attempt.workspace.key}/artifacts/agent/answer.txt"
        tree.files.add(answer)
        platform.tree_contents[answer] = b"shared-answer"

        await attempt.end_agent_phase()
        # Grading in main: the sandbox the agent used is not released at the phase boundary.
        assert attempt.environments["main"] is main
        await attempt.stage_verification()
        # The workspace mount main already carries is filled only now: the tests were hidden.
        assert platform.tree_contents.get(f"{attempt.workspace.key}/tests/test.sh") is not None
        assert (await attempt.open_verification_role()) is main

        # A consumer driving the steps itself runs the grading command on the returned handle.
        service = main.service("main")
        exec = await service.exec(VERIFY_COMMAND, timeout=30)
        assert exec.exit_code == 0
        assert await service.read_file("/logs/verifier/reward.txt") == b"reward:shared-answer"
        (spec,) = harness.verification_specs
        assert spec.env.get("TASK_TOKEN") is None  # no [verifier] table: nothing is passed

        with pytest.raises(RuntimeError, match="agent phase has ended"):
            await attempt.open("main")
        await attempt.close()


@pytest.mark.asyncio
async def test_create_failure_releases_attempt_directory(
    tmp_path: Path, deployment: Deployment, monkeypatch: pytest.MonkeyPatch
) -> None:
    cfg, _platform, tree, harness, source = prepare(tmp_path, deployment, monkeypatch)
    harness.fail_chown_of = "artifacts/agent"

    async with Flotilla(deployment=cfg) as fleet:
        with pytest.raises(FlotillaError):
            await NativeAttempt.create(fleet, source)
        assert not any("/attempts/" in key for key in tree.dirs | tree.files)


@pytest.mark.asyncio
async def test_task_files_must_be_task_data_on_declared_storage(
    tmp_path: Path, deployment: Deployment, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A dataset may live on any declared storage root, but never where flotilla keeps its own state.

    Task files are mounted read-only, so a dataset published anywhere the deployment declared is
    fine. What a plan must not name is a launch tree or the shared tools: those are not task data,
    and a launch tree is another client's.
    """
    cfg, _platform, _tree, _harness, source = prepare(tmp_path, deployment, monkeypatch)
    published = NativeAttempt.resolve(Flotilla(deployment=cfg), source).plans["main"]
    on_work_root = replace(published, task_files_root="work")
    in_a_launch_tree = replace(published, task_files_root="work", task_files="launches/L9/t1")
    undeclared = replace(published, task_files_root="nowhere")

    def plan_of(plan: TrialPlan) -> Any:
        async def run(manager: Manager) -> TrialPlan:
            return manager.plan(RunRequest(plan, "r1"))

        return run

    async with Flotilla(deployment=cfg) as fleet:
        with pytest.raises(FlotillaError, match="which flotilla owns"):
            await fleet._call(plan_of(in_a_launch_tree))
        with pytest.raises(FlotillaError, match="undeclared storage root"):
            await fleet._call(plan_of(undeclared))
        # Elsewhere on declared storage the same plan is admitted, on either root.
        assert (await fleet._call(plan_of(published))).task_files == published.task_files
        assert (await fleet._call(plan_of(on_work_root))).task_files == published.task_files


@pytest.mark.asyncio
async def test_releases_on_named_read_only_input_roots(
    tmp_path: Path, deployment: Deployment, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Datasets come from two named read-only input roots; attempt state stays on the work root."""
    cfg, platform, tree = deployment
    inputs = {"datasets": Storage(host_path="@user:owner"), "extra": Storage(host_path="@user:other")}
    cfg = cfg.model_copy(update={"inputs": inputs})
    tree.dirs.update({tree.input_key("datasets"), tree.input_key("extra")})
    published = write_task(tmp_path / "build", image=IMAGE, verifier=True)
    platform_copy = tmp_path / "platform-copy"
    shutil.copytree(published, platform_copy, symlinks=True)
    harness = AttemptHarness(platform, tree, monkeypatch)
    # The same dataset content on both roots: each attempt mounts its own root.
    harness.stage_dataset(tree.input_key("datasets", "tb/r1"), platform_copy)
    harness.stage_dataset(tree.input_key("extra", "tb/r1"), platform_copy)
    sources = {name: Dataset(published, "tb/r1", root=name).source("ds/task-a") for name in ("datasets", "extra")}
    assert {name: s.root for name, s in sources.items()} == {"datasets": "datasets", "extra": "extra"}
    inputs_before = {k: v for k, v in platform.tree_contents.items() if k.startswith("@")}

    async with Flotilla(deployment=cfg) as fleet:
        anchors = [i for i in platform.iids() if platform.spec_of(i).labels.get(labels.ROLE) == labels.ROLE_ANCHOR]
        anchor_inputs = [v for v in platform.spec_of(anchors[0]).volumes if v.root != "work"]
        assert [(v.root, v.key, v.mount_path, v.read_only) for v in anchor_inputs] == [
            ("datasets", "", "/flotilla-inputs/datasets", True),
            ("extra", "", "/flotilla-inputs/extra", True),
        ]
        for name in ("datasets", "extra"):
            attempt = await NativeAttempt.create(fleet, sources[name])
            harness.workspace_key = attempt.workspace.key
            await attempt.open("main")
            (unit,) = [i for i in platform.iids() if platform.spec_of(i).labels.get(labels.ROLE) == labels.ROLE_UNIT]
            main_spec = platform.spec_of(unit)
            mounted = volume_of(main_spec, "/input")
            assert mounted is not None and mounted.read_only and mounted.root == name
            assert mounted.key == f"tb/r1/ds/task-a/{INPUT_KEY}"
            artifacts = volume_of(main_spec, "/logs/agent")
            assert artifacts is not None and artifacts.root == "work"
            share = volume_of(main_spec, "/.flotilla")
            assert share is not None and share.root == "work"  # share tools stay on the work root

            answer = f"{attempt.workspace.key}/artifacts/agent/answer.txt"
            tree.files.add(answer)
            platform.tree_contents[answer] = b"answer"
            result = await attempt.verify()
            assert result.exit_code == 0
            # Hidden tests were copied from the input root into the attempt's work-root directory.
            assert platform.tree_contents.get(f"{attempt.workspace.key}/tests/test.sh") is not None
            await attempt.close()
    # Nothing on an input root was written; SharedTree also fails any write attempt there.
    assert {k: v for k, v in platform.tree_contents.items() if k.startswith("@")} == inputs_before
    assert platform.instances() == []
