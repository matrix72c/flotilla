"""flotilla 命令行入口（Architecture §16.3）。

已实现：`probe`、`scan`、`build`、`publish`、`gc`、`share`。

平台凭证只从配置 `opensandbox.credential_env` 指定的环境变量读取（原则 8）。
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
import uuid
from collections import Counter
from collections.abc import Sequence
from pathlib import Path

from flotilla.build.docker import DockerBuilder, DockerImageExport, DockerRegistry
from flotilla.build.driver import FILES_DIR, MANIFEST_NAME, BuildContext, BuildOutcome, build_task, write_report
from flotilla.capabilities import CapabilityReport
from flotilla.compose.task import TaskError, find_tasks, load_task
from flotilla.config import ConfigError, FlotillaConfig
from flotilla.core.anchor import Anchor
from flotilla.core.deletion import Deleter
from flotilla.core.gc import Gc, GcReport
from flotilla.core.volumes import LAUNCHES
from flotilla.manifest import Manifest, Resources
from flotilla.platform.base import Capabilities, FlotillaError
from flotilla.platform.clock import SystemClock
from flotilla.platform.opensandbox import OpenSandboxPlatform, build
from flotilla.probe import Declared, ProbeSettings, probe
from flotilla.scan import scan_task
from flotilla.share.publish import PublishError, mark_published, publish_task_files
from flotilla.share.release import ReleaseError, VerifyReport, gc_releases, publish_release, verify_release


def _platform(cfg: FlotillaConfig, clock: SystemClock, caps: Capabilities) -> OpenSandboxPlatform:
    """构造后端平台。`caps` 由调用方给出：`probe` 用声明文件的引导能力，其余用能力报告。"""
    return build(
        endpoint=cfg.opensandbox.endpoint,
        credential=cfg.credential(),
        clock=clock,
        settings=cfg.network_settings(),
        storage=cfg.storage_settings(),
        caps=caps,
        execd=cfg.execd_settings(),
        extensions=cfg.opensandbox.extensions,
        create_fields=cfg.opensandbox.create_fields,
        group_extension=cfg.opensandbox.group_extension,
    )


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="flotilla", description="Compose 环境 → 一组 sandbox 的编排层")
    sub = parser.add_subparsers(dest="command", required=True)
    b = sub.add_parser("build", help="离线构建并写任务清单（第 4 节）")
    b.add_argument("tasks", nargs="+", type=Path, help="Harbor 任务目录，或包含它们的上级目录")
    b.add_argument("--deployment", required=True, type=Path, help="部署配置（提供 [build] 与能力报告路径）")
    b.add_argument("--out", required=True, type=Path, help="输出目录：<out>/<build_key>/ 下是清单与任务文件")
    b.add_argument("--docker", default="docker", help="docker 可执行与前置参数，空格分隔")
    b.add_argument("--force", action="store_true", help="已有同键的清单也重建")
    b.add_argument("--report", type=Path, help="逐任务的构建报告，每行一个 JSON")
    pb = sub.add_parser("publish", help="上传任务文件并把清单标为已发布（§4.7）")
    pb.add_argument("out", type=Path, help="flotilla build 的输出目录（其下是 <build_key>/）")
    pb.add_argument("--deployment", required=True, type=Path, help="部署配置（提供共享存储与锚点镜像）")
    pb.add_argument("--keys", nargs="*", help="只发布这些构建键；缺省发布输出目录下的全部")
    s = sub.add_parser("scan", help="逐字段归类报告（第 13 节）")
    s.add_argument("tasks", nargs="+", type=Path, help="Harbor 任务目录，或包含任务目录的上级目录（递归找 task.toml）")
    s.add_argument("--capabilities", required=True, type=Path, help="目标部署的能力报告（flotilla probe 的产物）")
    s.add_argument("--out", type=Path, help="逐任务的报告，每行一个 JSON；缺省只打印汇总")
    s.add_argument("--top", type=int, default=15, help="汇总中列出的拒绝原因条数")
    g = sub.add_parser("gc", help="清理残留，经临时锚点（§5.6）")
    g.add_argument("--deployment", required=True, type=Path, help="部署配置（提供共享存储与锚点镜像）")
    g.add_argument("--launch", help="只处理指定的 launch，跳过两次观察的等待（操作者确认其进程已退出）")
    p = sub.add_parser("probe", help="对部署运行契约测试，写出能力报告（第 14 节）")
    p.add_argument("--deployment", required=True, type=Path, help="部署配置（FlotillaConfig 的 TOML）")
    p.add_argument("--declared", required=True, type=Path, help="部署者的声明文件（probe 不测的项，JSON）")
    p.add_argument("--image", required=True, help="被测单元的镜像（repo@sha256:…，须有 /bin/sh 与常见工具）")
    p.add_argument("--out", type=Path, help="能力报告的写出路径；缺省为配置中的 opensandbox.capabilities")
    p.add_argument("--hosts-recheck", type=float, default=30.0, help="写入 /etc/hosts 后隔多久再读（验收为 600）")
    sh = sub.add_parser("share", help="share 发布 / 校验 / gc（§3.3）")
    share_sub = sh.add_subparsers(dest="share_command", required=True)
    sp = share_sub.add_parser("publish", help="把一个目录发布为不可变的 share 发布")
    sp.add_argument("dir", type=Path, help="发布内容的根目录（其下即 /.flotilla 的内容）")
    sp.add_argument("--deployment", required=True, type=Path, help="部署配置（提供共享存储与锚点镜像）")
    sp.add_argument("--release", help="发布号；缺省为配置里的 share.release")
    sp.add_argument("--declared", type=Path, help="部署者声明文件；首次发布时用它引导，尚无能力报告")
    sp.add_argument("--component", action="append", default=[], help="组件版本，形如 name=version，可给多次")
    sv = share_sub.add_parser("verify", help="按配置钉住的摘要全量校验一次发布")
    sv.add_argument("--deployment", required=True, type=Path, help="部署配置（提供共享存储、发布号与摘要）")
    sv.add_argument("--release", help="要校验的发布号；给定时必须同时给 --sha256")
    sv.add_argument("--sha256", help="该发布 RELEASE.json 的 sha256；与 --release 成对使用")
    sg = share_sub.add_parser("gc", help="删除配置与 --keep 之外的旧发布")
    sg.add_argument("--deployment", required=True, type=Path, help="部署配置（其 share.release 一定保留）")
    sg.add_argument("--keep", action="append", default=[], help="额外保留的发布号，可给多次")
    sg.add_argument("--dry-run", action="store_true", help="只列出将删除的发布，不真的删")
    return parser


async def _probe(args: argparse.Namespace) -> int:
    cfg = FlotillaConfig.load(args.deployment)
    declared = Declared.model_validate_json(args.declared.read_bytes())
    clock = SystemClock()
    platform = _platform(cfg, clock, declared.bootstrap_capabilities())
    # 临时锚点（§5.6）：自己的 launch_id，挂共享根目录做目录操作，命令结束时删除；只在 launches/<id> 下建目录。
    anchor = Anchor(platform, clock, f"probe-{uuid.uuid4().hex[:12]}", cfg.anchor_settings())
    try:
        await anchor.start()
        await anchor.mkdir(f"launches/{anchor.launch_id}", stage="prepare")
        try:
            settings = ProbeSettings(
                image=args.image,
                share_release=cfg.share.release,
                endpoint=cfg.opensandbox.endpoint,
                hosts_recheck_s=args.hosts_recheck,
            )
            report = await probe(platform, clock, anchor, declared, settings)
        finally:
            await anchor.remove(f"launches/{anchor.launch_id}", stage="run")
    finally:
        await anchor.close()
        await platform.aclose()

    out = args.out or cfg.capabilities_path()
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(report.dump_json())
    missing = report.missing_required()
    print(f"能力报告已写出：{out}")
    print("必需项：" + ("全部满足" if not missing else "不满足 " + "、".join(missing)))
    print("安全类缺口：" + ("无" if not report.security_gaps() else "、".join(report.security_gaps())))
    return 1 if missing else 0


def _scan(args: argparse.Namespace) -> int:
    """逐任务归类（§13）：每行一个 JSON 写到 `--out`，汇总打印到 stdout。读不出来的任务记为拒绝，不中止扫描。"""
    caps = CapabilityReport.load_json(args.capabilities.read_bytes()).to_capabilities()
    status: Counter[str] = Counter()
    reasons: Counter[str] = Counter()
    by_shape: Counter[tuple[str, str]] = Counter()
    out = args.out.open("w") if args.out is not None else None
    try:
        for i, path in enumerate(find_tasks(args.tasks), 1):
            try:
                task = load_task(path)
            except TaskError as exc:
                line = {"task": path.name, "path": str(path), "status": "rejected", "findings": [
                    {"path": "task", "kind": "reject", "reason": f"无法读取：{exc}"}
                ]}  # fmt: skip
                reasons["无法读取任务目录"] += 1
                shape = "unreadable"
            else:
                result = scan_task(task, caps)
                line = {**result.to_json(), "path": str(path)}
                reasons.update({f.reason for f in result.findings if f.kind == "reject"})
                shape = "single" if len(result.services) == 1 else "multi"
            status[str(line["status"])] += 1
            by_shape[(shape, str(line["status"]))] += 1
            if out is not None:
                out.write(json.dumps(line, ensure_ascii=False) + "\n")
            if i % 1000 == 0:
                print(f"已扫描 {i} 个任务", file=sys.stderr, flush=True)
    finally:
        if out is not None:
            out.close()
    total = sum(status.values())
    print(f"任务 {total} 个：通过 {status['accepted']}，拒绝 {status['rejected']}")
    for shape in ("single", "multi", "unreadable"):
        a, r = by_shape[(shape, "accepted")], by_shape[(shape, "rejected")]
        if a or r:
            print(f"  {dict(single='单服务', multi='多服务', unreadable='读不出')[shape]}：通过 {a}，拒绝 {r}")
    if reasons:
        print(f"拒绝原因（按涉及的任务数，前 {args.top} 条）：")
        for reason, n in reasons.most_common(args.top):
            print(f"  {n:6d}  {reason}")
    return 0


def _build(args: argparse.Namespace) -> int:
    """逐任务构建（§4.2）。单个任务失败不中止，照实记入报告；有失败时退出码非 0。"""
    cfg = FlotillaConfig.load(args.deployment)
    docker_cmd = tuple(args.docker.split())
    ctx = BuildContext(
        settings=cfg.build.settings(),
        caps=cfg.load_report().to_capabilities(),
        registry=DockerRegistry(docker_cmd=docker_cmd),
        builder=DockerBuilder(docker_cmd=docker_cmd),
        image_export=DockerImageExport(docker_cmd=docker_cmd),
        resources={name: Resources(cpu=r.cpu, memory=r.memory) for name, r in cfg.resources.defaults.items()},
    )
    outcomes: list[BuildOutcome] = []
    for path in find_tasks(args.tasks):
        try:
            task = load_task(path)
        except TaskError as exc:
            outcomes.append(BuildOutcome(path.name, "failed", reason=f"无法读取任务目录：{exc}"))
            continue
        outcome = build_task(task, args.out, ctx, force=args.force)
        outcomes.append(outcome)
        print(f"{outcome.status:9s} {outcome.task}" + (f"  {outcome.reason}" if outcome.reason else ""), flush=True)
    counts = Counter(o.status for o in outcomes)
    print(f"共 {len(outcomes)} 个任务：" + "，".join(f"{k} {v}" for k, v in sorted(counts.items())))
    pending = sum(1 for o in outcomes if o.needs_publish)
    if pending:
        print(f"其中 {pending} 个导出了任务文件，运行前须先 flotilla publish（尚未实现）")
    if args.report is not None:
        write_report(outcomes, args.report)
    return 1 if counts["failed"] else 0


async def _publish(args: argparse.Namespace) -> int:
    """把构建输出里的任务文件发布到共享存储，并把清单标为已发布（§4.7）。

    经临时锚点操作（与 `probe` 一样有自己的 launch_id，命令结束即删除）。没有任务文件的构建键跳过。
    """
    cfg = FlotillaConfig.load(args.deployment)
    storage_root = cfg.storage_settings().root
    keys = args.keys or sorted(p.name for p in args.out.iterdir() if (p / MANIFEST_NAME).is_file())
    pending = [(k, args.out / k) for k in keys]
    if not pending:
        print(f"{args.out} 下没有可发布的构建输出", file=sys.stderr)
        return 2
    clock = SystemClock()
    platform = _platform(cfg, clock, cfg.load_report().to_capabilities())
    anchor = Anchor(platform, clock, f"publish-{uuid.uuid4().hex[:12]}", cfg.anchor_settings())
    failed = 0
    try:
        await anchor.start()
        for key, directory in pending:
            try:
                note = await _publish_one(anchor, key, directory, storage_root)
            except (PublishError, FlotillaError, OSError, ValueError) as exc:
                failed += 1
                note = f"失败：{type(exc).__name__}: {exc}"
            print(f"{key[:12]}  {note}", flush=True)
    finally:
        await anchor.close()
        await platform.aclose()
    return 1 if failed else 0


async def _publish_one(anchor: Anchor, key: str, directory: Path, storage_root: str) -> str:
    manifest_path = directory / MANIFEST_NAME
    manifest = Manifest.load_json(manifest_path.read_bytes())
    files_dir = directory / FILES_DIR
    if not files_dir.is_dir() or not any(files_dir.iterdir()):
        return "无任务文件，跳过"
    result = await publish_task_files(anchor, key, files_dir, storage_root=storage_root)
    manifest_path.write_text(mark_published(manifest, result, storage_root).dump_json())
    verb = "已是最新" if result.skipped else f"上传 {result.uploaded} 个文件"
    return f"{verb} → {result.task_files}"


async def _gc(args: argparse.Namespace) -> int:
    """清理残留 launch（§5.6）。

    一次性进程没有常驻锚点，所以像 `publish` 一样起一个临时锚点执行目录操作。它**不**创建
    `launches/<自己的 id>`，因此不会成为 gc 的候选。`--launch` 跳过两次观察的等待，由操作者确认该 launch
    的进程已退出；目标锚点仍在时 `Gc.collect` 拒绝。
    """
    cfg = FlotillaConfig.load(args.deployment)
    clock = SystemClock()
    platform = _platform(cfg, clock, cfg.load_report().to_capabilities())
    anchor = Anchor(platform, clock, f"gc-{uuid.uuid4().hex[:12]}", cfg.anchor_settings())
    try:
        await anchor.start()
        # `Gc.run_once` 第一件事是列出 `launches/`；目录还不存在时 `ls` 非零退出会让整轮放弃。
        await anchor.mkdir(LAUNCHES, stage="prepare")
        gc = Gc(platform, clock, anchor, Deleter(platform, clock), window_s=cfg.reaper.gc_window)
        if args.launch:
            try:
                done = await gc.collect(args.launch)
            except FlotillaError as exc:
                print(f"拒绝清理 {args.launch}：{exc}", file=sys.stderr)
                return 1
            print(f"{args.launch}：{'已删尽' if done else '未能删尽，下一轮再来'}")
            return 0 if done else 1
        window = cfg.reaper.gc_window
        print(f"第一轮观察（候选需隔 {window:.0f}s 的第二次观察才删除）")
        first = await gc.run_once()
        _print_gc(first)
        await clock.sleep(window)
        print("第二轮观察")
        second = await gc.run_once()
        _print_gc(second)
        return 1 if second.pending else 0
    finally:
        await anchor.close()
        await platform.aclose()


def _print_gc(report: GcReport) -> None:
    print(
        f"  存活 {sorted(report.alive)}；候选 {sorted(report.candidates)}；"
        f"已删除 {sorted(report.removed)}；未删尽 {sorted(report.pending)}"
    )


async def _share(args: argparse.Namespace) -> int:
    """share 发布的 publish / verify / gc（§3.3）。都经临时锚点操作共享存储。"""
    cfg = FlotillaConfig.load(args.deployment)
    clock = SystemClock()
    # 先把纯本地的参数校验做完再连平台：`--component` 写错时不该先看到一个凭证错误。
    components: dict[str, str] = {}
    if args.share_command == "publish":
        for pair in args.component:
            name, sep, value = pair.partition("=")
            if not sep or not name:
                print(f"--component 要写成 name=version：{pair!r}", file=sys.stderr)
                return 2
            components[name] = value
    if args.share_command == "verify":
        expected = args.sha256 or cfg.share.release_sha256
        if args.release and not args.sha256:
            # 配置钉的是别的发布号，它的摘要对不上这个发布，验了也没有意义。
            print("指定 --release 时必须同时指定该发布的 --sha256（不能拿配置里另一个发布的摘要来验）", file=sys.stderr)
            return 2
        caps = cfg.load_report().to_capabilities()
    elif args.share_command == "publish" and args.declared is not None:
        # 首次发布时还没有能力报告（probe 需要已备好的 share 发布），用声明文件的引导能力打破这个环。
        caps = Declared.model_validate_json(args.declared.read_bytes()).bootstrap_capabilities()
        expected = ""
    else:
        caps = cfg.load_report().to_capabilities()
        expected = ""
    platform = _platform(cfg, clock, caps)
    anchor = Anchor(platform, clock, f"share-{uuid.uuid4().hex[:12]}", cfg.anchor_settings())
    try:
        await anchor.start()
        if args.share_command == "publish":
            return await _share_publish(args, cfg, anchor, components)
        if args.share_command == "verify":
            release = args.release or cfg.share.release
            report = await verify_release(anchor, release, expected_sha256=expected)
            _print_verify(report)
            return 0 if report.ok else 1
        keep = {cfg.share.release, *args.keep}
        gc_report = await gc_releases(anchor, keep=keep, dry_run=args.dry_run)
        verb = "将删除" if args.dry_run else "已删除"
        print(f"保留 {sorted(gc_report.kept)}（钉住的发布 {cfg.share.release} 与 --keep）")
        print(f"{verb} {sorted(gc_report.removed)}")
        if gc_report.staging:
            # staging 可能是另一个正在组装发布的进程，所以只列不删（§3.3）。
            print(f"share/staging 下的条目未回收（可能是正在进行的发布）：{list(gc_report.staging)}")
        return 0
    finally:
        await anchor.close()
        await platform.aclose()


async def _share_publish(
    args: argparse.Namespace, cfg: FlotillaConfig, anchor: Anchor, components: dict[str, str]
) -> int:
    release = args.release or cfg.share.release
    try:
        result = await publish_release(anchor, release, args.dir, components=components)
    except (ReleaseError, FlotillaError, OSError, ValueError) as exc:
        print(f"发布失败：{type(exc).__name__}: {exc}", file=sys.stderr)
        return 1
    verb = "已是最新，未上传" if result.skipped else f"上传 {result.uploaded} 个文件"
    print(f"{release} {verb} → {result.path}")
    print("把下面两行写进部署配置的 [share]（provider 启动时按 release_sha256 核对）：")
    print(f'  release = "{release}"')
    print(f'  release_sha256 = "{result.release_sha256}"')
    return 0


def _print_verify(report: VerifyReport) -> None:
    if not report.manifest_ok:
        print(f"RELEASE.json 摘要不符：实际 {report.actual_sha256}，配置钉的是 {report.expected_sha256}")
        return
    print(f"{report.release}：校验 {report.checked} 个文件；" + ("全部一致" if report.ok else "有不一致"))
    for label, items in (
        ("缺失", report.missing),
        ("大小不符", report.size_mismatch),
        ("内容不符", report.digest_mismatch),
        ("权限不符", report.mode_mismatch),
    ):
        if items:
            print(f"  {label}：{list(items)}")


def main(argv: Sequence[str] | None = None) -> int:
    parser = _build_parser()
    args = parser.parse_args(argv)
    if args.command == "probe":
        try:
            return asyncio.run(_probe(args))
        except ConfigError as exc:
            print(exc, file=sys.stderr)
            return 2
    if args.command == "scan":
        return _scan(args)
    if args.command == "build":
        try:
            return _build(args)
        except ConfigError as exc:
            print(exc, file=sys.stderr)
            return 2
    if args.command == "publish":
        try:
            return asyncio.run(_publish(args))
        except ConfigError as exc:
            print(exc, file=sys.stderr)
            return 2
    if args.command == "gc":
        try:
            return asyncio.run(_gc(args))
        except ConfigError as exc:
            print(exc, file=sys.stderr)
            return 2
    if args.command == "share":
        try:
            return asyncio.run(_share(args))
        except ConfigError as exc:
            print(exc, file=sys.stderr)
            return 2
    print(f"子命令 {args.command!r} 尚未实现", file=sys.stderr)
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
