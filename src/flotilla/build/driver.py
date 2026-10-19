"""Prepare one task into a validated manifest and local publication files."""

from __future__ import annotations

import hashlib
import uuid
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from pathlib import Path

from flotilla.build.builder import (
    Builder,
    BuildRequest,
    build_contexts,
    derive_dockerfile,
    image_tag,
)
from flotilla.build.files import ImageExport, export_task_files
from flotilla.build.images import (
    BuildSettings,
    ImageError,
    Registry,
    ServiceImage,
    image_changes,
    pin_images,
    plan_images,
    resolve_images,
)
from flotilla.build.meta import ImageAccounts, UserLookup, parse_config
from flotilla.compose.layout import classify
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
    #: Task files were exported (`files/` non-empty). Such tasks must be published before running: the
    #: manifest's `task_files` is written by publication, and training accepts only published manifests.
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


def build_task(task: HarborTask, out_dir: Path, ctx: BuildContext) -> BuildOutcome:
    """Always compile current inputs in an operation-private directory."""
    result = scan_task(task, ctx.caps)
    if result.status == "rejected":
        reasons = [f.reason for f in result.findings if f.kind == "reject"]
        return BuildOutcome(task.name, "rejected", reason="; ".join(reasons[:3]), scan=result)
    try:
        return _build(task, out_dir, ctx, result)
    except (ImageError, OSError, ValueError) as exc:
        return BuildOutcome(task.name, "failed", reason=f"{type(exc).__name__}: {exc}", scan=result)


def _build(task: HarborTask, out_dir: Path, ctx: BuildContext, result: ScanResult) -> BuildOutcome:
    project = task.project
    plans = pin_images(plan_images(task.project, ctx.settings), ctx.registry)
    resources = {name: Resources.model_validate(svc.resources) for name, svc in project.services.items()}
    # Execution-plan identity only: never an input-version or a cache key.
    key = hashlib.sha256(uuid.uuid4().bytes).hexdigest()
    target_dir = out_dir / key
    manifest_path = target_dir / MANIFEST_NAME
    # Output tags are stable across source edits and independent of resources.
    tag_key = hashlib.sha256(str(task.env_dir.resolve()).encode()).hexdigest()

    resolution = resolve_images(plans, ctx.settings, ctx.registry)
    images = dict(resolution.images)
    for plan in plans:
        if plan.disposition == "build":
            images[plan.service] = _build_one(task, plan, tag_key, ctx)

    files_dir = target_dir / FILES_DIR
    exported = export_task_files(project, task.env_dir, files_dir, images=images, image_export=ctx.image_export)

    meta, users = _metadata(project, images, ctx)
    manifest = build_manifest(
        task=task.name,
        build_key=key,
        project=project,
        images=images,
        meta=meta,
        resources=resources,
        users=users,
        seeds=exported.seeds,
    )
    target_dir.mkdir(parents=True, exist_ok=True)
    manifest_path.write_text(manifest.dump_json())
    return BuildOutcome(
        task.name,
        "built",
        key,
        manifest_path,
        resolution.dispositions,
        tuple(resolution.to_build),
        needs_publish=not exported.empty,
        scan=result,
    )


def _build_one(task: HarborTask, plan: ServiceImage, key: str, ctx: BuildContext) -> str:
    """Build or derive one service's image, returning `repo@sha256:...`."""
    service = plan.service
    if ctx.builder is None:
        raise ImageError(f"service {service} needs a build, but no builder was provided")
    if not ctx.settings.target:
        raise ImageError(f"service {service} needs a build, but [build].target is not configured")
    tag = image_tag(ctx.settings.target, task.name, service, key)
    svc = task.project.services[service]
    derive = service in image_changes(task.project)
    if plan.ref is not None:
        assert plan.ref.digest is not None
        base = plan.ref.pinned(plan.ref.digest)
    else:
        assert svc.build is not None
        dockerfile = svc.build.dockerfile or "Dockerfile"
        text = (task.env_dir / svc.build.context / dockerfile).read_text()
        request = BuildRequest(
            service=service,
            tag=image_tag(ctx.settings.target, f"{task.name}-base", service, key) if derive else tag,
            context=task.env_dir / svc.build.context,
            dockerfile=dockerfile,
            args=svc.build.args,
            contexts=build_contexts(text, ctx.settings),
        )
        base = f"{ctx.settings.target}@{ctx.builder.build(request)}"
    if not derive:
        return base
    config = ctx.registry.image_config(base)
    user = str(config.get("User") or "0:0")
    request = BuildRequest(
        service=service, tag=tag, context=task.env_dir, dockerfile_text=_derive(task, service, base, user=user)
    )
    return f"{ctx.settings.target}@{ctx.builder.build(request)}"


def _derive(task: HarborTask, service: str, base: str, *, user: str) -> str:
    """Apply binds and nocopy on top of the digest-pinned image, regardless of where the image came from."""
    layout = classify(task.project.services)
    copy = [
        (source, target)
        for group in layout.binds
        if group.mode == "image"
        for unit, source, target, _ in group.references
        if unit == service
    ]
    clear = [
        target
        for volume in layout.volumes
        if volume.mode == "local" and volume.nocopy
        for unit, target, _, _ in volume.references
        if unit == service
    ]
    clear.extend(task.project.services[service].tmpfs)
    return derive_dockerfile(base, copy=copy, clear=clear, user=user)


def _metadata(
    project: Project, images: Mapping[str, str], ctx: BuildContext
) -> tuple[dict[str, ImageMeta], dict[str, tuple[int, int]]]:
    """Per-service image metadata and Compose `user` resolution."""
    meta: dict[str, ImageMeta] = {}
    users: dict[str, tuple[int, int]] = {}
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
        fields = parse_config(config, lookup=lookup, group_lookup=group_lookup)
        meta[name] = fields
        if svc.user:
            users[name] = (fields.uid, fields.gid)
    return meta, users
