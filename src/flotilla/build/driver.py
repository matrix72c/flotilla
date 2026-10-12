"""`flotilla build` 的驱动：一个任务目录 → 镜像与任务清单（Architecture §4.2）。

把 §4.2 的各步串起来，每步的实现都在别的模块里，本模块只负责顺序、跳过与错误归属：

1. 解析与规范化（`compose.task`）；
2. 扫描（`scan`）：有拒绝项就不构建，照实报告；
3. 定每个服务的镜像（`images` / `prebuilt`）：预构建 → 校验后 link；否则按可拉前缀 link / mirror / build；
4. 构建键（`keys`）：键不变则整个任务跳过；
5. 构建需要构建的服务（`builder`）；
6. 导出任务文件（`files`）；
7. 取镜像元数据（`meta`）并写清单（`compose.manifest_gen`）。

构建机离线、没有平台凭证：只写本地输出目录，任务文件的上传由 `flotilla publish` 完成（§4.7）。
"""

from __future__ import annotations

import json
from collections.abc import Mapping
from dataclasses import asdict, dataclass, field, replace
from pathlib import Path

from flotilla.build.builder import (
    Builder,
    BuildRequest,
    build_contexts,
    derive_dockerfile,
    image_tag,
    unreplaced_bases,
)
from flotilla.build.files import ImageExport, export_task_files
from flotilla.build.images import (
    BuildSettings,
    ImageError,
    ImageRef,
    Registry,
    ServiceImage,
    image_changes,
    pin_images,
    plan_images,
    resolve_images,
)
from flotilla.build.keys import build_key, tree_hash
from flotilla.build.meta import UserLookup, parse_config, parse_user
from flotilla.build.prebuilt import PrebuiltMismatch, verify
from flotilla.compose.layout import classify
from flotilla.compose.manifest_gen import ImageMeta, build_manifest
from flotilla.compose.model import MAIN, Project
from flotilla.compose.task import HarborTask
from flotilla.manifest import Resources
from flotilla.platform.base import Capabilities
from flotilla.scan import ScanResult, scan_task

MANIFEST_NAME = "flotilla.manifest.json"
FILES_DIR = "files"
#: 没有 `[resources]` 配置时每个服务的占位资源（§4.5 由 task.toml 与配置给出，这里是兜底）。
DEFAULT_RESOURCES = Resources(cpu="1", memory="2Gi")


@dataclass(frozen=True)
class BuildOutcome:
    """一个任务的构建结果。`status` 决定是否产出了清单。"""

    task: str
    status: str  # built / skipped / rejected / failed
    build_key: str = ""
    manifest_path: Path | None = None
    dispositions: Mapping[str, str] = field(default_factory=dict)
    built: tuple[str, ...] = ()
    #: 导出了任务文件（`files/` 非空）。这类任务必须先 `flotilla publish` 才能运行：清单的 `task_files`
    #: 由发布写入（§4.7），训练侧只接受已发布的清单。
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
    """构建一次所需的外部依赖。`builder` / `image_export` 为 None 时，需要它们的任务以 `failed` 报告。"""

    settings: BuildSettings
    caps: Capabilities
    registry: Registry
    builder: Builder | None = None
    image_export: ImageExport | None = None
    requires_shell: bool = False  # 部署声明入口包装需要 /bin/sh（§3.4、§4.2 第 7 步）
    resources: Mapping[str, Resources] = field(default_factory=dict)
    user_lookup: UserLookup | None = None


def build_task(task: HarborTask, out_dir: Path, ctx: BuildContext, *, force: bool = False) -> BuildOutcome:
    """构建一个任务。已有同键的清单时跳过（`force` 时重建）。失败不抛出，以 `BuildOutcome` 报告。"""
    result = scan_task(task, ctx.caps)
    if result.status == "rejected":
        reasons = [f.reason for f in result.findings if f.kind == "reject"]
        return BuildOutcome(task.name, "rejected", reason="；".join(reasons[:3]), scan=result)
    try:
        return _build(task, out_dir, ctx, result, force=force)
    except (ImageError, PrebuiltMismatch, OSError, ValueError) as exc:
        return BuildOutcome(task.name, "failed", reason=f"{type(exc).__name__}: {exc}", scan=result)


