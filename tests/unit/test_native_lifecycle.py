"""Consume-only NativeAttempt lifecycle on the fake platform: handoff, isolation, cleanup."""

from __future__ import annotations

import io
import shutil
import tarfile
from pathlib import Path
from typing import Any

import pytest

from flotilla import Flotilla
from flotilla.capabilities import CapabilityReport
from flotilla.config import FlotillaConfig, Storage
from flotilla.core import labels
from flotilla.core.plan import TrialPlan
from flotilla.manifest import Context, Manifest, Mount, Resources, Unit
from flotilla.native import (
    Artifact,
    BoundTask,
    NativeAttempt,
    ReleaseBinding,
    ReleaseBindings,
    ReleaseWriter,
    Task,
    Verification,
    read_dataset,
)
from flotilla.platform.base import (
    ExecResult,
    FlotillaError,
    InstanceHandle,
    InstanceSpec,
    ProcessSpec,
    SharedVolume,
)
from flotilla.prepared import PreparedEnvironment
from flotilla.runtime.manager import Manager
from flotilla.runtime.types import OpenOptions, RunRequest
from flotilla.testing.platform import FakePlatform, SharedTree
from flotilla.testing.runtime import deployment as deployment

IMAGE = "example.invalid/unit@sha256:" + "c" * 64
VERIFY_COMMAND = ("verify", "run")
HOST = Storage(volumes="host", host_path="/srv/shared").settings()
Deployment = tuple[FlotillaConfig, FakePlatform, SharedTree]


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


INPUT_KEY = "tasks/ds/task-a/files"


def build_release(base: Path, *, name: str = "tb1", verifier: bool = True) -> tuple[Path, Task]:
    files = base / "input"
    (files / "nested").mkdir(parents=True)
    (files / "data.txt").write_text("static input")
    roles: dict[str, Manifest] = {
        "main": role("ds/task-a", (Mount(scope="task", key=INPUT_KEY, target="/input", read_only=True),))
    }
    copies = {"files": files}
    verification = None
    artifacts: tuple[Artifact, ...] = ()
    if verifier:
        tests = base / "tests"
        tests.mkdir()
        (tests / "check.sh").write_text("#!/bin/sh\nexit 0\n")
        copies["tests"] = tests
        roles["verifier"] = role("ds/task-a#verifier")
        verification = Verification(
            role="verifier",
            command=VERIFY_COMMAND,
            tests="tasks/ds/task-a/tests",
            env={"TASK_TOKEN": "native"},
            uid=1000,
            gid=1000,
            output_paths={"reward.txt": "/logs/verifier/reward.txt"},
            optional_output_paths={"details.txt": "/logs/verifier/details.txt"},
            require_output=True,
        )
        artifacts = (Artifact(name="agent", role="main", target="/logs/agent", verifier_target="/handoff/agent"),)
    task = Task(key="ds/task-a", roles=roles, verification=verification, artifacts=artifacts)
    with ReleaseWriter(base / "release", name) as writer:
        writer.add_task(task, "Solve the task.\n", copies)
        writer.publish([{"task": "ds/task-a"}])
    return base / "release", task


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

    def stage_release(self, key: str, root: Path) -> None:
        """Place a release copy on the fake shared storage as tree entries for anchor operations."""
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
            if argv[1] in ("rm", "mkdir"):
                return ExecResult(0, b"", b"")
            raise AssertionError(f"unexpected busybox command: {argv!r}")
        if argv == VERIFY_COMMAND:
            self.verification_specs.append(proc)
            answer = self.platform.tree_contents.get(f"{self.workspace_key}/handoff/agent/answer.txt")
            assert answer is not None, "verifier did not receive the agent artifact through the handoff"
            self.unit_files[(iid, "/logs/verifier/reward.txt")] = b"reward:" + answer
            if self.with_optional:
                self.unit_files[(iid, "/logs/verifier/details.txt")] = b"details"
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
) -> tuple[FlotillaConfig, FakePlatform, SharedTree, NativeHarness, BoundTask, Task]:
    """Build a release, stage a separate platform copy, and install the harness."""
    cfg, platform, tree = deployment
    root, task = build_release(tmp_path / "build", name=name, verifier=verifier)
    platform_copy = tmp_path / "platform-copy"
    shutil.copytree(root, platform_copy, symlinks=True)
    harness = NativeHarness(deployment, monkeypatch)
    harness.stage_release(f"releases/{name}", platform_copy)
    bindings = ReleaseBindings(
        {name: ReleaseBinding(local_root=root, platform_root=f"/srv/shared/releases/{name}")}, storage=HOST
    )
    return cfg, platform, tree, harness, bindings.resolve(read_dataset(root / "dataset.jsonl")[0]), task


