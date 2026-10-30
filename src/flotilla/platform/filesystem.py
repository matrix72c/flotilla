"""Backend-independent filesystem requirements and normalized quantities."""

from __future__ import annotations

import posixpath
import re
from collections.abc import Iterable
from dataclasses import dataclass
from decimal import Decimal

RUNTIME_MOUNT = "/.flotilla-runtime"
RESERVED_MOUNTS = ("/.flotilla", RUNTIME_MOUNT)


def memory_bytes(value: str) -> int:
    """Parse a backend byte quantity (binary Ki/Mi/... or decimal K/M/...)."""
    match = re.fullmatch(r"([0-9]+(?:\.[0-9]+)?)([KMGTPE]i?|k)?", value)
    if match is None:
        raise ValueError("memory must be a positive byte count or backend quantity")
    suffix = match[2] or ""
    power = "KMGTPE".index(suffix[0].upper()) + 1 if suffix else 0
    count = Decimal(match[1]) * (1024 if suffix.endswith("i") else 1000) ** power
    if count <= 0 or count != count.to_integral_value():
        raise ValueError("memory must resolve to a positive whole byte count")
    return int(count)


def overlaps(a: str, b: str) -> bool:
    return a == b or a.startswith(b.rstrip("/") + "/") or b.startswith(a.rstrip("/") + "/")


@dataclass(frozen=True)
class TmpfsMount:
    mount_path: str
    # None mirrors docker's unsized tmpfs: bounded by the sandbox memory, not a fixed cap.
    # The runtime resolves it with `unsized_tmpfs_bytes` before the instance spec is built.
    size_bytes: int | None = None
    mode: int = 0o1777
    uid: int = 0
    gid: int = 0

    def __post_init__(self) -> None:
        path = self.mount_path
        if (
            not isinstance(path, str)
            or not path.startswith("/")
            or path.startswith("//")
            or path == "/"
            or posixpath.normpath(path) != path
            or any(ord(c) < 32 for c in path)
        ):
            raise ValueError("tmpfs mount_path must be a canonical absolute directory other than root")
        if self.size_bytes is not None:
            if type(self.size_bytes) is not int:
                raise ValueError("tmpfs size_bytes must be an integer or null")
            if self.size_bytes <= 0:
                raise ValueError("tmpfs size_bytes must be positive")
        for name in ("mode", "uid", "gid"):
            if type(getattr(self, name)) is not int:
                raise ValueError(f"tmpfs {name} must be an integer")
        if not 0 <= self.mode <= 0o7777:
            raise ValueError("tmpfs mode must be between 0000 and 7777")
        if not 0 <= self.uid < 2**32 - 1 or not 0 <= self.gid < 2**32 - 1:
            raise ValueError("tmpfs uid and gid must be valid nonnegative IDs")


@dataclass(frozen=True)
class FilesystemLimits:
    max_tmpfs_mounts: int | None = None
    forbidden_paths: tuple[str, ...] = ()
    memory_budget: bool = False


@dataclass(frozen=True)
class FilesystemState:
    readonly_rootfs: bool
    tmpfs_mounts: tuple[TmpfsMount, ...] = ()


NO_FILESYSTEM_LIMITS = FilesystemLimits()
_TMPFS_FLOOR_BYTES = 64 * 1024 * 1024


def unsized_tmpfs_bytes(memory: str) -> int:
    """Concrete size for an unsized tmpfs: docker bounds it only by the container memory, so half the service
    memory (the Linux tmpfs convention) is the closest fixed approximation, floored at the historical 64 MiB so
    small services keep working. Admission and planning share it, so both check the size that is created."""
    return max(_TMPFS_FLOOR_BYTES, memory_bytes(memory) // 2)


def validate_shm_size(size_bytes: int | None) -> None:
    if size_bytes is not None and (type(size_bytes) is not int or size_bytes <= 0):
        raise ValueError("shm_size must be a positive whole byte count")


def validate_mounts(
    mounts: tuple[TmpfsMount, ...],
    volume_paths: Iterable[str] = (),
    *,
    limits: FilesystemLimits = NO_FILESYSTEM_LIMITS,
    memory: str | None = None,
    shm_size_bytes: int | None = None,
) -> None:
    """`shm_size_bytes` (an explicit `/dev/shm` size) shares the tmpfs memory budget."""
    validate_shm_size(shm_size_bytes)
    if limits.max_tmpfs_mounts is not None and len(mounts) > limits.max_tmpfs_mounts:
        raise ValueError(f"tmpfs mount count exceeds deployment limit {limits.max_tmpfs_mounts}")
    paths = tuple(volume_paths)
    for i, mount in enumerate(mounts):
        path = mount.mount_path
        if any(overlaps(path, reserved) for reserved in (*RESERVED_MOUNTS, *limits.forbidden_paths)):
            raise ValueError(f"tmpfs path {path} conflicts with a reserved mount")
        if any(overlaps(path, other.mount_path) for other in mounts[:i]):
            raise ValueError(f"tmpfs path {path} duplicates or overlaps another tmpfs mount")
        if any(overlaps(path, other) for other in paths):
            raise ValueError(f"tmpfs path {path} conflicts with a volume mount")
    if (mounts or shm_size_bytes) and limits.memory_budget and memory is not None:
        tmpfs = sum(m.size_bytes if m.size_bytes is not None else unsized_tmpfs_bytes(memory) for m in mounts)
        if tmpfs + (shm_size_bytes or 0) > memory_bytes(memory):
            raise ValueError("tmpfs and shm_size capacities exceed the sandbox requested memory")
