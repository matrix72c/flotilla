"""Prepare one task into a validated manifest and local publication files."""

from __future__ import annotations

import hashlib
import uuid
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from flotilla.build.builder import (
    Builder,
    BuildRequest,
    base_images,
    build_contexts,
    image_tag,
)
from flotilla.build.cache import build_key, cache_tag
from flotilla.build.dockerfile import Unfoldable, fold, parse
from flotilla.build.files import ImageExport, export_task_files
from flotilla.build.images import (
    BuildSettings,
    Disposition,
    ImageError,
    ImageRef,
    Registry,
    ServiceImage,
    pin_images,
    plan_images,
    resolve_images,
)
from flotilla.build.meta import ImageAccounts, UserLookup, parse_config
from flotilla.compose.manifest_gen import build_manifest
from flotilla.compose.model import ImageMeta, Project
from flotilla.compose.task import HarborTask
from flotilla.manifest import Resources
from flotilla.platform.base import Capabilities
from flotilla.scan import ScanResult, scan_task

MANIFEST_NAME = "flotilla.manifest.json"
FILES_DIR = "files"


@dataclass(frozen=True)
class BuildOutcome:
    """Build result for one task. `status` determines whether a manifest was produced."""

    task: str
    status: str  # built / rejected / failed
    build_key: str = ""
    manifest_path: Path | None = None
    #: Source image per service: link, mirror, config-only, cache-hit or built.
    dispositions: Mapping[str, str] = field(default_factory=dict)
    #: Services whose final image this call produced with the builder (application build or USER0 adaptation).
    built: tuple[str, ...] = ()
    #: Services running a USER0 adaptation of their source image.
    adapted: tuple[str, ...] = ()
    #: Internal staging contains bind data or seeds. Legacy callers publish it; Harbor attempt owners
    #: transfer it to attempt-private storage and set task_files without a persistent public dataset.
    needs_publish: bool = False
    reason: str = ""
    scan: ScanResult | None = None

    def to_json(self) -> dict[str, object]:
        return {
            "task": self.task,
            "status": self.status,
            "build_key": self.build_key,
            "manifest": str(self.manifest_path) if self.manifest_path else None,
            "dispositions": dict(self.dispositions),
            "built": list(self.built),
            "adapted": list(self.adapted),
            "needs_publish": self.needs_publish,
            "reason": self.reason,
        }


@dataclass(frozen=True)
class BuildContext:
    """External dependencies for a build. When `builder` / `image_export` is None, tasks needing it report `failed`."""

    settings: BuildSettings
    caps: Capabilities
    registry: Registry
    builder: Builder | None = None
    image_export: ImageExport | None = None
    user_lookup: UserLookup | None = None
    image_accounts: Callable[[str], ImageAccounts] | None = None
    validate_mount_paths: Callable[[str, tuple[str, ...]], None] | None = None
    # Operation-scoped only: shared across roles/rows, never a global build cache.
    adaptations: dict[str, str] = field(default_factory=dict)


def preflight_images(plans: Sequence[ServiceImage], ctx: BuildContext) -> None:
    """Fail before any registry or builder side effect when publication requirements are missing.

    A non-root image USER is adapted with a published USER0 derivation even for
    linked and mirrored services, so it requires a builder and a target like an
    explicit build or mirror does. Mirroring is only ever explicit configuration;
    an unpullable source without it is rejected at planning time.
    """
    for plan in plans:
        if plan.ref is not None:
            source = plan.ref.pinned(plan.ref.digest or "")
            nonroot = str(ctx.registry.image_config(source).get("User") or "") not in ("", "0", "0:0")
        else:
            nonroot = False
        if nonroot and (not ctx.settings.target or ctx.builder is None):
            raise ImageError(
                f"service {plan.service} has an image USER requiring a root exec daemon image; "
                "configure a builder and --image-repo or [build].target"
            )
        if plan.disposition in ("build", "mirror") and not ctx.settings.target:
            raise ImageError("image publication requires --image-repo or [build].target")
        if plan.disposition == "build" and ctx.builder is None:
            raise ImageError(f"service {plan.service} needs a build, but no builder was provided")


def build_task(task: HarborTask, out_dir: Path, ctx: BuildContext, *, identity: str | None = None) -> BuildOutcome:
    """Always compile current inputs in an operation-private directory.

    `identity` (e.g. `<task>--<role>`) names output image tags; without it tags derive from the
    environment directory path.
    """
    result = scan_task(task, ctx.caps)
    if result.status == "rejected":
        reasons = [f.reason for f in result.findings if f.kind == "reject"]
        return BuildOutcome(task.name, "rejected", reason="; ".join(reasons[:3]), scan=result)
    try:
        return _build(task, out_dir, ctx, result, identity)
    except (ImageError, OSError, ValueError) as exc:
        return BuildOutcome(task.name, "failed", reason=f"{type(exc).__name__}: {exc}", scan=result)


