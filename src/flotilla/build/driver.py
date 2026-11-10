"""Prepare one task into a validated manifest and local publication files."""

from __future__ import annotations

import hashlib
import uuid
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path

from flotilla.build.builder import (
    Builder,
    BuildRequest,
    build_contexts,
    image_tag,
)
from flotilla.build.files import ImageExport, export_task_files
from flotilla.build.images import (
    BuildSettings,
    ImageError,
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
    dispositions: Mapping[str, str] = field(default_factory=dict)
    built: tuple[str, ...] = ()
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
    for plan in plans:
        if plan.disposition == "build":
            images[plan.service] = _build_one(task, name, plan, tag_key, ctx)

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

    meta = _metadata(project, images, ctx)
    # Capture task identity before changing the container's default user. The
    # exec daemon needs root to initialize networking/files and switch identities;
    # service commands and healthchecks still use the original manifest context.
    runtime_built = _runtime_images(name, images, tag_key, target_dir / "runtime-context", ctx)
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
        {**resolution.dispositions, **dict.fromkeys(runtime_built, "build")},
        tuple(sorted(set(resolution.to_build) | runtime_built)),
        needs_publish=not exported.empty,
        scan=result,
    )


def _runtime_images(name: str, images: dict[str, str], key: str, context: Path, ctx: BuildContext) -> set[str]:
    """Give the exec daemon root without changing previously captured task metadata.

    This also applies to linked and mirrored images, not just source builds or
    file derivations. A config-only layer requires no shell in the base image.
    """
    changed: set[str] = set()
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
            context.mkdir(parents=True, exist_ok=True)
            request = BuildRequest(
                service=service,
                tag=f"{ctx.settings.target}:user0-{adaptation[:32]}",
                context=context,
                dockerfile_text=f"FROM {image}\nUSER 0:0\n",
            )
            built_digest = ctx.builder.build(request)
            remote_digest = ctx.registry.digest(ctx.settings.target, request.tag.rsplit(":", 1)[1])
            if remote_digest != built_digest:
                raise ImageError("published adaptation digest differs from builder result")
            derived[adaptation] = f"{ctx.settings.target}@{remote_digest}"
        images[service] = derived[adaptation]
        changed.add(service)
    return changed


def _build_one(task: HarborTask, name: str, plan: ServiceImage, key: str, ctx: BuildContext) -> str:
    """Build one service's application image from its Dockerfile, returning `repo@sha256:...`."""
    service = plan.service
    if ctx.builder is None:
        raise ImageError(f"service {service} needs a build, but no builder was provided")
    if not ctx.settings.target:
        raise ImageError(f"service {service} needs a build, but [build].target is not configured")
    tag = image_tag(ctx.settings.target, name, service, key)
    svc = task.project.services[service]
    assert svc.build is not None
    dockerfile = svc.build.dockerfile or "Dockerfile"
    text = (task.env_dir / svc.build.context / dockerfile).read_text()
    request = BuildRequest(
        service=service,
        tag=tag,
        context=task.env_dir / svc.build.context,
        dockerfile=dockerfile,
        args=svc.build.args,
        contexts=build_contexts(text, ctx.settings),
    )
    return f"{ctx.settings.target}@{ctx.builder.build(request)}"


def _metadata(project: Project, images: Mapping[str, str], ctx: BuildContext) -> dict[str, ImageMeta]:
    """Per-service image metadata and Compose `user` resolution."""
    meta: dict[str, ImageMeta] = {}
    for name, svc in project.services.items():
        config = dict(ctx.registry.image_config(images[name]))
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
