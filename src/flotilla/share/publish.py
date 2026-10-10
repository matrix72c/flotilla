"""`flotilla publish`：把构建产出的任务文件放上共享存储（Architecture §4.7）。

构建离线、没有平台凭证；上传需要凭证，所以拆成这一步，在能访问平台的机器上执行。

步骤（§4.7）：

1. 经临时锚点把 `<out>/<build_key>/files/` 上传到 `tasks/.staging/<build_key>.<uuid>/`，逐文件校验 sha256；
2. 恢复属主与权限，写出 `FILES.json`（每个条目的路径、类型、mode、uid/gid、大小与 sha256、符号链接目标，
   按路径排序），再原子 `rename` 到 `tasks/<build_key>/`。目标已存在且 `FILES.json` 一致时跳过，不一致报错、不覆盖；
3. 在清单里写入 `task_files` 与 `task_files_published`（`storage_root` + `FILES.json` 的 sha256）。

`FILES.json` 的条目规则与构建键的内容哈希（`keys.py`）相同，这样"构建出来的"与"发布上去的"用同一套口径描述。
没有任务文件的任务不需要发布：清单的 `task_files` 保持为空。
"""

from __future__ import annotations

import contextlib
import hashlib
import json
import os
import posixpath
import stat
import uuid
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path

from flotilla.core.anchor import Anchor
from flotilla.manifest import Manifest, Published

TASKS = "tasks"
STAGING = f"{TASKS}/.staging"
FILES_JSON = "FILES.json"
#: 单次上传的上限：任务文件逐个经执行通道上传，过大的文件应当作构建产物问题而不是默默传上去。
MAX_FILE_BYTES = 512 * 1024 * 1024


class PublishError(ValueError):
    """发布失败（文件过大、校验不一致、目标已存在且内容不同等）。"""


@dataclass(frozen=True)
class PublishResult:
    build_key: str
    task_files: str  # `tasks/<build_key>`
    files_sha256: str
    uploaded: int  # 本次上传的文件数；跳过时为 0
    skipped: bool


def entries(files_dir: Path) -> list[dict[str, object]]:
    """`FILES.json` 的条目（按路径排序）。规则与 `keys.tree_hash` 一致（§4.7）。"""
    out: list[dict[str, object]] = []
    for rel, info in _walk(files_dir):
        entry: dict[str, object] = {
            "path": rel,
            "type": _kind(info),
            "mode": f"{stat.S_IMODE(info.st_mode):04o}",
            "uid": info.st_uid,
            "gid": info.st_gid,
        }
        path = files_dir / rel
        if stat.S_ISLNK(info.st_mode):
            entry["target"] = os.readlink(path)
        elif stat.S_ISREG(info.st_mode):
            entry["size"] = info.st_size
            entry["sha256"] = _sha256(path)
        out.append(entry)
    return sorted(out, key=lambda e: str(e["path"]))


def files_json(files_dir: Path) -> tuple[bytes, str]:
    """`FILES.json` 的内容与它自身的 sha256（清单记录后者）。"""
    blob = json.dumps(entries(files_dir), sort_keys=True, ensure_ascii=False, indent=2).encode() + b"\n"
    return blob, hashlib.sha256(blob).hexdigest()


async def publish_task_files(
    anchor: Anchor,
    build_key: str,
    files_dir: Path,
    *,
    storage_root: str,
) -> PublishResult:
    """把一个任务的 `files/` 发布到 `tasks/<build_key>`（§4.7 第 1、2 步）。

    已发布且 `FILES.json` 一致时跳过；不一致则报错，不覆盖——同一个构建键必须对应同一份内容。
    """
    blob, digest = files_json(files_dir)
    target = f"{TASKS}/{build_key}"
    if await anchor.exists(f"{target}/{FILES_JSON}", stage="prepare"):
        existing = await anchor.download(f"{target}/{FILES_JSON}", stage="prepare")
        if hashlib.sha256(existing).hexdigest() == digest:
            return PublishResult(build_key, target, digest, 0, True)
        raise PublishError(
            f"{target} 已发布，但 {FILES_JSON} 与本次构建不一致（同一构建键必须对应同一份内容）；要重新发布请先删除它"
        )
    staging = f"{STAGING}/{build_key}.{uuid.uuid4().hex[:12]}"
    await anchor.mkdir(staging, stage="prepare")
    try:
        uploaded = await _upload_tree(anchor, files_dir, staging)
        await anchor.upload(f"{staging}/{FILES_JSON}", blob, mode=0o644, uid=0, gid=0, stage="prepare")
        await anchor.mkdir(TASKS, stage="prepare")
        await anchor.rename(staging, target, stage="prepare")
    except BaseException:
        await _discard(anchor, staging)
        raise
    return PublishResult(build_key, target, digest, uploaded, False)


