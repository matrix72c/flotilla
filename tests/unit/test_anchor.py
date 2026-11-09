"""Unit tests for `Anchor`: startup order, renewal, grace-period deactivation, directory ops."""

from __future__ import annotations

import asyncio
from datetime import datetime

import pytest

from flotilla.core import labels
from flotilla.core.anchor import Anchor, AnchorSettings
from flotilla.platform.base import (
    ErrorCategory,
    ExecOutcomeUnknown,
    ExecResult,
    FlotillaError,
    InstanceHandle,
    InstanceStatus,
    ProcessSpec,
)
from flotilla.testing.platform import FakePlatform, ManualClock, SharedTree

SETTINGS = AnchorSettings(image="anchor@sha256:abc")


def _transient() -> FlotillaError:
    return FlotillaError("503", stage="run", category=ErrorCategory.TRANSIENT, retryable=True)


async def _started(platform: FakePlatform, clock: ManualClock, settings: AnchorSettings = SETTINGS) -> Anchor:
    anchor = Anchor(platform, clock, "L1", settings)
    await clock.run(anchor.start())
    return anchor


# ───────────────────────────── Startup ─────────────────────────────


@pytest.mark.asyncio
async def test_start_waits_until_listed(platform: FakePlatform, clock: ManualClock) -> None:
    anchor = await _started(platform, clock)
    # list only shows it after list_visibility_s (30s); start does not return before that.
    assert clock.monotonic() >= platform.caps.list_visibility_s
    [state] = await platform.list(labels.anchor_labels("L1"))
    assert state.iid == anchor.iid
    anchor.ensure_alive("prepare")  # does not raise


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
    assert not task.done()  # instance stays PENDING
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
    await clock.advance(5.0)  # created, not yet in the list
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


@pytest.mark.asyncio
@pytest.mark.parametrize("unknown", [False, True])
async def test_start_waits_for_command_readiness_without_replaying_work(
    platform: FakePlatform, clock: ManualClock, unknown: bool
) -> None:
    seen: list[tuple[str, ...]] = []

    def execute(_iid: str, proc: ProcessSpec) -> ExecResult:
        seen.append(proc.argv)
        if len(seen) < 3 or proc.argv != ("true",):
            if unknown:
                raise ExecOutcomeUnknown("command response was lost")
            raise _transient()
        return ExecResult(0, b"", b"")

    platform.set_exec_handler(execute)
    anchor = await _started(platform, clock)
    assert seen == [("true",)] * 3
    with pytest.raises(FlotillaError):
        await anchor.execute(("compiler",), stage="prepare", timeout_s=10)
    assert seen == [("true",)] * 3 + [("compiler",)]
    await anchor.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("outcome", ["auth", "nonretryable", "nonzero", "timed_out", "unavailable", "hung"])
async def test_start_readiness_failure_cleans_up(platform: FakePlatform, clock: ManualClock, outcome: str) -> None:
    seen = 0

    if outcome == "hung":
        platform.set_latency("exec", 10000)

    def execute(_iid: str, proc: ProcessSpec) -> ExecResult:
        nonlocal seen
        seen += 1
        assert proc.argv == ("true",)
        if outcome == "unavailable":
            raise _transient()
        if outcome in ("auth", "nonretryable"):
            raise FlotillaError(
                "unavailable",
                stage="run",
                category=ErrorCategory.INVALID if outcome == "auth" else ErrorCategory.TRANSIENT,
                retryable=False,
            )
        return ExecResult(1 if outcome == "nonzero" else 0, b"", b"", timed_out=outcome == "timed_out")

    platform.set_exec_handler(execute)
    anchor = Anchor(platform, clock, "L1", SETTINGS)
    with pytest.raises(FlotillaError):
        await clock.run(anchor.start())
    assert not platform.instances()
    assert not anchor._ready
    if outcome == "hung":
        assert seen == 0
    elif outcome != "unavailable":
        assert seen == 1


# ───────────────────────────── Renewal and deactivation ─────────────────────────────


