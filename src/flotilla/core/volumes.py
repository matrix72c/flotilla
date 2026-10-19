"""Confine mounts to immutable publications or launch-owned directories."""

from __future__ import annotations

import posixpath

from flotilla.core.plan import Mount, TrialPlan
from flotilla.platform.base import SharedVolume

LAUNCHES = "launches"
SHARE_MOUNT = "/.flotilla"


def launch_key(launch_id: str) -> str:
    return f"{LAUNCHES}/{launch_id}"


def trial_key(launch_id: str, trial_id: str) -> str:
    return f"{LAUNCHES}/{launch_id}/{trial_id}"


def share_key(release: str) -> str:
    return f"share/releases/{release}"


def unit_volumes(
    plan: TrialPlan,
    unit: str,
    *,
    share_release: str,
    launch_id: str,
    trial_id: str,
) -> tuple[SharedVolume, ...]:
    """All shared volumes of a unit: share first, then manifest order (outer nested mounts first)."""
    out = [SharedVolume(key=share_key(share_release), mount_path=SHARE_MOUNT, read_only=True)]
    for m in plan.units[unit].mounts:
        out.append(SharedVolume(key=_resolve(plan, m, launch_id, trial_id), mount_path=m.target, read_only=m.read_only))
    return tuple(out)


def _resolve(plan: TrialPlan, mount: Mount, launch_id: str, trial_id: str) -> str:
    if mount.scope == "task":
        assert plan.task_files is not None  # checked by validate()
        return _join(plan.task_files, mount.key)
    return _join(trial_key(launch_id, trial_id), mount.key)


def _join(base: str, key: str) -> str:
    parts = key.split("/") if key else []
    if any(p in ("", ".", "..") for p in parts):
        raise ValueError(f"invalid volume key: {key!r}")
    return posixpath.join(base, *parts)