def _build(
    task: HarborTask, out_dir: Path, ctx: BuildContext, result: ScanResult, identity: str | None
) -> BuildOutcome:
    project = task.project
    plans = pin_images(plan_images(task.project, ctx.settings), ctx.registry)
    resources = {name: Resources.model_validate(svc.resources) for name, svc in project.services.items()}
    # Output directory identity only: never an input-version or a cache key.
    key = hashlib.sha256(uuid.uuid4().bytes).hexdigest()
    target_dir = out_dir / key
    manifest_path = target_dir / MANIFEST_NAME
    # Output tags are stable across source edits and independent of resources.
    name = identity or task.name
    tag_key = hashlib.sha256((identity or str(task.env_dir.resolve())).encode()).hexdigest()

    # Reject missing publication requirements before *any* build/copy side effect.
    preflight_images(plans, ctx)

    resolution = resolve_images(plans, ctx.settings, ctx.registry)
    images = dict(resolution.images)
    dispositions: dict[str, str] = dict(resolution.dispositions)
    configs: dict[str, Mapping[str, Any]] = {}
    for plan in plans:
        if plan.disposition == "build":
            source = _source_image(task, name, plan, tag_key, ctx)
            images[plan.service], dispositions[plan.service] = source.image, source.disposition
            if source.config is not None:
                configs[plan.service] = source.config

    for service, svc in project.services.items():
        paths = tuple(m.mount_path for m in svc.tmpfs)
        if svc.readonly_rootfs:
            paths += ("/.flotilla-runtime",)
        if paths:
            if ctx.validate_mount_paths is None:
                raise ImageError("tmpfs/readonly preparation requires image mount-path inspection")
            ctx.validate_mount_paths(images[service], paths)

    files_dir = target_dir / FILES_DIR
    exported = export_task_files(project, task.env_dir, files_dir, images=images, image_export=ctx.image_export)

    meta = _metadata(project, images, ctx, configs)
    # Capture task identity before changing the container's default user. The
    # exec daemon needs root to initialize networking/files and switch identities;
    # service commands and healthchecks still use the original manifest context.
    adapted, adapted_now = _runtime_images(name, images, tag_key, target_dir / "runtime-context", ctx)
    manifest = build_manifest(
        task=task.name,
        project=project,
        images=images,
        meta=meta,
        resources=resources,
        seeds=exported.seeds,
    )
    target_dir.mkdir(parents=True, exist_ok=True)
    manifest_path.write_text(manifest.dump_json())
    return BuildOutcome(
        task.name,
        "built",
        key,
        manifest_path,
        dispositions,
        tuple(sorted({s for s, d in dispositions.items() if d == "built"} | adapted_now)),
        tuple(sorted(adapted)),
        needs_publish=not exported.empty,
        scan=result,
    )


def _runtime_images(
    name: str, images: dict[str, str], key: str, context: Path, ctx: BuildContext
) -> tuple[set[str], set[str]]:
    """Give the exec daemon root without changing previously captured task metadata.

    This also applies to linked and mirrored images, not just source builds or
    file derivations. A config-only layer requires no shell in the base image.
    """
    changed: set[str] = set()
    built: set[str] = set()
    derived = ctx.adaptations
    for service, image in images.items():
        user = str(ctx.registry.image_config(image).get("User") or "")
        if user in ("", "0", "0:0"):
            continue
        if ctx.builder is None or not ctx.settings.target:
            raise ImageError(
                f"service {service} has an image USER requiring a root exec daemon image; "
                "configure a builder and [build].target"
            )
        adaptation = hashlib.sha256(f"user0-v1|linux/amd64|{ctx.settings.target}|{image}".encode()).hexdigest()
        if adaptation not in derived:
            reference = f"user0-{adaptation[:32]}"
            # The tag is a function of the source digest and target: an existing one is this adaptation.
            remote_digest = ctx.registry.find(ctx.settings.target, reference)
            if remote_digest is None:
                context.mkdir(parents=True, exist_ok=True)
                request = BuildRequest(
                    service=service,
                    tag=f"{ctx.settings.target}:{reference}",
                    context=context,
                    dockerfile_text=f"FROM {image}\nUSER 0:0\n",
                )
                built_digest = ctx.builder.build(request)
                built.add(service)
                remote_digest = ctx.registry.digest(ctx.settings.target, reference)
                if remote_digest != built_digest:
                    raise ImageError("published adaptation digest differs from builder result")
            derived[adaptation] = f"{ctx.settings.target}@{remote_digest}"
        images[service] = derived[adaptation]
        changed.add(service)
    return changed, built


