"""镜像处置：link / mirror / build（Architecture §4.2 第 3 步、§4.3）。

每个服务的镜像走三条路之一，构建时逐个判定：

- **link**：`image` 服务，镜像落在部署能直接拉的 registry 前缀下，且不需要改镜像——解析 digest 后直接引用源
  registry，不拉不推；
- **mirror**：`image` 服务，镜像不在可拉前缀下（公共镜像等），或配置要求固化——按 digest 复制到使用方的 registry；
- **build**：有 `build`，或需要改镜像（§4.2 第 7 步）——由 BuildKit 构建（本模块不含，见 §4.2 第 3 步 build/derive）。

分类是纯函数（`plan_image`，不触网）；解析 digest 与复制经 `Registry`（`resolve_images`）。tag → digest 只查 registry 的
manifest，不下载镜像层。`Registry` 的真实实现见 `docker.py`，测试注入内存实现。
"""

from __future__ import annotations

import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any, Literal, Protocol

from flotilla.compose.layout import classify
from flotilla.compose.model import Project, Service

Disposition = Literal["link", "mirror", "build"]

_DIGEST = re.compile(r"sha256:[0-9a-f]{64}")


class ImageError(ValueError):
    """镜像引用不合法、或 digest 解析失败。构建时以 `invalid` 记入扫描报告。"""


@dataclass(frozen=True)
class ImageRef:
    """一个镜像引用。`registry` 是 host[:port]（无则为 Docker Hub，这里一律要求显式 registry）。"""

    registry: str
    repository: str  # 不含 registry 的路径，例如 `ailab/cvebench2tb`
    tag: str | None
    digest: str | None

    @property
    def name(self) -> str:
        """registry + 仓库路径，不含 tag / digest。"""
        return f"{self.registry}/{self.repository}"

    def pinned(self, digest: str) -> str:
        return f"{self.name}@{digest}"

    @classmethod
    def parse(cls, ref: str) -> ImageRef:
        rest, _, digest = ref.partition("@")
        if digest and not _DIGEST.fullmatch(digest):
            raise ImageError(f"镜像 digest 不是 sha256:…：{ref!r}")
        head, _, tail = rest.partition("/")
        if "." not in head and ":" not in head and head != "localhost":
            raise ImageError(f"镜像引用需带显式 registry（host/…）：{ref!r}")
        repo, tag = tail, None
        colon = tail.rfind(":")
        if colon != -1 and "/" not in tail[colon:]:
            repo, tag = tail[:colon], tail[colon + 1 :]
        if not repo:
            raise ImageError(f"镜像引用缺少仓库路径：{ref!r}")
        return cls(registry=head, repository=repo, tag=tag or None, digest=digest or None)


@dataclass(frozen=True)
class BuildSettings:
    """部署配置 `[build]`（§4.3）。"""

    pullable_registries: tuple[str, ...] = ()  # 平台能直接拉的 registry / 前缀；落在其下的 image 服务默认 link
    mirror_images: bool = False  # true 时 image 服务一律 mirror（把内容固化进使用方的 registry）
    target: str = ""  # mirror / build 的推送目标仓库，形如 `registry/namespace/repo`

    def __post_init__(self) -> None:
        for prefix in self.pullable_registries:
            if not prefix or prefix.endswith("/"):
                raise ValueError(f"pullable_registries 的前缀不能为空或以 / 结尾：{prefix!r}")

    def pullable(self, ref: ImageRef) -> bool:
        """`ref` 是否落在某个可拉前缀下（按 registry/路径的段边界匹配，不做子串匹配）。"""
        return any(ref.name == p or ref.name.startswith(p + "/") for p in self.pullable_registries)


@dataclass(frozen=True)
class ServiceImage:
    service: str
    disposition: Disposition
    ref: ImageRef | None  # build 服务为 None（镜像由 BuildKit 产出，这里还没有引用）


def image_changes(project: Project) -> frozenset[str]:
    """需要在构建时改镜像的服务（§4.2 第 7 步，不含"镜像缺 /bin/sh"——那由 build 步骤按部署能力补判）：

    - bind 源组写入镜像目标路径（§4.4 `mode="image"`，单写者单引用）的那个服务；
    - `nocopy` 的本地卷（§7.3 清空镜像挂载点）的引用服务。
    """
    layout = classify(project.services)
    changed: set[str] = set()
    for g in layout.binds:
        if g.mode == "image":
            changed.update(unit for unit, _, _, _ in g.references)
    for v in layout.volumes:
        if v.mode == "local" and v.nocopy:
            changed.update(unit for unit, _, _ in v.references)
    return frozenset(changed)


