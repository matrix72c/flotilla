"""Compose `shm_size` survives parsing, planning and admission; only probed internal deployments accept it."""

from __future__ import annotations

from dataclasses import replace
from pathlib import Path
from typing import Any

import pytest

from flotilla.compose.normalize import normalize
from flotilla.compose.task import load_task
from flotilla.config import FlotillaConfig
from flotilla.platform.base import Capabilities, TmpfsMount
from flotilla.platform.opensandbox.filesystem import INTERNAL_LIMITS
from flotilla.scan import scan
from flotilla.testing.config import config_data
from flotilla.testing.planning import APP, _caps, _task, plan


@pytest.mark.parametrize(
    ("value", "size"),
    [("2gb", 2 * 1024**3), ("64M", 64 * 1024**2), (1073741824, 1073741824), ("1.5g", 1610612736)],
)
def test_shm_size_is_normalized_to_bytes(value: object, size: int) -> None:
    project = normalize({"services": {"main": {"image": APP, "shm_size": value}}})
    assert not project.rejected, project.findings
    assert project.services["main"].shm_size_bytes == size


@pytest.mark.parametrize("value", ["lots", "0", 0, True, "1.5b", "-1g"])
def test_invalid_shm_size_is_rejected(value: object) -> None:
    project = normalize({"services": {"main": {"image": APP, "shm_size": value}}})
    assert [f.path for f in project.findings if f.kind == "reject"] == ["services.main.shm_size"]


def test_shm_size_roundtrips_to_the_plan(tmp_path: Path) -> None:
    compose = f"services:\n  main:\n    image: {APP}\n    shm_size: 1gb\n    mem_limit: 2g\n    cpus: 1\n"
    task = load_task(_task(tmp_path, "shm", compose=compose))
    assert plan(task).units["main"].shm_size_bytes == 1024**3


def test_a_deployment_without_shm_size_refuses_the_task_at_admission(tmp_path: Path) -> None:
    """Planning is deployment-independent: an unsupported field is caught by the capability scan."""
    task = load_task(_task(tmp_path, "shm", compose=f"services:\n  main:\n    image: {APP}\n    shm_size: 1g\n"))
    result = scan(task.name, task.project, _caps())
    assert result.status == "rejected"
    assert any("shm_size" in finding.reason for finding in result.findings if finding.kind == "reject")


def test_shm_size_admission_shares_the_tmpfs_memory_budget(caps: Capabilities) -> None:
    raw: dict[str, Any] = {"services": {"main": {"image": APP, "shm_size": "1g", "mem_limit": "2g"}}}
    project = normalize(raw)
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


def test_only_the_internal_dialect_can_express_shm_size() -> None:
    data = config_data()
    data["opensandbox"]["filesystem_protocol"] = "sandbox-system-v1"
    cfg = FlotillaConfig.from_mapping(data)
    assert cfg.platform_capabilities().shm_size
    upstream = cfg.model_copy(
        update={"opensandbox": cfg.opensandbox_settings().model_copy(update={"filesystem_protocol": "upstream"})}
    )
    assert not upstream.platform_capabilities().shm_size
