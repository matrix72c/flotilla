"""Export bind sources with their original numeric metadata."""

from __future__ import annotations

import json
import os
import shutil
import stat
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path

from flotilla.compose.layout import Layout, classify
from flotilla.compose.model import Project
from flotilla.compose.paths import confined, valid_name


class ExportError(ValueError):
    """Task file export failed (a missing bind source, an unsupported file type, etc.)."""


@dataclass(frozen=True)
class Exported:
    """Export result. Paths are relative to `files/`, matching the manifest references."""

    binds: Mapping[int, str] = field(default_factory=dict)  # bind group index -> `binds/<n>`

    @property
    def empty(self) -> bool:
        return not self.binds


def export_task_files(project: Project, env_dir: Path, files_dir: Path) -> Exported:
    """Export one task's task files into `files_dir`.

    Only bind sources are exported. A named volume starts empty: flotilla does not
    copy the image content Docker would have copied into it.
    """
    layout = classify(project.services)
    for volume in layout.volumes:
        if not valid_name(volume.name):
            raise ExportError(f"unsafe volume name: {volume.name!r}")
    files_dir.mkdir(parents=True, exist_ok=True)
    identities: dict[str, tuple[int, int, int]] = {}
    result = Exported(binds=_export_binds(layout, env_dir, files_dir, identities))
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
