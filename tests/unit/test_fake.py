"""Unit tests for `FakePlatform` and `ManualClock`.

Covers the platform semantics later core tests rely on: list visibility delay, create after-effect
faults, idempotent deletion, fault injection, background process progression, files, exec handlers,
links, and an end-to-end run of one full trial lifecycle.
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
from tests.support.platform import FakePlatform, ManualClock


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


# ───────────────────────────── Lifecycle and visibility ─────────────────────────────


@pytest.mark.asyncio
async def test_create_then_get_is_immediate(platform: FakePlatform) -> None:
    handle = await platform.create(_spec())
    state = await platform.get(handle.iid)
    assert state.iid == handle.iid
    assert state.status is InstanceStatus.RUNNING


@pytest.mark.asyncio
async def test_list_respects_visibility_window(platform: FakePlatform, clock: ManualClock) -> None:
    handle = await platform.create(_spec(labels={"flotilla/trial": "t1"}))
    # Just created: visible to get, not to list (list_visibility_s not reached).
    assert (await platform.get(handle.iid)).status is InstanceStatus.RUNNING
    assert await platform.list({"flotilla/trial": "t1"}) == []
    # Still invisible while within the delay.
    await clock.advance(29.0)
    assert await platform.list({"flotilla/trial": "t1"}) == []
    # Visible once the delay is reached.
    await clock.advance(1.0)
    listed = await platform.list({"flotilla/trial": "t1"})
    assert [s.iid for s in listed] == [handle.iid]


@pytest.mark.asyncio
async def test_list_filters_by_labels(platform: FakePlatform, clock: ManualClock) -> None:
    a = await platform.create(_spec(labels={"flotilla/trial": "t1", "flotilla/role": "unit"}))
    await platform.create(_spec(labels={"flotilla/trial": "t2", "flotilla/role": "unit"}))
    await clock.advance(30.0)
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
    await platform.delete(handle.iid)  # second call does not raise
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
async def test_set_status_is_visible_through_get_and_list(platform: FakePlatform, clock: ManualClock) -> None:
    handle = await platform.create(_spec(labels={"flotilla/trial": "t1"}))
    platform.set_status(handle.iid, InstanceStatus.TERMINAL, reason="OOMKilled")
    state = await platform.get(handle.iid)
    assert state.status is InstanceStatus.TERMINAL and state.reason == "OOMKilled"
    await clock.advance(30.0)
    [listed] = await platform.list({"flotilla/trial": "t1"})
    assert listed.status is InstanceStatus.TERMINAL


# ───────────────────────────── Fault injection and call counting ─────────────────────────────


@pytest.mark.asyncio
async def test_inject_fault_fires_once(platform: FakePlatform) -> None:
    platform.inject_fault("create", _fault())
    with pytest.raises(FlotillaError):
        await platform.create(_spec())
    # The next call is back to normal.
    handle = await platform.create(_spec())
    assert (await platform.get(handle.iid)).status is InstanceStatus.RUNNING


@pytest.mark.asyncio
async def test_create_then_fail_leaves_instance(platform: FakePlatform, clock: ManualClock) -> None:
    # Platform accepted but the response was lost: create raises, yet the instance really exists
    # (the core scenario of create reconciliation).
    platform.inject_create_then_fail(_fault())
    with pytest.raises(FlotillaError):
        await platform.create(_spec(labels={"flotilla/trial": "t1"}))
    assert len(platform.instances()) == 1
    await clock.advance(30.0)
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
    assert (await platform.get(handle.iid)).status is InstanceStatus.RUNNING  # third call recovers


def test_after_effect_only_allowed_for_create(platform: FakePlatform) -> None:
    with pytest.raises(ValueError):
        platform.inject_fault("delete", _fault(), after_effect=True)


@pytest.mark.asyncio
async def test_calls_count_every_invocation_including_faulted(platform: FakePlatform) -> None:
    # Core tests rely on this to assert "a call did not happen" (neither release nor liveness
    # confirmation calls list).
    platform.inject_fault("get", _fault())
    handle = await platform.create(_spec())
    with pytest.raises(FlotillaError):
        await platform.get(handle.iid)
    await platform.get(handle.iid)
    await platform.delete(handle.iid)
    assert platform.calls == {"create": 1, "get": 2, "delete": 1}
    assert platform.calls["list"] == 0


# ───────────────────────────── Processes, files, exec, link ─────────────────────────────


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
    assert not status.found  # execd restart lost the record -> core treats it as lost


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
    assert (await platform.exec(handle, _proc())).exit_code == 0  # exits 0 by default

    def failing(iid: str, proc: ProcessSpec) -> ExecResult:
        return ExecResult(exit_code=7, stdout=b"", stderr=b"boom")

    platform.set_exec_handler(failing)
    result = await platform.exec(handle, _proc())
    assert result.exit_code == 7 and result.stderr == b"boom"


@pytest.mark.asyncio
async def test_internal_address_stable(platform: FakePlatform) -> None:
    handle = await platform.create(_spec())
    addr = await platform.internal_address(handle)
    assert await platform.internal_address(handle) == addr  # stable for the lifetime


@pytest.mark.asyncio
async def test_link_applies_topology_idempotently(platform: FakePlatform) -> None:
    a = await platform.create(_spec())
    b = await platform.create(_spec())
    outsider = await platform.create(_spec())
    members = {"web": a, "db": b}
    topo = Topology(networks={"default": frozenset({"web", "db"})})
    await platform.link(members, topo)
    await platform.link(members, topo)  # idempotent: repeating with the same args does not raise
    assert platform.topology_of(a.iid) == topo
    assert platform.topology_of(b.iid) == topo
    assert platform.topology_of(outsider.iid) is None


# ───────────────────────────── End-to-end trial lifecycle ─────────────────────────────


@pytest.mark.asyncio
async def test_full_trial_walkthrough(platform: FakePlatform, clock: ManualClock) -> None:
    """Run through the startup phases to confirm the fake can drive a multi-unit trial."""
    labels = {"flotilla/trial": "t1", "flotilla/launch": "L1"}

    # create: two units created in parallel.
    web = await platform.create(_spec(labels=labels))
    db = await platform.create(_spec(labels=labels))
    for h in (web, db):
        assert (await platform.get(h.iid)).status is InstanceStatus.RUNNING

    # address: fetch internal addresses.
    web_addr = await platform.internal_address(web)
    db_addr = await platform.internal_address(db)
    assert web_addr != db_addr

    # wire: a single link.
    await platform.link({"web": web, "db": db}, Topology(networks={"default": frozenset({"web", "db"})}))

    # hosts: write, read back and compare.
    hosts = f"{db_addr} db\n{web_addr} web\n".encode()
    await platform.write_file(web, "/etc/hosts", hosts, mode=0o644, uid=0, gid=0)
    assert await platform.read_file(web, "/etc/hosts") == hosts

    # start: launch the workload in the background; health checks go through exec.
    pid = await platform.start_process(db, _proc(("mysqld",), timeout_s=None))
    assert (await platform.process_status(db, pid)).running
    assert (await platform.exec(db, _proc(("true",)))).exit_code == 0

    # Running: reconciliation via list must wait for the visibility delay.
    assert await platform.list({"flotilla/trial": "t1"}) == []
    await clock.advance(30.0)
    assert len(await platform.list({"flotilla/trial": "t1"})) == 2

    # release: delete everything.
    for h in (web, db):
        await platform.delete(h.iid)
    assert platform.instances() == []
