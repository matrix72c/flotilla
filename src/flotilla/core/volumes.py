"""卷键与单元的共享卷挂载（Architecture 第 7 节、§4.4）。

卷键是相对共享根目录的路径（§7.2）。目录布局：

- `share/releases/<release>`：share 发布，每个单元只读挂到 `/.flotilla`（§3.3）；
- `tasks/<build_key>/…`：已发布的任务文件，只读、所有 trial 共用（§4.7）；
- `launches/<launch_id>`：provider `start` 时由锚点创建，进程退出或 gc 时删除（§5.5、§5.6）；
- `launches/<launch_id>/<trial_id>/…`：trial 的 `prepare` 阶段创建，trial 的实例删尽后删除（§7.4）。
"""

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
    """单元的全部共享卷：share 在前，其后按清单顺序（嵌套挂载外层先挂，§4.4）。"""
    out = [SharedVolume(key=share_key(share_release), mount_path=SHARE_MOUNT, read_only=True)]
    for m in plan.units[unit].mounts:
        out.append(SharedVolume(key=_resolve(plan, m, launch_id, trial_id), mount_path=m.target, read_only=m.read_only))
    return tuple(out)


def _resolve(plan: TrialPlan, mount: Mount, launch_id: str, trial_id: str) -> str:
    if mount.scope == "task":
        assert plan.task_files is not None  # validate() 已检查
        return _join(plan.task_files, mount.key)
    return _join(trial_key(launch_id, trial_id), mount.key)


def _join(base: str, key: str) -> str:
    parts = key.split("/") if key else []
    if any(p in ("", ".", "..") for p in parts):
        raise ValueError(f"非法的卷键：{key!r}")
    return posixpath.join(base, *parts)
