"""Public preparation and file transfers with a read-only root and framework scratch mount."""

from __future__ import annotations

import io
import tarfile
from dataclasses import replace
from pathlib import Path

import pytest

from flotilla import Flotilla
from flotilla.capabilities import Item
from flotilla.config import FlotillaConfig
from flotilla.platform.base import ExecAttempt, ExecResult, FlotillaError, InstanceHandle, ProcessSpec, TmpfsMount
from flotilla.platform.opensandbox.filesystem import INTERNAL_LIMITS
from flotilla.runtime.manager import Manager
from flotilla.runtime.types import RunRequest, Task
from flotilla.testing.environments import FixtureRequest, environment
from flotilla.testing.platform import FakePlatform, SharedTree
from flotilla.testing.runtime import deployment as deployment
from flotilla.testing.runtime import task


def test_tmpfs_budget_uses_final_resource_defaults_and_overrides(
    deployment: tuple[FlotillaConfig, FakePlatform, SharedTree],
) -> None:
    cfg, platform, _ = deployment
    platform.caps = replace(platform.caps, native_tmpfs=True, filesystem_limits=INTERNAL_LIMITS)
    manager = Manager(cfg, platform=platform)
    manifest = task().manifest()
    unit = manifest.units["main"]
    unit = unit.model_copy(
        update={
            "tmpfs_mounts": [TmpfsMount("/tmp")],
            "resources": unit.resources.model_copy(update={"memory": None}),
        }
    )
    request = RunRequest(Task(manifest.model_copy(update={"units": {"main": unit}})), "budget")
    assert manager.plan(request).units["main"].resources.memory == cfg.resources.default.memory
    with pytest.raises(FlotillaError, match="capacities exceed") as failure:
        manager.plan(replace(request, resources={"main": {"memory": "32Mi"}}))
    assert not failure.value.retryable and platform.calls["create"] == 0


@pytest.mark.asyncio
async def test_readonly_directory_transfers_use_scratch_and_preserve_environment(
    deployment: tuple[FlotillaConfig, FakePlatform, SharedTree],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    cfg, platform, tree = deployment
    cfg = cfg.model_copy(
        update={"opensandbox": cfg.opensandbox.model_copy(update={"filesystem_protocol": "sandbox-system-v1"})}
    )
    measured = Item(ok=True, source="probed", evidence="test double")
    report = cfg.load_report()
    report = report.model_copy(
        update={
            "runtime": report.runtime.model_copy(update={"readonly_rootfs": measured}),
            "storage": report.storage.model_copy(update={"native_tmpfs": measured}),
            "filesystem_profile": cfg.filesystem_profile(),
        }
    )
    cfg.capabilities_path().write_text(report.dump_json())
    platform.caps = cfg.platform_capabilities(report)
    manifest = task().manifest()
    unit = manifest.units["main"].model_copy(
        update={
            "readonly_rootfs": True,
            "tmpfs_mounts": [TmpfsMount("/tmp", 1024**2)],
            "context": manifest.units["main"].context.model_copy(update={"uid": 1000, "gid": 1001}),
        }
    )
    prepared = Task(manifest.model_copy(update={"units": {"main": unit}}))
    source = tmp_path / "source"
    source.mkdir()
    (source / "result.txt").write_text("contents")
    archive = io.BytesIO()
    with tarfile.open(fileobj=archive, mode="w") as tar:
        tar.add(source / "result.txt", arcname="result.txt")
    calls: list[ProcessSpec] = []
    original = platform.exec

    async def execute(handle: InstanceHandle, proc: ProcessSpec, *, attempt: ExecAttempt | None = None) -> ExecResult:
        if len(proc.argv) > 3 and proc.argv[1] == "tar":
            calls.append(proc)
            path = proc.argv[3]
            assert path.startswith("/.flotilla-runtime/flotilla-")
            assert proc.uid == 1000 and proc.gid == 1001
            assert "TMPDIR" not in proc.env
            if proc.argv[2] == "-cf":
                await platform.write_file(handle, path, archive.getvalue(), mode=0o600, uid=1000, gid=1001)
            else:
                with tarfile.open(fileobj=io.BytesIO(await platform.read_file(handle, path))) as tar:
                    assert "result.txt" in tar.getnames()
            return ExecResult(0, b"", b"")
        return await original(handle, proc, attempt=attempt)

    monkeypatch.setattr(platform, "exec", execute)
    async with Flotilla(deployment=cfg) as fleet:
        async with environment(fleet, FixtureRequest(prepared, "readonly-transfer")) as env:
            await env.agent.upload_dir(source, "/tmp/upload")
            destination = tmp_path / "download"
            await env.agent.download_dir("/tmp/upload", destination)
            assert (destination / "result.txt").read_text() == "contents"
        assert (await fleet.drain(2)).clean
    assert len(calls) == 2
    assert not platform.iids()
    assert not any("/_runtime/" in directory for directory in tree.dirs)
