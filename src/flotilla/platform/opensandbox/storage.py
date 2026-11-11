"""Validate deployment shared-storage settings."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any, Literal

from flotilla.platform.base import WORK_ROOT, SharedVolume
from flotilla.platform.opensandbox.volumes import check_host_path, check_pvc, check_root_subpath, host_entry, pvc_entry

VolumeForm = Literal["host", "pvc"]


@dataclass(frozen=True)
class StorageSettings:
    volumes: VolumeForm = "host"
    host_path: str = ""  # volumes = "host": host.path, an absolute path or platform identifier, passed verbatim
    claim_name: str = ""  # volumes = "pvc": pvc.claimName
    root_subpath: str = ""  # shared root relative to the host path or claim; "" is the host path / claim root

    def __post_init__(self) -> None:
        if self.volumes == "host":
            if self.claim_name:
                raise ValueError("storage.volumes = host does not use claim_name")
        elif self.volumes == "pvc":
            if self.host_path:
                raise ValueError("storage.volumes = pvc does not use host_path")
        else:
            raise ValueError(f"storage.volumes must be host or pvc: {self.volumes!r}")
        if self.volumes == "host":
            check_host_path(self.host_path)
            check_root_subpath(self.root_subpath)
        else:
            check_pvc(self.claim_name, self.root_subpath)

    @property
    def root(self) -> str:
        """Shared root identifier: published manifests record it so the provider can check they share the same root."""
        suffix = f"/{self.root_subpath}" if self.root_subpath else ""
        if self.volumes == "host":
            return f"host:{self.host_path}" + suffix
        return f"pvc:{self.claim_name}" + suffix

    def entry(self, index: int, volume: SharedVolume) -> dict[str, Any]:
        if self.volumes == "host":
            return host_entry(index, volume, self.host_path, self.root_subpath)
        return pvc_entry(index, volume, self.claim_name, self.root_subpath)

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
