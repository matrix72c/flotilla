"""Publish, verify, and explicitly collect immutable runtime-tool releases."""

from __future__ import annotations

import contextlib
import hashlib
import json
import os
import posixpath
import stat
import time
import uuid
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from tempfile import TemporaryDirectory

from flotilla._io import owned_io
from flotilla.busybox import BUSYBOX_SHA256, BUSYBOX_URL
from flotilla.core.anchor import Anchor
from flotilla.platform.base import FlotillaError

SHARE = "share"
RELEASES = f"{SHARE}/releases"
STAGING = f"{SHARE}/staging"
RELEASE_JSON = "RELEASE.json"
#: Per-file limit: files go one by one over exec, so an oversized file is a build problem, not uploaded.
MAX_FILE_BYTES = 512 * 1024 * 1024

#: The built-in share: the Anchor image's own pinned BusyBox, published as `bin/busybox` (mode 0755). A deployment
#: without `[share]` uses it, and runtime/probe start publishes it into the work root when it is missing.
BUILTIN_COMPONENT = BUSYBOX_URL.rsplit("/", 1)[-1].removesuffix(".apk").removeprefix("busybox-static-")
BUILTIN_RELEASE = f"busybox-{BUILTIN_COMPONENT}"
#: sha256 of the built-in release's `RELEASE.json`; byte-identical to releases published by earlier tooling.
BUILTIN_RELEASE_SHA256 = "9b571fcfe2ac28acf951f082785c2718b4d6bff3c2400728de7841dce4d5ad9b"
#: Where the Anchor image ships the BusyBox binary (see `anchor_assets/Dockerfile`).
ANCHOR_BUSYBOX = "/.flotilla/bin/busybox"


class ReleaseError(ValueError):
    """Release failed (symlink or oversized file, release exists with different content, rename not applied, etc.)."""


@dataclass(frozen=True)
class ReleaseResult:
    release: str
    path: str  # `share/releases/<release>`
    release_sha256: str  # sha256 of `RELEASE.json` itself, the value pinned in the deployment config
    uploaded: int  # files uploaded this time; 0 when skipped
    skipped: bool


def release_entries(root: Path) -> list[tuple[str, os.stat_result]]:
    """Release file entries sorted by path, using the same rules as `RELEASE.json`.

    Only regular files: the manifest has no directory entries; directories are created on upload with default
    permissions. The root `RELEASE.json` is the manifest itself and regenerated, so it is excluded; deeper files
    with that name are normal content.

    Symlinks are rejected, not skipped: the anchor image has no `ln`, and skipping would diverge from the source.
    """
    if not root.is_dir():
        raise ReleaseError(f"release source is not an existing directory: {root}")

    def fail(exc: OSError) -> None:
        raise ReleaseError(f"cannot read release directory {exc.filename}: {exc.strerror}") from exc

    out: list[tuple[str, os.stat_result]] = []
    for current, dirs, names in os.walk(root, onerror=fail):
        dirs.sort()
        base = Path(current)
        for name in sorted([*dirs, *names]):
            path = base / name
            info = path.lstat()
            rel = str(path.relative_to(root))
            if stat.S_ISLNK(info.st_mode):
                raise ReleaseError(f"{rel} is a symlink, which share releases do not support (anchor image has no ln)")
            if stat.S_ISDIR(info.st_mode):
                continue
            if not stat.S_ISREG(info.st_mode):
                raise ReleaseError(f"{rel} is not a regular file, which share releases do not support")
            if rel == RELEASE_JSON:
                continue
            if info.st_size > MAX_FILE_BYTES:
                raise ReleaseError(f"{rel} is {info.st_size} bytes, exceeding the limit {MAX_FILE_BYTES}")
            out.append((rel, info))
    return sorted(out, key=lambda item: item[0])


def _release_json(
    release: str,
    components: Mapping[str, str],
    entries: Sequence[tuple[str, os.stat_result]],
    contents: Mapping[str, bytes],
) -> tuple[bytes, str]:
    """Content of `RELEASE.json` and its own sha256 (the latter is pinned in the deployment config).

    Serialization fixes key order, indentation and trailing newline so identical content yields the same digest.
    """
    files: dict[str, object] = {}
    for rel, info in entries:
        files[rel] = {
            "mode": f"{stat.S_IMODE(info.st_mode):04o}",
            "sha256": hashlib.sha256(contents[rel]).hexdigest(),
            "size": len(contents[rel]),
        }
    document = {"components": dict(components), "files": files, "release": release}
    blob = json.dumps(document, sort_keys=True, ensure_ascii=False, indent=2).encode() + b"\n"
    return blob, hashlib.sha256(blob).hexdigest()


