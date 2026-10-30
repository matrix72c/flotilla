"""Explicit lifecycle filesystem dialect; upstream requests omit extension fields."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, replace
from typing import Any, Literal

from flotilla.platform.base import Capabilities, FilesystemLimits, FilesystemState, InstanceSpec, TmpfsMount
from flotilla.platform.filesystem import memory_bytes

FilesystemProtocol = Literal["upstream", "sandbox-system-v1"]
#: Internal `/dev/shm` size extension (Kubernetes quantity, at most the requested memory). Managed per instance.
SHM_SIZE_EXTENSION = "sandbox-system.io/shm-size"
INTERNAL_LIMITS = FilesystemLimits(
    max_tmpfs_mounts=8,
    forbidden_paths=(
        "/run",
        "/var/run",
        "/opt/opensandbox",
        "/dev",
        "/proc",
        "/sys",
        "/sandbox-data",
        "/data",
        "/var/lib/docker",
    ),
    memory_budget=True,
)


@dataclass(frozen=True)
class FilesystemSettings:
    protocol: FilesystemProtocol = "upstream"

    def capabilities(self, caps: Capabilities) -> Capabilities:
        if self.protocol == "sandbox-system-v1":
            return replace(caps, filesystem_limits=INTERNAL_LIMITS)
        if caps.readonly_rootfs or caps.native_tmpfs or caps.shm_size:
            return replace(caps, readonly_rootfs=False, native_tmpfs=False, shm_size=False)
        return caps

    def extensions(self, spec: InstanceSpec) -> dict[str, str]:
        if self.protocol == "upstream" or spec.shm_size_bytes is None:
            return {}
        return {SHM_SIZE_EXTENSION: str(spec.shm_size_bytes)}

    def compile(self, spec: InstanceSpec) -> dict[str, Any]:
        if self.protocol == "upstream":
            return {}
        return {
            "readOnlyRootFilesystem": spec.readonly_rootfs,
            "tmpfsMounts": [
                {
                    "mountPath": m.mount_path,
                    "size": str(_resolved_size(m)),
                    "mode": f"{m.mode:04o}",
                    "uid": m.uid,
                    "gid": m.gid,
                }
                for m in spec.tmpfs_mounts
            ],
        }


UPSTREAM_FILESYSTEM = FilesystemSettings()


def _resolved_size(mount: TmpfsMount) -> int:
    if mount.size_bytes is None:
        raise ValueError(f"tmpfs mount {mount.mount_path} reached the platform without a resolved size")
    return mount.size_bytes


def parse_effective(item: Mapping[str, Any]) -> FilesystemState | None:
    """Malformed/missing effective data must never pass verification; do not retain the response (it contains env)."""
    if type(item.get("readOnlyRootFilesystem")) is not bool:
        return None
    raw = item.get("tmpfsMounts", [])
    if not isinstance(raw, list):
        return None
    mounts = []
    try:
        for m in raw:
            if not isinstance(m, dict) or not isinstance(m.get("mode"), str):
                return None
            mode = m["mode"]
            if not 1 <= len(mode) <= 4 or any(c not in "01234567" for c in mode):
                return None
            mounts.append(TmpfsMount(m["mountPath"], memory_bytes(m["size"]), int(mode, 8), m["uid"], m["gid"]))
    except (KeyError, TypeError, ValueError, AttributeError):
        return None
    if len({m.mount_path for m in mounts}) != len(mounts):
        return None
    return FilesystemState(item["readOnlyRootFilesystem"], tuple(sorted(mounts, key=lambda m: m.mount_path)))
