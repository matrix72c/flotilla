"""Consume-only NativeAttempt lifecycle on the fake platform: handoff, isolation, cleanup."""

from __future__ import annotations

import io
import shutil
import tarfile
from dataclasses import replace
from pathlib import Path
from typing import Any

import pytest

from flotilla import Flotilla
from flotilla.config import FlotillaConfig, Storage
from flotilla.core import labels
from flotilla.core.plan import TaskSource, TrialPlan
from flotilla.native import Dataset, NativeAttempt
from flotilla.platform.base import (
    ExecResult,
    FlotillaError,
    InstanceHandle,
    InstanceSpec,
    ProcessSpec,
    SharedVolume,
)
from flotilla.runtime.manager import Manager
from flotilla.runtime.types import OpenOptions, RunRequest
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


def build_dataset(base: Path, *, verifier: bool = True) -> Path:
    """One published task: a read-only bind input, an instruction, and optionally hidden tests."""
    task = base / "dataset" / "ds" / "task-a"
    files = task / "environment" / "input"
    (files / "nested").mkdir(parents=True)
    (files / "data.txt").write_text("static input")
    (task / "instruction.md").write_text("Solve the task.\n")
    config = f'[environment]\ndocker_image = "{IMAGE}"\ncpus = 1\nmemory_mb = 128\n'
    if verifier:
        (task / "tests").mkdir()
        (task / "tests/test.sh").write_text("#!/bin/sh\nexit 0\n")
        config += '[verifier]\nenvironment_mode = "separate"\nenv = {TASK_TOKEN = "native"}\n'
        # Its own Compose project, so the agent's task input is not in the verifier's sandbox.
        (task / "tests/docker-compose.yaml").write_text(f"services:\n  main:\n    image: {IMAGE}\n")
    (task / "task.toml").write_text(config)
    (task / "environment/docker-compose.yaml").write_text(
        "services:\n  main:\n    volumes:\n      - ./input:/input:ro\n"
    )
    return base / "dataset"


def volume_of(spec: InstanceSpec, mount_path: str) -> SharedVolume | None:
    matches = [v for v in spec.volumes if v.mount_path == mount_path]
    return matches[0] if matches else None


