"""Task publishing `flotilla publish`: FILES.json, upload, atomic rename, idempotency and conflicts."""

from __future__ import annotations

import hashlib
import json
import os
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

import pytest
import pytest_asyncio

from flotilla.core.anchor import Anchor, AnchorSettings
from flotilla.manifest import Manifest
from flotilla.platform.base import ErrorCategory, ExecResult, FlotillaError, InstanceHandle, ProcessSpec
from flotilla.share.publish import (
    FILES_JSON,
    PublishError,
    entries,
    files_json,
    mark_published,
    publish_task_files,
)
from tests.support.platform import FakePlatform, ManualClock, SharedTree

KEY = "a" * 64
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


# ───────────────────────────── FILES.json ─────────────────────────────


def test_entries_cover_every_kind(tmp_path: Path) -> None:
    files = _files(tmp_path)
    os.symlink("entrypoint.sh", files / "binds" / "0" / "link")
    by_path = {str(e["path"]): e for e in entries(files)}
    assert by_path["binds"]["type"] == "dir"
    script = by_path["binds/0/entrypoint.sh"]
    assert script["type"] == "file" and script["mode"] == "0755" and script["size"] == 18
    assert script["sha256"] == hashlib.sha256((files / "binds/0/entrypoint.sh").read_bytes()).hexdigest()
    assert by_path["binds/0/link"] == {
        **by_path["binds/0/link"],
        "type": "link",
        "target": "entrypoint.sh",
    }
    # Sorted by path for easy reading and comparison.
    assert [str(e["path"]) for e in entries(files)] == sorted(str(e["path"]) for e in entries(files))


def test_files_json_is_stable_and_content_sensitive(tmp_path: Path) -> None:
    a, b = tmp_path / "a", tmp_path / "b"
    _files(a)
    _files(b)
    assert files_json(a / "files")[1] == files_json(b / "files")[1]
    (a / "files" / "seeds" / "data.tar").write_bytes(b"different")
    assert files_json(a / "files")[1] != files_json(b / "files")[1]


# ───────────────────────────── Publishing ─────────────────────────────


@pytest.mark.asyncio
async def test_publish_uploads_and_renames_atomically(
    anchor: Anchor, clock: ManualClock, tree: SharedTree, tmp_path: Path
) -> None:
    files = _files(tmp_path)
    result = await publish_task_files(anchor, KEY, files)
    assert result.task_files == f"tasks/{KEY}" and result.uploaded == 2 and not result.skipped
    # The final directory has the content and FILES.json; staging is not left behind.
    assert f"tasks/{KEY}/binds/0/entrypoint.sh" in tree.files
    assert tree.content(f"tasks/{KEY}/seeds/data.tar") == b"tar-bytes"
    assert json.loads(tree.content(f"tasks/{KEY}/{FILES_JSON}"))
    # The staging copy is gone (the empty parent `tasks/.staging` may remain).
    assert not any(p.startswith("tasks/.staging/") for p in {*tree.dirs, *tree.files})
    # Upload to staging first, then a single mv: a mid-way failure never leaves a half-built final directory.
    assert any(e.startswith("mv tasks/.staging/") and e.endswith(f"tasks/{KEY}") for e in tree.events)


@pytest.mark.asyncio
async def test_publish_preserves_mode_and_owner(
    anchor: Anchor, clock: ManualClock, tree: SharedTree, tmp_path: Path
) -> None:
    files = _files(tmp_path)
    await publish_task_files(anchor, KEY, files)
    assert tree.modes[f"tasks/{KEY}/binds/0/entrypoint.sh"] == "0755"
    assert tree.owners[f"tasks/{KEY}/binds/0/entrypoint.sh"] == f"{os.getuid()}:{os.getgid()}"


@pytest.mark.asyncio
async def test_publish_is_idempotent(anchor: Anchor, clock: ManualClock, tree: SharedTree, tmp_path: Path) -> None:
    files = _files(tmp_path)
    first = await publish_task_files(anchor, KEY, files)
    second = await publish_task_files(anchor, KEY, files)
    assert not first.skipped and second.skipped and second.uploaded == 0
    assert second.files_sha256 == first.files_sha256


