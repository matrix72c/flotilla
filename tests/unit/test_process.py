"""`core/process.py` 的单元测试：Docker 的健康检查判定、启动期退出检测、按 depends_on 门控、就绪条件（§3.6、§5.3）。"""

from __future__ import annotations

import asyncio
from collections import defaultdict
from dataclasses import replace

import pytest

from flotilla.core import process
from flotilla.core.plan import Dependency, ExecContext, Healthcheck, TrialPlan, Unit
from flotilla.platform.base import (
    ErrorCategory,
    ExecResult,
    ExternalPolicy,
    FlotillaError,
    InstanceHandle,
    InstanceSpec,
    ProcessSpec,
    Resources,
)
from flotilla.platform.fake import FakePlatform, ManualClock

CTX = ExecContext(env={"PATH": "/usr/bin:/bin", "APP": "1"}, uid=1000, gid=1000, cwd="/app")
OK = ExecResult(exit_code=0, stdout=b"", stderr=b"")
FAIL = ExecResult(exit_code=1, stdout=b"", stderr=b"not ready")
TIMEOUT = ExecResult(exit_code=-1, stdout=b"", stderr=b"", timed_out=True)


def _unit(**kw: object) -> Unit:
    base = Unit(image="x@sha256:1", resources=Resources(cpu="1", memory="1Gi"), context=CTX, command=("serve",))
    return replace(base, **kw)  # type: ignore[arg-type]


def _hc(**kw: object) -> Healthcheck:
    return replace(Healthcheck(test=("check",), interval_s=10, timeout_s=5, retries=3), **kw)  # type: ignore[arg-type]


class Script:
    """按服务编排 fake 平台上的执行结果。健康检查结果按调用次序取自 `health[service]`（用尽后重复最后一个）；
    `logs[service]` 是 `tail` 读到的内容。其余命令（准备日志文件等）成功。"""

    def __init__(self, platform: FakePlatform) -> None:
        self.health: dict[str, list[ExecResult]] = {}
        self.logs: dict[str, bytes] = {}
        self.calls: dict[str, list[ProcessSpec]] = defaultdict(list)
        self.service_of: dict[str, str] = {}  # iid → 服务名
        self._n: dict[str, int] = defaultdict(int)
        platform.set_exec_handler(self._exec)

    def _exec(self, iid: str, proc: ProcessSpec) -> ExecResult:
        service = self.service_of[iid]
        self.calls[service].append(proc)
        if proc.argv == ("check",) or proc.argv[:1] == ("check",):
            seq = self.health.get(service, [OK])
            result = seq[min(self._n[service], len(seq) - 1)]
            self._n[service] += 1
            return result
        if proc.argv[1:3] == ("tail", "-c"):
            return ExecResult(exit_code=0, stdout=self.logs.get(service, b""), stderr=b"")
        return OK

    def checks(self, service: str) -> int:
        return self._n[service]


async def _handles(platform: FakePlatform, script: Script, plan: TrialPlan) -> dict[str, InstanceHandle]:
    out = {}
    for name in plan.units:
        h = await platform.create(
            InstanceSpec(
                image="x",
                entrypoint=("sleep",),
                labels={"flotilla/unit": name},
                timeout_seconds=3600,
                resources=Resources(cpu="1", memory="1Gi"),
                volumes=(),
                external=ExternalPolicy(mode="none"),
            )
        )
        script.service_of[h.iid] = name
        out[name] = h
    return out


def _plan(units: dict[str, Unit], depends_on: dict[str, dict[str, Dependency]] | None = None) -> TrialPlan:
    plan = TrialPlan(task="t", task_files=None, units=units, depends_on=depends_on or {})
    plan.validate()
    return plan


def _started(platform: FakePlatform, handles: dict[str, InstanceHandle], service: str) -> bool:
    return bool(platform.pids(handles[service].iid))


def _finish(platform: FakePlatform, handles: dict[str, InstanceHandle], service: str, exit_code: int) -> None:
    """让服务的业务进程以 `exit_code` 退出（fake 中每个单元至多一个后台进程）。"""
    [pid] = platform.pids(handles[service].iid)
    platform.finish_process(handles[service].iid, pid, exit_code)


# ───────────────────────────── 单个服务 ─────────────────────────────