@pytest.mark.asyncio
async def test_renews_every_interval(platform: FakePlatform, clock: ManualClock) -> None:
    anchor = await _started(platform, clock)
    assert anchor.iid is not None
    before = platform.calls["renew"]
    await clock.advance(300.0)
    assert platform.calls["renew"] - before == pytest.approx(300.0 / SETTINGS.renew_interval_s, abs=1)
    anchor.ensure_alive("create")
    # Renewal pushes the expiry to now + ttl.
    state = await platform.get(anchor.iid)
    assert state.expires_at is not None
    assert (state.expires_at - clock.now()).total_seconds() > SETTINGS.ttl_s - SETTINGS.renew_interval_s


@pytest.mark.asyncio
async def test_transient_renew_failures_within_grace_are_tolerated(platform: FakePlatform, clock: ManualClock) -> None:
    anchor = await _started(platform, clock)
    for _ in range(5):  # 5 x 5s retry interval, far below G
        platform.inject_fault("renew", _transient())
    await clock.advance(200.0)
    anchor.ensure_alive("create")
    assert not anchor.deactivated.is_set()


@pytest.mark.asyncio
async def test_deactivates_exactly_at_grace(platform: FakePlatform, clock: ManualClock) -> None:
    # Make every renewal fail before start: t0 is the create request time 0, deactivation is exactly at t0 + G.
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
    # No more renewals after deactivation.
    renews = platform.calls["renew"]
    await clock.advance(600.0)
    assert platform.calls["renew"] == renews


@pytest.mark.asyncio
async def test_grace_counts_from_last_successful_renew_send(platform: FakePlatform, clock: ManualClock) -> None:
    anchor = await _started(platform, clock)
    await clock.advance(SETTINGS.renew_interval_s * 2 - clock.monotonic() % SETTINGS.renew_interval_s)
    t0 = clock.monotonic()  # exactly at a successful renewal
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
    platform.set_latency("renew", 10_000.0)  # later renewal requests hang, neither returning nor failing
    # The last successful renewal is no later than now, so deactivation happens by G at the latest, without waiting for
    # the hung request.
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
    platform.clear_faults()  # platform recovers
    await clock.advance(600.0)
    with pytest.raises(FlotillaError):
        anchor.ensure_alive("create")


@pytest.mark.asyncio
async def test_ensure_alive_checks_time_without_waiting_for_loop(platform: FakePlatform, clock: ManualClock) -> None:
    anchor = await _started(platform, clock)
    for _ in range(1000):
        platform.inject_fault("renew", _transient())
    # The renewal loop is sleeping and G has passed: ensure_alive judges by time itself.
    clock.jump(SETTINGS.grace_s)  # only move the clock, do not drive the event loop
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
    await anchor.close()  # idempotent
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


# ───────────────────────────── Directory operations ─────────────────────────────


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
async def test_rename_reports_conflict_when_target_exists(
    platform: FakePlatform, clock: ManualClock, tree: SharedTree
) -> None:
    """`mv -n` exits 0 silently when the target exists, so a concurrent conflict is detected by "source still present".

    Verified on real BusyBox 1.36: `mv -Tn src dest` returns 0 and moves nothing when dest already exists.
    """
    anchor = await _started(platform, clock)
    tree.dirs.update({"share", "share/staging", "share/staging/a", "share/releases", "share/releases/a"})
    with pytest.raises(FlotillaError) as exc:
        await anchor.rename("share/staging/a", "share/releases/a", stage="prepare")
    assert exc.value.category is ErrorCategory.INVALID and not exc.value.retryable
    assert "rename had no effect" in str(exc.value)
    assert "share/staging/a" in tree.dirs  # source was not moved
    assert "mv share/staging/a share/releases/a" not in tree.events


@pytest.mark.asyncio
async def test_rename_moves_when_target_absent(platform: FakePlatform, clock: ManualClock, tree: SharedTree) -> None:
    anchor = await _started(platform, clock)
    tree.dirs.update({"share/staging/a", "share/releases"})
    await anchor.rename("share/staging/a", "share/releases/a", stage="prepare")
    assert "share/releases/a" in tree.dirs and "share/staging/a" not in tree.dirs


