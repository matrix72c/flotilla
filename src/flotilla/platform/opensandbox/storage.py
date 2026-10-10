"""部署配置 `[storage]`：把 `SharedVolume` 编译为标准 `host` 或 `pvc` 卷（backends/opensandbox.md 第 6 节）。"""

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
    host_path: str = ""  # volumes = "host"：host.path，绝对路径
    claim_name: str = ""  # volumes = "pvc"：pvc.claimName
    root_subpath: str = ""  # volumes = "pvc"：共享根目录在 claim 内的相对路径；空串为 claim 根目录

    def __post_init__(self) -> None:
        if self.volumes == "host":
            if self.claim_name or self.root_subpath:
                raise ValueError("storage.volumes = host 时不使用 claim_name、root_subpath")
        elif self.volumes == "pvc":
            if self.host_path:
                raise ValueError("storage.volumes = pvc 时不使用 host_path")
        else:
            raise ValueError(f"storage.volumes 须为 host 或 pvc：{self.volumes!r}")
        self.compile(())

    @property
    def root(self) -> str:
        """共享根目录的标识：已发布清单记录它，provider 据此核对清单与配置指向同一个共享根目录（§4.7）。"""
        if self.volumes == "host":
            return f"host:{self.host_path}"
        return f"pvc:{self.claim_name}" + (f"/{self.root_subpath}" if self.root_subpath else "")

    def compile(self, volumes: Sequence[SharedVolume]) -> list[dict[str, Any]]:
        if self.volumes == "host":
            return compile_host_volumes(volumes, self.host_path)
        return compile_pvc_volumes(volumes, self.claim_name, self.root_subpath)