@pytest.mark.asyncio
async def test_launch_redirects_output_and_uses_context(platform: FakePlatform, clock: ManualClock) -> None:
    plan = _plan({"web": _unit()})
    script = Script(platform)
    handles = await _handles(platform, script, plan)
    await process.launch(platform, handles["web"], "web", plan.units["web"])
    prep = script.calls["web"][0]
    assert prep.uid == 0 and prep.env == {} and "chown 1000:1000 /run/flotilla/web.log" in prep.argv[-1]
    [pid] = platform.pids(handles["web"].iid)
    proc = platform.process_spec(handles["web"].iid, pid)
    assert proc.argv[-1] == "serve" and "/run/flotilla/web.log" in proc.argv
    assert (proc.uid, proc.gid, proc.cwd) == (1000, 1000, "/app")
    assert proc.env == CTX.env  # 环境恰好是上下文，日志路径不进环境
    assert proc.timeout_s is None


@pytest.mark.asyncio
async def test_first_check_after_one_interval_then_healthy(platform: FakePlatform, clock: ManualClock) -> None:
    plan = _plan({"db": _unit(healthcheck=_hc())})
    script = Script(platform)
    script.health["db"] = [FAIL, OK]
    handles = await _handles(platform, script, plan)
    started = clock.monotonic()
    await clock.run(process.start_all(platform, clock, plan, handles))
    assert script.checks("db") == 2
    assert clock.monotonic() - started == pytest.approx(20.0)  # 10s 后第一次失败，20s 后成功


@pytest.mark.asyncio
async def test_unhealthy_after_retries_is_service_error_with_logs(platform: FakePlatform, clock: ManualClock) -> None:
    plan = _plan({"db": _unit(healthcheck=_hc(retries=3))})
    script = Script(platform)
    script.health["db"] = [FAIL]
    script.logs["db"] = b"FATAL: cannot bind port\n"
    handles = await _handles(platform, script, plan)
    with pytest.raises(FlotillaError) as exc:
        await clock.run(process.start_all(platform, clock, plan, handles))
    err = exc.value
    assert (err.stage, err.category, err.retryable, err.service) == ("start", ErrorCategory.SERVICE, False, "db")
    assert "cannot bind port" in str(err)
    assert script.checks("db") == 3


@pytest.mark.asyncio
async def test_timed_out_check_counts_as_failure(platform: FakePlatform, clock: ManualClock) -> None:
    plan = _plan({"db": _unit(healthcheck=_hc(retries=2))})
    script = Script(platform)
    script.health["db"] = [TIMEOUT]
    handles = await _handles(platform, script, plan)
    with pytest.raises(FlotillaError) as exc:
        await clock.run(process.start_all(platform, clock, plan, handles))
    assert exc.value.category is ErrorCategory.SERVICE and "超时" in str(exc.value)


@pytest.mark.asyncio
async def test_start_period_failures_not_counted_and_use_start_interval(
    platform: FakePlatform, clock: ManualClock
) -> None:
    # start_period 60s、start_interval 5s：前 60s 内的失败不计入 retries=1，第一次检查在 5s。
    plan = _plan({"db": _unit(healthcheck=_hc(retries=1, start_period_s=60, start_interval_s=5))})
    script = Script(platform)
    script.health["db"] = [FAIL] * 11 + [OK]  # 5,10,…,55 共 11 次失败，60s 那次成功
    handles = await _handles(platform, script, plan)
    started = clock.monotonic()
    await clock.run(process.start_all(platform, clock, plan, handles))
    assert script.checks("db") == 12
    assert clock.monotonic() - started == pytest.approx(60.0)


@pytest.mark.asyncio
async def test_transient_exec_error_is_skipped_not_counted(platform: FakePlatform, clock: ManualClock) -> None:
    plan = _plan({"db": _unit(healthcheck=_hc(retries=1))})
    script = Script(platform)
    handles = await _handles(platform, script, plan)
    await process.launch(platform, handles["db"], "db", plan.units["db"])  # 消耗准备日志那次 exec
    platform.inject_fault("exec", FlotillaError("503", stage="run", category=ErrorCategory.TRANSIENT, retryable=True))
    [pid] = platform.pids(handles["db"].iid)
    await clock.run(
        process.wait_healthy(platform, clock, handles["db"], "db", plan.units["db"], pid, started_at=clock.monotonic())
    )
    assert script.checks("db") == 1  # 第一次被执行通道错误吞掉，第二次成功；retries=1 也没判 unhealthy


