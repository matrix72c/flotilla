"""Validate deployment shared-storage settings and compile volume mount requests."""

from __future__ import annotations

import posixpath
import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any, Literal

from flotilla.platform.base import WORK_ROOT, SharedVolume

VolumeForm = Literal["host", "pvc"]

#: PVC names must be DNS labels (upstream OpenSandbox `PVC.claimName`).
_CLAIM_NAME = re.compile(r"[a-z0-9]([-a-z0-9]*[a-z0-9])?")
_CLAIM_NAME_MAX = 253


def check_key(key: str) -> None:
    """A volume key is a normalized path relative to the shared root: "" is the root, else no empty, `.`, `..` parts."""
    if key and any(p in ("", ".", "..") for p in key.split("/")):
        raise ValueError(f"volume key is not a valid relative path: {key!r}")


def check_mount_path(mount_path: str) -> None:
    if not posixpath.isabs(mount_path):
        raise ValueError(f"mount path must be absolute: {mount_path!r}")


def check_claim_name(claim_name: str) -> None:
    if len(claim_name) > _CLAIM_NAME_MAX or not _CLAIM_NAME.fullmatch(claim_name):
        raise ValueError(f"storage.claim_name must be a DNS label (lowercase letters, digits, and -): {claim_name!r}")


def check_host_path(host_path: str) -> None:
    """The host root is opaque: an absolute path or a platform-defined identifier, passed through verbatim."""
    if not host_path or host_path != host_path.strip() or any(ord(c) < 32 or ord(c) == 127 for c in host_path):
        raise ValueError(
            f"storage.host_path must be a non-empty path or platform identifier without whitespace: {host_path!r}"
        )


def check_root_subpath(root_subpath: str) -> None:
    try:
        check_key(root_subpath)
    except ValueError:
        raise ValueError(f"storage.root_subpath is not a valid relative path: {root_subpath!r}") from None


@dataclass(frozen=True)
class StorageSettings:
    """The compiled `[storage]` config; the `Storage` config model validated it at load."""

    volumes: VolumeForm = "host"
    host_path: str = ""  # volumes = "host": host.path, an absolute path or platform identifier, passed verbatim
    claim_name: str = ""  # volumes = "pvc": pvc.claimName
    root_subpath: str = ""  # shared root relative to the host path or claim; "" is the host path / claim root

    @property
    def root(self) -> str:
        """Shared root identifier: published manifests record it so the provider can check they share the same root."""
        suffix = f"/{self.root_subpath}" if self.root_subpath else ""
        if self.volumes == "host":
            return f"host:{self.host_path}" + suffix
        return f"pvc:{self.claim_name}" + suffix

    def entry(self, index: int, volume: SharedVolume) -> dict[str, Any]:
        backend: dict[str, Any]
        if self.volumes == "host":
            backend = {"host": {"path": self.host_path}}
        else:
            backend = {"pvc": {"claimName": self.claim_name, "createIfNotExists": False}}
        return _entry(index, volume, backend, _under(self.root_subpath, volume.key))

    def compile(self, volumes: Sequence[SharedVolume]) -> list[dict[str, Any]]:
        return [self.entry(i, volume) for i, volume in enumerate(volumes)]


def compile_volumes(
    volumes: Sequence[SharedVolume], work: StorageSettings, inputs: Mapping[str, StorageSettings] | None = None
) -> list[dict[str, Any]]:
    """Compile each volume against its configured root: `work` or a named read-only input root."""
    inputs = inputs or {}
    out = []
    for i, volume in enumerate(volumes):
        if volume.root == WORK_ROOT:
            settings = work
        elif volume.root in inputs:
            settings = inputs[volume.root]
        else:
            raise ValueError(f"volume uses unconfigured storage root {volume.root!r}")
        out.append(settings.entry(i, volume))
    return out


def _under(root_subpath: str, key: str) -> str:
    return posixpath.join(root_subpath, key) if root_subpath and key else root_subpath or key


def _entry(i: int, vol: SharedVolume, backend: dict[str, Any], sub_path: str) -> dict[str, Any]:
    check_key(vol.key)
    check_mount_path(vol.mount_path)
    entry: dict[str, Any] = {"name": f"v{i}", **backend, "mountPath": vol.mount_path, "readOnly": vol.read_only}
    if sub_path:
        entry["subPath"] = sub_path
    return entry
