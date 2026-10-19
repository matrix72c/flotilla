"""Export bind sources and image-volume contents with original numeric metadata."""

from __future__ import annotations

import contextlib
import json
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

        Returns whether the path has content: False if absent or an empty directory, so the caller emits no seed.
        """
        ...


@dataclass(frozen=True)
class Exported:
    """Export result. Paths are relative to `files/`, matching the manifest references."""

    binds: Mapping[int, str] = field(default_factory=dict)  # bind group index -> `binds/<n>`
    seeds: Mapping[str, str] = field(default_factory=dict)  # volume name -> `seeds/<volume>.tar`
    readonly_volumes: Mapping[str, str] = field(default_factory=dict)  # volume name -> `_volumes/<volume>`

    @property
    def empty(self) -> bool:
        return not (self.binds or self.seeds or self.readonly_volumes)


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
        readonly_volumes=_export_readonly(layout, files_dir, images, image_export, identities),
    )
    (files_dir.parent / "file-identities.json").write_text(json.dumps(identities, sort_keys=True))
    return result


def _export_binds(
    layout: Layout, env_dir: Path, files_dir: Path, identities: dict[str, tuple[int, int, int]]
) -> dict[int, str]:
    """Copy platform-mounted bind source groups (`task_ro` / `trial`) to `binds/<n>`, keeping owner and mode."""
    out: dict[int, str] = {}
    for group in layout.binds:
        if group.mode == "image":  # baked into the image, not task files
            continue
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
            # As in Docker, a missing source mounts as an empty dir (scan rejects create_host_path=false).
            dest.mkdir(parents=True, exist_ok=True)
        elif source.is_dir():
            shutil.copytree(source, dest, symlinks=True, dirs_exist_ok=True)
        else:
            shutil.copy2(source, dest, follow_symlinks=False)
        out[group.index] = key
    return out


def _export_seeds(
    layout: Layout,
    files_dir: Path,
    images: Mapping[str, str] | None,
    image_export: ImageExport | None,
) -> dict[str, str]:
    """Pack initial content of multi-reference read-write volumes into `seeds/<volume>.tar`, keeping owner and mode."""
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
                return info

            with tarfile.open(tar_path, "w") as tar:
                for child in sorted(staged.iterdir()):
                    tar.add(child, arcname=child.name, filter=numeric_owner)
        out[volume.name] = key
    return out


def _export_readonly(
    layout: Layout,
    files_dir: Path,
    images: Mapping[str, str] | None,
    image_export: ImageExport | None,
    identities: dict[str, tuple[int, int, int]],
) -> dict[str, str]:
    """Volumes with only read-only references: content is unpacked to `_volumes/<volume>/`, shared by all trials."""
    out: dict[str, str] = {}
    for volume in layout.volumes:
        if volume.mode != "task_ro":
            continue
        key = f"_volumes/{volume.name}"
        dest = confined(files_dir, key)
        _reset(dest)
        owners: dict[str, tuple[int, int, int]] = {}
        _populate(volume.references, dest, images, image_export, volume.name, owners)
        identities.update({key if p == "." else f"{key}/{p}": owner for p, owner in owners.items()})
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
    """Use the first populated copy-enabled reference, propagating export failures."""
    for unit, mount, _, nocopy in references:
        if nocopy:
            continue
        if image_export is None or images is None or unit not in images:
            raise ExportError(
                f"initial content of volume {volume!r} comes from the image of service {unit!r}, "
                "but no image or exporter was provided"
            )
        identities.clear()
        if image_export.extract(images[unit], mount, dest, identities=identities):
            return True
        _reset(dest)
    return False


def _reset(path: Path) -> None:
    with contextlib.suppress(FileNotFoundError):
        shutil.rmtree(path)
    path.mkdir(parents=True, exist_ok=True)
