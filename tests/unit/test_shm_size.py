"""Compose `shm_size` survives parsing, preparation and admission; only probed internal deployments accept it."""

from __future__ import annotations

from dataclasses import replace
from pathlib import Path
from typing import Any

import pytest

from flotilla.build.driver import build_task
from flotilla.compose.normalize import normalize
from flotilla.compose.task import load_task
from flotilla.config import FlotillaConfig
from flotilla.manifest import Manifest
from flotilla.platform.base import Capabilities, TmpfsMount
from flotilla.platform.opensandbox.filesystem import INTERNAL_LIMITS
from flotilla.runtime.planning import to_plan
from flotilla.scan import scan
from flotilla.testing.build import FakeBuilder, _ctx, _task
from flotilla.testing.config import config_data
from flotilla.testing.reports import ok, report


@pytest.mark.parametrize(
    ("value", "size"),
    [("2gb", 2 * 1024**3), ("64M", 64 * 1024**2), (1073741824, 1073741824), ("1.5g", 1610612736)],
)
def test_shm_size_is_normalized_to_bytes(value: object, size: int) -> None:
    project = normalize({"services": {"main": {"image": "r.io/app", "shm_size": value}}}, {})
    assert not project.rejected, project.findings
    assert project.services["main"].shm_size_bytes == size


@pytest.mark.parametrize("value", ["lots", "0", 0, True, "1.5b", "-1g"])
def test_invalid_shm_size_is_rejected(value: object) -> None:
    project = normalize({"services": {"main": {"image": "r.io/app", "shm_size": value}}}, {})
    assert [f.path for f in project.findings if f.kind == "reject"] == ["services.main.shm_size"]


def test_shm_size_roundtrips_to_runtime(tmp_path: Path) -> None:
    compose = "services:\n  main:\n    image: r.io/app\n    shm_size: 1gb\n    mem_limit: 2g\n    cpus: 1\n"
    task = load_task(_task(tmp_path, "shm", compose=compose))
    builder = FakeBuilder()
    ctx = _ctx(builder=builder)
    ctx = replace(ctx, caps=replace(ctx.caps, shm_size=True, filesystem_limits=INTERNAL_LIMITS))
    outcome = build_task(task, tmp_path / "out", ctx)
    assert outcome.manifest_path, outcome.reason
    manifest = Manifest.load_json(outcome.manifest_path.read_bytes())
    assert manifest.units["main"].shm_size_bytes == 1024**3
    assert to_plan(manifest, runtime_params={}).units["main"].shm_size_bytes == 1024**3
    assert not builder.requests  # /dev/shm needs no derived image


def test_unsupported_shm_size_is_rejected_before_building(tmp_path: Path) -> None:
    task = load_task(_task(tmp_path, "shm", compose="services:\n  main:\n    image: r.io/app\n    shm_size: 1g\n"))
    builder = FakeBuilder()
    outcome = build_task(task, tmp_path / "out", _ctx(builder=builder))
    assert outcome.status == "rejected" and "shm_size" in (outcome.reason or "")
    assert not builder.requests


def test_shm_size_admission_shares_the_tmpfs_memory_budget(caps: Capabilities) -> None:
    raw: dict[str, Any] = {"services": {"main": {"image": "r.io/app", "shm_size": "1g", "mem_limit": "2g"}}}
    project = normalize(raw, {})
    assert scan("shm", project, caps).status == "rejected"
    supported = replace(caps, shm_size=True, native_tmpfs=True, filesystem_limits=INTERNAL_LIMITS)
    assert scan("shm", project, supported).status == "accepted"
    supported.require_filesystem(False, (TmpfsMount("/tmp", 1024**3),), memory="2Gi", shm_size_bytes=1024**3)
    with pytest.raises(ValueError, match="exceed"):
        supported.require_filesystem(False, (TmpfsMount("/tmp", 1024**3),), memory="2Gi", shm_size_bytes=1024**3 + 1)
    with pytest.raises(ValueError, match="exceed"):
        supported.require_filesystem(False, (), memory="1Gi", shm_size_bytes=2 * 1024**3)
    with pytest.raises(ValueError, match="privileged"):
        supported.require_filesystem(False, (), privileged=True, shm_size_bytes=1024**2)


def test_report_requires_probed_shm_evidence_and_matching_profile() -> None:
    data = config_data()
    data["opensandbox"]["filesystem_protocol"] = "sandbox-system-v1"
    cfg = FlotillaConfig.from_mapping(data)
    evidence = report(**{"storage.shm_size": ok(), "filesystem_profile": cfg.filesystem_profile()})
    assert cfg.platform_capabilities(evidence).shm_size
    assert not cfg.platform_capabilities(report()).shm_size
    assert not cfg.platform_capabilities(evidence.model_copy(update={"filesystem_profile": None})).shm_size
    upstream = cfg.model_copy(
        update={"opensandbox": cfg.opensandbox_settings().model_copy(update={"filesystem_protocol": "upstream"})}
    )
    assert not upstream.platform_capabilities(evidence).shm_size