@pytest.mark.asyncio
@pytest.mark.parametrize("with_optional", [False, True])
async def test_consume_only_lifecycle_handoff_and_cleanup(
    tmp_path: Path, deployment: Deployment, monkeypatch: pytest.MonkeyPatch, with_optional: bool
) -> None:
    cfg, platform, tree, harness, bound, _task = prepare(tmp_path, deployment, monkeypatch)
    harness.with_optional = with_optional

    async with Flotilla(deployment=cfg) as fleet:
        attempt = await NativeAttempt.create(fleet, bound)
        harness.workspace_key = attempt.workspace.key

        await attempt.open("main")
        units = [i for i in platform.iids() if platform.spec_of(i).labels.get(labels.ROLE) == labels.ROLE_UNIT]
        assert len(units) == 1
        main_spec = platform.spec_of(units[0])
        source = volume_of(main_spec, "/input")
        assert source is not None and source.read_only
        assert source.key == f"releases/tb1/{INPUT_KEY}"  # input binds the published release, not a rebuild
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
        assert ("details.txt" in result.outputs) is with_optional
        (spec,) = harness.verification_specs
        assert spec.argv == VERIFY_COMMAND
        assert (spec.uid, spec.gid) == (1000, 1000)
        assert spec.env.get("TASK_TOKEN") == "native"

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

        # Hidden tests were staged only now, from the release on shared storage.
        assert platform.tree_contents.get(f"{attempt.workspace.key}/tests/check.sh") is not None

        downloaded = tmp_path / "downloaded"
        await attempt.download_artifact("agent", downloaded)
        assert (downloaded / "answer.txt").read_bytes() == b"agent-owned-answer"

        await attempt.close()
        assert not any(key.startswith(attempt.workspace.key) for key in tree.dirs | tree.files)
        assert not [i for i in platform.iids() if platform.spec_of(i).labels.get(labels.ROLE) == labels.ROLE_UNIT]

        second = await NativeAttempt.create(fleet, bound)
        await second.close()
    assert platform.instances() == []  # fleet teardown deleted anchor and units


@pytest.mark.asyncio
async def test_attempt_isolation_guards(
    tmp_path: Path, deployment: Deployment, monkeypatch: pytest.MonkeyPatch
) -> None:
    cfg, _platform, _tree, harness, bound, _ = prepare(tmp_path, deployment, monkeypatch)
    plain_root, _ = build_release(tmp_path / "plain", name="plain", verifier=False)
    plain_copy = tmp_path / "plain-copy"
    shutil.copytree(plain_root, plain_copy, symlinks=True)
    harness.stage_release("releases/plain", plain_copy)
    plain = ReleaseBindings(
        {"plain": ReleaseBinding(local_root=plain_root, platform_root="/srv/shared/releases/plain")}, storage=HOST
    ).resolve(read_dataset(plain_root / "dataset.jsonl")[0])

    async with Flotilla(deployment=cfg) as fleet:
        attempt = await NativeAttempt.create(fleet, bound)
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
    cfg, platform, tree, harness, bound, _ = prepare(tmp_path, deployment, monkeypatch)

    async with Flotilla(deployment=cfg) as fleet:
        attempt = await NativeAttempt.create(fleet, bound)
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
    cfg, _platform, tree, harness, bound, _ = prepare(tmp_path, deployment, monkeypatch)
    harness.fail_chown_of = "artifacts/agent"

    async with Flotilla(deployment=cfg) as fleet:
        with pytest.raises(FlotillaError):
            await NativeAttempt.create(fleet, bound)
        assert not any("/attempts/" in key for key in tree.dirs | tree.files)