@pytest.mark.asyncio
async def test_stat_reads_mode_owner_and_size(platform: FakePlatform, clock: ManualClock, tree: SharedTree) -> None:
    """mode/uid/gid/size parsed from `ls -ln -d`; `share verify` relies on it to compare modes."""
    anchor = await _started(platform, clock)
    tree.files.add("share/releases/r1/bin/busybox")
    tree.modes["share/releases/r1/bin/busybox"] = "0755"
    tree.owners["share/releases/r1/bin/busybox"] = "0:0"
    platform.tree_contents["share/releases/r1/bin/busybox"] = b"x" * 17
    info = await anchor.stat("share/releases/r1/bin/busybox", stage="run")
    assert info.mode == 0o755 and info.uid == 0 and info.gid == 0 and info.size == 17


@pytest.mark.asyncio
@pytest.mark.parametrize("failure", ["unknown", "transient", "rate_limited"])
@pytest.mark.parametrize("recover", [False, True])
async def test_stat_transport_retries_are_bounded_and_preserve_cause(
    platform: FakePlatform, clock: ManualClock, failure: str, recover: bool
) -> None:
    anchor = await _started(platform, clock)
    errors: list[FlotillaError] = []
    sent: list[float] = []
    start = clock.monotonic()

    def execute(_iid: str, proc: ProcessSpec) -> ExecResult:
        assert proc.argv == ("/.flotilla/bin/busybox", "stat", "-c", "%a %u %g %s", "--", "/flotilla-root/result")
        sent.append(clock.monotonic() - start)
        if recover and len(sent) == 5:
            return ExecResult(0, b"6755 1001 1002 17\n", b"")
        error = (
            ExecOutcomeUnknown("stat response lost", command_id=f"stat-{len(sent)}")
            if failure == "unknown"
            else FlotillaError(
                "stat transport unavailable",
                stage="run",
                category=ErrorCategory.TRANSIENT if failure == "transient" else ErrorCategory.RATE_LIMITED,
                retryable=True,
            )
        )
        errors.append(error)
        raise error

    platform.set_exec_handler(execute)
    try:
        if recover:
            info = await clock.run(anchor.stat("result", stage="prepare"))
            assert (info.mode, info.uid, info.gid, info.size) == (0o6755, 1001, 1002, 17)
        else:
            with pytest.raises(FlotillaError) as failure_info:
                await clock.run(anchor.stat("result", stage="prepare"))
            assert failure_info.value.stage == "prepare"
            assert failure_info.value.__cause__ is errors[-1]
            assert failure_info.value.category is errors[-1].category
        assert sent == [0.0, 0.5, 1.5, 3.5, 7.5]
    finally:
        await anchor.close()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("category", "retryable"),
    [
        (ErrorCategory.INVALID, False),  # Includes authentication/authorization failures.
        (ErrorCategory.INVALID, True),
        (ErrorCategory.NOT_FOUND, True),
        (ErrorCategory.LOST, True),
        (ErrorCategory.CAPACITY, True),
        (ErrorCategory.IMAGE, True),
        (ErrorCategory.SERVICE, True),
        (ErrorCategory.TRANSIENT, False),
        (ErrorCategory.RATE_LIMITED, False),
    ],
)
async def test_stat_does_not_retry_other_transport_errors(
    platform: FakePlatform, clock: ManualClock, category: ErrorCategory, retryable: bool
) -> None:
    anchor = await _started(platform, clock)
    error = FlotillaError("not recoverable by stat", stage="run", category=category, retryable=retryable)
    platform.inject_fault("exec", error)
    before = platform.calls["exec"]
    try:
        with pytest.raises(FlotillaError) as failure:
            await clock.run(anchor.stat("result", stage="prepare"))
        assert platform.calls["exec"] - before == 1
        assert failure.value.stage == "prepare" and failure.value.__cause__ is error
    finally:
        await anchor.close()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "result",
    [
        ExecResult(1, b"", b"No such file"),
        ExecResult(0, b"644 0 0 17", b"", timed_out=True),
        ExecResult(0, b"bad metadata", b""),
        ExecResult(0, b"644 0 0 -1", b""),
        ExecResult(0, b"\xff", b""),
    ],
    ids=["nonzero", "timed-out", "malformed", "invalid-size", "non-ascii"],
)
async def test_stat_does_not_retry_command_results(
    platform: FakePlatform, clock: ManualClock, result: ExecResult
) -> None:
    anchor = await _started(platform, clock)
    platform.set_exec_handler(lambda _iid, _proc: result)
    before = platform.calls["exec"]
    try:
        with pytest.raises(FlotillaError) as failure:
            await clock.run(anchor.stat("result", stage="prepare"))
        assert failure.value.stage == "prepare"
        assert platform.calls["exec"] - before == 1
    finally:
        await anchor.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("latency", [29.75, 30.0, 35.0])
