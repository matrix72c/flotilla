"""Task file publication during prepare: metadata, staging, replacement and failure cleanup."""

from __future__ import annotations

import os
from collections.abc import AsyncIterator
from pathlib import Path

import pytest
import pytest_asyncio

from flotilla.core.anchor import Anchor, AnchorSettings
from flotilla.manifest import SCHEMA_VERSION, Manifest
from flotilla.platform.base import ExecResult, FlotillaError, ProcessSpec
from flotilla.share.publish import PublishError, entries, mark_published, publish_task_dir
from flotilla.testing.platform import FakePlatform, ManualClock, SharedTree

TARGET = "tasks/ds/t/main"
ROOT = "pvc:data/opt/flotilla"


@pytest_asyncio.fixture
async def anchor(platform: FakePlatform, clock: ManualClock, tree: SharedTree) -> AsyncIterator[Anchor]:
    a = Anchor(platform, clock, "L1", AnchorSettings(image="anchor@sha256:abc"))
    await clock.run(a.start())
    yield a
    await a.close()


def _files(root: Path) -> Path:
    files = root / "files"
    (files / "binds" / "0").mkdir(parents=True)
    (files / "binds" / "0" / "entrypoint.sh").write_text("#!/bin/sh\necho hi\n")
    (files / "binds" / "0" / "entrypoint.sh").chmod(0o755)
    (files / "seeds").mkdir()
    (files / "seeds" / "data.tar").write_bytes(b"tar-bytes")
    return files


# ───────────────────────────── File metadata ─────────────────────────────


def test_entries_cover_every_kind(tmp_path: Path) -> None:
    files = _files(tmp_path)
    os.symlink("entrypoint.sh", files / "binds" / "0" / "link")
    by_path = {str(e["path"]): e for e in entries(files)}
    assert by_path["binds"]["type"] == "dir"
    script = by_path["binds/0/entrypoint.sh"]
    assert script["type"] == "file" and script["mode"] == "0755" and script["size"] == 18
    assert by_path["binds/0/link"] == {
        **by_path["binds/0/link"],
        "type": "link",
        "target": "entrypoint.sh",
    }
    # Sorted by path for easy reading and comparison.
    assert [str(e["path"]) for e in entries(files)] == sorted(str(e["path"]) for e in entries(files))


# ───────────────────────────── Publishing ─────────────────────────────


# ───────────────────────────── Manifest ─────────────────────────────


def _manifest() -> Manifest:
    return Manifest.model_validate(
        {
            "schema": SCHEMA_VERSION,
            "task": "ds/t",
            "agent_service": "main",
            "units": {
                "main": {
                    "image": "r.io/a@sha256:" + "b" * 64,
                    "command": ["sh", "-c", "sleep infinity"],
                    "context": {"env": {}, "uid": 0, "gid": 0, "cwd": "/"},
                    "resources": {"cpu": "1", "memory": "1Gi"},
                }
            },
        }
    )


@pytest.mark.asyncio
async def test_mark_published_fills_manifest(
    anchor: Anchor, clock: ManualClock, tree: SharedTree, tmp_path: Path
) -> None:
    files = _files(tmp_path)
    result = await publish_task_dir(anchor, TARGET, files)
    published = mark_published(_manifest(), result.task_files, ROOT)
    assert published.task_files == TARGET
    assert published.task_files_published is not None
    assert published.task_files_published.storage_root == ROOT
    # The manifest still reads back unchanged (schema unchanged).
    assert Manifest.load_json(published.dump_json()).task_files == TARGET


@pytest.mark.asyncio
async def test_exists_does_not_read_missing_binary_as_absent(
    anchor: Anchor, clock: ManualClock, tree: SharedTree, platform: FakePlatform
) -> None:
    """A missing applet in the anchor image exits 127 ("not found").

    Hit on a real deployment: `exists` used `test -e`, the anchor image had no `test`, and 127 was read as
    "file absent", so published tasks were re-uploaded. A missing command must raise as a fault, not be taken as an
    answer.
    """

    def not_found(iid: str, proc: ProcessSpec) -> ExecResult:
        if proc.argv[0] == "ls":
            return ExecResult(exit_code=127, stdout=b"", stderr=b"sh: ls: not found")
        return ExecResult(exit_code=0, stdout=b"", stderr=b"")

    platform.set_exec_handler(not_found)
    with pytest.raises(FlotillaError):
        await clock.run(anchor.exists("tasks/x", stage="prepare"))