@pytest.mark.asyncio
async def test_native_task_files_rejected_until_an_attempt_registers_them(
    tmp_path: Path, deployment: Deployment, monkeypatch: pytest.MonkeyPatch
) -> None:
    cfg, _platform, _tree, _harness, bound, _ = prepare(tmp_path, deployment, monkeypatch)
    request = RunRequest(
        task=PreparedEnvironment(bound.task.roles["main"].model_copy(update={"task_files": bound.platform_key})),
        request_id="r1",
    )

    async def plan_of(manager: Manager) -> TrialPlan:
        return manager.plan(request)

    async with Flotilla(deployment=cfg) as fleet:
        with pytest.raises(FlotillaError, match="validated native release"):
            await fleet._call(plan_of)
        attempt = await NativeAttempt.create(fleet, bound)
        plan = await fleet._call(plan_of)
        assert plan.task_files == bound.platform_key
        await attempt.close()


@pytest.mark.asyncio
async def test_releases_on_named_read_only_input_roots(
    tmp_path: Path, deployment: Deployment, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Releases come from two named read-only input roots; attempt state stays on the work root."""
    cfg, platform, tree = deployment
    inputs = {"datasets": Storage(host_path="@user:owner"), "extra": Storage(host_path="@user:other")}
    cfg = cfg.model_copy(update={"inputs": inputs})
    report_path = cfg.capabilities_path()
    caps = CapabilityReport.load_json(report_path.read_bytes()).model_copy(
        update={"evidence_profile": cfg.evidence_profile()}
    )
    report_path.write_text(caps.dump_json())
    tree.dirs.update({tree.input_key("datasets"), tree.input_key("extra")})
    root, _task = build_release(tmp_path / "build", name="r1", verifier=True)
    platform_copy = tmp_path / "platform-copy"
    shutil.copytree(root, platform_copy, symlinks=True)
    harness = NativeHarness((cfg, platform, tree), monkeypatch)
    # The same release content on both roots: each attempt mounts its own root.
    harness.stage_release(tree.input_key("datasets", "tb/r1"), platform_copy)
    harness.stage_release(tree.input_key("extra", "tb/r1"), platform_copy)
    # The same release name bound on each root, through separate binding sets.
    record = read_dataset(root / "dataset.jsonl")[0]
    bound = {
        name: ReleaseBindings(
            {"r1": ReleaseBinding(local_root=root, platform_root="tb/r1", root=name)},
            storage=cfg.storage_settings(),
            inputs=cfg.input_settings(),
        ).resolve(record)
        for name in ("datasets", "extra")
    }
    assert {name: b.root for name, b in bound.items()} == {"datasets": "datasets", "extra": "extra"}
    inputs_before = {k: v for k, v in platform.tree_contents.items() if k.startswith("@")}

    async with Flotilla(deployment=cfg) as fleet:
        anchors = [i for i in platform.iids() if platform.spec_of(i).labels.get(labels.ROLE) == labels.ROLE_ANCHOR]
        anchor_inputs = [v for v in platform.spec_of(anchors[0]).volumes if v.root != "work"]
        assert [(v.root, v.key, v.mount_path, v.read_only) for v in anchor_inputs] == [
            ("datasets", "", "/flotilla-inputs/datasets", True),
            ("extra", "", "/flotilla-inputs/extra", True),
        ]
        for name in ("datasets", "extra"):
            attempt = await NativeAttempt.create(fleet, bound[name])
            harness.workspace_key = attempt.workspace.key
            await attempt.open("main")
            (unit,) = [i for i in platform.iids() if platform.spec_of(i).labels.get(labels.ROLE) == labels.ROLE_UNIT]
            main_spec = platform.spec_of(unit)
            source = volume_of(main_spec, "/input")
            assert source is not None and source.read_only and source.root == name
            assert source.key == f"tb/r1/{INPUT_KEY}"
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
            assert platform.tree_contents.get(f"{attempt.workspace.key}/tests/check.sh") is not None
            await attempt.close()
    # Nothing on an input root was written; SharedTree also fails any write attempt there.
    assert {k: v for k, v in platform.tree_contents.items() if k.startswith("@")} == inputs_before
    assert platform.instances() == []
