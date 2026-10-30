"""Native filesystem semantics survive parsing, preparation, admission and wire adaptation."""

from __future__ import annotations

from dataclasses import replace
from pathlib import Path
from typing import Any

import pytest

from flotilla.build.driver import build_task
from flotilla.compose.layout import classify
from flotilla.compose.normalize import normalize
from flotilla.compose.task import load_task
from flotilla.config import FlotillaConfig
from flotilla.manifest import Manifest
from flotilla.platform.base import Capabilities, Resources, TmpfsMount
from flotilla.platform.filesystem import memory_bytes
from flotilla.platform.opensandbox.filesystem import INTERNAL_LIMITS, parse_effective
from flotilla.runtime.planning import _resolve_tmpfs, to_plan
from flotilla.scan import scan
from flotilla.testing.build import FakeBuilder, _ctx, _task
from flotilla.testing.config import config_data
from flotilla.testing.reports import ok, report


def test_compose_filesystem_fields_roundtrip_to_runtime(tmp_path: Path) -> None:
    task = load_task(
        _task(
            tmp_path,
            "fs",
            compose="""services:
  main:
    image: r.io/app
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
    builder = FakeBuilder()
    ctx = _ctx(builder=builder)
    ctx = replace(
        ctx, caps=replace(ctx.caps, readonly_rootfs=True, native_tmpfs=True, filesystem_limits=INTERNAL_LIMITS)
    )
    outcome = build_task(task, tmp_path / "out", ctx)
    assert outcome.manifest_path, outcome.reason
    manifest = Manifest.load_json(outcome.manifest_path.read_bytes())
    unit = to_plan(manifest, runtime_params={}).units["main"]
    assert unit.readonly_rootfs
    assert unit.tmpfs_mounts == (
        TmpfsMount("/tmp", 16 * 1024**2, 0o750, 1000, 1001),
        TmpfsMount("/cache", 8 * 1024**2, 0o700),
    )
    dockerfile = "\n".join(r.dockerfile_text or "" for r in builder.requests)
    assert "mkdir -p /tmp" in dockerfile and "mkdir -p /.flotilla-runtime" in dockerfile
    assert "test ! -L /cache" in dockerfile
    assert "rm -rf /tmp" not in dockerfile and "rm -rf /cache" not in dockerfile
    assert not any(f.kind == "equivalent" and "tmpfs" in f.path for f in manifest.findings)
    with pytest.raises(ValueError, match="schema"):
        Manifest.load_json(manifest.dump_json().replace('"schema": 6', '"schema": 5'))


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
    project = normalize({"services": {"main": {"image": "r.io/app", **fields}}}, {})
    assert project.rejected, project


def test_filesystem_admission_and_internal_limits(caps: Capabilities) -> None:
    project = normalize({"services": {"main": {"image": "r.io/app", "read_only": True, "tmpfs": ["/tmp"]}}}, {})
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


def test_unsized_tmpfs_resolves_to_half_service_memory_at_planning() -> None:
    planned = _resolve_tmpfs((TmpfsMount("/tmp"), TmpfsMount("/cache", 8 * 1024**2)), Resources(cpu="1", memory="2Gi"))
    assert planned == (TmpfsMount("/tmp", 1024**3), TmpfsMount("/cache", 8 * 1024**2))
    small = _resolve_tmpfs((TmpfsMount("/tmp"),), Resources(cpu="1", memory="128Mi"))
    assert small == (TmpfsMount("/tmp", 64 * 1024**2),)


def test_scan_and_planning_agree_on_unsized_tmpfs_with_shm_size(caps: Capabilities) -> None:
    supported = replace(caps, native_tmpfs=True, shm_size=True, filesystem_limits=INTERNAL_LIMITS)
    for shm, status in (("1g", "accepted"), ("1100m", "rejected")):  # unsized /tmp takes 1 GiB of 2 GiB
        raw = {"services": {"main": {"image": "r.io/app", "tmpfs": ["/tmp"], "shm_size": shm, "mem_limit": "2g"}}}
        project = normalize(raw, {})
        assert scan("fs", project, supported).status == status, shm
        svc = project.services["main"]
        planned = _resolve_tmpfs(svc.tmpfs, Resources(cpu="1", memory="2Gi"))
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
    writable = classify(normalize(raw, {}).services)
    assert writable.volumes[0].mode == "local" and writable.binds[0].mode == "image"
    raw["services"]["main"]["read_only"] = True
    readonly = classify(normalize(raw, {}).services)
    assert readonly.volumes[0].mode == readonly.binds[0].mode == "trial"
    assert {m.target for m in readonly.mounts["main"]} == {"/db", "/work"}
    assert all(not m.read_only for m in readonly.mounts["main"])


def test_unsupported_tmpfs_is_rejected_before_building(tmp_path: Path) -> None:
    task = load_task(_task(tmp_path, "fs", compose="services:\n  main:\n    image: r.io/app\n    tmpfs: /tmp\n"))
    builder = FakeBuilder()
    outcome = build_task(task, tmp_path / "out", _ctx(builder=builder))
    assert outcome.status == "rejected" and "native_tmpfs" in (outcome.reason or "")
    assert not builder.requests


def test_report_requires_matching_dialect_and_runtime_profile() -> None:
    data = config_data()
    data["opensandbox"]["filesystem_protocol"] = "sandbox-system-v1"
    cfg = FlotillaConfig.from_mapping(data)
    evidence = report(
        **{
            "runtime.readonly_rootfs": ok(),
            "storage.native_tmpfs": ok(),
            "filesystem_profile": cfg.filesystem_profile(),
        }
    )
    assert cfg.platform_capabilities(evidence).readonly_rootfs and cfg.platform_capabilities(evidence).native_tmpfs
    assert not cfg.platform_capabilities(report()).readonly_rootfs
    changes: list[dict[str, Any]] = [{"filesystem_protocol": "upstream"}, {"extensions": {"runtimeClass": "kata"}}]
    for changed in changes:
        other = cfg.model_copy(update={"opensandbox": cfg.opensandbox_settings().model_copy(update=changed)})
        assert not other.platform_capabilities(evidence).readonly_rootfs
    unmeasured = evidence.model_copy(update={"filesystem_profile": None})
    assert not cfg.platform_capabilities(unmeasured).native_tmpfs


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
