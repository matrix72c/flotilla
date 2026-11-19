"""Compose `/dev/kvm` and `/dev/net/tun` survive parsing, planning and admission on the privileged runtime."""

from __future__ import annotations

from dataclasses import replace
from pathlib import Path
from typing import Any

import pytest

from flotilla import Flotilla
from flotilla.compose.normalize import normalize
from flotilla.compose.task import load_task
from flotilla.config import FlotillaConfig
from flotilla.platform.base import (
    Capabilities,
    ErrorCategory,
    ExternalPolicy,
    FlotillaError,
    InstanceSpec,
    Resources,
)
from flotilla.runtime.manager import Manager
from flotilla.runtime.types import RunRequest
from flotilla.scan import scan
from flotilla.testing.planning import APP, _caps, _task, plan
from flotilla.testing.platform import FakePlatform, SharedTree
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
    return [f.path for f in normalize(raw).findings if f.kind == "reject"]


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
    project = normalize(_service(devices=devices, cap_add=["NET_ADMIN"]))
    assert not project.rejected, project.findings
    assert project.services["main"].devices == {"kvm", "tun"}
    assert [(f.path, f.kind) for f in project.findings] == [("services.main.cap_add", "equivalent")]


@pytest.mark.parametrize(
    ("devices", "path"),
    [
        (["/dev/sda"], "services.main.devices[0]"),
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
    return replace(caps, privileged_runtime=True, devices=frozenset({"fuse", "kvm", "tun"}))


def test_scan_requires_device_evidence(caps: Capabilities) -> None:
    project = normalize({"services": {"main": OSWORLD}})
    result = scan("osworld", project, caps)
    assert result.status == "rejected"
    assert [f.path for f in result.findings if f.kind == "reject"] == ["services.main.devices"]
    assert scan("osworld", project, replace(_device_caps(caps), devices=frozenset({"kvm"}))).status == "rejected"
    assert scan("osworld", project, _device_caps(caps)).status == "accepted"
    readonly = normalize({"services": {"main": {**OSWORLD, "read_only": True}}})
    assert scan("osworld", readonly, replace(_device_caps(caps), readonly_rootfs=True)).status == "rejected"


def test_devices_roundtrip_to_the_plan(tmp_path: Path) -> None:
    compose = (
        f"services:\n  main:\n    image: {APP}\n    devices: [/dev/net/tun, /dev/kvm]\n    mem_limit: 1g\n    cpus: 1\n"
    )
    task = load_task(_task(tmp_path, "dev", compose=compose))
    # Planning does not consult the deployment; the capability scan is what refuses the task.
    assert scan(task.name, task.project, _caps()).status == "rejected"
    assert scan(task.name, task.project, _device_caps(_caps())).status == "accepted"
    unit = plan(task).units["main"]
    assert unit.devices == {"kvm", "tun"} and unit.privileged_runtime and not unit.privileged


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
        replace(spec, devices=frozenset({"sda"}))  # type: ignore[arg-type]


@pytest.mark.asyncio
async def test_device_units_open_on_the_privileged_runtime(
    deployment: tuple[FlotillaConfig, FakePlatform, SharedTree],
) -> None:
    cfg, platform, _ = deployment
    base = task()
    prepared = replace(base, units={"main": replace(base.units["main"], devices=frozenset({"kvm", "tun"}))})
    # A deployment whose runtime has no device support at all refuses the task before any create.
    platform.caps = replace(platform.caps, privileged_runtime=False, devices=frozenset())
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


def test_fuse_is_normalized_and_needs_the_privileged_runtime() -> None:
    project = normalize(_service(devices=["/dev/fuse"]))
    assert not project.rejected, project.findings
    assert project.services["main"].devices == {"fuse"}