def _build(task: HarborTask, out_dir: Path, ctx: BuildContext, result: ScanResult, *, force: bool) -> BuildOutcome:
    project = task.project
    plans = pin_images(_plan(task, ctx), ctx.registry)
    resources = {name: ctx.resources.get(name, DEFAULT_RESOURCES) for name in project.services}
    key = build_key(
        task=task.name,
        project=project,
        # 预构建与 link 也可能导出 bind，整个 environment 都参与，不随镜像处置丢掉输入。
        context_hashes={"environment": tree_hash(task.env_dir)} if task.env_dir.exists() else {},
        source_images={p.service: p.ref.pinned(p.ref.digest) for p in plans if p.ref is not None and p.ref.digest},
        deployment_inputs={
            "requires_shell": ctx.requires_shell,
            "build": asdict(ctx.settings),
            "resources": {name: r.model_dump() for name, r in resources.items()},
        },
    )
    target_dir = out_dir / key
    manifest_path = target_dir / MANIFEST_NAME
    if manifest_path.is_file() and not force:
        files_dir = target_dir / FILES_DIR
        has_files = files_dir.is_dir() and any(files_dir.iterdir())
        return BuildOutcome(
            task.name, "skipped", key, manifest_path, _dispositions(plans), needs_publish=has_files, scan=result
        )

    resolution = resolve_images(plans, ctx.settings, ctx.registry)
    images = dict(resolution.images)
    for plan in plans:
        if plan.disposition == "build":
            images[plan.service] = _build_one(task, plan, key, ctx)

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
        _dispositions(plans),
        tuple(resolution.to_build),
        needs_publish=not exported.empty,
        scan=result,
    )


def _plan(task: HarborTask, ctx: BuildContext) -> list[ServiceImage]:
    """每个服务的镜像处置；agent 服务的预构建镜像校验通过时作为 link 或派生基础（§4.2 第 3 步）。

    预构建镜像是整个任务环境的产物，对应 Harbor 的 agent 服务（`main`）；只在它本来要构建时替换。
    引用模板不见得覆盖所有数据集：**该任务没有预构建镜像时回退到正常构建**，不让整个任务失败。
    校验不通过则是另一回事——那说明镜像与任务脱钩，必须拒绝（`PrebuiltMismatch` 向上抛）。
    """
    plans = plan_images(task.project, ctx.settings)
    prebuilt = ctx.settings.prebuilt.ref_for(task.path.name)
    svc = task.project.services.get(MAIN)
    if prebuilt is None or svc is None or svc.build is None:
        return plans
    dockerfile = task.env_dir / svc.build.context / (svc.build.dockerfile or "Dockerfile")
    if not dockerfile.is_file():
        return plans
    ref = ImageRef.parse(prebuilt)
    try:
        (pinned,) = pin_images([ServiceImage(MAIN, "link", ref)], ctx.registry)
        assert pinned.ref is not None and pinned.ref.digest is not None
        history = ctx.registry.image_history(pinned.ref.pinned(pinned.ref.digest))
    except ImageError:
        return plans  # 没有这个任务的预构建镜像：照常构建
    if ctx.settings.prebuilt.verify:
        verify(dockerfile.read_text(), history)
    # 预构建只替代原 Dockerfile 构建；Compose 要求的 bind/nocopy 改动仍须派生。
    if MAIN in image_changes(task.project):
        pinned = replace(pinned, disposition="build")
    return [pinned if p.service == MAIN else p for p in plans]


def _build_one(task: HarborTask, plan: ServiceImage, key: str, ctx: BuildContext) -> str:
    """构建或派生一个服务的镜像，返回 `repo@sha256:…`。"""
    service = plan.service
    if ctx.builder is None:
        raise ImageError(f"服务 {service} 需要构建，但没有提供构建器")
    if not ctx.settings.target:
        raise ImageError(f"服务 {service} 需要构建，但 [build].target 未配置")
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
        missing = unreplaced_bases(text, ctx.settings)
        if missing:
            raise ImageError(f"服务 {service} 的基础镜像既无替换也不可拉：{missing}（见 [build].image_replacements）")
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
    """以固定 digest 的镜像为基础，统一应用 bind 与 nocopy，不受原镜像来源影响。"""
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
        for unit, target, _ in volume.references
        if unit == service
    ]
    return derive_dockerfile(base, copy=copy, clear=clear, user=user)


def _metadata(
    project: Project, images: Mapping[str, str], ctx: BuildContext
) -> tuple[dict[str, ImageMeta], dict[str, tuple[int, int]]]:
    """每个服务的镜像元数据与 Compose `user` 解析（§4.2 第 5 步）。"""
    meta: dict[str, ImageMeta] = {}
    users: dict[str, tuple[int, int]] = {}
    for name, svc in project.services.items():
        fields = parse_config(ctx.registry.image_config(images[name]), lookup=ctx.user_lookup)
        meta[name] = ImageMeta(
            entrypoint=fields.entrypoint,
            cmd=fields.cmd,
            uid=fields.uid,
            gid=fields.gid,
            workdir=fields.workdir,
            env=fields.env,
            healthcheck=fields.healthcheck,
        )
        if svc.user:
            users[name] = parse_user(svc.user, lookup=ctx.user_lookup)
    return meta, users


def _dispositions(plans: list[ServiceImage]) -> dict[str, str]:
    return {p.service: p.disposition for p in plans}


def write_report(outcomes: list[BuildOutcome], path: Path) -> None:
    """逐任务一行 JSON（与 `flotilla scan` 的报告同形）。"""
    with path.open("w") as handle:
        for outcome in outcomes:
            handle.write(json.dumps(outcome.to_json(), ensure_ascii=False) + "\n")
