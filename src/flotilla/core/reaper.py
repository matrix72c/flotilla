"""回收器：创建登记与对账、后台删除与确认、trial 目录清理、进程退出时的收尾（Architecture §5.5）。

每个 launch 一个 `Reaper`，按 trial 记账：已知实例、进行中的创建、最晚可能出现实例的时刻、共享卷目录。

**创建**（`create`）作为 Reaper 名下的任务执行，调用方只经 `asyncio.shield` 等结果：调用方被取消时请求照常完成，
拿到的实例记入 trial。创建先排队（进程级并发与速率限制）；trial 释放或 launch 停用时，尚未发出的请求立即放弃；
launch 停用时在途的请求也取消（§5.6：`t0 + G` 之后不再有创建请求，后端在调用内的退避重试随之停止）。

结果（§2.4）：

- 返回实例 ID：记下，结果明确；
- 抛错：平台可能已接受（后端的 `capacity` / `image` 也可能来自平台接受之后的失败），一律按**结果未知**记账，
  释放时按标签扫尾。其中 `transient`、请求超时与非 `FlotillaError` 的异常还要**对账**：按标签反复列出，
  出现没记下的实例就当作这次创建的结果返回；`list_visibility_s` 已过仍没有，才向调用方报错。

C1 的可见时限从请求到达平台起算，而后端可能排队或退避重试，到达时刻只知道不晚于调用结束，所以对账与扫尾的
时限都从**调用结束**的时刻起算。

**释放**（`release`）立即返回。等该 trial 的进行中的创建全部结束后，后台删除记下的全部实例并以 `get` 确认
（`not_found`）；有过结果未知的创建时，另按标签列出、删除列出的实例，直到可见时限已过且列出为空。
实例删尽之后才经锚点删除 trial 的共享卷目录。创建都成功的 trial 释放时不调用 `list`（§12）。
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass, field

from flotilla.core import labels
from flotilla.core.anchor import Anchor
from flotilla.core.deletion import Deleter, DeleteScope
from flotilla.core.errors import at_stage
from flotilla.core.volumes import launch_key
from flotilla.core.waiting import Aborted, Backoff, RateLimiter, step_towards, wait_all, within
from flotilla.platform.base import (
    Clock,
    ErrorCategory,
    FlotillaError,
    InstanceHandle,
    InstanceSpec,
    Platform,
    Stage,
)

log = logging.getLogger(__name__)


@dataclass(frozen=True)
class ReaperSettings:
    """§16.3 `[limits]` 与 `[timeouts]` 中回收器用到的部分。"""

    create_rate: float = 5.0  # 个/秒，进程级
    create_concurrency: int = 64
    create_request_timeout_s: float = 600.0  # 单个创建请求；超过按结果未知处理
    close_timeout_s: float = 20.0

    def __post_init__(self) -> None:
        if self.create_rate <= 0 or self.create_concurrency < 1:
            raise ValueError("create_rate 须 > 0，create_concurrency 须 >= 1")


@dataclass
class _Trial:
    trial_id: str
    cleanup: DeleteScope
    released: asyncio.Event = field(default_factory=asyncio.Event)
    known: set[str] = field(default_factory=set)  # 记下的实例 ID（含已删除的）
    inflight: set[asyncio.Task[InstanceHandle]] = field(default_factory=set)
    # 有过结果未知的创建时：此后按标签列出不会再出现新实例的时刻（monotonic）；None 表示创建都结果明确。
    list_until: float | None = None
    dirs: list[str] = field(default_factory=list)  # 共享卷目录的卷键

    def labels(self, launch_id: str) -> dict[str, str]:
        return {labels.LAUNCH: launch_id, labels.TRIAL: self.trial_id}


class Reaper:
    """一个 launch 的回收器。`anchor` 须已 `start`；`close` 时由回收器删除它。`deleter` 可与 gc 共用。"""

    def __init__(
        self,
        platform: Platform,
        clock: Clock,
        anchor: Anchor,
        deleter: Deleter,
        settings: ReaperSettings,
    ) -> None:
        self._platform = platform
        self._clock = clock
        self._anchor = anchor
        self._deleter = deleter
        self._settings = settings
        self._create_slots = asyncio.Semaphore(settings.create_concurrency)
        self._create_rate = RateLimiter(clock, settings.create_rate)
        self._trials: dict[str, _Trial] = {}
        self._reaping: set[asyncio.Task[None]] = set()
        self._leaked = False  # 有实例未能确认删除
        self._closing = False

    @property
    def launch_id(self) -> str:
        return self._anchor.launch_id

    # ───────────────────────────── trial 记账 ─────────────────────────────

    def track(self, trial_id: str) -> None:
        """开始为 `trial_id` 记账。之后必须 `release`。"""
        if self._closing:
            raise RuntimeError("回收器已关闭")
        if trial_id in self._trials:
            raise ValueError(f"trial {trial_id} 已在记账中")
        self._trials[trial_id] = _Trial(trial_id, self._deleter.scope())

    def add_dir(self, trial_id: str, key: str) -> None:
        """登记 trial 的共享卷目录。在 `mkdir` **之前**登记，建了一半的目录也会被删除。"""
        self._open(trial_id, "prepare").dirs.append(key)

    async def create(self, trial_id: str, unit: str, spec: InstanceSpec) -> InstanceHandle:
        """为 trial 的 `unit` 创建一个实例（见模块说明）。`spec.labels` 须带本 launch、trial 与单元的标签。"""
        trial = self._open(trial_id, "create")
        expected = {labels.LAUNCH: self.launch_id, labels.TRIAL: trial_id, labels.UNIT: unit}
        if any(spec.labels.get(k) != v for k, v in expected.items()):
            raise ValueError(f"spec.labels 须包含 {expected}，实际为 {dict(spec.labels)}")
        task = asyncio.create_task(self._create(trial, unit, spec))
        trial.inflight.add(task)
        task.add_done_callback(_retrieve)
        task.add_done_callback(trial.inflight.discard)
        return await asyncio.shield(task)

    def discard(self, trial_id: str, iid: str) -> None:
        """不等 release，立即在后台删除 trial 的一个实例（§5.4 单元重建前删除旧实例）。"""
        trial = self._trials.get(trial_id)
        if trial is None:
            raise ValueError(f"trial {trial_id} 未在记账中")
        trial.known.add(iid)
        trial.cleanup.start(iid)

    def release(self, trial_id: str) -> None:
        """把 trial 交给回收器，立即返回，不 await 任何平台调用。重复调用无效果。"""
        trial = self._trials.get(trial_id)
        if trial is None or trial.released.is_set():
            return
        trial.released.set()  # 排队中的创建随之放弃
        task = asyncio.create_task(self._reap(trial))
        self._reaping.add(task)
        task.add_done_callback(self._reaping.discard)

    def is_tracking(self, trial_id: str) -> bool:
        """trial 是否仍在记账中（未释放，或已释放但尚未回收完）。"""
        return trial_id in self._trials

    # ───────────────────────────── 进程退出（§5.5 末条）─────────────────────────────

    async def close(self) -> bool:
        """释放全部 trial，等回收队列清空（最多 `close_timeout_s`）。

        清空且本 launch 的实例都已确认删除时，先经锚点删除 `launches/<launch_id>`，再删除锚点，返回 True；
        否则取消剩余回收、只删除锚点，剩余部分留给其他进程的 gc（§5.6），返回 False。
        """
        self._closing = True
        for trial_id in list(self._trials):
            self.release(trial_id)
        drained = await wait_all(self._clock, set(self._reaping), self._settings.close_timeout_s)
        clean = drained and not self._leaked and not self._anchor.deactivated.is_set()
        if not drained:
            # 进行中的创建也取消：平台可能已接受，留下的实例带本 launch 的标签，由 gc 清理。
            leftover = set(self._reaping) | {t for trial in self._trials.values() for t in trial.inflight}
            for task in leftover:
                task.cancel()
            await asyncio.wait(leftover)
        if clean:
            try:
                await self._anchor.remove(launch_key(self.launch_id), stage="run")
            except FlotillaError as exc:
                log.warning("删除 launch 目录失败，留给 gc：%s", exc)
                clean = False
        await self._anchor.close()
        return clean

    # ───────────────────────────── 创建 ─────────────────────────────

    def _open(self, trial_id: str, stage: Stage) -> _Trial:
        trial = self._trials.get(trial_id)
        if trial is None:
            raise ValueError(f"trial {trial_id} 未在记账中")
        if trial.released.is_set():
            raise self._not_sent(trial, stage)
        return trial

    def _not_sent(self, trial: _Trial, stage: Stage) -> FlotillaError:
        """请求未发出就放弃时的错误：launch 停用报 `lost`；trial 已被释放（调用方已不再等它）报可重试。"""
        if self._anchor.deactivated.is_set():
            return self._anchor.lost(stage)
        return FlotillaError(
            f"trial {trial.trial_id} 已释放，未发出请求",
            stage=stage,
            category=ErrorCategory.TRANSIENT,
            retryable=True,
        )

    async def _admit(self) -> None:
        """取得一个创建名额并等到限速放行。返回时持有名额，由调用方释放。"""
        await self._create_slots.acquire()
        try:
            await self._create_rate.acquire()
        except BaseException:
            self._create_slots.release()
            raise

    async def _create(self, trial: _Trial, unit: str, spec: InstanceSpec) -> InstanceHandle:
        try:
            await within(self._clock, None, self._admit(), abort=(trial.released, self._anchor.deactivated))
        except Aborted:
            raise self._not_sent(trial, "create") from None
        try:
            if trial.released.is_set():
                raise self._not_sent(trial, "create")
            self._anchor.ensure_alive("create")
            try:
                handle = await within(
                    self._clock,
                    self._settings.create_request_timeout_s,
                    self._platform.create(spec),
                    abort=(self._anchor.deactivated,),
                )
            except Aborted:
                self._uncertain(trial)
                raise self._anchor.lost("create") from None
            except Exception as exc:
                ended = self._uncertain(trial)
                if isinstance(exc, FlotillaError) and exc.category is not ErrorCategory.TRANSIENT:
                    raise at_stage(exc, "create") from exc  # 平台给了明确的失败：不当作成功找回
                failure = (
                    at_stage(exc, "create")
                    if isinstance(exc, FlotillaError)
                    else FlotillaError(
                        f"创建请求结果未知：{exc!r}",
                        stage="create",
                        category=ErrorCategory.TRANSIENT,
                        retryable=True,
                    )
                )
            else:
                trial.known.add(handle.iid)
                return handle
        finally:
            self._create_slots.release()
        # 对账不占创建名额。
        found = await self._reconcile(trial, unit, deadline=ended)
        if found is None:
            raise failure
        log.info("trial %s 单元 %s 的创建结果未知，对账找到实例 %s", trial.trial_id, unit, found)
        return InstanceHandle(iid=found)

    def _uncertain(self, trial: _Trial) -> float:
        """记一次结果未知的创建（调用刚结束）。返回此后列出不会再出现它的实例的时刻。"""
        until = self._clock.monotonic() + self._platform.caps.list_visibility_s
        trial.list_until = until if trial.list_until is None else max(trial.list_until, until)
        return until

    async def _reconcile(self, trial: _Trial, unit: str, *, deadline: float) -> str | None:
        """按标签列出，直到出现没记下的该单元实例（返回其 ID），或 `deadline` 已过（None）。

        到时限仍没有成功列出也返回 None：调用方得到可重试的错误，实例若存在由释放时的扫尾删除。
        """
        query = {**trial.labels(self.launch_id), labels.UNIT: unit}
        backoff = Backoff(1.0, cap=8.0)
        while True:
            issued = self._clock.monotonic()
            try:
                listed = await self._platform.list(query)
            except FlotillaError as exc:
                log.warning("trial %s 对账列出失败：%s", trial.trial_id, exc)
            else:
                fresh = sorted(st.iid for st in listed if st.iid not in trial.known)
                if fresh:
                    trial.known.update(fresh)  # 多出的（不应出现）也记下，释放时一并删除
                    return fresh[0]
            if issued >= deadline:
                return None
            await self._clock.sleep(step_towards(self._clock.monotonic(), deadline, backoff.next()))

    # ───────────────────────────── 回收 ─────────────────────────────

    async def _reap(self, trial: _Trial) -> None:
        try:
            if trial.inflight:
                await asyncio.wait(set(trial.inflight))
            complete = await trial.cleanup.delete_all(trial.known)
            if complete and trial.list_until is not None:
                complete = await trial.cleanup.sweep(trial.labels(self.launch_id), until=trial.list_until)
            if not complete:
                self._leaked = True
                log.error("trial %s 有实例未能确认删除，留给 gc 与平台 TTL", trial.trial_id)
            elif trial.dirs:
                await self._remove_dirs(trial)
        except asyncio.CancelledError:
            trial.cleanup.cancel()
            raise
        except Exception:
            self._leaked = True
            log.exception("回收 trial %s 时出错", trial.trial_id)
        finally:
            self._trials.pop(trial.trial_id, None)

    async def _remove_dirs(self, trial: _Trial) -> None:
        """实例删尽之后经锚点删除 trial 的共享卷目录（§7.4）。launch 已停用时锚点拒绝，留给 gc。"""
        for key in trial.dirs:
            try:
                await self._anchor.remove(key, stage="run")
            except FlotillaError as exc:
                log.warning("删除 trial %s 的目录 %s 失败，留给 gc：%s", trial.trial_id, key, exc)
                return


def _retrieve(task: asyncio.Task[object]) -> None:
    """后台任务的异常已由回收器自己处理或记录；取走它，免得 asyncio 报 "never retrieved"。"""
    if not task.cancelled():
        task.exception()
