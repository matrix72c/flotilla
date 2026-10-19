"""End-to-end task-path preparation, service operations and confirmed cleanup.

Run with --deployment deployment.toml --task /path/to/harbor/task --check true.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
import time
from dataclasses import asdict, dataclass
from pathlib import Path

from flotilla import Environment, Flotilla, OpenOptions
from flotilla.platform.base import FlotillaError


async def run(args: argparse.Namespace) -> tuple[dict[str, object], list[Check]]:
    fleet = Flotilla(deployment=args.deployment)
    out: dict[str, object] = {"task": str(args.task)}
    checks: list[Check] = []
    began = time.monotonic()
    try:
        async with fleet.environment(
            args.task, OpenOptions(request_id="task-e2e", startup_timeout=args.timeout)
        ) as env:
            info = await env.info()
            out["ready_s"] = round(time.monotonic() - began, 1)
            out["timings"] = dict(info.timings)
            out["security_gaps"] = info.security_gaps
            out["accepted_security_gaps"] = info.accepted_security_gaps
            out["created"] = {service.name: service.instance_id for service in info.services}
            checks = await _checks(env, args.check)
            await env.check()
            out["checks"] = [asdict(check) for check in checks]
    except FlotillaError as exc:
        out["error"] = {"stage": exc.stage, "category": exc.category.value, "message": str(exc)}
    except Exception as exc:
        out["error"] = {"type": type(exc).__name__, "message": str(exc)}
    finally:
        report = await fleet.close()
        out["cleanup"] = asdict(report)
        out["reaper_clean"] = report.clean
        out["leftover"] = [*(iid for record in report.remaining for iid in record.instances), *report.anchor_instances]
    return out, checks


@dataclass(frozen=True)
class Check:
    unit: str
    command: str
    exit_code: int
    output: str


async def _checks(env: Environment, commands: list[str]) -> list[Check]:
    info = await env.info()
    results = []
    for raw in commands:
        name, command = _split(raw, info.agent_service, {service.name for service in info.services})
        result = await env.service(name).shell(command, timeout=120)
        output = (result.stdout + result.stderr).decode(errors="replace").strip()
        results.append(
            Check(
                name,
                command,
                result.exit_code if result.exit_code is not None and not result.timed_out else 124,
                output[:400],
            )
        )
    return results


def _split(raw: str, default_unit: str, units: set[str]) -> tuple[str, str]:
    head, sep, rest = raw.partition(":")
    if sep and head in units:
        return head, rest
    if sep and head and not head.startswith((" ", "/", ".")) and " " not in head and head not in units:
        raise SystemExit(f"--check unit {head!r} is not in the task: {sorted(units)}")
    return default_unit, raw


def _print(out: dict[str, object], checks: list[Check]) -> None:
    print(f"task {out['task']}")
    if "error" in out:
        print(f"startup failed: {out['error']}")
    else:
        print(f"ready in {out['ready_s']}s, stages {out['timings']}")
        for check in checks:
            status = "ok" if check.exit_code == 0 else f"exit code {check.exit_code}"
            print(f"  [{status}] {check.unit}: {check.command}\n      {check.output}")
    print(f"instances {out.get('created')}")
    print(
        f"cleanup: {'clean' if out.get('reaper_clean') else 'not confirmed in time, left to gc'}; "
        f"leftover {out['leftover'] or 'none'}"
    )


def main() -> int:
    parser = argparse.ArgumentParser(
        prog="python -m tests.e2e.task_trial",
        description="Deliver an environment directly from a Harbor task directory",
    )
    parser.add_argument("--deployment", required=True, type=Path, help="deployment config")
    parser.add_argument("--task", required=True, type=Path, help="Harbor task directory")
    parser.add_argument(
        "--check", action="append", default=[], help="command to run in the agent service once ready; repeatable"
    )
    parser.add_argument("--out", type=Path, help="write results as JSON")
    parser.add_argument("--timeout", type=float, default=3600, help="time limit for preparation, queueing and startup")
    args = parser.parse_args()
    out, checks = asyncio.run(run(args))
    _print(out, checks)
    if args.out is not None:
        args.out.write_text(json.dumps(out, ensure_ascii=False, indent=2) + "\n")
    failed = "error" in out or any(c.exit_code != 0 for c in checks) or not out["reaper_clean"]
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
