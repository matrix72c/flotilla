"""Attempt ownership, cancellation settlement, and independent copy contracts."""

from __future__ import annotations

import asyncio
import json
from dataclasses import replace
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, Mock

import pytest

from flotilla.core.plan import ExecContext, TrialPlan, Unit
from flotilla.platform.base import Resources, SharedVolume
from flotilla.runtime.workspace import WorkspaceInfo, WorkspaceStore, relative_key
from flotilla.testing.config import config


def store() -> tuple[WorkspaceStore, Mock]:
    anchor = Mock(launch_id="launch-test")
    for name in ("copy_source_tree", "remove", "mkdir", "upload", "download", "copy_tree", "copy_contents"):
        setattr(anchor, name, AsyncMock())
    return WorkspaceStore(anchor, config()), anchor


@pytest.mark.parametrize("key", ["", "/tasks/a", "a//b", "a/../b", "a/./b", "a\\b", "a\nb", None])
def test_relative_key_rejects_ambiguous_paths(key: str | None) -> None:
    with pytest.raises(ValueError):
        relative_key(key)  # type: ignore[arg-type]  # Exercise invalid runtime input.


@pytest.mark.asyncio
async def test_same_basename_and_repeated_keys_create_independent_attempts() -> None:
    workspaces, anchor = store()
    a, b, c = [await workspaces.create(key) for key in ("dataset/one/t", "dataset/two/t", "dataset/one/t")]
    assert a.task_id == b.task_id == c.task_id == "t"
    assert len({a.id, b.id, c.id}) == 3
    for value in (a, b, c):
        assert value.source_key == f"{value.key}/source/t"
        anchor.copy_source_tree.assert_any_await(value.task_key, value.source_key, stage="prepare")
    await workspaces.remove(a.id)
    assert a.id not in workspaces.records
    assert workspaces.owned(b.id) and workspaces.owned(c.id)
    anchor.remove.assert_awaited_once_with(a.key, stage="run")


@pytest.mark.asyncio
async def test_cancelled_copy_settles_before_removing_workspace() -> None:
    workspaces, anchor = store()
    started, finish = asyncio.Event(), asyncio.Event()

    async def copy(*args: object, **kwargs: object) -> None:
        started.set()
        await finish.wait()

    anchor.copy_source_tree.side_effect = copy
    task = asyncio.create_task(workspaces.create("dataset/t"))
    await started.wait()
    task.cancel()
    await asyncio.sleep(0)
    assert not task.done()
    anchor.remove.assert_not_awaited()
    finish.set()
    with pytest.raises(asyncio.CancelledError):
        await task
    anchor.remove.assert_awaited_once()
    assert not workspaces.records and not workspaces.jobs


@pytest.mark.asyncio
async def test_copy_failure_preserves_cleanup_failure_for_retry() -> None:
    workspaces, anchor = store()
    anchor.copy_source_tree.side_effect = ValueError("copy failed")
    anchor.remove.side_effect = RuntimeError("delete failed")
    with pytest.raises(ValueError, match="copy failed") as failure:
        await workspaces.create("dataset/t")
    assert "delete failed" in str(failure.value.__notes__)
    [wid] = workspaces.records
    with pytest.raises(ValueError, match="released"):
        workspaces.owned(wid)
    anchor.remove.side_effect = None
    await workspaces.remove(wid)
    assert not workspaces.records


@pytest.mark.asyncio
async def test_workspace_removal_waits_for_inflight_operation() -> None:
    workspaces, anchor = store()
    info = await workspaces.create("dataset/t")
    started, finish = asyncio.Event(), asyncio.Event()

    async def operation(_: WorkspaceInfo) -> None:
        started.set()
        await finish.wait()

    task = asyncio.create_task(workspaces.operation(info.id, operation))
    await started.wait()
    removal = asyncio.create_task(workspaces.remove(info.id))
    await asyncio.sleep(0)
    anchor.remove.assert_not_awaited()
    finish.set()
    await task
    await removal
    with pytest.raises(ValueError):
        await workspaces.read(info.id, "result.json")


