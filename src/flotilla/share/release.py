"""share 发布的布局与 publish / verify / gc（Architecture §3.3）。

share 是每个单元只读挂到 `/.flotilla` 的内容：占位入口要的静态 busybox、agent 运行时与 harness。
它按发布号版本化，发布不可变，`provider` 配置钉住发布号与 `RELEASE.json` 的 sha256（§16.3）。

布局（相对共享根目录，§7.2）：

```
share/releases/<release>/     ← 一次发布的正式位置，写完就不再改动
  bin/busybox
  RELEASE.json                发布号、各组件版本、全部文件的内容清单
share/staging/<release>.<id>/ ← 组装中的发布，原子改名后才成为正式位置
```

`RELEASE.json` 的形状与序列化方式按 AFS 上真实发布（`2026-10-07.1`）反推，逐字节可复现：整份 JSON 用
`sort_keys=True, ensure_ascii=False, indent=2` 加一个尾换行。条目只有 `mode`、`sha256`、`size` 三项。

命令都在能访问平台的机器上执行，经临时锚点（§5.6）操作共享存储：构建与训练进程都不持有平台凭证。
"""

from __future__ import annotations

import contextlib
import hashlib
import json
import os
import posixpath
import stat
import uuid
from collections.abc import Mapping, Set
from dataclasses import dataclass
from pathlib import Path

from flotilla.core.anchor import Anchor

SHARE = "share"
RELEASES = f"{SHARE}/releases"
STAGING = f"{SHARE}/staging"
RELEASE_JSON = "RELEASE.json"
#: 单文件上限：任务内容经执行通道逐个上传，过大的文件是构建产物的问题，不该默默传上去。
MAX_FILE_BYTES = 512 * 1024 * 1024


class ReleaseError(ValueError):
    """发布失败（含符号链接或过大文件、发布号已存在且内容不同、改名未生效等）。"""


@dataclass(frozen=True)
class ReleaseResult:
    release: str
    path: str  # `share/releases/<release>`
    release_sha256: str  # `RELEASE.json` 自身的 sha256，写进部署配置的那一个
    uploaded: int  # 本次上传的文件数；跳过时为 0
    skipped: bool


@dataclass(frozen=True)
class VerifyReport:
    """一次全量校验的结果（§3.3：全量 sha256 由 `flotilla share verify` 按需执行）。"""

    release: str
    #: `RELEASE.json` 自身的 sha256 与部署配置钉的值是否一致。为假时其余校验不再有意义。
    manifest_ok: bool
    actual_sha256: str
    expected_sha256: str
    checked: int
    missing: tuple[str, ...] = ()
    size_mismatch: tuple[str, ...] = ()
    digest_mismatch: tuple[str, ...] = ()
    mode_mismatch: tuple[str, ...] = ()

    @property
    def ok(self) -> bool:
        broken = self.missing or self.size_mismatch or self.digest_mismatch or self.mode_mismatch
        return self.manifest_ok and not broken


@dataclass(frozen=True)
class GcReleasesReport:
    """`share gc` 的结果。`staging` 只列出、不删除（见 `gc_releases`）。"""

    kept: frozenset[str]
    removed: frozenset[str]
    staging: tuple[str, ...] = ()
    dry_run: bool = False


def release_entries(root: Path) -> list[tuple[str, os.stat_result]]:
    """发布的文件条目，按路径排序。规则与 `RELEASE.json` 一致。

    只收常规文件：清单里没有目录条目，目录在上传时按需创建为默认权限。根目录的 `RELEASE.json` 是清单
    本身、每次重新生成，所以排除；更深层的同名文件是正常内容，照收。

    符号链接一律拒绝而不是跳过：锚点镜像没有 `ln`，静默跳过会让发布上去的内容与源不一致。
    """
    out: list[tuple[str, os.stat_result]] = []
    for current, dirs, names in os.walk(root):
        dirs.sort()
        base = Path(current)
        for name in sorted(names):
            path = base / name
            info = path.lstat()
            rel = str(path.relative_to(root))
            if stat.S_ISLNK(info.st_mode):
                raise ReleaseError(f"{rel} 是符号链接，share 发布不支持（锚点镜像没有 ln）")
            if not stat.S_ISREG(info.st_mode):
                raise ReleaseError(f"{rel} 不是普通文件，share 发布不支持")
            if rel == RELEASE_JSON:
                continue
            if info.st_size > MAX_FILE_BYTES:
                raise ReleaseError(f"{rel} 有 {info.st_size} 字节，超过上限 {MAX_FILE_BYTES}")
            out.append((rel, info))
    return sorted(out, key=lambda item: item[0])


