"""构建键与内容哈希（Architecture §4.1 幂等）。

构建键包含任务身份、environment 内容哈希、Compose 规范化结果、固定 digest 的源镜像、构建器版本和影响
产物的配置。键不变则跳过构建；部署输入不同得到不同的键，清单记录所用的值。

内容哈希的规则与任务发布的 `FILES.json`（§4.7）相同：逐条目计入**路径、类型、mode、uid / gid、文件内容、
符号链接目标**；不计 mtime、xattr、ACL。目录条目也计入（空目录是有意义的）。遍历按路径排序，跨机器可复现。
"""

from __future__ import annotations

import hashlib
import json
import os
import stat
from collections.abc import Iterable, Iterator, Mapping
from dataclasses import asdict, is_dataclass
from pathlib import Path
from typing import Any

from flotilla import __version__
from flotilla.compose.model import Project

#: 构建器版本：影响构建产物的实现变化要手动提升它（`__version__` 只跟 git tag 走，发布不一定带构建改动）。
BUILDER_VERSION = 2

_CHUNK = 1 << 20


def tree_hash(root: Path, *, exclude: Iterable[str] = ()) -> str:
    """目录树的内容哈希（见模块说明）。`exclude` 是相对 `root` 的顶层名字（例如 `.git`）。"""
    skip = set(exclude)
    digest = hashlib.sha256()
    for rel, entry in _walk(root, skip):
        digest.update(_entry_line(rel, entry, root).encode())
    return digest.hexdigest()


def _walk(root: Path, skip: set[str]) -> Iterator[tuple[str, os.stat_result]]:
    """按路径排序遍历 `root`，产出 (相对路径, lstat)。不跟随符号链接。"""
    stack = [root]
    while stack:
        current = stack.pop()
        with os.scandir(current) as scanned:
            entries = sorted(scanned, key=lambda e: e.name)
        for entry in entries:
            rel = str(Path(entry.path).relative_to(root))
            if rel.split("/")[0] in skip:
                continue
            info = entry.stat(follow_symlinks=False)
            yield rel, info
            if stat.S_ISDIR(info.st_mode):
                stack.append(Path(entry.path))


def _entry_line(rel: str, info: os.stat_result, root: Path) -> str:
    """一个条目的规范化描述：路径、类型、mode、uid/gid，再加内容（文件）或链接目标（符号链接）。"""
    mode = stat.S_IMODE(info.st_mode)
    head = f"{rel}\0{_kind(info)}\0{mode:04o}\0{info.st_uid}:{info.st_gid}"
    path = root / rel
    if stat.S_ISLNK(info.st_mode):
        return f"{head}\0{os.readlink(path)}\n"
    if stat.S_ISREG(info.st_mode):
        return f"{head}\0{_file_hash(path)}\n"
    return f"{head}\n"


def _kind(info: os.stat_result) -> str:
    if stat.S_ISDIR(info.st_mode):
        return "dir"
    if stat.S_ISLNK(info.st_mode):
        return "link"
    return "file" if stat.S_ISREG(info.st_mode) else "other"


def _file_hash(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        while chunk := handle.read(_CHUNK):
            digest.update(chunk)
    return digest.hexdigest()


def build_key(
    *,
    project: Project,
    context_hashes: Mapping[str, str],
    deployment_inputs: Mapping[str, Any],
    task: str = "",
    source_images: Mapping[str, str] | None = None,
) -> str:
    """任务的构建键（§4.1）。

    - `task`：任务身份，防止相同环境的不同任务复用带着另一个任务名的清单；
    - `context_hashes`：输入目录的内容哈希，含构建上下文与待导出的 bind，不限于需要 BuildKit 的服务；
    - `source_images`：服务 → 实际选中且固定 digest 的源镜像（含预构建与派生基础镜像）；
    - `deployment_inputs`：影响产物的构建配置、有效资源配置与入口包装要求。
    """
    payload = {
        "builder": BUILDER_VERSION,
        "flotilla": __version__.split("+")[0],  # 本地构建的 `+dirty` 之类不参与键
        "project": _canonical(project),
        "task": task,
        "source_images": dict(sorted((source_images or {}).items())),
        "contexts": dict(sorted(context_hashes.items())),
        "deployment": dict(sorted(deployment_inputs.items())),
    }
    blob = json.dumps(payload, sort_keys=True, ensure_ascii=False, separators=(",", ":"), default=_default)
    return hashlib.sha256(blob.encode()).hexdigest()


def _canonical(value: Any) -> Any:
    """`Project` → 可稳定序列化的结构：dataclass 转 dict，集合排序，映射按键排序。"""
    if is_dataclass(value) and not isinstance(value, type):
        return _canonical(asdict(value))
    if isinstance(value, Mapping):
        return {str(k): _canonical(v) for k, v in sorted(value.items(), key=lambda kv: str(kv[0]))}
    if isinstance(value, frozenset | set):
        return sorted(_canonical(v) for v in value)
    if isinstance(value, list | tuple):
        return [_canonical(v) for v in value]
    return value


def _default(value: Any) -> Any:
    return _canonical(value)
