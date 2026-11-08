"""Opt-in *live* Docker Compose vs HarborAttempt comparison of a narrow supported subset.

Requires an admitted deployment, installed official Compose, a Docker daemon, and
one immutable Python 3 image accessible to both runtimes. No tooling is installed.
Only startup argv/environment and bind observations below are compared, not Docker
PID 1, signals, networking, resource limits, or general container equivalence.

--local-root must already exist at the SAME absolute path in this process and
on the Docker daemon host. A driver container can bind it at that exact path;
never assume /workspace exists on the daemon host. Sources are fresh and owned;
Docker resources are cleaned using the unique Compose project label.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import shutil
import tempfile
import traceback
import uuid
from collections.abc import Awaitable, Callable, Mapping, Sequence
from pathlib import Path
from typing import Any

import yaml

from flotilla.config import FlotillaConfig
from flotilla.core import labels
from flotilla.core.anchor import Anchor
from flotilla.core.deletion import Deleter
from flotilla.platform.clock import SystemClock

REPORT_PATH = "/tmp/flotilla-compose-observed.json"
INTERPOLATION = {"CONTRACT_EXPLICIT": "explicit value"}
ENVIRONMENT = {
    "CONTRACT_LITERAL": "$HOME",
    "CONTRACT_BRACED": "$${DO_NOT_EXPAND}",
    "CONTRACT_DOTENV": "${DOT_LITERAL}",
    "CONTRACT_FILE": "${FILE_LITERAL}",
    "CONTRACT_RAW": "$${RAW_LITERAL}",
    "CONTRACT_EXPLICIT": "explicit value",
    "CONTRACT_FALLBACK": "fallback",
}
ARGV = ["two words", "", "$HOME", "$${DO_NOT_EXPAND}", "explicit value"]
# Child target intentionally precedes its parent in the source document.
BINDS = (
    ("overlay", "/data/nested", True),
    ("data", "/data", False),
    ("data", "/readonly", True),
    ("data/sub", "/descendant", False),
    ("probe.py", "/probe.py", True),
)
PROBE = """import errno
import json
import os
from pathlib import Path
import sys
import time


def readonly(path):
    try:
        Path(path).write_text("must-not-write")
    except OSError as error:
        return error.errno == errno.EROFS
    return False


Path("/data/marker").write_text("written-through-rw")
Path("/descendant/token").write_text("written-through-descendant")
result = {
    "argv": sys.argv[1:],
    "environment": {key: value for key, value in os.environ.items() if key.startswith("CONTRACT_")},
    "seed": Path("/data/seed").read_text(),
    "shared_rw_to_ro": Path("/readonly/marker").read_text(),
    "descendant_to_parent": Path("/data/sub/token").read_text(),
    "descendant_to_ro": Path("/readonly/sub/token").read_text(),
    "ro_write_rejected": readonly("/readonly/forbidden"),
    "nested_overlay": Path("/data/nested/value").read_text(),
    "parent_under_overlay": Path("/readonly/nested/value").read_text(),
    "nested_write_rejected": readonly("/data/nested/forbidden"),
}
output = Path("/tmp/flotilla-compose-observed.json")
output.with_suffix(".tmp").write_text(json.dumps(result))
output.with_suffix(".tmp").replace(output)
while True:
    time.sleep(3600)
