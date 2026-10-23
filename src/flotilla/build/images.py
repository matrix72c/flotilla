"""Normalize image references and resolve runtime link, mirror, or build dispositions."""

from __future__ import annotations

import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field, replace
from typing import Any, Literal, Protocol

from flotilla.compose.layout import classify
from flotilla.compose.model import Project, Service

Disposition = Literal["link", "mirror", "build"]

_DIGEST = re.compile(r"sha256:[0-9a-f]{64}")


class ImageError(ValueError):
    """Invalid image reference or failed digest resolution. Recorded as `invalid` in the scan report."""


@dataclass(frozen=True)
class ImageRef:
    """An image reference. `registry` is host[:port] (Docker Hub when absent; an explicit registry is required)."""

    registry: str
    repository: str  # path without the registry, e.g. `example/tasks`
    tag: str | None
    digest: str | None

    @property
    def name(self) -> str:
        """Registry plus repository path, without tag / digest."""
        return f"{self.registry}/{self.repository}"

    def pinned(self, digest: str) -> str:
        return f"{self.name}@{digest}"

    @classmethod
    def parse(cls, ref: str) -> ImageRef:
        rest, _, digest = ref.partition("@")
        if digest and not _DIGEST.fullmatch(digest):
            raise ImageError(f"image digest is not sha256:...: {ref!r}")
        head, _, tail = rest.partition("/")
        if "/" not in rest or ("." not in head and ":" not in head and head != "localhost"):
            tail = rest if "/" in rest else "library/" + rest
            head = "docker.io"
        repo, tag = tail, None
        colon = tail.rfind(":")
        if colon != -1 and "/" not in tail[colon:]:
            repo, tag = tail[:colon], tail[colon + 1 :]
        if not repo or any(not part for part in repo.split("/")):
            raise ImageError(f"image reference lacks a repository path: {ref!r}")
        return cls(registry=head, repository=repo, tag=tag or None, digest=digest or None)


@dataclass(frozen=True)
class BuildSettings:
    """Deployment config `[build]`."""

    pullable_registries: tuple[str, ...] = ()  # registries / prefixes the platform can pull; image services link
    mirror_images: bool = False  # if true, image services always mirror (content pinned into the user's registry)
    target: str = ""  # push target repo for mirror / build, like `registry/namespace/repo`
    #: Public image substitutions (applied after parsing, before building): original ref -> substituted ref. Both
    #: service `image` and Dockerfile `FROM` are substituted. Points to internal copies when public registries are
    #: unreachable; a substituted ref under a pullable prefix makes the service link. Keys match exactly (with tag).
    image_replacements: Mapping[str, str] = field(default_factory=dict)

    def __post_init__(self) -> None:
        for prefix in self.pullable_registries:
            if not prefix or prefix.endswith("/"):
                raise ValueError(f"pullable_registries prefixes must be non-empty and not end with /: {prefix!r}")
        for source, target in self.image_replacements.items():
            if not source or not target:
                raise ValueError(
                    f"image_replacements entries must be non-empty on both sides: {source!r} -> {target!r}"
                )

    def replace(self, ref: str) -> str:
        """Map an image reference through the substitution table; unchanged if absent."""
        return self.image_replacements.get(ref, ref)

    def pullable(self, ref: ImageRef) -> bool:
        """Whether `ref` is under a pullable prefix (matched on registry/path segment boundaries, not substrings)."""
        return any(ref.name == p or ref.name.startswith(p + "/") for p in self.pullable_registries)


@dataclass(frozen=True)
class ServiceImage:
    service: str
    disposition: Disposition
    ref: ImageRef | None  # None: build from Dockerfile; build + ref: derive from that base image


def image_changes(project: Project) -> frozenset[str]:
    """Services whose image must change at build time (excluding a missing /bin/sh, checked by the build step):

    - the service whose bind source group is baked into the image target (`mode="image"`, single writer/reference);
    - services referencing a `nocopy` local volume (image mount point is emptied).
    """
    layout = classify(project.services)
    changed: set[str] = {name for name, svc in project.services.items() if svc.tmpfs or svc.readonly_rootfs}
    for g in layout.binds:
        if g.mode == "image":
            changed.update(unit for unit, _, _, _ in g.references)
    for v in layout.volumes:
        if v.mode == "local" and v.nocopy:
            changed.update(unit for unit, _, _, _ in v.references)
    return frozenset(changed)


