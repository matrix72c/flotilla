"""业务进程：启动、健康检查、启动期退出检测、日志尾部、按 depends_on 门控启动（Architecture §3.5、§3.6、§5.3）。

**启动顺序**：每个服务等它自己的依赖满足条件后立即启动——与 Compose 一样按服务门控，而不是整层齐步；
先后约束与 §5.3 的分层相同（被依赖者总先启动），只是不让无关的慢服务拖住别人。

**就绪**（§3.6，与 `docker compose up --wait` 一致）：被 `service_completed_successfully` 依赖的服务以退出码 0 结束
（它的健康检查不执行，Compose 对这类服务只看退出）；其余带健康检查的服务 healthy 且进程仍在运行；其余服务的进程
已启动。任何一项失败，trial 以 `start` 阶段、`service` 类别失败，带上该服务的日志尾部（§5.3）；实例消失、执行通道
持续出错等基础设施问题按可重试报告。

时间一律经注入的 `Clock`；本模块不设整体时限，由 trial 以 `start_timeout(plan)` 套上（§5.1）。
"""

from __future__ import annotations

import asyncio
import logging
import shlex
from collections.abc import Mapping
from dataclasses import dataclass

from flotilla.core.plan import Condition, Healthcheck, TrialPlan, Unit
from flotilla.core.waiting import run_all
from flotilla.platform.base import (
    Clock,
    ErrorCategory,
    ExecResult,
    FlotillaError,
    InstanceHandle,
    Platform,
    ProcessSpec,
)

log = logging.getLogger(__name__)

BUSYBOX = "/.flotilla/bin/busybox"
LOG_DIR = "/run/flotilla"
TAIL_BYTES = 4096
_OWN_TIMEOUT_S = 30.0  # 编排核心自己的执行（建日志目录、读日志尾部）


def log_path(service: str) -> str:
    return f"{LOG_DIR}/{service}.log"


def service_spec(unit: Unit, argv: tuple[str, ...], *, timeout_s: float | None) -> ProcessSpec:
    """从服务的执行上下文构造 `ProcessSpec`（§3.5：业务进程与健康检查用上下文的环境、用户与工作目录）。"""
    ctx = unit.context
    return ProcessSpec(argv=argv, uid=ctx.uid, gid=ctx.gid, cwd=ctx.cwd, env=ctx.env, timeout_s=timeout_s)


def own_spec(argv: tuple[str, ...], *, timeout_s: float = _OWN_TIMEOUT_S) -> ProcessSpec:
    """编排核心自己的操作：空环境、root、`/`（§3.5）。"""
    return ProcessSpec(argv=argv, uid=0, gid=0, cwd="/", env={}, timeout_s=timeout_s)


# ───────────────────────────── 单个服务 ─────────────────────────────


async def launch(platform: Platform, handle: InstanceHandle, service: str, unit: Unit) -> str:
    """启动业务进程，标准输出与错误追加到 `/run/flotilla/<service>.log`（§3.5）。返回进程 ID。

    日志文件先由 root 建好并交给服务的 uid / gid，业务进程以自己的身份只追加写。
    """
    assert unit.command is not None
    path = log_path(service)
    ctx = unit.context
    prep = f"mkdir -p {LOG_DIR} && : > {shlex.quote(path)} && chown {ctx.uid}:{ctx.gid} {shlex.quote(path)}"
    try:
        result = await platform.exec(handle, own_spec((BUSYBOX, "sh", "-c", prep)))
    except FlotillaError as exc:
        raise _infra(service, exc) from exc
    if result.exit_code != 0 or result.timed_out:
        raise FlotillaError(
            f"{service}：准备日志文件失败：{_stderr(result)}",
            stage="start",
            category=ErrorCategory.TRANSIENT,
            retryable=True,
            service=service,
        )
    # 日志路径作为位置参数传入，不进环境：业务进程的环境恰好是上下文（§2.3）。
    wrapper = (BUSYBOX, "sh", "-c", 'log=$1; shift; exec "$@" >>"$log" 2>&1', "flotilla", path, *unit.command)
    try:
        return await platform.start_process(handle, service_spec(unit, wrapper, timeout_s=None))
    except FlotillaError as exc:
        raise _infra(service, exc) from exc


async def tail(platform: Platform, handle: InstanceHandle, service: str, nbytes: int = TAIL_BYTES) -> str:
    """服务日志的尾部；读不到时返回空串（只用于诊断，不让诊断失败掩盖原始错误）。"""
    try:
        result = await platform.exec(handle, own_spec((BUSYBOX, "tail", "-c", str(nbytes), log_path(service))))
    except FlotillaError:
        return ""
    return result.stdout.decode(errors="replace") if result.exit_code == 0 else ""


