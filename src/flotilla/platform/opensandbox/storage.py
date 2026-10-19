"""Validate deployment shared-storage settings."""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any, Literal

from flotilla.platform.base import SharedVolume
from flotilla.platform.opensandbox.volumes import compile_host_volumes, compile_pvc_volumes

VolumeForm = Literal["host", "pvc"]


@dataclass(frozen=True)
class StorageSettings:
    volumes: VolumeForm = "host"
    host_path: str = ""  # volumes = "host": host.path, absolute path
    claim_name: str = ""  # volumes = "pvc": pvc.claimName
    root_subpath: str = ""  # volumes = "pvc": shared root relative to the claim; "" is the claim root

    def __post_init__(self) -> None:
        if self.volumes == "host":
            if self.claim_name or self.root_subpath:
                raise ValueError("storage.volumes = host does not use claim_name or root_subpath")
        elif self.volumes == "pvc":
            if self.host_path:
                raise ValueError("storage.volumes = pvc does not use host_path")
        else:
            raise ValueError(f"storage.volumes must be host or pvc: {self.volumes!r}")
        self.compile(())

    @property
    def root(self) -> str:
        """Shared root identifier: published manifests record it so the provider can check they share the same root."""
        if self.volumes == "host":
            return f"host:{self.host_path}"
        return f"pvc:{self.claim_name}" + (f"/{self.root_subpath}" if self.root_subpath else "")

    def compile(self, volumes: Sequence[SharedVolume]) -> list[dict[str, Any]]:
        if self.volumes == "host":
            return compile_host_volumes(volumes, self.host_path)
        return compile_pvc_volumes(volumes, self.claim_name, self.root_subpath)
