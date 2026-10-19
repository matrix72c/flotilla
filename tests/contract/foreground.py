"""Opt-in real backend contract. Creates 1 or 3 x (0.5 CPU, 512 MiB) and one anchor.

Only this process's new launch is released. Credentials come from the deployment's
credential_env. A non-clean close is a failing result with owned resource IDs.
"""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import json
import time
import uuid
from dataclasses import asdict
from pathlib import Path
from typing import Any

from flotilla import Flotilla
from flotilla.config import FlotillaConfig, Network
from flotilla.core import labels
from flotilla.core.anchor import Anchor
from flotilla.core.deletion import Deleter
from flotilla.manifest import Context, Manifest, Resources, Unit
from flotilla.manifest import Network as ManifestNetwork
from flotilla.platform.base import ErrorCategory, FlotillaError
from flotilla.platform.clock import SystemClock
from flotilla.platform.opensandbox import build
from flotilla.runtime.types import Task
from tests.support.environments import FixtureRequest as RunRequest
from tests.support.environments import open_fixture


async def run(deployment: Path, image: str, output: Path, *, services: int = 3, soak_seconds: int = 0) -> bool:
    if services not in (1, 3):
        raise ValueError("services must be 1 or 3")
    if not 0 <= soak_seconds <= 300:
        raise ValueError("soak_seconds must be between 0 and 300")
    names = ("main", "worker-a", "worker-b") if services == 3 else ("main",)
    cfg = FlotillaConfig.load(deployment).model_copy(update={"network": Network()})
    task = Task(
        Manifest(
            task="foreground-contract",
            build_key="c" * 64,
            agent_service="main",
            units={
                name: Unit(
                    command=None,
                    image=image,
                    resources=Resources(cpu="0.5", memory="512Mi"),
                    context=Context(env={"PATH": "/bin:/usr/bin"}, uid=0, gid=0, cwd="/"),
                )
                for name in names
            },
            networks={
                "default": ManifestNetwork(
                    internal=True,
                    members=list(names),
                    names={n: [n] for n in names},
                )
            },
        )
    )
    result: dict[str, Any] = {
        "protocol": cfg.opensandbox.execd.protocol,
        "service_count": services,
        "checks": [],
        "skipped": []
        if services == 3
        else ["peer_connection", "peer_payload", "sidecar_usable_after_main_termination"],
        "clean": False,
    }
    output.parent.mkdir(parents=True, exist_ok=True)

    def save() -> None:
        output.write_text(json.dumps(result, indent=2) + "\n")

    def check(name: str, passed: bool) -> None:
        result["checks"].append({"name": name, "passed": passed})
        save()
        print(f"{name}: {passed}", flush=True)
        if not passed:
            raise AssertionError(name)

    observer = build(
        endpoint=cfg.opensandbox.endpoint,
        credential=cfg.credential(),
        clock=SystemClock(),
        settings=cfg.network_settings(),
        storage=cfg.storage_settings(),
        caps=cfg.load_report().to_capabilities(),
        execd=cfg.execd_settings(),
        extensions=cfg.opensandbox.extensions,
        create_fields=cfg.opensandbox.create_fields,
        group_extension=cfg.opensandbox.group_extension,
    )
    fleet = Flotilla(deployment=cfg)
    pending: asyncio.Task[Any] | None = None
    try:
        print("preparing_environment", flush=True)
        env = await open_fixture(fleet, RunRequest(task, "real-foreground-contract", timeout=600))
        result["environment"] = asdict(await env.info())
        save()
        check("services_ready", len(result["environment"]["services"]) == services)
        check("nonzero_is_result", (await env.agent.shell("exit 7")).exit_code == 7)
        w = "/.flotilla/bin/busybox"
        payload = bytes(range(256)) * 16 + b"\r\n--flotilla-boundary\r\n"
        await env.agent.write_file("/tmp/flotilla-transfer", payload)
        check("binary_transfer", await env.agent.read_file("/tmp/flotilla-transfer") == payload)
        process = await env.service("worker-b" if services == 3 else "main").start_process([w, "sh", "-c", "exit 7"])
        async with asyncio.timeout(15):
            while (status := await process.status()).running:
                await asyncio.sleep(0.1)
        check("background_exit", status.found and status.exit_code == 7)
        if services == 3:
            listener = await env.service("worker-a").start_process(
                [w, "sh", "-c", f"printf peer-reply | {w} nc -w 20 -l -p 19080 > /tmp/flotilla-peer-message"]
            )
            async with asyncio.timeout(10):
                while True:
                    ready = await env.service("worker-a").exec(
                        [
                            w,
                            "awk",
                            '$2 ~ /:4A88$/ && $4 == "0A" {found=1} END {exit !found}',
                            "/proc/net/tcp",
                            "/proc/net/tcp6",
                        ]
                    )
                    if ready.exit_code == 0:
                        break
                    state = await listener.status()
                    if not state.running:
                        result["peer_listener_status"] = asdict(state)
                        raise AssertionError("peer listener exited before accepting connections")
                    await asyncio.sleep(0.1)
            check("peer_listener_ready", True)
            sent = await env.service("worker-b").shell(f"printf peer-message | {w} nc -w 2 worker-a 19080")
            result["peer_client"] = {
                "exit_code": sent.exit_code,
                "stderr": sent.stderr.decode(errors="replace")[:500],
            }
            result["peer_listener_status"] = asdict(await listener.status())
            result["peer_received_bytes"] = len(await env.service("worker-a").read_file("/tmp/flotilla-peer-message"))
            # BusyBox nc can exit nonzero on its idle deadline after exchanging
            # data. Require a reply and an exact received payload instead.
            check("peer_connection", sent.stdout.strip() == b"peer-reply")
            async with asyncio.timeout(30):
                while (await listener.status()).running:
                    await asyncio.sleep(0.1)
            check(
                "peer_payload", await env.service("worker-a").read_file("/tmp/flotilla-peer-message") == b"peer-message"
            )
        child = (
            f'trap "" TERM; {w} touch /tmp/flotilla-child-started; {w} sleep 8; {w} touch /tmp/flotilla-cancel-marker'
        )
        pending = asyncio.create_task(env.agent.shell(f"{w} sh -c '{child}' & wait"))
        async with asyncio.timeout(30):
            while True:
                try:
                    await env.agent.read_file("/tmp/flotilla-child-started")
                    break
                except FlotillaError as error:
                    if error.category is not ErrorCategory.NOT_FOUND:
                        raise
                if pending.done():
                    completed = await pending
                    raise AssertionError(f"cancellation fixture exited before readiness: {completed.exit_code}")
                await asyncio.sleep(0.1)
        check("independent_child_started", True)
        began = time.monotonic()
        pending.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await pending
        async with asyncio.timeout(cfg.runtime.cancel_confirm_timeout_s + 5):
            while True:
                try:
                    await env.check()
                    break
                except FlotillaError as error:
                    if "being confirmed" not in str(error):
                        raise
                    await asyncio.sleep(0.1)
        result["cancel_confirm_s"] = time.monotonic() - began
        check("environment_usable_after_cancel", (await env.agent.exec([w, "true"])).exit_code == 0)
        timed = await env.agent.exec([w, "sleep", "8"], timeout=1)
        check("local_timeout_confirmed", timed.timed_out)
        await asyncio.sleep(8)
        check(
            "cancelled_child_did_not_write",
            (await env.agent.exec([w, "test", "!", "-e", "/tmp/flotilla-cancel-marker"])).exit_code == 0,
        )
        if soak_seconds:
            instance_id = result["environment"]["services"][0]["instance_id"]
            before = await observer.get(instance_id)
            anchors = await observer.list(labels.anchor_labels(before.labels[labels.LAUNCH]))
            check("soak_anchor_identified", len(anchors) == 1)
            anchor_before = anchors[0]
            identity = await env.agent.exec([w, "id", "-u"], uid=12345, gid=23456)
            check("numeric_exec_identity", identity.exit_code == 0 and identity.stdout.strip() == b"12345")
            until = time.monotonic() + soak_seconds
            result["soak_seconds"] = soak_seconds
            result["soak_batches"] = 0
            while time.monotonic() < until:
                batch = await asyncio.gather(*(env.agent.exec([w, "true"]) for _ in range(4)))
                if any(item.exit_code != 0 or item.timed_out for item in batch):
                    raise AssertionError("concurrent execution failed during soak")
                await env.check()
                await env.agent.write_file("/tmp/flotilla-soak", payload)
                if await env.agent.read_file("/tmp/flotilla-soak") != payload:
                    raise AssertionError("binary transfer failed during soak")
                result["soak_batches"] += 1
                save()
                await asyncio.sleep(min(5, max(0, until - time.monotonic())))
            after = await observer.get(instance_id)
            anchor_after = await observer.get(anchor_before.iid)
            check("concurrent_execution_soak", result["soak_batches"] > 0)
            check(
                "service_renewal_observed",
                before.expires_at is not None and after.expires_at is not None and after.expires_at > before.expires_at,
            )
            check(
                "anchor_renewal_observed",
                anchor_before.expires_at is not None
                and anchor_after.expires_at is not None
                and anchor_after.expires_at > anchor_before.expires_at,
            )
        await asyncio.gather(env.agent.terminate(), env.agent.terminate())
        await env.check()
        if services == 3:
            check(
                "sidecar_usable_after_main_termination",
                (await env.service("worker-a").exec([w, "true"])).exit_code == 0,
            )
        try:
            await env.agent.exec([w, "true"])
        except FlotillaError:
            check("terminated_main_rejects_execution", True)
        else:
            check("terminated_main_rejects_execution", False)
        check("environment_release_confirmed", (await env.release()).clean)
    except Exception as error:
        # Do not serialize arbitrary request/response bodies or credentials.
        result["error_type"] = type(error).__name__
        save()
        print(f"contract failed: {type(error).__name__}", flush=True)
    finally:
        if pending is not None:
            pending.cancel()
            with contextlib.suppress(asyncio.CancelledError, FlotillaError):
                await pending
        cleanup = await fleet.close(timeout=90)
        result["cleanup"] = asdict(cleanup)
        result["clean"] = cleanup.clean
        try:
            remaining = await observer.list({labels.LAUNCH: cleanup.launch_id})
            result["independent_instances_absent"] = not remaining
            audit = Anchor(observer, SystemClock(), "audit-" + uuid.uuid4().hex[:16], cfg.anchor_settings())
            try:
                await audit.start()
                result["independent_directory_absent"] = not await audit.exists(
                    f"launches/{cleanup.launch_id}", stage="run"
                )
                if not result["independent_directory_absent"]:
                    result["clean"] = False
            finally:
                await audit.close()
                result["audit_anchor_absent"] = audit.iid is None or await Deleter(
                    observer, SystemClock(), retry_deadline_s=30
                ).delete(audit.iid)
                if not result["audit_anchor_absent"]:
                    result["clean"] = False
            if remaining:
                result["clean"] = False
        except Exception as error:
            result["clean"] = False
            result["independent_cleanup_error_type"] = type(error).__name__
        finally:
            await observer.aclose()
            save()
    return result["clean"] and "error_type" not in result


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--deployment", type=Path, required=True)
    parser.add_argument("--image", required=True)
    parser.add_argument("--out", type=Path, required=True)
    parser.add_argument(
        "--services", type=int, choices=(1, 3), default=3, help="single-service deployments explicitly skip peer checks"
    )
    parser.add_argument(
        "--soak-seconds", type=int, default=0, help="bounded concurrent execution/renewal check; maximum 300"
    )
    args = parser.parse_args()
    raise SystemExit(
        0
        if asyncio.run(
            run(args.deployment, args.image, args.out, services=args.services, soak_seconds=args.soak_seconds)
        )
        else 1
    )


if __name__ == "__main__":
    main()