async def _upload_tree(anchor: Anchor, files_dir: Path, staging: str) -> int:
    """上传目录树：先建目录（并设 mode 与属主），再传文件，最后建符号链接。逐文件校验 sha256。"""
    uploaded = 0
    for rel, info in _walk(files_dir):
        key = posixpath.join(staging, rel)
        mode = stat.S_IMODE(info.st_mode)
        if stat.S_ISDIR(info.st_mode):
            await anchor.mkdir(key, stage="prepare")
            await anchor.chmod(key, mode, stage="prepare")
            await anchor.chown(key, info.st_uid, info.st_gid, stage="prepare")
            continue
        if stat.S_ISLNK(info.st_mode):
            # 锚点镜像只带必要的 busybox applet，没有 ln；任务文件里出现符号链接时明确拒绝，
            # 不静默跳过（跳过会让发布上去的任务文件与构建产物不一致）。
            raise PublishError(f"任务文件 {rel} 是符号链接，发布尚不支持（锚点镜像没有 ln）")
        if not stat.S_ISREG(info.st_mode):
            raise PublishError(f"任务文件里有不支持的条目类型：{rel}")
        if info.st_size > MAX_FILE_BYTES:
            raise PublishError(f"任务文件 {rel} 有 {info.st_size} 字节，超过上限 {MAX_FILE_BYTES}")
        data = (files_dir / rel).read_bytes()
        await anchor.upload(key, data, mode=mode, uid=info.st_uid, gid=info.st_gid, stage="prepare")
        back = await anchor.download(key, stage="prepare")
        if hashlib.sha256(back).hexdigest() != hashlib.sha256(data).hexdigest():
            raise PublishError(f"任务文件 {rel} 上传后校验不一致")
        uploaded += 1
    return uploaded


async def _discard(anchor: Anchor, staging: str) -> None:
    """清理 staging；清理失败不覆盖原始错误（残留的 .staging 留给 gc）。"""
    with contextlib.suppress(Exception):
        await anchor.remove(staging, stage="run")


def mark_published(manifest: Manifest, result: PublishResult, storage_root: str) -> Manifest:
    """清单改为已发布（§4.7 第 3 步）。"""
    return manifest.model_copy(
        update={
            "task_files": result.task_files,
            "task_files_published": Published(storage_root=storage_root, files_sha256=result.files_sha256),
        }
    )


def _walk(root: Path) -> Iterator[tuple[str, os.stat_result]]:
    """按路径排序遍历，产出 (相对路径, lstat)。目录先于其内容，便于逐级创建。

    不跟随符号链接：`os.walk` 会把指向目录的链接列在 `dirs` 里但不进入它，这里按 lstat 判定类型，
    所以这种条目作为 link 产出（`_kind` 给 "link"），它指向的内容不重复遍历。
    """
    for current, dirs, names in os.walk(root):
        dirs.sort()
        base = Path(current)
        for name in sorted([*dirs, *names]):
            path = base / name
            yield str(path.relative_to(root)), path.lstat()


def _kind(info: os.stat_result) -> str:
    if stat.S_ISDIR(info.st_mode):
        return "dir"
    if stat.S_ISLNK(info.st_mode):
        return "link"
    return "file" if stat.S_ISREG(info.st_mode) else "other"


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        while chunk := handle.read(1 << 20):
            digest.update(chunk)
    return digest.hexdigest()