async def test_stat_does_not_admit_retry_after_thirty_seconds(
    platform: FakePlatform, clock: ManualClock, latency: float
) -> None:
    anchor = await _started(platform, clock)
    platform.set_latency("exec", latency)
    error = ExecOutcomeUnknown("slow stat response lost")
    platform.inject_fault("exec", error)
    before = platform.calls["exec"]
    try:
        with pytest.raises(FlotillaError) as failure:
            await clock.run(anchor.stat("result", stage="prepare"))
        assert platform.calls["exec"] - before == 1
        assert failure.value.stage == "prepare" and failure.value.__cause__ is error
    finally:
        await anchor.close()


@pytest.mark.asyncio
async def test_stat_retry_window_does_not_cancel_an_admitted_attempt(
    platform: FakePlatform, clock: ManualClock, monkeypatch: pytest.MonkeyPatch
) -> None:
    anchor = await _started(platform, clock)
    original_exec = platform.exec
    calls = 0
    started = clock.monotonic()

    async def execute(handle: InstanceHandle, proc: ProcessSpec) -> ExecResult:
        nonlocal calls
        calls += 1
        if calls == 1:
            raise ExecOutcomeUnknown("first stat response lost")
        await clock.sleep(35.0)
        return await original_exec(handle, proc)

    platform.set_exec_handler(lambda _iid, _proc: ExecResult(0, b"644 0 0 17", b""))
    monkeypatch.setattr(platform, "exec", execute)
    try:
        assert (await clock.run(anchor.stat("result", stage="prepare"))).size == 17
        assert calls == 2
        assert clock.monotonic() - started == 35.5
    finally:
        await anchor.close()


@pytest.mark.asyncio
async def test_stat_rechecks_retry_admission_after_delayed_wakeup(platform: FakePlatform, clock: ManualClock) -> None:
    anchor = await _started(platform, clock)
    error = ExecOutcomeUnknown("stat response lost")
    platform.inject_fault("exec", error)
    before = platform.calls["exec"]
    task = asyncio.create_task(anchor.stat("result", stage="prepare"))
    try:
        await clock.advance(0)
        assert not task.done() and platform.calls["exec"] - before == 1
        clock.jump(30.0)
        await clock.advance(0)
        with pytest.raises(FlotillaError) as failure:
            await clock.run(task)
        assert failure.value.__cause__ is error
        assert platform.calls["exec"] - before == 1
    finally:
        await anchor.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("stop", ["cancel", "deactivate"])
async def test_stat_retry_wait_stops_on_cancellation_or_deactivation(
    platform: FakePlatform, clock: ManualClock, stop: str
) -> None:
    anchor = await _started(platform, clock)
    platform.inject_fault("exec", ExecOutcomeUnknown("stat response lost"))
    before = platform.calls["exec"]
    task = asyncio.create_task(anchor.stat("result", stage="prepare"))
    try:
        await clock.advance(0.25)
        assert not task.done() and platform.calls["exec"] - before == 1
        if stop == "cancel":
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task
        else:
            anchor.deactivated.set()
            await clock.advance(0)
            assert task.done()  # Do not wait for the remaining backoff to expire.
            with pytest.raises(FlotillaError) as failure:
                await task
            assert failure.value.category is ErrorCategory.LOST and failure.value.stage == "prepare"
        await clock.advance(10)
        assert platform.calls["exec"] - before == 1
    finally:
        await anchor.close()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "operation",
    [
        "execute",
        "mkdir",
        "remove",
        "copy_tree",
        "copy_contents",
        "copy_source_tree",
        "listdir",
        "chmod",
        "chown",
        "unpack",
        "rename",
        "exists",
    ],
)
@pytest.mark.parametrize("unknown", [False, True])
async def test_non_stat_operations_never_replay_transport_failures(
    platform: FakePlatform, clock: ManualClock, operation: str, unknown: bool
) -> None:
    anchor = await _started(platform, clock)
    error = ExecOutcomeUnknown("response lost") if unknown else _transient()
    before = platform.calls["exec"]
    platform.inject_fault("exec", error)
    args = {
        "execute": (("helper",),),
        "mkdir": ("dest",),
        "remove": ("dest",),
        "copy_tree": ("source", "dest"),
        "copy_contents": ("source", "dest"),
        "copy_source_tree": ("source", "dest"),
        "listdir": ("source",),
        "chmod": ("dest", 0o755),
        "chown": ("dest", 1, 1),
        "unpack": ("source.tar", "dest"),
        "rename": ("source", "dest"),
        "exists": ("source",),
    }[operation]
    kwargs = {"timeout_s": 10} if operation == "execute" else {}
    try:
        with pytest.raises(FlotillaError):
            await clock.run(getattr(anchor, operation)(*args, stage="prepare", **kwargs))
        assert platform.calls["exec"] - before == 1
    finally:
        await anchor.close()