@pytest.mark.asyncio
async def test_process_exit_during_healthcheck_fails_fast(platform: FakePlatform, clock: ManualClock) -> None:
    plan = _plan({"db": _unit(healthcheck=_hc(retries=100))})
    script = Script(platform)
    script.health["db"] = [FAIL]
    handles = await _handles(platform, script, plan)
    task = asyncio.create_task(process.start_all(platform, clock, plan, handles))
    await clock.advance(1.0)
    _finish(platform, handles, "db", 137)
    with pytest.raises(FlotillaError) as exc:
        await clock.run(task)
    assert "退出码 137" in str(exc.value) and exc.value.category is ErrorCategory.SERVICE
    assert script.checks("db") == 1  # 不等满 100 次


@pytest.mark.asyncio
async def test_lost_process_record_is_retryable(platform: FakePlatform, clock: ManualClock) -> None:
    plan = _plan({"db": _unit(healthcheck=_hc())})
    script = Script(platform)
    script.health["db"] = [FAIL]
    handles = await _handles(platform, script, plan)
    task = asyncio.create_task(process.start_all(platform, clock, plan, handles))
    await clock.advance(1.0)
    platform.lose_processes(handles["db"].iid)  # 执行守护进程重启，丢了记录
    with pytest.raises(FlotillaError) as exc:
        await clock.run(task)
    assert exc.value.retryable and exc.value.category is ErrorCategory.TRANSIENT


# ───────────────────────────── 门控与就绪 ─────────────────────────────


@pytest.mark.asyncio
async def test_dependent_starts_only_after_dependency_healthy(platform: FakePlatform, clock: ManualClock) -> None:
    plan = _plan(
        {"db": _unit(healthcheck=_hc()), "app": _unit()},
        {"app": {"db": Dependency("service_healthy")}},
    )
    script = Script(platform)
    script.health["db"] = [FAIL, FAIL, OK]
    handles = await _handles(platform, script, plan)
    task = asyncio.create_task(process.start_all(platform, clock, plan, handles))
    await clock.advance(25.0)
    assert not _started(platform, handles, "app")  # db 还没 healthy
    await clock.advance(5.0)
    assert _started(platform, handles, "app")  # 30s：db healthy，app 启动
    await clock.run(task)


@pytest.mark.asyncio
async def test_completed_successfully_waits_for_exit_zero(platform: FakePlatform, clock: ManualClock) -> None:
    plan = _plan(
        {"init": _unit(command=("migrate",)), "app": _unit()},
        {"app": {"init": Dependency("service_completed_successfully")}},
    )
    script = Script(platform)
    handles = await _handles(platform, script, plan)
    task = asyncio.create_task(process.start_all(platform, clock, plan, handles, poll_interval_s=2.0))
    await clock.advance(7.0)
    assert not _started(platform, handles, "app")
    _finish(platform, handles, "init", 0)
    started = await clock.run(task)
    assert started.exit_codes == {"init": 0}
    assert set(started.pids) == {"init", "app"}


@pytest.mark.asyncio
async def test_one_shot_nonzero_exit_fails_trial(platform: FakePlatform, clock: ManualClock) -> None:
    plan = _plan(
        {"init": _unit(command=("migrate",)), "app": _unit()},
        {"app": {"init": Dependency("service_completed_successfully")}},
    )
    script = Script(platform)
    script.logs["init"] = b"migration 0042 failed\n"
    handles = await _handles(platform, script, plan)
    task = asyncio.create_task(process.start_all(platform, clock, plan, handles))
    await clock.advance(1.0)
    _finish(platform, handles, "init", 3)
    with pytest.raises(FlotillaError) as exc:
        await clock.run(task)
    assert exc.value.service == "init" and "退出码 3" in str(exc.value) and "0042" in str(exc.value)
    assert not _started(platform, handles, "app")  # 依赖者从未启动