def plan_images(project: Project, settings: BuildSettings) -> list[ServiceImage]:
    """Decide image dispositions per service for a whole task (pure function)."""
    changed = image_changes(project)
    return [
        plan_image(name, svc, settings, needs_change=name in changed) for name, svc in sorted(project.services.items())
    ]


def plan_image(service: str, svc: Service, settings: BuildSettings, *, needs_change: bool) -> ServiceImage:
    """Decide one service's image disposition (pure function, no network).

    `needs_change`: the image must be modified (bind baked in, `nocopy` emptied); such services must build/derive.
    """
    if svc.build is not None:
        return ServiceImage(service, "build", None)
    if svc.image is None:
        raise ImageError(f"service {service} has neither image nor build")
    ref = ImageRef.parse(settings.replace(svc.image))
    if needs_change:
        return ServiceImage(service, "build", ref)
    if settings.mirror_images or not settings.pullable(ref):
        return ServiceImage(service, "mirror", ref)
    return ServiceImage(service, "link", ref)


class Registry(Protocol):
    """Registry operations, working only via digests (never pulling layers locally)."""

    def digest(self, name: str, reference: str) -> str:
        """Manifest digest (`sha256:...`) of `name` (registry/repo) at `reference` (tag or digest)."""
        ...

    def image_config(self, pinned: str) -> Mapping[str, Any]:
        """`config` section of the image config of `pinned` (`repo@sha256:...`); reads only manifest and blob."""
        ...

    def copy(self, source: str, target: str) -> None:
        """Copy `source` (`repo@sha256:...`) by digest to `target` (`repo:tag`), preserving the digest."""
        ...


@dataclass(frozen=True)
class ImageResolution:
    images: Mapping[str, str]  # service -> `repo@sha256:...` (link / mirror services only)
    dispositions: Mapping[str, Disposition]
    to_build: tuple[str, ...]  # services needing BuildKit build/derive (no images produced here)


def pin_images(plans: Sequence[ServiceImage], registry: Registry) -> list[ServiceImage]:
    """Resolve all source images read-only (including derive bases), without copying or building, for cache keys.

    Tags are deduplicated by full reference; explicit digests skip the tag cache. Re-passing the result never
    queries the registry again.
    """
    resolved: dict[tuple[str, str], str] = {}
    pinned: list[ServiceImage] = []
    for plan in plans:
        ref = plan.ref
        if ref is None:
            pinned.append(plan)
            continue
        digest = ref.digest
        if digest is None:
            key = (ref.name, ref.tag or "latest")
            digest = resolved.get(key)
            if digest is None:
                digest = registry.digest(*key)
                resolved[key] = digest
        if not _DIGEST.fullmatch(digest):
            raise ImageError(f"service {plan.service} resolved to a malformed image digest: {digest!r}")
        pinned.append(replace(plan, ref=replace(ref, tag=None, digest=digest)))
    return pinned


def resolve_images(
    plans: Sequence[ServiceImage],
    settings: BuildSettings,
    registry: Registry,
) -> ImageResolution:
    """Resolve digests for link / mirror services and copy (for mirror); return each service's `repo@sha256:...`.

    A source image (same registry/repo + digest) is resolved once and mirrored once per build. Build services
    produce no image here; they are only listed in `to_build` for the BuildKit step.
    """
    images: dict[str, str] = {}
    dispositions: dict[str, Disposition] = {}
    to_build: list[str] = []
    mirrored: dict[str, str] = {}  # source pinned -> target pinned (dedup)
    for plan in pin_images(plans, registry):
        dispositions[plan.service] = plan.disposition
        if plan.disposition == "build":
            to_build.append(plan.service)
            continue
        assert plan.ref is not None
        ref = plan.ref
        digest = ref.digest
        assert digest is not None
        source = ref.pinned(digest)
        if plan.disposition == "link":
            images[plan.service] = source
            continue
        if not settings.target:
            raise ImageError(f"service {plan.service} needs a mirror, but [build].target is not configured")
        pinned = mirrored.get(source)
        if pinned is None:
            target_tag = f"{settings.target}:mirror-{digest.removeprefix('sha256:')[:12]}"
            registry.copy(source, target_tag)
            pinned = f"{settings.target}@{digest}"
            mirrored[source] = pinned
        images[plan.service] = pinned
    return ImageResolution(images=images, dispositions=dispositions, to_build=tuple(to_build))
