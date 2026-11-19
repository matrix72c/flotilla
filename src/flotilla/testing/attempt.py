"""A fake-platform harness for `NativeAttempt` contract tests: staging, handoff, grading.

`FakePlatform` models a shared tree but not the installed helper's directory operations, and a
unit answers only what the runtime's own startup bookkeeping asks. The attempt lifecycle needs
more than that: the anchor's clear-and-copy staging commands, a grading command that reads what
the handoff staged, and output reads served from a side table. This module installs those, so a
downstream test can drive a real attempt -- open, both phases of grading, artifact download --
with no platform, no registry, and no network.
"""

from __future__ import annotations

import io
import shutil
import tarfile
from pathlib import Path
from typing import Any

import pytest

from flotilla.core import labels
from flotilla.models.plan import TaskSource
from flotilla.native.dataset import Dataset
from flotilla.platform.base import ExecResult, InstanceHandle, InstanceSpec, ProcessSpec
from flotilla.testing.planning import APP, DATASET_ROOT
from flotilla.testing.platform import FakePlatform, SharedTree

#: The grading command `flotilla.compose.verification` derives for a task that ships `tests/`.
VERIFY_COMMAND = ("bash", "/tests/test.sh")

#: Where the task's own files live inside the dataset, as the trial mounts them.
INPUT_KEY = "environment/input"


def write_task(base: Path, *, image: str = APP, verifier: bool = True, shared_verifier: bool = False) -> Path:
    """Write one published task directory: a read-only bind input, an instruction, hidden tests.

    `verifier` gives the task a separate verifier environment, so grading runs in a second role;
    `shared_verifier` ships the tests without one, so grading runs in main. Neither flag writes
    tests at all, which leaves a task with no grading contract. Returns the dataset root, with
    the task at `ds/task-a` below it.
    """
    task = base / "ds" / "task-a"
    files = task / "environment" / "input"
    (files / "nested").mkdir(parents=True)
    (files / "data.txt").write_text("static input")
    (task / "instruction.md").write_text("Solve the task.\n")
    config = f'[environment]\ndocker_image = "{image}"\ncpus = 1\nmemory_mb = 128\n'
    if verifier or shared_verifier:
        (task / "tests").mkdir()
        (task / "tests/test.sh").write_text("#!/bin/sh\nexit 0\n")
    if verifier:
        config += '[verifier]\nenvironment_mode = "separate"\nenv = {TASK_TOKEN = "native"}\n'
        # Its own Compose project, so the agent's task input is not in the verifier's sandbox.
        (task / "tests/docker-compose.yaml").write_text(f"services:\n  main:\n    image: {image}\n")
    (task / "task.toml").write_text(config)
    (task / "environment/docker-compose.yaml").write_text(
        "services:\n  main:\n    volumes:\n      - ./input:/input:ro\n"
    )
    return base


def prepared_source(
    tmp: Path,
    platform: FakePlatform,
    tree: SharedTree,
    *,
    name: str,
    image: str = APP,
    verifier: bool = True,
    shared_verifier: bool = False,
) -> TaskSource:
    """Write one task under `tmp`, publish it on the fake shared tree, and name its source.

    The published copy is separate from the client-side directory, because the two sides of a
    dataset are separate in production too: `resolve` reads the task through the client path
    while the anchor stages from what the platform sees.
    """
    root = write_task(tmp / name, image=image, verifier=verifier, shared_verifier=shared_verifier)
    platform_copy = tmp / f"platform-copy-{name}"
    shutil.copytree(root, platform_copy, symlinks=True)
    stage_dataset(platform, tree, tree.input_key(DATASET_ROOT, name), platform_copy)
    return Dataset(root, name, root=DATASET_ROOT).source("ds/task-a")


def stage_dataset(platform: FakePlatform, tree: SharedTree, key: str, root: Path) -> None:
    """Place a dataset copy on the fake shared storage as tree entries for anchor operations."""
    for path in sorted(root.rglob("*")):
        entry = f"{key}/{path.relative_to(root).as_posix()}"
        info = path.lstat()
        if path.is_dir():
            tree.dirs.add(entry)
        else:
            tree.files.add(entry)
            platform.tree_contents[entry] = path.read_bytes()
            tree.modes[entry] = oct(info.st_mode & 0o777)
        tree.owners[entry] = f"{info.st_uid}:{info.st_gid}"


class AttemptHarness:
    """Fake-platform shims for the attempt lifecycle.

    Anchor side: emulates the installed helper's `--clear-directory`, `cp -a -- src/. dest`
    (the one operation `SharedTree` does not model), and can inject a mid-create directory
    failure. Unit side: interprets the grading command (asserting the handoff content), serves
    output reads and optional-output probes from a side table, and builds real tars for
    `download_dir` from the shared tree.

    Set `workspace_key` to the attempt's workspace key before grading runs; `verification_specs`
    collects each grading `ProcessSpec`; `with_optional` makes the grader write both reward
    forms; `fail_chown_of` fails the anchor's chown for one directory suffix.
    """

    def __init__(self, platform: FakePlatform, tree: SharedTree, monkeypatch: pytest.MonkeyPatch) -> None:
        self.platform = platform
        self.tree = tree
        self.unit_files: dict[tuple[str, str], bytes] = {}
        self.verification_specs: list[ProcessSpec] = []
        self.with_optional = False
        self.workspace_key: str | None = None
        self.fail_chown_of: str | None = None
        original_exec, original_read = platform.exec, platform.read_file

        async def execute(handle: InstanceHandle, proc: ProcessSpec, **kwargs: Any) -> ExecResult:
            spec = platform.spec_of(handle.iid)
            argv = proc.argv
            if spec.labels.get(labels.ROLE) == labels.ROLE_ANCHOR:
                if argv[:2] == ("/.flotilla/bin/busybox", "find") and argv[3:] == ("-mindepth", "1", "-delete"):
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

    def stage_dataset(self, key: str, root: Path) -> None:
        """Place a dataset copy on this harness's shared tree."""
        stage_dataset(self.platform, self.tree, key, root)

    # anchor-side emulation

    def _helper(self, argv: tuple[str, ...]) -> ExecResult:
        rel = self.tree._rel
        key = rel(argv[2], write=True)
        for entries in (self.tree.dirs, self.tree.files):
            entries.difference_update(k for k in entries if k.startswith(key + "/"))
        for book in (self.tree.modes, self.tree.owners, self.platform.tree_contents):
            for key_part in [k for k in book if k.startswith(key + "/")]:
                del book[key_part]
        return ExecResult(0, b"", b"")

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
            if answer is None:
                # A shared verifier grades in main, where the artifact already lives.
                answer = self.platform.tree_contents.get(f"{self.workspace_key}/artifacts/agent/answer.txt")
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


__all__ = ["VERIFY_COMMAND", "AttemptHarness", "prepared_source", "stage_dataset", "write_task"]
