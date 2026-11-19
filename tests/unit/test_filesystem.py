"""Native filesystem semantics survive parsing, planning, admission and wire adaptation."""

from __future__ import annotations

from dataclasses import replace
from pathlib import Path
from typing import Any

import pytest

from flotilla.compose.layout import classify
from flotilla.compose.normalize import normalize
from flotilla.compose.task import load_task
from flotilla.config import FlotillaConfig
from flotilla.platform.base import Capabilities, TmpfsMount
from flotilla.platform.filesystem import memory_bytes, unsized_tmpfs_bytes
from flotilla.platform.opensandbox.filesystem import INTERNAL_LIMITS, parse_effective
from flotilla.scan import scan
from flotilla.testing.config import config_data
from flotilla.testing.planning import APP, _caps, _task, plan


def test_compose_filesystem_fields_roundtrip_to_the_plan(tmp_path: Path) -> None:
    task = load_task(
        _task(
            tmp_path,
            "fs",
            compose=f"""services:
  main:
    image: {APP}
    read_only: true
    user: "1000:1001"
    tmpfs: ["/tmp:size=16m,mode=0750,uid=1000,gid=1001"]
    mem_limit: 128m
    cpus: 1
    volumes:
      - type: tmpfs
        target: /cache
        tmpfs:
          size: 8m
          mode: 0700
""",
        )
    )
    supported = replace(_caps(), readonly_rootfs=True, native_tmpfs=True, filesystem_limits=INTERNAL_LIMITS)
    assert scan(task.name, task.project, supported).status == "accepted"
    unit = plan(task).units["main"]
    assert unit.readonly_rootfs
    assert unit.tmpfs_mounts == (
        TmpfsMount("/tmp", 16 * 1024**2, 0o750, 1000, 1001),
        TmpfsMount("/cache", 8 * 1024**2, 0o700),
    )
    # Mount points and the readonly runtime dir are runtime responsibilities: no derived image build.
    assert not any(f.kind == "equivalent" and "tmpfs" in f.path for f in task.project.findings)


@pytest.mark.parametrize(
    "fields",
    [
        {"tmpfs": ["/tmp:noexec"]},
        {"tmpfs": ["/tmp:size=1m,size=2m"]},
        {"tmpfs": ["/tmp:mode=0899"]},
        {"tmpfs": ["/tmp:uid=-1"]},
        {"tmpfs": ["/tmp:size=0"]},
        {"tmpfs": ["/tmp/../run"]},
        {"tmpfs": ["/tmp", "/tmp/sub"]},
        {"tmpfs": ["/tmp", "/tmp"]},
        {"tmpfs": ["/.flotilla-runtime/tmp"]},
        {"tmpfs": ["/tmp"], "volumes": ["./seed:/tmp/sub"]},
        {"volumes": [{"type": "tmpfs", "target": "/tmp", "read_only": True}]},
        {"volumes": [{"type": "tmpfs", "target": "/tmp", "tmpfs": {"unknown": True}}]},
    ],
)
def test_tmpfs_invalid_or_unrepresentable_options_are_rejected(fields: dict[str, Any]) -> None:
    project = normalize({"services": {"main": {"image": APP, **fields}}})
    assert project.rejected, project


def test_filesystem_admission_and_internal_limits(caps: Capabilities) -> None:
    project = normalize({"services": {"main": {"image": APP, "read_only": True, "tmpfs": ["/tmp"]}}})
    assert scan("fs", project, caps).status == "rejected"
    supported = replace(caps, readonly_rootfs=True, native_tmpfs=True, filesystem_limits=INTERNAL_LIMITS)
    assert scan("fs", project, supported).status == "accepted"
    assert scan("fs", project, replace(supported, native_tmpfs=False)).status == "rejected"
    for tmpfs, memory in [
        ((TmpfsMount("/run/app"),), "1Gi"),
        ((TmpfsMount("/tmp", 64 * 1024 * 1024),), "32Mi"),
        (tuple(TmpfsMount(f"/cache{i}", 1024**2) for i in range(9)), "1Gi"),
    ]:
        with pytest.raises(ValueError):
            supported.require_filesystem(False, tmpfs, memory=memory)
    # Admission counts unsized mounts at the size planning resolves (max(64 MiB, memory / 2)).
    supported.require_filesystem(False, (TmpfsMount("/tmp"),), memory="128Mi")
    with pytest.raises(ValueError, match="exceed"):
        supported.require_filesystem(False, (TmpfsMount("/tmp"),), memory="32Mi")
    with pytest.raises(ValueError, match="privileged"):
        supported.require_filesystem(True, (), privileged=True)
    with pytest.raises(ValueError, match="privileged"):
        supported.require_filesystem(False, (TmpfsMount("/tmp"),), privileged=True)


