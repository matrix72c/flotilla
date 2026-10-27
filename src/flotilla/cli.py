"""Operator commands for inspection, capability probes, publication, and explicit garbage collection."""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
import uuid
from collections.abc import Sequence
from pathlib import Path
from typing import Any

from flotilla.compose.task import find_tasks
from flotilla.config import ConfigError, FlotillaConfig
from flotilla.core.anchor import Anchor
from flotilla.core.deletion import Deleter
from flotilla.core.gc import Gc, GcReport
from flotilla.core.volumes import LAUNCHES
from flotilla.platform.base import FlotillaError
from flotilla.platform.clock import SystemClock
from flotilla.probe import Declared, ProbeSettings, probe
from flotilla.share.release import ReleaseError, VerifyReport, gc_releases, publish_release, verify_release


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="flotilla", description="orchestrate Compose environments as groups of sandboxes"
    )
    sub = parser.add_subparsers(dest="command", required=True)
    command = sub.add_parser("inspect", help="inspect task environments and resources")
    command.add_argument("tasks", nargs="+", type=Path)
    command.add_argument("--deployment", required=True, type=Path)
    command.add_argument("--out", type=Path)
    g = sub.add_parser("gc", help="clean up leftover launches via a temporary anchor")
    g.add_argument("--deployment", required=True, type=Path, help="deployment configuration (TOML)")
    g.add_argument(
        "--launch", help="collect only this launch, skipping the two-observation wait (operator confirms it has exited)"
    )
    p = sub.add_parser("probe", help="run contract tests against a deployment and write a capability report")
    p.add_argument("--deployment", required=True, type=Path, help="deployment configuration (TOML)")
    p.add_argument(
        "--declared", required=True, type=Path, help="deployer declarations for items probe cannot test (JSON)"
    )
    p.add_argument(
        "--image", required=True, help="image under test (repo@sha256:..., must have /bin/sh and common tools)"
    )
    p.add_argument(
        "--out",
        type=Path,
        help="output path for the capability report; defaults to the backend table's capabilities path",
    )
    p.add_argument(
        "--hosts-recheck",
        type=float,
        default=30.0,
        help="seconds to wait before re-reading /etc/hosts (use 600 for acceptance runs)",
    )
    sh = sub.add_parser("share", help="publish, verify, or gc share releases")
    share_sub = sh.add_subparsers(dest="share_command", required=True)
    sp = share_sub.add_parser("publish", help="publish a directory as an immutable share release")
    sp.add_argument("dir", type=Path, help="release content root (becomes the contents of /.flotilla)")
    sp.add_argument("--deployment", required=True, type=Path, help="deployment configuration (TOML)")
    sp.add_argument("--release", help="release name; defaults to share.release in the config")
    sp.add_argument(
        "--declared",
        type=Path,
        help="deployer declarations (JSON); bootstraps the first publish before a capability report exists",
    )
    sp.add_argument("--component", action="append", default=[], help="component version as name=version; repeatable")
    sv = share_sub.add_parser("verify", help="fully verify a release against the digest pinned in the config")
    sv.add_argument("--deployment", required=True, type=Path, help="deployment configuration (TOML)")
    sv.add_argument("--release", help="release to verify; requires --sha256")
    sv.add_argument("--sha256", help="sha256 of that release's RELEASE.json; use with --release")
    sg = share_sub.add_parser("gc", help="delete releases other than the configured one and --keep")
    sg.add_argument(
        "--deployment",
        required=True,
        type=Path,
        help="deployment configuration (TOML); its share.release is always kept",
    )
    sg.add_argument("--keep", action="append", default=[], help="additional release to keep; repeatable")
    sg.add_argument("--dry-run", action="store_true", help="list releases that would be deleted without deleting them")
    return parser


