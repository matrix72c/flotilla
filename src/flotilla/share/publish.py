"""Publish canonical task-file snapshots atomically and verify source consistency."""

from __future__ import annotations

import contextlib
import hashlib
import json
import os
import posixpath
import stat
import uuid
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from flotilla._io import owned_io
from flotilla.build.keys import entries as entries
from flotilla.build.keys import files_json as files_json
from flotilla.core.anchor import Anchor
from flotilla.manifest import Manifest, Published

TASKS = "tasks"
STAGING = f"{TASKS}/.staging"
FILES_JSON = "FILES.json"
#: Per-file upload limit: files go one by one over exec, so an oversized file is a build problem, not uploaded.
MAX_FILE_BYTES = 512 * 1024 * 1024


class PublishError(ValueError):
    """Publication failed (file too large, checksum mismatch, target exists with different content, etc.)."""


@dataclass(frozen=True)
class PublishResult:
    build_key: str
    task_files: str  # `tasks/<build_key>`
    files_sha256: str
    uploaded: int  # files uploaded this time; 0 when skipped
    skipped: bool


async def publish_task_files(
    anchor: Anchor,
    build_key: str,
    files_dir: Path,
    *,
    owned_staging: set[str] | None = None,
) -> PublishResult:
    """Publish a task's `files/` to `tasks/<build_key>`.

    Skips if already published with a matching `FILES.json`; otherwise raises without overwriting, since one
    content key must always map to the same content.
    """
    blob, digest = await owned_io(files_json, files_dir)
    snapshot = json.loads(blob)
    if any(entry["type"] not in ("dir", "file") for entry in snapshot):
        raise PublishError("task files contain an unsupported symbolic link or special file")
    if any(entry.get("size", 0) > MAX_FILE_BYTES for entry in snapshot):
        raise PublishError("task file exceeds transfer limit")
    target = f"{TASKS}/{build_key}"
    if await anchor.exists(f"{target}/{FILES_JSON}", stage="prepare"):
        existing = await anchor.download(f"{target}/{FILES_JSON}", stage="prepare")
        if hashlib.sha256(existing).hexdigest() == digest:
            return PublishResult(build_key, target, digest, 0, True)
        raise PublishError(
            f"{target} is already published but its {FILES_JSON} mismatches these files; key collision or corruption"
        )
    staging = f"{STAGING}/{build_key}.{uuid.uuid4().hex[:12]}"
    if owned_staging is not None:
        owned_staging.add(staging)
    try:
        await anchor.mkdir(staging, stage="prepare")
        uploaded = await _upload_tree(anchor, files_dir, staging, json.loads(blob))
        if (await owned_io(files_json, files_dir))[0] != blob:
            raise PublishError("task files changed during publication")
        await anchor.upload(f"{staging}/{FILES_JSON}", blob, mode=0o644, uid=0, gid=0, stage="prepare")
        await anchor.mkdir(TASKS, stage="prepare")
        try:
            await anchor.rename(staging, target, stage="prepare")
        except Exception:
            # Another independent open may publish the same immutable files.
            if not await anchor.exists(f"{target}/{FILES_JSON}", stage="prepare"):
                raise
            existing = await anchor.download(f"{target}/{FILES_JSON}", stage="prepare")
            if hashlib.sha256(existing).hexdigest() != digest:
                raise
            if await _discard(anchor, staging) and owned_staging is not None:
                owned_staging.discard(staging)
            return PublishResult(build_key, target, digest, 0, True)
        if owned_staging is not None:
            owned_staging.discard(staging)
    except BaseException:
        if await _discard(anchor, staging) and owned_staging is not None:
            owned_staging.discard(staging)
        raise
    return PublishResult(build_key, target, digest, uploaded, False)


async def _upload_tree(anchor: Anchor, files_dir: Path, staging: str, snapshot: list[dict[str, Any]]) -> int:
    """Upload the tree: directories first (with mode and owner), then files, then symlinks. Verifies each sha256."""
    expected = {str(entry["path"]): entry for entry in snapshot}
    uploaded = 0
    for rel, info in await owned_io(lambda: list(_walk(files_dir))):
        key = posixpath.join(staging, rel)
        original = expected.get(rel)
        if original is None:
            raise PublishError("task files changed during publication")
        mode = int(str(original["mode"]), 8)
        uid, gid = int(original["uid"]), int(original["gid"])
        if stat.S_ISDIR(info.st_mode):
            await anchor.mkdir(key, stage="prepare")
            await anchor.chown(key, uid, gid, stage="prepare")
            await anchor.chmod(key, mode, stage="prepare")
            continue
        if stat.S_ISLNK(info.st_mode):
            # The anchor image has only the needed busybox applets, no ln; reject symlinks explicitly rather
            # than skip them (skipping would make published files diverge from the build output).
            raise PublishError(
                f"task file {rel} is a symlink, which publication does not support (anchor image has no ln)"
            )
        if not stat.S_ISREG(info.st_mode):
            raise PublishError(f"task files contain an unsupported entry type: {rel}")
        if info.st_size > MAX_FILE_BYTES:
            raise PublishError(f"task file {rel} is {info.st_size} bytes, exceeding the limit {MAX_FILE_BYTES}")
        data = await owned_io((files_dir / rel).read_bytes)
        if rel not in expected or hashlib.sha256(data).hexdigest() != expected[rel].get("sha256"):
            raise PublishError(f"task file {rel} changed during publication")
        await anchor.upload(key, data, mode=mode, uid=uid, gid=gid, stage="prepare")
        back = await anchor.download(key, stage="prepare")
        if hashlib.sha256(back).hexdigest() != hashlib.sha256(data).hexdigest():
            raise PublishError(f"task file {rel} checksum mismatch after upload")
        uploaded += 1
    return uploaded


async def _discard(anchor: Anchor, staging: str) -> bool:
    """Remove staging; a failure does not mask the original error (leftover .staging is left to gc)."""
    with contextlib.suppress(Exception):
        await anchor.remove(staging, stage="run")
        return True
    return False


def mark_published(manifest: Manifest, result: PublishResult, storage_root: str) -> Manifest:
    """Mark the manifest as published."""
    return manifest.model_copy(
        update={
            "task_files": result.task_files,
            "task_files_published": Published(storage_root=storage_root, files_sha256=result.files_sha256),
        }
    )


def _walk(root: Path) -> Iterator[tuple[str, os.stat_result]]:
    """Walk in path order, yielding (relative path, lstat). Directories precede their contents.

    Symlinks are not followed: `os.walk` lists links to directories in `dirs` without entering them, and
    lstat classifies them, so they are yielded as links (`_kind` gives "link") and their targets are not walked.
    """
    for current, dirs, names in os.walk(root):
        dirs.sort()
        base = Path(current)
        for name in sorted([*dirs, *names]):
            path = base / name
            yield str(path.relative_to(root)), path.lstat()
