"""share 发布的单元测试（Architecture §3.3）：清单格式、发布、校验、gc 与并发。

`RELEASE.json` 的序列化方式必须与 AFS 上真实发布（`2026-10-07.1`）逐字节一致，`test_matches_real_release`
钉住这一点 —— 部署配置里钉的 sha256 就是按这个方式算出来的。
"""

from __future__ import annotations

import hashlib
import json
import os
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

import pytest
import pytest_asyncio

from flotilla.core.anchor import Anchor, AnchorSettings
from flotilla.platform.base import ErrorCategory, FlotillaError
from flotilla.platform.fake import FakePlatform, ManualClock, SharedTree
from flotilla.share.release import (
    RELEASE_JSON,
    RELEASES,
    STAGING,
    ReleaseError,
    gc_releases,
    publish_release,
    release_entries,
    release_json,
    verify_release,
)

SHA = "65ebd81fd085c1d7a6eb4398cb1b3b050a1f249ec68b6f91c8d1e43d4c48cd36"


@pytest_asyncio.fixture
async def anchor(platform: FakePlatform, clock: ManualClock, tree: SharedTree) -> AsyncIterator[Anchor]:
    a = Anchor(platform, clock, "L1", AnchorSettings(image="anchor@sha256:abc"))
    await clock.run(a.start())
    yield a
    await a.close()


def _tree(root: Path, *, mode: int = 0o755) -> Path:
    """一份和真实发布同形的内容：bin/busybox + 一个顶层文件。"""
    (root / "bin").mkdir(parents=True)
    (root / "bin" / "busybox").write_bytes(b"\x00busybox-bytes")
    (root / "bin" / "busybox").chmod(mode)
    return root


# ───────────────────────────── RELEASE.json ─────────────────────────────


def test_release_json_shape_and_entries(tmp_path: Path) -> None:
    root = _tree(tmp_path / "src")
    blob, digest = release_json("r1", root, {"busybox": "1.36.1"})
    document = json.loads(blob)
    assert document["release"] == "r1"
    assert document["components"] == {"busybox": "1.36.1"}
    assert set(document["files"]) == {"bin/busybox"}
    entry = document["files"]["bin/busybox"]
    assert entry["mode"] == "0755"
    assert entry["size"] == len(b"\x00busybox-bytes")
    assert entry["sha256"] == hashlib.sha256(b"\x00busybox-bytes").hexdigest()
    assert digest == hashlib.sha256(blob).hexdigest()
    # 逐字节可复现：同样内容算两次是同一个摘要。
    assert release_json("r1", root, {"busybox": "1.36.1"}) == (blob, digest)


def test_matches_real_release(tmp_path: Path) -> None:
    """真实发布 2026-10-07.1 的 RELEASE.json 必须能被逐字节复现。

    这份内容是从 AFS 上读回来的（287 字节），sha256 与部署配置钉的值相同。
    """
    real = (
        b"{\n"
        b'  "components": {\n'
        b'    "busybox": "1.36.1 (alpine busybox-static 1.36.1-r31)"\n'
        b"  },\n"
        b'  "files": {\n'
        b'    "bin/busybox": {\n'
        b'      "mode": "0755",\n'
        b'      "sha256": "6d4ae568988ee24beb9dac4afdac4df67f90bbaed6ca47628da35ff5eb632a4c",\n'
        b'      "size": 1025960\n'
        b"    }\n"
        b"  },\n"
        b'  "release": "2026-10-07.1"\n'
        b"}\n"
    )
    assert len(real) == 287
    assert hashlib.sha256(real).hexdigest() == SHA
    # 形状一致：键序、缩进、尾换行都相同。内容用假字节，只比结构。
    root = tmp_path / "src"
    (root / "bin").mkdir(parents=True)
    (root / "bin" / "busybox").write_bytes(b"x")
    (root / "bin" / "busybox").chmod(0o755)
    blob, _ = release_json("2026-10-07.1", root, {"busybox": "1.36.1 (alpine busybox-static 1.36.1-r31)"})
    assert blob.decode().splitlines()[:4] == real.decode().splitlines()[:4]
    assert blob.endswith(b"\n")


