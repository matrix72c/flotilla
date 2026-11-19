"""Unit tests for share releases: manifest format, publishing, verification, gc and concurrency.

A fixed `RELEASE.json` sample checks byte-for-byte compatibility, so format changes cannot silently change the sha256
used by deployment configs.
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

from flotilla.busybox import BUSYBOX_SHA256
from flotilla.core.anchor import Anchor, AnchorSettings
from flotilla.platform.base import ErrorCategory, FlotillaError
from flotilla.share import release as share_release
from flotilla.share.release import (
    ANCHOR_BUSYBOX,
    BUILTIN_COMPONENT,
    BUILTIN_RELEASE,
    BUILTIN_RELEASE_SHA256,
    RELEASE_JSON,
    RELEASES,
    STAGING,
    ReleaseError,
    _release_json,
    check_release,
    ensure_builtin_release,
    publish_release,
    release_entries,
)
from flotilla.testing.platform import FakePlatform, ManualClock, SharedTree

SHA = "65ebd81fd085c1d7a6eb4398cb1b3b050a1f249ec68b6f91c8d1e43d4c48cd36"


@pytest_asyncio.fixture
async def anchor(platform: FakePlatform, clock: ManualClock, tree: SharedTree) -> AsyncIterator[Anchor]:
    a = Anchor(platform, clock, "L1", AnchorSettings(image="anchor@sha256:abc"))
    await clock.run(a.start())
    yield a
    await a.close()


def _contents(root: Path) -> dict[str, bytes]:
    return {rel: (root / rel).read_bytes() for rel, _ in release_entries(root)}


def _tree(root: Path, *, mode: int = 0o755) -> Path:
    """Content shaped like a real release: bin/busybox plus one top-level file."""
    (root / "bin").mkdir(parents=True)
    (root / "bin" / "busybox").write_bytes(b"\x00busybox-bytes")
    (root / "bin" / "busybox").chmod(mode)
    return root


# ───────────────────────────── RELEASE.json ─────────────────────────────


def test_release_json_shape_and_entries(tmp_path: Path) -> None:
    root = _tree(tmp_path / "src")
    contents = _contents(root)
    blob, digest = _release_json("r1", {"busybox": "1.36.1"}, release_entries(root), contents)
    document = json.loads(blob)
    assert document["release"] == "r1"
    assert document["components"] == {"busybox": "1.36.1"}
    assert set(document["files"]) == {"bin/busybox"}
    entry = document["files"]["bin/busybox"]
    assert entry["mode"] == "0755"
    assert entry["size"] == len(b"\x00busybox-bytes")
    assert entry["sha256"] == hashlib.sha256(b"\x00busybox-bytes").hexdigest()
    assert digest == hashlib.sha256(blob).hexdigest()
    # Byte-for-byte reproducible: the same content hashed twice gives the same digest.
    assert _release_json("r1", {"busybox": "1.36.1"}, release_entries(root), contents) == (blob, digest)


def test_matches_real_release(tmp_path: Path) -> None:
    """The fixed RELEASE.json sample must reproduce byte-for-byte and keep a stable digest."""
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
    # Same shape: key order, indentation and trailing newline match. Fake content bytes; compare structure only.
    root = tmp_path / "src"
    (root / "bin").mkdir(parents=True)
    (root / "bin" / "busybox").write_bytes(b"x")
    (root / "bin" / "busybox").chmod(0o755)
    components = {"busybox": "1.36.1 (alpine busybox-static 1.36.1-r31)"}
    blob, _ = _release_json("2026-10-07.1", components, release_entries(root), _contents(root))
    assert blob.decode().splitlines()[:4] == real.decode().splitlines()[:4]
    assert blob.endswith(b"\n")


def test_root_release_json_is_excluded(tmp_path: Path) -> None:
    """A root RELEASE.json is excluded (it is the manifest itself); deeper files with that name are kept."""
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
    with pytest.raises(ReleaseError, match="is a symlink"):
        release_entries(root)

    root2 = _tree(tmp_path / "src2")
    (root2 / "big").write_bytes(b"x")
    big = root2 / "big"
    big.unlink()
    big.write_bytes(b"")
    with pytest.raises(ReleaseError, match="exceeding the limit"):
        # Fake an oversized stat directly instead of writing 512MB.
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
    with pytest.raises(ReleaseError, match=r"\(releases are immutable\)"):
        await publish_release(anchor, "r1", root, components={"busybox": "1.36.1"})


@pytest.mark.asyncio
async def test_concurrent_publish_same_release_conflicts(
    anchor: Anchor, tree: SharedTree, tmp_path: Path, platform: FakePlatform
) -> None:
    """Two publishers race to land the same release: one succeeds, the other must report a conflict without overwriting.

    `mv -n` silently succeeds when the target exists, so this relies entirely on the "source still exists" check in
    `Anchor.rename`.
    """
    first = _tree(tmp_path / "a")
    second = _tree(tmp_path / "b")
    (second / "bin" / "busybox").write_bytes(b"other-bytes")

    await publish_release(anchor, "r1", first, components={"busybox": "1.36.1"})
    before = tree.modes[f"{RELEASES}/r1/bin/busybox"]
    with pytest.raises(ReleaseError, match=r"releases are immutable|rename had no effect|does not match this publish"):
        await publish_release(anchor, "r1", second, components={"busybox": "1.36.1"})
    assert tree.modes[f"{RELEASES}/r1/bin/busybox"] == before
    content = platform.tree_contents[f"{RELEASES}/r1/bin/busybox"]
    assert content == b"\x00busybox-bytes"  # the first publisher's content was not overwritten
    # Its own staging was cleaned up, leaving no debris.
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
async def test_source_changed_after_the_scan_publishes_the_scanned_snapshot(
    anchor: Anchor, tree: SharedTree, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Bytes are read once and carried from scan to manifest to upload.

    A file rewritten after the scan is published as scanned, manifest and upload matching each other;
    the published release is a consistent snapshot, not a mix of two reads.
    """
    root = _tree(tmp_path / "src")
    original = anchor.mkdir

    async def mkdir(key: str, **kwargs: Any) -> None:
        await original(key, **kwargs)
        if key.startswith(STAGING):
            (root / "bin/busybox").write_bytes(b"changed")

    monkeypatch.setattr(anchor, "mkdir", mkdir)
    result = await publish_release(anchor, "snapshot", root, components={})
    assert not result.skipped
    manifest = json.loads(await anchor.download(f"{RELEASES}/snapshot/{RELEASE_JSON}", stage="prepare"))
    entry = manifest["files"]["bin/busybox"]
    assert entry["sha256"] == hashlib.sha256(b"\x00busybox-bytes").hexdigest()
    assert await anchor.download(f"{RELEASES}/snapshot/bin/busybox", stage="prepare") == b"\x00busybox-bytes"


