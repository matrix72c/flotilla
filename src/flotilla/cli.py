"""flotilla 命令行入口（Architecture §16.3）。

已实现：`probe`。其余子命令（build / publish / scan / gc / share）参数骨架在位，执行时报"尚未实现"，随对应模块落地。

平台凭证只从配置 `opensandbox.credential_env` 指定的环境变量读取（原则 8）。
"""

from __future__ import annotations

import argparse
import asyncio
import sys
import uuid
from collections.abc import Sequence
from pathlib import Path

from flotilla.config import ConfigError, FlotillaConfig
from flotilla.core.anchor import Anchor
from flotilla.platform.clock import SystemClock
from flotilla.platform.opensandbox import build
from flotilla.probe import Declared, ProbeSettings, probe


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="flotilla", description="Compose 环境 → 一组 sandbox 的编排层")
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("build", help="离线构建并写任务清单（第 4 节）")
    sub.add_parser("publish", help="上传任务文件并把清单标为已发布（§4.7）")
    sub.add_parser("scan", help="逐字段归类报告（第 13 节）")
    sub.add_parser("gc", help="清理残留，经临时锚点（§5.6）")
    p = sub.add_parser("probe", help="对部署运行契约测试，写出能力报告（第 14 节）")
    p.add_argument("--deployment", required=True, type=Path, help="部署配置（FlotillaConfig 的 TOML）")
    p.add_argument("--declared", required=True, type=Path, help="部署者的声明文件（probe 不测的项，JSON）")
    p.add_argument("--image", required=True, help="被测单元的镜像（repo@sha256:…，须有 /bin/sh 与常见工具）")
    p.add_argument("--out", type=Path, help="能力报告的写出路径；缺省为配置中的 opensandbox.capabilities")
    p.add_argument("--hosts-recheck", type=float, default=30.0, help="写入 /etc/hosts 后隔多久再读（验收为 600）")
    sub.add_parser("share", help="share 发布 / 校验 / gc（§3.3）")
    return parser


async def _probe(args: argparse.Namespace) -> int:
    cfg = FlotillaConfig.load(args.deployment)
    declared = Declared.model_validate_json(args.declared.read_bytes())
    clock = SystemClock()
    platform = build(
        endpoint=cfg.opensandbox.endpoint,
        credential=cfg.credential(),
        clock=clock,
        settings=cfg.network_settings(),
        storage=cfg.storage_settings(),
        caps=declared.bootstrap_capabilities(),
        execd=cfg.execd_settings(),
        extensions=cfg.opensandbox.extensions,
        create_fields=cfg.opensandbox.create_fields,
    )
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


def main(argv: Sequence[str] | None = None) -> int:
    parser = _build_parser()
    args = parser.parse_args(argv)
    if args.command == "probe":
        try:
            return asyncio.run(_probe(args))
        except ConfigError as exc:
            print(exc, file=sys.stderr)
            return 2
    print(f"子命令 {args.command!r} 尚未实现", file=sys.stderr)
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
