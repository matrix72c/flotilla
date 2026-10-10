"""任务发布 `flotilla publish`（§4.7）：FILES.json、上传、原子改名、幂等与冲突。"""

from __future__ import annotations

import hashlib
import json
import os
from collections.abc import AsyncIterator
from pathlib import Path

import pytest
import pytest_asyncio

from flotilla.core.anchor import Anchor, AnchorSettings
from flotilla.manifest import Manifest
from flotilla.platform.base import ErrorCategory, ExecResult, FlotillaError, InstanceHandle, ProcessSpec
from flotilla.platform.fake import FakePlatform, ManualClock, SharedTree
from flotilla.share.publish import (
    FILES_JSON,
    PublishError,
    entries,
    files_json,
    mark_published,
    publish_task_files,
)

KEY = "a" * 64
ROOT = "pvc:data/opt/flotilla"


@pytest_asyncio.fixture
async def anchor(platform: FakePlatform, clock: ManualClock, tree: SharedTree) -> AsyncIterator[Anchor]:
    a = Anchor(platform, clock, "L1", AnchorSettings(image="anchor@sha256:abc"))
    await clock.run(a.start())
    yield a
    await a.close()


def _files(root: Path) -> Path:
    files = root / "files"
    (files / "binds" / "0").mkdir(parents=True)
    (files / "binds" / "0" / "entrypoint.sh").write_text("#!/bin/sh\necho hi\n")
    (files / "binds" / "0" / "entrypoint.sh").chmod(0o755)
    (files / "seeds").mkdir()
    (files / "seeds" / "data.tar").write_bytes(b"tar-bytes")
    return files


# ───────────────────────────── FILES.json ─────────────────────────────


def test_entries_cover_every_kind(tmp_path: Path) -> None:
    files = _files(tmp_path)
    os.symlink("entrypoint.sh", files / "binds" / "0" / "link")
    by_path = {str(e["path"]): e for e in entries(files)}
    assert by_path["binds"]["type"] == "dir"
    script = by_path["binds/0/entrypoint.sh"]
    assert script["type"] == "file" and script["mode"] == "0755" and script["size"] == 18
    assert script["sha256"] == hashlib.sha256((files / "binds/0/entrypoint.sh").read_bytes()).hexdigest()
    assert by_path["binds/0/link"] == {
        **by_path["binds/0/link"],
        "type": "link",
        "target": "entrypoint.sh",
    }
    # 按路径排序，便于人读与比对。
    assert [str(e["path"]) for e in entries(files)] == sorted(str(e["path"]) for e in entries(files))


def test_files_json_is_stable_and_content_sensitive(tmp_path: Path) -> None:
    a, b = tmp_path / "a", tmp_path / "b"
    _files(a)
    _files(b)
    assert files_json(a / "files")[1] == files_json(b / "files")[1]
    (a / "files" / "seeds" / "data.tar").write_bytes(b"different")
    assert files_json(a / "files")[1] != files_json(b / "files")[1]


# ───────────────────────────── 发布 ─────────────────────────────


@pytest.mark.asyncio
async def test_publish_uploads_and_renames_atomically(
    anchor: Anchor, clock: ManualClock, tree: SharedTree, tmp_path: Path
) -> None:
    files = _files(tmp_path)
    result = await clock.run(publish_task_files(anchor, KEY, files, storage_root=ROOT))
    assert result.task_files == f"tasks/{KEY}" and result.uploaded == 2 and not result.skipped
    # 正式目录下有内容与 FILES.json；staging 不留下。
    assert f"tasks/{KEY}/binds/0/entrypoint.sh" in tree.files
    assert tree.content(f"tasks/{KEY}/seeds/data.tar") == b"tar-bytes"
    assert json.loads(tree.content(f"tasks/{KEY}/{FILES_JSON}"))
    # staging 的那一份不留（`tasks/.staging` 这个空父目录留着无妨）。
    assert not any(p.startswith("tasks/.staging/") for p in {*tree.dirs, *tree.files})
    # 先传 staging、最后一次 mv：中途失败不会留下半个正式目录。
    assert any(e.startswith("mv tasks/.staging/") and e.endswith(f"tasks/{KEY}") for e in tree.events)


@pytest.mark.asyncio
async def test_publish_preserves_mode_and_owner(
    anchor: Anchor, clock: ManualClock, tree: SharedTree, tmp_path: Path
) -> None:
    files = _files(tmp_path)
    await clock.run(publish_task_files(anchor, KEY, files, storage_root=ROOT))
    assert tree.modes[f"tasks/{KEY}/binds/0/entrypoint.sh"] == "0755"
    assert tree.owners[f"tasks/{KEY}/binds/0/entrypoint.sh"] == f"{os.getuid()}:{os.getgid()}"


@pytest.mark.asyncio
async def test_publish_is_idempotent(anchor: Anchor, clock: ManualClock, tree: SharedTree, tmp_path: Path) -> None:
    files = _files(tmp_path)
    first = await clock.run(publish_task_files(anchor, KEY, files, storage_root=ROOT))
    second = await clock.run(publish_task_files(anchor, KEY, files, storage_root=ROOT))
    assert not first.skipped and second.skipped and second.uploaded == 0
    assert second.files_sha256 == first.files_sha256


