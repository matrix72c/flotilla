"""Opt-in local Docker archive metadata acceptance; uses only an owned scratch image."""

from __future__ import annotations

import argparse
import json
import subprocess
import tempfile
import uuid
from pathlib import Path

from flotilla.build.docker import DockerImageExport
from flotilla.build.keys import entries


def run(command: list[str]) -> None:
    image = f"flotilla-metadata-test:{uuid.uuid4().hex}"
    built = False
    with tempfile.TemporaryDirectory(prefix="flotilla-docker-contract-") as directory:
        root = Path(directory)
        (root / "payload").write_bytes(b"numeric ownership survives host-independent extraction\n")
        (root / "Dockerfile").write_text(
            'FROM scratch\nCOPY --chown=12345:23456 --chmod=6751 payload /data/payload\nCMD ["/bin/true"]\n'
        )
        try:
            build = subprocess.run(
                [*command, "build", "--network=none", "--pull=false", "-t", image, str(root)],
                capture_output=True,
                timeout=120,
                check=False,
            )
            if build.returncode:
                raise RuntimeError("scratch fixture build failed")
            built = True
            files = root / "files"
            files.mkdir()
            identities: dict[str, tuple[int, int, int]] = {}
            assert DockerImageExport(docker_cmd=command).extract(image, "/data", files, identities=identities)
            (root / "file-identities.json").write_text(json.dumps(identities))
            record = next(item for item in entries(files) if item["path"] == "payload")
            assert (record["uid"], record["gid"], record["mode"]) == (12345, 23456, "6751"), record
            assert (files / "payload").read_bytes() == (root / "payload").read_bytes()
            assert (files / "payload").stat().st_uid != 12345
            print("Docker numeric uid/gid/mode and byte-content acceptance: PASS")
        finally:
            if built:
                result = subprocess.run([*command, "image", "rm", image], capture_output=True, timeout=30, check=False)
                if result.returncode:
                    raise RuntimeError("owned fixture image cleanup failed")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--sudo", action="store_true", help="use noninteractive sudo for the local Docker socket")
    args = parser.parse_args()
    run(["sudo", "-n", "docker"] if args.sudo else ["docker"])