def plan_images(project: Project, settings: BuildSettings) -> list[ServiceImage]:
    """整个任务按服务判定镜像处置（纯函数）。"""
    changed = image_changes(project)
    return [
        plan_image(name, svc, settings, needs_change=name in changed) for name, svc in sorted(project.services.items())
    ]


def plan_image(service: str, svc: Service, settings: BuildSettings, *, needs_change: bool) -> ServiceImage:
    """判定一个服务的镜像处置（纯函数，不触网）。

    `needs_change`：§4.2 第 7 步，镜像需要被改动（bind 写入、`nocopy` 清空、补 `/bin/sh`）——这类必须 build/derive。
    """
    if svc.build is not None or needs_change:
        return ServiceImage(service, "build", None)
    if svc.image is None:
        raise ImageError(f"服务 {service} 既没有 image 也没有 build")
    ref = ImageRef.parse(svc.image)
    if settings.mirror_images or not settings.pullable(ref):
        return ServiceImage(service, "mirror", ref)
    return ServiceImage(service, "link", ref)


class Registry(Protocol):
    """registry 操作，只经 digest 工作（不把镜像层拉到本地）。"""

    def digest(self, name: str, reference: str) -> str:
        """`name`（registry/repo）在 `reference`（tag 或 digest）处的 manifest digest（`sha256:…`）。"""
        ...

    def image_config(self, pinned: str) -> Mapping[str, Any]:
        """`pinned`（`repo@sha256:…`）的 image config 的 `config` 段（§4.2 第 5 步），只读 manifest 与 config blob。"""
        ...

    def copy(self, source: str, target: str) -> None:
        """把 `source`（`repo@sha256:…`）按 digest 复制到 `target`（`repo:tag`），保持 digest 不变。"""
        ...


@dataclass(frozen=True)
class ImageResolution:
    images: Mapping[str, str]  # 服务 → `repo@sha256:…`（只含 link / mirror 的服务）
    dispositions: Mapping[str, Disposition]
    to_build: tuple[str, ...]  # 需要 BuildKit 的服务（§4.2 第 3 步 build/derive；本模块不产出镜像）


def resolve_images(
    plans: Sequence[ServiceImage],
    settings: BuildSettings,
    registry: Registry,
) -> ImageResolution:
    """对 link / mirror 的服务解析 digest 并（mirror 时）复制；返回每个服务钉住的 `repo@sha256:…`。

    同一个源镜像（同 registry/repo + 同 digest）在一次构建里只解析一次、mirror 只复制一次。build 的服务不在
    这里产出镜像，只登记到 `to_build`，由 BuildKit 步骤处理（§4.2 第 3 步）。
    """
    images: dict[str, str] = {}
    dispositions: dict[str, Disposition] = {}
    to_build: list[str] = []
    resolved: dict[str, str] = {}  # 源 name → digest（去重）
    mirrored: dict[str, str] = {}  # 源 pinned → 目标 pinned（去重）
    for plan in plans:
        dispositions[plan.service] = plan.disposition
        if plan.disposition == "build":
            to_build.append(plan.service)
            continue
        assert plan.ref is not None
        ref = plan.ref
        digest = ref.digest or resolved.get(ref.name) or registry.digest(ref.name, ref.tag or "latest")
        if not _DIGEST.fullmatch(digest):
            raise ImageError(f"服务 {plan.service} 的镜像 digest 解析异常：{digest!r}")
        resolved[ref.name] = digest
        source = ref.pinned(digest)
        if plan.disposition == "link":
            images[plan.service] = source
            continue
        if not settings.target:
            raise ImageError(f"服务 {plan.service} 需要 mirror，但 [build].target 未配置")
        pinned = mirrored.get(source)
        if pinned is None:
            target_tag = f"{settings.target}:mirror-{digest.removeprefix('sha256:')[:12]}"
            registry.copy(source, target_tag)
            pinned = f"{settings.target}@{digest}"
            mirrored[source] = pinned
        images[plan.service] = pinned
    return ImageResolution(images=images, dispositions=dispositions, to_build=tuple(to_build))
