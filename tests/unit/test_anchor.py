"""`Anchor` 的单元测试（Architecture §5.6）：启动顺序、续期、宽限期停用、目录操作。"""

from __future__ import annotations

import asyncio
from datetime import datetime

import pytest

from flotilla.core import labels
from flotilla.core.anchor import Anchor, AnchorSettings
from flotilla.platform.base import ErrorCategory, ExecResult, FlotillaError, InstanceStatus, ProcessSpec
from flotilla.platform.fake import FakePlatform, ManualClock

SETTINGS = AnchorSettings(image="anchor@sha256:abc")


def _transient() -> FlotillaError:
    return FlotillaError("503", stage="run", category=ErrorCategory.TRANSIENT, retryable=True)


async def _started(platform: FakePlatform, clock: ManualClock, settings: AnchorSettings = SETTINGS) -> Anchor:
    anchor = Anchor(platform, clock, "L1", settings)
    await clock.run(anchor.start())
    return anchor


# ───────────────────────────── 启动 ─────────────────────────────


@pytest.mark.asyncio
async def test_start_waits_until_listed(platform: FakePlatform, clock: ManualClock) -> None:
    anchor = await _started(platform, clock)
    # list 要到 list_visibility_s（30s）才列出它；start 在那之前不返回。
    assert clock.monotonic() >= platform.caps.list_visibility_s
    [state] = await platform.list(labels.anchor_labels("L1"))
    assert state.iid == anchor.iid
    anchor.ensure_alive("prepare")  # 不抛


@pytest.mark.asyncio
async def test_start_creates_isolated_instance_with_shared_root(platform: FakePlatform, clock: ManualClock) -> None:
    await _started(platform, clock)
    [inst] = platform.instances()
    assert inst.labels == {labels.LAUNCH: "L1", labels.ROLE: labels.ROLE_ANCHOR}
    spec = platform.spec_of(inst.iid)
    assert spec.external.mode == "none"
    [vol] = spec.volumes
    assert vol.key == "" and not vol.read_only


@pytest.mark.asyncio
async def test_start_waits_for_running(platform: FakePlatform, clock: ManualClock) -> None:
    platform.create_status = InstanceStatus.PENDING
    anchor = Anchor(platform, clock, "L1", SETTINGS)
    task = asyncio.create_task(anchor.start())
    await clock.advance(60.0)
    assert not task.done()  # 实例一直 PENDING
    [inst] = platform.instances()
    platform.set_status(inst.iid, InstanceStatus.RUNNING)
    await clock.run(task)
    anchor.ensure_alive("prepare")


@pytest.mark.asyncio
async def test_start_timeout_deletes_instance(platform: FakePlatform, clock: ManualClock) -> None:
    platform.create_status = InstanceStatus.PENDING
    anchor = Anchor(platform, clock, "L1", SETTINGS)
    with pytest.raises(FlotillaError) as exc:
        await clock.run(anchor.start())
    assert exc.value.category is ErrorCategory.CAPACITY and exc.value.retryable
    assert platform.instances() == []


@pytest.mark.asyncio
async def test_start_cancelled_deletes_instance(platform: FakePlatform, clock: ManualClock) -> None:
    anchor = Anchor(platform, clock, "L1", SETTINGS)
    task = asyncio.create_task(anchor.start())
    await clock.advance(5.0)  # 已创建、尚未出现在列表中
    assert len(platform.instances()) == 1
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert platform.instances() == []


@pytest.mark.asyncio
async def test_not_ready_before_start(platform: FakePlatform, clock: ManualClock) -> None:
    anchor = Anchor(platform, clock, "L1", SETTINGS)
    with pytest.raises(RuntimeError):
        anchor.ensure_alive("create")


# ───────────────────────────── 续期与停用 ─────────────────────────────


@pytest.mark.asyncio
async def test_renews_every_interval(platform: FakePlatform, clock: ManualClock) -> None:
    anchor = await _started(platform, clock)
    assert anchor.iid is not None
    before = platform.calls["renew"]
    await clock.advance(300.0)
    assert platform.calls["renew"] - before == pytest.approx(300.0 / SETTINGS.renew_interval_s, abs=1)
    anchor.ensure_alive("create")
    # 续期把过期时间推到 now + ttl。
    state = await platform.get(anchor.iid)
    assert state.expires_at is not None
    assert (state.expires_at - clock.now()).total_seconds() > SETTINGS.ttl_s - SETTINGS.renew_interval_s


@pytest.mark.asyncio
async def test_transient_renew_failures_within_grace_are_tolerated(platform: FakePlatform, clock: ManualClock) -> None:
    anchor = await _started(platform, clock)
    for _ in range(5):  # 5 次 × 5s 重试间隔，远小于 G
        platform.inject_fault("renew", _transient())
    await clock.advance(200.0)
    anchor.ensure_alive("create")
    assert not anchor.deactivated.is_set()