def test_unsized_tmpfs_resolves_to_half_service_memory_at_planning(tmp_path: Path) -> None:
    compose = f"""services:
  main:
    image: {APP}
    tmpfs: ["/tmp", "/cache:size=8m"]
    mem_limit: 2g
    cpus: 1
"""
    unit = plan(load_task(_task(tmp_path, "fs", compose=compose))).units["main"]
    assert unit.tmpfs_mounts == (TmpfsMount("/tmp", 1024**3), TmpfsMount("/cache", 8 * 1024**2))
    assert unsized_tmpfs_bytes("128Mi") == 64 * 1024**2  # floored, so small services keep working


def test_scan_and_planning_agree_on_unsized_tmpfs_with_shm_size(caps: Capabilities) -> None:
    supported = replace(caps, native_tmpfs=True, shm_size=True, filesystem_limits=INTERNAL_LIMITS)
    for shm, status in (("1g", "accepted"), ("1100m", "rejected")):  # unsized /tmp takes 1 GiB of 2 GiB
        raw = {"services": {"main": {"image": APP, "tmpfs": ["/tmp"], "shm_size": shm, "mem_limit": "2g"}}}
        project = normalize(raw)
        assert scan("fs", project, supported).status == status, shm
        svc = project.services["main"]
        planned = tuple(
            m if m.size_bytes is not None else replace(m, size_bytes=unsized_tmpfs_bytes("2Gi")) for m in svc.tmpfs
        )
        if status == "accepted":
            supported.require_filesystem(False, planned, memory="2Gi", shm_size_bytes=svc.shm_size_bytes)
        else:
            with pytest.raises(ValueError, match="exceed"):
                supported.require_filesystem(False, planned, memory="2Gi", shm_size_bytes=svc.shm_size_bytes)


def test_readonly_rootfs_promotes_single_writer_mounts_to_real_storage() -> None:
    raw: dict[str, Any] = {
        "services": {"main": {"image": "app", "volumes": ["data:/db", "./seed:/work"]}},
        "volumes": {"data": {}},
    }
    raw["services"]["main"]["read_only"] = True
    readonly = classify(normalize(raw).services)
    assert {m.target for m in readonly.mounts["main"]} == {"/db", "/work"}
    assert all(not m.read_only for m in readonly.mounts["main"])


def test_unsupported_tmpfs_is_rejected_at_admission(tmp_path: Path) -> None:
    task = load_task(_task(tmp_path, "fs", compose=f"services:\n  main:\n    image: {APP}\n    tmpfs: /tmp\n"))
    result = scan(task.name, task.project, _caps())
    assert result.status == "rejected"
    assert any("native_tmpfs" in f.reason for f in result.findings if f.kind == "reject")


def test_only_the_internal_dialect_can_express_readonly_rootfs_and_tmpfs() -> None:
    """A deployment on the upstream protocol has no field to send these in, so a task asking is refused."""
    data = config_data()
    data["opensandbox"]["filesystem_protocol"] = "sandbox-system-v1"
    cfg = FlotillaConfig.from_mapping(data)
    assert cfg.platform_capabilities().readonly_rootfs and cfg.platform_capabilities().native_tmpfs
    upstream = cfg.model_copy(
        update={"opensandbox": cfg.opensandbox_settings().model_copy(update={"filesystem_protocol": "upstream"})}
    )
    caps = upstream.platform_capabilities()
    assert not caps.readonly_rootfs and not caps.native_tmpfs


def test_effective_configuration_is_normalized_and_malformed_values_fail_closed() -> None:
    item: dict[str, Any] = {
        "readOnlyRootFilesystem": True,
        "tmpfsMounts": [
            {"mountPath": "/tmp", "size": "64Mi", "mode": "750", "uid": 1000, "gid": 1001},
        ],
    }
    state = parse_effective(item)
    assert state and state.tmpfs_mounts == (TmpfsMount("/tmp", 67108864, 0o750, 1000, 1001),)
    assert parse_effective({**item, "readOnlyRootFilesystem": "true"}) is None
    assert parse_effective({**item, "tmpfsMounts": item["tmpfsMounts"] * 2}) is None
    del item["tmpfsMounts"][0]["uid"]
    assert parse_effective(item) is None
    assert memory_bytes("1.5Gi") == 1610612736
