"""Compose `/dev/kvm` and `/dev/net/tun` survive parsing, preparation and admission on the privileged runtime."""

from __future__ import annotations

from dataclasses import replace
from pathlib import Path
from typing import Any

import pytest
from pydantic import ValidationError

from flotilla import Flotilla
from flotilla.build.driver import build_task
from flotilla.capabilities import Item, ProbeSettings
from flotilla.compose.normalize import normalize
from flotilla.compose.task import load_task
from flotilla.config import FlotillaConfig
from flotilla.core.anchor import Anchor, AnchorSettings
from flotilla.manifest import Manifest
from flotilla.platform.base import (
    Capabilities,
    ErrorCategory,
    ExecResult,
    ExternalPolicy,
    FlotillaError,
    InstanceSpec,
    ProcessSpec,
    Resources,
)
from flotilla.probe import _Run
from flotilla.probe_checks import device
from flotilla.runtime.manager import Manager
from flotilla.runtime.planning import to_plan
from flotilla.runtime.types import RunRequest, Task
from flotilla.scan import scan
from flotilla.testing.build import FakeBuilder, _ctx, _task
from flotilla.testing.config import config_data
from flotilla.testing.platform import FakePlatform, ManualClock, SharedTree
from flotilla.testing.reports import ok, report
from flotilla.testing.runtime import deployment as deployment
from flotilla.testing.runtime import task

OSWORLD = {
    "image": "r.io/osworld",
    "devices": ["/dev/kvm", "/dev/net/tun"],
    "cap_add": ["NET_ADMIN"],
}


def _service(**fields: Any) -> dict[str, Any]:
    return {"services": {"main": {"image": "r.io/app", **fields}}}


def _rejected(raw: dict[str, Any]) -> list[str]:
    return [f.path for f in normalize(raw, {}).findings if f.kind == "reject"]


@pytest.mark.parametrize(
    "devices",
    [
        ["/dev/kvm", "/dev/net/tun"],
        ["/dev/kvm:/dev/kvm", "/dev/net/tun:/dev/net/tun:rwm"],
        [  # official `compose config` long form
            {"source": "/dev/kvm", "target": "/dev/kvm", "permissions": "rwm"},
            {"source": "/dev/net/tun", "target": "/dev/net/tun", "permissions": "rwm"},
        ],
    ],
)
def test_kvm_and_tun_are_normalized(devices: list[object]) -> None:
    project = normalize(_service(devices=devices, cap_add=["NET_ADMIN"]), {})
    assert not project.rejected, project.findings
    assert project.services["main"].devices == {"kvm", "tun"}
    assert [(f.path, f.kind) for f in project.findings] == [("services.main.cap_add", "equivalent")]


@pytest.mark.parametrize(
    ("devices", "path"),
    [
        (["/dev/sda"], "services.main.devices[0]"),
        (["/dev/fuse"], "services.main.devices[0]"),
        (["/dev/kvm:/dev/kvm0"], "services.main.devices[0]"),
        (["/dev/kvm:/dev/kvm:r"], "services.main.devices[0]"),
        (["/dev/kvm:/dev/kvm:rwm:x"], "services.main.devices[0]"),
        ([{"source": "/dev/kvm", "target": "/dev/kvm", "cgroup": "x"}], "services.main.devices[0].cgroup"),
        ([7], "services.main.devices[0]"),
        ("/dev/kvm", "services.main.devices"),
    ],
)
def test_other_devices_are_rejected(devices: object, path: str) -> None:
    assert _rejected(_service(devices=devices)) == [path]


@pytest.mark.parametrize(
    ("devices", "cap_add"),
    [
        (["/dev/kvm"], ["NET_ADMIN"]),
        ([], ["NET_ADMIN"]),
        (["/dev/net/tun"], ["SYS_ADMIN"]),
        (["/dev/net/tun"], ["NET_ADMIN", "SYS_ADMIN"]),
        (["/dev/net/tun"], "NET_ADMIN"),
    ],
)
def test_cap_add_is_only_net_admin_with_tun(devices: list[str], cap_add: object) -> None:
    assert _rejected(_service(devices=devices, cap_add=cap_add)) == ["services.main.cap_add"]


