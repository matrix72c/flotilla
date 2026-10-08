"""`FakePlatform` 与 `ManualClock` 的单元测试。

覆盖后续 core 测试依赖的平台语义：list 可见时限、create 的 after-effect 故障、删除幂等、
故障注入、后台进程推进、文件、exec handler、link，以及一次完整 trial 生命周期的串联。
"""

from __future__ import annotations

from datetime import timedelta

import pytest

from flotilla.platform.base import (
    ErrorCategory,
    ExecResult,
    ExternalPolicy,
    FlotillaError,
    InstanceSpec,
    InstanceStatus,
    ProcessSpec,
    Resources,
    Topology,
)
from flotilla.platform.fake import FakePlatform, ManualClock


def _spec(*, labels: dict[str, str] | None = None, timeout: int = 3600) -> InstanceSpec:
    return InstanceSpec(
        image="repo@sha256:abc",
        entrypoint=("/.flotilla/bin/busybox", "sleep", "2147483647"),
        labels=labels or {"flotilla/trial": "t1"},
        timeout_seconds=timeout,
        resources=Resources(cpu="1", memory="2Gi"),
        volumes=(),
        external=ExternalPolicy(mode="none"),
    )


def _proc(argv: tuple[str, ...] = ("true",), *, timeout_s: float | None = 5.0) -> ProcessSpec:
    return ProcessSpec(argv=argv, uid=0, gid=0, cwd="/", env={}, timeout_s=timeout_s)


def _fault() -> FlotillaError:
    return FlotillaError("injected", stage="run", category=ErrorCategory.TRANSIENT, retryable=True)


# ───────────────────────────── 生命周期与可见性 ─────────────────────────────


@pytest.mark.asyncio
async def test_create_then_get_is_immediate(platform: FakePlatform) -> None:
    handle = await platform.create(_spec())
    state = await platform.get(handle.iid)
    assert state.iid == handle.iid
    assert state.status is InstanceStatus.RUNNING


@pytest.mark.asyncio
async def test_list_respects_visibility_window(platform: FakePlatform) -> None:
    handle = await platform.create(_spec(labels={"flotilla/trial": "t1"}))
    # 刚创建：get 可见，list 不可见（未到 list_visibility_s）。
    assert (await platform.get(handle.iid)).status is InstanceStatus.RUNNING
    assert await platform.list({"flotilla/trial": "t1"}) == []
    # 推进到时限内仍不可见。
    await platform.clock.advance(29.0)
    assert await platform.list({"flotilla/trial": "t1"}) == []
    # 到达时限后可见。
    await platform.clock.advance(1.0)
    listed = await platform.list({"flotilla/trial": "t1"})
    assert [s.iid for s in listed] == [handle.iid]


@pytest.mark.asyncio
async def test_list_filters_by_labels(platform: FakePlatform) -> None:
    a = await platform.create(_spec(labels={"flotilla/trial": "t1", "flotilla/role": "unit"}))
    await platform.create(_spec(labels={"flotilla/trial": "t2", "flotilla/role": "unit"}))
    await platform.clock.advance(30.0)
    listed = await platform.list({"flotilla/trial": "t1"})
    assert [s.iid for s in listed] == [a.iid]
    anchors = await platform.list({"flotilla/role": "unit"})
    assert len(anchors) == 2


@pytest.mark.asyncio
async def test_get_missing_raises_not_found(platform: FakePlatform) -> None:
    with pytest.raises(FlotillaError) as exc:
        await platform.get("nope")
    assert exc.value.category is ErrorCategory.NOT_FOUND


@pytest.mark.asyncio
async def test_delete_is_idempotent(platform: FakePlatform) -> None:
    handle = await platform.create(_spec())
    await platform.delete(handle.iid)
    await platform.delete(handle.iid)  # 第二次不报错
    with pytest.raises(FlotillaError) as exc:
        await platform.get(handle.iid)
    assert exc.value.category is ErrorCategory.NOT_FOUND


