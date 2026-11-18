"""Prepare one task into a validated manifest and local publication files."""

from __future__ import annotations

import hashlib
import uuid
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from pathlib import Path

from flotilla.build.files import ImageExport, export_task_files
from flotilla.build.images import (
    BuildSettings,
    ImageError,
    Registry,
    pin_images,
    pinned_images,
    plan_images,
)
from flotilla.build.meta import ImageAccounts, parse_config
from flotilla.compose.manifest_gen import build_manifest
from flotilla.compose.model import ImageMeta, Project
from flotilla.compose.task import HarborTask
from flotilla.manifest import Resources
from flotilla.platform.base import Capabilities
from flotilla.scan import ScanResult, scan_task

MANIFEST_NAME = "flotilla.manifest.json"
FILES_DIR = "files"

_ROOT_USERS = frozenset({"", "0", "0:0", "root", "root:root"})
"""Image `USER` / Compose `user` values that run as root (0:0).

Docker resolves `USER` by name inside the image, making the string and numeric
spellings equivalent; published base images use both forms. They need no lookup
in the image account databases.
"""


@dataclass(frozen=True)
class BuildOutcome:
    """Build result for one task. `status` determines whether a manifest was produced."""

    task: str
    status: str  # built / rejected / failed
    build_key: str = ""
    manifest_path: Path | None = None
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
            "needs_publish": self.needs_publish,
            "reason": self.reason,
        }


@dataclass(frozen=True)
class BuildContext:
    """External dependencies for preparing a task. Without `image_export`, tasks needing it report `failed`."""

    settings: BuildSettings
    caps: Capabilities
    registry: Registry
    image_export: ImageExport | None = None
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
    images = pinned_images(pin_images(plan_images(task.project, ctx.settings), ctx.registry))
    resources = {name: Resources.model_validate(svc.resources) for name, svc in project.services.items()}
    # Output directory identity only: never an input-version or a cache key.
    key = hashlib.sha256(uuid.uuid4().bytes).hexdigest()
    target_dir = out_dir / key
    manifest_path = target_dir / MANIFEST_NAME

    _require_root_images(images, ctx)

    files_dir = target_dir / FILES_DIR
    exported = export_task_files(project, task.env_dir, files_dir, images=images, image_export=ctx.image_export)

    meta = _metadata(project, images, ctx)
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
    return BuildOutcome(task.name, "built", key, manifest_path, needs_publish=not exported.empty, scan=result)


def _require_root_images(images: Mapping[str, str], ctx: BuildContext) -> None:
    """The exec daemon is the sandbox entrypoint and needs root to set up networking, files and identities.

    Flotilla used to publish a `USER 0:0` derivation of a non-root image, which made preparation a
    builder with registry write access. A delivered image carries its own root `USER` instead; a
    service that must run as someone else says so with Compose `user`, which this does not constrain.
    """
    for service, image in sorted(images.items()):
        user = str(ctx.registry.image_config(image).get("User") or "")
        if user not in _ROOT_USERS:
            raise ImageError(
                f"service {service} has image USER {user!r}: the exec daemon needs a root image; "
                "publish the image with USER 0:0 and declare a non-root process with Compose user"
            )


def _metadata(project: Project, images: Mapping[str, str], ctx: BuildContext) -> dict[str, ImageMeta]:
    """Per-service image metadata and Compose `user` resolution."""
    meta: dict[str, ImageMeta] = {}
    for name, svc in project.services.items():
        config = dict(ctx.registry.image_config(images[name]))
        # An explicit Compose identity makes the image USER irrelevant.
        identity = svc.user if svc.user is not None else str(config.get("User") or "0:0")
        if identity in _ROOT_USERS:
            identity = "0:0"
        config["User"] = identity
        lookup, group_lookup = None, None
        user, _, group = identity.partition(":")
        if ctx.image_accounts is not None and not (user.isdigit() and group.isdigit()):
            accounts = ctx.image_accounts(images[name])
            lookup, group_lookup = accounts.user, accounts.group
        meta[name] = parse_config(config, lookup=lookup, group_lookup=group_lookup)
    return meta
