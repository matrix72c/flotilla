"""Unit tests for `Trial`: stage order, link skipped for single service, recreate before wire, hosts, per-stage
deadlines and error attribution."""

from __future__ import annotations

from collections.abc import AsyncIterator
from dataclasses import replace

import pytest
import pytest_asyncio

from flotilla.core import labels
from flotilla.core.anchor import Anchor, AnchorSettings
from flotilla.core.deletion import Deleter
from flotilla.core.plan import (
    Dependency,
    ExecContext,
    Healthcheck,
    Mount,
    Network,
    TrialPlan,
    TrialVolume,
    Unit,
)
from flotilla.core.reaper import Reaper, ReaperSettings
from flotilla.core.trial import Trial, TrialSettings
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
from tests.support.platform import FakePlatform, ManualClock, SharedTree

CTX = ExecContext(env={"PATH": "/usr/bin:/bin"}, uid=0, gid=0, cwd="/")
SETTINGS = TrialSettings(share_release="2026-09-28.1", external=ExternalPolicy(mode="any"))
SYSTEM_HOSTS = b"127.0.0.1\tlocalhost\n10.99.0.1\tsandbox-host\n"


def _unit(**kw: object) -> Unit:
    base = Unit(image="x@sha256:1", resources=Resources(cpu="1", memory="1Gi"), context=CTX, command=("serve",))
    return replace(base, **kw)  # type: ignore[arg-type]


def _three() -> TrialPlan:
    names = {"main": frozenset({"main"}), "app": frozenset({"app"}), "db": frozenset({"db"})}
    return TrialPlan(
        task="web-app-db",
        task_files=None,
        units={"main": _unit(), "app": _unit(), "db": _unit(healthcheck=Healthcheck(test=("check",), interval_s=2))},
        networks={"default": Network(internal=False, members=frozenset(names), names=names)},
        depends_on={"app": {"db": Dependency("service_healthy")}},
        external_units=frozenset(names),
    )


class Shell:
    """exec handler: anchor commands go to SharedTree; unit commands succeed (healthchecks too)."""

    def __init__(self, platform: FakePlatform, tree: SharedTree, anchor_iid: str) -> None:
        self._tree_exec = platform.exec_handler  # SharedTree already installed
        self._anchor = anchor_iid
        self.unit_calls: list[tuple[str, ProcessSpec]] = []
        platform.set_exec_handler(self._exec)
        self.health = ExecResult(exit_code=0, stdout=b"", stderr=b"")

    def _exec(self, iid: str, proc: ProcessSpec) -> ExecResult:
        if iid == self._anchor:
            return self._tree_exec(iid, proc)
        self.unit_calls.append((iid, proc))
        if proc.argv == ("check",):
            return self.health
        return ExecResult(exit_code=0, stdout=b"", stderr=b"")


@pytest_asyncio.fixture
async def anchor(platform: FakePlatform, clock: ManualClock, tree: SharedTree) -> AsyncIterator[Anchor]:
    a = Anchor(platform, clock, "L1", AnchorSettings(image="anchor@sha256:abc"))
    await clock.run(a.start())
    await a.mkdir("launches/L1", stage="prepare")
    yield a
    await a.close()


@pytest.fixture
def reaper(platform: FakePlatform, clock: ManualClock, anchor: Anchor) -> Reaper:
    return Reaper(platform, clock, anchor, Deleter(platform, clock), ReaperSettings())


@pytest.fixture
def shell(platform: FakePlatform, tree: SharedTree, anchor: Anchor) -> Shell:
    assert anchor.iid is not None
    return Shell(platform, tree, anchor.iid)


def _new_trial(
    platform: FakePlatform,
    clock: ManualClock,
    anchor: Anchor,
    reaper: Reaper,
    plan: TrialPlan,
    settings: TrialSettings = SETTINGS,
) -> Trial:
    reaper.track("t1")
    return Trial(platform, clock, anchor, reaper, plan, "t1", settings)


def _seed_hosts(platform: FakePlatform) -> None:
    """The /etc/hosts the platform presets on every new instance."""
    platform.initial_files["/etc/hosts"] = SYSTEM_HOSTS


# ───────────────────────────── Happy path ─────────────────────────────


