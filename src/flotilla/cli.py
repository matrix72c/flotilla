"""The three things a deployment needs from outside the process that runs it.

`inspect` checks a dataset before anything is created, `probe` measures isolation, and `gc` cleans
up after a launch that died. Everything else a deployment needs -- building an Anchor, publishing a
share release -- is a function someone calls from their own code (`flotilla.anchor_build`,
`flotilla.share.release`), because the caller already holds the deployment spec there and does not
need a command to re-import it.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
import uuid
from collections.abc import Sequence
from pathlib import Path
from typing import Any

from flotilla.capabilities import ISOLATION_PROPERTIES, PRIVILEGED_PROPERTIES, UNMEASURED
from flotilla.config import ConfigError, FlotillaConfig
from flotilla.core.anchor import Anchor
from flotilla.core.deletion import Deleter
from flotilla.core.gc import Gc, GcReport
from flotilla.core.volumes import LAUNCHES
from flotilla.platform.base import FlotillaError
from flotilla.platform.clock import SystemClock
from flotilla.probe import probe_deployment

#: What every command takes instead of a configuration file: where to import the spec from.
DEPLOYMENT_HELP = "deployment spec to import, as package.module:attribute"


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="flotilla", description="inspect published tasks and operate a deployment's sandbox storage"
    )
    sub = parser.add_subparsers(dest="command", required=True)
    command = sub.add_parser(
        "inspect", help="plan every task of a dataset; nonzero exit means at least one would not open"
    )
    command.add_argument(
        "tasks",
        nargs="*",
        help="declared dataset names, or local task directories; omitted means every declared dataset",
    )
    command.add_argument("--deployment", required=True, help=DEPLOYMENT_HELP)
    command.add_argument("--out", type=Path, help="write the full JSON report here as well as to stdout")
    command.add_argument(
        "--json", action="store_true", help="print the JSON report instead of one line per failing task"
    )
    g = sub.add_parser("gc", help="clean up leftover launches via a temporary anchor")
    g.add_argument("--deployment", required=True, help=DEPLOYMENT_HELP)
    g.add_argument(
        "--launch", help="collect only this launch, skipping the two-observation wait (operator confirms it has exited)"
    )
    g.add_argument(
        "--staging-age",
        type=float,
        default=24 * 3600,
        help="remove share/staging entries older than this many seconds",
    )
    p = sub.add_parser("probe", help="measure a deployment's isolation; nonzero exit means it was not demonstrated")
    p.add_argument("--deployment", required=True, help=DEPLOYMENT_HELP)
    p.add_argument("--env", type=Path, help="explicit platform credential environment file")
    p.add_argument("--image", help="explicit override of [probe].image (digest-pinned)")
    p.add_argument(
        "--hosts-recheck",
        type=float,
        default=None,
        help="explicit override of [probe].hosts_recheck_s",
    )
    return parser


async def _probe(args: argparse.Namespace) -> int:
    """Measure isolation and print what held. Exit status is the verdict; nothing is written.

    A client in another process cannot re-derive these observations, so they are not persisted and
    not re-checked at startup: running this, and reading its exit status, is the gate.
    """
    cfg = FlotillaConfig.load(args.deployment)
    evidence = await probe_deployment(cfg, image=args.image, hosts_recheck=args.hosts_recheck)

    def line(name: str) -> None:
        item = getattr(evidence, name)
        print(f"{'held' if item.held else 'NOT SHOWN'}  {name}: {item.observed}")

    for name in (*ISOLATION_PROPERTIES, "cleanup"):
        line(name)
    for name in PRIVILEGED_PROPERTIES:
        if getattr(evidence, name) != UNMEASURED:
            line(name)
    print(f"egress forms exercised: {', '.join(sorted(evidence.external_forms)) or 'none'}")
    if evidence.implicit_egress:
        print(f"reached without a policy allowing it: {', '.join(evidence.implicit_egress)}")
    accepted = sorted(cfg.security.accept_insecure)
    if accepted:
        # check_isolation already refused anything missing that is not named here.
        print(f"gaps this deployment accepts running without: {', '.join(accepted)}")
    print("every isolation property held, except those accepted above")
    return 0


def _inspect(args: argparse.Namespace) -> int:
    """Plan every task and report which would not open. Creates nothing; exit status is the verdict.

    This is the cheap gate in front of a run: planning here is the same code `open` runs there, so
    a dataset of 60k tasks with 1% malformed costs minutes to find instead of GPU hours. It warms
    the image-config cache on the way, because planning is what fills it.
    """
    from flotilla.inspect import inspect_source, sources_of
    from flotilla.registry import ImageConfigs

    cfg = FlotillaConfig.load(args.deployment)
    sources = sources_of(cfg, args.tasks)
    configs = ImageConfigs(credentials=cfg.registry.credentials(), store=cfg.registry.config_cache)
    reports = [inspect_source(cfg, source, configs) for source in sources]
    failed = [report for report in reports if not report.planned]
    document: dict[str, Any] = {
        "tasks": [report.to_json() for report in reports],
        "planned": len(reports) - len(failed),
        "failed": len(failed),
    }
    output = json.dumps(document, ensure_ascii=False, indent=2, default=str)
    if args.out:
        args.out.parent.mkdir(parents=True, exist_ok=True)
        args.out.write_text(output + "\n")
    if args.json:
        print(output)
    else:
        for report in failed:
            reason = report.error or "; ".join(report.rejected[:3])
            print(f"{report.task}: {reason}")
        print(f"{len(reports) - len(failed)} of {len(reports)} tasks would open")
    return 1 if failed else 0


async def _gc(args: argparse.Namespace) -> int:
    """Clean up leftover launches.

    A one-shot process has no resident anchor, so like `publish` it starts a temporary one for directory
    operations. It does **not** create `launches/<own id>`, so it never becomes a gc candidate. `--launch`
    skips the two-observation wait (the operator confirms the launch has exited); `Gc.collect` refuses if the
    target anchor still exists.
    """
    cfg = FlotillaConfig.load(args.deployment)
    clock = SystemClock()
    platform = cfg.build_platform(clock=clock, caps=cfg.platform_capabilities())
    temporary = Anchor(platform, clock, f"gc-{uuid.uuid4().hex[:12]}", cfg.anchor_settings())
    anchor: Anchor | None = temporary
    try:
        try:
            await temporary.start()
            # `Gc.run_once` first lists `launches/`; if it is missing, `ls` exits non-zero and the round is abandoned.
            await temporary.mkdir(LAUNCHES, stage="prepare")
        except FlotillaError as exc:
            # Without an anchor nothing on shared storage can be touched, but sandboxes can still be deleted by label.
            print(f"anchor unavailable, collecting sandboxes only (directories are left): {exc}", file=sys.stderr)
            await temporary.close()
            anchor = None
        gc = Gc(platform, clock, anchor, Deleter(platform, clock), window_s=cfg.reaper.gc_window)
        if args.launch:
            try:
                done = await gc.collect(args.launch)
            except FlotillaError as exc:
                print(f"refusing to collect {args.launch}: {exc}", file=sys.stderr)
                return 1
            if anchor is None:
                state = "sandboxes removed" if done else "sandboxes not fully removed"
                print(f"{args.launch}: {state}; directory left")
                return 1
            print(f"{args.launch}: {'fully removed' if done else 'not fully removed, retry next round'}")
            return 0 if done else 1
        window = cfg.reaper.gc_window
        print(f"first observation (candidates are removed only after a second observation {window:.0f}s later)")
        first = await gc.run_once()
        _print_gc(first)
        await clock.sleep(window)
        print("second observation")
        second = await gc.run_once()
        _print_gc(second)
        if anchor is None:
            return 1  # directories were not collected
        staging = await gc.collect_staging(args.staging_age)
        print(f"  staging removed {list(staging.removed)}")
        if staging.unrecognized:
            print(f"  staging entries without a timestamp, not removed: {list(staging.unrecognized)}")
        return 1 if second.pending else 0
    finally:
        if anchor is not None:
            await anchor.close()
        await platform.aclose()


def _print_gc(report: GcReport) -> None:
    print(
        f"  [{report.mode}] alive {sorted(report.alive)}; candidates {sorted(report.candidates)}; "
        f"removed {sorted(report.removed)}; pending {sorted(report.pending)}"
    )
    if report.directories_skipped:
        print(f"  sandboxes removed, directories left: {sorted(report.directories_skipped)}")


def main(argv: Sequence[str] | None = None) -> int:
    parser = _build_parser()
    args = parser.parse_args(argv)
    if getattr(args, "env", None) is not None:
        from flotilla.environment import load_env_file

        try:
            load_env_file(args.env)
        except (OSError, ValueError) as exc:
            print(exc, file=sys.stderr)
            return 2
    if args.command == "probe":
        try:
            return asyncio.run(_probe(args))
        except ConfigError as exc:
            print(exc, file=sys.stderr)
            return 2
    if args.command == "inspect":
        try:
            return _inspect(args)
        except (ConfigError, ValueError, OSError) as exc:
            print(exc, file=sys.stderr)
            return 2
    if args.command == "gc":
        try:
            return asyncio.run(_gc(args))
        except ConfigError as exc:
            print(exc, file=sys.stderr)
            return 2
    print(f"subcommand {args.command!r} is not implemented", file=sys.stderr)
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
