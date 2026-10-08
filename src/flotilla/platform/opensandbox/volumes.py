"""`SharedVolume` → 标准 `host` 卷（backends/opensandbox.md 第 6 节）。纯函数。

```json
{"name": "v2", "host": {"path": "<storage.host_path>"}, "subPath": "launches/L/t/static",
 "mountPath": "/srv/static", "readOnly": false}
```

`name` 按 `v<序号>` 生成，不承载语义；顺序即挂载顺序（嵌套挂载外层先挂，Architecture §4.4）。
空 key（锚点挂共享根目录本身）不写 `subPath`。
"""

from __future__ import annotations

import posixpath
from collections.abc import Sequence
from typing import Any

from flotilla.platform.base import SharedVolume


def check_key(key: str) -> None:
    """卷键是相对共享根目录的规范路径（§7.2）：空串表示根目录本身，否则不含空段、`.`、`..`。"""
    if key and any(p in ("", ".", "..") for p in key.split("/")):
        raise ValueError(f"卷键不是合法的相对路径：{key!r}")


def check_mount_path(mount_path: str) -> None:
    if not posixpath.isabs(mount_path):
        raise ValueError(f"挂载点须为绝对路径：{mount_path!r}")


def compile_volumes(volumes: Sequence[SharedVolume], host_path: str) -> list[dict[str, Any]]:
    if not posixpath.isabs(host_path):
        raise ValueError(f"storage.host_path 须为绝对路径：{host_path!r}")
    out: list[dict[str, Any]] = []
    for i, vol in enumerate(volumes):
        check_key(vol.key)
        check_mount_path(vol.mount_path)
        entry: dict[str, Any] = {
            "name": f"v{i}",
            "host": {"path": host_path},
            "mountPath": vol.mount_path,
            "readOnly": vol.read_only,
        }
        if vol.key:
            entry["subPath"] = vol.key
        out.append(entry)
    return out