@pytest.mark.asyncio
async def test_publish_refuses_to_overwrite_different_content(
    anchor: Anchor, clock: ManualClock, tree: SharedTree, tmp_path: Path
) -> None:
    files = _files(tmp_path)
    await clock.run(publish_task_files(anchor, KEY, files, storage_root=ROOT))
    (files / "seeds" / "data.tar").write_bytes(b"tampered")
    with pytest.raises(PublishError, match="不一致"):
        await clock.run(publish_task_files(anchor, KEY, files, storage_root=ROOT))
    assert tree.content(f"tasks/{KEY}/seeds/data.tar") == b"tar-bytes"  # 原内容没被覆盖


@pytest.mark.asyncio
async def test_publish_cleans_staging_on_failure(
    anchor: Anchor, clock: ManualClock, tree: SharedTree, platform: FakePlatform, tmp_path: Path
) -> None:
    files = _files(tmp_path)
    platform.inject_fault(
        "write_file", FlotillaError("boom", stage="prepare", category=ErrorCategory.TRANSIENT, retryable=True)
    )
    with pytest.raises(FlotillaError):
        await clock.run(publish_task_files(anchor, KEY, files, storage_root=ROOT))
    assert not any(p.startswith("tasks/.staging/") for p in tree.dirs)  # 失败后不留 staging 的那一份
    assert f"tasks/{KEY}" not in tree.dirs  # 也没有半个正式目录


@pytest.mark.asyncio
async def test_publish_detects_corrupted_upload(
    anchor: Anchor, clock: ManualClock, tree: SharedTree, platform: FakePlatform, tmp_path: Path
) -> None:
    files = _files(tmp_path)
    real_read = platform.read_file

    async def corrupt(handle: InstanceHandle, path: str) -> bytes:
        data = await real_read(handle, path)
        return data + b"extra" if path.endswith("data.tar") else data

    platform.read_file = corrupt  # type: ignore[method-assign]
    with pytest.raises(PublishError, match="校验不一致"):
        await clock.run(publish_task_files(anchor, KEY, files, storage_root=ROOT))


# ───────────────────────────── 清单 ─────────────────────────────


def _manifest() -> Manifest:
    return Manifest.model_validate(
        {
            "schema": 2,
            "task": "ds/t",
            "build_key": KEY,
            "agent_service": "main",
            "units": {
                "main": {
                    "image": "r.io/a@sha256:" + "b" * 64,
                    "command": ["sh", "-c", "sleep infinity"],
                    "context": {"env": {}, "uid": 0, "gid": 0, "cwd": "/"},
                    "resources": {"cpu": "1", "memory": "1Gi"},
                }
            },
        }
    )


@pytest.mark.asyncio
async def test_mark_published_fills_manifest(
    anchor: Anchor, clock: ManualClock, tree: SharedTree, tmp_path: Path
) -> None:
    files = _files(tmp_path)
    result = await clock.run(publish_task_files(anchor, KEY, files, storage_root=ROOT))
    published = mark_published(_manifest(), result, ROOT)
    assert published.task_files == f"tasks/{KEY}"
    assert published.task_files_published is not None
    assert published.task_files_published.storage_root == ROOT
    assert published.task_files_published.files_sha256 == files_json(files)[1]
    # 清单仍能原样读回（schema 不变）。
    assert Manifest.load_json(published.dump_json()).task_files == f"tasks/{KEY}"


@pytest.mark.asyncio
async def test_exists_does_not_read_missing_binary_as_absent(
    anchor: Anchor, clock: ManualClock, tree: SharedTree, platform: FakePlatform
) -> None:
    """锚点镜像缺某个 applet 时退出码 127（"not found"）。

    真实部署上踩过：当时 `exists` 用 `test -e`，而锚点镜像里没有 `test`，127 被当成"文件不存在"，
    于是已发布的任务被重复上传。缺命令必须按故障抛出，不能当成答案。
    """

    def not_found(iid: str, proc: ProcessSpec) -> ExecResult:
        if proc.argv[0] == "ls":
            return ExecResult(exit_code=127, stdout=b"", stderr=b"sh: ls: not found")
        return ExecResult(exit_code=0, stdout=b"", stderr=b"")

    platform.set_exec_handler(not_found)
    with pytest.raises(FlotillaError):
        await clock.run(anchor.exists("tasks/x", stage="prepare"))


@pytest.mark.asyncio
async def test_publish_rejects_symlinks_in_task_files(
    anchor: Anchor, clock: ManualClock, tree: SharedTree, tmp_path: Path
) -> None:
    files = _files(tmp_path)
    os.symlink("data.tar", files / "seeds" / "alias.tar")
    with pytest.raises(PublishError, match="符号链接"):
        await clock.run(publish_task_files(anchor, KEY, files, storage_root=ROOT))