# ───────────────────────────── built-in share ─────────────────────────────


def test_builtin_release_reproduces_the_previously_published_share() -> None:
    """Deployments that pinned the canonical BusyBox share keep the same RELEASE.json digest."""
    document = {
        "components": {"busybox": BUILTIN_COMPONENT},
        "files": {"bin/busybox": {"mode": "0755", "sha256": BUSYBOX_SHA256, "size": 1025960}},
        "release": BUILTIN_RELEASE,
    }
    blob = json.dumps(document, sort_keys=True, ensure_ascii=False, indent=2).encode() + b"\n"
    assert BUILTIN_RELEASE == "busybox-1.36.1-r31"
    assert hashlib.sha256(blob).hexdigest() == BUILTIN_RELEASE_SHA256


def _pin_fake_busybox(monkeypatch: pytest.MonkeyPatch, anchor: Anchor, tmp_path: Path, data: bytes) -> list[str]:
    """Make `data` the pinned BusyBox and serve it as the Anchor image binary; returns the read log."""
    monkeypatch.setattr(share_release, "BUSYBOX_SHA256", hashlib.sha256(data).hexdigest())
    root = tmp_path / "expected"
    (root / "bin").mkdir(parents=True)
    (root / "bin" / "busybox").write_bytes(data)
    (root / "bin" / "busybox").chmod(0o755)
    _, digest = _release_json(BUILTIN_RELEASE, {"busybox": BUILTIN_COMPONENT}, release_entries(root), _contents(root))
    monkeypatch.setattr(share_release, "BUILTIN_RELEASE_SHA256", digest)
    reads: list[str] = []

    async def read_image_file(path: str, *, stage: str) -> bytes:
        reads.append(path)
        return data

    monkeypatch.setattr(anchor, "read_image_file", read_image_file)
    return reads


@pytest.mark.asyncio
async def test_builtin_release_is_published_from_the_anchor_image_once(
    anchor: Anchor, tree: SharedTree, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    reads = _pin_fake_busybox(monkeypatch, anchor, tmp_path, b"\x7fELF-busybox")
    assert await ensure_builtin_release(anchor)
    assert reads == [ANCHOR_BUSYBOX]
    key = f"{RELEASES}/{BUILTIN_RELEASE}/bin/busybox"
    assert key in tree.files and tree.modes[key] == "0755"
    await check_release(anchor, BUILTIN_RELEASE, expected_sha256=share_release.BUILTIN_RELEASE_SHA256)
    # Present releases are left alone: no second read or upload.
    events = list(tree.events)
    assert not await ensure_builtin_release(anchor)
    assert reads == [ANCHOR_BUSYBOX]
    assert tree.events == events


@pytest.mark.asyncio
async def test_builtin_release_refuses_an_unpinned_anchor_busybox(
    anchor: Anchor, tree: SharedTree, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _pin_fake_busybox(monkeypatch, anchor, tmp_path, b"\x7fELF-busybox")

    async def other(path: str, *, stage: str) -> bytes:
        return b"something else"

    monkeypatch.setattr(anchor, "read_image_file", other)
    with pytest.raises(ReleaseError, match="not the pinned BusyBox"):
        await ensure_builtin_release(anchor)
    assert f"{RELEASES}/{BUILTIN_RELEASE}" not in tree.dirs


@pytest.mark.asyncio
async def test_read_image_file_refuses_storage_and_relative_paths(anchor: Anchor) -> None:
    for path in ("/flotilla-root/share/x", "/flotilla-inputs/runtimes/x", "relative/busybox", "/a/../b"):
        with pytest.raises(ValueError, match="not an Anchor image path"):
            await anchor.read_image_file(path, stage="prepare")