@pytest.mark.asyncio
async def test_copy_and_contents_have_distinct_anchor_contracts() -> None:
    workspaces, anchor = store()
    info = await workspaces.create("dataset/t")
    await workspaces.copy(info.id, "source/t/tests", "private-tests")
    await workspaces.copy_contents(info.id, "private-tests", "roles/main/tests")
    anchor.copy_tree.assert_awaited_once_with(
        info.key + "/source/t/tests", info.key + "/private-tests", stage="prepare"
    )
    anchor.copy_contents.assert_awaited_once_with(
        info.key + "/private-tests", info.key + "/roles/main/tests", stage="prepare"
    )
    with pytest.raises(ValueError):
        await workspaces.write(info.id, "../other", b"x")
    anchor.upload.assert_not_awaited()


@pytest.mark.asyncio
async def test_store_shutdown_rejects_new_work() -> None:
    workspaces, anchor = store()
    await workspaces.settle()
    with pytest.raises(RuntimeError, match="closing"):
        await workspaces.create("dataset/t")
    assert not workspaces.records
    anchor.copy_source_tree.assert_not_awaited()


def metadata_store() -> tuple[WorkspaceStore, Mock, list[dict[str, Any]]]:
    workspaces, anchor = store()
    requests: list[dict[str, Any]] = []
    anchor.path.side_effect = lambda key: "/storage/" + key
    anchor.stat = AsyncMock(return_value=SimpleNamespace(size=1))
    anchor.execute = AsyncMock(return_value=SimpleNamespace(exit_code=0, timed_out=False))

    async def upload(key: str, data: bytes, **kwargs: Any) -> None:
        requests.append(json.loads(data))

    async def download(key: str, **kwargs: Any) -> bytes:
        return json.dumps(
            {key: {"instruction": key, "task_config": {"metadata": {"key": key}}} for key in requests[-1]["task_keys"]}
        ).encode()

    anchor.upload.side_effect = upload
    anchor.download.side_effect = download
    return workspaces, anchor, requests


@pytest.mark.asyncio
async def test_metadata_batches_deduplicate_full_keys_without_attempts_or_copies() -> None:
    workspaces, anchor, requests = metadata_store()
    keys = [f"dataset/{index}/same" for index in range(260)]
    result = await workspaces.describe_tasks([*keys, keys[0]])
    assert list(result) == keys
    assert [len(request["task_keys"]) for request in requests] == [128, 128, 4]
    assert len({request["workspace"] for request in requests}) == 3
    assert all("/launches/launch-test/metadata/" in request["workspace"] for request in requests)
    assert all(result[key]["instruction"] == key for key in keys)
    assert not workspaces.records and not workspaces.jobs
    anchor.copy_source_tree.assert_not_awaited()
    anchor.copy_tree.assert_not_awaited()
    anchor.copy_contents.assert_not_awaited()
    assert anchor.remove.await_count == 3
    for call in anchor.execute.await_args_list:
        assert call.args[0][:-2] == workspaces.config.prepare.workspace_command
        assert call.args[0][-2] == "--describe-tasks"
        assert call.kwargs["timeout_s"] == workspaces.config.prepare.workspace_timeout


@pytest.mark.asyncio
async def test_metadata_chunks_bound_encoded_bytes(monkeypatch: pytest.MonkeyPatch) -> None:
    from flotilla.runtime import workspace

    workspaces, anchor, requests = metadata_store()
    monkeypatch.setattr(workspace, "METADATA_REQUEST_BYTES", 400)
    keys = [f"dataset/{i}/" + 'é"' * 20 for i in range(3)]
    assert list(await workspaces.describe_tasks(keys)) == keys
    assert [len(request["task_keys"]) for request in requests] == [1, 1, 1]
    assert all(len(call.args[1]) <= 400 for call in anchor.upload.await_args_list)
    with pytest.raises(ValueError, match="request byte limit"):
        await workspaces.describe_tasks(["dataset/" + "x" * 400])
    assert len(requests) == 3


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "key", ["", "/tasks/a", "a/../b", "a/./b", "a//b", "a\\b", "a\nb", "a\x7fb", "share/t", "launches/L/t", None]
)
async def test_metadata_rejects_noncanonical_and_runtime_keys_before_io(key: Any) -> None:
    workspaces, anchor, _ = metadata_store()
    with pytest.raises(ValueError):
        await workspaces.describe_tasks(["dataset/valid", key])
    anchor.mkdir.assert_not_awaited()


