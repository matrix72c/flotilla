"""Measured probe behavior and cleanup on the fake platform.

On the fake platform, anchor directory operations go to `SharedTree`; unit shell commands are answered by `ProbeShell`
the way a compliant deployment would, and each item can be made non-compliant to check that probe reports it as
unsatisfied instead of raising. See backend docs section 8 for validating real deployments.
"""

from __future__ import annotations

import asyncio
from dataclasses import replace
from datetime import datetime, timedelta
from typing import Any

import pytest

from flotilla.capabilities import CapabilityReport
from flotilla.core.anchor import Anchor, AnchorSettings
from flotilla.platform.base import ErrorCategory, ExecResult, FlotillaError, InstanceHandle, InstanceSpec, ProcessSpec
from flotilla.probe import PROBE_LABEL, ProbeSettings, _mounted_subdir, _Run, probe
from flotilla.testing.platform import FakePlatform, ManualClock, SharedTree

SETTINGS = ProbeSettings(
    image="reg/unit@sha256:" + "b" * 64,
    share_release="r1",
    endpoint="http://os.test",
    evidence_profile="test-profile",
    hosts_recheck_s=5.0,
    link_members=2,
)


class ProbeShell:
    """Commands on units: answer by script content. Items in `broken` get non-compliant answers."""

    def __init__(self, platform: FakePlatform, tree: SharedTree, anchor: Anchor) -> None:
        self._platform = platform
        self._tree_exec = platform.exec_handler
        self._anchor = anchor
        self.broken: set[str] = set()
        platform.set_exec_handler(self._exec)

    def _exec(self, iid: str, proc: ProcessSpec) -> ExecResult:
        if iid == self._anchor.iid:
            return self._tree_exec(iid, proc)
        script = proc.argv[-1]
        if "flotilla tmpfs output probe" in script:
            return ExecResult(
                1 if "tmpfs-exit" in self.broken else 0,
                b"" if "tmpfs-output" in self.broken else b"flotilla-tmpfs-stdout\n",
                b"" if "tmpfs-stderr" in self.broken else b"flotilla-tmpfs-stderr\n",
            )
        if "flotilla shm_size probe" in script:
            return ExecResult(1 if "shm" in self.broken else 0, b"", b"")
        if "flotilla filesystem" in script:
            broken = "rootfs" if "rootfs probe" in script else "tmpfs"
            return ExecResult(1 if broken in self.broken else 0, b"", b"")
        if script.startswith("id -u"):
            out = f"{proc.uid}\n{proc.gid}\n{proc.cwd}\n{proc.env.get('PROBE_VAR', '')}\n"
            return _out(out if "identity" not in self.broken else "0\n0\n/\n\n")
        if script == "sleep 30":
            return ExecResult(exit_code=-1, stdout=b"", stderr=b"", timed_out="timeout" not in self.broken)
        if script.startswith("stat -c"):
            return _out("1000:1000 640\nowned\n")
        if "/sys/fs/cgroup/memory.max" in script:  # cgroup v2: memory.max and cpu.max
            memory = "1073741824" if "memory" in self.broken else "536870912"
            cpu = {"cpu-double": "200000 100000", "cpu-low": "50000 100000", "cpu-max": "max 100000"}.get(
                next((b for b in self.broken if b.startswith("cpu-")), ""), "100000 100000"
            )
            return _out(f"{memory}\n{cpu}\n")
        if script.startswith("ls -A /probe-ro"):
            listing = "inner\nsibling\n" if "isolation" in self.broken else "inner\n"
            run_id = self._platform.spec_of(iid).labels[PROBE_LABEL]
            # NFS mount shape: mount server:/export/<subpath> directly, mount root is /.
            source = "srv:/export" if "mount-root" in self.broken else f"srv:/export/launches/Lp/{run_id}/visible"
            return _out(f"{listing}ROOT={source}|/\n")
        return _out("")


def _out(text: str) -> ExecResult:
    return ExecResult(exit_code=0, stdout=text.encode(), stderr=b"")


async def _finish_background(platform: FakePlatform, clock: ManualClock, exit_code: int) -> None:
    """Fake-platform background processes exit only when advanced: once probe starts `sleep 3; exit 7`, mark it
    exited 3s later."""
    while True:
        for iid in platform.iids():
            for pid in platform.pids(iid):
                if platform.process_spec(iid, pid).argv[-1] == "sleep 3; exit 7":
                    await clock.sleep(3.0)
                    platform.finish_process(iid, pid, exit_code)
                    return
        await clock.sleep(0.5)