@pytest.mark.asyncio
async def test_optional_dependency_failure_does_not_block(platform: FakePlatform, clock: ManualClock) -> None:
    # required: false 的依赖失败不阻塞依赖者；但按就绪条件（up --wait），失败的那个服务本身仍让 trial 失败。
    plan = _plan(
        {"metrics": _unit(healthcheck=_hc(retries=1)), "app": _unit()},
        {"app": {"metrics": Dependency("service_healthy", required=False)}},
    )
    script = Script(platform)
    script.health["metrics"] = [FAIL]
    handles = await _handles(platform, script, plan)
    with pytest.raises(FlotillaError) as exc:
        await clock.run(process.start_all(platform, clock, plan, handles))
    assert exc.value.service == "metrics"


@pytest.mark.asyncio
async def test_failed_optional_dependency_reports_its_own_error(platform: FakePlatform, clock: ManualClock) -> None:
    # 可选依赖失败时依赖者不报"依赖未满足"；但就绪条件（up --wait）要求每个一次性服务以 0 退出，所以 trial 以
    # init 自己的错误失败。依赖者是否来得及启动没有意义（trial 无论如何失败），不断言。
    # 可选依赖真正"不阻塞"的场景是它缺席，见 test_plan.py::test_absent_optional_dependency_is_valid。
    plan = _plan(
        {"init": _unit(command=("seed",)), "app": _unit()},
        {"app": {"init": Dependency("service_completed_successfully", required=False)}},
    )
    script = Script(platform)
    script.logs["init"] = b"seed failed\n"
    handles = await _handles(platform, script, plan)
    task = asyncio.create_task(process.start_all(platform, clock, plan, handles))
    await clock.advance(1.0)
    _finish(platform, handles, "init", 1)
    with pytest.raises(FlotillaError) as exc:
        await clock.run(task)
    assert exc.value.service == "init" and "seed failed" in str(exc.value)


@pytest.mark.asyncio
async def test_ready_waits_for_unawaited_healthcheck(platform: FakePlatform, clock: ManualClock) -> None:
    # 没有服务以 service_healthy 等它，但就绪条件与 up --wait 一致：它也要 healthy。
    plan = _plan({"target": _unit(healthcheck=_hc()), "main": _unit()})
    script = Script(platform)
    script.health["target"] = [FAIL, FAIL, OK]
    handles = await _handles(platform, script, plan)
    started = clock.monotonic()
    await clock.run(process.start_all(platform, clock, plan, handles))
    assert clock.monotonic() - started == pytest.approx(30.0)


@pytest.mark.asyncio
async def test_unit_without_process_is_started_immediately(platform: FakePlatform, clock: ManualClock) -> None:
    plan = _plan({"judge": _unit(command=None), "main": _unit()}, {"main": {"judge": Dependency("service_started")}})
    script = Script(platform)
    handles = await _handles(platform, script, plan)
    started = await clock.run(process.start_all(platform, clock, plan, handles))
    assert set(started.pids) == {"main"}


@pytest.mark.asyncio
async def test_failure_cancels_other_waiters(platform: FakePlatform, clock: ManualClock) -> None:
    # 一个服务失败时，其他还在等健康检查的服务不再继续检查。
    plan = _plan({"bad": _unit(healthcheck=_hc(retries=1)), "slow": _unit(healthcheck=_hc(retries=100))})
    script = Script(platform)
    script.health["bad"] = [FAIL]
    script.health["slow"] = [FAIL]
    handles = await _handles(platform, script, plan)
    with pytest.raises(FlotillaError):
        await clock.run(process.start_all(platform, clock, plan, handles))
    checks = script.checks("slow")
    await clock.advance(600.0)
    assert script.checks("slow") == checks


# ───────────────────────────── 时限 ─────────────────────────────


def test_start_timeout_sums_longest_chain() -> None:
    hc = _hc(interval_s=5, timeout_s=5, retries=2)  # (2+1)×10 = 30
    plan = _plan(
        {"db": _unit(healthcheck=hc), "app": _unit(healthcheck=hc), "web": _unit(), "side": _unit(healthcheck=hc)},
        {"app": {"db": Dependency("service_healthy")}, "web": {"app": Dependency("service_healthy")}},
    )
    assert process.start_timeout(plan) == 30 + 30 + 120


def test_start_timeout_capped() -> None:
    plan = _plan({"db": _unit(healthcheck=_hc(interval_s=60, timeout_s=60, retries=100))})
    assert process.start_timeout(plan) == 1800