# ───────────────────────────── Flat per-environment publishing ─────────────────────────────


@pytest.mark.asyncio
async def test_publish_task_dir_uploads_with_owner_and_mode(
    anchor: Anchor, clock: ManualClock, tree: SharedTree, tmp_path: Path
) -> None:
    files = _files(tmp_path)
    result = await publish_task_dir(anchor, TARGET, files)
    assert result.task_files == TARGET and result.uploaded == 2
    assert tree.content(f"{TARGET}/seeds/data.tar") == b"tar-bytes"
    assert tree.modes[f"{TARGET}/binds/0/entrypoint.sh"] == "0755"
    assert tree.owners[f"{TARGET}/binds/0/entrypoint.sh"] == f"{os.getuid()}:{os.getgid()}"
    assert f"{TARGET}/FILES.json" not in tree.files  # no checksum manifest
    assert not any(p.startswith("tasks/.staging/") for p in {*tree.dirs, *tree.files})
    assert any(e.startswith("mv tasks/.staging/ds-t-main.") and e.endswith(TARGET) for e in tree.events)


@pytest.mark.asyncio
async def test_publish_task_dir_replaces_previous_content(
    anchor: Anchor, clock: ManualClock, tree: SharedTree, tmp_path: Path
) -> None:
    files = _files(tmp_path)
    await publish_task_dir(anchor, TARGET, files)
    (files / "seeds" / "data.tar").write_bytes(b"new")
    (files / "binds" / "0" / "entrypoint.sh").unlink()
    await publish_task_dir(anchor, TARGET, files)
    assert tree.content(f"{TARGET}/seeds/data.tar") == b"new"
    assert f"{TARGET}/binds/0/entrypoint.sh" not in tree.files  # no stale files survive a replacement
    assert not any(p.startswith(("tasks/.staging/", "tasks/.trash/")) for p in {*tree.dirs, *tree.files})


@pytest.mark.asyncio
async def test_publish_task_dir_restores_previous_content_when_swap_fails(
    anchor: Anchor, clock: ManualClock, tree: SharedTree, platform: FakePlatform, tmp_path: Path
) -> None:
    files = _files(tmp_path)
    await publish_task_dir(anchor, TARGET, files)
    (files / "seeds" / "data.tar").write_bytes(b"new")

    def failing_swap(iid: str, proc: ProcessSpec) -> ExecResult:
        if proc.argv[0] == "mv" and "/tasks/.staging/" in proc.argv[-2]:
            return ExecResult(exit_code=1, stdout=b"", stderr=b"Input/output error")
        return tree._exec(iid, proc)

    platform.set_exec_handler(failing_swap)
    with pytest.raises(FlotillaError):
        await publish_task_dir(anchor, TARGET, files)
    assert tree.content(f"{TARGET}/seeds/data.tar") == b"tar-bytes"
    assert not any(p.startswith(("tasks/.staging/", "tasks/.trash/")) for p in {*tree.dirs, *tree.files})


@pytest.mark.asyncio
async def test_publish_task_dir_rejects_symlinks_and_hidden_targets(
    anchor: Anchor, clock: ManualClock, tree: SharedTree, tmp_path: Path
) -> None:
    files = _files(tmp_path)
    with pytest.raises(PublishError, match="hidden segments"):
        await publish_task_dir(anchor, "tasks/.staging/x", files)
    with pytest.raises(PublishError, match="hidden segments"):
        await publish_task_dir(anchor, "share/x", files)
    os.symlink("entrypoint.sh", files / "binds" / "0" / "link")
    with pytest.raises(PublishError, match="link"):
        await publish_task_dir(anchor, TARGET, files)
    assert TARGET not in tree.dirs