async def run_probe(
    platform: FakePlatform,
    clock: ManualClock,
    *,
    broken: frozenset[str] = frozenset(),
    exit_code: int = 7,
    settings: ProbeSettings = SETTINGS,
) -> CapabilityReport:
    tree = SharedTree(platform)
    anchor = Anchor(platform, clock, "Lp", AnchorSettings(image="anchor@sha256:abc"))
    await clock.run(anchor.start())
    await anchor.mkdir("launches/Lp", stage="prepare")
    shell = ProbeShell(platform, tree, anchor)
    shell.broken = set(broken)
    platform.initial_files["/etc/hosts"] = b"127.0.0.1 localhost\n"
    finisher = asyncio.ensure_future(_finish_background(platform, clock, exit_code))
    try:
        return await clock.run(probe(platform, clock, anchor, settings))
    finally:
        finisher.cancel()
        await asyncio.wait({finisher})
        await anchor.close()


@pytest.mark.asyncio
async def test_unmeasured_network_and_auth_never_become_passed(platform: FakePlatform, clock: ManualClock) -> None:
    value = await run_probe(platform, clock)
    assert value.lifecycle.create_get_delete.ok and value.lifecycle.renew.ok
    assert value.exec.exec_identity.ok and value.exec.background.ok
    assert value.runtime.memory_limit.ok
    assert not value.network.external_none.ok
    assert not value.network.network_acceptance.ok
    assert value.runtime.exec_auth.status == "skipped"
    assert not value.to_capabilities().supports_no_network
    assert not platform.iids()


@pytest.mark.asyncio
@pytest.mark.parametrize("broken,item", [("identity", "exec_identity"), ("timeout", "exec_identity")])
async def test_exec_behavior_not_json_echo(platform: FakePlatform, clock: ManualClock, broken: str, item: str) -> None:
    value = await run_probe(platform, clock, broken=frozenset({broken}))
    assert getattr(value.exec, item).status == "failed"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "broken", [frozenset(), frozenset({"rootfs"}), frozenset({"tmpfs-output"}), frozenset({"tmpfs"})]
)
async def test_filesystem_actual_behavior_and_cleanup(
    platform: FakePlatform, clock: ManualClock, broken: frozenset[str]
) -> None:
    platform.caps = replace(platform.caps, readonly_rootfs=True, native_tmpfs=True, shm_size=True)
    value = await run_probe(platform, clock, settings=replace(SETTINGS, filesystem_supported=True), broken=broken)
    assert value.runtime.readonly_rootfs.ok == ("rootfs" not in broken)
    assert value.storage.native_tmpfs.ok == (not broken & {"tmpfs", "tmpfs-output"})
    assert not platform.iids()


@pytest.mark.asyncio
async def test_command_failure_is_error_not_failed(platform: FakePlatform, clock: ManualClock) -> None:
    platform.inject_fault(
        "start_process", FlotillaError("secret-token", stage="run", category=ErrorCategory.TRANSIENT, retryable=True)
    )
    value = await run_probe(platform, clock)
    assert value.exec.background.status == "error"
    assert "secret-token" not in value.dump_json()
    assert not platform.iids()


@pytest.mark.asyncio
async def test_cpu_unreadable_is_not_unlimited(platform: FakePlatform, clock: ManualClock) -> None:
    anchor = Anchor(platform, clock, "Lp", AnchorSettings(image="anchor"))
    await clock.run(anchor.start())
    run = _Run(platform, clock, anchor, SETTINGS, "p1")
    platform.set_exec_handler(lambda *_: ExecResult(0, b"", b""))
    unit = await clock.run(run._create(run._spec("unit")))
    _, cpu, _ = await clock.run(run.resources(unit))
    assert cpu.status == "error"
    await clock.run(run.cleanup())
    await anchor.close()


@pytest.mark.asyncio
async def test_external_positive_control_failure_is_error(
    platform: FakePlatform, clock: ManualClock, monkeypatch: pytest.MonkeyPatch
) -> None:
    from flotilla import probe_network

    anchor = Anchor(platform, clock, "Lp", AnchorSettings(image="anchor"))
    await clock.run(anchor.start())
    settings = replace(SETTINGS, external_targets=(("192.0.2.1", 1234, "tcp"),))
    run = _Run(platform, clock, anchor, settings, "p1")

    async def unavailable(*args: object) -> str:
        return "blocked"

    monkeypatch.setattr(probe_network, "exchange", unavailable)
    none, allow = await clock.run(probe_network.egress(run))
    assert none.status == allow.status == "error"
    await clock.run(run.cleanup())
    await anchor.close()


