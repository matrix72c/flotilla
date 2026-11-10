"""Consume-only NativeAttempt lifecycle on the fake platform: handoff, isolation, cleanup."""

from __future__ import annotations

import io
import json
import shutil
import tarfile
from pathlib import Path

import pytest

from flotilla import Flotilla
from flotilla.config import Storage
from flotilla.core import labels
from flotilla.manifest import Context, Manifest, Mount, Resources, Unit
from flotilla.native import (
    Artifact,
    NativeAttempt,
    ReleaseBinding,
    ReleaseBindings,
    ReleaseWriter,
    Task,
    Verification,
    read_dataset,
)
from flotilla.native.platform_verify import verify as run_platform_verify
from flotilla.platform.base import ExecResult, FlotillaError, SharedVolume
from flotilla.prepared import PreparedEnvironment
from flotilla.runtime.types import OpenOptions, RunRequest
from flotilla.testing.runtime import deployment as deployment

IMAGE = "example.invalid/unit@sha256:" + "c" * 64
VERIFY_COMMAND = ("verify", "run")
HOST = Storage(volumes="host", host_path="/srv/shared").settings()


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


def build_release(base: Path, *, verifier: bool = True) -> tuple[Path, Task]:
    files = base / "input"
    (files / "nested").mkdir(parents=True)
    (files / "data.txt").write_text("static input")
    roles: dict[str, Manifest] = {}
    assets: list[str] = []
    verification = None
    artifacts: tuple[Artifact, ...] = ()
    with ReleaseWriter(base / "release") as writer:
        assets.append(writer.add_asset(files))
        roles["main"] = role("ds/task-a", (Mount(scope="task", key=assets[0], target="/input", read_only=True),))
        if verifier:
            tests = base / "tests"
            tests.mkdir()
            (tests / "check.sh").write_text("#!/bin/sh\nexit 0\n")
            assets.append(writer.add_asset(tests))
            roles["verifier"] = role("ds/task-a#verifier")
            verification = Verification(
                role="verifier",
                command=VERIFY_COMMAND,
                tests=assets[1],
                env={"TASK_TOKEN": "native"},
                uid=1000,
                gid=1000,
                output_paths={"reward.txt": "/logs/verifier/reward.txt"},
                optional_output_paths={"details.txt": "/logs/verifier/details.txt"},
                require_output=True,
            )
            artifacts = (Artifact(name="agent", role="main", target="/logs/agent", verifier_target="/handoff/agent"),)
        task = Task(key="ds/task-a", roles=roles, assets=tuple(assets), verification=verification, artifacts=artifacts)
        record = writer.add_task(task, "Solve the task.\n")
        writer.publish([{"task_ref": record.task_ref, "task_digest": record.task_digest}])
    return base / "release", task


def volume_of(spec, mount_path: str):
    matches = [v for v in spec.volumes if v.mount_path == mount_path]
    return matches[0] if matches else None


class NativeHarness:
    """Fake-platform shims for the consume-only lifecycle, local to these tests.

    Anchor side: runs the real installed-helper verification in-process against a
    local "platform copy" of the release, emulates `cp -a -- src/. dest` (the one
    SharedTree does not model) and can inject a mid-create directory failure.
    Unit side: interprets the verification command (asserting the handoff content),
    serves output reads and optional-output probes from a side table, and builds
    real tars for download_dir from the shared tree.
    """

    def stage_release(self, key: str, root: Path) -> None:
        """Place a release copy on the fake shared storage: tree entries for anchor
        directory operations, the local directory for in-process verification."""
        self.platform_copies[key] = root
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

    def __init__(self, deployment, monkeypatch: pytest.MonkeyPatch) -> None:
        _, self.platform, self.tree = deployment
        self.platform_copies: dict[str, Path] = {}
        self.unit_files: dict[tuple[str, str], bytes] = {}
        self.verification_specs = []
        self.verify_calls = 0
        self.with_optional = False
        self.workspace_key: str | None = None
        self.fail_chown_of: str | None = None
        platform, _tree = self.platform, self.tree
        original_exec, original_read = platform.exec, platform.read_file

        async def execute(handle, proc, **kwargs):
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

        async def read(handle, path, **kwargs):
            data = self.unit_files.get((handle.iid, path))
            if data is not None:
                return data
            return await original_read(handle, path, **kwargs)

        monkeypatch.setattr(platform, "exec", execute)
        monkeypatch.setattr(platform, "read_file", read)

    # anchor-side emulation

    def _helper(self, argv: tuple[str, ...]) -> ExecResult:
        mount = self.tree.mount
        if "--clear-directory" in argv:
            key = argv[5][len(mount) :]
            for entries in (self.tree.dirs, self.tree.files):
                entries.difference_update(k for k in entries if k.startswith(key + "/"))
            for book in (self.tree.modes, self.tree.owners, self.platform.tree_contents):
                for key_part in [k for k in book if k.startswith(key + "/")]:
                    del book[key_part]
            return ExecResult(0, b"", b"")
        self.verify_calls += 1
        request = json.loads(self.platform.tree_contents[argv[4][len(mount) :]])
        try:
            result = run_platform_verify(
                {
                    "root": str(self.platform_copies[request["root"][len(mount) :]]),
                    "row": request["row"],
                    "validated_assets": request.get("validated_assets", ()),
                }
            )
        except ValueError as exc:
            return ExecResult(1, b"", f"native release verification failed: {exc}".encode())
        result_key = request["result"][len(mount) :]
        self.platform.tree_contents[result_key] = json.dumps(result).encode()
        self.tree.files.add(result_key)
        return ExecResult(0, b"", b"")

    def _copy_contents(self, argv: tuple[str, ...]) -> ExecResult:
        mount = self.tree.mount
        src, dst = argv[-2][len(mount) : -2], argv[-1][len(mount) :]
        for entries in (self.tree.dirs, self.tree.files):
            entries.update(dst + p[len(src) :] for p in list(entries) if p.startswith(src + "/"))
        for book in (self.tree.modes, self.tree.owners, self.platform.tree_contents):
            book.update({dst + k[len(src) :]: v for k, v in list(book.items()) if k.startswith(src + "/")})
        return ExecResult(0, b"", b"")

    # unit-side emulation

    def _unit_exec(self, spec, iid: str, proc) -> ExecResult:
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


