"""任务文件导出：bind 源与卷种子 → `<out>/<build_key>/files/`（Architecture §4.2 第 4、6 步）。

构建机离线、没有平台凭证，所以这一步**只写本地输出目录**，不碰共享存储；上传由 `flotilla publish`（§4.7）完成。

导出三类内容：

| 目录 | 内容 | 来源 |
|---|---|---|
| `binds/<n>/` | 由平台挂载的 bind 源组（§4.4 `task_ro` 与 `trial`） | 任务的 environment 目录 |
| `seeds/<volume>.tar` | 多引用读写卷的初始内容（§7.3） | 来源服务镜像中挂载点下的内容 |
| `_volumes/<volume>/` | 全部引用只读的卷的内容（§7.3） | 同上 |

`mode="image"` 的 bind 组不在这里——它写进镜像（派生层，§4.2 第 7 步）。

卷的初始内容要从镜像里取，需要能读镜像文件系统（`ImageExport`）。镜像在挂载点下没有内容时不产出种子，
与 Docker 一致（空卷就是空的）。
"""

from __future__ import annotations

import shutil
import tarfile
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Protocol

from flotilla.compose.layout import Layout, classify
from flotilla.compose.model import Project


class ExportError(ValueError):
    """任务文件导出失败（bind 源缺失、镜像内容取不到等）。"""


class ImageExport(Protocol):
    """从镜像里取出某个路径下的内容。"""

    def extract(self, image: str, path: str, dest: Path) -> bool:
        """把 `image` 中 `path` 下的内容取到 `dest`（已存在的空目录）。

        返回该路径在镜像中是否有内容：没有该路径、或它是空目录时返回 False，调用方据此不产出种子。
        """
        ...


@dataclass(frozen=True)
class Exported:
    """导出结果。路径都相对 `files/`，与清单里的引用一致。"""

    binds: Mapping[int, str] = field(default_factory=dict)  # bind 组序号 → `binds/<n>`
    seeds: Mapping[str, str] = field(default_factory=dict)  # 卷名 → `seeds/<volume>.tar`
    readonly_volumes: Mapping[str, str] = field(default_factory=dict)  # 卷名 → `_volumes/<volume>`

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
    """导出一个任务的任务文件到 `files_dir`（§4.2 第 6 步）。

    `images` / `image_export` 只在需要卷的初始内容时用到；缺少它们而任务需要种子时报 `ExportError`——
    静默产出空种子会让服务看到空目录，与 Docker 的语义不同。
    """
    layout = classify(project.services)
    files_dir.mkdir(parents=True, exist_ok=True)
    return Exported(
        binds=_export_binds(layout, env_dir, files_dir),
        seeds=_export_seeds(layout, files_dir, images, image_export),
        readonly_volumes=_export_readonly(layout, files_dir, images, image_export),
    )


def _export_binds(layout: Layout, env_dir: Path, files_dir: Path) -> dict[int, str]:
    """由平台挂载的 bind 源组（`task_ro` / `trial`）复制到 `binds/<n>`，保留属主与权限。"""
    out: dict[int, str] = {}
    for group in layout.binds:
        if group.mode == "image":  # 写进镜像，不进任务文件
            continue
        source = (env_dir / group.root).resolve()
        if not str(source).startswith(str(env_dir.resolve())):
            raise ExportError(f"bind 源越出任务目录：{group.root!r}")
        key = f"binds/{group.index}"
        dest = files_dir / key
        dest.parent.mkdir(parents=True, exist_ok=True)
        if not source.exists():
            # 与 Docker 一致：源不存在时挂载为空目录（scan 已对 create_host_path=false 的情形拒绝）。
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
    """多引用读写卷的初始内容打成 `seeds/<volume>.tar`（§7.3），保留属主与权限。"""
    out: dict[str, str] = {}
    for volume in layout.volumes:
        if volume.mode != "trial" or volume.nocopy:
            continue
        source = _seed_source(volume.references)
        if source is None:
            continue
        unit, mount = source
        staged = files_dir / "_seed-staging" / volume.name
        _reset(staged)
        if not _extract(unit, mount, staged, images, image_export, volume.name):
            shutil.rmtree(staged.parent, ignore_errors=True)
            continue  # 镜像里挂载点为空：空卷，不产出种子
        key = f"seeds/{volume.name}.tar"
        tar_path = files_dir / key
        tar_path.parent.mkdir(parents=True, exist_ok=True)
        with tarfile.open(tar_path, "w") as tar:
            for child in sorted(staged.iterdir()):
                tar.add(child, arcname=child.name)
        shutil.rmtree(staged.parent, ignore_errors=True)
        out[volume.name] = key
    return out


def _export_readonly(
    layout: Layout,
    files_dir: Path,
    images: Mapping[str, str] | None,
    image_export: ImageExport | None,
) -> dict[str, str]:
    """全部引用只读的卷：内容展开到 `_volumes/<volume>/`，所有 trial 共用（§7.3）。"""
    out: dict[str, str] = {}
    for volume in layout.volumes:
        if volume.mode != "task_ro":
            continue
        source = _seed_source(volume.references)
        if source is None:
            continue
        unit, mount = source
        key = f"_volumes/{volume.name}"
        dest = files_dir / key
        _reset(dest)
        if not _extract(unit, mount, dest, images, image_export, volume.name):
            continue  # 空目录留着：只读卷挂空目录与 Docker 一致
        out[volume.name] = key
    return out


def _seed_source(references: Iterable[tuple[str, str, bool]]) -> tuple[str, str] | None:
    """初始内容的来源：第一个引用服务与它的挂载点（§7.3 按 Docker 规则取第一个有内容的引用）。

    这里按引用顺序取第一个；"是否真有内容"由 `ImageExport.extract` 回答。
    """
    for unit, mount, _ in references:
        return unit, mount
    return None


def _extract(
    unit: str,
    mount: str,
    dest: Path,
    images: Mapping[str, str] | None,
    image_export: ImageExport | None,
    volume: str,
) -> bool:
    if image_export is None or images is None or unit not in images:
        raise ExportError(f"卷 {volume!r} 的初始内容要从服务 {unit!r} 的镜像取，但没有提供镜像或导出器")
    return image_export.extract(images[unit], mount, dest)


def _reset(path: Path) -> None:
    shutil.rmtree(path, ignore_errors=True)
    path.mkdir(parents=True, exist_ok=True)
