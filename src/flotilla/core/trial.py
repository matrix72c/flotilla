"""一个 trial 的启动：prepare / create / address / wire / hosts / start → ready（Architecture §5.1、§5.4）。

每个阶段独立超时，以该阶段报告错误（§5.4）。失败或被取消时已建的实例都在回收器名下：调用方 `release` 后
后台删除，清理失败不覆盖原始错误（PRD F6）。

- **prepare**：需要共享卷时经锚点创建本 trial 的目录、解包种子、复制可写 bind 源（§7.2、§7.3、§4.4）。
- **create**：并行创建全部单元（回收器限速、登记、对账，§5.5），等全部 `RUNNING`。单个单元失败且可重试时
  删除旧实例、重建一次（§5.4：此时还没有 `link`，不需要重写 hosts）。
- **address**：取每个单元的内部地址（C7）。
- **wire**：按 Compose 网络一次 `link`；成员少于两个时跳过（§6.2）。
- **hosts**：为每个单元写入 `/etc/hosts` 的标记块并读回比对（§3.7）。
- **start**：按 depends_on 门控启动业务进程，等就绪（§3.6、§5.3）。
"""

from __future__ import annotations

import logging
import uuid
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass, field

from flotilla.core import hosts, labels, network, process, volumes
from flotilla.core.anchor import Anchor
from flotilla.core.errors import at_stage
from flotilla.core.plan import TrialPlan
from flotilla.core.reaper import Reaper
from flotilla.core.waiting import Expired, run_all, wait_running, within
from flotilla.platform.base import (
    Clock,
    ErrorCategory,
    ExternalPolicy,
    FlotillaError,
    InstanceHandle,
    InstanceSpec,
    Platform,
    Stage,
)

log = logging.getLogger(__name__)

ENTRYPOINT = (process.BUSYBOX, "sleep", "2147483647")  # 占位入口（§3.4）
HOSTS_PATH = "/etc/hosts"


@dataclass(frozen=True)
class TrialSettings:
    """§16.3 `[timeouts]` 与 trial 用到的部署参数。"""

    share_release: str  # 钉住的 share 发布号（§3.3）
    external: ExternalPolicy  # 调用方的出站策略 `[network] external`（§6.4）
    ttl_s: int = 6 * 3600  # 每个实例的平台 TTL；不超过部署的 max_ttl_seconds（§5.6）
    create_timeout_s: float = 600.0
    address_timeout_s: float = 30.0
    wire_timeout_s: float = 60.0
    hosts_timeout_s: float = 30.0
    prepare_timeout_s: float = 600.0
    start_slack_s: float = 120.0
    start_max_s: float = 1800.0
    process_poll_s: float = 2.0
    extra_labels: Mapping[str, str] = field(default_factory=dict)  # 使用方的 request.labels（加 x- 前缀后并入）


@dataclass(frozen=True)
class Ready:
    """就绪的 trial：每个单元的句柄与内部地址、业务进程 ID、各阶段耗时（秒）、重建次数。"""

    handles: Mapping[str, InstanceHandle]
    addresses: Mapping[str, str]
    pids: Mapping[str, str]
    exit_codes: Mapping[str, int]
    timings: Mapping[str, float]
    recreated: frozenset[str]