@pytest.mark.asyncio
async def test_three_unit_trial_reaches_ready(
    platform: FakePlatform, clock: ManualClock, anchor: Anchor, reaper: Reaper, shell: Shell
) -> None:
    _seed_hosts(platform)
    trial = _new_trial(platform, clock, anchor, reaper, _three())
    ready = await clock.run(trial.start())
    assert set(ready.handles) == {"main", "app", "db"}
    assert set(ready.pids) == {"main", "app", "db"}
    assert set(ready.timings) == {"create", "address", "hosts", "wire", "start"}
    assert not ready.recreated
    # One link, members are all units, topology is the default network.
    assert platform.calls["link"] == 1
    topo = platform.topology_of(ready.handles["db"].iid)
    assert topo == Topology(networks={"default": frozenset({"main", "app", "db"})})
    # A normal trial does not list by label: only the few calls from anchor startup.
    lists_before_release = platform.calls["list"]
    reaper.release("t1")
    await clock.run(reaper.close())
    assert platform.calls["list"] == lists_before_release


@pytest.mark.asyncio
async def test_spec_has_placeholder_entrypoint_labels_share_and_policy(
    platform: FakePlatform, clock: ManualClock, anchor: Anchor, reaper: Reaper, shell: Shell
) -> None:
    _seed_hosts(platform)
    settings = replace(SETTINGS, extra_labels={"owner": "rl", "flotilla/trial": "spoof"})
    ready = await clock.run(_new_trial(platform, clock, anchor, reaper, _three(), settings).start())
    spec = platform.spec_of(ready.handles["app"].iid)
    assert spec.entrypoint == ("/.flotilla/bin/busybox", "sleep", "2147483647")
    assert spec.labels[labels.TRIAL] == "t1" and spec.labels[labels.UNIT] == "app"
    assert spec.labels[labels.ROLE] == labels.ROLE_UNIT and spec.labels[labels.LAUNCH] == "L1"
    assert spec.labels["x-owner"] == "rl" and spec.labels["x-flotilla/trial"] == "spoof"  # cannot be overridden
    assert spec.volumes[0].key == "share/releases/2026-09-28.1" and spec.volumes[0].read_only
    assert spec.external.mode == "any"
    assert spec.timeout_seconds == min(SETTINGS.ttl_s, platform.caps.max_ttl_seconds or SETTINGS.ttl_s)


@pytest.mark.asyncio
async def test_hosts_block_written_and_system_entries_kept(
    platform: FakePlatform, clock: ManualClock, anchor: Anchor, reaper: Reaper, shell: Shell
) -> None:
    _seed_hosts(platform)
    ready = await clock.run(_new_trial(platform, clock, anchor, reaper, _three()).start())
    content = (platform.file(ready.handles["main"].iid, "/etc/hosts") or b"").decode()
    assert content.startswith(SYSTEM_HOSTS.decode())
    for unit in ("main", "app", "db"):
        assert f"{ready.addresses[unit]}\t{unit}\n" in content