# ───────────────────────────── 审查回归 ─────────────────────────────


@pytest.mark.asyncio
async def test_one_shot_with_healthcheck_only_waits_for_exit(platform: FakePlatform, clock: ManualClock) -> None:
    # 被 service_completed_successfully 依赖的服务带健康检查（例如镜像自带 HEALTHCHECK）且检查一直失败：
    # Compose 只看它的退出码，以 0 退出即满足，健康检查不参与。
    plan = _plan(
        {"init": _unit(command=("migrate",), healthcheck=_hc(retries=1)), "app": _unit()},
        {"app": {"init": Dependency("service_completed_successfully")}},
    )
    script = Script(platform)
    script.health["init"] = [FAIL]
    handles = await _handles(platform, script, plan)
    task = asyncio.create_task(process.start_all(platform, clock, plan, handles))
    await clock.advance(30.0)
    _finish(platform, handles, "init", 0)
    started = await clock.run(task)
    assert started.exit_codes == {"init": 0}
    assert script.checks("init") == 0


@pytest.mark.asyncio
@pytest.mark.parametrize("method", ["exec", "start_process"])
async def test_instance_gone_at_launch_is_retryable(platform: FakePlatform, clock: ManualClock, method: str) -> None:
    plan = _plan({"web": _unit()})
    script = Script(platform)
    handles = await _handles(platform, script, plan)
    gone = FlotillaError("404", stage="run", category=ErrorCategory.NOT_FOUND, retryable=False)
    platform.inject_fault(method, gone)  # type: ignore[arg-type]
    with pytest.raises(FlotillaError) as exc:
        await clock.run(process.start_all(platform, clock, plan, handles))
    err = exc.value
    assert (err.stage, err.category, err.retryable, err.service) == ("start", ErrorCategory.TRANSIENT, True, "web")


@pytest.mark.asyncio
async def test_no_background_polling_after_failure(platform: FakePlatform, clock: ManualClock) -> None:
    # 一个服务失败后，其他服务的退出检测与健康检查都停止，不在 start_all 返回后继续打平台。
    plan = _plan(
        {"bad": _unit(healthcheck=_hc(retries=1)), "init": _unit(command=("migrate",)), "app": _unit()},
        {"app": {"init": Dependency("service_completed_successfully")}},
    )
    script = Script(platform)
    script.health["bad"] = [FAIL]
    handles = await _handles(platform, script, plan)
    with pytest.raises(FlotillaError):
        await clock.run(process.start_all(platform, clock, plan, handles))
    calls = (platform.calls["process_status"], platform.calls["exec"])
    await clock.advance(600.0)
    assert (platform.calls["process_status"], platform.calls["exec"]) == calls


@pytest.mark.asyncio
async def test_healthy_check_after_process_exit_is_failure(platform: FakePlatform, clock: ManualClock) -> None:
    # `test -f /ready` 这类检查在业务进程退出后仍会成功；Docker 中容器已退出，up --wait 报错。
    plan = _plan({"web": _unit(healthcheck=_hc())})
    script = Script(platform)
    script.health["web"] = [OK]
    handles = await _handles(platform, script, plan)
    task = asyncio.create_task(process.start_all(platform, clock, plan, handles))
    await clock.advance(1.0)
    _finish(platform, handles, "web", 0)
    with pytest.raises(FlotillaError) as exc:
        await clock.run(task)
    assert exc.value.category is ErrorCategory.SERVICE and "已退出" in str(exc.value)


@pytest.mark.asyncio
async def test_first_failure_is_deterministic(platform: FakePlatform, clock: ManualClock) -> None:
    # 两个服务在同一时刻 unhealthy：每次都报同一个（按名字顺序），不随集合遍历顺序变。
    plan = _plan({"b": _unit(healthcheck=_hc(retries=1)), "a": _unit(healthcheck=_hc(retries=1))})
    seen = set()
    for _ in range(5):
        script = Script(platform)
        script.health = {"a": [FAIL], "b": [FAIL]}
        handles = await _handles(platform, script, plan)
        with pytest.raises(FlotillaError) as exc:
            await clock.run(process.start_all(platform, clock, plan, handles))
        seen.add(exc.value.service)
    assert seen == {"a"}
