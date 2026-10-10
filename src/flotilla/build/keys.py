"""构建键与内容哈希（Architecture §4.1 幂等）。

构建键 = "构建上下文内容哈希 + Compose 规范化结果 + flotilla 构建器版本 + 影响构建产物的部署输入"。键不变则跳过
构建。同一任务为两种部署构建时得到两个键，清单记录所用的值。

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
BUILDER_VERSION = 1

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
        try:
            entries = sorted(os.scandir(current), key=lambda e: e.name)
        except OSError:
            continue
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
) -> str:
    """任务的构建键（§4.1）。

    - `context_hashes`：服务 → 该服务构建上下文的 `tree_hash`（只有 `build` 的服务有）；
    - `deployment_inputs`：影响构建产物的部署输入，目前只有"入口包装是否需要 /bin/sh"（§4.2 第 7 步）。
      两种部署的输入不同即得到两个键。
    """
    payload = {
        "builder": BUILDER_VERSION,
        "flotilla": __version__.split("+")[0],  # 本地构建的 `+dirty` 之类不参与键
        "project": _canonical(project),
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