@dataclass(frozen=True)
class _Source:
    image: str  # repo@sha256:...
    disposition: str  # config-only, cache-hit or built
    config: Mapping[str, Any] | None = None  # synthesized image config for config-only


def _source_image(task: HarborTask, name: str, plan: ServiceImage, key: str, ctx: BuildContext) -> _Source:
    """Reuse the base digest of a config-only Dockerfile, a cached build of identical inputs, or build."""
    service = plan.service
    svc = task.project.services[service]
    assert svc.build is not None
    dockerfile = svc.build.dockerfile or "Dockerfile"
    context = task.env_dir / svc.build.context
    text = (context / dockerfile).read_text()
    folded = _fold(service, text, svc.build.args, ctx)
    if folded is not None:
        return folded
    if ctx.builder is None:
        raise ImageError(f"service {service} needs a build, but no builder was provided")
    if not ctx.settings.target:
        raise ImageError(f"service {service} needs a build, but [build].target is not configured")
    contexts = build_contexts(text, ctx.settings)
    content_key = _content_key(dockerfile, text, context, svc.build.args, contexts, ctx)
    if content_key is not None:
        reference = cache_tag(content_key)
        found = ctx.registry.find(ctx.settings.target, reference)
        if found is not None:
            return _Source(f"{ctx.settings.target}@{found}", "cache-hit")
        tag = f"{ctx.settings.target}:{reference}"
    else:
        tag = image_tag(ctx.settings.target, name, service, key)
    request = BuildRequest(
        service=service, tag=tag, context=context, dockerfile=dockerfile, args=svc.build.args, contexts=contexts
    )
    return _Source(f"{ctx.settings.target}@{ctx.builder.build(request)}", "built")


def _fold(service: str, text: str, args: Mapping[str, str], ctx: BuildContext) -> _Source | None:
    try:
        base = ImageRef.parse(ctx.settings.replace(parse(text).base))
    except (Unfoldable, ImageError):
        return None
    disposition: Disposition
    if ctx.settings.mirror_images:
        disposition = "mirror"
    elif ctx.settings.pullable(base):
        disposition = "link"
    else:
        return None  # only the builder can reach this base
    [pinned] = pin_images([ServiceImage(service, disposition, base)], ctx.registry)
    assert pinned.ref is not None and pinned.ref.digest is not None
    try:
        config = fold(text, ctx.registry.image_config(pinned.ref.pinned(pinned.ref.digest)), args)
    except Unfoldable:
        return None
    image = resolve_images([pinned], ctx.settings, ctx.registry).images[service]
    return _Source(image, "config-only", config)


def _content_key(
    dockerfile: str,
    text: str,
    context: Path,
    args: Mapping[str, str],
    contexts: Mapping[str, str],
    ctx: BuildContext,
) -> str | None:
    """`None` when a base cannot be pinned: the build is then never reused."""
    bases: dict[str, str] = {}
    for ref in base_images(text):
        if "$" in ref:
            return None
        try:
            parsed = ImageRef.parse(ctx.settings.replace(ref))
            digest = parsed.digest or ctx.registry.digest(parsed.name, parsed.tag or "latest")
        except ImageError:
            return None
        bases[ref] = parsed.pinned(digest)
    return build_key(
        dockerfile=dockerfile, dockerfile_text=text, context=context, args=args, bases=bases, contexts=contexts
    )


def _metadata(
    project: Project,
    images: Mapping[str, str],
    ctx: BuildContext,
    configs: Mapping[str, Mapping[str, Any]] | None = None,
) -> dict[str, ImageMeta]:
    """Per-service image metadata and Compose `user` resolution; `configs` replaces folded images' configs."""
    meta: dict[str, ImageMeta] = {}
    for name, svc in project.services.items():
        folded = (configs or {}).get(name)
        config = dict(folded if folded is not None else ctx.registry.image_config(images[name]))
        # An explicit Compose identity makes the image USER irrelevant.
        identity = svc.user if svc.user is not None else str(config.get("User") or "0:0")
        config["User"] = identity
        lookup, group_lookup = ctx.user_lookup, None
        user, _, group = identity.partition(":")
        if ctx.image_accounts is not None and not (user.isdigit() and group.isdigit()):
            accounts = ctx.image_accounts(images[name])
            lookup, group_lookup = accounts.user, accounts.group
        meta[name] = parse_config(config, lookup=lookup, group_lookup=group_lookup)
    return meta
