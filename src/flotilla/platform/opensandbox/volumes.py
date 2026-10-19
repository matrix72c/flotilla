"""Translate confined shared volumes into deployment mount requests."""

from __future__ import annotations

import posixpath
import re
from collections.abc import Sequence
from typing import Any

from flotilla.platform.base import SharedVolume

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


def compile_host_volumes(volumes: Sequence[SharedVolume], host_path: str) -> list[dict[str, Any]]:
    if not posixpath.isabs(host_path):
        raise ValueError(f"storage.host_path must be absolute: {host_path!r}")
    return [_entry(i, vol, {"host": {"path": host_path}}, vol.key) for i, vol in enumerate(volumes)]


def compile_pvc_volumes(volumes: Sequence[SharedVolume], claim_name: str, root_subpath: str) -> list[dict[str, Any]]:
    """`createIfNotExists` is always false: a mistyped claim fails creation instead of mounting a new empty volume."""
    check_claim_name(claim_name)
    try:
        check_key(root_subpath)
    except ValueError:
        raise ValueError(f"storage.root_subpath is not a valid relative path: {root_subpath!r}") from None
    backend = {"pvc": {"claimName": claim_name, "createIfNotExists": False}}
    return [
        _entry(i, vol, backend, posixpath.join(root_subpath, vol.key) if vol.key else root_subpath)
        for i, vol in enumerate(volumes)
    ]


def _entry(i: int, vol: SharedVolume, backend: dict[str, Any], sub_path: str) -> dict[str, Any]:
    check_key(vol.key)
    check_mount_path(vol.mount_path)
    entry: dict[str, Any] = {"name": f"v{i}", **backend, "mountPath": vol.mount_path, "readOnly": vol.read_only}
    if sub_path:
        entry["subPath"] = sub_path
    return entry
