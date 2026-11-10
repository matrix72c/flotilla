"""Run in the built Anchor with no network, mounts, or host environment."""

from __future__ import annotations

import hashlib
import importlib.metadata
import importlib.util
import json
import os
import platform
import shutil
import subprocess
import sys
from pathlib import Path
from tempfile import TemporaryDirectory


def main() -> None:
    if sys.version_info[:2] != (3, 12) or sys.platform != "linux" or platform.machine() != "x86_64":
        raise RuntimeError("Anchor requires Linux amd64 / Python 3.12")
    snapshot = json.loads(Path("/opt/flotilla/snapshot.json").read_text())
    for name, version in snapshot["distributions"].items():
        if importlib.metadata.version(name) != version:
            raise RuntimeError(f"Anchor distribution version mismatch: {name}")
    root = Path("/opt/flotilla/lib/python3.12/site-packages")
    for rel, digest in snapshot["files"].items():
        if hashlib.sha256((root / rel).read_bytes()).hexdigest() != digest:
            raise RuntimeError(f"Anchor installed content mismatch: {rel}")
    for module in ("harbor", "torch"):
        if importlib.util.find_spec(module) is not None:
            raise RuntimeError(f"Anchor unexpectedly includes {module}")
    for command in ("docker", "dockerd", "docker-compose", "buildctl", "buildkitd", "skopeo"):
        if shutil.which(command):
            raise RuntimeError(f"Anchor base unexpectedly includes build tool {command}")
    busybox = "/.flotilla/bin/busybox"
    applets = subprocess.check_output([busybox, "--list"], text=True).splitlines()
    required = {"sh", "env", "ip", "chown", "chmod", "mkdir", "rm", "ls", "stat", "tar", "cp", "sleep"}
    if missing := required - set(applets):
        raise RuntimeError(f"Anchor BusyBox lacks applets: {sorted(missing)}")
    # Exercise file/archive helpers, not just binary presence or --help.
    with TemporaryDirectory(prefix="anchor-check-") as directory:
        work = Path(directory)
        (work / "source").mkdir()
        (work / "source/payload").write_text("anchor self-containment\n")
        subprocess.run([busybox, "tar", "-cf", str(work / "payload.tar"), "-C", str(work / "source"), "."], check=True)
        (work / "destination").mkdir()
        subprocess.run([busybox, "tar", "-xf", str(work / "payload.tar"), "-C", str(work / "destination")], check=True)
        if (work / "destination/payload").read_bytes() != (work / "source/payload").read_bytes():
            raise RuntimeError("Anchor archive round trip failed")
    # Test the actual platform helper with execd's cleared environment / PATH.
    subprocess.run(
        ["/usr/bin/python3", "-I", "-m", "flotilla.native.platform_verify", "--self-test"],
        check=True,
        env={"PATH": "/usr/sbin:/usr/bin:/sbin:/bin"},
        cwd="/",
    )
    # Import the runtime/API without the Harbor extra; this also loads native
    # extension dependencies, detecting an incompatible libc in the pinned base.
    subprocess.run(
        ["/usr/bin/python3", "-I", "-c", "import flotilla; import flotilla.runtime; import yaml; import httpx"],
        check=True,
        env={"PATH": "/usr/sbin:/usr/bin:/sbin:/bin"},
        cwd="/",
    )
    if os.getuid() != 0:
        raise RuntimeError("Anchor management identity must default to root")
    print(json.dumps({"anchor_check": "ok", "distributions": snapshot["distributions"]}, sort_keys=True))


if __name__ == "__main__":
    main()
