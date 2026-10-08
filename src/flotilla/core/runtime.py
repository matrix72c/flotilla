"""就绪之后的运行期：存活确认、TTL 续期、`lost` 标记与 `check()`（Architecture §5.6"平台 TTL"、§5.7、§3.6）。

`Monitor` 管一个已就绪的 trial，`Trial.start()` 返回后 `start()`，trial 释放前 `stop()`。

**标记 `lost`** 的信号（§5.7）：某个单元不存在或状态终止（每单元每 `probe_interval_s` 一次 `get`，按单元错开）；
执行调用返回 `not_found` 时由客户端调用 `confirm(unit)` 立即确认；续期失败；本 launch 停用（锚点的 `deactivated`）。
`lost` 只记第一个原因，之后不变；`lost` 属性返回该错误（`stage="run"`、`category=lost`、可重试），客户端据此让
之后的调用立即失败。

**不算**基础设施故障：业务进程退出、崩溃——运行期不轮询进程。带 restart 策略的服务退出由 `check()` 报为
`service`（§3.6），不是 `lost`。

**`check()`** 做一次有时限的确认，超时或状态未知按 `lost`（未知不等于健康）。运行期不调用 `list`（§5.7）。
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime, timedelta

from flotilla.core import process
from flotilla.core.anchor import Anchor
from flotilla.core.plan import TrialPlan
from flotilla.core.waiting import Backoff, Expired, describe, run_all, within
from flotilla.platform.base import (
    Clock,
    ErrorCategory,
    FlotillaError,
    InstanceHandle,
    InstanceStatus,
    Platform,
)

log = logging.getLogger(__name__)


@dataclass(frozen=True)
class MonitorSettings:
    probe_interval_s: float = 300.0  # 每单元一次 get（§5.7）
    renew_before_s: float = 1800.0  # 剩余 TTL 不足它时续期（§5.6：剩余 30 分钟）
    ttl_s: int = 6 * 3600  # 续期时把过期时间设为 now + ttl_s（不超过部署的 max_ttl_seconds）
    check_timeout_s: float = 10.0  # check() 的时限（§5.7）
    renew_margin_s: float = 120.0  # 续期失败时按退避重试，直到剩余 TTL 不足它才判 lost

    def __post_init__(self) -> None:
        if self.probe_interval_s <= 0 or self.check_timeout_s <= 0:
            raise ValueError("probe_interval_s 与 check_timeout_s 须 > 0")
        if not 0 < self.renew_margin_s < self.renew_before_s < self.ttl_s:
            raise ValueError("须满足 0 < renew_margin_s < renew_before_s < ttl_s")


class Monitor:
    """一个就绪 trial 的运行期监视（见模块说明）。"""

    def __init__(
        self,
        platform: Platform,
        clock: Clock,
        anchor: Anchor,
        plan: TrialPlan,
        handles: Mapping[str, InstanceHandle],
        pids: Mapping[str, str],
        settings: MonitorSettings,
    ) -> None:
        self._platform = platform
        self._clock = clock
        self._anchor = anchor
        self._plan = plan
        self._handles = dict(handles)
        self._pids = dict(pids)
        self._settings = settings
        max_ttl = platform.caps.max_ttl_seconds
        self._ttl = min(settings.ttl_s, max_ttl) if max_ttl is not None else settings.ttl_s
        # 部署的 TTL 上限可能小于 renew_before_s：续期提前量不超过 TTL 的一半，否则续完立刻又到期、空转。
        self._renew_before = min(settings.renew_before_s, self._ttl / 2)
        self._renew_margin = min(settings.renew_margin_s, self._renew_before / 2)
        self._lost: FlotillaError | None = None
        self._tasks: list[asyncio.Task[None]] = []

    @property
    def lost(self) -> FlotillaError | None:
        """trial 已标记 `lost` 时的错误；否则 None。"""
        return self._lost

    # ───────────────────────────── 生命周期 ─────────────────────────────

    def start(self) -> None:
        if self._tasks:
            raise RuntimeError("monitor 只能 start 一次")
        units = sorted(self._handles)
        step = self._settings.probe_interval_s / max(1, len(units))
        for i, unit in enumerate(units):
            # 错开：第 i 个单元的第一次查询在 (i + 1) × step 之后，避免 trial 内的单元同时查询。
            self._tasks.append(asyncio.create_task(self._probe_loop(unit, offset=(i + 1) * step)))
        self._tasks.append(asyncio.create_task(self._renew_loop()))
        self._tasks.append(asyncio.create_task(self._watch_launch()))

    async def stop(self) -> None:
        for task in self._tasks:
            task.cancel()
        if self._tasks:
            await asyncio.wait(self._tasks)

    # ───────────────────────────── lost ─────────────────────────────

    def mark_lost(self, why: str, *, cause: BaseException | None = None) -> FlotillaError:
        """标记 `lost`（只记第一次），返回 `lost` 错误。"""
        if self._lost is None:
            log.error("trial 标记为 lost：%s", why)
            err = FlotillaError(why, stage="run", category=ErrorCategory.LOST, retryable=True)
            if cause is not None:
                err.__cause__ = cause
            self._lost = err
        return self._lost

    async def confirm(self, unit: str) -> FlotillaError | None:
        """执行调用返回 `not_found` 时立即确认该单元（§5.7）：实例不在或终止即标记 `lost`。返回 `lost` 错误或 None。"""
        if self._lost is not None:
            return self._lost
        await self._probe(unit)
        return self._lost

    # ───────────────────────────── check()（§5.7）─────────────────────────────

    async def check(self) -> BaseException | None:
        """有时限的健康确认；不抛异常。健康返回 None，否则返回带 stage / category / retryable 的错误。"""
        if self._lost is not None:
            return self._lost
        try:
            result = await within(self._clock, self._settings.check_timeout_s, self._check())
        except Expired:
            return self.mark_lost(f"check() 在 {self._settings.check_timeout_s}s 内未能确认健康")
        except FlotillaError as exc:  # 查询本身失败：无法确认健康，按 lost（service 错误由 _check 作为返回值给出）
            return self.mark_lost(f"check() 失败：{exc}", cause=exc)
        return result

    async def _check(self) -> FlotillaError | None:
        if self._anchor.deactivated.is_set():
            return self.mark_lost(f"launch {self._anchor.launch_id} 已停用")
        units = sorted(self._handles)
        await run_all(self._check_unit(u) for u in units)
        if self._lost is not None:
            return self._lost
        for unit in units:
            if (err := await self._check_restart(unit)) is not None:
                return err
        return None

    async def _check_unit(self, unit: str) -> None:
        handle = self._handles[unit]
        state = await self._platform.get(handle.iid)
        if state.status is not InstanceStatus.RUNNING:
            self.mark_lost(f"单元 {unit} 状态不是 running：{describe(state)}")
            return
        result = await self._platform.exec(handle, process.own_spec((process.BUSYBOX, "true"), timeout_s=5.0))
        if result.exit_code != 0 or result.timed_out:
            self.mark_lost(f"单元 {unit} 的执行通道不可用（空命令 {'超时' if result.timed_out else result.exit_code}）")
            return
        pid = self._pids.get(unit)
        if pid is not None:
            status = await self._platform.process_status(handle, pid)
            if not status.found:
                self.mark_lost(f"单元 {unit} 的业务进程 {pid} 查询不到（执行守护进程可能已重启）")

    async def _check_restart(self, unit: str) -> FlotillaError | None:
        """restart 策略不是 `no` 的服务若已退出、且 Docker 会重启它，样本作废（§3.6）。"""
        policy = self._plan.units[unit].restart
        pid = self._pids.get(unit)
        if policy == "no" or pid is None:
            return None
        status = await self._platform.process_status(self._handles[unit], pid)
        if status.running:
            return None
        code = status.exit_code
        if policy in ("always", "unless-stopped") or (policy == "on-failure" and code != 0):
            return FlotillaError(
                f"{unit}：带 restart={policy} 的服务已退出（退出码 {code}），Docker 会重启它；"
                "flotilla 不在运行中重启，样本作废",
                stage="run",
                category=ErrorCategory.SERVICE,
                retryable=False,
                service=unit,
            )
        return None

    # ───────────────────────────── 后台循环 ─────────────────────────────

    async def _probe_loop(self, unit: str, *, offset: float) -> None:
        await self._clock.sleep(offset)
        while self._lost is None:
            await self._probe(unit)
            await self._clock.sleep(self._settings.probe_interval_s)

    async def _probe(self, unit: str) -> None:
        """一次 `get`：实例不在或不是 `RUNNING` 即 `lost`（`UNKNOWN` 运行期视同终止，§2.3）；查询本身的瞬时错误不算。"""
        try:
            state = await self._platform.get(self._handles[unit].iid)
        except FlotillaError as exc:
            if exc.category is ErrorCategory.NOT_FOUND:
                self.mark_lost(f"单元 {unit} 的实例已不存在", cause=exc)
            else:
                log.warning("查询单元 %s 失败，下一次再查：%s", unit, exc)
            return
        if state.status is not InstanceStatus.RUNNING:
            self.mark_lost(f"单元 {unit} 状态为 {describe(state)}")

    async def _renew_loop(self) -> None:
        """剩余 TTL 不足 `renew_before_s` 的单元续期到 now + ttl（§5.6）。失败按退避重试；剩余不足
        `renew_margin_s` 仍未成功即 `lost`——实例即将被平台回收。"""
        expires = await self._read_expiry()
        while self._lost is None:
            soonest = min(expires.values())
            await self._clock.sleep(max(0.0, _left(soonest, self._clock.now()) - self._renew_before))
            for unit in sorted(u for u, at in expires.items() if _left(at, self._clock.now()) <= self._renew_before):
                renewed = await self._renew(unit, expires[unit])
                if renewed is None:
                    return
                expires[unit] = renewed

    async def _read_expiry(self) -> dict[str, datetime]:
        """各单元当前的过期时间。读不到（或平台没给）时按"现在"处理：立即续一次，早续无害，晚续会丢实例。"""
        out: dict[str, datetime] = {}
        for unit in sorted(self._handles):
            try:
                state = await self._platform.get(self._handles[unit].iid)
            except FlotillaError as exc:
                log.warning("读取单元 %s 的过期时间失败，立即续期：%s", unit, exc)
                state = None
            out[unit] = state.expires_at if state is not None and state.expires_at is not None else self._clock.now()
        return out

    async def _renew(self, unit: str, current: datetime) -> datetime | None:
        backoff = Backoff(5.0, cap=60.0)
        while True:
            new = self._clock.now() + timedelta(seconds=self._ttl)
            try:
                await self._platform.renew(self._handles[unit].iid, new)
                return new
            except FlotillaError as exc:
                left = _left(current, self._clock.now())
                if exc.category is ErrorCategory.NOT_FOUND or left <= self._renew_margin:
                    self.mark_lost(f"单元 {unit} 续期失败（剩余 {left:.0f}s）", cause=exc)
                    return None
                log.warning("单元 %s 续期失败，重试：%s", unit, exc)
            await self._clock.sleep(min(backoff.next(), max(0.0, left - self._renew_margin)))

    async def _watch_launch(self) -> None:
        await self._anchor.deactivated.wait()
        self.mark_lost(f"launch {self._anchor.launch_id} 已停用")


def _left(expires_at: datetime, now: datetime) -> float:
    return (expires_at - now).total_seconds()