@pytest.mark.asyncio
async def test_download_does_not_add_outer_transport_retries(platform: FakePlatform, clock: ManualClock) -> None:
    anchor = await _started(platform, clock)
    error = _transient()  # The platform's idempotent HTTP retry budget is already exhausted.
    platform.inject_fault("read_file", error)
    before = platform.calls["read_file"]
    try:
        with pytest.raises(FlotillaError) as failure:
            await clock.run(anchor.download("result", stage="prepare"))
        assert failure.value.stage == "prepare" and failure.value.__cause__ is error
        assert platform.calls["read_file"] - before == 1
    finally:
        await anchor.close()


@pytest.mark.asyncio
async def test_stat_reports_missing_as_error(platform: FakePlatform, clock: ManualClock, tree: SharedTree) -> None:
    anchor = await _started(platform, clock)
    with pytest.raises(FlotillaError):
        await anchor.stat("share/releases/nope", stage="run")


@pytest.mark.asyncio
async def test_upload_writes_as_root_and_restores_owner_before_mode(
    platform: FakePlatform, clock: ManualClock, tree: SharedTree, monkeypatch: pytest.MonkeyPatch
) -> None:
    anchor = await _started(platform, clock)
    try:
        await anchor.mkdir("staging/bin", stage="prepare")
        original = platform.write_file
        operations: list[str] = []

        async def write(handle: InstanceHandle, path: str, data: bytes, *, mode: int, uid: int, gid: int) -> None:
            # staging is owned by root; a non-root write identity gets Permission denied on current execd.
            assert (uid, gid) == (0, 0)
            operations.append("write")
            await original(handle, path, data, mode=mode, uid=uid, gid=gid)

        def execute(iid: str, proc: ProcessSpec) -> ExecResult:
            operations.append(proc.argv[0])
            result = tree._exec(iid, proc)
            if proc.argv[0] == "chown":
                tree.modes["staging/bin/tool"] = "0755"  # kernel clears setuid/setgid
            return result

        monkeypatch.setattr(platform, "write_file", write)
        platform.set_exec_handler(execute)
        await anchor.upload("staging/bin/tool", b"tool", uid=1001, gid=1002, mode=0o6755, stage="prepare")
        assert operations == ["write", "chown", "chmod"]
        assert tree.content("staging/bin/tool") == b"tool"
        assert tree.owners["staging/bin/tool"] == "1001:1002"
        assert tree.modes["staging/bin/tool"] == "6755"
    finally:
        await anchor.close()


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
    assert platform.calls["exec"] == execs  # nothing was sent


# ───────────────────────────── Review regressions ─────────────────────────────


@pytest.mark.asyncio
async def test_start_fails_if_grace_elapsed_during_start(platform: FakePlatform, clock: ManualClock) -> None:
    # Creation itself takes longer than G (slow platform, backend queueing): start must not return a deactivated anchor.
    platform.set_latency("create", SETTINGS.grace_s + 5.0)
    anchor = Anchor(platform, clock, "L1", SETTINGS)
    with pytest.raises(FlotillaError):
        await clock.run(anchor.start())
    assert platform.instances() == []


@pytest.mark.asyncio
async def test_unmapped_renew_timeout_uses_grace(
    platform: FakePlatform, clock: ManualClock, monkeypatch: pytest.MonkeyPatch
) -> None:
    # The backend failed to map a single request timeout to FlotillaError: only this renewal is missed, retried within
    # the grace period, not deactivated immediately.
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
