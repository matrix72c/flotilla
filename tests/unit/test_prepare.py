"""`flotilla prepare`: rows, failure report, skip/force, publication, and resumption."""

from __future__ import annotations

import json
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import Any

import pytest

from flotilla.compose.task import HarborTask, load_task
from flotilla.config import FlotillaConfig
from flotilla.core.anchor import Anchor, AnchorSettings
from flotilla.prepare import PrepareReport, prepare
from flotilla.prepare.runner import failures_path
from flotilla.prepared import load_prepared
from flotilla.testing.build import FakeBuilder, _ctx, _task
from flotilla.testing.config import config
from flotilla.testing.platform import FakePlatform, ManualClock, SharedTree

WITH_FILES = (
    "services:\n"
    "  main:\n    image: r.io/a:1\n"
    "  app:\n    image: r.io/b:1\n"
    "    volumes:\n      - {type: bind, source: ./shared, target: /data, read_only: true}\n"
    "  web:\n    image: r.io/c:1\n"
    "    volumes:\n      - {type: bind, source: ./shared, target: /mirror, read_only: true}\n"
)


@pytest.fixture
def cfg() -> FlotillaConfig:
    return config()


@pytest.fixture
def dataset(tmp_path: Path) -> Path:
    root = tmp_path / "ds"
    _task(root, "plain", toml='[environment]\ndocker_image = "r.io/ns/app:1"\n')
    _task(root, "files", compose=WITH_FILES, **{"shared__f.txt": "x"})
    _task(root, "broken", compose="services:\n  main:\n    image: r.io/a:1\n    devices: [/dev/kvm]\n")
    return root


class Anchors:
    def __init__(self, platform: FakePlatform, clock: ManualClock) -> None:
        self.platform, self.clock, self.started = platform, clock, 0

    def factory(self) -> Callable[[], Awaitable[Anchor]]:
        async def start() -> Anchor:
            self.started += 1
            anchor = Anchor(self.platform, self.clock, "P1", AnchorSettings(image="anchor@sha256:abc"))
            await self.clock.run(anchor.start())
            return anchor

        return start


@pytest.fixture
def anchors(platform: FakePlatform, clock: ManualClock, tree: SharedTree) -> Anchors:
    return Anchors(platform, clock)


def _with_verifier(path: Path) -> list[HarborTask]:
    return [load_task(path), load_task(path)]


async def _run(cfg: FlotillaConfig, roots: list[Path], out: Path, anchors: Anchors, **kw: Any) -> PrepareReport:
    return await prepare(cfg, roots, out, anchor=anchors.factory(), context=lambda journal: _ctx(), **kw)


@pytest.mark.asyncio
async def test_prepare_writes_rows_failures_and_task_files(
    cfg: FlotillaConfig, dataset: Path, anchors: Anchors, tree: SharedTree, tmp_path: Path
) -> None:
    out = tmp_path / "out" / "ds.jsonl"
    report = await _run(cfg, [dataset], out, anchors, environments=_with_verifier)
    assert report.prepared == ("ds/files", "ds/plain") and report.failed == ("ds/broken",)
    rows = {row.id: row for row in load_prepared(out)}
    assert list(rows) == ["ds/files", "ds/plain"]  # compacted and sorted
    files = rows["ds/files"]
    assert files.task_dir == str(dataset / "files") and files.attrs["has_verifier"] is True
    assert files.attrs["services"] == 3 and files.attrs["cpu"] == 3.0 and files.attrs["memory_mib"] == 6144
    for role in ("main", "verifier"):
        manifest = files.environment(role).manifest()
        assert manifest.task_files == f"tasks/ds/files/{role}"
        assert any(path.startswith(f"tasks/ds/files/{role}/binds/") for path in tree.files)
    assert rows["ds/plain"].environment().manifest().task_files is None
    assert anchors.started == 1
    failure = json.loads(failures_path(out).read_text())
    assert failure["id"] == "ds/broken" and failure["stage"] == "scan" and "devices" in failure["reason"]
    assert not any((out.parent / "ds.jsonl.work").glob("ds-*"))  # per-task scratch removed