def test_cap_prefix_is_accepted() -> None:
    assert not _rejected(_service(devices=["/dev/net/tun"], cap_add=["CAP_NET_ADMIN"]))


def _device_caps(caps: Capabilities) -> Capabilities:
    return replace(caps, privileged_runtime=True, devices=frozenset({"kvm", "tun"}))


def test_scan_requires_device_evidence(caps: Capabilities) -> None:
    project = normalize({"services": {"main": OSWORLD}}, {})
    result = scan("osworld", project, caps)
    assert result.status == "rejected"
    assert [f.path for f in result.findings if f.kind == "reject"] == ["services.main.devices"]
    assert scan("osworld", project, replace(_device_caps(caps), devices=frozenset({"kvm"}))).status == "rejected"
    assert scan("osworld", project, _device_caps(caps)).status == "accepted"
    readonly = normalize({"services": {"main": {**OSWORLD, "read_only": True}}}, {})
    assert scan("osworld", readonly, replace(_device_caps(caps), readonly_rootfs=True)).status == "rejected"


def test_devices_roundtrip_to_runtime_plan(tmp_path: Path) -> None:
    compose = (
        "services:\n  main:\n    image: r.io/app\n    devices: [/dev/net/tun, /dev/kvm]\n"
        "    mem_limit: 1g\n    cpus: 1\n"
    )
    builder = FakeBuilder()
    ctx = _ctx(builder=builder)
    outcome = build_task(load_task(_task(tmp_path, "rejected", compose=compose)), tmp_path / "no", ctx)
    assert outcome.status == "rejected" and "devices" in (outcome.reason or "")
    ctx = replace(ctx, caps=_device_caps(ctx.caps))
    outcome = build_task(load_task(_task(tmp_path, "dev", compose=compose)), tmp_path / "out", ctx)
    assert outcome.manifest_path, outcome.reason
    manifest = Manifest.load_json(outcome.manifest_path.read_bytes())
    assert manifest.units["main"].devices == ["kvm", "tun"] and not manifest.units["main"].privileged
    unit = to_plan(manifest, runtime_params={}).units["main"]
    assert unit.devices == {"kvm", "tun"} and unit.privileged_runtime and not unit.privileged
    assert not builder.requests


def test_manifest_devices_are_sorted_and_known() -> None:
    unit = task().manifest().units["main"]
    data = unit.model_dump(mode="json")
    with pytest.raises(ValidationError, match="sorted"):
        type(unit).model_validate({**data, "devices": ["tun", "kvm"]})
    with pytest.raises(ValidationError):
        type(unit).model_validate({**data, "devices": ["fuse"]})


def test_instance_spec_devices_need_the_privileged_runtime() -> None:
    spec = InstanceSpec(
        image="r.io/app@sha256:" + "0" * 64,
        entrypoint=("sleep",),
        labels={},
        timeout_seconds=60,
        resources=Resources(cpu="1", memory="1Gi"),
        volumes=(),
        external=ExternalPolicy(mode="none"),
        privileged=True,
        devices=frozenset({"kvm", "tun"}),
    )
    with pytest.raises(ValueError, match="privileged runtime"):
        replace(spec, privileged=False)
    with pytest.raises(ValueError, match="unknown devices"):
        replace(spec, devices=frozenset({"fuse"}))  # type: ignore[arg-type]


