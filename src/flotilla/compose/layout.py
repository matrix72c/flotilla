"""Plan attempt-owned mounts without baking file-backed storage into images."""

from __future__ import annotations

import posixpath
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Literal

from flotilla.compose.model import Service


@dataclass(frozen=True)
class MountPlan:
    """One real mount; trial scope means the current attempt owns its backing storage."""

    scope: Literal["trial"]
    key: str
    target: str
    read_only: bool


@dataclass(frozen=True)
class VolumePlan:
    """Only references without nocopy may provide initial content."""

    name: str
    mode: Literal["trial"]
    references: tuple[tuple[str, str, bool, bool], ...]

    @property
    def nocopy(self) -> bool:
        return all(nocopy for *_, nocopy in self.references)


@dataclass(frozen=True)
class BindGroup:
    """Equal sources and ancestor/subpath sources retain one backing object."""

    index: int
    root: str
    mode: Literal["trial"]
    references: tuple[tuple[str, str, str, bool], ...]


@dataclass(frozen=True)
class Layout:
    volumes: tuple[VolumePlan, ...]
    binds: tuple[BindGroup, ...]
    mounts: Mapping[str, tuple[MountPlan, ...]]


def classify(services: Mapping[str, Service]) -> Layout:
    vol_refs: dict[str, list[tuple[str, str, bool, bool]]] = {}
    bind_refs: list[tuple[str, str, str, bool]] = []
    order: list[tuple[str, str]] = []
    for name, service in services.items():
        for mount in service.volumes:
            if mount.kind == "volume":
                vol_refs.setdefault(mount.source, []).append((name, mount.target, mount.read_only, mount.nocopy))
            else:
                bind_refs.append((name, posixpath.normpath(mount.source), mount.target, mount.read_only))
            order.append((name, mount.target))

    volumes = tuple(VolumePlan(name, "trial", tuple(refs)) for name, refs in sorted(vol_refs.items()))
    sources = sorted({src for _, src, _, _ in bind_refs}, key=lambda s: (s.count("/"), s))
    roots: list[str] = []
    for source in sources:
        if not any(_within(source, root) for root in roots):
            roots.append(source)
    binds = tuple(
        BindGroup(index, root, "trial", tuple(ref for ref in bind_refs if _within(ref[1], root)))
        for index, root in enumerate(sorted(roots))
    )
    by_target: dict[tuple[str, str], MountPlan] = {}
    for volume in volumes:
        for unit, target, read_only, _ in volume.references:
            by_target[(unit, target)] = MountPlan("trial", volume.name, target, read_only)
    for group in binds:
        for unit, source, target, read_only in group.references:
            subpath = posixpath.relpath(source, group.root)
            key = f"binds/{group.index}" + (f"/{subpath}" if subpath != "." else "")
            by_target[(unit, target)] = MountPlan("trial", key, target, read_only)
    mounts: dict[str, list[MountPlan]] = {}
    for unit, target in sorted(order, key=lambda item: item[1].count("/")):
        mounts.setdefault(unit, []).append(by_target[(unit, target)])
    return Layout(volumes, binds, {unit: tuple(items) for unit, items in mounts.items()})


def _within(path: str, root: str) -> bool:
    return root == "." or path == root or path.startswith(root + "/")