@pytest.mark.asyncio
async def test_deactivates_exactly_at_grace(platform: FakePlatform, clock: ManualClock) -> None:
    # start 之前就让续期全部失败：t0 是创建请求的发出时刻 0，停用恰在 t0 + G。
    for _ in range(1000):
        platform.inject_fault("renew", _transient())
    anchor = await _started(platform, clock)
    await clock.advance(SETTINGS.grace_s - clock.monotonic() - 0.001)
    assert not anchor.deactivated.is_set()
    anchor.ensure_alive("create")
    await clock.advance(0.001)
    assert anchor.deactivated.is_set()
    with pytest.raises(FlotillaError) as exc:
        anchor.ensure_alive("create")
    assert exc.value.category is ErrorCategory.LOST and exc.value.stage == "create"
    # 停用后不再续期。
    renews = platform.calls["renew"]
    await clock.advance(600.0)
    assert platform.calls["renew"] == renews


@pytest.mark.asyncio
async def test_grace_counts_from_last_successful_renew_send(platform: FakePlatform, clock: ManualClock) -> None:
    anchor = await _started(platform, clock)
    await clock.advance(SETTINGS.renew_interval_s * 2 - clock.monotonic() % SETTINGS.renew_interval_s)
    t0 = clock.monotonic()  # 恰在一次成功续期的时刻
    for _ in range(1000):
        platform.inject_fault("renew", _transient())
    await clock.advance(SETTINGS.grace_s - 0.001)
    assert not anchor.deactivated.is_set()
    await clock.advance(0.001)
    assert anchor.deactivated.is_set()
    assert clock.monotonic() == t0 + SETTINGS.grace_s


@pytest.mark.asyncio
async def test_hanging_renew_does_not_delay_deactivation(platform: FakePlatform, clock: ManualClock) -> None:
    anchor = await _started(platform, clock)
    platform.set_latency("renew", 10_000.0)  # 之后的续期请求都卡住，不返回也不报错
    # 最后一次成功续期不晚于现在，所以停用最晚在 G 之后，不等卡住的请求返回。
    await clock.advance(SETTINGS.grace_s)
    assert anchor.deactivated.is_set()
    with pytest.raises(FlotillaError):
        anchor.ensure_alive("create")


@pytest.mark.asyncio
async def test_deactivation_is_permanent(platform: FakePlatform, clock: ManualClock) -> None:
    anchor = await _started(platform, clock)
    for _ in range(1000):
        platform.inject_fault("renew", _transient())
    await clock.advance(SETTINGS.grace_s)
    assert anchor.deactivated.is_set()
    platform.clear_faults()  # 平台恢复
    await clock.advance(600.0)
    with pytest.raises(FlotillaError):
        anchor.ensure_alive("create")


@pytest.mark.asyncio
async def test_ensure_alive_checks_time_without_waiting_for_loop(platform: FakePlatform, clock: ManualClock) -> None:
    anchor = await _started(platform, clock)
    for _ in range(1000):
        platform.inject_fault("renew", _transient())
    # 续期循环在 sleep 中，时间已过 G：ensure_alive 自己按时间判定。
    clock.jump(SETTINGS.grace_s)  # 只拨表，不驱动事件循环
    with pytest.raises(FlotillaError):
        anchor.ensure_alive("create")
    assert anchor.deactivated.is_set()


@pytest.mark.asyncio
async def test_anchor_gone_deactivates(platform: FakePlatform, clock: ManualClock) -> None:
    anchor = await _started(platform, clock)
    assert anchor.iid is not None
    await platform.delete(anchor.iid)
    await clock.advance(SETTINGS.renew_interval_s)
    assert anchor.deactivated.is_set()


@pytest.mark.asyncio
async def test_close_deletes_and_rejects(platform: FakePlatform, clock: ManualClock) -> None:
    anchor = await _started(platform, clock)
    await anchor.close()
    await anchor.close()  # 幂等
    assert platform.instances() == []
    with pytest.raises(FlotillaError):
        anchor.ensure_alive("create")
    renews = platform.calls["renew"]
    await clock.advance(600.0)
    assert platform.calls["renew"] == renews


def test_settings_reject_unsafe_timing() -> None:
    with pytest.raises(ValueError):
        AnchorSettings(image="x", renew_interval_s=90.0, grace_s=90.0)
    with pytest.raises(ValueError):
        AnchorSettings(image="x", grace_s=900.0, ttl_s=600)
    with pytest.raises(ValueError):
        AnchorSettings(image="x", mount_path="relative")


# ───────────────────────────── 目录操作 ─────────────────────────────


