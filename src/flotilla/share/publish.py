"""Publish task files atomically into per-environment flat directories."""

from __future__ import annotations

import contextlib
import posixpath
import re
import time
import uuid
from dataclasses import dataclass
from pathlib import Path

from flotilla._io import owned_io
from flotilla.build.keys import entries as entries
from flotilla.core.anchor import Anchor
from flotilla.manifest import Manifest, Published

TASKS = "tasks"
STAGING = f"{TASKS}/.staging"
TRASH = f"{TASKS}/.trash"
#: Per-file upload limit: files go one by one over exec, so an oversized file is a build problem, not uploaded.
MAX_FILE_BYTES = 512 * 1024 * 1024
_UNSAFE = re.compile(r"[^A-Za-z0-9_.-]+")


class PublishError(ValueError):
    """Publication failed (unsupported file type, file too large, invalid target)."""


@dataclass(frozen=True)
class TaskPublication:
    task_files: str  # the target, relative to the shared root
    uploaded: int


async def publish_task_dir(anchor: Anchor, target: str, files_dir: Path) -> TaskPublication:
    """Publish `files_dir` to `target` (`tasks/<dataset>/<task>/<role>`), replacing any previous content.

    Files are uploaded to a staging directory and renamed into place, so readers never see a partial tree. A
    previous `target` is renamed aside first and removed after the swap, or restored if the swap fails.
    """
    parts = target.split("/")
    if len(parts) < 2 or parts[0] != TASKS or any(not part or part.startswith(".") for part in parts[1:]):
        raise PublishError(f"task file target must be {TASKS}/<name>/... without hidden segments: {target!r}")
    snapshot = await owned_io(lambda: entries(files_dir))
    for entry in snapshot:
        if entry["type"] not in ("dir", "file"):
            # The anchor image has no ln: symlinks and special files cannot be reproduced on shared storage.
            raise PublishError(f"task file {entry['path']} is a {entry['type']}, which publication does not support")
        if int(str(entry.get("size", 0))) > MAX_FILE_BYTES:
            raise PublishError(f"task file {entry['path']} exceeds the transfer limit {MAX_FILE_BYTES}")
    name = f"{_UNSAFE.sub('-', '/'.join(parts[1:]))}.{int(time.time())}.{uuid.uuid4().hex[:12]}"
    staging = f"{STAGING}/{name}"
    try:
        await anchor.mkdir(staging, stage="prepare")
        uploaded = 0
        for entry in snapshot:
            rel = str(entry["path"])
            key = posixpath.join(staging, rel)
            mode, uid, gid = int(str(entry["mode"]), 8), int(str(entry["uid"])), int(str(entry["gid"]))
            if entry["type"] == "dir":
                await anchor.mkdir(key, stage="prepare")
                await anchor.chown(key, uid, gid, stage="prepare")
                await anchor.chmod(key, mode, stage="prepare")
                continue
            data = await owned_io((files_dir / rel).read_bytes)
            await anchor.upload(key, data, mode=mode, uid=uid, gid=gid, stage="prepare")
            uploaded += 1
        await anchor.mkdir(posixpath.dirname(target), stage="prepare")
        if await anchor.exists(target, stage="prepare"):
            trash = f"{TRASH}/{name}"
            await anchor.mkdir(TRASH, stage="prepare")
            await anchor.rename(target, trash, stage="prepare")
            try:
                await anchor.rename(staging, target, stage="prepare")
            except BaseException:
                with contextlib.suppress(Exception):
                    await anchor.rename(trash, target, stage="prepare")
                raise
            await _discard(anchor, trash)
        else:
            await anchor.rename(staging, target, stage="prepare")
    except BaseException:
        await _discard(anchor, staging)
        raise
    return TaskPublication(target, uploaded)


async def _discard(anchor: Anchor, staging: str) -> bool:
    """Remove staging; a failure does not mask the original error (leftover .staging is left to gc)."""
    with contextlib.suppress(Exception):
        await anchor.remove(staging, stage="run")
        return True
    return False


def mark_published(manifest: Manifest, task_files: str, storage_root: str) -> Manifest:
    """Point the manifest at its published task files."""
    return manifest.model_copy(
        update={"task_files": task_files, "task_files_published": Published(storage_root=storage_root)}
    )
