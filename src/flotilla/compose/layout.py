"""Decide which mounts the attempt owns and which are read straight from the task files.

A bind source no service writes is the same bytes for every trial, so it is mounted read-only
from the published task directory: `scope="task"`. Copying it per trial would move bytes to
produce the same bytes under a different name. Everything else -- a named volume, or a bind group
with a writer -- is this trial's own storage, created and populated under its own directory.
"""

from __future__ import annotations

import posixpath
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Literal

from flotilla.compose.model import Service


@dataclass(frozen=True)
class MountPlan:
    """One real mount; trial scope means the current attempt owns its backing storage."""

    scope: Literal["task", "trial"]
    key: str
    target: str
    read_only: bool


@dataclass(frozen=True)
class VolumePlan:
    """A named volume and the (unit, target, read_only) references that mount it."""

    name: str
    references: tuple[tuple[str, str, bool], ...]


@dataclass(frozen=True)
class BindGroup:
    """Equal sources and ancestor/subpath sources retain one backing object.

    `mode="task"` means every reference is read-only, so the group is mounted where it is
    published; `mode="trial"` means something writes it, so the trial gets its own copy.
    """

    index: int
    root: str
    mode: Literal["task", "trial"]
    references: tuple[tuple[str, str, str, bool], ...]


@dataclass(frozen=True)
class Layout:
    volumes: tuple[VolumePlan, ...]
    binds: tuple[BindGroup, ...]
    mounts: Mapping[str, tuple[MountPlan, ...]]


def classify(services: Mapping[str, Service]) -> Layout:
    vol_refs: dict[str, list[tuple[str, str, bool]]] = {}
    bind_refs: list[tuple[str, str, str, bool]] = []
    order: list[tuple[str, str]] = []
    for name, service in services.items():
        for mount in service.volumes:
            if mount.kind == "volume":
                vol_refs.setdefault(mount.source, []).append((name, mount.target, mount.read_only))
            else:
                bind_refs.append((name, posixpath.normpath(mount.source), mount.target, mount.read_only))
            order.append((name, mount.target))

    volumes = tuple(VolumePlan(name, tuple(refs)) for name, refs in sorted(vol_refs.items()))
    sources = sorted({src for _, src, _, _ in bind_refs}, key=lambda s: (s.count("/"), s))
    roots: list[str] = []
    for source in sources:
        if not any(_within(source, root) for root in roots):
            roots.append(source)
    binds = []
    for index, root in enumerate(sorted(roots)):
        references = tuple(ref for ref in bind_refs if _within(ref[1], root))
        # One writable reference makes the whole group the trial's, because they share storage.
        mode: Literal["task", "trial"] = "task" if all(ref[3] for ref in references) else "trial"
        binds.append(BindGroup(index, root, mode, references))
    by_target: dict[tuple[str, str], MountPlan] = {}
    for volume in volumes:
        for unit, target, read_only in volume.references:
            by_target[(unit, target)] = MountPlan("trial", volume.name, target, read_only)
    for group in binds:
        for unit, source, target, read_only in group.references:
            if group.mode == "task":
                # The published path itself, relative to the task files directory.
                by_target[(unit, target)] = MountPlan("task", source.removeprefix("./"), target, read_only)
                continue
            subpath = posixpath.relpath(source, group.root)
            key = f"binds/{group.index}" + (f"/{subpath}" if subpath != "." else "")
            by_target[(unit, target)] = MountPlan("trial", key, target, read_only)
    mounts: dict[str, list[MountPlan]] = {}
    for unit, target in sorted(order, key=lambda item: item[1].count("/")):
        mounts.setdefault(unit, []).append(by_target[(unit, target)])
    return Layout(volumes, tuple(binds), {unit: tuple(items) for unit, items in mounts.items()})


def _within(path: str, root: str) -> bool:
    return root == "." or path == root or path.startswith(root + "/")