@pytest.mark.asyncio
async def test_publish_refuses_to_overwrite_different_content(
    anchor: Anchor, clock: ManualClock, tree: SharedTree, tmp_path: Path
) -> None:
    files = _files(tmp_path)
    await publish_task_files(anchor, KEY, files)
    (files / "seeds" / "data.tar").write_bytes(b"tampered")
    with pytest.raises(PublishError, match=r"FILES\.json mismatches these files"):
        await publish_task_files(anchor, KEY, files)
    assert tree.content(f"tasks/{KEY}/seeds/data.tar") == b"tar-bytes"  # original content not overwritten


@pytest.mark.asyncio
async def test_publish_cleans_staging_on_failure(
    anchor: Anchor, clock: ManualClock, tree: SharedTree, platform: FakePlatform, tmp_path: Path
) -> None:
    files = _files(tmp_path)
    platform.inject_fault(
        "write_file", FlotillaError("boom", stage="prepare", category=ErrorCategory.TRANSIENT, retryable=True)
    )
    with pytest.raises(FlotillaError):
        await publish_task_files(anchor, KEY, files)
    assert not any(p.startswith("tasks/.staging/") for p in tree.dirs)  # no staging copy left after failure
    assert f"tasks/{KEY}" not in tree.dirs  # and no half-built final directory


@pytest.mark.asyncio
async def test_publish_detects_corrupted_upload(
    anchor: Anchor, clock: ManualClock, tree: SharedTree, platform: FakePlatform, tmp_path: Path
) -> None:
    files = _files(tmp_path)
    real_read = platform.read_file

    async def corrupt(handle: InstanceHandle, path: str) -> bytes:
        data = await real_read(handle, path)
        return data + b"extra" if path.endswith("data.tar") else data

    platform.read_file = corrupt  # type: ignore[method-assign]
    with pytest.raises(PublishError, match="checksum mismatch after upload"):
        await publish_task_files(anchor, KEY, files)


# ───────────────────────────── Manifest ─────────────────────────────


def _manifest() -> Manifest:
    return Manifest.model_validate(
        {
            "schema": 4,
            "task": "ds/t",
            "build_key": KEY,
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
    result = await publish_task_files(anchor, KEY, files)
    published = mark_published(_manifest(), result, ROOT)
    assert published.task_files == f"tasks/{KEY}"
    assert published.task_files_published is not None
    assert published.task_files_published.storage_root == ROOT
    assert published.task_files_published.files_sha256 == files_json(files)[1]
    # The manifest still reads back unchanged (schema unchanged).
    assert Manifest.load_json(published.dump_json()).task_files == f"tasks/{KEY}"


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


@pytest.mark.asyncio
async def test_publish_rejects_symlinks_in_task_files(
    anchor: Anchor, clock: ManualClock, tree: SharedTree, tmp_path: Path
) -> None:
    files = _files(tmp_path)
    os.symlink("data.tar", files / "seeds" / "alias.tar")
    with pytest.raises(PublishError, match="symbolic link"):
        await publish_task_files(anchor, KEY, files)


@pytest.mark.asyncio
async def test_mutation_after_snapshot_never_publishes_stale_files_json(
    anchor: Anchor,
    clock: ManualClock,
    tree: SharedTree,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:

    files = _files(tmp_path)
    original = anchor.exists

    async def mutate(*args: Any, **kwargs: Any) -> bool:
        (files / "seeds/data.tar").write_bytes(b"changed-after-snapshot")
        return await original(*args, **kwargs)

    monkeypatch.setattr(anchor, "exists", mutate)
    with pytest.raises(PublishError, match="changed"):
        await publish_task_files(anchor, KEY, files)
    assert not any(p.startswith(f"tasks/{KEY}/") for p in tree.files)
    assert not any(p.startswith("tasks/.staging/") for p in tree.files)


@pytest.mark.asyncio
async def test_competing_publisher_accepts_identical_completed_content(
    anchor: Anchor, clock: ManualClock, tree: SharedTree, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    files = _files(tmp_path)
    original = anchor.rename
    owned: set[str] = set()

    async def competing(source: str, target: str, **kwargs: Any) -> None:
        # Model another publisher completing before our rename responds.
        await original(source, target, **kwargs)
        raise OSError("rename lost race or response")

    monkeypatch.setattr(anchor, "rename", competing)
    result = await publish_task_files(anchor, KEY, files, owned_staging=owned)
    assert result.skipped and not owned
    assert tree.content(f"tasks/{KEY}/seeds/data.tar") == b"tar-bytes"
    assert not any(p.startswith("tasks/.staging/") for p in {*tree.dirs, *tree.files})
