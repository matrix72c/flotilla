"""`SharedVolume` → 标准 `host` / `pvc` 卷（backends/opensandbox.md 第 6 节）。纯函数。

```json
{"name": "v2", "host": {"path": "<storage.host_path>"}, "subPath": "launches/L/t/static",
 "mountPath": "/srv/static", "readOnly": false}
{"name": "v2", "pvc": {"claimName": "<storage.claim_name>", "createIfNotExists": false},
 "subPath": "<storage.root_subpath>/launches/L/t/static", "mountPath": "/srv/static", "readOnly": false}
```

`name` 按 `v<序号>` 生成，不承载语义；顺序即挂载顺序（嵌套挂载外层先挂，Architecture §4.4）。
空 key（锚点挂共享根目录本身）时 `subPath` 只剩共享根目录在后端中的位置，也为空时不写 `subPath`。
"""

from __future__ import annotations

import posixpath
import re
from collections.abc import Sequence
from typing import Any

from flotilla.platform.base import SharedVolume

#: PVC 名字须为 DNS label（上游 OpenSandbox `PVC.claimName`）。
_CLAIM_NAME = re.compile(r"[a-z0-9]([-a-z0-9]*[a-z0-9])?")
_CLAIM_NAME_MAX = 253


def check_key(key: str) -> None:
    """卷键是相对共享根目录的规范路径（§7.2）：空串表示根目录本身，否则不含空段、`.`、`..`。"""
    if key and any(p in ("", ".", "..") for p in key.split("/")):
        raise ValueError(f"卷键不是合法的相对路径：{key!r}")


def check_mount_path(mount_path: str) -> None:
    if not posixpath.isabs(mount_path):
        raise ValueError(f"挂载点须为绝对路径：{mount_path!r}")


def check_claim_name(claim_name: str) -> None:
    if len(claim_name) > _CLAIM_NAME_MAX or not _CLAIM_NAME.fullmatch(claim_name):
        raise ValueError(f"storage.claim_name 须为 DNS label（小写字母、数字与 -）：{claim_name!r}")


def compile_host_volumes(volumes: Sequence[SharedVolume], host_path: str) -> list[dict[str, Any]]:
    if not posixpath.isabs(host_path):
        raise ValueError(f"storage.host_path 须为绝对路径：{host_path!r}")
    return [_entry(i, vol, {"host": {"path": host_path}}, vol.key) for i, vol in enumerate(volumes)]


def compile_pvc_volumes(volumes: Sequence[SharedVolume], claim_name: str, root_subpath: str) -> list[dict[str, Any]]:
    """`createIfNotExists` 固定为 false：claim 名写错时创建失败，而不是自动建一个空的新卷让 trial 挂上去。"""
    check_claim_name(claim_name)
    try:
        check_key(root_subpath)
    except ValueError:
        raise ValueError(f"storage.root_subpath 不是合法的相对路径：{root_subpath!r}") from None
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
