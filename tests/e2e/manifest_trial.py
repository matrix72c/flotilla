"""端到端：用 `flotilla build` 产出的真实清单起一个 trial（Architecture 第 14 节"端到端"）。

`web_app_db.py` 用手写的 plan 验证编排与平台契约；本脚本验证的是**另一端**：构建产出的清单能不能真的跑起来。
路径是 清单 → `integrations.plan.to_plan` → `Trial`，与 provider 用的是同一条（§10 的 `open` / `get`）。

    uv run python -m tests.e2e.manifest_trial \\
        --deployment deployments/local/opensandbox.toml \\
        --manifest build-output/<build_key>/flotilla.manifest.json \\
        --check 'ls /app'

就绪后按 `--check` 执行命令（可给多次）：写 `命令` 在 agent 服务里执行，写 `单元:命令` 在该单元里执行。
不给时只验证就绪与基本信息。任务自己的验证脚本
（Harbor 的 `tests/`）不在这里跑：那属于评测，要经 Harbor 适配层。

已发布的清单（`task_files_published`）会核对共享根目录与配置一致——与 provider `open` 的检查相同（§4.7）。
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
import time
import uuid
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path

from flotilla.config import FlotillaConfig
from flotilla.core import labels
from flotilla.core.anchor import Anchor
from flotilla.core.deletion import Deleter
from flotilla.core.plan import TrialPlan
from flotilla.core.reaper import Reaper
from flotilla.core.trial import Trial
from flotilla.integrations.plan import to_plan
from flotilla.manifest import Manifest
from flotilla.platform.base import FlotillaError, InstanceHandle, Platform, ProcessSpec
from flotilla.platform.clock import SystemClock
from flotilla.platform.opensandbox import build


def load_plan(manifest_path: Path, cfg: FlotillaConfig) -> tuple[Manifest, TrialPlan]:
    """读清单并翻译为 `TrialPlan`，顺带做 provider `open` 的那几项检查（§10）。"""
    manifest = Manifest.load_json(manifest_path.read_bytes())
    published = manifest.task_files_published
    if manifest.task_files is not None:
        if published is None:
            raise SystemExit(f"清单有任务文件（{manifest.task_files}）但未发布：先跑 flotilla publish（§4.7）")
        expected = cfg.storage_settings().root
        if published.storage_root != expected:
            raise SystemExit(f"清单发布在 {published.storage_root}，与本部署的 {expected} 不符")
    return manifest, to_plan(manifest, runtime_params=dict(cfg.runtime_params))


async def run(args: argparse.Namespace) -> tuple[dict[str, object], list[Check]]:
    cfg = FlotillaConfig.load(args.deployment)
    report = cfg.load_report()
    manifest, plan = load_plan(args.manifest, cfg)
    clock = SystemClock()
    platform = build(
        endpoint=cfg.opensandbox.endpoint,
        credential=cfg.credential(),
        clock=clock,
        settings=cfg.network_settings(),
        storage=cfg.storage_settings(),
        caps=report.to_capabilities(),
        execd=cfg.execd_settings(),
        extensions=cfg.opensandbox.extensions,
        privileged_extensions=cfg.opensandbox.privileged_extensions,
        create_fields=cfg.opensandbox.create_fields,
        group_extension=cfg.opensandbox.group_extension,
    )
    out: dict[str, object] = {
        "task": manifest.task,
        "build_key": manifest.build_key,
        "units": sorted(plan.units),
        "agent_service": manifest.agent_service,
        "task_files": manifest.task_files,
        "security_gaps": report.security_gaps(),
    }
    launch_id = f"mt-{uuid.uuid4().hex[:12]}"
    trial_id = f"t{uuid.uuid4().hex[:12]}"
    anchor = Anchor(platform, clock, launch_id, cfg.anchor_settings())
    reaper: Reaper | None = None
    checks: list[Check] = []
    began = time.monotonic()
    try:
        await anchor.start()
        await anchor.mkdir(f"launches/{launch_id}", stage="prepare")
        reaper = Reaper(platform, clock, anchor, Deleter(platform, clock), cfg.reaper_settings())
        reaper.track(trial_id)
        trial = Trial(platform, clock, anchor, reaper, plan, trial_id, cfg.trial_settings())
        try:
            ready = await trial.start()
        except FlotillaError as exc:
            out["error"] = {"stage": exc.stage, "category": exc.category.value, "message": str(exc)}
            out["created"] = {u: h.iid for u, h in trial.handles.items()}
            return out, checks
        out["ready_s"] = round(time.monotonic() - began, 1)
        out["timings"] = {k: round(v, 1) for k, v in ready.timings.items()}
        out["created"] = {u: h.iid for u, h in ready.handles.items()}
        out["addresses"] = dict(ready.addresses)
        out["pids"] = dict(ready.pids)
        checks = await _checks(platform, manifest, ready.handles, args.check)
        out["checks"] = [c.__dict__ for c in checks]
    finally:
        if reaper is not None:
            reaper.release(trial_id)
            out["reaper_clean"] = await reaper.close()
        else:
            await anchor.close()
        out["leftover"] = await _leftover(platform, launch_id)
        await platform.aclose()
    return out, checks


@dataclass(frozen=True)
class Check:
    unit: str
    command: str
    exit_code: int
    output: str


async def _checks(
    platform: Platform, manifest: Manifest, handles: Mapping[str, InstanceHandle], commands: list[str]
) -> list[Check]:
    """按清单的执行上下文执行 `--check`（与使用方的 `execute` 同一条路径，§3.5）。

    `单元:命令` 指定单元，否则用 agent 服务。单元名用清单里的名字；不存在时报错而不是回退（PRD F2）。
    """
    results: list[Check] = []
    for raw in commands:
        unit, command = _split(raw, manifest.agent_service, set(manifest.units))
        context = manifest.units[unit].context
        proc = ProcessSpec(
            argv=("/bin/sh", "-c", command),
            uid=context.uid,
            gid=context.gid,
            cwd=context.cwd,
            env=dict(context.env),
            timeout_s=120.0,
        )
        result = await platform.exec(handles[unit], proc)
        output = (result.stdout + result.stderr).decode(errors="replace").strip()
        results.append(Check(unit=unit, command=command, exit_code=result.exit_code, output=output[:400]))
    return results


def _split(raw: str, default_unit: str, units: set[str]) -> tuple[str, str]:
    head, sep, rest = raw.partition(":")
    if sep and head in units:
        return head, rest
    if sep and head and not head.startswith((" ", "/", ".")) and " " not in head and head not in units:
        raise SystemExit(f"--check 指定的单元 {head!r} 不在任务中：{sorted(units)}")
    return default_unit, raw


async def _leftover(platform: Platform, launch_id: str) -> list[str]:
    await asyncio.sleep(platform.caps.list_visibility_s)
    left = [s.iid for s in await platform.list({labels.LAUNCH: launch_id})]
    for iid in left:
        await platform.delete(iid)
    return left


def _print(out: dict[str, object], checks: list[Check]) -> None:
    print(f"任务 {out['task']}（构建键 {str(out['build_key'])[:12]}）；单元 {out['units']}")
    print(f"任务文件：{out['task_files'] or '无'}；能力报告的安全类缺口：{out['security_gaps']}")
    if "error" in out:
        print(f"启动失败：{out['error']}")
    else:
        print(f"就绪用时 {out['ready_s']}s，各阶段 {out['timings']}")
        print(f"地址 {out['addresses']}")
        for check in checks:
            status = "通过" if check.exit_code == 0 else f"退出码 {check.exit_code}"
            print(f"  [{status}] {check.unit}: {check.command}\n      {check.output}")
    print(f"实例 {out.get('created')}")
    print(f"回收：{'干净' if out.get('reaper_clean') else '未在时限内确认，交给 gc'}；残留 {out['leftover'] or '无'}")


def main() -> int:
    parser = argparse.ArgumentParser(
        prog="python -m tests.e2e.manifest_trial", description="用构建产出的清单起一个 trial"
    )
    parser.add_argument("--deployment", required=True, type=Path, help="部署配置")
    parser.add_argument("--manifest", required=True, type=Path, help="flotilla.manifest.json")
    parser.add_argument("--check", action="append", default=[], help="就绪后在 agent 服务里执行的命令，可给多次")
    parser.add_argument("--out", type=Path, help="结果写成 JSON")
    args = parser.parse_args()
    out, checks = asyncio.run(run(args))
    _print(out, checks)
    if args.out is not None:
        args.out.write_text(json.dumps(out, ensure_ascii=False, indent=2) + "\n")
    failed = "error" in out or any(c.exit_code != 0 for c in checks)
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
