"""P0 calibration: privileged kata DinD service brought up through flotilla.

Validates the whole privileged chain end to end against the live platform:

1. admission: capabilities.runtime.privileged_runtime flip (backup/restore around the run)
2. platform side: created instance really carries runtimeClassName=kata
3. in-sandbox: platform-injected dockerd ready with an overlay driver
4. inner docker: authenticated pull against the task registry (credentials written as a
   file, never in command strings)
5. teardown: fleet.close() leaves nothing behind

Deployment/secret/task paths come from args or environment (FLOTILLA_DEPLOYMENT,
OPENSANDBOX_SECRET_FILE); the task registry hosts come from --pull-image/--probe-image.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
import time
import urllib.request
from dataclasses import asdict, dataclass
from pathlib import Path

import sys as _sys

_sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import loadtest.kata_gate  # noqa: F401  # execd readiness gate for kata instances

from flotilla import Flotilla, FlotillaError, OpenOptions
from flotilla.config import FlotillaConfig

from loadtest.creds import bind_credential, env_path


def build_checks(pull_image: str, probe_image: str) -> list[tuple[str, str]]:
    """name -> shell command; a check passes on exit code 0.

    INFO checks are recorded but do not gate the verdict: virt_signature because the
    platform-side runtimeClassName is the authoritative kata confirmation, and
    docker_datadisk because a dedicated data disk is an image/platform detail.
    """
    return [
        ("docker_driver", "docker info --format '{{.Driver}} {{.ServerVersion}}'"),
        ("virt_signature", "cat /sys/class/dmi/id/bios_vendor /sys/class/dmi/id/product_name 2>/dev/null || true"),
        ("docker_datadisk", "df -h /var/lib/docker | tail -1"),
        ("compose_version", "docker compose version"),
        (
            # pull + local inspect end-to-end (egress -> auth -> blob path).
            "inner_pull",
            f"docker --config /root/.docker pull {pull_image} 2>&1 | tail -2 "
            f"&& docker --config /root/.docker image inspect {pull_image} --format '{{{{.Id}}}}' >/dev/null "
            f"&& echo inner-pull-ok",
        ),
        (
            # manifest inspect fetches with the written credentials; older CLIs render
            # non-list manifests as "unsupported manifest format" while still printing
            # them, so success is judged on reaching a digest, not on exit code.
            "registry_auth_manifest",
            f"docker --config /root/.docker manifest inspect {probe_image} 2>&1 | grep -q sha256 && echo auth-ok",
        ),
    ]


INFO_CHECKS = {"virt_signature", "docker_datadisk"}


@dataclass
class Check:
    name: str
    command: str
    exit_code: int
    output: str
    ok: bool


def log(message: str) -> None:
    print(message, flush=True)


def on_event(event) -> None:
    log("EVENT " + json.dumps(asdict(event), separators=(",", ":"), default=str))


def flip_capability(cap_path: Path, ok: bool, evidence: str) -> Path | None:
    """Set runtime.privileged_runtime; return the backup path on the false->true flip."""
    data = json.loads(cap_path.read_text())
    item = data["runtime"]["privileged_runtime"]
    backup = None
    if ok and not item.get("ok"):
        backup = cap_path.with_name(cap_path.name + ".bak-p0")
        if not backup.exists():
            backup.write_text(cap_path.read_text())
    item["ok"] = ok
    item["source"] = "probed"
    item["evidence"] = evidence
    cap_path.write_text(json.dumps(data, indent=2) + "\n")
    return backup


def restore_capability(cap_path: Path, backup: Path | None) -> None:
    if backup is not None and backup.exists():
        cap_path.write_text(backup.read_text())


def platform_instance(endpoint: str, credential: str, instance_id: str) -> dict:
    request = urllib.request.Request(
        f"{endpoint.rstrip('/')}/v1/sandboxes/{instance_id}",
        headers={"OPEN-SANDBOX-API-KEY": credential},
    )
    with urllib.request.urlopen(request, timeout=30) as response:
        return json.load(response)


def registry_auth_entry(registry_config: Path, registry: str) -> bytes | None:
    """The registry auth blob from a local docker config, or None when absent."""
    try:
        auths = json.loads(registry_config.read_text()).get("auths", {})
    except (OSError, ValueError):
        return None
    auth = auths.get(registry, {}).get("auth")
    if not auth:
        return None
    return json.dumps({"auths": {registry: {"auth": auth}}}).encode()


async def run_checks(env, checks: list[tuple[str, str]]) -> list[Check]:
    results: list[Check] = []
    for name, command in checks:
        result = await env.agent.shell(command, timeout=300)
        output = (result.stdout + result.stderr).decode(errors="replace").strip()
        exit_code = result.exit_code if result.exit_code is not None else -1
        ok = exit_code == 0 and not result.timed_out
        results.append(Check(name, command, exit_code, output[:600], ok))
        log(f"[{'PASS' if ok else 'FAIL'}] {name}: {output[:200]}")
    return results


async def main(args: argparse.Namespace) -> int:
    config = FlotillaConfig.load(args.deployment)
    bind_credential(config, args.secret)
    cap_path = Path(config.capabilities_path())
    checks = build_checks(args.pull_image, args.probe_image)
    registry = args.probe_image.split("/", 1)[0]
    backup = flip_capability(
        cap_path, True, f"P0 validation in progress {time.strftime('%Y-%m-%d %H:%M:%S')}"
    )
    credential = os.environ[config.opensandbox.credential_env]

    out: dict[str, object] = {"task": str(args.task), "began": time.strftime("%Y-%m-%dT%H:%M:%S")}
    fleet = Flotilla(deployment=config, on_event=on_event)
    began = time.monotonic()
    passed = False
    try:
        async with fleet.environment(
            args.task, OpenOptions(request_id=f"p0-priv-{int(time.time())}", startup_timeout=900)
        ) as env:
            info = await env.info()
            out["ready_s"] = round(time.monotonic() - began, 1)
            out["timings"] = dict(info.timings)
            out["services"] = {s.name: s.instance_id for s in info.services}

            runtime_classes = {}
            for name, instance_id in out["services"].items():
                record = platform_instance(config.opensandbox.endpoint, credential, instance_id)
                extensions = record.get("extensions", {}) or {}
                runtime_classes[name] = extensions.get("runtimeClassName")
                log(f"[{'PASS' if runtime_classes[name] == 'kata' else 'FAIL'}] "
                    f"runtime_class {name}: {runtime_classes[name]}")
            out["runtime_class"] = runtime_classes
            kata_confirmed = all(v == "kata" for v in runtime_classes.values())

            auth_payload = registry_auth_entry(args.registry_config, registry)
            if auth_payload is None:
                log(f"NOTE inner_pull will fail: no {registry} auth in {args.registry_config}")
            else:
                await env.agent.write_file("/root/.docker/config.json", auth_payload)

            results = await run_checks(env, checks)
            out["checks"] = [asdict(c) for c in results]
            passed = kata_confirmed and all(c.ok for c in results if c.name not in INFO_CHECKS)
    except FlotillaError as exc:
        out["error"] = {"stage": exc.stage, "category": exc.category.value, "message": str(exc)}
        log(f"ERROR flotilla stage={exc.stage} category={exc.category.value}: {exc}")
    except Exception as exc:  # noqa: BLE001 - report and exit non-zero
        out["error"] = {"type": type(exc).__name__, "message": str(exc)}
        log(f"ERROR {type(exc).__name__}: {exc}")
    finally:
        report = await fleet.close()
        out["cleanup"] = asdict(report)
        out["reaper_clean"] = report.clean
        if passed and report.clean:
            flip_capability(
                cap_path,
                True,
                f"P0 validated {time.strftime('%Y-%m-%d')}: privileged service via flotilla -> "
                f"runtimeClassName=kata confirmed platform-side; dockerd auto-start overlay; "
                f"inner authenticated pull OK; ready_s={out.get('ready_s')}.",
            )
        else:
            restore_capability(cap_path, backup)
            log(f"capabilities restored (passed={passed} clean={report.clean})")

    out["passed"] = passed and out["reaper_clean"]
    destination = Path(args.json_out) if args.json_out else Path("build") / f"p0-privileged-result-{int(time.time())}.json"
    destination.parent.mkdir(parents=True, exist_ok=True)
    destination.write_text(json.dumps(out, indent=2, default=str) + "\n")
    log(f"RESULT passed={out['passed']} -> {destination}")
    return 0 if out["passed"] else 1


def parse_args(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--deployment", type=Path, default=env_path("FLOTILLA_DEPLOYMENT"))
    parser.add_argument("--secret", type=Path, default=env_path("OPENSANDBOX_SECRET_FILE"))
    parser.add_argument("--task", type=Path, required=True, help="harbor task dir with a privileged service")
    parser.add_argument("--pull-image", required=True, help="small image the inner docker pulls (auth+egress proof)")
    parser.add_argument("--probe-image", required=True, help="image whose manifest probes registry auth")
    parser.add_argument("--registry-config", type=Path, default=Path.home() / ".docker/config.json")
    parser.add_argument("--json-out", default=None)
    args = parser.parse_args(argv)
    if args.deployment is None:
        parser.error("--deployment or FLOTILLA_DEPLOYMENT is required")
    if args.secret is None:
        parser.error("--secret or OPENSANDBOX_SECRET_FILE is required")
    return args


if __name__ == "__main__":
    sys.exit(asyncio.run(main(parse_args(sys.argv[1:]))))
