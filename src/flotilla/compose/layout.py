"""卷与 bind 引用的分类（Architecture §7.1、§4.4）。纯函数，build 与 scan 共用。

**named volume** 按挂载引用（单元、挂载点、读写模式）分类，不按引用它的单元数（§7.1）：

| 引用 | 做法 |
|---|---|
| 全部只读 | 任务文件 `_volumes/<v>/`，只读，所有 trial 共用 |
| 恰好 1 个、读写 | 单元本地磁盘（不挂载），随 sandbox 删除 |
| 其余（≥ 2 个且至少一个读写） | 本 trial 的共享卷目录，每个引用按自己的读写模式挂载 |

**bind** 按源组分类：规范化后源路径相同、或一个是另一个子路径的引用构成一个源组（§4.4）：

| 源组 | 做法 |
|---|---|
| 没有写者 | 每个引用只读挂载任务文件 `binds/<n>`（子路径时指向对应子路径），所有 trial 共用 |
| 有写者、只有这一个引用 | 构建时写入镜像目标路径（不挂载） |
| 有写者、多个引用 | 本 trial 复制 `binds/<n>`，每个引用按自己的读写模式挂载 |
"""

from __future__ import annotations

import posixpath
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Literal

from flotilla.compose.model import Service


@dataclass(frozen=True)
class MountPlan:
    """一个单元的一个共享存储挂载。`scope` 与 `key` 的含义同 `flotilla.core.plan.Mount`。"""

    unit: str
    scope: Literal["task", "trial"]
    key: str
    target: str
    read_only: bool


@dataclass(frozen=True)
class VolumePlan:
    """一个 named volume 的处理方式。`seed_from`：第一个在挂载点有内容的引用服务（构建时确定，§7.3）。"""

    name: str
    mode: Literal["task_ro", "local", "trial"]
    references: tuple[tuple[str, str, bool], ...]  # (单元, 挂载点, 只读)
    nocopy: bool = False


@dataclass(frozen=True)
class BindGroup:
    """一个 bind 源组：`root` 是组内最短的源（其余引用是它或它的子路径），`index` 是任务文件 `binds/<index>`。"""

    index: int
    root: str
    mode: Literal["task_ro", "image", "trial"]
    references: tuple[tuple[str, str, str, bool], ...]  # (单元, 源, 挂载点, 只读)


@dataclass(frozen=True)
class Layout:
    volumes: tuple[VolumePlan, ...]
    binds: tuple[BindGroup, ...]
    mounts: Mapping[str, tuple[MountPlan, ...]]  # 单元 → 按 Compose 顺序的挂载（嵌套挂载外层先挂，§4.4）
    trial_dirs: tuple[str, ...]  # 本 trial 需要准备的目录（相对 trial 目录）


def classify(services: Mapping[str, Service]) -> Layout:
    vol_refs: dict[str, list[tuple[str, str, bool, bool]]] = {}  # 卷 → (单元, 挂载点, 只读, nocopy)
    bind_refs: list[tuple[str, str, str, bool]] = []  # (单元, 规范化的源, 挂载点, 只读)
    order: list[tuple[str, str, str]] = []  # (单元, kind, 挂载点)：保持 Compose 顺序
    for name in services:
        for m in services[name].volumes:
            if m.kind == "volume":
                vol_refs.setdefault(m.source, []).append((name, m.target, m.read_only, m.nocopy))
            else:
                bind_refs.append((name, _norm_source(m.source), m.target, m.read_only))
            order.append((name, m.kind, m.target))

    volumes = tuple(_volume_plan(v, refs) for v, refs in sorted(vol_refs.items()))
    binds = _bind_groups(bind_refs)

    by_target: dict[tuple[str, str], MountPlan] = {}
    trial_dirs: list[str] = []
    for vp in volumes:
        if vp.mode == "trial":
            trial_dirs.append(vp.name)
        for unit, target, ro in vp.references:
            if vp.mode == "task_ro":
                by_target[(unit, target)] = MountPlan(unit, "task", f"_volumes/{vp.name}", target, True)
            elif vp.mode == "trial":
                by_target[(unit, target)] = MountPlan(unit, "trial", vp.name, target, ro)
    for g in binds:
        if g.mode == "trial":
            trial_dirs.append(f"binds/{g.index}")
        for unit, source, target, ro in g.references:
            sub = source[len(g.root) :].lstrip("/")
            base = f"binds/{g.index}"
            key = f"{base}/{sub}" if sub else base
            if g.mode == "task_ro":
                by_target[(unit, target)] = MountPlan(unit, "task", key, target, True)
            elif g.mode == "trial":
                by_target[(unit, target)] = MountPlan(unit, "trial", key, target, ro)

    mounts: dict[str, list[MountPlan]] = {}
    for unit, _, target in order:
        plan = by_target.get((unit, target))
        if plan is not None:
            mounts.setdefault(unit, []).append(plan)
    return Layout(
        volumes=volumes,
        binds=binds,
        mounts={u: tuple(ms) for u, ms in mounts.items()},
        trial_dirs=tuple(trial_dirs),
    )


def _volume_plan(name: str, refs: list[tuple[str, str, bool, bool]]) -> VolumePlan:
    references = tuple((u, t, ro) for u, t, ro, _ in refs)
    nocopy = all(nc for *_, nc in refs)
    if all(ro for _, _, ro in references):
        mode: Literal["task_ro", "local", "trial"] = "task_ro"
    elif len(references) == 1:
        mode = "local"
    else:
        mode = "trial"
    return VolumePlan(name=name, mode=mode, references=references, nocopy=nocopy)


def _bind_groups(refs: list[tuple[str, str, str, bool]]) -> tuple[BindGroup, ...]:
    """按源的包含关系分组：源相同或一个是另一个的子路径即同组（传递闭包）。组按根源排序编号。"""
    sources = sorted({src for _, src, _, _ in refs}, key=lambda s: (s.count("/"), s))
    roots: list[str] = []
    for src in sources:
        if not any(_within(src, r) for r in roots):
            roots.append(src)
    roots.sort()
    groups: list[BindGroup] = []
    for i, root in enumerate(roots):
        members = tuple(r for r in refs if _within(r[1], root))
        writers = [r for r in members if not r[3]]
        if not writers:
            mode: Literal["task_ro", "image", "trial"] = "task_ro"
        elif len(members) == 1:
            mode = "image"
        else:
            mode = "trial"
        groups.append(BindGroup(index=i, root=root, mode=mode, references=members))
    return tuple(groups)


def _within(path: str, root: str) -> bool:
    return path == root or path.startswith(root + "/")


def _norm_source(source: str) -> str:
    """bind 源相对 environment 目录规范化：去掉 `./`、合并 `a/./b`，结果不含 `..`（normalize 已拒绝越界）。"""
    norm = posixpath.normpath(source)
    return "" if norm == "." else norm