def test_root_release_json_is_excluded(tmp_path: Path) -> None:
    """根目录里已有的 RELEASE.json 不进清单（它是清单本身），更深层的同名文件保留。"""
    root = _tree(tmp_path / "src")
    (root / RELEASE_JSON).write_text("{}\n")
    (root / "nested").mkdir()
    (root / "nested" / RELEASE_JSON).write_text("{}\n")
    paths = [rel for rel, _ in release_entries(root)]
    assert RELEASE_JSON not in paths
    assert f"nested/{RELEASE_JSON}" in paths


def test_rejects_symlink_and_oversized(tmp_path: Path) -> None:
    root = _tree(tmp_path / "src")
    os.symlink("busybox", root / "bin" / "link")
    with pytest.raises(ReleaseError, match="符号链接"):
        release_entries(root)

    root2 = _tree(tmp_path / "src2")
    (root2 / "big").write_bytes(b"x")
    big = root2 / "big"
    big.unlink()
    big.write_bytes(b"")
    with pytest.raises(ReleaseError, match="超过上限"):
        # 直接造一个超过上限的声明，避免真写 512MB。
        import flotilla.share.release as module

        original = module.MAX_FILE_BYTES
        module.MAX_FILE_BYTES = 0
        try:
            release_entries(root2)
        finally:
            module.MAX_FILE_BYTES = original


# ───────────────────────────── publish ─────────────────────────────


@pytest.mark.asyncio
async def test_publish_uploads_matches_and_is_idempotent(anchor: Anchor, tree: SharedTree, tmp_path: Path) -> None:
    root = _tree(tmp_path / "src")
    result = await publish_release(anchor, "r1", root, components={"busybox": "1.36.1"})
    assert result.uploaded == 1 and not result.skipped
    assert result.path == f"{RELEASES}/r1"
    assert f"{RELEASES}/r1/bin/busybox" in tree.files
    assert f"{RELEASES}/r1/{RELEASE_JSON}" in tree.files
    assert tree.modes[f"{RELEASES}/r1/bin/busybox"] == "0755"
    assert tree.owners[f"{RELEASES}/r1/{RELEASE_JSON}"] == "0:0"

    again = await publish_release(anchor, "r1", root, components={"busybox": "1.36.1"})
    assert again.skipped and again.uploaded == 0
    assert again.release_sha256 == result.release_sha256


@pytest.mark.asyncio
async def test_publish_refuses_changed_content_for_same_release(anchor: Anchor, tmp_path: Path) -> None:
    root = _tree(tmp_path / "src")
    await publish_release(anchor, "r1", root, components={"busybox": "1.36.1"})
    (root / "bin" / "busybox").write_bytes(b"different")
    with pytest.raises(ReleaseError, match="发布不可变"):
        await publish_release(anchor, "r1", root, components={"busybox": "1.36.1"})


@pytest.mark.asyncio
async def test_concurrent_publish_same_release_conflicts(
    anchor: Anchor, tree: SharedTree, tmp_path: Path, platform: FakePlatform
) -> None:
    """两个发布者抢先落地同一个发布号：一个成功，另一个必须报冲突且不覆盖。

    `mv -n` 在目标已存在时静默成功，所以这条完全靠 `Anchor.rename` 的"源仍在"检查挡住。
    """
    first = _tree(tmp_path / "a")
    second = _tree(tmp_path / "b")
    (second / "bin" / "busybox").write_bytes(b"other-bytes")

    await publish_release(anchor, "r1", first, components={"busybox": "1.36.1"})
    before = tree.modes[f"{RELEASES}/r1/bin/busybox"]
    with pytest.raises(ReleaseError, match=r"发布不可变|未生效|不一致"):
        await publish_release(anchor, "r1", second, components={"busybox": "1.36.1"})
    assert tree.modes[f"{RELEASES}/r1/bin/busybox"] == before
    content = platform.tree_contents[f"{RELEASES}/r1/bin/busybox"]
    assert content == b"\x00busybox-bytes"  # 第一个发布者的内容没被覆盖
    # 自己的 staging 被清掉了，不留残骸。
    assert not [d for d in tree.dirs if d.startswith(f"{STAGING}/r1.")]


