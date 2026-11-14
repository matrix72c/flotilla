"""Compare folded Dockerfile configuration with real Docker builds.

Builds each config-only case on a local base image with the Docker daemon's builder (no push, no
pull when the base is present), then compares Env, WorkingDir, User, Cmd and Entrypoint with
`flotilla.build.dockerfile.fold`. Built images are removed afterwards.

    python -m tests.contract.dockerfile_fold --base IMAGE [--docker "sudo docker"]
"""

from __future__ import annotations

import argparse
import json
import shlex
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Any

from flotilla.build.dockerfile import fold

FIELDS = ("Env", "WorkingDir", "User", "Cmd", "Entrypoint")
CASES: tuple[tuple[str, dict[str, str]], ...] = (
    ("WORKDIR {workdir}\n", {}),
    ('ENV A=1 B=two\nENV B="$A $B" C=${{A:-x}} D=${{MISSING:-dflt}} E=${{A:+alt}}\n', {}),
    ('ENV GREETING hello  world\nENV Q=\'$A\' R="a\\"b" S=a\\ b\n', {}),
    ("ARG X\nARG Y=dflt\nENV X=$X Y=${{Y}}\n", {"X": "given"}),
    ("ENV LANG=fold\nWORKDIR /\nUSER 1234:5678\n", {}),
    ('CMD ["run", "x"]\nENTRYPOINT /entry --flag\n', {}),
    ('ENTRYPOINT ["/e", "arg"]\n', {}),
    ("CMD echo shell form\n", {}),
    ("LABEL a=$UNSET\nEXPOSE 80/udp\nSTOPSIGNAL SIGTERM\nENV A=1 \\\n# note\n    B=2\n", {}),
)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--base", required=True, help="local base image (name or ID)")
    parser.add_argument("--docker", default="docker", help="Docker command, parsed as argv")
    options = parser.parse_args()
    docker = shlex.split(options.docker)

    def run(*args: str) -> str:
        return subprocess.run([*docker, *args], check=True, capture_output=True, text=True).stdout

    base: dict[str, Any] = json.loads(run("image", "inspect", options.base))[0]["Config"]
    workdir = base.get("WorkingDir") or "/"
    failures = 0
    built: list[str] = []
    try:
        for index, (body, args) in enumerate(CASES):
            text = f"FROM {options.base}\n" + body.format(workdir=workdir)
            tag = f"flotilla-fold-check:{index}"
            with tempfile.TemporaryDirectory() as context:
                (Path(context) / "Dockerfile").write_text(text)
                build_args = [item for key, value in args.items() for item in ("--build-arg", f"{key}={value}")]
                run("build", "--quiet", "--tag", tag, *build_args, context)
            built.append(tag)
            actual = json.loads(run("image", "inspect", tag))[0]["Config"]
            expected = fold(text, base, args)
            diff = {
                field: (expected.get(field) or None, actual.get(field) or None)
                for field in FIELDS
                if (expected.get(field) or None) != (actual.get(field) or None)
            }
            print(json.dumps({"case": index, "ok": not diff, "diff": diff}))
            failures += bool(diff)
    finally:
        if built:
            subprocess.run([*docker, "image", "rm", *built], capture_output=True, check=False)
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
