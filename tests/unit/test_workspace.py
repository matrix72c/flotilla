"""Attempt ownership, cancellation settlement, and independent copy contracts."""

from __future__ import annotations

import asyncio
from dataclasses import replace
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
