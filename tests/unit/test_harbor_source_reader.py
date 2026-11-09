from __future__ import annotations

import asyncio
import os
from concurrent.futures import Future, ProcessPoolExecutor
from pathlib import Path
from threading import Event
from typing import Any

import pytest
from harbor.models.task.config import TaskConfig
from harbor.models.task.task import strip_canary
from pydantic import ValidationError

from flotilla.integrations import harbor_source_reader as reader
from flotilla.integrations.harbor_source_reader import NativeTaskSource, SourceMapping, metadata_identity


def make_task(root: Path, name: str = "task", *, instruction: str = "Do the work.\n", config: str = "") -> Path:
    task = root / name
    task.mkdir(parents=True)
    (task / "task.toml").write_text(config, encoding="utf-8")
    (task / "instruction.md").write_text(instruction, encoding="utf-8")
    return task


@pytest.fixture
def source(tmp_path: Path) -> NativeTaskSource:
    make_task(tmp_path)
    return NativeTaskSource([{"local_root": tmp_path, "platform_prefix": "datasets/native"}])


@pytest.mark.parametrize(
    "instruction",
    [
        "Do the work.\n",
        "# BENCHMARK CANARY marker\n\nDo the work.\n",
        " <!-- Canary marker --> \n# another CANARY\n\nDo the work.\n",
        "\n\nDo the work.\n# trailing canary stays\n",
    ],
)
def test_native_defaults_and_canary(tmp_path: Path, instruction: str) -> None:
    task = make_task(tmp_path, instruction=instruction)
    source = NativeTaskSource([SourceMapping(local_root=tmp_path, platform_prefix="data")])
    metadata = source.read_metadata("data/task")
    assert metadata == {
        "task_id": "task",
        "instruction": strip_canary(instruction),
        "task_config": TaskConfig.model_validate_toml("").model_dump(mode="json"),
    }
    assert isinstance(source.read_config("data/task"), TaskConfig)
    assert source.resolve_task("data/task") == task
    assert source.key_for_path(task) == "data/task"
    assert source.key_for_path(tmp_path) == "data"
    assert source.resolve_task("data") == tmp_path


def test_config_only_does_not_read_instruction(source: NativeTaskSource) -> None:
    task = source.resolve_task("datasets/native/task")
    (task / "instruction.md").unlink()
    assert source.read_config("datasets/native/task") == TaskConfig.model_validate_toml("")
    with pytest.raises(FileNotFoundError):
        source.read_metadata("datasets/native/task")


def test_no_payload_traversal_or_filesystem_cache(source: NativeTaskSource, monkeypatch: pytest.MonkeyPatch) -> None:
    task = source.resolve_task("datasets/native/task")
    # Neither an invalid hidden tree nor absent payloads affect metadata reads.
    (task / "tests").symlink_to("/not-a-real-hidden-tree")
    (task / "solution").symlink_to("/not-a-real-solution-tree")
    original = Path.read_text
    opened = []

    def track(path: Path, *args: Any, **kwargs: Any) -> str:
        opened.append(path)
        assert path in {task / "task.toml", task / "instruction.md"}
        return original(path, *args, **kwargs)

    def forbidden(*args: Any, **kwargs: Any) -> Any:
        pytest.fail("metadata reader traversed a payload tree")

    monkeypatch.setattr(Path, "read_text", track)
    for method in ("rglob", "glob", "iterdir", "walk"):
        monkeypatch.setattr(Path, method, forbidden)
    monkeypatch.setattr(os, "scandir", forbidden)
    monkeypatch.setattr(os, "walk", forbidden)
    assert source.read_metadata("datasets/native/task")["instruction"] == "Do the work.\n"
    (task / "instruction.md").write_text("New instruction", encoding="utf-8")
    assert source.read_metadata("datasets/native/task")["instruction"] == "New instruction"
    assert opened == [task / "task.toml", task / "instruction.md"] * 2


@pytest.mark.parametrize("bad", ["", "/abs", "a/../b", "a/./b", "a//b", "a/", "a\\b", "a\x00b", "a\x7fb"])
def test_invalid_keys_and_prefixes(source: NativeTaskSource, bad: str) -> None:
    with pytest.raises(ValueError):
        source.resolve_task(bad)
    with pytest.raises(ValidationError):
        SourceMapping(local_root=source.sources[0].local_root, platform_prefix=bad)


