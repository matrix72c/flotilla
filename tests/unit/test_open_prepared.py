"""Opening prepared rows: no local builds or subprocesses, and clear errors for the removed in-open preparation."""

from __future__ import annotations

import asyncio
import subprocess
from pathlib import Path
from typing import Any

import pytest

import flotilla
from flotilla import Flotilla, OpenOptions
from flotilla.cli import _build_parser
from flotilla.config import FlotillaConfig
from flotilla.testing.build import prepared_environment
from flotilla.testing.config import config_data
from flotilla.testing.platform import FakePlatform, SharedTree
from flotilla.testing.runtime import deployment as deployment


@pytest.mark.asyncio
async def test_open_runs_no_subprocess(
    tmp_path: Path, deployment: tuple[FlotillaConfig, FakePlatform, SharedTree], monkeypatch: pytest.MonkeyPatch
) -> None:
    cfg, platform, _ = deployment
    task = tmp_path / "ds" / "task"
    (task / "environment").mkdir(parents=True)
    (task / "task.toml").write_text('[environment]\ndocker_image = "r.io/main:1"\n')
    prepared = prepared_environment(task, cfg)

    def forbidden(*args: Any, **kwargs: Any) -> Any:
        raise AssertionError("open must not start local processes")

    monkeypatch.setattr(subprocess, "run", forbidden)
    monkeypatch.setattr(subprocess, "Popen", forbidden)
    monkeypatch.setattr(asyncio, "create_subprocess_exec", forbidden)
    async with Flotilla(deployment=cfg) as fleet, fleet.environment(prepared, OpenOptions(request_id="row")) as env:
        info = await env.info()
        assert info.services[0].image.startswith("r.io/main@")
        assert not any(name.startswith(("prepare_", "publish_")) for name, _ in info.timings)
    assert not platform.iids()


def test_removed_in_open_preparation_has_migration_errors() -> None:
    data = config_data()
    data["preparation"] = {"concurrency": 4}
    with pytest.raises(ValueError, match=r"\[preparation\] was removed.*\[prepare\].*startup_timeout_s"):
        FlotillaConfig.from_mapping(data)
    for name in ("open_task", "open_environment", "inspect", "inspect_environment", "prepare"):
        assert not hasattr(Flotilla, name), name
    for name in ("EnvironmentDefinition", "EnvironmentInspection", "PreparationResidual"):
        assert not hasattr(flotilla, name), name
    assert "force_build" not in OpenOptions.__dataclass_fields__
    # Preparation is the offline `flotilla prepare` command.
    assert _build_parser().parse_args(["prepare", "ds", "--deployment", "d.toml", "--out", "o.jsonl"]).dirs


def test_open_rejects_task_paths(deployment: tuple[FlotillaConfig, FakePlatform, SharedTree]) -> None:
    cfg, _, _ = deployment
    with pytest.raises(TypeError, match="flotilla prepare"):
        asyncio.run(Flotilla(deployment=cfg).open("tasks/x"))  # type: ignore[arg-type]