@pytest.mark.asyncio
async def test_business_starts_only_after_wire_and_hosts(
    platform: FakePlatform,
    clock: ManualClock,
    anchor: Anchor,
    reaper: Reaper,
    shell: Shell,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # PRD N1: main processes start only after the topology is in effect and hosts are written.
    _seed_hosts(platform)
    order: list[str] = []
    for name in ("link", "write_file", "start_process"):
        original = getattr(platform, name)

        async def wrapped(*a: object, _orig: object = original, _name: str = name, **kw: object) -> object:
            order.append(_name)
            return await _orig(*a, **kw)  # type: ignore[operator]

        monkeypatch.setattr(platform, name, wrapped)
    await clock.run(_new_trial(platform, clock, anchor, reaper, _three()).start())
    first_start = order.index("start_process")
    assert order.index("link") < first_start
    assert order.count("write_file") == 3 and all(i < first_start for i, op in enumerate(order) if op == "write_file")


@pytest.mark.asyncio
async def test_single_unit_trial_skips_link(
    platform: FakePlatform, clock: ManualClock, anchor: Anchor, reaper: Reaper, shell: Shell
) -> None:
    _seed_hosts(platform)
    plan = TrialPlan(
        task="solo",
        task_files=None,
        units={"main": _unit()},
        networks={"default": Network(internal=False, members=frozenset({"main"}), names={"main": frozenset({"main"})})},
        external_units=frozenset({"main"}),
    )
    ready = await clock.run(_new_trial(platform, clock, anchor, reaper, plan).start())
    assert platform.calls["link"] == 0
    assert "wire" not in ready.timings
    assert platform.spec_of(ready.handles["main"].iid).group is None  # no networking, no group


@pytest.mark.asyncio
async def test_linked_units_share_one_group_per_trial(
    platform: FakePlatform, clock: ManualClock, anchor: Anchor, reaper: Reaper, shell: Shell
) -> None:
    _seed_hosts(platform)
    ready = await clock.run(_new_trial(platform, clock, anchor, reaper, _three()).start())
    groups = {platform.spec_of(h.iid).group for h in ready.handles.values()}
    assert len(groups) == 1 and None not in groups
    reaper.track("t2")
    other = await clock.run(Trial(platform, clock, anchor, reaper, _three(), "t2", SETTINGS).start())
    assert platform.spec_of(other.handles["main"].iid).group not in groups  # one group per trial


# ───────────────────────────── prepare ─────────────────────────────


@pytest.mark.asyncio
async def test_prepare_creates_trial_dirs_before_units(
    platform: FakePlatform, clock: ManualClock, anchor: Anchor, reaper: Reaper, tree: SharedTree, shell: Shell
) -> None:
    _seed_hosts(platform)
    # Like real cp, the fake cp does not create intermediate directories: the parent launches/L1/t1/binds of binds/0
    # must be created by copy_tree first.
    tree.dirs.update({"tasks", "tasks/bk", "tasks/bk/binds", "tasks/bk/binds/0", "tasks/bk/binds/0/conf.d"})
    import io
    import tarfile

    data = io.BytesIO()
    with tarfile.open(fileobj=data, mode="w") as archive:
        entry = tarfile.TarInfo("fixture.txt")
        entry.size = 4
        archive.addfile(entry, io.BytesIO(b"seed"))
    tree.files.add("tasks/bk/seeds/static.tar")
    platform.tree_contents["tasks/bk/seeds/static.tar"] = data.getvalue()
    plan = replace(
        _three(),
        task_files="tasks/bk",
        volumes=(
            TrialVolume("static", seed="seeds/static.tar"),
            TrialVolume("data", owner=(999, 999)),
            TrialVolume("binds/0", copy_from="binds/0"),
        ),
        units={
            **_three().units,
            "app": _unit(mounts=(Mount("trial", "static", "/srv", False), Mount("trial", "binds/0", "/etc/x", False))),
            "db": _unit(
                healthcheck=Healthcheck(test=("check",), interval_s=2),
                mounts=(Mount("trial", "data", "/var/lib", False),),
            ),
        },
    )
    creates_before = platform.calls["create"]
    tree.events.clear()
    ready = await clock.run(_new_trial(platform, clock, anchor, reaper, plan).start())
    assert {"launches/L1/t1/static", "launches/L1/t1/data", "launches/L1/t1/binds/0"} <= tree.dirs
    assert "launches/L1/t1/binds/0/conf.d" in tree.dirs  # subdirectory copied along, not as binds/0/0
    assert "unpack tasks/bk/seeds/static.tar launches/L1/t1/static" in tree.events
    assert tree.content("launches/L1/t1/static/fixture.txt") == b"seed"
    assert "cp tasks/bk/binds/0 launches/L1/t1/binds/0" in tree.events
    assert tree.owners["launches/L1/t1/data"] == "999:999"
    assert platform.calls["create"] - creates_before == 3
    app_keys = [v.key for v in platform.spec_of(ready.handles["app"].iid).volumes]
    assert "launches/L1/t1/static" in app_keys and "launches/L1/t1/binds/0" in app_keys
    # After release the directory is deleted once all instances are gone.
    reaper.release("t1")
    await clock.run(reaper.close())
    assert not any(d.startswith("launches/L1/t1") for d in tree.dirs)


@pytest.mark.asyncio
async def test_prepare_failure_creates_no_units(
    platform: FakePlatform, clock: ManualClock, anchor: Anchor, reaper: Reaper, tree: SharedTree, shell: Shell
) -> None:
    plan = replace(_three(), task_files="tasks/bk", volumes=(TrialVolume("static", seed="seeds/missing.tar"),))
    creates = platform.calls["create"]
    with pytest.raises(FlotillaError) as exc:
        await clock.run(_new_trial(platform, clock, anchor, reaper, plan).start())
    assert exc.value.stage == "prepare"
    assert platform.calls["create"] == creates
    # A half-built trial directory is registered and deleted on release.
    reaper.release("t1")
    await clock.run(reaper.close())
    assert not any(d.startswith("launches/L1/t1") for d in tree.dirs)


# ───────────────────────────── create and recreate ─────────────────────────────


@pytest.mark.asyncio
async def test_unit_terminal_before_running_is_recreated_once(
    platform: FakePlatform,
    clock: ManualClock,
    anchor: Anchor,
    reaper: Reaper,
    shell: Shell,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _seed_hosts(platform)
    failed: list[str] = []

    def fail_first_db(iid: str, spec: InstanceSpec) -> None:
        if spec.labels.get(labels.UNIT) == "db" and not failed:
            platform.set_status(iid, InstanceStatus.TERMINAL, reason="Failed")
            failed.append(iid)

    platform.on_create = fail_first_db
    ready = await clock.run(_new_trial(platform, clock, anchor, reaper, _three()).start())
    assert ready.recreated == {"db"}
    assert ready.handles["db"].iid != failed[0]
    assert platform.spec_of(ready.handles["db"].iid).group == platform.spec_of(ready.handles["main"].iid).group
    await clock.advance(5.0)
    assert failed[0] not in {s.iid for s in platform.instances()}  # old instance deleted


@pytest.mark.asyncio
async def test_non_retryable_create_error_is_not_recreated(
    platform: FakePlatform, clock: ManualClock, anchor: Anchor, reaper: Reaper, shell: Shell
) -> None:
    platform.inject_fault(
        "create", FlotillaError("no image", stage="run", category=ErrorCategory.IMAGE, retryable=False)
    )
    with pytest.raises(FlotillaError) as exc:
        await clock.run(_new_trial(platform, clock, anchor, reaper, _three()).start())
    assert (exc.value.stage, exc.value.category) == ("create", ErrorCategory.IMAGE)


@pytest.mark.asyncio
async def test_create_timeout_reports_create_stage(
    platform: FakePlatform, clock: ManualClock, anchor: Anchor, reaper: Reaper, shell: Shell
) -> None:
    platform.create_status = InstanceStatus.PENDING  # never RUNNING
    with pytest.raises(FlotillaError) as exc:
        await clock.run(_new_trial(platform, clock, anchor, reaper, _three()).start())
    assert exc.value.stage == "create" and exc.value.retryable
    # After failure, created instances are all owned by the reaper.
    reaper.release("t1")
    await clock.run(reaper.close())
    assert not [s for s in platform.instances() if s.labels.get(labels.ROLE) == labels.ROLE_UNIT]


# ───────────────────────────── Error attribution for wire / hosts / start ─────────────────────────────


@pytest.mark.asyncio
async def test_link_rejection_is_wire_invalid(
    platform: FakePlatform, clock: ManualClock, anchor: Anchor, reaper: Reaper, shell: Shell
) -> None:
    _seed_hosts(platform)
    platform.inject_fault("link", FlotillaError("413", stage="run", category=ErrorCategory.INVALID, retryable=False))
    with pytest.raises(FlotillaError) as exc:
        await clock.run(_new_trial(platform, clock, anchor, reaper, _three()).start())
    assert (exc.value.stage, exc.value.category) == ("wire", ErrorCategory.INVALID)
    assert platform.calls["start_process"] == 0


@pytest.mark.asyncio
async def test_link_timeout_is_wire_transient(
    platform: FakePlatform, clock: ManualClock, anchor: Anchor, reaper: Reaper, shell: Shell
) -> None:
    _seed_hosts(platform)
    platform.set_latency("link", 1000.0)
    with pytest.raises(FlotillaError) as exc:
        await clock.run(_new_trial(platform, clock, anchor, reaper, _three()).start())
    assert (exc.value.stage, exc.value.category, exc.value.retryable) == ("wire", ErrorCategory.TRANSIENT, True)


@pytest.mark.asyncio
async def test_hosts_readback_mismatch_fails(
    platform: FakePlatform,
    clock: ManualClock,
    anchor: Anchor,
    reaper: Reaper,
    shell: Shell,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _seed_hosts(platform)
    original = platform.write_file

    async def rewriting(handle, path, data, **kw):  # type: ignore[no-untyped-def]
        await original(handle, path, data + b"# platform rewrote me\n", **kw)

    monkeypatch.setattr(platform, "write_file", rewriting)
    with pytest.raises(FlotillaError) as exc:
        await clock.run(_new_trial(platform, clock, anchor, reaper, _three()).start())
    assert exc.value.stage == "hosts"
    assert platform.calls["start_process"] == 0


@pytest.mark.asyncio
async def test_unhealthy_service_fails_start_stage(
    platform: FakePlatform, clock: ManualClock, anchor: Anchor, reaper: Reaper, shell: Shell
) -> None:
    _seed_hosts(platform)
    shell.health = ExecResult(exit_code=1, stdout=b"", stderr=b"")
    with pytest.raises(FlotillaError) as exc:
        await clock.run(_new_trial(platform, clock, anchor, reaper, _three()).start())
    err = exc.value
    assert (err.stage, err.category, err.retryable, err.service) == ("start", ErrorCategory.SERVICE, False, "db")


@pytest.mark.asyncio
async def test_start_stage_timeout(
    platform: FakePlatform, clock: ManualClock, anchor: Anchor, reaper: Reaper, shell: Shell
) -> None:
    # Extreme setting where an always-timing-out healthcheck is never unhealthy: the start stage deadline backstops.
    _seed_hosts(platform)
    plan = replace(
        _three(),
        units={**_three().units, "db": _unit(healthcheck=Healthcheck(test=("check",), interval_s=2, retries=10_000))},
    )
    shell.health = ExecResult(exit_code=1, stdout=b"", stderr=b"")
    settings = replace(SETTINGS, start_max_s=300.0)
    with pytest.raises(FlotillaError) as exc:
        await clock.run(_new_trial(platform, clock, anchor, reaper, plan, settings).start())
    assert exc.value.stage == "start" and exc.value.category is ErrorCategory.TRANSIENT


@pytest.mark.asyncio
async def test_launch_deactivated_reports_lost(
    platform: FakePlatform, clock: ManualClock, anchor: Anchor, reaper: Reaper, shell: Shell
) -> None:
    for _ in range(1000):
        platform.inject_fault(
            "renew", FlotillaError("503", stage="run", category=ErrorCategory.TRANSIENT, retryable=True)
        )
    await clock.advance(AnchorSettings(image="x").grace_s)
    platform.clear_faults()
    with pytest.raises(FlotillaError) as exc:
        await clock.run(_new_trial(platform, clock, anchor, reaper, _three()).start())
    assert exc.value.category is ErrorCategory.LOST and exc.value.stage == "create"


@pytest.mark.asyncio
@pytest.mark.parametrize("unit", ["x" * 80, "web/app", "-svc", "svc_"])
async def test_bad_unit_label_rejected_before_any_create(
    platform: FakePlatform, clock: ManualClock, anchor: Anchor, reaper: Reaper, unit: str
) -> None:
    # Unit names go into the flotilla/unit label and cannot be rewritten (reclamation and reconciliation list by it);
    # invalid ones fail at construction, before any create is sent.
    names = {"main": frozenset({"main"}), unit: frozenset({unit})}
    plan = TrialPlan(
        task="t",
        task_files=None,
        units={"main": _unit(), unit: _unit()},
        networks={"default": Network(internal=False, members=frozenset(names), names=names)},
    )
    reaper.track("t1")
    creates = platform.calls["create"]
    with pytest.raises(ValueError):
        Trial(platform, clock, anchor, reaper, plan, "t1", SETTINGS)
    assert platform.calls["create"] == creates


@pytest.mark.asyncio
async def test_task_slug_and_user_labels_sanitized(
    platform: FakePlatform, clock: ManualClock, anchor: Anchor, reaper: Reaper, shell: Shell
) -> None:
    # Task path with "/" and too long; user label with spaces and ending in "-": all must be sanitized into values the
    # platform accepts (the fake validates with K8s rules).
    _seed_hosts(platform)
    plan = replace(_three(), task="fixture-suite/cvebench/" + "y" * 100)
    settings = replace(SETTINGS, extra_labels={"note": "hello world-"})
    ready = await clock.run(_new_trial(platform, clock, anchor, reaper, plan, settings).start())
    got = platform.spec_of(ready.handles["main"].iid).labels
    assert labels.is_valid_value(got[labels.TASK], platform.caps.max_label_value_len)
    assert got[labels.TASK].startswith("fixture-suite-cvebench-")
    assert labels.is_valid_value(got["x-note"], platform.caps.max_label_value_len)


def test_sanitize_keeps_distinct_values_distinct() -> None:
    a = labels.sanitize_value("tasks/" + "a" * 80 + "/1", 63)
    b = labels.sanitize_value("tasks/" + "a" * 80 + "/2", 63)
    assert a != b and len(a) <= 63 and len(b) <= 63
    assert labels.sanitize_value("ok-value", 63) == "ok-value"  # valid values kept as-is
    assert labels.is_valid_value(labels.sanitize_value("///", 63), 63)
