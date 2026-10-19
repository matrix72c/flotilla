"""Inspect built distributions. Prints paths/categories, never matched secret values."""

from __future__ import annotations

import argparse
import re
import tarfile
import zipfile
from pathlib import Path, PurePosixPath

FORBIDDEN = {"internal", "trials", ".env", ".aws", ".codex", ".agents", "__pycache__", "artifacts"}
PATTERNS = {
    "private key": re.compile(rb"-----BEGIN (?:RSA |EC |OPENSSH )?PRIVATE KEY-----"),
    "AWS access key": re.compile(rb"AKIA[A-Z0-9]{16}"),
    "personal absolute path": re.compile(rb"/(?:home|Users)/[a-zA-Z][a-zA-Z0-9_-]{2,}/"),
    "private workspace path": re.compile(rb"/mnt/" + rb"shared-storage-[^\s\"']+"),
}


def inspect_member(name: str, data: bytes, *, wheel: bool) -> list[str]:
    path = PurePosixPath(name)
    problems = []
    if any(part in FORBIDDEN for part in path.parts) or "deployments/local" in name:
        problems.append("excluded path")
    if wheel and ("tests" in path.parts or name.endswith(("platform/fake.py", "integrations/xtuner.py"))):
        problems.append("non-runtime module in wheel")
    # Scanner fixtures are source code; review literal matches rather than echoing them.
    for category, pattern in PATTERNS.items():
        if pattern.search(data):
            problems.append(category)
    return [f"{name}: {problem}" for problem in problems]


def check(path: Path) -> list[str]:
    if path.suffix == ".whl":
        with zipfile.ZipFile(path) as archive:
            return [p for name in archive.namelist() for p in inspect_member(name, archive.read(name), wheel=True)]
    with tarfile.open(path) as archive:
        problems = []
        for member in archive:
            if member.isfile():
                source = archive.extractfile(member)
                assert source is not None
                problems.extend(inspect_member(member.name, source.read(), wheel=False))
        return problems


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("directory", type=Path)
    args = parser.parse_args()
    paths = sorted([*args.directory.glob("*.whl"), *args.directory.glob("*.tar.gz")])
    if not any(p.suffix == ".whl" for p in paths) or not any(p.name.endswith(".tar.gz") for p in paths):
        parser.error("both a wheel and a source distribution are required")
    problems = [f"{path.name}: {problem}" for path in paths for problem in check(path)]
    for problem in problems:
        print(problem)
    if problems:
        raise SystemExit(1)
    print(f"Checked {len(paths)} distributions; no excluded files or configured sensitive patterns found.")
    print("Pattern checks supplement, and do not replace, manual snapshot/image-context review.")


if __name__ == "__main__":
    main()