async def wait_healthy(
    platform: Platform,
    clock: Clock,
    handle: InstanceHandle,
    service: str,
    unit: Unit,
    pid: str,
    *,
    started_at: float,
) -> None:
    """按 Docker 的规则执行健康检查，直到第一次 healthy（§3.6）。unhealthy 时抛 `service` 错误。

    - 第一次检查在启动后一个间隔；`start_period` 内用 `start_interval`，之后用 `interval`；
    - 退出码 0 即 healthy；超时或非 0 是一次失败，`start_period` 内的失败不计入 `retries`；
    - 执行通道的瞬时错误不是检查结果，该次跳过；
    - 每次有结果的检查后查一次进程：进程已退出则立即失败。Docker 中容器随主进程退出——失败时不会再等满
      retries，成功也不算数（`test -f /ready` 这类检查在进程退出后仍可能成功，而占位入口让 sandbox 一直活着）。
    """
    hc = unit.healthcheck
    assert hc is not None
    failures = 0
    while True:
        await clock.sleep(_interval(hc, clock.monotonic() - started_at))
        in_start_period = clock.monotonic() - started_at < hc.start_period_s
        result = await _probe(platform, handle, service, unit, hc)
        if result is None:
            continue
        exit_code = await _exited(platform, handle, service, pid)
        if exit_code is not None:
            raise await _service_error(platform, handle, service, f"健康检查期间进程已退出（退出码 {exit_code}）")
        if result.exit_code == 0 and not result.timed_out:
            return
        if not in_start_period:
            failures += 1
        if failures >= hc.retries:
            why = "超时" if result.timed_out else f"退出码 {result.exit_code}"
            raise await _service_error(platform, handle, service, f"健康检查连续失败 {failures} 次（最后一次：{why}）")


async def wait_exit(
    platform: Platform,
    clock: Clock,
    handle: InstanceHandle,
    service: str,
    pid: str,
    *,
    poll_interval_s: float,
) -> int:
    """按 `poll_interval_s` 轮询 `process_status`，返回退出码（§3.6 启动期的退出检测）。"""
    while True:
        exit_code = await _exited(platform, handle, service, pid)
        if exit_code is not None:
            return exit_code
        await clock.sleep(poll_interval_s)


def _interval(hc: Healthcheck, elapsed: float) -> float:
    return hc.start_interval_s if elapsed < hc.start_period_s else hc.interval_s


async def _probe(
    platform: Platform, handle: InstanceHandle, service: str, unit: Unit, hc: Healthcheck
) -> ExecResult | None:
    try:
        return await platform.exec(handle, service_spec(unit, hc.test, timeout_s=hc.timeout_s))
    except FlotillaError as exc:
        if exc.category in (ErrorCategory.TRANSIENT, ErrorCategory.RATE_LIMITED):
            log.warning("%s：健康检查的执行通道出错，本次跳过：%s", service, exc)
            return None
        raise _infra(service, exc) from exc


async def _exited(platform: Platform, handle: InstanceHandle, service: str, pid: str) -> int | None:
    """进程已退出时返回退出码，仍在运行返回 None。查询不到进程（执行守护进程丢了记录）按基础设施故障。"""
    try:
        status = await platform.process_status(handle, pid)
    except FlotillaError as exc:
        if exc.category in (ErrorCategory.TRANSIENT, ErrorCategory.RATE_LIMITED):
            return None
        raise _infra(service, exc) from exc
    if not status.found:
        raise FlotillaError(
            f"{service}：查询不到业务进程 {pid}（执行守护进程可能已重启）",
            stage="start",
            category=ErrorCategory.TRANSIENT,
            retryable=True,
            service=service,
        )
    if status.running:
        return None
    return status.exit_code if status.exit_code is not None else -1


def _infra(service: str, exc: FlotillaError) -> FlotillaError:
    """启动期的平台错误：实例消失按可重试，其余保留类别。"""
    if exc.category is ErrorCategory.NOT_FOUND:
        return FlotillaError(
            f"{service}：实例在启动期消失：{exc}",
            stage="start",
            category=ErrorCategory.TRANSIENT,
            retryable=True,
            service=service,
        )
    return FlotillaError(str(exc), stage="start", category=exc.category, retryable=exc.retryable, service=service)


async def _service_error(platform: Platform, handle: InstanceHandle, service: str, why: str) -> FlotillaError:
    logs = await tail(platform, handle, service)
    message = f"{service}：{why}" + (f"\n--- 日志尾部 ---\n{logs}" if logs else "")
    return FlotillaError(message, stage="start", category=ErrorCategory.SERVICE, retryable=False, service=service)


def _stderr(result: ExecResult) -> str:
    return result.stderr.decode(errors="replace").strip()[-500:]


# ───────────────────────────── 整个 trial ─────────────────────────────


@dataclass(frozen=True)
class Started:
    """启动阶段的结果：各服务的进程 ID（没有业务进程的不在其中）与已结束的一次性服务的退出码。"""

    pids: Mapping[str, str]
    exit_codes: Mapping[str, int]


async def start_all(
    platform: Platform,
    clock: Clock,
    plan: TrialPlan,
    handles: Mapping[str, InstanceHandle],
    *,
    poll_interval_s: float = 2.0,
) -> Started:
    """按 depends_on 门控启动全部服务，等到就绪（见模块说明）。任一服务失败即取消其余并抛出。"""
    runner = _Runner(platform, clock, plan, handles, poll_interval_s)
    return await runner.run()