@pytest.mark.asyncio
async def test_renew_updates_expiry(platform: FakePlatform) -> None:
    handle = await platform.create(_spec(timeout=3600))
    before = (await platform.get(handle.iid)).expires_at
    assert before is not None
    new_expiry = before + timedelta(seconds=1800)
    await platform.renew(handle.iid, new_expiry)
    assert (await platform.get(handle.iid)).expires_at == new_expiry


@pytest.mark.asyncio
async def test_set_status_is_visible_through_get_and_list(platform: FakePlatform) -> None:
    handle = await platform.create(_spec(labels={"flotilla/trial": "t1"}))
    platform.set_status(handle.iid, InstanceStatus.TERMINAL, reason="OOMKilled")
    state = await platform.get(handle.iid)
    assert state.status is InstanceStatus.TERMINAL and state.reason == "OOMKilled"
    await platform.clock.advance(30.0)
    [listed] = await platform.list({"flotilla/trial": "t1"})
    assert listed.status is InstanceStatus.TERMINAL


# ───────────────────────────── 故障注入与调用计数 ─────────────────────────────


@pytest.mark.asyncio
async def test_inject_fault_fires_once(platform: FakePlatform) -> None:
    platform.inject_fault("create", _fault())
    with pytest.raises(FlotillaError):
        await platform.create(_spec())
    # 下一次恢复正常。
    handle = await platform.create(_spec())
    assert (await platform.get(handle.iid)).status is InstanceStatus.RUNNING


@pytest.mark.asyncio
async def test_create_then_fail_leaves_instance(platform: FakePlatform) -> None:
    # 平台已接受、响应丢失：create 抛错，但实例真的建好了（§5.5 对账的核心场景）。
    platform.inject_create_then_fail(_fault())
    with pytest.raises(FlotillaError):
        await platform.create(_spec(labels={"flotilla/trial": "t1"}))
    assert len(platform.instances()) == 1
    await platform.clock.advance(30.0)
    listed = await platform.list({"flotilla/trial": "t1"})
    assert len(listed) == 1


@pytest.mark.asyncio
async def test_faults_consumed_in_order(platform: FakePlatform) -> None:
    platform.inject_fault("get", _fault())
    platform.inject_fault("get", _fault())
    handle = await platform.create(_spec())
    with pytest.raises(FlotillaError):
        await platform.get(handle.iid)
    with pytest.raises(FlotillaError):
        await platform.get(handle.iid)
    assert (await platform.get(handle.iid)).status is InstanceStatus.RUNNING  # 第三次恢复


def test_after_effect_only_allowed_for_create(platform: FakePlatform) -> None:
    with pytest.raises(ValueError):
        platform.inject_fault("delete", _fault(), after_effect=True)


@pytest.mark.asyncio
async def test_calls_count_every_invocation_including_faulted(platform: FakePlatform) -> None:
    # core 测试靠它断言"某调用未发生"（§14：释放与存活确认都不调用 list）。
    platform.inject_fault("get", _fault())
    handle = await platform.create(_spec())
    with pytest.raises(FlotillaError):
        await platform.get(handle.iid)
    await platform.get(handle.iid)
    await platform.delete(handle.iid)
    assert platform.calls == {"create": 1, "get": 2, "delete": 1}
    assert platform.calls["list"] == 0


# ───────────────────────────── 进程、文件、exec、link ─────────────────────────────


@pytest.mark.asyncio
async def test_background_process_lifecycle(platform: FakePlatform) -> None:
    handle = await platform.create(_spec())
    pid = await platform.start_process(handle, _proc(("sleep", "100"), timeout_s=None))
    status = await platform.process_status(handle, pid)
    assert status.found and status.running and status.exit_code is None
    platform.finish_process(handle.iid, pid, exit_code=0)
    status = await platform.process_status(handle, pid)
    assert status.found and not status.running and status.exit_code == 0


