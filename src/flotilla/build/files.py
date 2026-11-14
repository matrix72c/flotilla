"""Export bind sources and image-volume contents with original numeric metadata."""

from __future__ import annotations

import contextlib
import hashlib
import json
import os
import shutil
import stat
import tarfile
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import Protocol

from flotilla.compose.layout import Layout, classify
from flotilla.compose.model import Project
from flotilla.compose.paths import confined, valid_name


class ExportError(ValueError):
    """Task file export failed (missing bind source, unavailable image content, etc.)."""


class ImageExport(Protocol):
    """Extract the content under a path from an image."""

    def extract(
        self, image: str, path: str, dest: Path, *, identities: dict[str, tuple[int, int, int]] | None = None
    ) -> bool:
        """Extract the content under `path` in `image` into `dest` (an existing empty directory).

        Returns whether the path has content. An empty directory may still supply its root identity in `identities`.
        """
        ...


@dataclass(frozen=True)
class Exported:
    """Export result. Paths are relative to `files/`, matching the manifest references."""

    binds: Mapping[int, str] = field(default_factory=dict)  # bind group index -> `binds/<n>`
    seeds: Mapping[str, str] = field(default_factory=dict)  # volume name -> `seeds/<volume>.tar`

    @property
    def empty(self) -> bool:
        return not (self.binds or self.seeds)


def export_task_files(
    project: Project,
    env_dir: Path,
    files_dir: Path,
    *,
    images: Mapping[str, str] | None = None,
    image_export: ImageExport | None = None,
) -> Exported:
    """Export one task's task files into `files_dir`.

    `images` / `image_export` are used only for initial volume content; if missing when seeds are needed, raise
    `ExportError`: silently emitting empty seeds would show services an empty directory, unlike Docker.
    """
    layout = classify(project.services)
    for volume in layout.volumes:
        if not valid_name(volume.name):
            raise ExportError(f"unsafe volume name: {volume.name!r}")
    files_dir.mkdir(parents=True, exist_ok=True)
    identities: dict[str, tuple[int, int, int]] = {}
    result = Exported(
        binds=_export_binds(layout, env_dir, files_dir, identities),
        seeds=_export_seeds(layout, files_dir, images, image_export),
    )
    (files_dir.parent / "file-identities.json").write_text(json.dumps(identities, sort_keys=True))
    return result


def _export_binds(
    layout: Layout, env_dir: Path, files_dir: Path, identities: dict[str, tuple[int, int, int]]
) -> dict[int, str]:
    """Stage every bind source for copying into attempt storage, keeping owner and mode."""
    out: dict[int, str] = {}
    hardlinks: dict[tuple[int, int], tuple[Path, int]] = {}

    def copy_file(source: str | Path, dest: str | Path) -> str:
        info = Path(source).stat(follow_symlinks=False)
        inode = (info.st_dev, info.st_ino)
        previous = hardlinks.get(inode)
        if previous is not None:
            if previous[1] != group.index:
                raise ExportError("hardlinks across independent bind source roots are unsupported")
            os.link(previous[0], dest)
        else:
            shutil.copy2(source, dest, follow_symlinks=False)
            hardlinks[inode] = (Path(dest), group.index)
        return str(dest)

    for group in layout.binds:
        source = confined(env_dir, group.root)
        key = f"binds/{group.index}"
        dest = confined(files_dir, key)
        dest.parent.mkdir(parents=True, exist_ok=True)
        if source.exists():
            for entry in [source, *sorted(source.rglob("*"))] if source.is_dir() else [source]:
                info = entry.lstat()
                if not (stat.S_ISDIR(info.st_mode) or stat.S_ISREG(info.st_mode)):
                    raise ExportError("task files must contain only regular files and directories")
                relative = str(entry.relative_to(source)) if entry != source else ""
                identities[key + ("/" + relative if relative else "")] = (
                    info.st_uid,
                    info.st_gid,
                    stat.S_IMODE(info.st_mode),
                )
        if not source.exists():
            raise ExportError(f"bind source does not exist: {group.root}")
        elif source.is_dir():
            shutil.copytree(source, dest, symlinks=True, dirs_exist_ok=True, copy_function=copy_file)
        else:
            copy_file(source, dest)
        out[group.index] = key
    return out