class _Gate:
    """一次性的结果门：`open()` 或 `fail(exc)` 之后，`wait()` 返回或抛出该异常。"""

    def __init__(self) -> None:
        self._event = asyncio.Event()
        self._error: FlotillaError | None = None

    def open(self) -> None:
        self._event.set()

    def fail(self, exc: FlotillaError) -> None:
        self._error = exc
        self._event.set()

    async def wait(self) -> None:
        await self._event.wait()
        if self._error is not None:
            raise self._error


class _Runner:
    def __init__(
        self,
        platform: Platform,
        clock: Clock,
        plan: TrialPlan,
        handles: Mapping[str, InstanceHandle],
        poll_interval_s: float,
    ) -> None:
        self._platform = platform
        self._clock = clock
        self._plan = plan
        self._handles = handles
        self._poll = poll_interval_s
        # 每个服务三个门：进程已启动、已 healthy、已以 0 退出。
        self._gates: dict[tuple[str, Condition], _Gate] = {
            (u, c): _Gate()
            for u in plan.units
            for c in ("service_started", "service_healthy", "service_completed_successfully")
        }
        awaited = {(dep, d.condition) for deps in plan.depends_on.values() for dep, d in deps.items()}
        self._wait_exit = {u for u, c in awaited if c == "service_completed_successfully"}
        self._pids: dict[str, str] = {}
        self._exit_codes: dict[str, int] = {}

    async def run(self) -> Started:
        await run_all(self._service(u) for u in sorted(self._plan.units))
        return Started(pids=dict(self._pids), exit_codes=dict(self._exit_codes))

    async def _service(self, name: str) -> None:
        unit = self._plan.units[name]
        if not await self._wait_dependencies(name):
            return  # 必需的依赖失败：它自己的任务会报告原始错误，依赖者不启动、不另报
        if unit.command is None:  # 只有客户端执行的独立 sandbox：没有业务进程
            self._gates[(name, "service_started")].open()
            return
        handle = self._handles[name]
        pid = await launch(self._platform, handle, name, unit)
        started_at = self._clock.monotonic()
        self._pids[name] = pid
        self._gates[(name, "service_started")].open()
        if name in self._wait_exit:
            # Compose 对被 service_completed_successfully 依赖的服务只等退出，不看健康检查（up --wait 同样）。
            await self._completion(name, pid)
        elif unit.healthcheck is not None:
            await self._health(name, unit, pid, started_at)

    async def _wait_dependencies(self, name: str) -> bool:
        """等依赖满足条件。返回 False 表示某个必需的依赖失败（依赖者不应启动）。"""
        for dep, d in sorted(self._plan.depends_on.get(name, {}).items()):
            if dep not in self._plan.units:
                continue  # validate() 保证必需的依赖存在；这里只可能是缺席的可选依赖
            try:
                await self._gates[(dep, d.condition)].wait()
            except FlotillaError:
                if d.required:
                    return False
                log.info("%s：可选依赖 %s 未满足 %s，不阻塞", name, dep, d.condition)
        return True

    async def _health(self, name: str, unit: Unit, pid: str, started_at: float) -> None:
        gate = self._gates[(name, "service_healthy")]
        try:
            await wait_healthy(self._platform, self._clock, self._handles[name], name, unit, pid, started_at=started_at)
        except FlotillaError as exc:
            gate.fail(exc)
            raise
        gate.open()

    async def _completion(self, name: str, pid: str) -> None:
        gate = self._gates[(name, "service_completed_successfully")]
        handle = self._handles[name]
        try:
            code = await wait_exit(self._platform, self._clock, handle, name, pid, poll_interval_s=self._poll)
            self._exit_codes[name] = code
            if code != 0:
                raise await _service_error(self._platform, handle, name, f"一次性服务退出码 {code}")
        except FlotillaError as exc:
            gate.fail(exc)
            raise
        gate.open()


# ───────────────────────────── 时限 ─────────────────────────────


def start_timeout(plan: TrialPlan, *, slack_s: float = 120.0, cap_s: float = 1800.0) -> float:
    """start 阶段的时限（§5.1）：依赖链上健康检查最长耗时之和加 `slack_s`，上限 `cap_s`。

    单个健康检查的最长耗时按 `start_period + (retries + 1) × (interval + timeout)` 估计。就绪要求所有健康检查通过
    （§3.6），所以取全部服务上最长的一条链，而不只是被依赖的。
    """
    longest: dict[str, float] = {}

    def chain(name: str) -> float:
        if name not in longest:
            hc = plan.units[name].healthcheck
            own = 0.0 if hc is None else hc.start_period_s + (hc.retries + 1) * (hc.interval_s + hc.timeout_s)
            deps = [d for d in plan.depends_on.get(name, {}) if d in plan.units]
            longest[name] = own + max((chain(d) for d in deps), default=0.0)
        return longest[name]

    return min(cap_s, max(chain(u) for u in plan.units) + slack_s)