@pytest.mark.asyncio
async def test_process_status_not_found(platform: FakePlatform) -> None:
    handle = await platform.create(_spec())
    status = await platform.process_status(handle, "p-missing")
    assert not status.found  # execd 重启丢失记录 → core 按 lost 处理


@pytest.mark.asyncio
async def test_file_roundtrip_and_missing(platform: FakePlatform) -> None:
    handle = await platform.create(_spec())
    await platform.write_file(handle, "/etc/hosts", b"127.0.0.1 localhost", mode=0o644, uid=0, gid=0)
    assert await platform.read_file(handle, "/etc/hosts") == b"127.0.0.1 localhost"
    with pytest.raises(FlotillaError) as exc:
        await platform.read_file(handle, "/nope")
    assert exc.value.category is ErrorCategory.INVALID


@pytest.mark.asyncio
async def test_exec_handler_is_programmable(platform: FakePlatform) -> None:
    handle = await platform.create(_spec())
    assert (await platform.exec(handle, _proc())).exit_code == 0  # 默认退出 0

    def failing(iid: str, proc: ProcessSpec) -> ExecResult:
        return ExecResult(exit_code=7, stdout=b"", stderr=b"boom")

    platform.set_exec_handler(failing)
    result = await platform.exec(handle, _proc())
    assert result.exit_code == 7 and result.stderr == b"boom"


@pytest.mark.asyncio
async def test_internal_address_stable(platform: FakePlatform) -> None:
    handle = await platform.create(_spec())
    addr = await platform.internal_address(handle)
    assert await platform.internal_address(handle) == addr  # 生命周期内不变（C7）


@pytest.mark.asyncio
async def test_link_applies_topology_idempotently(platform: FakePlatform) -> None:
    a = await platform.create(_spec())
    b = await platform.create(_spec())
    outsider = await platform.create(_spec())
    members = {"web": a, "db": b}
    topo = Topology(networks={"default": frozenset({"web", "db"})})
    await platform.link(members, topo)
    await platform.link(members, topo)  # 幂等：同参再调不报错
    assert platform.topology_of(a.iid) == topo
    assert platform.topology_of(b.iid) == topo
    assert platform.topology_of(outsider.iid) is None


# ───────────────────────────── 完整 trial 生命周期串联 ─────────────────────────────


@pytest.mark.asyncio
async def test_full_trial_walkthrough(platform: FakePlatform, clock: ManualClock) -> None:
    """按 §5.1 阶段串起来跑一遍，确认 fake 能驱动一个多单元 trial。"""
    labels = {"flotilla/trial": "t1", "flotilla/launch": "L1"}

    # create：两个单元并行创建。
    web = await platform.create(_spec(labels=labels))
    db = await platform.create(_spec(labels=labels))
    for h in (web, db):
        assert (await platform.get(h.iid)).status is InstanceStatus.RUNNING

    # address：取内部地址。
    web_addr = await platform.internal_address(web)
    db_addr = await platform.internal_address(db)
    assert web_addr != db_addr

    # wire：一次 link。
    await platform.link({"web": web, "db": db}, Topology(networks={"default": frozenset({"web", "db"})}))

    # hosts：写入并读回比对。
    hosts = f"{db_addr} db\n{web_addr} web\n".encode()
    await platform.write_file(web, "/etc/hosts", hosts, mode=0o644, uid=0, gid=0)
    assert await platform.read_file(web, "/etc/hosts") == hosts

    # start：后台起业务进程；健康检查经 exec。
    pid = await platform.start_process(db, _proc(("mysqld",), timeout_s=None))
    assert (await platform.process_status(db, pid)).running
    assert (await platform.exec(db, _proc(("healthcheck",)))).exit_code == 0

    # 运行期：对账 list 需等可见时限。
    assert await platform.list({"flotilla/trial": "t1"}) == []
    await clock.advance(30.0)
    assert len(await platform.list({"flotilla/trial": "t1"})) == 2

    # release：删尽。
    for h in (web, db):
        await platform.delete(h.iid)
    assert platform.instances() == []
