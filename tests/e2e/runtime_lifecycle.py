"""Live public-runtime acceptance with independent remote cleanup verification.

Requires a deployed share, accepted capability report, credential environment variable,
plus a digest-pinned images/probe image. This tests lifecycle, not dataset conversion.
Creates CPU instances (0.5 CPU / 512Mi each) and only cleans its own launch IDs.

    python -m tests.e2e.runtime_lifecycle --deployment <config> --image <digest> --out <directory>
"""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import time
import uuid
from contextlib import suppress
from dataclasses import asdict, replace
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from flotilla import Flotilla
from flotilla.config import FlotillaConfig
from flotilla.core import labels
from flotilla.core.anchor import Anchor
from flotilla.core.deletion import Deleter
from flotilla.manifest import Manifest
from flotilla.platform.base import ErrorCategory, FlotillaError
from flotilla.platform.clock import SystemClock
from flotilla.platform.opensandbox import build
from flotilla.runtime.types import Task
from flotilla.testing.environments import FixtureRequest as RunRequest
from flotilla.testing.environments import open_fixture
from tests.e2e.web_app_db import web_app_db


def fixture(image: str, *, single: bool = False, waiting: bool = False) -> Task:
    data = asdict(web_app_db(image))
    data["trial_volumes"] = data.pop("volumes")
    data.update(agent_service="web", build_key=hashlib.sha256(image.encode()).hexdigest())
    if single:
        data.update(
            units={"web": data["units"]["web"]}, networks={}, depends_on={}, trial_volumes=[], external_units=[]
        )
    if waiting:
        data["units"]["web"]["healthcheck"] = {
            "test": ["/bin/sh", "-c", "exit 1"],
            "interval_s": 2,
            "timeout_s": 5,
            "retries": 300,
            "start_period_s": 600,
            "start_interval_s": 2,
        }
    if single:
        data["trial_volumes"] = [{"key": "data"}]
        data["units"]["web"]["mounts"] = [{"scope": "trial", "key": "data", "target": "/data", "read_only": False}]
    manifest = Manifest.model_validate(json.loads(json.dumps(data, default=sorted)))
    return Task(manifest)


