"""`flotilla prepare`: rows, failure report, skip/force, publication, and resumption."""

from __future__ import annotations

import asyncio
import json
import threading
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import Any

import pytest

from flotilla.build.driver import BuildContext, BuildOutcome, build_task
from flotilla.compose.task import HarborTask, load_task
from flotilla.config import FlotillaConfig
from flotilla.core.anchor import Anchor, AnchorSettings
from flotilla.prepare import PrepareReport, prepare, runner
from flotilla.prepare.runner import failures_path
from flotilla.prepared import PreparedEnvironment, load_prepared
from flotilla.testing.build import FakeBuilder, FakeRegistry, _ctx, _task
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
    assert not any((out.parent / "ds.jsonl.work").glob("task-*"))  # per-task scratch removed


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


@pytest.mark.asyncio
async def test_force_interruption_invalidates_old_row_before_publication(
    cfg: FlotillaConfig, anchors: Anchors, tree: SharedTree, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = tmp_path / "ds"
    path = _task(root, "t", compose=WITH_FILES, **{"shared__f.txt": "old"})
    out = tmp_path / "ds.jsonl"
    await _run(cfg, [root], out, anchors, environments=_with_verifier)
    (path / "environment" / "shared" / "f.txt").write_text("new")
    registry = FakeRegistry(digests={"r.io/a:1": "sha256:" + "12" * 32})
    original = runner._Session._environment
    published = asyncio.Event()

    async def pause_after_main(
        self: runner._Session, source: runner._Source, role: str, env: HarborTask, scratch: Path
    ) -> PreparedEnvironment:
        result = await original(self, source, role, env, scratch)
        if role == "main":
            published.set()
            await asyncio.Future[None]()
        return result

    with monkeypatch.context() as patch:
        patch.setattr(runner._Session, "_environment", pause_after_main)
        work = asyncio.create_task(
            prepare(
                cfg,
                [root],
                out,
                anchor=anchors.factory(),
                context=lambda j: _ctx(registry=registry),
                environments=_with_verifier,
                force=True,
            )
        )
        try:
            await asyncio.wait_for(published.wait(), 5)
            assert load_prepared(out) == []
        finally:
            work.cancel()
            await asyncio.gather(work, return_exceptions=True)
    report = await prepare(
        cfg,
        [root],
        out,
        anchor=anchors.factory(),
        context=lambda j: _ctx(registry=registry),
        environments=_with_verifier,
    )
    assert report.prepared == ("ds/t",) and report.skipped == ()
    row = load_prepared(out)[0]
    for role in ("main", "verifier"):
        assert row.environment(role).manifest().units["main"].image.endswith("12" * 32)
        files = [key for key in tree.files if key.startswith(f"tasks/ds/t/{role}/binds/")]
        assert files and all(anchors.platform.tree_contents[key] == b"new" for key in files)


@pytest.mark.asyncio
async def test_concurrent_nested_task_ids_have_independent_files(
    cfg: FlotillaConfig, anchors: Anchors, tree: SharedTree, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = tmp_path / "ds"
    for name in ("a/b", "a-b"):
        _task(root, name, compose=WITH_FILES, **{"shared__f.txt": name})
    built = threading.Barrier(2, timeout=5)

    def build(env: HarborTask, scratch: Path, ctx: BuildContext, *, identity: str | None = None) -> BuildOutcome:
        result = build_task(env, scratch, ctx, identity=identity)
        built.wait()
        return result

    monkeypatch.setattr(runner, "build_task", build)
    report = await _run(cfg, [root], tmp_path / "ds.jsonl", anchors, concurrency=2)
    assert report.prepared == ("ds/a-b", "ds/a/b") and not report.failed
    for name in ("a/b", "a-b"):
        files = [key for key in tree.files if key.startswith(f"tasks/ds/{name}/main/binds/")]
        assert files and all(anchors.platform.tree_contents[key] == name.encode() for key in files)


@pytest.mark.asyncio
async def test_cancel_waits_for_builder_before_removing_scratch(
    cfg: FlotillaConfig, dataset: Path, anchors: Anchors, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    entered = asyncio.Event()
    finish = threading.Event()
    loop = asyncio.get_running_loop()
    directories: list[Path] = []

    def build(env: HarborTask, scratch: Path, ctx: BuildContext, *, identity: str | None = None) -> BuildOutcome:
        result = build_task(env, scratch, ctx, identity=identity)
        directories.append(scratch)
        loop.call_soon_threadsafe(entered.set)
        assert finish.wait(5)
        assert scratch.exists()
        return result

    monkeypatch.setattr(runner, "build_task", build)
    work = asyncio.create_task(_run(cfg, [dataset / "plain"], tmp_path / "ds.jsonl", anchors, concurrency=2))
    try:
        await asyncio.wait_for(entered.wait(), 5)
        work.cancel()
        await asyncio.sleep(0.02)
        assert not work.done()
        assert directories[0].exists()
    finally:
        finish.set()
        with pytest.raises(asyncio.CancelledError):
            await work
    assert not directories[0].parent.exists()


@pytest.mark.asyncio
@pytest.mark.parametrize("tail", [b'{"id": "unfinished', b'{"id": "\xe4\xb8'])
@pytest.mark.parametrize("force", [False, True])
async def test_resume_recovers_only_unterminated_final_json(
    cfg: FlotillaConfig, dataset: Path, anchors: Anchors, tmp_path: Path, tail: bytes, force: bool
) -> None:
    out = tmp_path / "ds.jsonl"
    await _run(cfg, [dataset / "plain"], out, anchors)
    original = out.read_bytes()
    out.write_bytes(original + tail)
    report = await _run(cfg, [dataset / "plain"], out, anchors, force=force)
    assert (report.prepared if force else report.skipped) == ("ds/plain",)
    assert out.read_bytes() == original


@pytest.mark.asyncio
async def test_resume_preserves_complete_json_without_newline(
    cfg: FlotillaConfig, dataset: Path, anchors: Anchors, tmp_path: Path
) -> None:
    out = tmp_path / "ds.jsonl"
    await _run(cfg, [dataset / "plain"], out, anchors)
    original = out.read_bytes()
    out.write_bytes(original.rstrip(b"\n"))
    report = await _run(cfg, [dataset / "plain", dataset / "files"], out, anchors)
    assert report.skipped == ("ds/plain",) and report.prepared == ("ds/files",)
    assert len(load_prepared(out)) == 2


@pytest.mark.asyncio
@pytest.mark.parametrize("bad", [b'{"id":\n', b"{}", b'{"id":\n{}\n'])
async def test_resume_rejects_complete_corrupt_rows(
    cfg: FlotillaConfig, dataset: Path, anchors: Anchors, tmp_path: Path, bad: bytes
) -> None:
    out = tmp_path / "ds.jsonl"
    out.write_bytes(bad)
    with pytest.raises(ValueError, match="invalid prepared row"):
        await _run(cfg, [dataset / "plain"], out, anchors)