"""
# Wait for the actual service-startup process, never substitute an exec probe for it.
READ_REPORT = (
    "import pathlib,time; p=pathlib.Path(" + repr(REPORT_PATH) + "); "
    "deadline=time.monotonic()+45\n"
    "while not p.exists() and time.monotonic()<deadline: time.sleep(.2)\n"
    "print(p.read_text())"
)


def compose_document(image: str) -> dict[str, Any]:
    return {
        "services": {
            "main": {
                "image": image,
                "entrypoint": ["python3", "-I", "/probe.py"],
                "command": ["two words", "", "$$HOME", "$$$${DO_NOT_EXPAND}", "${CONTRACT_EXPLICIT}"],
                "env_file": ["normal.env", {"path": "raw.env", "format": "raw"}],
                "environment": {
                    "CONTRACT_LITERAL": "$$HOME",
                    "CONTRACT_BRACED": "$$$${DO_NOT_EXPAND}",
                    "CONTRACT_DOTENV": "${DOT_LITERAL}",
                    "CONTRACT_EXPLICIT": "${CONTRACT_EXPLICIT}",
                    "CONTRACT_FALLBACK": "${CONTRACT_UNSET:-fallback}",
                },
                "volumes": [
                    {
                        "type": "bind",
                        "source": "./" + source,
                        "target": target,
                        "read_only": ro,
                        "bind": {"create_host_path": False},
                    }
                    for source, target, ro in BINDS
                ],
            }
        }
    }


def fixture_files(image: str) -> dict[str, bytes]:
    return {
        "task.toml": ("[environment]\ndocker_image = " + json.dumps(image) + "\n").encode(),
        "instruction.md": b"Observe the deterministic startup fixture; no agent action is required.\n",
        "tests/test.sh": b"#!/bin/sh\nexit 0\n",
        "environment/docker-compose.yaml": yaml.safe_dump(compose_document(image), sort_keys=False).encode(),
        "environment/.env": b"DOT_LITERAL='${DOT_LITERAL}'\n",
        "environment/normal.env": b"CONTRACT_FILE='${FILE_LITERAL}'\n",
        "environment/raw.env": b"CONTRACT_RAW=$${RAW_LITERAL}\n",
        "environment/data/seed": b"seed-content",
        "environment/data/sub/initial": b"sub-content",
        "environment/data/nested/value": b"underlying-parent",
        "environment/overlay/value": b"nested-overlay",
        "environment/probe.py": PROBE.encode(),
    }


def expected_observation() -> dict[str, Any]:
    return {
        "argv": ARGV,
        "environment": ENVIRONMENT,
        "seed": "seed-content",
        "shared_rw_to_ro": "written-through-rw",
        "descendant_to_parent": "written-through-descendant",
        "descendant_to_ro": "written-through-descendant",
        "ro_write_rejected": True,
        "nested_overlay": "nested-overlay",
        "parent_under_overlay": "underlying-parent",
        "nested_write_rejected": True,
    }


def docker_environment(ambient: Mapping[str, str]) -> dict[str, str]:
    # Preserve only Docker transport/auth-file selection, not ambient interpolation.
    keys = ("HOME", "DOCKER_HOST", "DOCKER_CONTEXT", "DOCKER_CONFIG", "DOCKER_TLS_VERIFY", "DOCKER_CERT_PATH")
    return {"PATH": os.defpath, **{key: ambient[key] for key in keys if key in ambient}, **INTERPOLATION}


async def command(argv: Sequence[str], env: Mapping[str, str], *, timeout: float = 180) -> bytes:
    proc = await asyncio.create_subprocess_exec(
        *argv, env=env, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE
    )
    try:
        stdout, _ = await asyncio.wait_for(proc.communicate(), timeout)
    except BaseException:
        if proc.returncode is None:
            proc.kill()
        await proc.wait()
        raise
    if proc.returncode:
        # Docker/remote diagnostics can include credentials. Do not copy them into reports.
        raise RuntimeError(f"command failed with exit status {proc.returncode}")
    return stdout


async def cleanup_docker(docker: str, project: str, env: Mapping[str, str]) -> bool:
    """Delete only this run's project-labelled resources, including partial startup."""
    selector = "label=com.docker.compose.project=" + project
    clean = True
    for kind in ("container", "network", "volume"):
        listing = [docker, kind, "ls", "-q", "--filter", selector]
        if kind == "container":
            listing.append("--all")
        try:
            ids = (await command(listing, env)).decode().split()
            if ids:
                removal = [docker, kind, "rm"]
                if kind != "network":
                    removal.append("--force")
                await command([*removal, *ids], env)
            clean = not (await command(listing, env)).strip() and clean
        except Exception:
            # Still attempt other resource types after a failed deletion.
            clean = False
    return clean