async def run(args: argparse.Namespace) -> dict[str, Any]:
    args.out.mkdir(parents=True, exist_ok=False)
    cfg = FlotillaConfig.load(args.deployment)
    # Test-only TTL: force renewal in minutes without modifying the deployment file.
    cfg = cfg.model_copy(update={"reaper": cfg.reaper.model_copy(update={"trial_ttl": 180})})
    cfg.check_against(cfg.load_report())
    clock = SystemClock()
    observer = build(
        endpoint=cfg.opensandbox.endpoint,
        credential=cfg.credential(),
        clock=clock,
        settings=cfg.network_settings(),
        storage=cfg.storage_settings(),
        caps=cfg.load_report().to_capabilities(),
        execd=cfg.execd_settings(),
        extensions=cfg.opensandbox.extensions,
        create_fields=cfg.opensandbox.create_fields,
        group_extension=cfg.opensandbox.group_extension,
        privileged_extensions=cfg.opensandbox.privileged_extensions,
    )
    # The observer shares the deliberately blocked caller loop, so its grace must
    # exceed that pause. The runtime under test retains the deployment's 90s grace.
    audit_settings = cfg.anchor_settings()
    audit_grace = max(300, audit_settings.grace_s)
    audit = Anchor(
        observer,
        clock,
        f"audit-{uuid.uuid4().hex[:16]}",
        replace(audit_settings, grace_s=audit_grace, ttl_s=max(audit_settings.ttl_s, int(audit_grace) + 300)),
    )
    fleet = Flotilla(deployment=cfg)
    out: dict[str, Any] = {"started_at": datetime.now(UTC).isoformat(), "trial_ttl": 180, "checks": [], "instances": {}}
    launch: str | None = None
    pending: asyncio.Task[Any] | None = None

    def save() -> None:
        (args.out / "report.json").write_text(json.dumps(out, ensure_ascii=False, indent=2, default=str) + "\n")

    def record(name: str, ok: bool, **evidence: Any) -> None:
        out["checks"].append({"name": name, "ok": ok, **evidence})
        save()
        print(json.dumps({"check": name, "ok": ok, **evidence}, ensure_ascii=False, default=str), flush=True)
        if not ok:
            raise AssertionError(name)

    async def remember() -> list[Any]:
        assert launch is not None
        states = await observer.list({labels.LAUNCH: launch})
        for state in states:
            out["instances"][state.iid] = dict(state.labels)
        save()
        return states

    async def absent(ids: list[str]) -> bool:
        for iid in ids:
            try:
                await observer.get(iid)
            except FlotillaError as exc:
                if exc.category is ErrorCategory.NOT_FOUND:
                    continue
                raise
            return False
        return True

    async def drained(name: str, eid: str | None = None) -> None:
        report = await fleet.drain(timeout=100)
        states = await remember()
        directory_absent = eid is None or not await audit.exists(f"launches/{launch}/{eid}", stage="prepare")
        record(
            name,
            report.clean and all(s.labels.get(labels.ROLE) == labels.ROLE_ANCHOR for s in states) and directory_absent,
            cleanup=asdict(report),
            trial_directory_absent=directory_absent,
        )

    try:
        await audit.start()
        out["audit_instance"] = audit.iid
        out["audit_launch"] = audit.launch_id
        save()
        await fleet.start()
        assert fleet._manager is not None  # Read identity only; all lifecycle operations use public API.
        launch = fleet._manager.launch_id
        out["launch_id"] = launch
        await remember()
        task = fixture(args.image, single=not args.multi_service)
        (args.out / "flotilla.manifest.json").write_text(task.manifest().dump_json())
        began = time.monotonic()
        env = await open_fixture(fleet, RunRequest(task, "live-services", timeout=600))
        info = await env.info()
        await remember()
        record(
            "services_ready",
            len(info.services) == (3 if args.multi_service else 1),
            elapsed_s=round(time.monotonic() - began, 2),
            info=asdict(info),
        )
        if args.multi_service:
            result = await env.agent.shell("echo q | nc -w 5 app 8080", timeout=15)
            record(
                "service_name_chain",
                result.exit_code == 0 and b"app-pong via db-pong" in result.stdout,
                stdout=result.stdout.decode(),
            )
        else:
            out["not_tested"] = ["multi-service networking; enable --multi-service on a link-capable deployment"]
        data = b"flotilla-live-file\x00\xff\n"
        writer = env.service("app") if args.multi_service else env.agent
        reader = env.service("db") if args.multi_service else env.agent
        await writer.write_file("/data/roundtrip", data, mode=0o600)
        record("trial_volume_binary_roundtrip", await reader.read_file("/data/roundtrip") == data)
        source = args.out / "upload"
        (source / "nested").mkdir(parents=True)
        (source / "nested" / "hello.txt").write_text("directory roundtrip\n")
        await env.agent.upload_dir(source, "/tmp/lifecycle-upload")
        await env.agent.download_dir("/tmp/lifecycle-upload", args.out / "download")
        record(
            "directory_roundtrip",
            (args.out / "download/nested/hello.txt").read_bytes() == (source / "nested/hello.txt").read_bytes(),
        )
        result = await env.agent.shell("exit 7")
        record("command_nonzero_is_result", result.exit_code == 7 and not result.timed_out)
        result = await env.agent.shell("sleep 30", timeout=2)
        record("command_timeout_is_result", result.timed_out)
        process = await env.agent.start_process(["/bin/sh", "-c", "sleep 3; exit 7"])
        async with asyncio.timeout(30):
            while (status := await process.status()).running:
                await asyncio.sleep(1)
        record("background_process_exit", status.found and status.exit_code == 7, status=asdict(status))
        before = {s.iid: s.expires_at for s in await remember()}
        record("expiry_observable", all(value is not None for value in before.values()))
        # Deliberately block the caller event loop; the worker must still renew every service and its anchor.
        for _ in range(3):
            print("Blocking caller loop for 40s; runtime renewal remains on its owned loop.", flush=True)
            time.sleep(40)
        after = {s.iid: s.expires_at for s in await remember()}
        record(
            "renewal_while_caller_blocked",
            before.keys() == after.keys() and all(after[iid] > expiry for iid, expiry in before.items()),
            before=before,
            after=after,
        )
        await env.check()
        await env.release()
        await env.release()
        await drained("release_idempotent_and_reaped", env.id)

        pending = asyncio.create_task(
            open_fixture(
                fleet,
                RunRequest(
                    fixture(args.image, single=not args.multi_service, waiting=True), "cancel-during-start", timeout=300
                ),
            )
        )
        async with asyncio.timeout(120):
            while True:
                states = await remember()
                units = [s for s in states if s.labels.get(labels.ROLE) == labels.ROLE_UNIT]
                if len(units) >= (3 if args.multi_service else 1):
                    break
                if pending.done():
                    await pending
                    raise AssertionError("waiting fixture unexpectedly ready")
                await asyncio.sleep(1)
        pending.cancel()
        with suppress(asyncio.CancelledError):
            await pending
        record("cancel_after_remote_creation", pending.cancelled(), instances=[s.iid for s in units])
        await drained("cancelled_start_reaped", units[0].labels[labels.TRIAL])
        pending = None

        began = time.monotonic()
        expiring = await open_fixture(
            fleet, RunRequest(fixture(args.image, single=not args.multi_service), "deadline", timeout=80)
        )
        expiry_info = await expiring.info()
        await remember()
        await asyncio.sleep(max(0, 82 - (time.monotonic() - began)))
        try:
            await expiring.check()
        except FlotillaError as exc:
            record("total_deadline_rejects_use", exc.category is ErrorCategory.LOST, category=exc.category.value)
        else:
            record("total_deadline_rejects_use", False)
        await drained("deadline_reaped", expiring.id)
        record("deadline_instances_not_found", await absent([s.instance_id for s in expiry_info.services]))

        lost = await open_fixture(
            fleet, RunRequest(fixture(args.image, single=not args.multi_service), "infrastructure-loss", timeout=180)
        )
        lost_info = await lost.info()
        await remember()
        iid = lost_info.services[0].instance_id
        deleted = await Deleter(observer, clock, retry_deadline_s=60).delete(iid)
        record("fault_injection_deleted_owned_instance", deleted, instance=iid)
        try:
            await lost.check()
        except FlotillaError as exc:
            record("lost_instance_reported", exc.category is ErrorCategory.LOST, category=exc.category.value)
        else:
            record("lost_instance_reported", False)
        await lost.release()
        await drained("lost_environment_reaped", lost.id)
    except Exception as exc:
        out["error"] = {"type": type(exc).__name__, "message": str(exc)}
        print(json.dumps(out["error"], ensure_ascii=False), flush=True)
    finally:
        if launch is None and fleet._manager is not None:
            launch = fleet._manager.launch_id
            out["launch_id"] = launch
        if pending is not None and not pending.done():
            pending.cancel()
            with suppress(asyncio.CancelledError):
                await pending
        try:
            report = await fleet.close(timeout=120)
            out["cleanup"] = asdict(report)
            record("fleet_close_clean", report.clean)
            record("fleet_close_idempotent", await fleet.close(timeout=120) == report)
            if launch:
                await asyncio.sleep(observer.caps.list_visibility_s)
                states = await remember()
                record(
                    "independent_no_instances",
                    not states and await absent(list(out["instances"])),
                    remaining=[s.iid for s in states],
                )
                record("independent_no_launch_directory", not await audit.exists(f"launches/{launch}", stage="prepare"))
        except Exception as exc:
            out["cleanup_error"] = {"type": type(exc).__name__, "message": str(exc)}
        finally:
            # Emergency cleanup is scoped to this invocation and never upgrades a failed runtime check to a pass.
            try:
                if launch:
                    scope = Deleter(observer, clock, retry_deadline_s=60).scope()
                    try:
                        async with asyncio.timeout(120):
                            out["fallback_instances_clean"] = await scope.sweep(
                                {labels.LAUNCH: launch}, until=clock.monotonic() + observer.caps.list_visibility_s
                            )
                        if await audit.exists(f"launches/{launch}", stage="prepare"):
                            await audit.remove(f"launches/{launch}", stage="prepare")
                    finally:
                        scope.cancel()
            except Exception as exc:
                out["fallback_error"] = {"type": type(exc).__name__, "message": str(exc)}
            finally:
                await audit.close()
                scope = Deleter(observer, clock, retry_deadline_s=60).scope()
                try:
                    async with asyncio.timeout(120):
                        out["audit_cleanup"] = await scope.sweep(
                            labels.anchor_labels(audit.launch_id),
                            until=clock.monotonic() + observer.caps.list_visibility_s,
                        )
                finally:
                    scope.cancel()
                    await observer.aclose()
                    out["finished_at"] = datetime.now(UTC).isoformat()
                    save()
    return out


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--deployment", type=Path, required=True)
    parser.add_argument("--image", required=True)
    parser.add_argument("--multi-service", action="store_true", help="Test web/app/db networking; requires link")
    parser.add_argument("--out", type=Path, required=True, help="New directory for fixture and incremental report")
    args = parser.parse_args()
    result = asyncio.run(run(args))
    ok = (
        not any(key in result for key in ("error", "cleanup_error", "fallback_error"))
        and result.get("audit_cleanup")
        and result.get("fallback_instances_clean")
        and all(c["ok"] for c in result["checks"])
    )
    print(f"Lifecycle acceptance: {'PASS' if ok else 'FAIL'}; report: {args.out / 'report.json'}", flush=True)
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
