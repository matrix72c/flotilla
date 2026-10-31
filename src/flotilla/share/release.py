"""Publish, verify, and explicitly collect immutable runtime-tool releases."""

from __future__ import annotations

import contextlib
import hashlib
import json
import os
import posixpath
import stat
import uuid
from collections.abc import Mapping, Sequence, Set
from dataclasses import dataclass
from pathlib import Path

from flotilla._io import owned_io
from flotilla.core.anchor import Anchor

SHARE = "share"
RELEASES = f"{SHARE}/releases"
STAGING = f"{SHARE}/staging"
RELEASE_JSON = "RELEASE.json"
#: Per-file limit: files go one by one over exec, so an oversized file is a build problem, not uploaded.
MAX_FILE_BYTES = 512 * 1024 * 1024


class ReleaseError(ValueError):
    """Release failed (symlink or oversized file, release exists with different content, rename not applied, etc.)."""


@dataclass(frozen=True)
class ReleaseResult:
    release: str
    path: str  # `share/releases/<release>`
    release_sha256: str  # sha256 of `RELEASE.json` itself, the value pinned in the deployment config
    uploaded: int  # files uploaded this time; 0 when skipped
    skipped: bool


@dataclass(frozen=True)
class VerifyReport:
    """Result of a full verification (full sha256 checks run on demand via `flotilla share verify`)."""

    release: str
    #: Whether the sha256 of `RELEASE.json` matches the pinned value. If false, other checks are meaningless.
    manifest_ok: bool
    actual_sha256: str
    expected_sha256: str
    checked: int
    missing: tuple[str, ...] = ()
    size_mismatch: tuple[str, ...] = ()
    digest_mismatch: tuple[str, ...] = ()
    mode_mismatch: tuple[str, ...] = ()

    @property
    def ok(self) -> bool:
        broken = self.missing or self.size_mismatch or self.digest_mismatch or self.mode_mismatch
        return self.manifest_ok and not broken


@dataclass(frozen=True)
class GcReleasesReport:
    """Result of `share gc`. `staging` is listed, not deleted (see `gc_releases`)."""

    kept: frozenset[str]
    removed: frozenset[str]
    would_remove: frozenset[str]
    staging: tuple[str, ...] = ()
    dry_run: bool = False


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


def release_json(release: str, root: Path, components: Mapping[str, str]) -> tuple[bytes, str]:
    """Content of `RELEASE.json` and its own sha256 (the latter is pinned in the deployment config).

    Serialization fixes key order, indentation and trailing newline so identical content yields the same digest.
    """
    return _release_json(release, root, components, release_entries(root))


def _release_json(
    release: str, root: Path, components: Mapping[str, str], entries: Sequence[tuple[str, os.stat_result]]
) -> tuple[bytes, str]:
    files: dict[str, object] = {}
    for rel, info in entries:
        files[rel] = {
            "mode": f"{stat.S_IMODE(info.st_mode):04o}",
            "sha256": _sha256(root / rel),
            "size": info.st_size,
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
    blob, digest = await owned_io(_release_json, release, root, components, entries)
    target = f"{RELEASES}/{release}"
    if await anchor.exists(f"{target}/{RELEASE_JSON}", stage="prepare"):
        if hashlib.sha256(await anchor.download(f"{target}/{RELEASE_JSON}", stage="prepare")).hexdigest() == digest:
            return ReleaseResult(release, target, digest, 0, True)
        raise ReleaseError(
            f"{target} is already published with a different {RELEASE_JSON} (releases are immutable); use a new name"
        )
    staging = f"{STAGING}/{release}.{uuid.uuid4().hex[:12]}"
    await anchor.mkdir(staging, stage="prepare")
    try:
        uploaded = await _upload_tree(anchor, root, staging, entries, json.loads(blob)["files"])
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
    root: Path,
    staging: str,
    entries: Sequence[tuple[str, os.stat_result]],
    manifest_files: Mapping[str, Mapping[str, object]],
) -> int:
    """Upload the tree: create directories first, then files. `upload` requires an existing parent."""
    uploaded = 0
    created: set[str] = set()
    for rel, info in entries:
        path = root / rel
        # Manifest and upload share entries; reject local changes in between rather than publish a mismatched tree.
        if await owned_io(path.is_symlink):
            raise ReleaseError(f"release source {rel} became a symlink after scanning")
        data = await owned_io(path.read_bytes)
        if (
            len(data) != manifest_files[rel]["size"]
            or hashlib.sha256(data).hexdigest() != manifest_files[rel]["sha256"]
        ):
            raise ReleaseError(f"release source {rel} changed after the manifest was generated")
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

    One download and one existence check; per-file verification is `verify_release` (`flotilla share verify`).
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


async def verify_release(anchor: Anchor, release: str, *, expected_sha256: str) -> VerifyReport:
    """Fully verify a release: the manifest's own digest first, then each file's size, content and mode.

    If the manifest digest does not match the pinned value, the rest is meaningless and is skipped.
    """
    target = f"{RELEASES}/{release}"
    manifest_key = f"{target}/{RELEASE_JSON}"
    if not await anchor.exists(manifest_key, stage="prepare"):
        raise ReleaseError(f"{manifest_key} does not exist")
    blob = await anchor.download(manifest_key, stage="prepare")
    actual = hashlib.sha256(blob).hexdigest()
    if actual != expected_sha256:
        return VerifyReport(release, False, actual, expected_sha256, 0)
    document = json.loads(blob)
    files: dict[str, dict[str, object]] = document.get("files") or {}
    missing, size_bad, digest_bad, mode_bad = [], [], [], []
    for rel in sorted(files):
        entry = files[rel]
        key = f"{target}/{rel}"
        if not await anchor.exists(key, stage="prepare"):
            missing.append(rel)
            continue
        info = await anchor.stat(key, stage="prepare")
        if info.size != entry.get("size"):
            size_bad.append(rel)
        else:
            data = await anchor.download(key, stage="prepare")
            if hashlib.sha256(data).hexdigest() != entry.get("sha256"):
                digest_bad.append(rel)
        if f"{info.mode:04o}" != entry.get("mode"):
            mode_bad.append(rel)
    return VerifyReport(
        release,
        True,
        actual,
        expected_sha256,
        len(files),
        tuple(missing),
        tuple(size_bad),
        tuple(digest_bad),
        tuple(mode_bad),
    )


async def gc_releases(anchor: Anchor, *, keep: Set[str], dry_run: bool) -> GcReleasesReport:
    """Delete releases not in `keep`.

    `keep` must include the release pinned in the deployment config. This keeps "the configured release and
    `--keep`"; it does **not** check all running references, so a release pinned by another experiment but
    missing from `keep` will be deleted.

    **`share/staging` is not reclaimed**: it cannot be told apart from another process assembling a release
    (which may rename it into place at any time), and deleting it would break that publish, so it is only listed.
    """
    present = sorted(await anchor.listdir(RELEASES, stage="run")) if await anchor.exists(RELEASES, stage="run") else []
    removed = [name for name in present if name not in keep]
    if not dry_run:
        for name in removed:
            await anchor.remove(f"{RELEASES}/{name}", stage="run")
    staging = sorted(await anchor.listdir(STAGING, stage="run")) if await anchor.exists(STAGING, stage="run") else []
    return GcReleasesReport(
        kept=frozenset(present) - frozenset(removed),
        removed=frozenset() if dry_run else frozenset(removed),
        would_remove=frozenset(removed),
        staging=tuple(staging),
        dry_run=dry_run,
    )


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        while chunk := handle.read(1 << 20):
            digest.update(chunk)
    return digest.hexdigest()