@pytest.mark.asyncio
async def test_device_units_open_on_the_privileged_runtime(
    deployment: tuple[FlotillaConfig, FakePlatform, SharedTree],
) -> None:
    cfg, platform, _ = deployment
    manifest = task().manifest()
    unit = manifest.units["main"].model_copy(update={"devices": ["kvm", "tun"]})
    prepared = Task(manifest.model_copy(update={"units": {"main": unit}}))
    with pytest.raises(FlotillaError, match="devices") as failure:
        Manager(cfg, platform=platform).plan(RunRequest(prepared, "devices"))
    assert failure.value.category is ErrorCategory.INVALID and platform.calls["create"] == 0
    platform.caps = replace(platform.caps, privileged_runtime=True, devices=frozenset({"kvm"}))
    with pytest.raises(FlotillaError, match="tun"):
        Manager(cfg, platform=platform).plan(RunRequest(prepared, "devices"))
    platform.caps = _device_caps(platform.caps)
    async with Flotilla(deployment=cfg) as fleet:
        async with fleet.environment(prepared) as env:
            spec = platform.spec_of((await env.info()).services[0].instance_id)
            assert spec.privileged and spec.devices == {"kvm", "tun"}
        assert (await fleet.drain(2)).clean


def test_report_devices_need_privileged_scoped_evidence() -> None:
    measured = {"runtime.kvm": {**ok(), "scope": "privileged"}, "runtime.tun": {**ok(), "scope": "privileged"}}
    assert report(**measured).to_capabilities().devices == {"fuse", "kvm", "tun"}
    ordinary = {"runtime.kvm": ok(), "runtime.tun": ok()}
    assert report(**ordinary).to_capabilities().devices == {"fuse"}
    unprivileged = {**measured, "runtime.privileged_network": {"status": "failed", "scope": "privileged"}}
    assert not report(**unprivileged).to_capabilities().devices
    no_docker = {**measured, "runtime.privileged_runtime": {"status": "skipped", "scope": "privileged"}}
    caps = report(**no_docker).to_capabilities()
    assert caps.devices == {"fuse", "kvm", "tun"} and caps.privileged_runtime
    cfg = FlotillaConfig.from_mapping(config_data())
    caps = cfg.platform_capabilities(report(**no_docker))
    assert caps.devices == {"fuse", "kvm", "tun"} and caps.privileged_runtime


SETTINGS = ProbeSettings(
    image="probe",
    privileged_image="dind",
    share_release="r1",
    endpoint="http://os.test",
    evidence_profile="test",
)


@pytest.mark.asyncio
@pytest.mark.parametrize(("name", "node"), [("kvm", "/dev/kvm"), ("tun", "/dev/net/tun")])
@pytest.mark.parametrize("exit_code", [0, 1])
async def test_device_probe_creates_one_privileged_instance_per_device(
    platform: FakePlatform, clock: ManualClock, name: str, node: str, exit_code: int
) -> None:
    SharedTree(platform)
    anchor = Anchor(platform, clock, "device-test", AnchorSettings(image="anchor"))
    await clock.run(anchor.start())
    run = _Run(platform, clock, anchor, SETTINGS, "p1")
    platform.caps = _device_caps(platform.caps)  # the fake has no evidence-free probe create path
    original = platform.exec_handler
    specs: dict[str, InstanceSpec] = {}
    platform.on_create = lambda iid, spec: specs.__setitem__(iid, spec)
    scripts: list[str] = []

    def execute(iid: str, proc: ProcessSpec) -> ExecResult:
        if iid == anchor.iid:
            return original(iid, proc)
        assert proc.uid == 0
        scripts.append(proc.argv[-1])
        return ExecResult(exit_code, b"", b"")

    platform.set_exec_handler(execute)
    result = await clock.run(device(run, name))  # type: ignore[arg-type]
    assert isinstance(result, Item) and result.scope == "privileged"
    assert result.status == ("passed" if exit_code == 0 else "failed")
    (spec,) = specs.values()
    assert spec.privileged and spec.devices == {name} and spec.image == "dind"
    assert len(scripts) == 1 and node in scripts[0]
    assert not run._created and platform.iids() == [anchor.iid]
