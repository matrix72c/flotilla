"""残留清理 gc：按锚点存活判定、两次观察（Architecture §5.6）。

一个 launch 的资源能否删除只看它的进程是否还活着（有没有锚点），不看实例的期限。一轮 gc：

1. 列出所有 `flotilla/role=anchor` 的实例，得到存活的 launch 集合；
2. 经本进程的锚点列出 `launches/` 下的目录，不在存活集合中的 launch 记为候选（有实例的 launch 一定有目录）；
3. 候选须在相隔至少 `W` 的两次观察中都没有锚点才删除——第一次只登记；
4. 按 `flotilla/launch=<id>` 列出候选的实例并删尽，再列出为空后，经锚点删除 `launches/<id>` 目录。目录是 gc
   找到该 launch 的唯一入口，所以总是最后删除；实例没删尽时目录保留，下一轮再来。

安全条件 `G + list_visibility_s < W`（§5.6）由配置校验保证；本模块只按 `W` 计时。停用的 launch 永不复用 ID，
所以候选一旦满 `W` 就不会再"复活"，删除期间不需要复查锚点。
"""

from __future__ import annotations

import logging
from dataclasses import dataclass

from flotilla.core import labels
from flotilla.core.anchor import Anchor
from flotilla.core.deletion import Deleter
from flotilla.core.volumes import LAUNCHES, launch_key
from flotilla.platform.base import Clock, ErrorCategory, FlotillaError, Platform

log = logging.getLogger(__name__)


@dataclass(frozen=True)
class GcReport:
    """一轮 gc 的结果，供日志与命令行输出。"""

    alive: frozenset[str]  # 有锚点的 launch
    candidates: frozenset[str]  # 本轮新登记、尚未满 W 的候选
    removed: frozenset[str]  # 实例删尽、目录已删除
    pending: frozenset[str]  # 满 W 但没能删尽，下一轮再来


class Gc:
    """一个进程的 gc 状态：候选及其首次观察时刻。provider 每 `gc_interval` 调用一次 `run_once`。

    `anchor` 是本进程的锚点：执行目录操作，它的 launch 永远不是候选。`window_s` 即 `W`。
    """

    def __init__(self, platform: Platform, clock: Clock, anchor: Anchor, deleter: Deleter, *, window_s: float) -> None:
        if window_s <= 0:
            raise ValueError("window_s 须 > 0")
        self._platform = platform
        self._clock = clock
        self._anchor = anchor
        self._deleter = deleter
        self._window_s = window_s
        self._first_seen: dict[str, float] = {}

    async def run_once(self) -> GcReport:
        """执行一轮 gc（见模块说明）。列出锚点或目录失败时抛出、整轮放弃——不能把"列不出"当作"没有锚点"。"""
        observed = self._clock.monotonic()
        alive = await self._alive_launches()
        dirs = set(await self._anchor.listdir(LAUNCHES, stage="run"))
        orphans = dirs - alive - {self._anchor.launch_id}

        # 复活（锚点出现）或目录已消失的候选不再跟踪；新候选登记首次观察时刻。
        for launch in list(self._first_seen):
            if launch not in orphans:
                del self._first_seen[launch]
        for launch in orphans:
            self._first_seen.setdefault(launch, observed)

        due = {launch for launch in orphans if observed - self._first_seen[launch] >= self._window_s}
        removed: set[str] = set()
        for launch in sorted(due):
            if await self._remove_launch(launch):
                removed.add(launch)
                del self._first_seen[launch]
        report = GcReport(
            alive=frozenset(alive),
            candidates=frozenset(orphans - due),
            removed=frozenset(removed),
            pending=frozenset(due - removed),
        )
        if report.removed or report.pending:
            log.info("gc：删除 %s，未删尽 %s", sorted(report.removed), sorted(report.pending))
        return report

    async def collect(self, launch: str) -> bool:
        """命令行 `flotilla gc --launch <id>`：只处理指定的 launch，跳过两次观察的等待（§5.6 末段）。

        由操作者确认该 launch 的进程已经退出；它的锚点仍被列出时拒绝（锚点在进程退出后至多一个锚点 TTL 内过期）。
        没有两次观察撑腰，进程退出前发出的创建可能还没出现在列表中：按标签反复列出删除，直到 `list_visibility_s`
        之后列出为空。返回是否删尽。
        """
        if launch == self._anchor.launch_id:
            raise ValueError("不能清理本进程自己的 launch")
        if launch in await self._alive_launches():
            raise FlotillaError(
                f"launch {launch} 的锚点仍存在，拒绝清理（进程可能还活着；锚点过期后再试）",
                stage="run",
                category=ErrorCategory.INVALID,
                retryable=False,
            )
        return await self._remove_launch(launch, until=self._clock.monotonic() + self._platform.caps.list_visibility_s)

    async def _alive_launches(self) -> set[str]:
        anchors = await self._platform.list({labels.ROLE: labels.ROLE_ANCHOR})
        return {st.labels[labels.LAUNCH] for st in anchors if labels.LAUNCH in st.labels}

    async def _remove_launch(self, launch: str, *, until: float | None = None) -> bool:
        """删尽该 launch 的实例（`until` 之后列出为空），再删它的目录。返回是否全部完成。

        `until` 缺省为现在：第二次观察已在 t0 + W 之后，该 launch 的创建都已出现在列表中或永远不会出现（§5.6），
        一次列出为空即删尽。
        """
        try:
            scope = self._deleter.scope()
            deadline = self._clock.monotonic() if until is None else until
            if not await scope.sweep({labels.LAUNCH: launch}, until=deadline):
                log.warning("gc：launch %s 的实例未能删尽，保留目录", launch)
                return False
            await self._anchor.remove(launch_key(launch), stage="run")
        except FlotillaError as exc:
            log.warning("gc：清理 launch %s 失败，下一轮重试：%s", launch, exc)
            return False
        return True