class NativeHarness:
    """Fake-platform shims for the consume-only lifecycle, local to these tests.

    Anchor side: emulates the installed helper's `--clear-directory`, `cp -a -- src/. dest`
    (the one SharedTree does not model) and can inject a mid-create directory failure.
    Unit side: interprets the verification command (asserting the handoff content),
    serves output reads and optional-output probes from a side table, and builds
    real tars for download_dir from the shared tree.
    """

    def stage_dataset(self, key: str, root: Path) -> None:
        """Place a dataset copy on the fake shared storage as tree entries for anchor operations."""
        for path in sorted(root.rglob("*")):
            entry = f"{key}/{path.relative_to(root).as_posix()}"
            info = path.lstat()
            if path.is_dir():
                self.tree.dirs.add(entry)
            else:
                self.tree.files.add(entry)
                self.platform.tree_contents[entry] = path.read_bytes()
                self.tree.modes[entry] = oct(info.st_mode & 0o777)
            self.tree.owners[entry] = f"{info.st_uid}:{info.st_gid}"

    def __init__(self, deployment: Deployment, monkeypatch: pytest.MonkeyPatch) -> None:
        _, self.platform, self.tree = deployment
        self.unit_files: dict[tuple[str, str], bytes] = {}
        self.verification_specs: list[ProcessSpec] = []
        self.with_optional = False
        self.workspace_key: str | None = None
        self.fail_chown_of: str | None = None
        platform, _tree = self.platform, self.tree
        original_exec, original_read = platform.exec, platform.read_file

        async def execute(handle: InstanceHandle, proc: ProcessSpec, **kwargs: Any) -> ExecResult:
            spec = platform.spec_of(handle.iid)
            argv = proc.argv
            if spec.labels.get(labels.ROLE) == labels.ROLE_ANCHOR:
                if argv[:4] == ("python3", "-I", "-m", "flotilla.native.platform_verify"):
                    return self._helper(argv)
                if argv[0] == "cp" and argv[-2].endswith("/."):
                    return self._copy_contents(argv)
                if argv[0] == "chown" and self.fail_chown_of and argv[-1].endswith(self.fail_chown_of):
                    return ExecResult(1, b"", b"injected failure")
                return await original_exec(handle, proc, **kwargs)
            return self._unit_exec(spec, handle.iid, proc)

        async def read(handle: InstanceHandle, path: str, **kwargs: Any) -> bytes:
            data = self.unit_files.get((handle.iid, path))
            if data is not None:
                return data
            return await original_read(handle, path, **kwargs)

        monkeypatch.setattr(platform, "exec", execute)
        monkeypatch.setattr(platform, "read_file", read)

    # anchor-side emulation

    def _helper(self, argv: tuple[str, ...]) -> ExecResult:
        rel = self.tree._rel
        if "--clear-directory" in argv:
            key = rel(argv[5], write=True)
            for entries in (self.tree.dirs, self.tree.files):
                entries.difference_update(k for k in entries if k.startswith(key + "/"))
            for book in (self.tree.modes, self.tree.owners, self.platform.tree_contents):
                for key_part in [k for k in book if k.startswith(key + "/")]:
                    del book[key_part]
            return ExecResult(0, b"", b"")
        raise AssertionError(f"unexpected installed helper call: {argv!r}")

    def _copy_contents(self, argv: tuple[str, ...]) -> ExecResult:
        src, dst = self.tree._rel(argv[-2].removesuffix("/.")), self.tree._rel(argv[-1], write=True)
        for entries in (self.tree.dirs, self.tree.files):
            entries.update(dst + p[len(src) :] for p in list(entries) if p.startswith(src + "/"))
        for book in (self.tree.modes, self.tree.owners):
            book.update({dst + k[len(src) :]: v for k, v in list(book.items()) if k.startswith(src + "/")})
        contents = self.platform.tree_contents
        contents.update({dst + k[len(src) :]: v for k, v in list(contents.items()) if k.startswith(src + "/")})
        return ExecResult(0, b"", b"")

    # unit-side emulation

    def _unit_exec(self, spec: InstanceSpec, iid: str, proc: ProcessSpec) -> ExecResult:
        argv = proc.argv
        if argv == ("true",):
            return ExecResult(0, b"", b"")
        if len(argv) == 3 and argv[:2] == ("test", "-f"):
            return ExecResult(0 if (iid, argv[2]) in self.unit_files else 1, b"", b"")
        if argv[0] == "/.flotilla/bin/busybox":
            if argv[1:3] == ("tar", "-cf"):
                source = argv[5]
                volume = next((v for v in spec.volumes if v.mount_path == source), None)
                assert volume is not None, f"download source {source} is not a mounted volume"
                self.unit_files[(iid, argv[3])] = self._tar_of(volume.key)
                return ExecResult(0, b"", b"")
            if argv[1] in ("rm", "mkdir", "sh"):
                # sh -c covers the runtime's own start-up bookkeeping (log file, ownership).
                return ExecResult(0, b"", b"")
            raise AssertionError(f"unexpected busybox command: {argv!r}")
        if argv == VERIFY_COMMAND:
            self.verification_specs.append(proc)
            answer = self.platform.tree_contents.get(f"{self.workspace_key}/handoff/agent/answer.txt")
            assert answer is not None, "verifier did not receive the agent artifact through the handoff"
            self.unit_files[(iid, "/logs/verifier/reward.txt")] = b"reward:" + answer
            if self.with_optional:
                # Both reward forms are optional; a verifier may write either, or both.
                self.unit_files[(iid, "/logs/verifier/reward.json")] = b'{"reward": 1}'

            return ExecResult(0, b"checked\n", b"")
        raise AssertionError(f"unexpected unit execution: {argv!r}")

    def _tar_of(self, prefix: str) -> bytes:
        buffer = io.BytesIO()
        with tarfile.open(fileobj=buffer, mode="w") as archive:
            for key in sorted(self.platform.tree_contents):
                if not key.startswith(prefix + "/"):
                    continue
                info = tarfile.TarInfo(key[len(prefix) + 1 :])
                info.mode = 0o644
                payload = self.platform.tree_contents[key]
                info.size = len(payload)
                archive.addfile(info, io.BytesIO(payload))
        return buffer.getvalue()


def prepare(
    tmp_path: Path,
    deployment: Deployment,
    monkeypatch: pytest.MonkeyPatch,
    name: str = "tb1",
    *,
    verifier: bool = True,
) -> tuple[FlotillaConfig, FakePlatform, SharedTree, NativeHarness, TaskSource]:
    """Publish a dataset, stage a separate platform copy, and install the harness."""
    cfg, platform, tree = deployment
    root = build_dataset(tmp_path / name, verifier=verifier)
    platform_copy = tmp_path / f"platform-copy-{name}"
    shutil.copytree(root, platform_copy, symlinks=True)
    harness = NativeHarness(deployment, monkeypatch)
    # A published dataset lives on a declared read-only input root (`inputs.datasets`).
    harness.stage_dataset(tree.input_key(DATASET_ROOT, name), platform_copy)
    source = Dataset(root, name, root=DATASET_ROOT).source("ds/task-a")
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
    plain_root = build_dataset(tmp_path / "plain-src", verifier=False)
    plain_copy = tmp_path / "plain-copy"
    shutil.copytree(plain_root, plain_copy, symlinks=True)
    harness.stage_dataset(tree.input_key(DATASET_ROOT, "plain"), plain_copy)
    plain = Dataset(plain_root, "plain", root=DATASET_ROOT).source("ds/task-a")

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
        with pytest.raises(RuntimeError, match="cannot reopen main"):
            await attempt.open("main")
        # The verifier itself stays usable until the attempt closes.
        assert (await attempt.open("verifier")) is attempt.environments["verifier"]
        await attempt.release("verifier")
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
    published = build_dataset(tmp_path / "build", verifier=True)
    platform_copy = tmp_path / "platform-copy"
    shutil.copytree(published, platform_copy, symlinks=True)
    harness = NativeHarness((cfg, platform, tree), monkeypatch)
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