def release_json(release: str, root: Path, components: Mapping[str, str]) -> tuple[bytes, str]:
    """`RELEASE.json` 的内容与它自身的 sha256（部署配置里钉的就是后者）。

    序列化方式与 AFS 上的真实发布逐字节一致，所以同一份内容总能算出同一个摘要。
    """
    files: dict[str, object] = {}
    for rel, info in release_entries(root):
        files[rel] = {
            "mode": f"{stat.S_IMODE(info.st_mode):04o}",
            "sha256": _sha256(root / rel),
            "size": info.st_size,
        }
    document = {"components": dict(components), "files": files, "release": release}
    blob = json.dumps(document, sort_keys=True, ensure_ascii=False, indent=2).encode() + b"\n"
    return blob, hashlib.sha256(blob).hexdigest()


async def publish_release(
    anchor: Anchor,
    release: str,
    root: Path,
    *,
    components: Mapping[str, str],
) -> ReleaseResult:
    """把 `root` 发布为 `share/releases/<release>`（§3.3）。

    发布不可变：目标已存在且 `RELEASE.json` 一致时跳过；不一致时报错，不覆盖。**不做硬链接复用** ——
    未改动的组件重新上传（能力上可行：busybox 的 `cp -l` 内建于 `cp`，不需要外部 `ln`，但真实发布只有少量
    文件，复用的收益为零）。
    """
    blob, digest = release_json(release, root, components)
    target = f"{RELEASES}/{release}"
    if await anchor.exists(f"{target}/{RELEASE_JSON}", stage="prepare"):
        if hashlib.sha256(await anchor.download(f"{target}/{RELEASE_JSON}", stage="prepare")).hexdigest() == digest:
            return ReleaseResult(release, target, digest, 0, True)
        raise ReleaseError(f"{target} 已发布，但 {RELEASE_JSON} 与本次内容不一致（发布不可变）；换了内容就用新的发布号")
    staging = f"{STAGING}/{release}.{uuid.uuid4().hex[:12]}"
    await anchor.mkdir(staging, stage="prepare")
    try:
        uploaded = await _upload_tree(anchor, root, staging)
        await anchor.upload(f"{staging}/{RELEASE_JSON}", blob, mode=0o644, uid=0, gid=0, stage="prepare")
        await anchor.mkdir(RELEASES, stage="prepare")
        await anchor.rename(staging, target, stage="prepare")
    except BaseException:
        await _discard(anchor, staging)
        raise
    # 改名只保证"命令没报错"：`mv -n` 在目标已存在时静默成功，且并发下可能是别人先落地。
    # 所以核对落到正式位置的就是本次的内容，且本次 staging 已经不在了。
    if await anchor.exists(staging, stage="prepare"):
        await _discard(anchor, staging)
        raise ReleaseError(f"{target} 已存在，本次发布未生效（并发发布？）；换了内容就用新的发布号")
    landed = hashlib.sha256(await anchor.download(f"{target}/{RELEASE_JSON}", stage="prepare")).hexdigest()
    if landed != digest:
        raise ReleaseError(f"{target} 里的 {RELEASE_JSON} 与本次发布的内容不一致（并发发布？）")
    return ReleaseResult(release, target, digest, uploaded, False)