def test_source_mapping_validation(tmp_path: Path) -> None:
    first = tmp_path / "first"
    second = tmp_path / "second"
    first.mkdir()
    second.mkdir()
    with pytest.raises(ValidationError, match="extra_forbidden"):
        SourceMapping.model_validate({"local_root": first, "platform_prefix": "a", "unexpected": True})
    with pytest.raises(FileNotFoundError):
        NativeTaskSource([{"local_root": tmp_path / "missing", "platform_prefix": "a"}])
    plain_file = tmp_path / "file"
    plain_file.touch()
    with pytest.raises(NotADirectoryError):
        NativeTaskSource([{"local_root": plain_file, "platform_prefix": "a"}])
    for left, right, prefix in [
        (first, second, "a"),
        (first, second, "a/b"),
        (first, first, "b"),
        (tmp_path, first, "b"),
    ]:
        with pytest.raises(ValueError, match="overlapping"):
            NativeTaskSource(
                [{"local_root": left, "platform_prefix": "a"}, {"local_root": right, "platform_prefix": prefix}]
            )
    # Component boundaries, not string startswith, define overlap.
    source = NativeTaskSource(
        [{"local_root": first, "platform_prefix": "a"}, {"local_root": second, "platform_prefix": "ab"}]
    )
    assert source.resolve_task("ab") == second
    assert source.key_for_path(second) == "ab"
    assert [mapping.platform_prefix for mapping in source.sources] == ["a", "ab"]
    with pytest.raises(ValueError, match="exactly one"):
        source.resolve_task("abc")
    with pytest.raises(ValueError, match="exactly one"):
        source.key_for_path(tmp_path)


def test_missing_malformed_and_non_file_inputs(source: NativeTaskSource) -> None:
    key = "datasets/native/task"
    task = source.resolve_task(key)
    with pytest.raises(FileNotFoundError):
        source.resolve_task("datasets/native/missing")
    with pytest.raises(NotADirectoryError):
        source.resolve_task(key + "/task.toml")
    with pytest.raises(NotADirectoryError):
        source.key_for_path(task / "task.toml")
    (task / "task.toml").write_text("not valid TOML", encoding="utf-8")
    with pytest.raises(ValueError):
        source.read_config(key)
    (task / "task.toml").write_text('[environment]\ncpu = "not-a-number"\ncpus = "bad"', encoding="utf-8")
    with pytest.raises(ValidationError):
        source.read_config(key)
    (task / "task.toml").unlink()
    with pytest.raises(FileNotFoundError):
        source.read_config(key)
    (task / "task.toml").mkdir()
    with pytest.raises(ValueError, match="regular file"):
        source.read_config(key)


@pytest.mark.parametrize("filename", ["task.toml", "instruction.md"])
def test_metadata_symlink_escape_rejected(tmp_path: Path, filename: str) -> None:
    root = tmp_path / "root"
    task = make_task(root)
    sibling = make_task(root, "sibling")
    source = NativeTaskSource([{"local_root": root, "platform_prefix": "data"}])
    (task / filename).unlink()
    (task / filename).symlink_to(sibling / filename)
    with pytest.raises(ValueError, match="escapes task"):
        source.read_metadata("data/task")


def test_task_escape_and_contained_symlinks(tmp_path: Path) -> None:
    root = tmp_path / "root"
    task = make_task(root)
    outside = make_task(tmp_path, "outside")
    (root / "escape").symlink_to(outside, target_is_directory=True)
    source = NativeTaskSource([{"local_root": root, "platform_prefix": "data"}])
    with pytest.raises(ValueError, match="escapes source"):
        source.resolve_task("data/escape")
    with pytest.raises(ValueError, match="exactly one"):
        source.key_for_path(root / "escape")
    instruction = task / "instruction.md"
    instruction.rename(task / "real-instruction.md")
    instruction.symlink_to("real-instruction.md")
    assert source.read_metadata("data/task")["instruction"] == "Do the work.\n"
    alias = tmp_path / "root-alias"
    alias.symlink_to(root, target_is_directory=True)
    assert NativeTaskSource([{"local_root": alias, "platform_prefix": "data"}]).sources[0].local_root == root
    with pytest.raises(ValueError, match="overlapping"):
        NativeTaskSource([{"local_root": root, "platform_prefix": "a"}, {"local_root": alias, "platform_prefix": "b"}])


