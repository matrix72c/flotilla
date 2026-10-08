"""部署配置 `[storage]`：把 `SharedVolume` 编译为标准 `host` 卷（backends/opensandbox.md 第 6 节）。"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any, Literal

from flotilla.platform.base import SharedVolume
from flotilla.platform.opensandbox.volumes import compile_volumes


@dataclass(frozen=True)
class StorageSettings:
    volumes: Literal["host"] = "host"
    host_path: str = ""  # volumes = "host"：host.path，绝对路径

    def __post_init__(self) -> None:
        if self.volumes != "host":
            raise ValueError(f"storage.volumes 须为 host：{self.volumes!r}")
        compile_volumes((), self.host_path)

    def compile(self, volumes: Sequence[SharedVolume]) -> list[dict[str, Any]]:
        return compile_volumes(volumes, self.host_path)
