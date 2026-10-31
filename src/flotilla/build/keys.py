"""File metadata (type, mode, numeric owner) for task-file publication."""

from __future__ import annotations

import json
import os
import stat
from collections.abc import Iterator
from pathlib import Path


def _walk(root: Path) -> Iterator[tuple[str, os.stat_result]]:
    """Walk `root` in path order, yielding (relative path, lstat). Does not follow symlinks."""
    stack = [root]
    while stack:
        current = stack.pop()
        with os.scandir(current) as scanned:
            entries = sorted(scanned, key=lambda e: e.name)
        for entry in entries:
            rel = str(Path(entry.path).relative_to(root))
            info = entry.stat(follow_symlinks=False)
            yield rel, info
            if stat.S_ISDIR(info.st_mode):
                stack.append(Path(entry.path))


def _kind(info: os.stat_result) -> str:
    if stat.S_ISDIR(info.st_mode):
        return "dir"
    if stat.S_ISLNK(info.st_mode):
        return "link"
    return "file" if stat.S_ISREG(info.st_mode) else "other"


def entries(root: Path) -> list[dict[str, object]]:
    """Path, type, mode and numeric owner of every entry under `root`, plus file size or link target."""
    metadata = root.parent / "file-identities.json"
    identities = json.loads(metadata.read_text()) if metadata.exists() else {}
    result: list[dict[str, object]] = []
    for relative, info in _walk(root):
        owner = identities.get(relative, (info.st_uid, info.st_gid, stat.S_IMODE(info.st_mode)))
        if not isinstance(owner, (list, tuple)) or len(owner) != 3 or any(type(v) is not int or v < 0 for v in owner):
            raise ValueError("invalid file identity metadata")
        entry: dict[str, object] = {
            "path": relative,
            "type": _kind(info),
            "mode": f"{owner[2]:04o}",
            "uid": owner[0],
            "gid": owner[1],
        }
        if stat.S_ISREG(info.st_mode):
            entry["size"] = info.st_size
        elif stat.S_ISLNK(info.st_mode):
            entry["target"] = os.readlink(root / relative)
        result.append(entry)
    return sorted(result, key=lambda entry: str(entry["path"]))