def prepare(tmp_path: Path, deployment, monkeypatch, name: str = "tb1", *, verifier: bool = True):
    """Build a release, stage a separate platform copy, and install the harness."""
    cfg, platform, tree = deployment
    root, task = build_release(tmp_path / "build", verifier=verifier)
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
async def test_consume_only_lifecycle_handoff_and_cleanup(tmp_path, deployment, monkeypatch, with_optional) -> None:
    cfg, platform, tree, harness, bound, task = prepare(tmp_path, deployment, monkeypatch)
    harness.with_optional = with_optional

    async with Flotilla(deployment=cfg) as fleet:
        attempt = await NativeAttempt.create(fleet, bound)
        harness.workspace_key = attempt.workspace.key
        assert harness.verify_calls == 1
        # The per-check directory is removed; the launch-owned empty parent goes at teardown.
        assert not any("native-checks/" in key for key in tree.dirs | tree.files)

        await attempt.open("main")
        units = [i for i in platform.iids() if platform.spec_of(i).labels.get(labels.ROLE) == labels.ROLE_UNIT]
        assert len(units) == 1
        main_spec = platform.spec_of(units[0])
        source = volume_of(main_spec, "/input")
        assert source is not None and source.read_only
        assert source.key == f"releases/tb1/{task.assets[0]}"  # input binds the published release, not a rebuild
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

        # A second attempt in the same launch reuses launch-local validation; the
        # installed helper is not invoked again for the same release identity.
        second = await NativeAttempt.create(fleet, bound)
        assert harness.verify_calls == 1
        await second.close()
    assert platform.instances() == []  # fleet teardown deleted anchor and units


@pytest.mark.asyncio
async def test_attempt_isolation_guards(tmp_path, deployment, monkeypatch) -> None:
    cfg, _platform, _tree, harness, bound, _ = prepare(tmp_path, deployment, monkeypatch)
    plain_root, _ = build_release(tmp_path / "plain", verifier=False)
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
        assert harness.verify_calls == 2  # a different release identity verifies again
        with pytest.raises(ValueError, match="no verification interface"):
            await plain_attempt.verify()
        await plain_attempt.close()


@pytest.mark.asyncio
async def test_verification_runs_once_and_main_stays_closed_after_handoff(tmp_path, deployment, monkeypatch) -> None:
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
async def test_platform_copy_drift_fails_create_and_cleans_checks(tmp_path, deployment, monkeypatch) -> None:
    cfg, _platform, tree, harness, bound, task = prepare(tmp_path, deployment, monkeypatch)
    drifted = harness.platform_copies["releases/tb1"]
    (drifted / task.assets[0] / "data.txt").write_text("tampered after publication")

    async with Flotilla(deployment=cfg) as fleet:
        with pytest.raises(FlotillaError, match="native release verification failed"):
            await NativeAttempt.create(fleet, bound)
        assert not any("native-checks/" in key for key in tree.dirs | tree.files)
        assert not any("/attempts/" in key for key in tree.dirs | tree.files)


@pytest.mark.asyncio
async def test_create_failure_releases_attempt_directory(tmp_path, deployment, monkeypatch) -> None:
    cfg, _platform, tree, harness, bound, _ = prepare(tmp_path, deployment, monkeypatch)
    harness.fail_chown_of = "artifacts/agent"

    async with Flotilla(deployment=cfg) as fleet:
        with pytest.raises(FlotillaError):
            await NativeAttempt.create(fleet, bound)
        assert not any("/attempts/" in key for key in tree.dirs | tree.files)


@pytest.mark.asyncio
async def test_unvalidated_native_task_files_rejected_until_launch_validates(tmp_path, deployment, monkeypatch) -> None:
    cfg, _platform, _tree, _harness, bound, _ = prepare(tmp_path, deployment, monkeypatch)
    request = RunRequest(
        task=PreparedEnvironment(bound.task.roles["main"].model_copy(update={"task_files": bound.platform_key})),
        request_id="r1",
    )

    async def plan_of(manager):
        return manager.plan(request)

    async with Flotilla(deployment=cfg) as fleet:
        with pytest.raises(FlotillaError, match="validated native release"):
            await fleet._call(plan_of)
        attempt = await NativeAttempt.create(fleet, bound)
        plan = await fleet._call(plan_of)
        assert plan.task_files == bound.platform_key
        await attempt.close()