async def publish_release(
    anchor: Anchor,
    release: str,
    root: Path,
    *,
    components: Mapping[str, str],
) -> ReleaseResult:
    """Publish `root` as `share/releases/<release>`.

    Releases are immutable: skip if the target exists with a matching `RELEASE.json`; otherwise raise without
    overwriting. **No hard-link reuse**: unchanged components are re-uploaded (busybox `cp -l` would work without
    an external `ln`, but real releases have few files, so reuse gains nothing).
    """
    entries = await owned_io(release_entries, root)
    contents = {rel: await owned_io((root / rel).read_bytes) for rel, _ in entries}
    blob, digest = await owned_io(_release_json, release, components, entries, contents)
    target = f"{RELEASES}/{release}"
    if await anchor.exists(f"{target}/{RELEASE_JSON}", stage="prepare"):
        if hashlib.sha256(await anchor.download(f"{target}/{RELEASE_JSON}", stage="prepare")).hexdigest() == digest:
            return ReleaseResult(release, target, digest, 0, True)
        raise ReleaseError(
            f"{target} is already published with a different {RELEASE_JSON} (releases are immutable); use a new name"
        )
    # Timestamped so `flotilla gc --staging-age` can remove abandoned attempts.
    staging = f"{STAGING}/{release}.{int(time.time())}.{uuid.uuid4().hex[:12]}"
    await anchor.mkdir(staging, stage="prepare")
    try:
        uploaded = await _upload_tree(anchor, staging, entries, contents)
        await anchor.upload(f"{staging}/{RELEASE_JSON}", blob, mode=0o644, uid=0, gid=0, stage="prepare")
        await anchor.mkdir(RELEASES, stage="prepare")
        await anchor.rename(staging, target, stage="prepare")
    except BaseException:
        await _discard(anchor, staging)
        raise
    # Verify the immutable manifest at its final location.
    landed = hashlib.sha256(await anchor.download(f"{target}/{RELEASE_JSON}", stage="prepare")).hexdigest()
    if landed != digest:
        raise ReleaseError(f"{RELEASE_JSON} in {target} does not match this publish (concurrent publish?)")
    return ReleaseResult(release, target, digest, uploaded, False)


async def _upload_tree(
    anchor: Anchor,
    staging: str,
    entries: Sequence[tuple[str, os.stat_result]],
    contents: Mapping[str, bytes],
) -> int:
    """Upload the tree: create directories first, then files. `upload` requires an existing parent."""
    uploaded = 0
    created: set[str] = set()
    for rel, info in entries:
        data = contents[rel]
        parent = posixpath.dirname(rel)
        if parent and parent not in created:
            await anchor.mkdir(f"{staging}/{parent}", stage="prepare")
            created.add(parent)
        key = f"{staging}/{rel}"
        await anchor.upload(
            key,
            data,
            mode=stat.S_IMODE(info.st_mode),
            uid=info.st_uid,
            gid=info.st_gid,
            stage="prepare",
        )
        uploaded += 1
    return uploaded


async def _discard(anchor: Anchor, staging: str) -> None:
    """Remove staging; a failure does not mask the original error (leftovers are listed by `share gc`)."""
    with contextlib.suppress(Exception):
        await anchor.remove(staging, stage="run")


async def check_release(anchor: Anchor, release: str, *, expected_sha256: str) -> None:
    """Startup check: the pinned RELEASE.json digest matches and `bin/busybox` exists. Raises `ReleaseError`.

    One download and one existence check; a caller needing per-file verification loops over `RELEASE.json` itself.
    """
    target = f"{RELEASES}/{release}"
    manifest_key = f"{target}/{RELEASE_JSON}"
    if not await anchor.exists(manifest_key, stage="prepare"):
        raise ReleaseError(f"{manifest_key} does not exist")
    actual = hashlib.sha256(await anchor.download(manifest_key, stage="prepare")).hexdigest()
    if actual != expected_sha256:
        raise ReleaseError(f"{manifest_key} sha256 is {actual}, but the config pins {expected_sha256}")
    if not await anchor.exists(f"{target}/bin/busybox", stage="prepare"):
        raise ReleaseError(f"share release {release} has no bin/busybox")


async def ensure_builtin_release(anchor: Anchor) -> bool:
    """Publish the built-in share from the Anchor image's BusyBox when the work root lacks it.

    Needs no network: the binary comes from the running Anchor and must be the pinned build. Returns whether this
    call published it; a concurrent publisher of the same content is fine. Callers still run `check_release`.
    """
    if await anchor.exists(f"{RELEASES}/{BUILTIN_RELEASE}/{RELEASE_JSON}", stage="prepare"):
        return False
    data = await anchor.read_image_file(ANCHOR_BUSYBOX, stage="prepare")
    if hashlib.sha256(data).hexdigest() != BUSYBOX_SHA256:
        raise ReleaseError(
            f"Anchor image {ANCHOR_BUSYBOX} is not the pinned BusyBox; publish a share and set [share] explicitly"
        )

    def stage_bundle(directory: str) -> Path:
        root = Path(directory) / "bundle"
        (root / "bin").mkdir(parents=True)
        binary = root / "bin" / "busybox"
        binary.write_bytes(data)
        binary.chmod(0o755)
        return root

    with TemporaryDirectory(prefix="flotilla-share-") as directory:
        root = await owned_io(stage_bundle, directory)
        try:
            result = await publish_release(anchor, BUILTIN_RELEASE, root, components={"busybox": BUILTIN_COMPONENT})
        except (ReleaseError, FlotillaError):
            # Lost a race to another launch: accept its release only if it is the built-in one.
            if not await anchor.exists(f"{RELEASES}/{BUILTIN_RELEASE}/{RELEASE_JSON}", stage="prepare"):
                raise
            await check_release(anchor, BUILTIN_RELEASE, expected_sha256=BUILTIN_RELEASE_SHA256)
            return False
    if result.release_sha256 != BUILTIN_RELEASE_SHA256:
        raise ReleaseError(f"built-in share published with unexpected {RELEASE_JSON} sha256 {result.release_sha256}")
    return not result.skipped