@pytest.mark.asyncio
async def test_publish_discards_staging_on_failure(anchor: Anchor, tree: SharedTree, tmp_path: Path) -> None:
    root = _tree(tmp_path / "src")
    original = anchor.upload
    calls = {"n": 0}

    async def boom(key: str, data: bytes, **kwargs: object) -> None:
        calls["n"] += 1
        if calls["n"] == 1:
            raise FlotillaError("nope", stage="prepare", category=ErrorCategory.TRANSIENT, retryable=True)
        await original(key, data, **kwargs)  # type: ignore[arg-type]

    anchor.upload = boom  # type: ignore[method-assign]
    with pytest.raises(FlotillaError):
        await publish_release(anchor, "r1", root, components={})
    assert not [d for d in tree.dirs if d.startswith(f"{STAGING}/r1.")]
    assert not [f for f in tree.files if f.startswith(f"{RELEASES}/r1/")]


# ───────────────────────────── verify ─────────────────────────────


@pytest.mark.asyncio
async def test_verify_ok_then_detects_content_change(anchor: Anchor, platform: FakePlatform, tmp_path: Path) -> None:
    root = _tree(tmp_path / "src")
    result = await publish_release(anchor, "r1", root, components={})
    report = await verify_release(anchor, "r1", expected_sha256=result.release_sha256)
    assert report.ok and report.checked == 1 and report.manifest_ok

    platform.tree_contents[f"{RELEASES}/r1/bin/busybox"] = b"tampered"
    bad = await verify_release(anchor, "r1", expected_sha256=result.release_sha256)
    assert not bad.ok and bad.size_mismatch == ("bin/busybox",)


@pytest.mark.asyncio
async def test_verify_detects_digest_change_of_same_size(
    anchor: Anchor, platform: FakePlatform, tmp_path: Path
) -> None:
    root = _tree(tmp_path / "src")
    result = await publish_release(anchor, "r1", root, components={})
    same_size = bytes(len(b"\x00busybox-bytes")) or b"x"
    assert len(same_size) == len(b"\x00busybox-bytes")
    platform.tree_contents[f"{RELEASES}/r1/bin/busybox"] = b"y" * len(b"\x00busybox-bytes")
    report = await verify_release(anchor, "r1", expected_sha256=result.release_sha256)
    assert not report.ok and report.digest_mismatch == ("bin/busybox",) and not report.size_mismatch


@pytest.mark.asyncio
async def test_verify_detects_mode_change_with_identical_content(
    anchor: Anchor, tree: SharedTree, tmp_path: Path
) -> None:
    """内容一字不改、只把 busybox 从 0755 改成 0644 —— 必须报出来。"""
    root = _tree(tmp_path / "src")
    result = await publish_release(anchor, "r1", root, components={})
    tree.modes[f"{RELEASES}/r1/bin/busybox"] = "0644"
    report = await verify_release(anchor, "r1", expected_sha256=result.release_sha256)
    assert not report.ok
    assert report.mode_mismatch == ("bin/busybox",)
    assert not report.digest_mismatch and not report.size_mismatch


@pytest.mark.asyncio
async def test_verify_flags_manifest_sha_mismatch(anchor: Anchor, tmp_path: Path) -> None:
    """部署配置钉的 sha256 与实际不符时立即报出，不再逐文件校验。"""
    root = _tree(tmp_path / "src")
    await publish_release(anchor, "r1", root, components={})
    report = await verify_release(anchor, "r1", expected_sha256="0" * 64)
    assert not report.manifest_ok and not report.ok
    assert report.checked == 0 and report.actual_sha256 != report.expected_sha256


@pytest.mark.asyncio
async def test_verify_reports_missing_file(anchor: Anchor, tree: SharedTree, tmp_path: Path) -> None:
    root = _tree(tmp_path / "src")
    result = await publish_release(anchor, "r1", root, components={})
    tree.files.discard(f"{RELEASES}/r1/bin/busybox")
    report = await verify_release(anchor, "r1", expected_sha256=result.release_sha256)
    assert not report.ok and report.missing == ("bin/busybox",)


@pytest.mark.asyncio
async def test_verify_missing_manifest_raises(anchor: Anchor) -> None:
    with pytest.raises(ReleaseError, match="不存在"):
        await verify_release(anchor, "nope", expected_sha256=SHA)


# ───────────────────────────── gc ─────────────────────────────