@pytest.mark.asyncio
async def test_cleanup_on_cancellation(
    platform: FakePlatform, clock: ManualClock, monkeypatch: pytest.MonkeyPatch
) -> None:
    from flotilla import probe_checks

    anchor = Anchor(platform, clock, "Lp", AnchorSettings(image="anchor"))
    await clock.run(anchor.start())

    async def cancelled(run: _Run, _results: dict[str, Any]) -> None:
        await run._create(run._spec("pending"))
        raise asyncio.CancelledError()

    monkeypatch.setattr(probe_checks, "measure", cancelled)
    with pytest.raises(asyncio.CancelledError):
        await clock.run(probe(platform, clock, anchor, SETTINGS))
    assert [iid for iid in platform.iids() if PROBE_LABEL in platform.spec_of(iid).labels] == []
    await anchor.close()


@pytest.mark.asyncio
async def test_cleanup_reports_unconfirmed_instances(
    platform: FakePlatform, clock: ManualClock, monkeypatch: pytest.MonkeyPatch
) -> None:
    from flotilla import probe_checks

    anchor = Anchor(platform, clock, "Lp", AnchorSettings(image="anchor"))
    await clock.run(anchor.start())
    real_delete = platform.delete

    async def create(run: _Run, _results: dict[str, Any]) -> None:
        await run._create(run._spec("leak"))

        async def fail_delete(iid: str) -> None:
            raise RuntimeError("private response")

        monkeypatch.setattr(platform, "delete", fail_delete)

    monkeypatch.setattr(probe_checks, "measure", create)
    value = await clock.run(probe(platform, clock, anchor, SETTINGS))
    assert value.cleanup.status == "error"
    assert value.cleanup.measurements["unconfirmed_instances"]
    assert "private response" not in value.dump_json()
    monkeypatch.setattr(platform, "delete", real_delete)
    await anchor.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("ttl_error", [0, -120, 120])
async def test_ttl_accounts_for_create_latency_but_rejects_wrong_duration(
    platform: FakePlatform, clock: ManualClock, monkeypatch: pytest.MonkeyPatch, ttl_error: int
) -> None:
    anchor = Anchor(platform, clock, "Lp", AnchorSettings(image="anchor"))
    await clock.run(anchor.start())
    run = _Run(platform, clock, anchor, SETTINGS, "p1")
    create = platform.create

    async def delayed(spec: InstanceSpec) -> InstanceHandle:
        await clock.sleep(20)
        handle = await create(replace(spec, timeout_seconds=spec.timeout_seconds + ttl_error))
        await clock.sleep(20)
        return handle

    monkeypatch.setattr(platform, "create", delayed)
    result = await clock.run(run.lifecycle(60, 1800))
    assert result["ttl"].ok is (ttl_error == 0)
    assert "create request interval" in result["ttl"].evidence
    await clock.run(run.cleanup())
    await anchor.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("ttl_error,renew_error", [(0, 0), (-120, 0), (120, 0), (0, 120)])
async def test_ttl_bounds_startup_origin_and_requires_explicit_renewal(
    platform: FakePlatform,
    clock: ManualClock,
    monkeypatch: pytest.MonkeyPatch,
    ttl_error: int,
    renew_error: int,
) -> None:
    anchor = Anchor(platform, clock, "Lp", AnchorSettings(image="anchor"))
    await clock.run(anchor.start())
    run = _Run(platform, clock, anchor, SETTINGS, "p1")
    wait_running = run._wait_running
    renew = platform.renew

    async def delayed_running(handle: InstanceHandle) -> None:
        # Platform assigns the initial deadline during startup, after create has returned.
        await clock.sleep(30)
        await renew(handle.iid, clock.now() + timedelta(seconds=1800 + ttl_error))
        await wait_running(handle)

    async def wrong_renewal(iid: str, expires_at: datetime) -> None:
        await renew(iid, expires_at + timedelta(seconds=renew_error))

    monkeypatch.setattr(run, "_wait_running", delayed_running)
    monkeypatch.setattr(platform, "renew", wrong_renewal)
    result = await clock.run(run.lifecycle(60, 1800))
    assert result["ttl"].ok is (ttl_error == 0 and renew_error == 0)
    evidence = result["ttl"].measurements
    assert evidence["running_observed_at"] > evidence["create_returned_at"]
    assert evidence["natural_expiration_measured"] is False
    assert evidence["renew_target"]
    await clock.run(run.cleanup())
    await anchor.close()


@pytest.mark.parametrize(
    "entry,ok",
    [
        ("srv:/export/p1/visible|/", True),
        ("/dev/sda1|/data/p1/visible", True),
        ("srv:/export|/", False),
        ("srv:/export/p1/visible-other|/", False),
    ],
)
def test_mount_subdir(entry: str, ok: bool) -> None:
    assert _mounted_subdir(entry, "p1/visible") is ok