class Trial:
    """一个 trial 的启动。调用方已 `reaper.track(trial_id)`；结束后（成功、失败或取消）须 `reaper.release`。

    构造时校验 plan 并算好全部单元的 `InstanceSpec`：标签、出站策略、卷键的错误在调用平台之前以 `ValueError` 报出
    （清单或翻译有误，不是平台问题）。
    """

    def __init__(
        self,
        platform: Platform,
        clock: Clock,
        anchor: Anchor,
        reaper: Reaper,
        plan: TrialPlan,
        trial_id: str,
        settings: TrialSettings,
    ) -> None:
        plan.validate()
        if settings.ttl_s <= 0:
            raise ValueError("ttl_s 须 > 0")
        max_ttl = platform.caps.max_ttl_seconds
        self._ttl = min(settings.ttl_s, max_ttl) if max_ttl is not None else settings.ttl_s
        self._platform = platform
        self._clock = clock
        self._anchor = anchor
        self._reaper = reaper
        self._plan = plan
        self._trial_id = trial_id
        self._settings = settings
        self._handles: dict[str, InstanceHandle] = {}
        self._timings: dict[str, float] = {}
        self._recreated: set[str] = set()
        # 需要互联的单元同一个放置组（C4 第 3 条）；重建的单元沿用同一个组。
        self._grouped = network.link_members(plan) if network.topology(plan) is not None else frozenset()
        self._group = str(uuid.uuid4()) if self._grouped else None
        self._specs = {unit: self._spec(unit) for unit in sorted(plan.units)}

    @property
    def handles(self) -> Mapping[str, InstanceHandle]:
        """目前已创建的单元（失败诊断用）。"""
        return dict(self._handles)

    async def start(self) -> Ready:
        s = self._settings
        if self._plan.volumes:
            await self._stage("prepare", s.prepare_timeout_s, self._prepare)
        await self._stage("create", s.create_timeout_s, self._create_all)
        addresses = await self._stage("address", s.address_timeout_s, self._addresses)
        topo = network.topology(self._plan)
        if topo is not None:
            members = {u: self._handles[u] for u in network.link_members(self._plan)}
            await self._stage("wire", s.wire_timeout_s, lambda: self._platform.link(members, topo))
        await self._stage("hosts", s.hosts_timeout_s, lambda: self._write_hosts(addresses))
        start_timeout = process.start_timeout(self._plan, slack_s=s.start_slack_s, cap_s=s.start_max_s)
        started = await self._stage(
            "start",
            start_timeout,
            lambda: process.start_all(
                self._platform, self._clock, self._plan, self._handles, poll_interval_s=s.process_poll_s
            ),
        )
        return Ready(
            handles=dict(self._handles),
            addresses=addresses,
            pids=started.pids,
            exit_codes=started.exit_codes,
            timings=dict(self._timings),
            recreated=frozenset(self._recreated),
        )

    # ───────────────────────────── 阶段 ─────────────────────────────

    async def _stage[T](self, stage: Stage, timeout: float, run: Callable[[], Awaitable[T]]) -> T:
        """以 `stage` 的时限执行；超时报该阶段的 `transient`，平台错误换到该阶段上（§5.4）。"""
        began = self._clock.monotonic()
        try:
            return await within(self._clock, timeout, run())
        except Expired as exc:
            raise FlotillaError(
                f"{stage} 阶段超过 {timeout}s",
                stage=stage,
                category=ErrorCategory.TRANSIENT,
                retryable=True,
            ) from exc
        except FlotillaError as exc:
            raise at_stage(exc, stage) from exc
        finally:
            self._timings[stage] = self._clock.monotonic() - began

    async def _prepare(self) -> None:
        """经锚点准备本 trial 的共享卷目录（§7.2、§7.3、§4.4）。目录先登记再创建，建了一半也会被回收。"""
        base = volumes.trial_key(self._anchor.launch_id, self._trial_id)
        self._reaper.add_dir(self._trial_id, base)
        await self._anchor.mkdir(base, stage="prepare")
        for vol in self._plan.volumes:
            key = f"{base}/{vol.key}"
            if vol.copy_from is not None:
                assert self._plan.task_files is not None
                await self._anchor.copy_tree(f"{self._plan.task_files}/{vol.copy_from}", key, stage="prepare")
                continue
            await self._anchor.mkdir(key, stage="prepare")
            if vol.seed is not None:
                assert self._plan.task_files is not None
                await self._anchor.unpack(f"{self._plan.task_files}/{vol.seed}", key, stage="prepare")
            elif vol.owner is not None:
                await self._anchor.chown(key, *vol.owner, stage="prepare")

    async def _create_all(self) -> None:
        await run_all(self._create_unit(u) for u in sorted(self._plan.units))

    async def _create_unit(self, unit: str) -> None:
        try:
            await self._create_once(unit)
        except FlotillaError as exc:
            # launch 停用（lost）时重建也不会被放行；只重建可能因换一个实例而成功的失败。
            if not exc.retryable or exc.category is ErrorCategory.LOST:
                raise
            log.warning("trial %s 单元 %s 创建失败，删除后重建一次：%s", self._trial_id, unit, exc)
            old = self._handles.pop(unit, None)
            if old is not None:
                self._reaper.discard(self._trial_id, old.iid)
            self._recreated.add(unit)
            await self._create_once(unit)

    async def _create_once(self, unit: str) -> None:
        handle = await self._reaper.create(self._trial_id, unit, self._specs[unit])
        self._handles[unit] = handle
        await wait_running(self._platform, self._clock, handle.iid, stage="create")

    def _spec(self, unit: str) -> InstanceSpec:
        u = self._plan.units[unit]
        return InstanceSpec(
            image=u.image,
            entrypoint=ENTRYPOINT,
            labels=self._labels(unit),
            timeout_seconds=self._ttl,
            resources=u.resources,
            volumes=volumes.unit_volumes(
                self._plan,
                unit,
                share_release=self._settings.share_release,
                launch_id=self._anchor.launch_id,
                trial_id=self._trial_id,
            ),
            external=network.external_policy(self._plan, unit, self._settings.external),
            privileged=u.privileged,
            devices=u.devices,
            group=self._group if unit in self._grouped else None,
        )

    def _labels(self, unit: str) -> dict[str, str]:
        """§5.2 的标签：用于列出的值原样校验，任务与使用方标签清洗（`labels` 模块）。

        使用方的键以 `x-` 前缀并入，不能覆盖 flotilla 的键。
        """
        limit = self._platform.caps.max_label_value_len
        out = {f"x-{k}": labels.sanitize_value(v, limit) for k, v in self._settings.extra_labels.items()}
        out.update(
            {
                labels.LAUNCH: labels.require_value(labels.LAUNCH, self._anchor.launch_id, limit),
                labels.TRIAL: labels.require_value(labels.TRIAL, self._trial_id, limit),
                labels.UNIT: labels.require_value(labels.UNIT, unit, limit),
                labels.TASK: labels.sanitize_value(self._plan.task, limit),
                labels.ROLE: labels.ROLE_UNIT,
            }
        )
        return out

    async def _addresses(self) -> dict[str, str]:
        names = sorted(self._plan.units)
        found = await run_all(self._platform.internal_address(self._handles[u]) for u in names)
        return dict(zip(names, found, strict=True))

    async def _write_hosts(self, addresses: Mapping[str, str]) -> None:
        await run_all(self._write_unit_hosts(u, addresses) for u in sorted(self._plan.units))

    async def _write_unit_hosts(self, unit: str, addresses: Mapping[str, str]) -> None:
        """读原 hosts、替换标记块、写入、读回比对（§3.7）。"""
        handle = self._handles[unit]
        original = (await self._platform.read_file(handle, HOSTS_PATH)).decode()
        content = hosts.merge(original, hosts.block(hosts.entries(self._plan, unit, addresses))).encode()
        await self._platform.write_file(handle, HOSTS_PATH, content, mode=0o644, uid=0, gid=0)
        if await self._platform.read_file(handle, HOSTS_PATH) != content:
            raise FlotillaError(
                f"{unit}：/etc/hosts 读回与写入不一致（平台可能在运行中重写它，C2 第 3 条）",
                stage="hosts",
                category=ErrorCategory.INVALID,
                retryable=False,
                service=unit,
            )