async def _probe(args: argparse.Namespace) -> int:
    cfg = FlotillaConfig.load(args.deployment)
    declared = Declared.model_validate_json(args.declared.read_bytes())
    clock = SystemClock()
    platform = cfg.build_platform(clock=clock, caps=declared.bootstrap_capabilities())
    # Temporary anchor with its own launch_id; mounts the shared root, removed on exit; only creates launches/<id>.
    anchor = Anchor(platform, clock, f"probe-{uuid.uuid4().hex[:12]}", cfg.anchor_settings())
    try:
        await anchor.start()
        await anchor.mkdir(f"launches/{anchor.launch_id}", stage="prepare")
        try:
            settings = ProbeSettings(
                image=args.image,
                share_release=cfg.share.release,
                endpoint=cfg.backend_settings.endpoint,
                hosts_recheck_s=args.hosts_recheck,
                filesystem_profile=cfg.filesystem_profile(),
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
    print(f"capability report written: {out}")
    print("required: " + ("all satisfied" if not missing else "unmet " + ", ".join(missing)))
    print("security gaps: " + ("none" if not report.security_gaps() else ", ".join(report.security_gaps())))
    return 1 if missing else 0


async def _tasks(args: argparse.Namespace) -> int:
    from dataclasses import asdict

    from flotilla import Flotilla

    runtime = Flotilla(deployment=args.deployment)
    paths = tuple(find_tasks(args.tasks))
    if not paths:
        raise ValueError("no Harbor tasks found")
    rows: list[dict[str, Any]] = []
    for path in paths:
        try:
            rows.extend({**asdict(entry), "path": str(path)} for entry in runtime.inspect(path))
        except (ValueError, OSError) as exc:
            rows.append({"path": str(path), "status": "rejected", "error": str(exc)})
    failed = any(row["status"] == "rejected" for row in rows)
    document = {"environments": rows}
    output = json.dumps(document, ensure_ascii=False, indent=2, default=str)
    if args.out:
        args.out.parent.mkdir(parents=True, exist_ok=True)
        args.out.write_text(output + "\n")
    print(output)
    return int(failed)


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
    anchor = Anchor(platform, clock, f"gc-{uuid.uuid4().hex[:12]}", cfg.anchor_settings())
    try:
        await anchor.start()
        # `Gc.run_once` first lists `launches/`; if it is missing, `ls` exits non-zero and the round is abandoned.
        await anchor.mkdir(LAUNCHES, stage="prepare")
        gc = Gc(platform, clock, anchor, Deleter(platform, clock), window_s=cfg.reaper.gc_window)
        if args.launch:
            try:
                done = await gc.collect(args.launch)
            except FlotillaError as exc:
                print(f"refusing to collect {args.launch}: {exc}", file=sys.stderr)
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
        return 1 if second.pending else 0
    finally:
        await anchor.close()
        await platform.aclose()


def _print_gc(report: GcReport) -> None:
    print(
        f"  alive {sorted(report.alive)}; candidates {sorted(report.candidates)}; "
        f"removed {sorted(report.removed)}; pending {sorted(report.pending)}"
    )


async def _share(args: argparse.Namespace) -> int:
    """publish / verify / gc for share releases, all via a temporary anchor on shared storage."""
    cfg = FlotillaConfig.load(args.deployment)
    clock = SystemClock()
    # Validate local arguments before connecting: a bad `--component` should not surface as a credential error.
    components: dict[str, str] = {}
    if args.share_command == "publish":
        for pair in args.component:
            name, sep, value = pair.partition("=")
            if not sep or not name:
                print(f"--component must be name=version: {pair!r}", file=sys.stderr)
                return 2
            components[name] = value
    if args.share_command == "verify":
        expected = args.sha256 or cfg.share.release_sha256
        if args.release and not args.sha256:
            # The config pins a different release, so its digest cannot verify this one.
            print(
                "--release requires --sha256 for that release (the config's digest belongs to another release)",
                file=sys.stderr,
            )
            return 2
        caps = cfg.platform_capabilities()
    elif args.share_command == "publish" and args.declared is not None:
        # No report exists before the first publish (probe needs a share release); bootstrap from declarations.
        caps = Declared.model_validate_json(args.declared.read_bytes()).bootstrap_capabilities()
        expected = ""
    else:
        caps = cfg.platform_capabilities()
        expected = ""
    platform = cfg.build_platform(clock=clock, caps=caps)
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
        verb = "would remove" if args.dry_run else "removed"
        print(f"kept {sorted(gc_report.kept)} (pinned release {cfg.share.release} and --keep)")
        print(f"{verb} {sorted(gc_report.would_remove if args.dry_run else gc_report.removed)}")
        if gc_report.staging:
            # staging may belong to another process assembling a release, so list it without deleting.
            print(f"share/staging entries not reclaimed (possibly in-progress publishes): {list(gc_report.staging)}")
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
        print(f"publish failed: {type(exc).__name__}: {exc}", file=sys.stderr)
        return 1
    verb = "already up to date, nothing uploaded" if result.skipped else f"uploaded {result.uploaded} files"
    print(f"{release} {verb} → {result.path}")
    print("add these two lines to [share] in the deployment config (provider checks release_sha256 at start):")
    print(f'  release = "{release}"')
    print(f'  release_sha256 = "{result.release_sha256}"')
    return 0


def _print_verify(report: VerifyReport) -> None:
    if not report.manifest_ok:
        print(f"RELEASE.json digest mismatch: actual {report.actual_sha256}, pinned {report.expected_sha256}")
        return
    print(f"{report.release}: checked {report.checked} files; " + ("all match" if report.ok else "mismatches found"))
    for label, items in (
        ("missing", report.missing),
        ("size mismatch", report.size_mismatch),
        ("content mismatch", report.digest_mismatch),
        ("mode mismatch", report.mode_mismatch),
    ):
        if items:
            print(f"  {label}: {list(items)}")


def main(argv: Sequence[str] | None = None) -> int:
    parser = _build_parser()
    args = parser.parse_args(argv)
    if args.command == "probe":
        try:
            return asyncio.run(_probe(args))
        except ConfigError as exc:
            print(exc, file=sys.stderr)
            return 2
    if args.command == "inspect":
        try:
            return asyncio.run(_tasks(args))
        except (ConfigError, ValueError, OSError, FlotillaError) as exc:
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
    print(f"subcommand {args.command!r} is not implemented", file=sys.stderr)
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
