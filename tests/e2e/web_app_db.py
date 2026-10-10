"""端到端：在一个部署上跑 web + app + db trial（Architecture 第 14 节"端到端"）。

经 `Anchor` / `Reaper` / `Trial` 启动一个真实 trial，就绪后在单元里执行检查，最后交给回收器并确认实例删尽。
需要已经跑过 `flotilla probe` 的部署配置与能力报告；不做 provider 的启动校验，报告里的缺口只打印。

    uv run python -m tests.e2e.web_app_db \\
        --deployment deployments/local/opensandbox.toml \\
        --image '<registry>/flotilla:probe@sha256:<digest>' \\
        --out deployments/local/e2e.json

三个单元都用 `--image`（须有 `/bin/sh`、`nc`（OpenBSD 版，支持 `-l -q -u -z`）、`getent`、`timeout`、`grep`；
`images/probe` 的镜像满足）。拓扑与 Compose 中的典型三层服务一致：

| 单元 | 网络 | 业务 | 依赖 |
|---|---|---|---|
| web | frontend | 无监听，相当于 agent 所在的服务 | app 健康 |
| app | frontend、backend | TCP 8080：每次连接先向 db 取一次回应再回复 | db 健康 |
| db | backend（internal） | TCP 5432 回 `db-pong`；UDP 5433 收到的内容记入 /tmp/udp.log；启动时向共享卷写标记 | — |

app 与 db 读写挂载同一个 trial 卷 `/data`。检查项见 `CHECKS`；全部通过时退出码为 0。
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
import time
import uuid
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from pathlib import Path

from flotilla.config import FlotillaConfig
from flotilla.core import labels
from flotilla.core.anchor import Anchor
from flotilla.core.deletion import Deleter
from flotilla.core.plan import Dependency, ExecContext, Healthcheck, Mount, Network, TrialPlan, TrialVolume, Unit
from flotilla.core.reaper import Reaper
from flotilla.core.trial import Ready, Trial
from flotilla.platform.base import FlotillaError, Platform, ProcessSpec, Resources
from flotilla.platform.clock import SystemClock
from flotilla.platform.opensandbox import build

PATH = "/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin"
CTX = ExecContext(env={"PATH": PATH}, uid=0, gid=0, cwd="/")
RESOURCES = Resources(cpu="0.5", memory="512Mi")
_PUBLIC = "1.1.1.1 443"

DB = (
    "echo from-db > /data/from-db; "
    "(while true; do timeout 5 nc -u -l 5433 >> /tmp/udp.log 2>/dev/null; done) & "
    "while true; do echo db-pong | nc -l -q 1 5432 > /dev/null 2>&1; done"
)
APP = (
    "while true; do r=$(echo q | nc -w 2 db 5432); "
    'echo "app-pong via ${r:-nothing}" | nc -l -q 1 8080 > /dev/null 2>&1; done'
)


def _health(script: str) -> Healthcheck:
    return Healthcheck(test=("/bin/sh", "-c", script), interval_s=2.0, timeout_s=5.0, retries=10, start_interval_s=2.0)


def web_app_db(image: str) -> TrialPlan:
    def unit(command: str | None, **kw: object) -> Unit:
        argv = ("/bin/sh", "-c", command) if command is not None else ("/bin/sh", "-c", "exec sleep 2147483647")
        return Unit(image=image, resources=RESOURCES, context=CTX, command=argv, **kw)  # type: ignore[arg-type]

    data = Mount(scope="trial", key="data", target="/data", read_only=False)
    return TrialPlan(
        task="e2e-web-app-db",
        task_files=None,
        units={
            "web": unit(None),
            "app": unit(
                APP, mounts=(data,), healthcheck=_health("echo hc | nc -w 5 127.0.0.1 8080 | grep -q app-pong")
            ),
            "db": unit(DB, mounts=(data,), healthcheck=_health("echo hc | nc -w 3 127.0.0.1 5432 | grep -q db-pong")),
        },
        networks={
            "frontend": Network(
                internal=False,
                members=frozenset({"web", "app"}),
                names={"web": frozenset({"web"}), "app": frozenset({"app"})},
            ),
            "backend": Network(
                internal=True,
                members=frozenset({"app", "db"}),
                names={"app": frozenset({"app"}), "db": frozenset({"db"})},
            ),
        },
        depends_on={"app": {"db": Dependency("service_healthy")}, "web": {"app": Dependency("service_healthy")}},
        volumes=(TrialVolume(key="data"),),
        external_units=frozenset({"web", "app"}),
    )


# ───────────────────────────── 检查 ─────────────────────────────


@dataclass(frozen=True)
class Result:
    name: str
    ok: bool
    evidence: str


class Checks:
    def __init__(self, platform: Platform, ready: Ready) -> None:
        self._platform = platform
        self._handles = ready.handles
        self.addresses = ready.addresses

    async def sh(self, unit: str, script: str, timeout_s: float = 30.0) -> tuple[int, str]:
        proc = ProcessSpec(
            argv=("/bin/sh", "-c", script), uid=0, gid=0, cwd="/", env={"PATH": PATH}, timeout_s=timeout_s
        )
        result = await self._platform.exec(self._handles[unit], proc)
        return result.exit_code, (result.stdout + result.stderr).decode(errors="replace").strip()

    async def reach(self, unit: str, target: str, port: int) -> tuple[bool, str]:
        _, out = await self.sh(unit, f"echo q | nc -w 3 {target} {port} 2>&1; true")
        return "pong" in out, out[:120] or "（无回应）"

    async def open_port(self, unit: str, target: str) -> tuple[bool, str]:
        _, out = await self.sh(unit, f"nc -z -w 3 {target} && echo open || echo closed")
        return out.endswith("open"), f"{target}: {out}"


async def _name_chain(c: Checks) -> Result:
    ok, out = await c.reach("web", "app", 8080)
    return Result("web→app:8080 按服务名可达，app→db 经 backend 可达", ok and "db-pong" in out, out)


async def _web_cannot_resolve_db(c: Checks) -> Result:
    code, out = await c.sh("web", "getent hosts db")
    return Result("web 解析不到 db（不在同一网络，hosts 中没有它）", code != 0, out or f"getent 退出码 {code}")


async def _web_cannot_reach_db(c: Checks) -> Result:
    ok, out = await c.reach("web", c.addresses["db"], 5432)
    return Result("web→db 按地址不可达（不共享网络）", not ok, f"{c.addresses['db']}:5432 → {out}")


async def _udp(c: Checks) -> Result:
    token = uuid.uuid4().hex[:12]
    await c.sh("app", f"for i in 1 2 3; do echo {token} | nc -u -w 1 db 5433; done; true")
    await asyncio.sleep(1.0)
    _, out = await c.sh("db", "cat /tmp/udp.log 2>/dev/null; true")
    return Result("app→db UDP 可达", token in out, f"db 收到：{out[-80:] or '（无）'}")


async def _shared_volume(c: Checks) -> Result:
    _, out = await c.sh("app", "cat /data/from-db 2>&1; echo from-app > /data/from-app")
    _, back = await c.sh("db", "cat /data/from-app 2>&1")
    return Result(
        "app 与 db 共享 trial 卷，互相看到对方的写入", out == "from-db" and back == "from-app", f"{out} / {back}"
    )


async def _no_external(c: Checks) -> Result:
    ok_web, ev_web = await c.open_port("web", _PUBLIC)
    ok_db, ev_db = await c.open_port("db", _PUBLIC)
    return Result(
        "外部出站按策略关闭（web 按调用方策略，db 只在 internal 网络）",
        not ok_web and not ok_db,
        f"web {ev_web}；db {ev_db}",
    )


async def _exec_channel(c: Checks) -> Result:
    body = '{"command":"echo e2e-intruder"}'
    request = (
        "POST /command HTTP/1.1\\r\\nHost: x\\r\\nContent-Type: application/json\\r\\n"
        f"Content-Length: {len(body)}\\r\\nConnection: close\\r\\n\\r\\n{body}"
    )
    _, out = await c.sh("app", f"printf '{request}' | nc -w 5 {c.addresses['db']} 44772 | head -c 300; true")
    status = out.splitlines()[0] if out else "（无回应）"
    return Result(
        "app 不带凭证直连 db 的执行通道被拒（PRD N7）", "e2e-intruder" not in out and " 401 " in status, status
    )


async def _hosts(c: Checks) -> Result:
    _, out = await c.sh("web", "cat /etc/hosts")
    names = {w for line in out.splitlines() if not line.startswith("#") for w in line.split()[1:]}
    return Result("web 的 /etc/hosts 有 app、没有 db", "app" in names and "db" not in names, ", ".join(sorted(names)))


CHECKS: tuple[Callable[[Checks], Awaitable[Result]], ...] = (
    _name_chain,
    _web_cannot_resolve_db,
    _web_cannot_reach_db,
    _udp,
    _shared_volume,
    _no_external,
    _exec_channel,
    _hosts,
)


# ───────────────────────────── 运行 ─────────────────────────────


async def run(args: argparse.Namespace) -> tuple[dict[str, object], list[Result]]:
    cfg = FlotillaConfig.load(args.deployment)
    report = cfg.load_report()
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
        "deployment": report.deployment,
        "missing_required": report.missing_required(),
        "security_gaps": report.security_gaps(),
    }
    launch_id = f"e2e-{uuid.uuid4().hex[:12]}"
    trial_id = f"t{uuid.uuid4().hex[:12]}"
    anchor = Anchor(platform, clock, launch_id, cfg.anchor_settings())
    reaper: Reaper | None = None
    results: list[Result] = []
    began = time.monotonic()
    try:
        await anchor.start()
        await anchor.mkdir(f"launches/{launch_id}", stage="prepare")
        reaper = Reaper(platform, clock, anchor, Deleter(platform, clock), cfg.reaper_settings())
        reaper.track(trial_id)
        trial = Trial(platform, clock, anchor, reaper, web_app_db(args.image), trial_id, cfg.trial_settings())
        try:
            ready = await trial.start()
        except FlotillaError as exc:
            out["error"] = {"stage": exc.stage, "category": exc.category.value, "message": str(exc)}
            out["units"] = {u: h.iid for u, h in trial.handles.items()}
            return out, results
        out["ready_s"] = round(time.monotonic() - began, 1)
        out["timings"] = {k: round(v, 1) for k, v in ready.timings.items()}
        out["units"] = {u: h.iid for u, h in ready.handles.items()}
        out["recreated"] = sorted(ready.recreated)
        checks = Checks(platform, ready)
        results = [await check(checks) for check in CHECKS]
        out["checks"] = [r.__dict__ for r in results]
    finally:
        if reaper is not None:
            reaper.release(trial_id)
            out["reaper_clean"] = await reaper.close()
        else:
            await anchor.close()
        out["leftover"] = await _leftover(platform, launch_id)
        await platform.aclose()
    return out, results


async def _leftover(platform: Platform, launch_id: str) -> list[str]:
    """回收器关闭后，本 launch 还剩的实例；有的话直接删除（等平台 TTL 也会回收），并照实报告。"""
    await asyncio.sleep(platform.caps.list_visibility_s)
    left = [s.iid for s in await platform.list({labels.LAUNCH: launch_id})]
    for iid in left:
        await platform.delete(iid)
    return left


def _print(out: dict[str, object], results: list[Result]) -> None:
    print(f"部署 {out['deployment']}；能力报告缺口：必需项 {out['missing_required']}，安全类 {out['security_gaps']}")
    if "error" in out:
        print(f"trial 启动失败：{out['error']}")
    else:
        print(f"就绪用时 {out['ready_s']}s，各阶段 {out['timings']}，重建 {out['recreated']}")
        for r in results:
            print(f"  {'通过' if r.ok else '失败'}  {r.name}：{r.evidence}")
    print(f"实例 {out.get('units')}")
    print(f"回收：{'干净' if out.get('reaper_clean') else '未在时限内确认，交给 gc'}；残留 {out['leftover'] or '无'}")


def main() -> int:
    parser = argparse.ArgumentParser(
        prog="python -m tests.e2e.web_app_db", description="在一个部署上跑 web + app + db trial"
    )
    parser.add_argument("--deployment", required=True, type=Path, help="部署配置（FlotillaConfig 的 TOML）")
    parser.add_argument("--image", required=True, help="三个单元共用的镜像（repo@sha256:…）")
    parser.add_argument("--out", type=Path, help="结果写成 JSON")
    args = parser.parse_args()
    out, results = asyncio.run(run(args))
    _print(out, results)
    if args.out is not None:
        args.out.write_text(json.dumps(out, ensure_ascii=False, indent=2) + "\n")
    passed = "error" not in out and bool(results) and all(r.ok for r in results)
    return 0 if passed else 1


if __name__ == "__main__":
    sys.exit(main())