async def run(
    deployment: Path,
    output: Path,
    *,
    compose: Path,
    docker: str,
    image: str,
    local_root: Path,
) -> bool:
    from flotilla.integrations.harbor_attempt import HarborAttempt
    from flotilla.runtime.client import Flotilla

    cfg = FlotillaConfig.load(deployment)
    cfg.check_against(cfg.load_report())
    if "@sha256:" not in image:
        raise ValueError("--image must be an immutable Python 3 image reference with @sha256 digest")
    local_root = local_root.resolve(strict=True)
    env = docker_environment(os.environ)
    version = (await command([str(compose), "version", "--short"], env)).decode().strip().removeprefix("v")
    if version != cfg.prepare.compose_version:
        raise ValueError("local Compose version differs from deployment's required version")

    launch = "compose-runtime-" + uuid.uuid4().hex[:12]
    local = Path(tempfile.mkdtemp(prefix=launch + "-", dir=local_root))
    root = local / "environment"
    remote_key = "datasets/" + launch
    clock = SystemClock()
    platform = cfg.build_platform(clock=clock, caps=cfg.management_capabilities())
    anchor = Anchor(platform, clock, launch, cfg.anchor_settings(workspace=True))
    fleet = Flotilla(deployment=cfg)
    attempt: HarborAttempt | None = None
    result: dict[str, Any] = {
        "scope": "startup-argv-environment-and-bind-observations-only",
        "launch": launch,
        "docker_project": launch,
        "local_fixture": str(local),
        "image": image,
        "compose_version": version,
        "expected": expected_observation(),
        "observations": {},
        "checks": {},
        "cleanup": {},
    }
    started = False
    docker_started = False
    output.parent.mkdir(parents=True, exist_ok=True)

    async def cleanup(name: str, action: Callable[[], Awaitable[bool]]) -> None:
        try:
            result["cleanup"][name] = await action()
        except Exception as exc:
            result["cleanup"][name] = False
            result.setdefault("cleanup_errors", {})[name] = type(exc).__name__

    try:
        result["stage"] = "fixture"
        files = fixture_files(image)
        for name, data in files.items():
            path = local / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(data)
            path.chmod(0o644)
        baseline = root / "docker-compose.yaml"
        compose_args = [
            str(compose),
            "--project-directory",
            str(root),
            "--project-name",
            launch,
            "--env-file",
            str(root / ".env"),
            "-f",
            str(baseline),
        ]
        result["stage"] = "docker-start"
        docker_started = True  # up may create resources before returning an error.
        await command([*compose_args, "up", "--detach", "--no-build"], env)
        result["stage"] = "docker-observe"
        observed = await command([*compose_args, "exec", "-T", "main", "python3", "-I", "-c", READ_REPORT], env)
        result["observations"]["docker"] = json.loads(observed)
        result["checks"]["docker_expected"] = result["observations"]["docker"] == result["expected"]

        result["stage"] = "platform-fixture"
        await anchor.start()
        started = True
        for name, data in files.items():
            parent = Path(name).parent.as_posix()
            await anchor.mkdir(remote_key if parent == "." else remote_key + "/" + parent, stage="prepare")
            await anchor.upload(remote_key + "/" + name, data, mode=0o644, uid=0, gid=0, stage="prepare")
        result["stage"] = "harbor-prepare"
        attempt = await HarborAttempt.create(fleet, remote_key, runtime_params=INTERPOLATION)
        result["workspace_key"] = attempt.workspace.key
        result["stage"] = "harbor-open"
        main = await attempt.open()
        result["stage"] = "harbor-observe"
        observed_remote = await main.agent.exec(["python3", "-I", "-c", READ_REPORT], timeout=60)
        if observed_remote.exit_code != 0 or observed_remote.timed_out:
            raise RuntimeError("Harbor startup observation unavailable")
        result["observations"]["flotilla"] = json.loads(observed_remote.stdout)
        result["checks"]["flotilla_expected"] = result["observations"]["flotilla"] == result["expected"]
        result["checks"]["runtimes_equal"] = result["observations"]["docker"] == result["observations"]["flotilla"]
        result["stage"] = "compared"
    except Exception as exc:
        result["error_type"] = type(exc).__name__
        result["error_frames"] = [
            {"file": Path(frame.filename).name, "line": frame.lineno, "function": frame.name}
            for frame in traceback.extract_tb(exc.__traceback__)
        ]
    finally:

        async def close_attempt() -> bool:
            if attempt is None:
                return True
            await attempt.close()
            return not await anchor.exists(attempt.workspace.key, stage="prepare")

        async def close_fleet() -> bool:
            report = await fleet.close()
            result["fleet_launch"] = report.launch_id
            absent = not report.launch_id or not await platform.list({labels.LAUNCH: report.launch_id})
            return report.clean and absent

        async def remove_source() -> bool:
            if not started:
                return True
            await anchor.remove(remote_key, stage="prepare")
            await anchor.remove("launches/" + launch, stage="prepare")
            return not await anchor.exists(remote_key, stage="prepare") and not await anchor.exists(
                "launches/" + launch, stage="prepare"
            )

        async def close_anchor() -> bool:
            iid = anchor.iid
            await anchor.close()
            deleted = iid is None or await Deleter(platform, clock, retry_deadline_s=60).delete(iid)
            return deleted and not await platform.list({labels.LAUNCH: launch})

        await cleanup("attempt", close_attempt)
        await cleanup("fleet", close_fleet)
        await cleanup("source", remove_source)
        await cleanup("anchor", close_anchor)
        try:
            await platform.aclose()
        except Exception as exc:
            result["cleanup"]["platform_client"] = False
            result.setdefault("cleanup_errors", {})["platform_client"] = type(exc).__name__
        if docker_started:
            await cleanup("docker", lambda: cleanup_docker(docker, launch, env))
        try:
            # Keep bind sources if a possibly live Docker container could still use them.
            if result["cleanup"].get("docker", True):
                shutil.rmtree(local)
            result["cleanup"]["local"] = not local.exists()
        except Exception as exc:
            result["cleanup"]["local"] = False
            result.setdefault("cleanup_errors", {})["local"] = type(exc).__name__
        result["passed"] = (
            "error_type" not in result
            and len(result["checks"]) == 3
            and all(result["checks"].values())
            and all(result["cleanup"].values())
        )
        output.write_text(json.dumps(result, indent=2) + "\n")
    return bool(result["passed"])


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--deployment", type=Path, required=True)
    parser.add_argument("--out", type=Path, required=True)
    parser.add_argument("--compose", type=Path, required=True, help="installed official Compose executable")
    parser.add_argument("--docker", default="docker", help="Docker CLI executable (or an exec wrapper)")
    parser.add_argument("--image", required=True, help="immutable Python 3 image, including @sha256 digest")
    parser.add_argument("--local-root", type=Path, required=True, help="scratch parent at the same path on daemon host")
    args = parser.parse_args()
    passed = asyncio.run(
        run(
            args.deployment,
            args.out,
            compose=args.compose,
            docker=args.docker,
            image=args.image,
            local_root=args.local_root,
        )
    )
    print(f"compose runtime contract: {'passed' if passed else 'failed'}; report: {args.out}")
    raise SystemExit(0 if passed else 1)


if __name__ == "__main__":
    main()