@pytest.mark.asyncio
async def test_prepare_skips_existing_rows_and_force_rebuilds(
    cfg: FlotillaConfig, dataset: Path, anchors: Anchors, tmp_path: Path
) -> None:
    out = tmp_path / "ds.jsonl"
    await _run(cfg, [dataset / "plain"], out, anchors)
    again = await _run(cfg, [dataset / "plain", dataset / "files"], out, anchors)
    assert again.skipped == ("ds/plain",) and again.prepared == ("ds/files",)
    forced = await _run(cfg, [dataset / "plain"], out, anchors, force=True)
    assert forced.prepared == ("ds/plain",)
    assert [row.id for row in load_prepared(out)] == ["ds/files", "ds/plain"]


@pytest.mark.asyncio
async def test_forced_failure_drops_the_old_row_and_no_files_means_no_anchor(
    cfg: FlotillaConfig, tmp_path: Path, anchors: Anchors
) -> None:
    root = tmp_path / "ds"
    task = _task(root, "t", toml='[environment]\ndocker_image = "r.io/ns/app:1"\n')
    out = tmp_path / "ds.jsonl"
    await _run(cfg, [root], out, anchors)
    (task / "environment" / "docker-compose.yaml").write_text("services:\n  main:\n    devices: [/dev/kvm]\n")
    report = await _run(cfg, [root], out, anchors, force=True)
    assert report.failed == ("ds/t",) and load_prepared(out) == []
    assert anchors.started == 0


@pytest.mark.asyncio
async def test_interrupted_appends_are_compacted_on_the_next_run(
    cfg: FlotillaConfig, dataset: Path, anchors: Anchors, tmp_path: Path
) -> None:
    out = tmp_path / "ds.jsonl"
    await _run(cfg, [dataset / "plain"], out, anchors)
    line = out.read_text()
    out.write_text(line + line)  # a crash between append and compaction leaves a duplicate
    await _run(cfg, [dataset / "plain"], out, anchors)
    assert out.read_text() == line


@pytest.mark.asyncio
async def test_multiple_verifiers_fail_the_task(
    cfg: FlotillaConfig, dataset: Path, anchors: Anchors, tmp_path: Path
) -> None:
    out = tmp_path / "ds.jsonl"
    report = await _run(cfg, [dataset / "plain"], out, anchors, environments=lambda p: [load_task(p)] * 3)
    assert report.failed == ("ds/plain",)
    assert "multiple verifier" in json.loads(failures_path(out).read_text())["reason"]


@pytest.mark.asyncio
async def test_each_dataset_pushes_to_its_own_repository(cfg: FlotillaConfig, anchors: Anchors, tmp_path: Path) -> None:
    builder = FakeBuilder()
    _task(tmp_path / "My_Set" / "group", "t", Dockerfile="FROM r.io/ns/base:1\n")  # main needs a build
    out = tmp_path / "set.jsonl"
    await prepare(
        cfg, [tmp_path / "My_Set"], out, anchor=anchors.factory(), context=lambda journal: _ctx(builder=builder)
    )
    assert [row.id for row in load_prepared(out)] == ["My_Set/group/t"]
    (tag,) = {request.tag for request in builder.requests if "-runtime" not in request.tag}
    assert tag.startswith("r.io/ns/out/my-set:group--t--main--main--")


@pytest.mark.asyncio
async def test_datasets_sharing_a_repository_are_rejected(
    cfg: FlotillaConfig, anchors: Anchors, tmp_path: Path
) -> None:
    _task(tmp_path / "a" / "My_Set", "t")
    _task(tmp_path / "b" / "my-set", "t")
    with pytest.raises(ValueError, match="same image repository"):
        await _run(cfg, [tmp_path / "a" / "My_Set", tmp_path / "b" / "my-set"], tmp_path / "x.jsonl", anchors)


def test_failures_path_and_dataset_rev(tmp_path: Path) -> None:
    from flotilla.prepare.runner import _dataset_rev

    assert failures_path(tmp_path / "x.jsonl") == tmp_path / "x.failures.jsonl"
    assert _dataset_rev(tmp_path) is None  # not a repository root