@pytest.mark.asyncio
async def test_metadata_empty_input_and_string_misuse() -> None:
    workspaces, anchor, _ = metadata_store()
    assert await workspaces.describe_tasks([]) == {}
    with pytest.raises(TypeError, match="sequence"):
        await workspaces.describe_tasks("dataset/task")
    anchor.mkdir.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "result",
    [
        [],
        {},
        {"other": {}},
        {"dataset/t": []},
        {"dataset/t": {"instruction": 1, "task_config": {}}},
        {"dataset/t": {"instruction": "x", "task_config": []}},
        {"dataset/t": {"instruction": "x", "task_config": {}, "tests": {}}},
    ],
)
async def test_metadata_validates_exact_result_keys_and_shapes(result: Any) -> None:
    workspaces, anchor, _ = metadata_store()
    anchor.download.side_effect = None
    anchor.download.return_value = json.dumps(result).encode()
    with pytest.raises(ValueError, match="metadata compiler returned"):
        await workspaces.describe_tasks(["dataset/t"])
    anchor.remove.assert_awaited_once()


@pytest.mark.asyncio
@pytest.mark.parametrize("failure", ["exit", "timeout", "transport", "size", "download-size", "malformed"])
async def test_metadata_failures_never_return_stale_or_partial_results(
    failure: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    from flotilla.runtime import workspace

    workspaces, anchor, _ = metadata_store()
    monkeypatch.setattr(workspace, "METADATA_RESULT_BYTES", 100)
    if failure == "exit":
        anchor.execute.return_value.exit_code = 1
    elif failure == "timeout":
        anchor.execute.return_value.timed_out = True
    elif failure == "transport":
        anchor.execute.side_effect = TimeoutError("remote timeout")
    elif failure == "size":
        anchor.stat.return_value.size = 101
    else:
        anchor.download.side_effect = None
        anchor.download.return_value = b"x" * (101 if failure == "download-size" else 1)
    with pytest.raises((RuntimeError, ValueError, TimeoutError)):
        await workspaces.describe_tasks(["dataset/t"])
    if failure in {"exit", "timeout", "transport", "size"}:
        anchor.download.assert_not_awaited()
    anchor.remove.assert_awaited_once()


@pytest.mark.asyncio
@pytest.mark.parametrize("primary_failure", [False, True])
async def test_metadata_cleanup_failure_is_explicit_and_launch_owned(primary_failure: bool) -> None:
    workspaces, anchor, _ = metadata_store()
    anchor.remove.side_effect = OSError("cleanup failed")
    if primary_failure:
        anchor.execute.side_effect = ValueError("primary compiler failure")
        with pytest.raises(ValueError, match="primary compiler failure") as failure:
            await workspaces.describe_tasks(["dataset/t"])
        assert "cleanup failed" in str(failure.value.__notes__)
    else:
        with pytest.raises(OSError, match="cleanup failed"):
            await workspaces.describe_tasks(["dataset/t"])
    assert anchor.remove.await_args.args[0].startswith("launches/launch-test/metadata/")
    assert not workspaces.records and not workspaces.jobs


@pytest.mark.asyncio
@pytest.mark.parametrize("cleanup_failure", [False, True])
async def test_metadata_cancellation_settles_current_chunk_and_cleanup_only(cleanup_failure: bool) -> None:
    workspaces, anchor, requests = metadata_store()
    started, finish = asyncio.Event(), asyncio.Event()

    async def execute(*args: Any, **kwargs: Any) -> SimpleNamespace:
        started.set()
        await finish.wait()
        return SimpleNamespace(exit_code=0, timed_out=False)

    anchor.execute.side_effect = execute
    if cleanup_failure:
        anchor.remove.side_effect = OSError("cleanup failed")
    task = asyncio.create_task(workspaces.describe_tasks([f"dataset/{i}" for i in range(129)]))
    await started.wait()
    task.cancel()
    await asyncio.sleep(0)
    task.cancel()
    await asyncio.sleep(0)
    assert not task.done()
    anchor.remove.assert_not_awaited()
    finish.set()
    with pytest.raises(asyncio.CancelledError) as cancelled:
        await task
    if cleanup_failure:
        assert "cleanup failed" in str(cancelled.value.__notes__)
    assert len(requests) == 1
    anchor.remove.assert_awaited_once()
    assert not workspaces.jobs


@pytest.mark.asyncio
async def test_metadata_reuses_compile_slots_and_shutdown_settlement() -> None:
    workspaces, anchor, _ = metadata_store()
    workspaces.compile_slots = asyncio.Semaphore(1)
    started, finish = asyncio.Event(), asyncio.Event()

    async def execute(*args: Any, **kwargs: Any) -> SimpleNamespace:
        started.set()
        await finish.wait()
        return SimpleNamespace(exit_code=0, timed_out=False)

    anchor.execute.side_effect = execute
    one = asyncio.create_task(workspaces.describe_tasks(["dataset/t"]))
    await started.wait()
    two = asyncio.create_task(workspaces.describe_tasks(["dataset/t"]))
    await asyncio.sleep(0)
    shutdown = asyncio.create_task(workspaces.settle())
    await asyncio.sleep(0)
    assert not shutdown.done()
    assert anchor.execute.await_count == 1
    finish.set()
    await asyncio.gather(one, two, shutdown)
    assert anchor.execute.await_count == 2
    with pytest.raises(RuntimeError, match="closing"):
        await workspaces.describe_tasks(["dataset/t"])


@pytest.mark.asyncio
async def test_public_describe_tasks_starts_and_uses_settled_worker_call(monkeypatch: pytest.MonkeyPatch) -> None:
    from flotilla.runtime.client import Flotilla

    fleet = Flotilla(deployment=config())
    start = AsyncMock()
    monkeypatch.setattr(fleet, "start", start)
    manager = SimpleNamespace(workspaces=SimpleNamespace(describe_tasks=AsyncMock(return_value={"dataset/t": {}})))

    async def call(action: Any, **kwargs: Any) -> Any:
        assert kwargs == {"settle_cancel": True}
        return await action(manager)

    monkeypatch.setattr(fleet, "_call", AsyncMock(side_effect=call))
    assert await fleet.describe_tasks(["dataset/t"]) == {"dataset/t": {}}
    start.assert_awaited_once()
    manager.workspaces.describe_tasks.assert_awaited_once_with(["dataset/t"])


def test_workspace_mounts_do_not_relax_external_mount_guarantees() -> None:
    unit = Unit(
        image="img",
        resources=Resources(cpu="1", memory="1Gi"),
        context=ExecContext(env={}, uid=0, gid=0, cwd="/"),
        command=None,
    )
    root = "launches/L/attempts/A"
    mount = SharedVolume(key=root + "/scratch", mount_path="/handoff", read_only=False)
    plan = TrialPlan(
        task="t", task_files=None, units={"main": unit}, workspace_root=root, workspace_mounts={"main": (mount,)}
    )
    plan.validate()
    with pytest.raises(ValueError, match="read-only"):
        replace(plan, workspace_mounts={}, external_mounts={"main": (mount,)}).validate()
    with pytest.raises(ValueError, match="escapes"):
        replace(plan, workspace_root="launches/L/attempts/B").validate()
    with pytest.raises(ValueError, match="valid relative"):
        replace(plan, workspace_mounts={"main": (replace(mount, key=root + "/../B"),)}).validate()
    with pytest.raises(ValueError, match="conflicts"):
        replace(plan, workspace_mounts={"main": (replace(mount, mount_path="/.flotilla"),)}).validate()