async def _upload_tree(anchor: Anchor, root: Path, staging: str) -> int:
    """上传目录树：先建各级目录，再传文件。`upload` 要求父目录已存在。"""
    uploaded = 0
    created: set[str] = set()
    for rel, info in release_entries(root):
        parent = posixpath.dirname(rel)
        if parent and parent not in created:
            await anchor.mkdir(f"{staging}/{parent}", stage="prepare")
            created.add(parent)
        key = f"{staging}/{rel}"
        await anchor.upload(
            key,
            (root / rel).read_bytes(),
            mode=stat.S_IMODE(info.st_mode),
            uid=info.st_uid,
            gid=info.st_gid,
            stage="prepare",
        )
        uploaded += 1
    return uploaded


async def _discard(anchor: Anchor, staging: str) -> None:
    """清理 staging；清理失败不覆盖原始错误（残留列在 `share gc` 的输出里）。"""
    with contextlib.suppress(Exception):
        await anchor.remove(staging, stage="run")


async def verify_release(anchor: Anchor, release: str, *, expected_sha256: str) -> VerifyReport:
    """全量校验一次发布（§3.3）—— 先核清单自身的摘要，再逐文件核大小、内容与 mode。

    清单摘要与部署配置钉的值不一致时，其余项不再有意义，直接返回。
    """
    target = f"{RELEASES}/{release}"
    manifest_key = f"{target}/{RELEASE_JSON}"
    if not await anchor.exists(manifest_key, stage="prepare"):
        raise ReleaseError(f"{manifest_key} 不存在")
    blob = await anchor.download(manifest_key, stage="prepare")
    actual = hashlib.sha256(blob).hexdigest()
    if actual != expected_sha256:
        return VerifyReport(release, False, actual, expected_sha256, 0)
    document = json.loads(blob)
    files: dict[str, dict[str, object]] = document.get("files") or {}
    missing, size_bad, digest_bad, mode_bad = [], [], [], []
    for rel in sorted(files):
        entry = files[rel]
        key = f"{target}/{rel}"
        if not await anchor.exists(key, stage="prepare"):
            missing.append(rel)
            continue
        info = await anchor.stat(key, stage="prepare")
        if info.size != entry.get("size"):
            size_bad.append(rel)
        else:
            data = await anchor.download(key, stage="prepare")
            if hashlib.sha256(data).hexdigest() != entry.get("sha256"):
                digest_bad.append(rel)
        if f"{info.mode:04o}" != entry.get("mode"):
            mode_bad.append(rel)
    return VerifyReport(
        release,
        True,
        actual,
        expected_sha256,
        len(files),
        tuple(missing),
        tuple(size_bad),
        tuple(digest_bad),
        tuple(mode_bad),
    )


async def gc_releases(anchor: Anchor, *, keep: Set[str], dry_run: bool) -> GcReleasesReport:
    """删除 `keep` 之外的旧发布（§3.3、§16.3）。

    `keep` 必须含部署配置钉住的发布号。这是"保留配置与 `--keep` 列出的"，**不等于**检查全部运行中引用 ——
    别的实验钉着某个旧发布号时，不看这份 `keep` 就会被删掉。

    **不回收 `share/staging`**：无法与另一个正在组装发布的进程区分（它随时可能被改名进正式位置），
    删掉会破坏那次发布，所以只把残留列出来。
    """
    present = sorted(await anchor.listdir(RELEASES, stage="run")) if await anchor.exists(RELEASES, stage="run") else []
    removed = [name for name in present if name not in keep]
    if not dry_run:
        for name in removed:
            await anchor.remove(f"{RELEASES}/{name}", stage="run")
    staging = sorted(await anchor.listdir(STAGING, stage="run")) if await anchor.exists(STAGING, stage="run") else []
    return GcReleasesReport(
        kept=frozenset(present) - frozenset(removed),
        removed=frozenset() if dry_run else frozenset(removed),
        staging=tuple(staging),
        dry_run=dry_run,
    )


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        while chunk := handle.read(1 << 20):
            digest.update(chunk)
    return digest.hexdigest()
