"""Copy a platform-only image into a dev-reachable registry via a kata transfer sandbox.

When the dev box cannot reach the registry a platform node can pull from (no credentials),
flotilla cannot digest-resolve those images locally. Pipeline: raw-API kata sandbox (the
platform pulls the image itself) -> docker pull + save|gzip + split inside -> execd
read_file chunks out -> dev box docker load -> retag -> push. Driven through flotilla's own
execd/Http plumbing (what the bare arm reuses).

The client generates the execd token and injects it via the create request env; it is kept
in memory and sent as a header, never logged.
"""

from __future__ import annotations

import argparse
import asyncio
import os
import secrets
import subprocess
import sys
import time
from pathlib import Path

import httpx

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from flotilla.platform.base import FlotillaError, ProcessSpec
from flotilla.platform.clock import SystemClock
from flotilla.platform.opensandbox.execd import Execd, ExecdSettings
from flotilla.platform.opensandbox.http import Http

from loadtest.creds import env_path, read_env_file_values

PART_BYTES = 64 * 1024 * 1024


def pull_save_script(image: str) -> str:
    # Absolute docker path: legacy exec wraps with env -i, so PATH lacks /usr/local/bin.
    docker = "/usr/local/bin/docker"
    return (
        "set -e; "
        f"{docker} pull {image} >/dev/null && "
        f"{docker} save {image} | gzip > /root/dind.tar.gz && "
        f"split -b {mb}m /root/dind.tar.gz /root/part. && "
        "ls -l /root/dind.tar.gz /root/part.* && df -h /root | tail -1"
    ).format(image=image, mb=PART_BYTES // (1024 * 1024))


def log(message: str) -> None:
    print(message, flush=True)


async def create_sandbox(client: httpx.AsyncClient, endpoint: str, project: str, image: str) -> tuple[str, str]:
    # The client generates the execd token and injects it via the create request env (flotilla's
    # mechanism: EXECD_ACCESS_TOKEN is the env var execd reads; the platform never echoes it back).
    token = secrets.token_urlsafe(32)
    body = {
        "image": {"uri": image},
        "entrypoint": ["sleep", "7200"],
        "timeout": 7200,
        "resourceRequests": {"cpu": "4", "memory": "8Gi"},
        "env": {"EXECD_ACCESS_TOKEN": token},
        "extensions": {"project": project, "runtimeClassName": "kata"},
    }
    response = await client.post(f"{endpoint}/v1/sandboxes", json=body)
    response.raise_for_status()
    record = response.json()
    if not record.get("id"):
        raise RuntimeError("create response missing id")
    return str(record["id"]), token


async def wait_running(client: httpx.AsyncClient, endpoint: str, sandbox_id: str, timeout_s: float = 600) -> None:
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        response = await client.get(f"{endpoint}/v1/sandboxes/{sandbox_id}")
        response.raise_for_status()
        state = response.json().get("state")
        if state == "running":
            return
        if state in ("failed", "terminated"):
            raise RuntimeError(f"sandbox reached {state} before running")
        await asyncio.sleep(3)
    raise TimeoutError(f"sandbox not running after {timeout_s}s")


def sh(*args: str, check: bool = True) -> subprocess.CompletedProcess:
    result = subprocess.run(args, capture_output=True, text=True)
    if check and result.returncode != 0:
        raise RuntimeError(f"{args[0]} failed: {result.stderr.strip()[-400:]}")
    return result


async def wait_execd(http: Http, timeout_s: float = 180) -> None:
    """Poll execd /ping until it answers; the platform injects it after the sandbox is running."""
    deadline = time.monotonic() + timeout_s
    began = time.monotonic()
    while time.monotonic() < deadline:
        try:
            await http.request("GET", "/ping", stage="run", idempotent=True)
            log(f"execd ready {time.monotonic() - began:.0f}s after running")
            return
        except FlotillaError:
            await asyncio.sleep(2)
    raise TimeoutError(f"execd not ready after {timeout_s}s")


async def main(args: argparse.Namespace) -> int:
    values = read_env_file_values(args.secret, ("OPEN_SANDBOX_ACCESS_KEY", "OPEN_SANDBOX_SECRET_KEY"))
    credential = values["OPEN_SANDBOX_ACCESS_KEY"] + ":" + values["OPEN_SANDBOX_SECRET_KEY"]
    endpoint = args.endpoint.rstrip("/")

    client = httpx.AsyncClient(base_url=endpoint, headers={"OPEN-SANDBOX-API-KEY": credential}, timeout=60)
    sandbox_id = token = ""
    try:
        began = time.monotonic()
        sandbox_id, token = await create_sandbox(client, endpoint, args.project, args.image)
        log(f"transfer sandbox {sandbox_id} created")
        await wait_running(client, endpoint, sandbox_id)
        log(f"sandbox running in {time.monotonic() - began:.0f}s")

        execd_http = Http(
            client,
            SystemClock(),
            prefix=f"/v1/sandboxes/{sandbox_id}/proxy/44772",
            headers={"X-EXECD-ACCESS-TOKEN": token},
        )
        await wait_execd(execd_http)
        execd = Execd(
            execd_http,
            ExecdSettings(protocol="legacy", wrapper=args.wrapper),
        )

        probe = await execd.exec(
            ProcessSpec(argv=[args.wrapper, "echo", "wrapper-ok"], uid=0, gid=0, cwd="/", env={}, timeout_s=60)
        )
        log(f"wrapper probe exit={probe.exit_code} out={probe.stdout.decode().strip()!r}")
        if probe.exit_code != 0:
            log("wrapper missing in dind image; pass --wrapper with a busybox path present in the image")
            return 2

        began = time.monotonic()
        result = await execd.exec(
            ProcessSpec(argv=["/bin/sh", "-c", pull_save_script(args.image)], uid=0, gid=0, cwd="/", env={}, timeout_s=2400)
        )
        listing = result.stdout.decode(errors="replace").strip()
        log(f"pull+save+split exit={result.exit_code} in {time.monotonic() - began:.0f}s")
        log(listing[-800:])
        if result.exit_code != 0:
            log("stderr tail: " + result.stderr.decode(errors="replace").strip()[-400:])
            return 3
        parts = sorted(line.split()[-1] for line in listing.splitlines() if line.startswith("-") and "/root/part." in line)
        log(f"{len(parts)} chunk(s) to transfer")

        args.out_dir.mkdir(parents=True, exist_ok=True)
        archive = args.out_dir / "dind.tar.gz"
        began = time.monotonic()
        with archive.open("wb") as stream:
            for part in parts:
                stream.write(await execd.read_file(part))
        size_mb = archive.stat().st_size / 1024 / 1024
        log(f"transferred {size_mb:.0f} MiB in {time.monotonic() - began:.0f}s -> {archive}")
    finally:
        if sandbox_id:
            await client.delete(f"{endpoint}/v1/sandboxes/{sandbox_id}")
            log(f"transfer sandbox {sandbox_id} deleted")
        await client.aclose()

    load = sh("sudo", "-n", "docker", "load", "--input", str(archive))
    log("docker load: " + load.stdout.strip())
    sh("sudo", "-n", "docker", "tag", args.image, args.target_tag)
    began = time.monotonic()
    push = sh("sudo", "-n", "docker", "push", args.target_tag)
    log(f"pushed {args.target_tag} in {time.monotonic() - began:.0f}s")
    digest = sh("sudo", "-n", "docker", "manifest", "inspect", args.target_tag, check=False)
    log("manifest inspect exit=" + str(digest.returncode))
    archive.unlink(missing_ok=True)
    log("DONE")
    return 0


def parse_args(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--secret", type=Path, default=env_path("OPENSANDBOX_SECRET_FILE"))
    parser.add_argument("--endpoint", default=os.environ.get("OPENSANDBOX_ENDPOINT"))
    parser.add_argument("--project", default=os.environ.get("OPENSANDBOX_PROJECT"))
    parser.add_argument("--image", required=True, help="source image (platform-side pullable)")
    parser.add_argument("--target-tag", required=True, help="destination image ref to push")
    parser.add_argument("--wrapper", default="/bin/busybox")
    parser.add_argument("--out-dir", type=Path, default=Path("build") / "dind-mirror")
    return parser.parse_args(argv)


if __name__ == "__main__":
    sys.exit(asyncio.run(main(parse_args(sys.argv[1:]))))
