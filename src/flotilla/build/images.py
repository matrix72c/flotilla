"""Normalize image references and pin them to digests."""

from __future__ import annotations

import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field, replace
from typing import Any, Protocol

from flotilla.compose.model import Project, Service

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

    pullable_registries: tuple[str, ...] = ()  # registries / prefixes the platform can pull
    #: Public image substitutions, applied after parsing: original ref -> substituted ref. Points to internal
    #: copies when public registries are unreachable. Keys match exactly (with tag).
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
    ref: ImageRef


def plan_images(project: Project, settings: BuildSettings) -> list[ServiceImage]:
    """Each service's source image for a whole task (pure function, no network)."""
    return [plan_image(name, svc, settings) for name, svc in sorted(project.services.items())]


def plan_image(service: str, svc: Service, settings: BuildSettings) -> ServiceImage:
    """One service's source image. Preparation consumes published images; it never produces one."""
    if svc.image is None:
        raise ImageError(f"service {service} has no image")
    ref = ImageRef.parse(settings.replace(svc.image))
    if not settings.pullable(ref):
        raise ImageError(f"service {service}: source registry is not pullable by the platform")
    return ServiceImage(service, ref)


class Registry(Protocol):
    """Registry operations, working only via digests (never pulling layers locally)."""

    def digest(self, name: str, reference: str) -> str:
        """Manifest digest (`sha256:...`) of `name` (registry/repo) at `reference` (tag or digest)."""
        ...

    def image_config(self, pinned: str) -> Mapping[str, Any]:
        """`config` section of the image config of `pinned` (`repo@sha256:...`); reads only manifest and blob."""
        ...


def pin_images(plans: Sequence[ServiceImage], registry: Registry) -> list[ServiceImage]:
    """Resolve every source image read-only, without copying or building.

    Tags are deduplicated by full reference; explicit digests skip the tag cache. Re-passing the result never
    queries the registry again.
    """
    resolved: dict[tuple[str, str], str] = {}
    pinned: list[ServiceImage] = []
    for plan in plans:
        ref = plan.ref
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


def pinned_images(plans: Sequence[ServiceImage]) -> dict[str, str]:
    """Service -> `repo@sha256:...` for already pinned plans."""
    images: dict[str, str] = {}
    for plan in plans:
        digest = plan.ref.digest
        if digest is None:
            raise ImageError(f"service {plan.service} image was not pinned")
        images[plan.service] = plan.ref.pinned(digest)
    return images