def test_metadata_identity_canonical_defaults_and_canary(source: NativeTaskSource) -> None:
    full = source.read_metadata("datasets/native/task")
    minimal = {"task_id": "some-other-location", "task_config": {}, "instruction": "# Canary\n\nDo the work.\n"}
    identity = metadata_identity(full)
    assert len(identity) == 64
    assert identity == metadata_identity(minimal)
    assert identity == metadata_identity({**full, "task_config": dict(reversed(list(full["task_config"].items())))})
    assert identity != metadata_identity({**full, "instruction": "Different work"})
    assert identity != metadata_identity({**minimal, "task_config": {"environment": {"cpus": 2}}})
    assert set(full) == {"task_id", "instruction", "task_config"}


@pytest.mark.asyncio
async def test_duplicates_full_keys_and_order(tmp_path: Path) -> None:
    first, second = tmp_path / "a", tmp_path / "b"
    make_task(first, instruction="First")
    make_task(second, instruction="Second")
    source = NativeTaskSource(
        [{"local_root": first, "platform_prefix": "a"}, {"local_root": second, "platform_prefix": "b"}]
    )
    result = await source.read_many(["b/task", "a/task", "b/task"], workers=1)
    assert list(result) == ["b/task", "a/task"]
    assert result["a/task"]["task_id"] == result["b/task"]["task_id"] == "task"
    assert result["a/task"]["instruction"] == "First"
    assert result["b/task"]["instruction"] == "Second"
    assert await NativeTaskSource([]).read_many([]) == {}


@pytest.mark.asyncio
@pytest.mark.parametrize("workers", [0, -1, 1.5, True, "2", None])
@pytest.mark.parametrize("method", ["read_many", "read_configs"])
async def test_worker_validation(source: NativeTaskSource, workers: Any, method: str) -> None:
    with pytest.raises(ValueError, match="positive integer"):
        await getattr(source, method)([], workers=workers)


@pytest.mark.asyncio
async def test_small_batch_direct_and_event_loop_responsive(
    source: NativeTaskSource, monkeypatch: pytest.MonkeyPatch
) -> None:
    started, release = Event(), Event()
    original = source.read_metadata

    def read(key: str) -> dict[str, Any]:
        started.set()
        assert release.wait(5), "event loop blocked by synchronous metadata reading"
        return original(key)

    def forbidden(*args: Any, **kwargs: Any) -> Any:
        pytest.fail("small batch should not create a process pool")

    monkeypatch.setattr(source, "read_metadata", read)
    monkeypatch.setattr(reader, "ProcessPoolExecutor", forbidden)
    job = asyncio.create_task(source.read_many(["datasets/native/task"], workers=8))
    try:
        assert await asyncio.to_thread(started.wait, 5)
        release.set()
        assert await job == {"datasets/native/task": original("datasets/native/task")}
    finally:
        release.set()
        if not job.done():
            job.cancel()
            with pytest.raises(asyncio.CancelledError):
                await job