def _export_seeds(
    layout: Layout,
    files_dir: Path,
    images: Mapping[str, str] | None,
    image_export: ImageExport | None,
) -> dict[str, str]:
    """Pack copy-enabled volume contents into transient `seeds/<volume>.tar`, keeping owner and mode."""
    out: dict[str, str] = {}
    for volume in layout.volumes:
        if volume.mode != "trial" or volume.nocopy:
            continue
        key = f"seeds/{volume.name}.tar"
        tar_path = confined(files_dir, key)
        tar_path.parent.mkdir(parents=True, exist_ok=True)
        # Never derive a recursive deletion target from a task-provided name.
        with TemporaryDirectory(prefix=".seed-", dir=files_dir) as temporary:
            staged = Path(temporary)
            owners: dict[str, tuple[int, int, int]] = {}
            if not _populate(volume.references, staged, images, image_export, volume.name, owners):
                continue

            def numeric_owner(
                info: tarfile.TarInfo, owners: dict[str, tuple[int, int, int]] = owners
            ) -> tarfile.TarInfo:
                info.uid, info.gid, info.mode = owners.get(info.name, (info.uid, info.gid, info.mode))
                info.uname = info.gname = ""
                # Extraction creates fresh host timestamps, not image metadata.
                # Keep those incidental times out of content-addressed seed bytes.
                info.mtime = 0
                return info

            with tarfile.open(tar_path, "w") as tar:
                if "." in owners:
                    tar.add(staged, arcname=".", recursive=False, filter=numeric_owner)
                for child in sorted(staged.iterdir()):
                    tar.add(child, arcname=child.name, filter=numeric_owner)
        out[volume.name] = key
    return out


def _populate(
    references: tuple[tuple[str, str, bool, bool], ...],
    dest: Path,
    images: Mapping[str, str] | None,
    image_export: ImageExport | None,
    volume: str,
    identities: dict[str, tuple[int, int, int]],
) -> bool:
    """Seed from the copy-enabled references' image content, as Docker initializes an empty volume.

    Docker copies image content only into an empty volume, so references whose path is absent or
    empty never change what another reference provides. Sources are therefore extracted and
    compared: at most one distinct populated content (or, with none, one root identity) is
    unambiguous. Differing contents or root identities are rejected.
    """
    sources: dict[tuple[str, str], tuple[str, str]] = {}
    for unit, mount, _, nocopy in references:
        if nocopy:
            continue
        if image_export is None or images is None or unit not in images:
            raise ExportError(
                f"initial content of volume {volume!r} comes from the image of service {unit!r}, "
                "but no image or exporter was provided"
            )
        image = images[unit]
        # Production images are digest-pinned. Registry aliases of one digest
        # have identical content; unpinned lower-layer fixtures compare literally.
        content = image.rpartition("@")[2] or image
        sources.setdefault((content, mount), (image, mount))
    identities.clear()
    if not sources:
        return False
    assert image_export is not None
    # Candidate extractions live beside `dest` and are removed with it, never inside published files.
    with TemporaryDirectory(prefix=".sources-", dir=dest.parent) as temporary:
        populated: dict[str, tuple[Path, dict[str, tuple[int, int, int]]]] = {}
        roots: set[tuple[int, int, int]] = set()
        for index, (image, mount) in enumerate(sources.values()):
            staged = Path(temporary) / str(index)
            staged.mkdir()
            owners: dict[str, tuple[int, int, int]] = {}
            if image_export.extract(image, mount, staged, identities=owners):
                populated.setdefault(_fingerprint(staged, owners), (staged, owners))
            elif "." in owners:
                roots.add(owners["."])
        if len(populated) > 1:
            raise ExportError(
                f"volume {volume!r} has copy-enabled sources with different initial content "
                "(use one source or volume.nocopy)"
            )
        _reset(dest)
        if populated:
            staged, owners = next(iter(populated.values()))
            for child in staged.iterdir():  # rename keeps hardlinks within the extracted tree
                child.rename(dest / child.name)
            identities.update(owners)
            return True
    if len(roots) > 1:
        raise ExportError(f"volume {volume!r} has copy-enabled sources with different empty root identities")
    if roots:
        identities["."] = next(iter(roots))
        return True
    return False


def _fingerprint(root: Path, owners: Mapping[str, tuple[int, int, int]]) -> str:
    """Content, numeric metadata and link targets of one extraction, without following symlinks."""
    digest = hashlib.sha256()
    entries: list[tuple[str, str, bytes]] = []
    for current, directories, files in os.walk(root, followlinks=False):
        directories.sort()
        for name in sorted(directories + files):
            path = Path(current) / name
            relative = path.relative_to(root).as_posix()
            if path.is_symlink():
                entries.append((relative, "link", os.readlink(path).encode()))
            elif path.is_dir():
                entries.append((relative, "dir", b""))
            else:
                entries.append((relative, "file", path.read_bytes()))
    for relative, kind, payload in sorted(entries):
        digest.update(f"{relative}\0{kind}\0{owners.get(relative)}\0".encode())
        digest.update(payload)
    digest.update(repr(owners.get(".")).encode())
    return digest.hexdigest()


def _reset(path: Path) -> None:
    with contextlib.suppress(FileNotFoundError):
        shutil.rmtree(path)
    path.mkdir(parents=True, exist_ok=True)