@pytest.mark.asyncio
async def test_gc_keeps_only_listed(anchor: Anchor, tmp_path: Path) -> None:
    for name in ("r1", "r2", "r3"):
        await publish_release(anchor, name, _tree(tmp_path / name), components={})
    report = await gc_releases(anchor, keep={"r2"}, dry_run=False)
    assert report.removed == frozenset({"r1", "r3"})
    assert report.kept == frozenset({"r2"})
    left = sorted(await anchor.listdir(RELEASES, stage="run"))
    assert left == ["r2"]


@pytest.mark.asyncio
async def test_gc_dry_run_removes_nothing(anchor: Anchor, tmp_path: Path) -> None:
    await publish_release(anchor, "r1", _tree(tmp_path / "r1"), components={})
    report = await gc_releases(anchor, keep=set(), dry_run=True)
    assert report.removed == frozenset() and report.dry_run
    assert report.would_remove == frozenset({"r1"})
    assert sorted(await anchor.listdir(RELEASES, stage="run")) == ["r1"]
    actual = await gc_releases(anchor, keep=set(), dry_run=False)
    assert actual.removed == actual.would_remove == report.would_remove


@pytest.mark.asyncio
async def test_gc_never_touches_staging(anchor: Anchor, tree: SharedTree, tmp_path: Path) -> None:
    """另一个发布者正在组装的 staging 目录不能被 gc 删掉。"""
    await publish_release(anchor, "r1", _tree(tmp_path / "r1"), components={})
    tree.dirs.update({f"{STAGING}/r2.inflight", f"{STAGING}/r2.inflight/bin"})
    report = await gc_releases(anchor, keep={"r1"}, dry_run=False)
    assert f"{STAGING}/r2.inflight" in tree.dirs
    assert report.staging == ("r2.inflight",)


@pytest.mark.asyncio
async def test_gc_without_releases_prefix_is_empty(anchor: Anchor) -> None:
    report = await gc_releases(anchor, keep=set(), dry_run=False)
    assert report.kept == frozenset() and report.removed == frozenset()


@pytest.mark.asyncio
@pytest.mark.parametrize("source", ["missing", "file", "directory-link"])
async def test_invalid_release_source_fails_before_remote_changes(
    anchor: Anchor, tree: SharedTree, tmp_path: Path, source: str
) -> None:
    root = tmp_path / "src"
    if source == "file":
        root.write_bytes(b"not a directory")
    elif source == "directory-link":
        _tree(root)
        runtime = tmp_path / "runtime"
        runtime.mkdir()
        (runtime / "python").write_bytes(b"python")
        (root / "runtime").symlink_to(runtime, target_is_directory=True)
    events = list(tree.events)
    with pytest.raises(ReleaseError):
        await publish_release(anchor, "invalid", root, components={})
    assert tree.events == events
    assert not any(p.startswith(f"{RELEASES}/invalid") for p in tree.dirs)


@pytest.mark.asyncio
async def test_unreadable_directory_is_not_silently_skipped(
    anchor: Anchor, tree: SharedTree, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = _tree(tmp_path / "src")
    blocked = root / "private"
    blocked.mkdir()
    original = os.scandir

    def scan(path: Any) -> Any:
        if path == str(blocked):
            raise PermissionError(13, "Permission denied", str(blocked))
        return original(path)

    monkeypatch.setattr(os, "scandir", scan)
    events = list(tree.events)
    with pytest.raises(ReleaseError, match="Permission denied"):
        await publish_release(anchor, "invalid", root, components={})
    assert tree.events == events


@pytest.mark.asyncio
async def test_changed_source_does_not_publish_an_inconsistent_manifest(
    anchor: Anchor, tree: SharedTree, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = _tree(tmp_path / "src")
    original = anchor.mkdir

    async def mkdir(key: str, **kwargs: Any) -> None:
        await original(key, **kwargs)
        if key.startswith(STAGING):
            (root / "bin/busybox").write_bytes(b"changed")

    monkeypatch.setattr(anchor, "mkdir", mkdir)
    with pytest.raises(ReleaseError, match="内容发生变化"):
        await publish_release(anchor, "changed", root, components={})
    assert f"{RELEASES}/changed" not in tree.dirs
    assert not any(p.startswith(f"{STAGING}/changed.") for p in tree.dirs)
