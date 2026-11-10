"""File inventory and confined reads for native releases."""

from __future__ import annotations

import hashlib
import os
import posixpath
import stat
import tarfile
from collections.abc import Mapping
from pathlib import Path

from .models import FileEntry, relative_path

METADATA_LIMIT = 16 * 1024 * 1024
INSTRUCTION_LIMIT = 4 * 1024 * 1024


def confined(root: Path, key: str, *, allow_link: bool = False) -> Path:
    relative_path(key)
    current = root
    parts = key.split("/")
    for index, part in enumerate(parts):
        current /= part
        if current.is_symlink() and not (allow_link and index == len(parts) - 1):
            raise ValueError(f"symlink in release path: {key}")
    return current


def read_bytes(root: Path, key: str, limit: int = METADATA_LIMIT) -> bytes:
    path = confined(root, key)
    if not path.is_file() or path.stat().st_size > limit:
        raise ValueError(f"invalid or oversized release file: {key}")
    with path.open("rb") as handle:
        data = handle.read(limit + 1)
    if len(data) > limit:
        raise ValueError(f"oversized release file: {key}")
    return data


def file_hash(path: Path) -> str:
    with path.open("rb") as handle:
        return hashlib.file_digest(handle, "sha256").hexdigest()


def inventory(root: Path, identities: Mapping[str, tuple[int, int, int]] | None = None) -> dict[str, FileEntry]:
    result: dict[str, FileEntry] = {}
    identities = identities or {}
    stack = [(".", root)]
    while stack:
        key, path = stack.pop()
        if key != ".":
            relative_path(key)
        info = path.lstat()
        uid, gid, mode = identities.get(key, (info.st_uid, info.st_gid, stat.S_IMODE(info.st_mode)))
        if mode & 0o7000:
            raise ValueError(f"unsupported special permission bits: {key}")
        common = dict(uid=uid, gid=gid, mode=mode)
        if stat.S_ISDIR(info.st_mode):
            result[key] = FileEntry(kind="directory", **common)
            for child in sorted(path.iterdir(), reverse=True):
                stack.append((child.name if key == "." else f"{key}/{child.name}", child))
        elif stat.S_ISREG(info.st_mode):
            result[key] = FileEntry(kind="file", size=info.st_size, sha256=file_hash(path), **common)
        elif stat.S_ISLNK(info.st_mode):
            target = os.readlink(path)
            if not target or target.startswith("/") or "\\" in target or any(ord(c) < 32 for c in target):
                raise ValueError(f"unsafe symlink: {key}")
            try:
                resolved = path.resolve(strict=True)
            except (OSError, RuntimeError) as exc:  # dangling link or link loop: no real target inside the asset
                raise ValueError(f"unsafe symlink: {key}") from exc
            if root.is_symlink() or not resolved.is_relative_to(root.resolve()):
                raise ValueError(f"symlink escapes asset: {key}")
            result[key] = FileEntry(kind="symlink", target=target, **common)
        else:
            raise ValueError(f"unsupported special file: {key}")
    if set(identities) - set(result):
        raise ValueError("identity metadata names missing asset entries")
    return dict(sorted(result.items()))


def validate_inventory(root: Path, expected: dict[str, FileEntry], *, ownership: bool = False) -> None:
    actual = inventory(root)
    if set(actual) != set(expected):
        raise ValueError(f"asset entries missing or unexpected: {root.name}")
    for key, entry in expected.items():
        got = actual[key]
        fields = {"uid", "gid"} if not ownership else set()
        if got.model_dump(exclude=fields) != entry.model_dump(exclude=fields):
            raise ValueError(f"asset integrity mismatch: {root.name}/{key}")


def validate_seed(path: Path) -> None:
    """Validate the same tar that core.Trial will extract; no payload execution."""
    with tarfile.open(path, "r:*") as archive:
        names: set[str] = set()
        links: list[tuple[str, str]] = []
        for member in archive:
            name = member.name.rstrip("/")
            if name == ".":
                continue
            if name.startswith("./"):
                name = name[2:]
            relative_path(name)
            if name in names:
                raise ValueError("duplicate seed entry")
            names.add(name)
            if not (member.isfile() or member.isdir() or member.issym() or member.islnk()):
                raise ValueError("special file in seed")
            if member.mode & 0o7000 or member.uid < 0 or member.gid < 0:
                raise ValueError("unsupported seed metadata")
            if member.issym() or member.islnk():
                target = member.linkname
                if target.startswith("/") or "\\" in target:
                    raise ValueError("absolute seed link")
                joined = posixpath.normpath(
                    posixpath.join(posixpath.dirname(name), target) if member.issym() else target
                )
                relative_path(joined)
                links.append((name, joined))
        # No archive entry may be written through a link created by another member.
        for name, target in links:
            if target not in names or any(p.startswith(name + "/") for p in names):
                raise ValueError("unsafe or dangling seed link")
            if any(target == other or target.startswith(other + "/") for other, _ in links):
                raise ValueError("chained seed links are unsupported")
