"""Classify shared-volume and bind-source lifetimes."""

from __future__ import annotations

import posixpath
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Literal

from flotilla.compose.model import Service


@dataclass(frozen=True)
class MountPlan:
    """One shared-storage mount of a unit. `scope` and `key` mean the same as in `flotilla.core.plan.Mount`."""

    scope: Literal["task", "trial"]
    key: str
    target: str
    read_only: bool


@dataclass(frozen=True)
class VolumePlan:
    """How a named volume is handled; only references without nocopy may provide initial content."""

    name: str
    mode: Literal["task_ro", "local", "trial"]
    references: tuple[tuple[str, str, bool, bool], ...]  # (unit, mount point, read-only, nocopy)

    @property
    def nocopy(self) -> bool:
        return all(nocopy for *_, nocopy in self.references)


@dataclass(frozen=True)
class BindGroup:
    """A bind source group: `root` is the shortest source (others are it or subpaths); `index` is `binds/<index>`."""

    index: int
    root: str
    mode: Literal["task_ro", "image", "trial"]
    references: tuple[tuple[str, str, str, bool], ...]  # (unit, source, mount point, read-only)


@dataclass(frozen=True)
class Layout:
    volumes: tuple[VolumePlan, ...]
    binds: tuple[BindGroup, ...]
    mounts: Mapping[str, tuple[MountPlan, ...]]  # unit -> mounts, outer ones first


def classify(services: Mapping[str, Service]) -> Layout:
    vol_refs: dict[str, list[tuple[str, str, bool, bool]]] = {}  # volume -> (unit, mount point, read-only, nocopy)
    bind_refs: list[tuple[str, str, str, bool]] = []  # (unit, normalized source, mount point, read-only)
    order: list[tuple[str, str]] = []  # (unit, mount point): declaration order kept at equal depth
    for name in services:
        for m in services[name].volumes:
            if m.kind == "volume":
                vol_refs.setdefault(m.source, []).append((name, m.target, m.read_only, m.nocopy))
            else:
                bind_refs.append((name, posixpath.normpath(m.source), m.target, m.read_only))
            order.append((name, m.target))

    volumes = tuple(_volume_plan(v, refs) for v, refs in sorted(vol_refs.items()))
    binds = _bind_groups(bind_refs)

    by_target: dict[tuple[str, str], MountPlan] = {}
    for vp in volumes:
        for unit, target, ro, _ in vp.references:
            if vp.mode == "task_ro":
                by_target[(unit, target)] = MountPlan("task", f"_volumes/{vp.name}", target, True)
            elif vp.mode == "trial":
                by_target[(unit, target)] = MountPlan("trial", vp.name, target, ro)
    for g in binds:
        for unit, source, target, ro in g.references:
            sub = posixpath.relpath(source, g.root)
            base = f"binds/{g.index}"
            key = base if sub == "." else f"{base}/{sub}"
            if g.mode == "task_ro":
                by_target[(unit, target)] = MountPlan("task", key, target, True)
            elif g.mode == "trial":
                by_target[(unit, target)] = MountPlan("trial", key, target, ro)

    mounts: dict[str, list[MountPlan]] = {}
    for unit, target in sorted(order, key=lambda item: item[1].count("/")):
        plan = by_target.get((unit, target))
        if plan is not None:
            mounts.setdefault(unit, []).append(plan)
    return Layout(
        volumes=volumes,
        binds=binds,
        mounts={u: tuple(ms) for u, ms in mounts.items()},
    )


def _volume_plan(name: str, refs: list[tuple[str, str, bool, bool]]) -> VolumePlan:
    if all(ro for _, _, ro, _ in refs):
        mode: Literal["task_ro", "local", "trial"] = "task_ro"
    elif len(refs) == 1:
        mode = "local"
    else:
        mode = "trial"
    return VolumePlan(name=name, mode=mode, references=tuple(refs))


def _bind_groups(refs: list[tuple[str, str, str, bool]]) -> tuple[BindGroup, ...]:
    """Group by source containment: equal sources or subpaths share a group (transitively). Numbered by root."""
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
    return root == "." or path == root or path.startswith(root + "/")
