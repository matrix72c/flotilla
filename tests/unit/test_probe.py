"""`flotilla probe` (Architecture section 14): verdict logic, declared-item merging and cleanup on the fake platform.

On the fake platform, anchor directory operations go to `SharedTree`; unit shell commands are answered by `ProbeShell`
the way a compliant deployment would, and each item can be made non-compliant to check that probe reports it as
unsatisfied instead of raising. See backend docs section 8 for validating real deployments.
"""

from __future__ import annotations

import asyncio

import pytest
from pydantic import ValidationError

from flotilla.capabilities import CapabilityReport, Item
from flotilla.core.anchor import Anchor, AnchorSettings
from flotilla.platform.base import ErrorCategory, ExecResult, FlotillaError, InstanceHandle, ProcessSpec
from flotilla.probe import _INBOUND_TOKEN, PROBE_LABEL, Declared, ProbeSettings, _mounted_subdir, probe
from tests.support.platform import FakePlatform, ManualClock, SharedTree

SETTINGS = ProbeSettings(
    image="reg/unit@sha256:" + "b" * 64, share_release="r1", endpoint="http://os.test", hosts_recheck_s=5.0
)


def _item(ok: bool) -> Item:
    return Item(ok=ok, source="declared", evidence="declared by deployer")


DECLARED = Declared(
    deployment="fake",
    list_visibility_s=30.0,
    link=_item(False),
    link_udp=_item(False),
    inbound_isolation=_item(False),
    external_forms=frozenset({"none", "allowlist"}),
    implicit_egress=("10.96.0.10:53/udp",),
    implicit_egress_declared=_item(True),
    shared_volume=_item(True),
    missing_subpath=_item(False),
    no_auto_mount=_item(False),
    volume_file=_item(False),
    exec_auth=_item(False),
    diagnostics=_item(False),
    privileged_runtime=_item(True),
    multi_container=_item(False),
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
        if "nc -z" in script:
            if "notool" in self.broken:
                return _out("NOTOOL\n")
            return _out("self=1\npublic=0\n" if "egress" in self.broken else "self=1\npublic=1\n")
        if _INBOUND_TOKEN in script:  # inbound: self always ok; external only if not isolated (broken "inbound")
            if "127.0.0.1" in script:
                return _out(f"{_INBOUND_TOKEN}\n")
            return _out(f"{_INBOUND_TOKEN}\n" if "inbound" in self.broken else "")
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
    platform: FakePlatform, clock: ManualClock, *, broken: frozenset[str] = frozenset(), exit_code: int = 7
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
        return await clock.run(probe(platform, clock, anchor, DECLARED, SETTINGS))
    finally:
        finisher.cancel()
        await asyncio.wait({finisher})
        await anchor.close()


# ───────────────────────────── Compliant deployment ─────────────────────────────


@pytest.mark.asyncio
async def test_compliant_deployment_satisfies_required_items(platform: FakePlatform, clock: ManualClock) -> None:
    report = await run_probe(platform, clock)
    assert report.missing_required() == []
    for item in (
        report.lifecycle.create_get_delete,
        report.lifecycle.label_list,
        report.lifecycle.renew,
        report.exec.exec_identity,
        report.exec.files,
        report.exec.placeholder_entry,
        report.exec.background,
        report.network.external_none,
        report.storage.read_only,
        report.storage.subdir_isolation,
        report.storage.dir_management,
        report.runtime.memory_limit,
        report.runtime.cpu_limit,
        report.runtime.image_digest,
    ):
        assert item.ok and item.source == "probed", item
    assert report.network.internal_address == "exec"
    assert report.endpoint == "http://os.test" and report.deployment == "fake"


@pytest.mark.asyncio
async def test_declared_items_are_copied_verbatim(platform: FakePlatform, clock: ManualClock) -> None:
    report = await run_probe(platform, clock)
    assert report.network.link == DECLARED.link and report.runtime.exec_auth == DECLARED.exec_auth
    assert report.network.external_forms == DECLARED.external_forms
    assert report.lifecycle.list_visibility_s == 30.0
    assert report.security_gaps() == ["inbound_isolation", "exec_auth", "no_auto_mount"]


@pytest.mark.asyncio
async def test_label_list_latency_is_measured(platform: FakePlatform, clock: ManualClock) -> None:
    # The fake platform makes list results visible caps.list_visibility_s after the request (30s, the declared bound).
    report = await run_probe(platform, clock)
    assert "listed by label 30.0s after create request" in report.lifecycle.label_list.evidence


@pytest.mark.asyncio
async def test_cleans_up_every_instance_and_directory(platform: FakePlatform, clock: ManualClock) -> None:
    await run_probe(platform, clock)
    assert [i for i in platform.iids() if PROBE_LABEL in platform.spec_of(i).labels] == []


# ───────────────────────────── Non-compliant answers are reported unsatisfied, not raised ─────────────────────────────


@pytest.mark.parametrize(
    ("broken", "item"),
    [
        ("identity", "exec.exec_identity"),
        ("timeout", "exec.exec_identity"),
        ("memory", "runtime.memory_limit"),
        ("egress", "network.external_none"),
        ("isolation", "storage.subdir_isolation"),
        ("mount-root", "storage.subdir_isolation"),  # mount source is the root, not the mounted subdirectory
    ],
)
@pytest.mark.asyncio
async def test_non_compliant_answer_reported_as_unsatisfied(
    platform: FakePlatform, clock: ManualClock, broken: str, item: str
) -> None:
    report = await run_probe(platform, clock, broken=frozenset({broken}))
    section, field = item.split(".")
    assert getattr(getattr(report, section), field).ok is False
    assert report.missing_required()  # all of these are required items


@pytest.mark.asyncio
async def test_inbound_not_isolated_is_security_gap(platform: FakePlatform, clock: ManualClock) -> None:
    # External instances can reach members: inbound isolation probes unsatisfied and becomes a security gap (provider
    # rejects the deployment by default).
    report = await run_probe(platform, clock, broken=frozenset({"inbound"}))
    assert report.network.inbound_isolation.ok is False
    assert report.network.inbound_isolation.source == "declared"
    assert "inbound_isolation" in report.security_gaps()


@pytest.mark.asyncio
async def test_inbound_falls_back_to_declared_without_platform_cidrs(
    platform: FakePlatform, clock: ManualClock
) -> None:
    # Cannot build an external instance with open egress (external=any needs platform_cidrs): skip, use the declared
    # value, create no instance.
    from flotilla.core.anchor import Anchor, AnchorSettings
    from flotilla.platform.base import ErrorCategory, FlotillaError
    from flotilla.probe import _Run

    anchor = Anchor(platform, clock, "Lp", AnchorSettings(image="anchor@sha256:abc"))
    await clock.run(anchor.start())
    run = _Run(platform, clock, anchor, SETTINGS, run_id="pX")
    platform.inject_fault(
        "create",
        FlotillaError("any requires platform_cidrs", stage="create", category=ErrorCategory.INVALID, retryable=False),
    )
    declared = DECLARED.inbound_isolation
    result = await clock.run(run.inbound(declared))
    assert result == declared
    assert [i for i in platform.iids() if PROBE_LABEL in platform.spec_of(i).labels] == []
    await anchor.close()


@pytest.mark.asyncio
async def test_wrong_background_exit_code_is_unsatisfied(platform: FakePlatform, clock: ManualClock) -> None:
    report = await run_probe(platform, clock, exit_code=0)
    assert not report.exec.background.ok and "(expected 7)" in report.exec.background.evidence


@pytest.mark.asyncio
async def test_hosts_rewritten_by_platform_is_unsatisfied(platform: FakePlatform, clock: ManualClock) -> None:
    real_read = platform.read_file
    reads = 0

    async def flaky_read(handle: InstanceHandle, path: str) -> bytes:
        nonlocal reads
        data = await real_read(handle, path)
        if path == "/etc/hosts":
            reads += 1
            if reads == 3:  # by the third read (after a delay) the platform has reverted it
                return b"127.0.0.1 localhost\n"
        return data

    platform.read_file = flaky_read  # type: ignore[method-assign]
    report = await run_probe(platform, clock)
    assert not report.exec.files.ok and ", rewritten after" in report.exec.files.evidence


@pytest.mark.asyncio
async def test_platform_error_aborts_probe_and_still_cleans_up(platform: FakePlatform, clock: ManualClock) -> None:
    # "The test did not run" must not become "capability unsatisfied": on error the whole probe raises and created
    # instances are still deleted. The fault targets start_process, since the anchor's own background renewal also
    # calls renew and would consume a fault on renew first.
    error = FlotillaError("503", stage="run", category=ErrorCategory.TRANSIENT, retryable=True)
    platform.inject_fault("start_process", error)
    with pytest.raises(FlotillaError):
        await run_probe(platform, clock)
    assert [i for i in platform.iids() if PROBE_LABEL in platform.spec_of(i).labels] == []


@pytest.mark.asyncio
async def test_tag_image_does_not_verify_digest_pull(platform: FakePlatform, clock: ManualClock) -> None:
    # The image capability requires pulling by digest: a successful create from a tag-referenced image does not count.
    tagged = ProbeSettings(image="reg/unit:latest", share_release="r1", endpoint="http://os.test", hosts_recheck_s=5.0)
    tree = SharedTree(platform)
    anchor = Anchor(platform, clock, "Lt", AnchorSettings(image="anchor@sha256:abc"))
    await clock.run(anchor.start())
    await anchor.mkdir("launches/Lt", stage="prepare")
    ProbeShell(platform, tree, anchor)
    platform.initial_files["/etc/hosts"] = b"127.0.0.1 localhost\n"
    finisher = asyncio.ensure_future(_finish_background(platform, clock, 7))
    try:
        report = await clock.run(probe(platform, clock, anchor, DECLARED, tagged))
    finally:
        finisher.cancel()
        await asyncio.wait({finisher})
        await anchor.close()
    assert report.exec.placeholder_entry.ok and not report.runtime.image_digest.ok
    assert "digest pull not verified" in report.runtime.image_digest.evidence


@pytest.mark.asyncio
async def test_missing_network_tool_aborts_instead_of_reporting_unreachable(
    platform: FakePlatform, clock: ManualClock
) -> None:
    # With the tool missing, "unreachable" must not count as "none policy in effect": the test itself failed.
    with pytest.raises(FlotillaError, match="nc -z"):
        await run_probe(platform, clock, broken=frozenset({"notool"}))
    assert [i for i in platform.iids() if PROBE_LABEL in platform.spec_of(i).labels] == []


@pytest.mark.asyncio
async def test_label_list_beyond_declared_limit_is_unsatisfied(platform: FakePlatform, clock: ManualClock) -> None:
    platform.set_latency("list", 45.0)  # every listing is slower than the declared 30s limit
    report = await run_probe(platform, clock)
    assert not report.lifecycle.label_list.ok


@pytest.mark.parametrize(
    "change",
    [
        {"external_forms": frozenset({"allowlist"})},  # probe always tests none
        {"list_visibility_s": 0},
        {"link_max_members": 0},
        {"exec_auth": Item(ok=True, source="probed")},  # items in the declaration file can only be declared
    ],
)
def test_declared_file_validated_before_probing(change: dict[str, object]) -> None:
    with pytest.raises(ValidationError):
        Declared.model_validate({**DECLARED.model_dump(), **change})


@pytest.mark.parametrize(
    ("entry", "ok"),
    [
        ("srv:/export/launches/L/p1/visible|/", True),  # NFS: subpath in the mount source
        ("/dev/sda1|/data/launches/L/p1/visible", True),  # bind / K8s subPath: subpath in the mount root
        ("srv:/export/launches/L|/p1/visible", True),
        ("srv:/export|/", False),  # mounted the shared root itself
        ("srv:/export/launches/L/p1/visible-other|/", False),
        ("|", False),
    ],
)
def test_mounted_subdir_reads_source_and_root(entry: str, ok: bool) -> None:
    assert _mounted_subdir(entry, "p1/visible") is ok


@pytest.mark.parametrize(
    ("broken", "ok", "ratio"),
    [
        ("cpu-double", True, 2.0),  # limit is 2x the given value: satisfied (not lower), ratio recorded as-is
        ("cpu-max", True, None),  # unlimited
        ("cpu-low", False, 0.5),  # below the given value: unsatisfied
    ],
)
@pytest.mark.asyncio
async def test_cpu_limit_at_least_requested_and_ratio_recorded(
    platform: FakePlatform, clock: ManualClock, broken: str, ok: bool, ratio: float | None
) -> None:
    report = await run_probe(platform, clock, broken=frozenset({broken}))
    assert report.runtime.cpu_limit.ok is ok and report.runtime.cpu_limit_ratio == ratio
    assert report.missing_required() == []  # CPU is a degraded item and does not affect "usable"