@pytest.mark.asyncio
async def test_parallel_parity_and_worker_cap(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    keys = []
    for index in range(130):
        make_task(tmp_path, f"t{index}", instruction=f"# Canary\n\nInstruction {index}")
        keys.append(f"data/t{index}")
    source = NativeTaskSource([{"local_root": tmp_path, "platform_prefix": "data"}])
    pools = []

    class ObservedPool(ProcessPoolExecutor):
        def __init__(self, *args: Any, **kwargs: Any) -> None:
            pools.append(kwargs)
            super().__init__(*args, **kwargs)

    monkeypatch.setattr(reader, "ProcessPoolExecutor", ObservedPool)
    ordered = list(reversed(keys)) + keys
    parallel = await source.read_many(ordered, workers=8)
    direct = await source.read_many(ordered, workers=1)
    assert parallel == direct
    assert list(parallel) == list(reversed(keys))
    assert len(pools) == 1
    assert pools[0]["max_workers"] == 3
    assert pools[0]["mp_context"].get_start_method() == "spawn"


@pytest.mark.asyncio
async def test_parallel_failure_propagates(tmp_path: Path) -> None:
    keys = []
    for index in range(65):
        task = make_task(tmp_path, f"t{index}")
        keys.append(f"data/t{index}")
    (task / "instruction.md").unlink()
    source = NativeTaskSource([{"local_root": tmp_path, "platform_prefix": "data"}])
    with pytest.raises(FileNotFoundError):
        await source.read_many(keys, workers=2)


@pytest.mark.asyncio
@pytest.mark.parametrize("method", ["read_many", "read_configs"])
async def test_bounded_submission_and_cancellation_cleanup(
    source: NativeTaskSource, monkeypatch: pytest.MonkeyPatch, method: str
) -> None:
    submitted, shutdown = Event(), Event()
    futures: list[Future[Any]] = []
    batches: list[list[str]] = []

    class BlockedPool:
        def __init__(self, *, max_workers: int, mp_context: Any) -> None:
            assert max_workers == 2
            assert mp_context.get_start_method() == "spawn"

        def submit(self, fn: Any, read: Any, batch: list[str]) -> Future[Any]:
            assert fn is reader._read_batch
            assert read.__self__ is source
            assert read.__name__ == ("read_config" if method == "read_configs" else "read_metadata")
            future: Future[Any] = Future()
            futures.append(future)
            batches.append(batch)
            if len(futures) == 4:
                submitted.set()
            return future

        def shutdown(self, *, wait: bool, cancel_futures: bool) -> None:
            assert wait and cancel_futures
            assert all(future.cancelled() for future in futures)
            shutdown.set()

    monkeypatch.setattr(reader, "ProcessPoolExecutor", BlockedPool)
    keys = [f"datasets/native/t{i}" for i in range(640)]
    job = asyncio.create_task(getattr(source, method)(keys, workers=2))
    assert await asyncio.to_thread(submitted.wait, 5)
    job.cancel()
    with pytest.raises(asyncio.CancelledError):
        await asyncio.wait_for(job, timeout=5)
    assert shutdown.is_set()
    assert len(futures) == 4
    assert all(len(batch) == 64 for batch in batches)


@pytest.mark.asyncio
async def test_direct_cancellation_joins_active_read(source: NativeTaskSource, monkeypatch: pytest.MonkeyPatch) -> None:
    started, release, finished = Event(), Event(), Event()
    calls = []

    def read(key: str) -> dict[str, Any]:
        calls.append(key)
        started.set()
        assert release.wait(5)
        finished.set()
        return {}

    monkeypatch.setattr(source, "read_metadata", read)
    job = asyncio.create_task(source.read_many(["datasets/native/one", "datasets/native/two"], workers=1))
    assert await asyncio.to_thread(started.wait, 5)
    job.cancel()
    await asyncio.sleep(0)
    job.cancel()  # Repeated cancellation must not abandon resource cleanup.
    release.set()
    with pytest.raises(asyncio.CancelledError):
        await asyncio.wait_for(job, timeout=5)
    assert finished.is_set()
    assert calls == ["datasets/native/one"]


@pytest.mark.asyncio
async def test_read_configs_parallel_parity_without_instruction_or_payloads(tmp_path: Path) -> None:
    keys = []
    for index in range(65):
        task = make_task(tmp_path, f"t{index}", config=f"[environment]\ncpus = {index + 1}\n")
        (task / "instruction.md").unlink()
        keys.append(f"data/t{index}")
    source = NativeTaskSource([{"local_root": tmp_path, "platform_prefix": "data"}])
    ordered = list(reversed(keys)) + keys
    parallel = await source.read_configs(ordered, workers=8)
    direct = await source.read_configs(ordered, workers=1)
    assert parallel == direct
    assert list(parallel) == list(reversed(keys))
    assert all(isinstance(config, TaskConfig) for config in parallel.values())
    assert parallel["data/t0"].environment.cpus == 1
    assert parallel["data/t64"].environment.cpus == 65
    assert await source.read_configs([]) == {}
    (tmp_path / "t0" / "task.toml").write_text("not valid TOML", encoding="utf-8")
    with pytest.raises(ValueError):
        await source.read_configs(keys, workers=2)