@pytest.mark.asyncio
async def test_directory_ops_run_in_anchor_under_mount(platform: FakePlatform, clock: ManualClock) -> None:
    anchor = await _started(platform, clock)
    seen: list[ProcessSpec] = []

    def handler(iid: str, proc: ProcessSpec) -> ExecResult:
        assert iid == anchor.iid
        seen.append(proc)
        stdout = b"a\nb\n" if proc.argv[0] == "ls" else b""
        return ExecResult(exit_code=0, stdout=stdout, stderr=b"")

    platform.set_exec_handler(handler)
    await anchor.mkdir("launches/L1", stage="prepare")
    assert await anchor.listdir("launches", stage="run") == ["a", "b"]
    await anchor.remove("launches/L1/t1", stage="run")
    assert [p.argv for p in seen] == [
        ("mkdir", "-p", "--", "/flotilla-root/launches/L1"),
        ("ls", "-1A", "--", "/flotilla-root/launches"),
        ("rm", "-rf", "--", "/flotilla-root/launches/L1/t1"),
    ]
    assert all(p.timeout_s is not None and p.uid == 0 for p in seen)


@pytest.mark.asyncio
@pytest.mark.parametrize("key", ["..", "a/../b", "/abs", "a//b", "a/", "./a"])
async def test_rejects_bad_keys(platform: FakePlatform, clock: ManualClock, key: str) -> None:
    anchor = await _started(platform, clock)
    with pytest.raises(ValueError):
        await anchor.mkdir(key, stage="prepare")


@pytest.mark.asyncio
async def test_refuses_to_remove_root(platform: FakePlatform, clock: ManualClock) -> None:
    anchor = await _started(platform, clock)
    with pytest.raises(ValueError):
        await anchor.remove("", stage="run")


@pytest.mark.asyncio
async def test_failed_command_is_transient_at_stage(platform: FakePlatform, clock: ManualClock) -> None:
    anchor = await _started(platform, clock)
    platform.set_exec_handler(lambda iid, proc: ExecResult(exit_code=1, stdout=b"", stderr=b"No space left"))
    with pytest.raises(FlotillaError) as exc:
        await anchor.mkdir("launches/L1", stage="prepare")
    assert exc.value.stage == "prepare" and exc.value.category is ErrorCategory.TRANSIENT
    assert "No space left" in str(exc.value)


@pytest.mark.asyncio
async def test_directory_ops_refused_after_deactivation(platform: FakePlatform, clock: ManualClock) -> None:
    anchor = await _started(platform, clock)
    for _ in range(1000):
        platform.inject_fault("renew", _transient())
    await clock.advance(SETTINGS.grace_s)
    execs = platform.calls["exec"]
    with pytest.raises(FlotillaError) as exc:
        await anchor.mkdir("launches/L1/t1", stage="prepare")
    assert exc.value.category is ErrorCategory.LOST
    assert platform.calls["exec"] == execs  # 没有发出


# ───────────────────────────── 审查回归 ─────────────────────────────


@pytest.mark.asyncio
async def test_start_fails_if_grace_elapsed_during_start(platform: FakePlatform, clock: ManualClock) -> None:
    # 创建本身耗时超过 G（平台慢、后端排队）：start 不能返回一个已停用的锚点。
    platform.set_latency("create", SETTINGS.grace_s + 5.0)
    anchor = Anchor(platform, clock, "L1", SETTINGS)
    with pytest.raises(FlotillaError):
        await clock.run(anchor.start())
    assert platform.instances() == []


@pytest.mark.asyncio
async def test_unmapped_renew_timeout_uses_grace(
    platform: FakePlatform, clock: ManualClock, monkeypatch: pytest.MonkeyPatch
) -> None:
    # 后端漏把单次请求超时映射成 FlotillaError：只算这次没续上，宽限期内重试，不立即停用。
    anchor = await _started(platform, clock)
    original = platform.renew
    failures = [3]

    async def flaky(iid: str, expires_at: datetime) -> None:
        if failures[0]:
            failures[0] -= 1
            raise TimeoutError("httpx read timeout")
        await original(iid, expires_at)

    monkeypatch.setattr(platform, "renew", flaky)
    await clock.advance(SETTINGS.grace_s * 2)
    assert not anchor.deactivated.is_set()
    anchor.ensure_alive("create")


@pytest.mark.asyncio
async def test_directory_op_in_flight_is_cancelled_on_deactivation(platform: FakePlatform, clock: ManualClock) -> None:
    for _ in range(1000):
        platform.inject_fault("renew", _transient())
    anchor = await _started(platform, clock)
    platform.set_latency("exec", 1000.0)
    op = asyncio.create_task(anchor.mkdir("launches/L1/t1", stage="prepare"))
    await clock.advance(SETTINGS.grace_s)
    assert op.done()
    with pytest.raises(FlotillaError) as exc:
        await op
    assert exc.value.category is ErrorCategory.LOST and exc.value.stage == "prepare"
